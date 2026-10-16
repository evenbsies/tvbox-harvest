"""The four-tier detection state machine.

Probing runs cheapest-first (T1 -> T4) and stops at the first tier that proves it can do
the job. The result is not just a tier number: it is a *patch* to apply to the site recipe
(so next run skips detection) plus the evidence that justifies it.

Two properties matter more than cleverness here:

* **Explainability.** Every stage records what it saw. When a site changes shape, the diff
  in ``detect.evidence`` tells you exactly which signal moved.
* **Cheapness ordering.** A Tier-1 site is identified in ~3 requests; a Tier-3 site is
  identified in ~2; only the genuinely static-address-free sites pay for a browser launch.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from harvest.config import Settings, get_settings
from harvest.fetch.http import HttpClient, RawRecorder, is_html_response, looks_like_json, safe_json
from harvest.models import ProbeResult, SiteConfig
from harvest.probe import signals
from harvest.probe.evidence import Evidence

STANDARD_MACCMS_PATHS = [
    "/api.php/provide/vod/",
    "/api.php/provide/vod/at/json/",
    "/api.php/provide/vod",
    "/api/provide/vod/",
]

MAX_JSON_CANDIDATES = 12
DETAIL_LINK_THRESHOLD = 20


@dataclass
class ProbeContext:
    settings: Settings
    http: HttpClient
    recorder: RawRecorder | None = None
    force_tier: int | None = None
    allow_browser: bool = True


async def detect(site: SiteConfig, ctx: ProbeContext) -> ProbeResult:
    ev = Evidence()
    base = site.base_url
    if not base.startswith(("http://", "https://")):
        base = "https://" + base

    home = None
    try:
        home = await ctx.http.get(
            base + "/", site=site, tolerate=(403, 404), record_name="homepage"
        )
    except Exception as exc:
        ev.note(f"homepage fetch failed: {exc}")

    body = home.text if home is not None else ""
    headers = dict(home.headers) if home is not None else {}

    # ---- stage 0: preflight signals --------------------------------------------------
    robots = None
    try:
        robots = await ctx.http.get(f"{base}/robots.txt", site=site, tolerate=(403, 404))
    except Exception:
        pass

    # The Go-404 fingerprint is the single cheapest way to rule out PHP/MacCMS, so look for it
    # in every preflight body rather than only the homepage (where a 200 is expected).
    go404_samples = [
        name
        for name, resp in (("homepage", home), ("robots", robots))
        if resp is not None and resp.status_code == 404 and signals.GO_DEFAULT_404 in resp.text[:200]
    ]

    ev.stage(
        "preflight",
        status=getattr(home, "status_code", None),
        server=headers.get("server"),
        robots_status=getattr(robots, "status_code", None),
        go_default_404=bool(go404_samples),
    )
    ev.fact(
        server=headers.get("server", ""),
        html_bytes=len(body),
        is_html=signals.looks_like_html(body),
        maccms_markers=signals.maccms_markers(body)[:6],
        detail_link_clusters=[
            {"pattern": c.pattern, "count": c.count}
            for c in signals.cluster_detail_links(body, base)[:8]
        ],
    )

    # ---- stage 1: Tier 1 (standard MacCMS interface) ---------------------------------
    if ctx.force_tier in (None, 1):
        tier1 = await _probe_tier1(base, site, ctx, ev, body)
        if tier1 is not None:
            return tier1

    # ---- stage 2: Tier 2 (custom JSON/XML interface) ---------------------------------
    if ctx.force_tier in (None, 2):
        tier2 = await _probe_tier2(base, site, ctx, ev, body)
        if tier2 is not None:
            return tier2

    # ---- stage 3: Tier 3 (static HTML with embedded media URLs) ----------------------
    # Only meaningful if the homepage is actually a document. An API-only domain answers `/`
    # with a JSON video list, and running the link-clustering heuristics over that JSON would
    # produce a confident -- and wrong -- Tier-3 verdict.
    home_is_html = home is not None and is_html_response(home)
    clusters = signals.cluster_detail_links(body, base) if home_is_html else []
    sample_url: str | None = None
    if clusters and clusters[0].dominant:
        sample_url = clusters[0].samples[0] if clusters[0].samples else None

    player_markers: list[str] = []
    detail_html = ""
    detail_error: str | None = None
    if sample_url:
        try:
            detail = await ctx.http.get(sample_url, site=site, record_name="detail_sample")
            detail_html = detail.text
        except Exception as exc:
            detail_error = f"{type(exc).__name__}: {exc}"[:200]
            ev.note(f"detail fetch failed: {detail_error}")

        media_urls = signals.extract_m3u8_urls(detail_html)
        from harvest.adapters.extractors import try_extract_episodes

        candidates = try_extract_episodes(detail_html, site.selectors)
        player_markers = signals.has_player_container(detail_html)

        ev.stage(
            "tier3_probe",
            sample=sample_url,
            detail_error=detail_error,
            static_m3u8=len(media_urls),
            extracted_episodes=len(candidates),
            # The counts are recorded alongside the lists because empty lists are dropped from
            # the evidence for readability -- and "player markers: 0" is exactly the signal
            # that separates a Tier-4 site from an unreachable one.
            player_marker_count=len(player_markers),
            player_markers=player_markers[:6],
        )
        ev.fact(
            detail_html_bytes=len(detail_html),
            detail_sample_url=sample_url,
            detail_cluster=clusters[0].pattern if clusters else None,
        )

        if (media_urls or candidates) and ctx.force_tier in (None, 3):
            ev.note("media URLs are present in static HTML")
            return ProbeResult(
                tier=3,
                adapter="html_ssr",
                status="ok",
                evidence=ev.as_dict(),
                patch={"selectors": _suggest_selectors(clusters, detail_html, media_urls)},
                detail_url_sample=sample_url,
            )

        if player_markers and not media_urls:
            ev.note("player container present but zero static media URLs -> runtime-only")
    elif not home_is_html:
        ev.stage("homepage_not_html", note="skipping HTML-oriented stages")
        ev.note("homepage is not an HTML document; Tier 3/4 heuristics not applicable")
    elif signals.is_spa_skeleton(body):
        ev.stage("spa_skeleton_detected")
        ev.note("SPA shell: catalogue rendered client-side")
    else:
        ev.stage("no_dominant_detail_cluster", clusters=len(clusters))

    # ---- stage 4: Tier 4 (browser network sniffing) ---------------------------------
    if ctx.force_tier in (None, 4) and ctx.allow_browser:
        tier4 = await _probe_tier4(base, site, ctx, ev, sample_url)
        if tier4 is not None:
            return tier4

    # ---- indeterminate ---------------------------------------------------------------
    ev.stage("indeterminate")
    return ProbeResult(
        tier=0,
        adapter="auto",
        status="unknown",
        evidence=ev.as_dict(),
        detail_url_sample=sample_url,
    )


# --------------------------------------------------------------------------------------
# Tier probes
# --------------------------------------------------------------------------------------


async def _probe_tier1(
    base: str, site: SiteConfig, ctx: ProbeContext, ev: Evidence, home_body: str
) -> ProbeResult | None:
    candidates: list[str] = []
    for path in signals.extract_api_hints(home_body):
        if path not in candidates:
            candidates.append(path)
    for path in STANDARD_MACCMS_PATHS:
        if path not in candidates:
            candidates.append(path)

    tried: list[dict[str, object]] = []
    for path in candidates[:8]:
        api_url = urljoin(base + "/", path.lstrip("/"))
        try:
            resp = await ctx.http.get(
                api_url, site=site, params={"ac": "list", "pg": "1"}, tolerate=(403, 404, 500)
            )
        except Exception as exc:
            tried.append({"path": path, "error": str(exc)[:80]})
            continue

        if resp.status_code != 200 or not looks_like_json(resp):
            tried.append({"path": path, "status": resp.status_code, "json": False})
            continue

        payload = safe_json(resp)
        if not signals.is_maccms_class_list(payload):
            tried.append({"path": path, "status": resp.status_code, "maccms_shape": False})
            continue

        items = payload.get("list") or []
        detail_ok = True
        sample_sid = None
        if items and isinstance(items[0], dict):
            sample_sid = str(items[0].get("vod_id") or "")
            if sample_sid:
                try:
                    dresp = await ctx.http.get(
                        api_url, site=site, params={"ac": "detail", "ids": sample_sid}, tolerate=(403, 404)
                    )
                    detail_ok = signals.is_maccms_detail(safe_json(dresp))
                except Exception:
                    detail_ok = False

        ev.stage(
            "tier1_match",
            path=path,
            trailing_slash=path.endswith("/"),
            total=payload.get("total"),
            class_entries=len(payload.get("class") or []),
            sample_id=sample_sid,
            detail_verified=detail_ok,
        )
        return ProbeResult(
            tier=1,
            adapter="maccms_v10",
            status="ok",
            evidence=ev.as_dict(),
            patch={
                "maccms": {
                    "api_path": path if path.endswith("/") else path + "/",
                    "categories_all": bool(payload.get("class")),
                },
                "field_hint": {
                    "total": payload.get("total"),
                    "class_count": len(payload.get("class") or []),
                },
            },
        )

    ev.stage("tier1_no_match", tried=tried[:8])
    return None


async def _probe_tier2(
    base: str, site: SiteConfig, ctx: ProbeContext, ev: Evidence, home_body: str
) -> ProbeResult | None:
    candidates: list[str] = []
    for extra in (site.probe or {}).get("paths") or []:
        candidates.append(str(extra))
    for path in signals.extract_json_candidates(home_body):
        if path not in candidates:
            candidates.append(path)

    scored: list[dict[str, object]] = []
    for path in candidates[:MAX_JSON_CANDIDATES]:
        url = path if path.startswith("http") else urljoin(base + "/", path.lstrip("/"))
        try:
            resp = await ctx.http.get(url, site=site, tolerate=(403, 404, 500))
        except Exception:
            continue
        if resp.status_code != 200 or not looks_like_json(resp):
            continue

        payload = safe_json(resp)
        if payload is None:
            continue

        # A MacCMS-shaped payload that Tier 1 rejected (e.g. non-standard path) is still
        # usable via json_custom, so we score it rather than discarding it.
        score = signals.field_fingerprint(payload)
        scored.append(
            {
                "url": url,
                "score": score.score,
                "strong": score.strong_fields[:6],
                "paths": list(score.sample_paths)[:12],
            }
        )
        ev.stage("tier2_candidate", url=url, score=score.score, strong=score.strong_fields[:4])

    if not scored:
        ev.stage("tier2_no_match", candidates_tried=len(candidates[:MAX_JSON_CANDIDATES]))
        return None

    best = max(scored, key=lambda s: int(s["score"]))  # type: ignore[arg-type]
    if int(best["score"]) < 6 and not best["strong"]:
        ev.stage("tier2_below_threshold", best_score=best["score"])
        return None

    has_mapping = bool(site.fields)
    ev.stage("tier2_match", url=best["url"], score=best["score"], needs_mapping=not has_mapping)
    return ProbeResult(
        tier=2,
        adapter="json_custom",
        status="ok" if has_mapping else "needs_mapping",
        evidence=ev.as_dict(),
        patch={
            "api": best["url"],
            **({} if has_mapping else {"field_candidates": best["paths"]}),
        },
    )


async def _probe_tier4(
    base: str, site: SiteConfig, ctx: ProbeContext, ev: Evidence, sample_url: str | None
) -> ProbeResult | None:
    if not sample_url:
        ev.stage("tier4_skipped", reason="no detail url sample")
        return None

    browser_cfg = site.browser or {}
    sniff_patterns = browser_cfg.get("sniff_patterns") or [r"\.m3u8", r"/m3u8/"]
    click_selector = browser_cfg.get("click_selector")
    read_globals = browser_cfg.get("read_globals") or []
    capture_body_patterns = browser_cfg.get("capture_body_patterns") or []
    timeout = float(browser_cfg.get("per_title_timeout") or 40)

    try:
        from harvest.fetch.browser import BrowserSniffer
    except Exception as exc:  # playwright not installed
        ev.stage("tier4_unavailable", error=str(exc)[:120])
        return None

    try:
        async with BrowserSniffer() as sniffer:
            result = await sniffer.sniff(
                sample_url,
                sniff_patterns=list(sniff_patterns),
                click_selector=click_selector,
                click_episode=1,
                read_globals=list(read_globals),
                wait_until=str(browser_cfg.get("wait_until") or "domcontentloaded"),
                timeout=timeout,
                capture_body_patterns=list(capture_body_patterns),
            )
    except Exception as exc:
        ev.stage("tier4_error", error=f"{type(exc).__name__}: {exc}"[:200])
        return None

    ev.stage("tier4_sniff", **result.as_evidence())

    if not result.ok:
        ev.note("browser sniff produced no media URL")
        return None

    patch: dict[str, object] = {}
    decoded = result.globals.get("_decoded_hosts")
    if isinstance(decoded, list) and decoded:
        patch["browser"] = {"fallback_hosts": decoded}

    from harvest.clean.normalize import host_of

    primary_host = host_of(result.media_urls[0])
    ev.note(f"primary media host: {primary_host}")

    return ProbeResult(
        tier=4,
        adapter="playwright_sniff",
        status="ok",
        evidence=ev.as_dict(),
        patch=patch,
        detail_url_sample=sample_url,
    )


# --------------------------------------------------------------------------------------
# Recipe suggestions
# --------------------------------------------------------------------------------------


def _suggest_selectors(clusters, detail_html: str, media_urls: list[str]) -> dict[str, object]:
    """Emit a *starting point* recipe for a Tier-3 site.

    This is intentionally shallow -- it gives whoever adds the site something that runs,
    with the cluster pattern and any literal media URLs already filled in. Real selector
    tuning stays a human step; guessing deep selectors automatically produces recipes that
    look plausible and return nothing.
    """
    selectors: dict[str, object] = {}
    if clusters:
        top = clusters[0]
        selectors["list"] = {
            "url": top.pattern.replace("{n}", "{page}").replace("{id}", "{page}"),
            "item": "a",
            "value": "::attr(href)",
            "cluster_pattern": top.pattern,
        }
    if media_urls:
        selectors["detail"] = {
            "episodes": [
                {"type": "regex", "pattern": r"""["']([^"']{0,400}\.m3u8[^"']{0,200})["']""", "limit": 200}
            ]
        }
    return selectors


def normalize_base_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def site_from_url(url: str, key: str | None = None, adapter: str = "auto") -> SiteConfig:
    base = normalize_base_url(url)
    host = urlparse(base).hostname or "site"
    default_key = host.replace("www.", "").split(".")[0] or "site"
    return SiteConfig(
        key=key or default_key,
        name=key or default_key,
        base_url=base,
        adapter=adapter,  # type: ignore[arg-type]
        request={"headers": {"User-Agent": signals_ua(), "Referer": base + "/"}},  # type: ignore[arg-type]
    )


def signals_ua() -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )


def default_context(http: HttpClient, settings: Settings | None = None, **kwargs) -> ProbeContext:
    return ProbeContext(settings=settings or get_settings(), http=http, **kwargs)
