"""Measure a candidate site instead of recommending one.

"Fast, HD, no ads" are the three things everyone wants from a source and nobody defines. This
turns each into a number with the method attached, so a comparison rests on evidence rather than
impression:

* **speed** -- wall-clock per collected title, requests and bytes per title, retry and error
  counts. A source that answers in 40ms but needs four requests per title is slower than one that
  answers in 80ms and needs one.
* **HD** -- the share of sampled titles whose remarks or name advertise 1080/2160/4K/UHD/蓝光.
  Read from what the source says about itself, which is the only thing available before playback.
* **ads** -- gambling-domain hits and site-level advertising signals, plus the share of titles the
  adult filter removed. Also counts *promotion parameters* (a `?from=bet365` style tail), which is
  how a legitimate-looking media host still carries advertising.
* **quality** -- catalogue size, category count, playable ratio, lines per title, and how much of
  the catalogue was updated recently.

Two deliberate omissions:

* **No composite score.** A single number would hide the weighting, and the weighting is a
  judgement about what the operator is willing to trade. The numbers are reported; the ranking is
  theirs to make.
* **"Not measured" is distinct from zero.** A site that could not be probed, or a Tier 4 site
  measured without a browser, leaves the affected metrics as ``None`` and says so. Reporting 0
  would read as "it has no ads" and "it has no HD", which is the opposite of "we did not look".
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

from harvest.clean.dedupe import clean_site_data
from harvest.clean.gambling import scan_site_html
from harvest.config import Settings, get_settings
from harvest.fetch.http import HttpClient
from harvest.models import SiteConfig
from harvest.pipeline import RunOptions, apply_probe_patch, run_collect, run_probe

#: Remarks that name an actual resolution. These are the only claims worth trusting at face value
#: in metadata; CJK forms included because the sources label things "蓝光".
#:
#: The lookarounds are ASCII-letter-only on purpose. `\b` does not work here: in Python 3 CJK
#: characters are word characters, so `\bhd\b` fails on `HD国语` -- the single most common remark
#: form on this class of site, and the reason the first version of this pattern reported 0 HD.
_HIGH_RES = re.compile(
    r"1080|2160|(?<![a-z0-9])4k(?![a-z0-9])|(?<![a-z])uhd(?![a-z])|hdr|蓝光",
    re.IGNORECASE,
)

#: A generic "good copy" label. On this class of site `HD` usually means "not a cam rip", not
#: 720p vs 1080p -- measured on a real catalogue where 289 of 298 titles said exactly "HD". Kept
#: separate from `_HIGH_RES` for that reason: blending them would let a site look "all HD" while
#: naming no resolution at all.
_GENERIC_HD = re.compile(r"(?<![a-z])hd(?![a-z])|高清|超清", re.IGNORECASE)

#: `RESOLUTION=1920x1080` inside an HLS master playlist. This is the only *measurement* of picture
#: size available; everything else is the source's own claim.
_HLS_RESOLUTION = re.compile(r"RESOLUTION=(\d{2,5})x(\d{2,5})", re.IGNORECASE)

#: A play URL carrying a tracking/promotion tail, e.g. `...m3u8?from=bet365`.
_PROMO_PARAM = re.compile(r"[?&](from|channel|source|promo|agent)=([^&]{1,32})", re.IGNORECASE)
_PROMO_WORDS = re.compile(r"bet|bc|casino|lottery|kaiyun|leyu|boyu|yabo|jinsha", re.IGNORECASE)


@dataclass
class Dimension:
    """One axis of the report, with the values that belong to it."""

    name: str
    values: dict[str, Any] = field(default_factory=dict)
    note: str = ""


@dataclass
class Scorecard:
    url: str
    base_url: str
    sampled: int = 0
    tier: int | None = None
    adapter: str = ""
    probe_status: str = ""
    probe_ms: int = 0
    collect_ms: int = 0
    dimensions: list[Dimension] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def dim(self, name: str) -> Dimension:
        for dimension in self.dimensions:
            if dimension.name == name:
                return dimension
        raise KeyError(name)

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "base_url": self.base_url,
            "tier": self.tier,
            "probe_status": self.probe_status,
            "adapter": self.adapter,
            "sampled_titles": self.sampled,
            "probe_ms": self.probe_ms,
            "collect_ms": self.collect_ms,
            "dimensions": {d.name: d.values for d in self.dimensions},
            "errors": self.errors,
            "notes": self.notes,
        }


def _ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


async def score(
    url: str,
    *,
    settings: Settings | None = None,
    sample: int = 60,
    streams: int = 5,
    allow_browser: bool = False,
) -> Scorecard:
    """Probe and sample a site, and return measurements.

    ``sample`` bounds how many titles are collected. It is a real cost control, not a nicety: the
    point of a scorecard is to decide whether a source is worth a full harvest, so the measurement
    has to be cheaper than the thing it is measuring.
    """
    from harvest.cli import validate_public_url  # shared SSRF guard; importing late avoids a cycle
    from harvest.probe.detector import normalize_base_url

    settings = settings or get_settings()
    ok, url_or_error = validate_public_url(url)
    if not ok:
        raise ValueError(url_or_error)

    base = normalize_base_url(url_or_error)
    site = _recipe_for_host(base, settings) or SiteConfig(
        key="scorecard",
        name="scorecard",
        base_url=base,
        adapter="auto",
        enabled=True,
        compliance={"policy": "block_adult", "bc_filter": True},
    )
    card = Scorecard(url=url_or_error, base_url=base)
    streams_checked = streams

    # A recipe that names its adapter is a human decision, and it usually carries knowledge the
    # probe cannot rediscover -- the selector that opens the player, the failover hosts. Probing
    # is therefore informational for such a site, not a gate: aborting when the probe comes back
    # inconclusive would report "unusable" for a site whose recipe demonstrably works.
    pinned_adapter = site.adapter != "auto"

    async with HttpClient(settings) as http:
        started = time.monotonic()
        probe = await run_probe(site, http, settings=settings, allow_browser=allow_browser)
        card.probe_ms = int((time.monotonic() - started) * 1000)
        card.tier = probe.tier
        card.adapter = str(probe.adapter)
        card.probe_status = probe.status

        card.dimensions.append(
            Dimension(
                name="identity",
                values={
                    "detected_adapter": str(probe.adapter),
                    "tier": probe.tier,
                    "probe_status": probe.status,
                    "automatable": probe.automatable,
                },
                note="tier 决定它能否直接进 index.json：T3/T4 需要网关或 drpy 桥接",
            )
        )

        if probe.status == "unknown" and pinned_adapter:
            card.notes.append(
                f"探测未完成（{card.probe_ms} ms），但配方已显式指定 {site.adapter}，按配方继续测量"
            )
        elif probe.status == "unknown":
            # "unknown" means the probe did not complete -- no response, a timeout, a block. That
            # is a failed *measurement*, not a property of the site: reporting tier 0 / "not
            # automatable" here would read as "this source is unusable", which is a different and
            # much stronger claim than "we could not look". Observed for real: a 99-second probe
            # against a site that had answered in 3 seconds minutes earlier.
            card.errors.append(
                f"探测未完成（status=unknown，耗时 {card.probe_ms} ms）：可能是无响应、超时或被拦截。"
                "这不代表站点不可用，只代表这次没量到——重试或换网络后再跑。"
            )
            return card

        if not probe.automatable and not pinned_adapter:
            card.errors.append(f"not automatable (tier={probe.tier}, status={probe.status})")
            return card

        # `run_collect` needs a concrete adapter; detection only decided which one it is. When the
        # recipe already pins one and the probe came back empty-handed, patching would replace the
        # pin with `auto` and the collect would fail for the reason the pin exists to avoid.
        if not (pinned_adapter and probe.status == "unknown"):
            site = apply_probe_patch(site, probe)

        # The site-level advertising scan needs one page of HTML. Fetched once, from the home page.
        try:
            home = await http.get(base, site=site, key="scorecard")
            scan = scan_site_html(home.text, base)
        except Exception as exc:  # a site that will not serve its home page is worth reporting
            card.errors.append(f"home page: {type(exc).__name__}: {exc}")
            scan = None

        options = RunOptions(
            sites=[site.key],
            mode="full",
            max_titles=sample,
            allow_browser=allow_browser,
            verify=False,
            dry_run=True,  # measuring must not write snapshots into the working tree
        )
        started = time.monotonic()
        data, collect_ctx = await run_collect(
            site, http, settings=settings, options=options
        )
        card.collect_ms = int((time.monotonic() - started) * 1000)
        card.sampled = data.vod_count
        card.errors.extend(f"collect: {e.get('error', e)}" for e in collect_ctx.errors)

    stats = http.stats.get("scorecard")

    if not card.sampled:
        card.errors.append("no titles collected; quality metrics unavailable")
        return card

    cleaned = clean_site_data(
        data,
        page_html=None,  # site-level scan already done above; do not double-count
        bc_filter=True,
        adult_filter=True,
        quarantine_bc_site=False,
    )
    kept = cleaned.site_data

    # -- speed -----------------------------------------------------------------------------
    card.dimensions.append(
        Dimension(
            name="speed",
            values={
                "probe_ms": card.probe_ms,
                "collect_ms": card.collect_ms,
                "ms_per_title": int(card.collect_ms / card.sampled) if card.sampled else None,
                "requests": stats.requests if stats else None,
                "requests_per_title": round(stats.requests / card.sampled, 2)
                if stats and card.sampled
                else None,
                "kb_per_title": round(stats.bytes / card.sampled / 1024, 1)
                if stats and card.sampled
                else None,
                "errors": stats.errors if stats else None,
                "statuses": dict(stats.statuses) if stats else None,
            },
            note=f"基于 {card.sampled} 条的抽样；ms_per_title 越低越快，requests_per_title 反映单位成本",
        )
    )

    # -- HD --------------------------------------------------------------------------------
    #
    # Three separate answers, because they are three different strengths of evidence:
    #   * what the source *claims* about resolution (metadata)
    #   * what it *claims* generically ("HD"), which on these sites means "not a cam rip"
    #   * what the playlist *says*, which is a measurement
    claims_high = sum(
        1 for vod in kept.vods if _HIGH_RES.search(f"{vod.remarks or ''} {vod.name or ''}")
    )
    claims_generic = sum(
        1
        for vod in kept.vods
        if _GENERIC_HD.search(f"{vod.remarks or ''} {vod.name or ''}")
        and not _HIGH_RES.search(f"{vod.remarks or ''} {vod.name or ''}")
    )
    streams = await _check_streams(kept, http, site, limit=streams_checked)
    card.dimensions.append(
        Dimension(
            name="hd",
            values={
                "claims_resolution": claims_high,
                "claims_resolution_ratio": _ratio(claims_high, kept.vod_count),
                "claims_generic_hd_only": claims_generic,
                "claims_generic_hd_ratio": _ratio(claims_generic, kept.vod_count),
                "streams_checked": streams["checked"],
                "streams_with_resolution": streams["with_resolution"],
                "streams_measured_by_sps": streams["measured_by_sps"],
                "max_resolution": streams["max_resolution"],
                "resolutions_seen": streams["seen"],
                "sps_resolutions": streams["sps"],
                "measured_is_4k": streams["is_4k"],
            },
            note=(
                "claims_* 是站点自述，不可验证。streams_* 里两组都是量出来的："
                "with_resolution 读主播放列表的 RESOLUTION=；measured_by_sps 是在媒体列表"
                "（不声明分辨率）时，从第一个 TS 分片解 H.264 SPS 得到的真实像素尺寸。"
                "max_resolution / measured_is_4k 为空表示这一轮没量到，不等于不高清"
            ),
        )
    )

    # -- advertising ----------------------------------------------------------------------
    promo_urls = 0
    promo_hosts: set[str] = set()
    for vod in kept.vods:
        for line in vod.lines:
            for ep in line.episodes:
                match = _PROMO_PARAM.search(ep.url)
                if match and _PROMO_WORDS.search(match.group(2)):
                    promo_urls += 1
                    promo_hosts.add(match.group(2))
    dropped_bc = cleaned.dropped.bc_hosts
    card.dimensions.append(
        Dimension(
            name="ads",
            values={
                "gambling_domains_hit": dict(dropped_bc),
                "gambling_entries_dropped": sum(dropped_bc.values()),
                "site_ad_signals": scan.kinds if scan else None,
                "site_ad_signal_kinds": sorted(scan.signals) if scan else None,
                "site_would_be_quarantined": scan.quarantined if scan else None,
                "promo_tail_urls": promo_urls,
                "promo_tail_values": sorted(promo_hosts)[:10],
                "adult_entries_dropped": cleaned.dropped.counts.get("adult", 0),
            },
            note="gambling_domains_hit 是本底黑名单与规则命中的域名；site_ad_signals 是首页广告信号种类数",
        )
    )

    # -- quality --------------------------------------------------------------------------
    playable = sum(1 for vod in kept.vods if vod.playable)
    fresh = _recent_fraction(kept, days=7)
    card.dimensions.append(
        Dimension(
            name="quality",
            values={
                "titles": kept.vod_count,
                "titles_playable": playable,
                "playable_ratio": _ratio(playable, kept.vod_count),
                "lines_per_title": round(kept.line_count / kept.vod_count, 2) if kept.vod_count else None,
                "categories": len(kept.categories),
                "updated_within_7d": fresh,
            },
            note="playable_ratio 低说明目录里挂着大量没有播放地址的条目",
        )
    )
    return card


async def _check_streams(
    data: Any, http: HttpClient, site: SiteConfig, *, limit: int
) -> dict[str, Any]:
    """Read a handful of HLS playlists and report the picture sizes that can be measured.

    Bounded and spread across titles: the point is a sample of *distinct* streams, not depth on
    one. Media playlists legitimately carry no `RESOLUTION`, so a low `with_resolution` count is
    "not measurable this way", not "low quality" -- the caller's note says so.
    """
    out: dict[str, Any] = {
        "checked": 0,
        "with_resolution": 0,
        "max_resolution": None,
        "seen": {},
        "measured_by_sps": 0,
        "sps": {},
        "is_4k": None,
    }
    if limit <= 0 or not data.vods:
        return out

    candidates: list[str] = []
    for vod in data.vods:
        # One stream per title: depth on a single title says nothing about the catalogue, and a
        # title whose CDN is down would otherwise consume the whole budget.
        chosen: str | None = None
        for line in vod.lines:
            for episode in line.episodes:
                if ".m3u8" in episode.url.lower():
                    chosen = episode.url
                    break
            if chosen:
                break
        if chosen:
            candidates.append(chosen)
        if len(candidates) >= limit:
            break
    candidates = list(dict.fromkeys(candidates))[:limit]

    seen: dict[str, int] = {}
    sps_seen: dict[str, int] = {}
    best: tuple[int, int] | None = None
    best_info = None
    for url in candidates:
        out["checked"] += 1
        try:
            resp = await http.get(url, site=site, key="scorecard")
        except Exception:
            continue
        matches = _HLS_RESOLUTION.findall(resp.text[:200_000])
        if matches:
            width, height = max((int(w), int(h)) for w, h in matches)
            out["with_resolution"] += 1
            key = f"{width}x{height}"
            seen[key] = seen.get(key, 0) + 1
            if best is None or (width, height) > best:
                best = (width, height)
            continue
        # A media playlist declares nothing, so the master-playlist route is closed. Read the
        # SPS out of the first segment instead -- that is the encoder's own word, and it is the
        # only way to answer "is this really 4K" for the majority of these hosts.
        info = await _resolution_from_segment(resp.text, url, http, site)
        if info is None:
            continue
        out["measured_by_sps"] += 1
        sps_seen[info.label] = sps_seen.get(info.label, 0) + 1
        if best_info is None or (info.width, info.height) > (best_info.width, best_info.height):
            best_info = info

    out["seen"] = seen
    out["sps"] = sps_seen
    if best is None and best_info is not None:
        best = (best_info.width, best_info.height)
    out["max_resolution"] = f"{best[0]}x{best[1]}" if best else None
    out["is_4k"] = None if best is None else (best[0] >= 3800 or best[1] >= 2100)
    return out


async def _resolution_from_segment(text: str, playlist_url: str, http: HttpClient, site: SiteConfig):
    """First TS segment of a media playlist -> its real resolution (or None)."""
    from urllib.parse import urljoin

    from harvest.probe.h264 import resolution_from_ts

    segment = None
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        if ".ts" in candidate or ".flv" in candidate:
            segment = urljoin(playlist_url, candidate)
            break
    if not segment:
        return None
    try:
        # The SPS sits at the head of the segment; 512 KB is far more than needed and keeps
        # this cheap even on a slow CDN.
        resp = await http.get(
            segment, site=site, key="scorecard", headers={"Range": "bytes=0-524287"}
        )
    except Exception:
        return None
    return resolution_from_ts(resp.content or b"")


def _recent_fraction(data: Any, *, days: int) -> float | None:
    """Share of titles whose ``updated_at`` falls inside the window, or None if undated."""
    dated = [vod for vod in data.vods if vod.updated_at]
    if not dated:
        return None
    from datetime import datetime, timedelta

    cutoff = datetime.now(UTC) - timedelta(days=days)
    recent = 0
    for vod in dated:
        text = str(vod.updated_at).replace("Z", "+00:00")
        try:
            stamp = datetime.fromisoformat(text)
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        if stamp >= cutoff:
            recent += 1
    return _ratio(recent, len(dated))


def _recipe_for_host(base_url: str, settings: Settings) -> SiteConfig | None:
    """Reuse the checked-in recipe when this host already has one.

    A recipe carries knowledge a probe cannot rediscover on its own -- most importantly the
    selector that opens the player on a Tier-4 site. Measuring a site while ignoring the recipe
    that was written for it would report "no media URL" for a source that demonstrably has one.
    Nothing is written back: the recipe is read, never modified.
    """
    from urllib.parse import urlparse

    from harvest.pipeline import load_recipe, load_registry

    host = urlparse(base_url).netloc.lower()
    if not host or not settings.sites_dir.exists():
        return None
    registry = load_registry(settings)
    for path in sorted(settings.sites_dir.glob("*.yaml")):
        if path.stem == "registry":
            continue
        ledger = registry.find(path.stem)
        # Templates exist to be copied, not applied: they carry a placeholder host and a
        # pretend adapter. Matching one by host would quietly measure `example.com` as if it
        # were a Tier-3 site. `origin` is a free-form mapping in the ledger, so read both shapes.
        origin = getattr(ledger, "origin", None) if ledger is not None else None
        origin_kind = origin.get("type") if isinstance(origin, dict) else getattr(origin, "type", None)
        if str(origin_kind or "") == "template":
            continue
        recipe = load_recipe(path.stem, settings)
        if recipe is None:
            continue
        other = urlparse(recipe.base_url).netloc.lower()
        if other == host or other.endswith("." + host) or host.endswith("." + other):
            return recipe
    return None
