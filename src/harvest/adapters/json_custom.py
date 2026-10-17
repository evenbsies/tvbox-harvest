"""Tier 2: custom JSON / XML interfaces.

Sites in this tier *do* expose structured data, just not in the MacCMS dialect. Rather than
writing a parser per site, the recipe declares a small field mapping and this adapter reads
it. Discovering a mapping is semi-automatic: the probe scores candidate endpoints against a
video-field dictionary and returns the paths it recognized, which become the starting point
for the recipe's ``fields:`` block.

Expected recipe shape::

    adapter: json_custom
    fields:
      list_url: "{base_url}/api/list?cat={cat}&page={page}"
      list_path: "$.data.items[*]"
      id: "$.id"
      name: "$.name"
      episodes_path: "$.play_list[*]"
      episode_name: "$.name"
      episode_url: "$.url"
      detail_url: "{base_url}/api/detail?id={id}"

Note on TVBox compatibility: a custom interface cannot be consumed by TVBox directly, so
``build_tvbox_site`` points the entry at the gateway, which re-expresses the snapshot as
MacCMS JSON. The entry is honest about needing that bridge rather than pretending to be a
direct source.
"""

from __future__ import annotations

from typing import Any, ClassVar

from harvest.adapters.base import CollectContext, empty_site_data, register
from harvest.adapters.extractors import jp_get, jp_str
from harvest.clean.normalize import extract_year, strip_html
from harvest.fetch.http import looks_like_json, safe_json
from harvest.models import Category, Episode, Line, SiteConfig, SiteData, Vod, compute_sid

DEFAULT_FIELDS: dict[str, str] = {
    "list_path": "$.data.list[*]",
    "id": "$.vod_id",
    "name": "$.vod_name",
    "pic": "$.vod_pic",
    "category": "$.type_name",
    "year": "$.vod_year",
    "remarks": "$.vod_remarks",
    "content": "$.vod_content",
    "updated_at": "$.vod_time",
    "episodes_path": "$.play_list[*]",
    "episode_name": "$.name",
    "episode_url": "$.url",
    "line_name": "$.line",
    "total_path": "$.total",
    "page_path": "$.page",
    "pagecount_path": "$.pagecount",
}

ENVELOPE_CANDIDATES = ("$.data", "$.result", "$.payload", "")


