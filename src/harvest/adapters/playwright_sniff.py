"""Tier 4: sites whose media addresses only exist at runtime.

These are the sites that break naive scrapers. The catalogue page is ordinary
server-rendered HTML, but the play address is produced by the page's own JavaScript, often
after a signed request, and never appears in any static response. There is no general way to
compute it without either reverse-engineering each site or running a browser -- and running
a browser generalizes.

So this adapter does exactly that, and pays the cost only for sites that need it:

1. Discover detail pages with plain HTTP (their listings are usually still SSR).
2. Parse episode structure from static HTML (the episode *buttons* are server-rendered even
   when the *addresses* are not).
3. Drive a headless browser through those episodes and observe the media requests.
4. Turn any failover host list the page exposes (e.g. 4kvm's base64 ``_pdf``) into extra
   playable lines, replacing the player's own retry logic with TVBox's "switch line".

Because this is expensive, the adapter is hard-limited by ``budget.max_titles_per_run`` and
``budget.concurrency`` (default 1: never run two browser sessions against one site).
"""

from __future__ import annotations

import re
import time
from typing import Any, ClassVar
from urllib.parse import urljoin, urlparse

from harvest.adapters.base import CollectContext, empty_site_data, register
from harvest.adapters.html_ssr import HREF_RE, HtmlSsrAdapter
from harvest.clean.normalize import canon_media_url, extract_year, host_of, strip_html, swap_host
from harvest.models import AltURL, Episode, Line, SiteConfig, SiteData, Vod, compute_sid
from harvest.probe.signals import (
    EPISODE_ATTR_RE,
    EPISODE_COUNT_RE,
    LINE_ATTR_RE,
    LINE_NAME_RE,
)

PLAY_URL_LITERAL_RE = re.compile(r"""https?://[^\s"'<>\\]{0,300}\.m3u8""", re.IGNORECASE)


def parse_play_payload(text: str) -> dict[str, Any]:
    """Interpret a structured play-API response.

    Observed shape on the reference site::

        {"code": 200, "message": "OK", "data": {"play_id": 39063,
          "quality_urls": [
            {"mtype":"m3u8","bitrate":2160000,"title":"4K","isvip":true, "locked":true, "url":"1"},
            {"mtype":"m3u8","bitrate":1080,   "title":"1080p","isvip":false,"locked":false,"url":"https://…m3u8"}
          ]}}

    Two things this must get right, and they are the reason we parse rather than regex-scan:

    * **Entitlement.** ``isvip``/``locked`` entries carry a *placeholder* URL (literally ``"1"``),
      not a real address. Selecting by those flags is the difference between emitting a playable
      line and emitting garbage that a player will choke on.
    * **Refusal.** A gated episode answers ``code != 200`` with an explanatory message and a null
      ``quality_urls``. That is a deliberate answer from the server, and it must be recorded as
      such rather than treated as "we failed to find something".
    """
    from harvest.probe.signals import safe_json_loads

    obj = safe_json_loads(text)
    if not isinstance(obj, dict):
        return {}

    data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
    qualities = data.get("quality_urls") or []

    playable: list[tuple[int, str]] = []
    locked = 0
    for quality in qualities:
        if not isinstance(quality, dict):
            continue
        url = str(quality.get("url") or "").strip()
        if quality.get("isvip") or quality.get("locked") or not url.startswith(("http://", "https://")):
            locked += 1
            continue
        playable.append((int(quality.get("bitrate") or 0), url))

    # Prefer the highest bitrate among the *free* qualities.
    playable.sort(key=lambda item: item[0], reverse=True)
    return {
        "code": obj.get("code"),
        "message": str(obj.get("message") or "").strip(),
        "playable": [url for _bitrate, url in playable],
        "locked_qualities": locked,
        "structured": bool(qualities) or obj.get("code") is not None,
    }


