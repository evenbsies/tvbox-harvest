"""Tier 3: server-rendered HTML sites whose media URLs are present in the markup.

These sites have no catalogue API, but they do not hide their play addresses behind
JavaScript either -- a plain HTTP GET of a detail page contains them, usually inside a
``player_aaaa = {...}`` blob or a playlist block. That makes them cheap to harvest (one
request per title) compared with Tier 4.

Recipe shape::

    adapter: html_ssr
    listing:
      pages:
        url: "/vodshow/{cat}/page/{page}.html"
        item: "a.vod-item"
        href: "::attr(href)"
      categories: [{id: "1", name: "电影"}]
    selectors:
      detail:
        title: "h1::text"
        episodes:
          - {type: css_list, selector: ".playlist a", name: "::text", url: "::attr(href)"}
          - {type: json_regex, pattern: "player_aaaa\\s*=\\s*({.*?})", path: "$.url"}

When no ``listing`` is configured the adapter falls back to whatever detail-link cluster the
probe discovered, which is enough to walk most sites' "latest updates" section.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar
from urllib.parse import urljoin, urlparse

from harvest.adapters.base import CollectContext, empty_site_data, register
from harvest.adapters.extractors import apply_rule, scan_m3u8_literals
from harvest.clean.normalize import extract_year, strip_html
from harvest.models import Episode, Line, SiteConfig, SiteData, Vod, compute_sid

HREF_RE = re.compile(r"""href=["']([^"'#]+)["']""", re.IGNORECASE)
# The single definition of "this is a media address", used both to accept a direct link and to
# reject a direct link while hunting for play pages. Keeping one regex means the two strategies
# can never disagree about what counts as media.
MEDIA_SUFFIX_RE = re.compile(r"\.(m3u8|mp4|flv|mkv)(\?|$)", re.IGNORECASE)


@register
class HtmlSsrAdapter:
    kind: ClassVar[str] = "html_ssr"
    tier: ClassVar[int] = 3

    async def collect(self, site: SiteConfig, ctx: CollectContext) -> SiteData:
        data = empty_site_data(site, self.kind, self.tier)
        detail_urls = await self._discover_details(site, ctx)
        data.stats["detail_urls"] = len(detail_urls)

        if not detail_urls:
            ctx.note_error("html_ssr:catalogue", "no detail pages discovered")
            return data

        limit = ctx.max_titles or 300
        for url in detail_urls[:limit]:
            if ctx.expired:
                ctx.note_error("html_ssr:budget", "deadline reached")
                break
            vod = await self._parse_detail(site, url, ctx)
            if vod is not None:
                data.vods.append(vod)
            else:
                # A detail page we could not turn into a source. Counted rather than ignored:
                # parse_success_rate alone cannot say whether the misses were exceptions or a
                # systematically unrecognised template.
                ctx.stats["titles_without_episodes"] = (
                    int(ctx.stats.get("titles_without_episodes") or 0) + 1
                )
                ctx.stats.setdefault("unparsed_details", []).append(url)

        data.stats.update(
            {
                "vods": data.vod_count,
                "lines": data.line_count,
                "episodes": data.episode_count,
                "parse_success_rate": round(
                    data.vod_count / max(1, min(len(detail_urls), limit)), 3
                ),
                # Which of the two strategies actually worked, and what it cost. A site that
                # silently needs one extra request per episode must not look like a cheap one.
                "episode_strategy": ctx.stats.get("episode_strategy") or "none",
                "play_pages_fetched": int(ctx.stats.get("play_pages_fetched") or 0),
                "titles_without_episodes": int(ctx.stats.get("titles_without_episodes") or 0),
            }
        )
        return data

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

    # -- catalogue discovery -----------------------------------------------------------

    async def _discover_details(self, site: SiteConfig, ctx: CollectContext) -> list[str]:
        listing = site.listing or {}
        pages = listing.get("pages") or {}
        urls: list[str] = []

        if pages.get("url"):
            categories = listing.get("categories") or [{"id": "", "name": ""}]
            max_pages = int(listing.get("max_pages") or 3)
            for cat in categories:
                cid = str(cat.get("id") if isinstance(cat, dict) else cat)
                for page in range(1, max_pages + 1):
                    if ctx.expired:
                        break
                    page_url = self._render(
                        str(pages["url"]), site, {"cat": cid, "page": page, "type": cid}
                    )
                    found = await self._links_from_page(page_url, site, ctx, str(pages.get("item") or ""))
                    if not found:
                        break
                    for url in found:
                        if url not in urls:
                            urls.append(url)
        if urls:
            return urls

        # Fallback: use the cluster the probe found, either from the recipe or by re-deriving.
        pattern = str(((site.selectors or {}).get("list") or {}).get("cluster_pattern") or "")
        seed_urls = [site.base_url + "/"]
        seed_urls.extend(str(s).strip() for s in ((site.probe or {}).get("seed_paths") or []))

        for seed in seed_urls:
            html = await self._get_text(seed, site, ctx)
            if not html:
                continue
            if not pattern:
                from harvest.probe.signals import cluster_detail_links

                clusters = cluster_detail_links(html, site.base_url)
                pattern = clusters[0].pattern if clusters and clusters[0].dominant else ""
            for href in HREF_RE.findall(html):
                full = urljoin(seed, href)
                if pattern and self._matches_pattern(full, pattern) and full not in urls:
                    urls.append(full)
            if len(urls) >= (ctx.max_titles or 300):
                break
        return urls

    async def _links_from_page(
        self, url: str, site: SiteConfig, ctx: CollectContext, item_selector: str
    ) -> list[str]:
        html = await self._get_text(url, site, ctx)
        if not html:
            return []
        out: list[str] = []
        if item_selector:
            selector = item_selector
            value = "::attr(href)"
            if "::" in item_selector:
                selector, _, rest = item_selector.partition("::")
                value = "::" + rest
            result = apply_rule(html, {"type": "css_list", "selector": selector, "value": value})
            for candidate in result.values:
                full = urljoin(url, candidate)
                if full not in out:
                    out.append(full)
            if out:
                return out

        pattern = str(((site.selectors or {}).get("list") or {}).get("cluster_pattern") or "")
        for href in HREF_RE.findall(html):
            full = urljoin(url, href)
            if full.rstrip("/") == url.rstrip("/"):
                continue
            if pattern and not self._matches_pattern(full, pattern):
                continue
            if full not in out:
                out.append(full)
        return out

    @staticmethod
    def _matches_pattern(url: str, pattern: str) -> bool:
        path = urlparse(url).path
        regex = re.escape(pattern)
        regex = regex.replace(r"\{id\}", r"[A-Za-z0-9_\-]{2,40}")
        regex = regex.replace(r"\{n\}", r"\d+")
        regex = regex.replace(r"\{slug\}", r"[A-Za-z0-9_\-]{1,60}")
        return bool(re.fullmatch(regex, path))

    # -- detail parsing ------------------------------------------------------------------

    async def _parse_detail(self, site: SiteConfig, url: str, ctx: CollectContext) -> Vod | None:
        html = await self._get_text(url, site, ctx)
        if not html:
            return None

        selectors = (site.selectors or {}).get("detail") or {}
        title = str(selectors.get("title") or "")
        name = ""
        if title:
            selector, _, value_spec = title.partition("::")
            result = apply_rule(
                html,
                {"type": "css", "selector": selector.strip(), "value": "::" + value_spec if value_spec else "::text"},
            )
            name = result.values[0] if result.values else ""
        if not name:
            import html as html_mod

            m = re.search(r"<title>([^<]{1,120})</title>", html, re.IGNORECASE)
            if m:
                name = html_mod.unescape(m.group(1)).split("_")[0].split("-")[0].strip()
        if not name:
            return None

        episodes = self._episodes(html, url, selectors, ctx)
        if not episodes:
            # The dominant real-world shape: the detail page's playlist does not link to media
            # at all, it links to one *play page per episode* (``/vodplay/12345-1-1.html``), and
            # the address only appears on that page. Without this hop the adapter finds nothing
            # and the site looks unharvestable -- which is wrong, it just costs one request per
            # episode. Recorded as its own strategy because the cost profile is different.
            episodes = await self._episodes_via_play_pages(site, url, html, selectors, ctx)
        if not episodes:
            return None

        year = None
        year_rule = (selectors.get("fields") or {}).get("year")
        if isinstance(year_rule, dict):
            res = apply_rule(html, year_rule)
            if res.values:
                year = extract_year(res.values[0])
        year = year or extract_year(fallback_text=name)

        vod = Vod(
            site_key=site.key,
            vid=self._vid_from_url(url, site.key),
            name=name,
            year=year,
            lines=[Line(name="线路1", episodes=episodes)],
            source_url=url,
        )
        vod.sid = compute_sid(site.key, vod.vid)

        content_rule = (selectors.get("fields") or {}).get("content")
        if isinstance(content_rule, dict):
            res = apply_rule(html, content_rule)
            if res.values:
                vod.content = strip_html(res.values[0]) or None
        return vod

    def _episodes(
        self,
        html: str,
        base_url: str,
        selectors: dict[str, Any],
        ctx: CollectContext,
        record_strategy: bool = True,
    ) -> list[Episode]:
        rules = list(selectors.get("episodes") or [])
        if not rules:
            rules = [
                {"type": "json_regex", "pattern": r"player_aaaa\s*=\s*(\{[\s\S]{0,6000}?\})\s*</script>"},
                {"type": "css_list", "selector": ".playlist a, .play-list a, .stui-content__playlist a",
                 "url": "::attr(href)"},
            ]

        episodes: list[Episode] = []
        seen: set[str] = set()
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            result = apply_rule(html, rule)
            candidates: list[tuple[str, str]] = [(p.get("name") or "", p.get("url") or "") for p in result.pairs]
            candidates.extend(("", v) for v in result.values)
            for raw_name, raw_url in candidates:
                if not raw_url:
                    continue
                full = raw_url if raw_url.startswith("http") else urljoin(base_url, raw_url)
                if full in seen:
                    continue
                # A detail page can link to a nested play page instead of the file itself.
                # Keep it only when it looks like media; otherwise the caller falls back to
                # following play pages, and only then to Tier 4.
                if not MEDIA_SUFFIX_RE.search(full):
                    continue
                seen.add(full)
                episodes.append(
                    Episode(index=len(episodes) + 1, name=raw_name.strip() or f"第{len(episodes) + 1:02d}集", url=full)
                )
            if episodes:
                break

        if not episodes:
            # Last resort: any literal m3u8 in the markup (sometimes the only signal present).
            for full in scan_m3u8_literals(html):
                url = full if full.startswith("http") else urljoin(base_url, full)
                if url not in seen:
                    seen.add(url)
                    episodes.append(Episode(index=len(episodes) + 1, name=f"第{len(episodes) + 1:02d}集", url=url))
        if episodes and record_strategy:
            ctx.stats.setdefault("episode_strategy", "direct")
        return episodes
    def _vid_from_url(self, url: str, site_key: str) -> str:
        path = urlparse(url).path
        for match in re.findall(r"([A-Za-z0-9_\-]{3,40})", path):
            if re.search(r"\d", match):
                return match
        return path.strip("/").replace("/", "-") or site_key

    # -- nested play pages ---------------------------------------------------------------

    def _nested_play_links(
        self, html: str, base_url: str, selectors: dict[str, Any], ctx: CollectContext
    ) -> list[tuple[str, str]]:
        """Links from the playlist that point at a page rather than at a media file.

        Returns ``(name, url)`` in document order, de-duplicated, same-origin only. Order is
        preserved on purpose: episode 1 must stay episode 1, and the link text is the only
        episode name available.
        """
        rules = list(selectors.get("play_pages") or [])
        if not rules:
            rules = [
                {"type": "css_list", "selector": ".playlist a, .play-list a, .stui-content__playlist a, .stui-content__playlist li a",
                 "name": "::text", "url": "::attr(href)"},
            ]

        origin = urlparse(base_url).netloc
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            result = apply_rule(html, rule)
            candidates: list[tuple[str, str]] = [(p.get("name") or "", p.get("url") or "") for p in result.pairs]
            candidates.extend(("", v) for v in result.values)
            for raw_name, raw_url in candidates:
                if not raw_url:
                    continue
                full = raw_url if raw_url.startswith("http") else urljoin(base_url, raw_url)
                if urlparse(full).netloc != origin:
                    continue
                if full.rstrip("/") == base_url.rstrip("/") or full in seen:
                    continue
                # Skip anything that already looks like media; that is the direct path, handled
                # by ``_episodes`` before we ever get here.
                if MEDIA_SUFFIX_RE.search(full):
                    continue
                seen.add(full)
                out.append((raw_name.strip(), full))
        return out

    async def _episodes_via_play_pages(
        self, site: SiteConfig, detail_url: str, html: str, selectors: dict[str, Any], ctx: CollectContext
    ) -> list[Episode]:
        """Follow one intermediate page per episode and read the address off that page.

        Cost is one extra request per episode, so this is capped independently of the title
        budget: a 200-episode series would otherwise silently multiply the run's request count
        by 200. The cap is reported, never applied silently.
        """
        links = self._nested_play_links(html, detail_url, selectors, ctx)
        if not links:
            return []

        cap = int(selectors.get("max_play_pages") or 60)
        walked = links[:cap]
        if len(links) > cap:
            ctx.stats.setdefault("play_page_truncated", []).append(
                {"detail": detail_url, "available": len(links), "walked": cap}
            )

        episodes: list[Episode] = []
        seen: set[str] = set()
        # A play page is just a detail page with one entry, so the ordinary strategies apply.
        # Recipes may override the extraction rules because the player variable is not always
        # called ``player_aaaa`` -- ``MacPlayer``, ``player_data`` and plain ``sources`` all occur.
        page_selectors = {"episodes": list(selectors.get("play_page_episodes") or [])}
        for raw_name, play_url in walked:
            if ctx.expired:
                break
            play_html = await self._get_text(play_url, site, ctx)
            if not play_html:
                continue
            found = self._episodes(play_html, play_url, page_selectors, ctx, record_strategy=False)
            if not found:
                continue
            url = found[0].url
            if url in seen:
                continue
            seen.add(url)
            episodes.append(
                Episode(
                    index=len(episodes) + 1,
                    name=raw_name or f"第{len(episodes) + 1:02d}集",
                    url=url,
                )
            )

        if episodes:
            ctx.stats["episode_strategy"] = "play_pages"
            ctx.stats.setdefault("play_pages_fetched", 0)
            ctx.stats["play_pages_fetched"] += len(walked)
        return episodes

    async def _get_text(self, url: str, site: SiteConfig, ctx: CollectContext) -> str:
        try:
            resp = await ctx.require_http().get(url, site=site, tolerate=(403, 404, 500))
        except Exception as exc:
            ctx.note_error("html_ssr:fetch", f"{url}: {exc}")
            return ""
        if resp.status_code != 200:
            ctx.note_error("html_ssr:status", f"{resp.status_code} {url}")
            return ""
        ctx.stats["last_fetch"] = url
        return resp.text

    def _render(self, template: str, site: SiteConfig, values: dict[str, Any]) -> str:
        url = template.replace("{base_url}", site.base_url)
        for key, value in values.items():
            url = url.replace("{" + key + "}", str(value))
        if not url.startswith("http"):
            url = urljoin(site.base_url + "/", url.lstrip("/"))
        return url