@register
class JsonCustomAdapter:
    kind: ClassVar[str] = "json_custom"
    tier: ClassVar[int] = 2

    async def collect(self, site: SiteConfig, ctx: CollectContext) -> SiteData:
        fields = {**DEFAULT_FIELDS, **(site.fields or {})}
        data = empty_site_data(site, self.kind, self.tier)

        list_url = str(fields.get("list_url") or "")
        if not list_url:
            api = str((site.probe or {}).get("api") or site.tvbox.api or "")
            if not api:
                ctx.note_error("json_custom:config", "recipe has neither fields.list_url nor api")
                return data
            items, _ = await self._fetch(api, site, ctx, fields)
            data.vods = await self._to_vods(site, items, fields, ctx)
            return self._finalize(data, ctx)

        categories = [str(c) for c in ((site.probe or {}).get("categories") or [])]
        # A site that can only be queried by keyword has a coverage problem that no amount of
        # hand-written keywords solves: the list goes stale the day after it is written. If the
        # recipe declares where the site publishes its own chart, read the titles from there and
        # use them as keywords. Provenance stays with the site; the recipe only says where to look.
        discovered = await self._discover_keywords(site, ctx)
        if discovered:
            categories = list(dict.fromkeys([*categories, *discovered]))
            ctx.stats["keywords_total"] = len(categories)
        max_pages = int(fields.get("max_pages") or site.maccms.max_pages)
        raw_items: list[dict[str, Any]] = []

        for cid in categories or [""]:
            page, pagecount = 1, 1
            while page <= min(pagecount, max_pages):
                if ctx.expired or (ctx.max_titles and len(raw_items) >= ctx.max_titles):
                    break
                url = self._render(list_url, site, {"cat": cid, "page": page, "type": cid})
                items, payload = await self._fetch(url, site, ctx, fields)
                if not items:
                    break
                raw_items.extend(items)
                pagecount = self._pagecount(payload, fields)
                if page >= pagecount:
                    break
                page += 1
            if not categories:
                break

        data.vods = await self._to_vods(site, raw_items, fields, ctx)
        return self._finalize(data, ctx)

    def build_tvbox_site(self, site: SiteConfig, data: SiteData | None) -> dict[str, Any]:
        gateway = str((site.probe or {}).get("gateway") or "")
        explicit = site.tvbox.model_dump(exclude_none=True)
        api = site.tvbox.api or (f"{gateway}/vod/{site.key}" if gateway else "")
        entry: dict[str, Any] = {
            "key": site.key,
            "name": site.name,
            "type": explicit.pop("type", 1),
            "api": api,
            "searchable": 1,
            "quickSearch": 1,
            "filterable": 1 if (data and data.categories) else 0,
            "changeable": 1,
            "playerType": 2,
        }
        entry.update(explicit)
        return {k: v for k, v in entry.items() if v not in (None, "")}

    # -- internals ---------------------------------------------------------------------

    async def _discover_keywords(self, site: SiteConfig, ctx: CollectContext) -> list[str]:
        """Titles to use as search keywords, read from the site's own chart.

        Declared as `probe.keyword_source: {url, path, limit}`. Returns [] when the recipe does
        not declare one, or when the chart request fails -- a missing chart must degrade to "the
        hand-written keywords only", never to an empty run.
        """
        spec = (site.probe or {}).get("keyword_source") or {}
        if not isinstance(spec, dict) or not spec.get("url"):
            return []
        url = self._render(str(spec["url"]), site, {})
        try:
            resp = await ctx.require_http().get(url, site=site, tolerate=(403, 404, 500))
        except Exception as exc:
            ctx.note_error("json_custom:keywords", f"{url}: {exc}")
            return []
        if resp.status_code != 200 or not looks_like_json(resp):
            ctx.note_error("json_custom:keywords", f"HTTP {resp.status_code} {url}")
            return []
        payload = safe_json(resp)
        if payload is None:
            return []
        raw = jp_get(payload, str(spec.get("path") or ""))
        # A keyword must be a scalar. `jp_tokens` silently *truncates* an unparseable path tail
        # (`$.categories.*[*].name` tokenises to `['categories']`), so a typo resolves to a parent
        # object instead of nothing -- and the whole dict then travels into the search URL
        # percent-encoded. Rejecting non-strings turns a nonsense request into a visible error.
        bad = [value for value in raw if not isinstance(value, str)]
        if bad:
            ctx.note_error(
                "json_custom:keywords",
                f"path {spec.get('path')!r} yielded {type(bad[0]).__name__}, not strings; "
                "check the wildcard syntax",
            )
            return []
        names = [value.strip() for value in raw if value.strip()]
        unique = list(dict.fromkeys(names))
        limit = int(spec.get("limit") or 0)
        if limit > 0:
            unique = unique[:limit]
        ctx.stats["keywords_discovered"] = len(unique)
        return unique

    async def _fetch(
        self, url: str, site: SiteConfig, ctx: CollectContext, fields: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], Any]:
        try:
            resp = await ctx.require_http().get(url, site=site, tolerate=(403, 404, 500))
        except Exception as exc:
            ctx.note_error("json_custom:fetch", f"{url}: {exc}")
            return [], None

        if resp.status_code != 200 or not looks_like_json(resp):
            ctx.note_error("json_custom:shape", f"HTTP {resp.status_code} {url}")
            return [], None

        payload = safe_json(resp)
        if payload is None:
            return [], None

        ctx.stats["last_payload_keys"] = sorted(payload.keys())[:25] if isinstance(payload, dict) else []

        items = jp_get(payload, str(fields.get("list_path") or ""))
        if not items:
            # The declared path may be wrong (or the site may nest under an envelope the
            # recipe author did not anticipate). Try the common envelopes before giving up.
            declared_tail = str(fields.get("list_path") or "").split("$.", 1)[-1]
            for prefix in ENVELOPE_CANDIDATES:
                candidate = f"${prefix}.{declared_tail}" if prefix else f"$.{declared_tail}"
                items = jp_get(payload, candidate)
                if items:
                    ctx.stats.setdefault("envelope_fallbacks", []).append(candidate)
                    break

        out = [item for item in items if isinstance(item, dict)]
        return out, payload

    def _pagecount(self, payload: Any, fields: dict[str, Any]) -> int:
        path = str(fields.get("pagecount_path") or "")
        if not path:
            return 1
        value = jp_str(payload, path)
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 1

    async def _to_vods(
        self, site: SiteConfig, items: list[dict[str, Any]], fields: dict[str, Any], ctx: CollectContext
    ) -> list[Vod]:
        vods: list[Vod] = []
        for index, item in enumerate(items):
            if ctx.max_titles and index >= ctx.max_titles:
                break
            vid = jp_str(item, str(fields.get("id") or "$.id"))
            name = jp_str(item, str(fields.get("name") or "$.name"))
            if not vid or not name:
                continue

            lines = self._episode_lines(item, fields)
            detail: dict[str, Any] = {}
            if not lines:
                detail_url = str(fields.get("detail_url") or "")
                if detail_url:
                    lines, detail = await self._episode_lines_from_detail(
                        self._render(detail_url, site, {"id": vid, "vid": vid}), site, ctx, fields
                    )

            def field(key: str, item: dict[str, Any] = item, detail: dict[str, Any] = detail) -> str:
                """Read a field from the list item, falling back to the detail payload.

                Search endpoints routinely return placeholder rows -- an empty `pic`, a missing
                `content` -- and only the detail carries the real value. We already paid for the
                detail request to get the episodes, so reading the same path from it costs
                nothing. Without this, a site whose search API omits covers publishes a
                subscription with no artwork at all, which reads as a broken source.
                """
                value = jp_str(item, str(fields.get(key) or ""))
                if not value and detail:
                    value = jp_str(detail, str(fields.get(key) or ""))
                return value

            cat = field("category")
            vods.append(
                Vod(
                    site_key=site.key,
                    vid=vid,
                    sid=compute_sid(site.key, vid),
                    name=name,
                    year=extract_year(field("year")),
                    cat_raw=[cat] if cat else [],
                    remarks=field("remarks") or None,
                    pic=field("pic") or None,
                    content=strip_html(field("content")) or None,
                    lines=lines,
                    updated_at=field("updated_at") or None,
                )
            )
        return vods

    async def _episode_lines_from_detail(
        self, url: str, site: SiteConfig, ctx: CollectContext, fields: dict[str, Any]
    ) -> tuple[list[Line], dict[str, Any]]:
        items, payload = await self._fetch(url, site, ctx, fields)
        target = items[0] if items else payload
        if target is None:
            return [], {}
        return self._episode_lines(target if isinstance(target, dict) else {}, fields), (
            target if isinstance(target, dict) else {}
        )

    def _episode_lines(self, item: dict[str, Any], fields: dict[str, Any]) -> list[Line]:
        episodes_path = str(fields.get("episodes_path") or "")
        if not episodes_path:
            return []
        raw_eps = jp_get(item, episodes_path)
        groups: dict[str, list[Episode]] = {}
        for raw in raw_eps:
            if not isinstance(raw, dict):
                continue
            url = jp_str(raw, str(fields.get("episode_url") or "$.url"))
            if not url:
                continue
            name = jp_str(raw, str(fields.get("episode_name") or "$.name"))
            line_name = jp_str(raw, str(fields.get("line_name") or "")) or "线路1"
            bucket = groups.setdefault(line_name, [])
            bucket.append(
                Episode(index=len(bucket) + 1, name=name or f"第{len(bucket) + 1:02d}集", url=url)
            )
        return [Line(name=name, episodes=eps) for name, eps in groups.items() if eps]

    def _render(self, template: str, site: SiteConfig, values: dict[str, Any]) -> str:
        url = template.replace("{base_url}", site.base_url)
        for key, value in values.items():
            url = url.replace("{" + key + "}", str(value))
        return url

    def _finalize(self, data: SiteData, ctx: CollectContext) -> SiteData:
        names: list[str] = []
        for vod in data.vods:
            for raw in vod.cat_raw:
                if raw and raw not in names:
                    names.append(raw)
        data.categories = [Category(id=str(i + 1), name=name) for i, name in enumerate(names)]
        data.stats.update(
            {"vods": data.vod_count, "lines": data.line_count, "episodes": data.episode_count}
        )
        return data