@register
class PlaywrightSniffAdapter:
    kind: ClassVar[str] = "playwright_sniff"
    tier: ClassVar[int] = 4

    async def collect(self, site: SiteConfig, ctx: CollectContext) -> SiteData:
        data = empty_site_data(site, self.kind, self.tier)
        budget = site.budget or {}
        # The recipe's budget is a *ceiling*: a browser session per title is the expensive part,
        # so a site can cap itself, while an explicit CLI/--max-titles can still lower it for a
        # quick trial run. Either way the effective value is the smaller of the two.
        recipe_cap = int(budget.get("max_titles_per_run") or ctx.max_titles or 50)
        max_titles = min(recipe_cap, ctx.max_titles or recipe_cap)
        # Second, independent budget dimension. Title count alone does not bound the cost: a
        # single 72-episode fully-free series costs far more than twenty single-episode films.
        # Measured at ~5s of browser time per episode, an unbounded title walk is the one thing
        # that can push a Tier-4 run past its job timeout.
        episode_budget = int(budget.get("max_episodes_per_run") or 300)

        detail_urls = await self._discover_details(site, ctx)
        data.stats["detail_urls"] = len(detail_urls)
        if not detail_urls:
            ctx.note_error("playwright_sniff:catalogue", "no detail pages discovered")
            return data

        browser_cfg = site.browser or {}
        click_selector = str(browser_cfg.get("click_selector") or "[data-episode='{ep}']")
        sniff_patterns = list(browser_cfg.get("sniff_patterns") or [r"\.m3u8", r"/m3u8/"])
        read_globals = list(browser_cfg.get("read_globals") or [])
        fallback_hosts = list(browser_cfg.get("fallback_hosts") or [])
        capture_body_patterns = list(browser_cfg.get("capture_body_patterns") or [])
        stop_body_patterns = list(browser_cfg.get("stop_body_patterns") or [])
        wait_until = str(browser_cfg.get("wait_until") or "domcontentloaded")
        timeout = float(browser_cfg.get("per_title_timeout") or 40)
        max_episodes = int(browser_cfg.get("max_episodes_per_title") or 40)
        follow_links = bool(browser_cfg.get("follow_links", True))
        per_episode_wait_ms = int(browser_cfg.get("per_episode_wait_ms") or 10000)

        from harvest.fetch.browser import BrowserSniffer

        harvested = 0
        paywall_titles = 0
        paywalled_episodes = 0
        episodes_walked = 0
        titles_truncated = 0
        async with BrowserSniffer() as sniffer:
            for url in detail_urls:
                if ctx.expired or harvested >= max_titles or episodes_walked >= episode_budget:
                    break
                title_t0 = time.perf_counter()
                html = await self._get_text(url, site, ctx)
                if not html:
                    continue

                title = self._title(html)
                if not title:
                    continue

                available = self._episode_numbers(html)
                remaining = max(0, episode_budget - episodes_walked)
                episode_numbers = available[: min(max_episodes, remaining)]
                if not episode_numbers:
                    continue
                if len(episode_numbers) < len(available):
                    titles_truncated += 1
                    ctx.stats.setdefault("truncated_titles", []).append(
                        {"title": title, "available": len(available), "walked": len(episode_numbers)}
                    )

                result = await sniffer.sniff_episodes(
                    url,
                    click_selector=click_selector,
                    episodes=episode_numbers,
                    sniff_patterns=sniff_patterns,
                    read_globals=read_globals,
                    wait_until=wait_until,
                    timeout=timeout,
                    per_episode_wait_ms=per_episode_wait_ms,
                    follow_links=follow_links,
                    capture_body_patterns=capture_body_patterns,
                    stop_body_patterns=stop_body_patterns,
                )
                for err in result.get("errors") or []:
                    ctx.note_error(f"sniff:{url}", str(err))
                walked_here = len(result.get("methods") or {})
                episodes_walked += walked_here

                parsed = self._interpret(result, episode_numbers)
                line = self._build_line(parsed, fallback_hosts or list(result.get("globals", {}).get("_decoded_hosts") or []))
                paywalled_episodes += parsed["paywalled"]
                if parsed["paywalled"]:
                    paywall_titles += 1
                    ctx.stats.setdefault("paywall_notices", []).append(
                        {"title": title, "message": parsed["message"], "paywalled": parsed["paywalled"]}
                    )
                if result.get("stopped_early"):
                    ctx.stats.setdefault("stopped_early", []).append(
                        {"title": title, **result["stopped_early"]}
                    )

                # Per-title cost is the number the operator actually needs: it is what decides
                # the batch size, and it is the only way to tell "this site is slow" apart from
                # "this site is broken". Tier 4 has no other visibility into wall-clock spend.
                ctx.stats.setdefault("per_title_ms", []).append(
                    {
                        "title": title,
                        "ms": int((time.perf_counter() - title_t0) * 1000),
                        "episodes_walked": walked_here,
                        "requested": len(episode_numbers),
                        "available": len(available),
                        "paywalled": parsed["paywalled"],
                        "playable_eps": len(parsed["playable"]),
                        "result": "paywall" if parsed["paywalled"] else ("ok" if line else "empty"),
                    }
                )

                if line is None:
                    # A title with no playable address is a per-title outcome, not a site-level
                    # failure: the catalogue was reachable and the rest of the site may be fine.
                    # Counted in stats so it stays visible without tripping the breaker.
                    ctx.stats.setdefault("empty_titles", []).append({"title": title, "url": url})
                    continue

                vod = Vod(
                    site_key=site.key,
                    vid=self._vid_from_url(url, site.key),
                    name=title,
                    year=extract_year(fallback_text=title),
                    lines=[line],
                    source_url=url,
                )
                vod.sid = compute_sid(site.key, vod.vid)
                data.vods.append(vod)
                harvested += 1

                # Persist the raw trace for the first few titles so a failed run is debuggable.
                if harvested <= int(budget.get("keep_traces") or 2):
                    data.stats.setdefault("sniff_traces", []).append(
                        {
                            "url": url,
                            "episodes": sorted(parsed["playable"], key=lambda k: int(k)),
                            "paywalled": parsed["paywalled"],
                            "globals": result.get("globals", {}),
                            "stopped_early": result.get("stopped_early"),
                        }
                    )

        data.stats.update(
            {
                "vods": data.vod_count,
                "lines": data.line_count,
                "episodes": data.episode_count,
                "browser_sessions": 1,
                "max_titles_per_run": max_titles,
                "episode_budget": episode_budget,
                "episodes_walked": episodes_walked,
                "titles_truncated": titles_truncated,
                "paywalled_episodes": paywalled_episodes,
                "paywalled_titles": paywall_titles,
                "empty_titles": len(ctx.stats.get("empty_titles") or []),
                # ctx.stats and data.stats are separate dictionaries; anything the report needs
                # must be copied across explicitly.
                "per_title_ms": list(ctx.stats.get("per_title_ms") or []),
            }
        )
        if titles_truncated:
            ctx.note_error(
                "playwright_sniff:budget",
                f"episode budget ({episode_budget}) reached; {titles_truncated} title(s) only "
                f"partially walked. Raise budget.max_episodes_per_run or accept a partial list.",
            )
        return data

    def _interpret(self, result: dict[str, Any], episode_numbers: list[int]) -> dict[str, Any]:
        """Merge the structured play payloads and the raw sniffed URLs into one per-episode view.

        The structured payload wins when present, because it is the only source that distinguishes
        a free quality from a members-only placeholder.
        """
        bodies: dict[str, list[str]] = result.get("episode_bodies") or {}
        sniffed: dict[str, list[str]] = result.get("episodes") or {}

        playable: dict[int, list[str]] = {}
        paywalled = 0
        refusals: list[str] = []
        locked = 0

        for ep in episode_numbers:
            payloads = [parse_play_payload(text) for text in bodies.get(str(ep), [])]
            payloads = [p for p in payloads if p.get("structured")]

            urls: list[str] = []
            for payload in payloads:
                locked += int(payload.get("locked_qualities") or 0)
                for url in payload.get("playable") or []:
                    if url not in urls:
                        urls.append(url)
                if not payload.get("playable"):
                    # The server answered and the answer contained nothing playable. Record the
                    # *refusal*, not a success message -- this string is what explains the
                    # shortfall to whoever reads the report.
                    message = str(payload.get("message") or "").strip()
                    if message:
                        refusals.append(message)

            if not urls:
                # No structured payload (or nothing free in it): fall back to raw sniffing.
                urls = list(sniffed.get(str(ep)) or [])
            if urls:
                playable[ep] = urls
            elif payloads:
                paywalled += 1

        return {
            "playable": playable,
            "paywalled": paywalled,
            "locked_qualities": locked,
            "message": next((m for m in refusals if m), ""),
            "literals": result.get("html_media_literals") or [],
        }

    def build_tvbox_site(self, site: SiteConfig, data: SiteData | None) -> dict[str, Any]:
        gateway = str((site.probe or {}).get("gateway") or "")
        explicit = site.tvbox.model_dump(exclude_none=True)
        entry: dict[str, Any] = {
            "key": site.key,
            "name": site.name,
            "type": explicit.pop("type", 1),
            "api": site.tvbox.api or (f"{gateway}/vod/{site.key}" if gateway else ""),
            "searchable": 1 if gateway else 0,
            "quickSearch": 1 if gateway else 0,
            "filterable": 0,
            "changeable": 1,
            "playerType": 2,
        }
        entry.update(explicit)
        return {k: v for k, v in entry.items() if v not in (None, "")}

    # -- catalogue ---------------------------------------------------------------------

    async def _discover_details(self, site: SiteConfig, ctx: CollectContext) -> list[str]:
        """Listings on Tier-4 sites are usually still server-rendered, so plain HTTP works."""
        listing = site.listing or {}
        pages = listing.get("pages") or {}
        urls: list[str] = []

        http_helper = HtmlSsrAdapter()

        if pages.get("url"):
            categories = listing.get("categories") or [{"id": "", "name": ""}]
            max_pages = int(listing.get("max_pages") or 2)
            for cat in categories:
                cid = str(cat.get("id") if isinstance(cat, dict) else cat)
                for page in range(1, max_pages + 1):
                    if ctx.expired:
                        break
                    page_url = http_helper._render(  # noqa: SLF001 - shared helper, same package
                        str(pages["url"]), site, {"cat": cid, "page": page, "type": cid}
                    )
                    found = await http_helper._links_from_page(  # noqa: SLF001
                        page_url, site, ctx, str(pages.get("item") or "")
                    )
                    if not found:
                        break
                    for url in found:
                        if url not in urls:
                            urls.append(url)
        if urls:
            return urls

        pattern = str(((site.selectors or {}).get("list") or {}).get("cluster_pattern") or "")
        seeds = [site.base_url + "/"] + [
            str(s).strip() for s in ((site.probe or {}).get("seed_paths") or [])
        ]
        for seed in seeds:
            html = await self._get_text(seed, site, ctx)
            if not html:
                continue
            if not pattern:
                from harvest.probe.signals import cluster_detail_links

                clusters = cluster_detail_links(html, site.base_url)
                pattern = clusters[0].pattern if clusters and clusters[0].dominant else ""
            for href in HREF_RE.findall(html):
                full = urljoin(seed, href)
                if full in urls:
                    continue
                if pattern and not http_helper._matches_pattern(full, pattern):  # noqa: SLF001
                    continue
                if full.rstrip("/") == seed.rstrip("/"):
                    continue
                urls.append(full)
            if urls:
                break
        return urls

    # -- detail parsing (static parts) ---------------------------------------------------

    def _title(self, html: str) -> str:
        m = re.search(r"<title>([^<]{1,160})</title>", html, re.IGNORECASE)
        if not m:
            return ""
        import html as html_mod

        title = html_mod.unescape(m.group(1)).strip()
        # Strip the "- 第N集" episode suffix and the site-name tail so the fingerprint is
        # per-title rather than per-episode.
        title = re.split(r"\s*[-_|]\s*第\s*\d+\s*[集话]", title)[0]
        parts = re.split(r"\s*[-_|]\s*", title)
        return (parts[0] if parts else title).strip()

    def _episode_numbers(self, html: str) -> list[int]:
        numbers = sorted({int(n) for n in EPISODE_ATTR_RE.findall(html)})
        if not numbers:
            counts = [int(n) for n in EPISODE_COUNT_RE.findall(html)]
            if counts:
                numbers = list(range(1, max(counts) + 1))
        if not numbers:
            return []
        # Episode numbering on these sites starts at 1 but is not always contiguous.
        return [n for n in numbers if n > 0]

    def _line_name(self, html: str) -> str:
        names = LINE_NAME_RE.findall(html)
        if names:
            return names[0].strip() or "线路1"
        lines = LINE_ATTR_RE.findall(html)
        return f"线路{lines[0]}" if lines else "线路1"

    def _build_line(self, parsed: dict[str, Any], fallback_hosts: list[str]) -> Line | None:
        playable_by_ep: dict[int, list[str]] = parsed.get("playable") or {}
        episodes: list[Episode] = []

        for ep in sorted(playable_by_ep):
            urls: list[str] = []
            for raw in playable_by_ep[ep]:
                # Play APIs frequently return a site-relative "/m3u8/..." and let the page join
                # it against a failover host. Resolve it against the first declared host so the
                # address is directly playable.
                absolute = self._absolutize(raw, fallback_hosts)
                if not absolute:
                    continue
                canonical = canon_media_url(absolute)
                if canonical not in urls:
                    urls.append(canonical)
            if not urls:
                continue

            primary = urls[0]
            alts: list[AltURL] = []
            for url in urls[1:]:
                alts.append(AltURL(url=url, host=host_of(url), note="mirror"))

            # Fan out the page's failover host list into alts. This is the whole point: the
            # player would have swapped hosts itself on failure; surfacing them as lines lets
            # TVBox do it, visibly, on any client.
            for host in fallback_hosts:
                if host and host != host_of(primary):
                    alt = swap_host(primary, host)
                    if alt != primary and all(a.url != alt for a in alts):
                        alts.append(AltURL(url=alt, host=host, note="alts"))

            episodes.append(
                Episode(index=len(episodes) + 1, name=f"第{ep:02d}集", url=primary, alts=alts)
            )

        if not episodes:
            # Fall back to any literal in the markup -- sometimes the address is present even
            # though the player never requested it (e.g. blocked by our resource filter).
            for i, url in enumerate(parsed.get("literals") or [], start=1):
                resolved = self._absolutize(url, fallback_hosts)
                if resolved:
                    episodes.append(Episode(index=i, name=f"第{i:02d}集", url=canon_media_url(resolved)))
            if not episodes:
                return None

        return Line(name="主线路", episodes=episodes)

    @staticmethod
    def _absolutize(url: str, fallback_hosts: list[str]) -> str:
        """Turn a site-relative media path into an absolute URL, or return '' if impossible."""
        if not url:
            return ""
        url = url.strip()
        if url.startswith(("http://", "https://")):
            return url
        if url.startswith("/") and fallback_hosts:
            return f"https://{fallback_hosts[0]}{url}"
        return ""

    # -- plumbing ----------------------------------------------------------------------

    def _vid_from_url(self, url: str, site_key: str) -> str:
        path = urlparse(url).path
        segments = [s for s in path.split("/") if s]
        return segments[-1] if segments else site_key

    async def _get_text(self, url: str, site: SiteConfig, ctx: CollectContext) -> str:
        try:
            resp = await ctx.require_http().get(url, site=site, tolerate=(403, 404, 500))
        except Exception as exc:
            ctx.note_error("playwright_sniff:fetch", f"{url}: {exc}")
            return ""
        if resp.status_code != 200:
            ctx.note_error("playwright_sniff:status", f"{resp.status_code} {url}")
            return ""
        return resp.text


def summarize_static_episode_links(html: str) -> str:
    return strip_html(html)[:200]
