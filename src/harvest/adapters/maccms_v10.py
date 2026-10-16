"""Tier 1: standard Apple CMS (MacCMS V10) collection interface.

This is the workhorse adapter and the one the MVP is built around, because such sites
publish a JSON catalogue that TVBox can consume *directly* -- no gateway, no JS spider, no
jar. Harvesting them is therefore also the cheapest and most reliable path.

Interface contract being implemented (``?ac=`` drives everything):

===================  ======================================================
``ac=list``          paginated catalogue; ``class`` carries the category table
``ac=detail&ids=``   full records; this is where ``vod_play_url`` lives
``wd=``              search by title
``t=`` / ``pg=``     category filter / page number
===================  ======================================================

The adapter is incremental by default: ``h=24`` asks the upstream for recently-updated
records only, which turns a full crawl into a single request on quiet days.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from harvest.adapters.base import CollectContext, empty_site_data, register
from harvest.clean.normalize import extract_year, strip_html
from harvest.fetch.http import looks_like_json, safe_json
from harvest.models import Category, Line, SiteConfig, SiteData, Vod, compute_sid, parse_play_url

DETAIL_BATCH = 20


@register
class MaccmsV10Adapter:
    kind: ClassVar[str] = "maccms_v10"
    tier: ClassVar[int] = 1

    # -- public API --------------------------------------------------------------------

    async def collect(self, site: SiteConfig, ctx: CollectContext) -> SiteData:
        data = empty_site_data(site, self.kind, self.tier)
        api = self.api_url(site)

        list_payload = await self._fetch(
            api, site, ctx, params=self._auth_params(site, {"ac": "list", "pg": "1"})
        )
        if not isinstance(list_payload, dict):
            ctx.note_error("maccms:list", f"non-JSON response from {api}")
            return data

        categories = self._categories(list_payload)
        data.categories = categories
        data.stats["upstream"] = {
            "api": api,
            "total": list_payload.get("total"),
            "pagecount": list_payload.get("pagecount"),
            "limit": list_payload.get("limit"),
        }

        since = self._since_hours(site, ctx)
        catalogue: dict[str, dict[str, Any]] = {}

        if since:
            # Incremental: one request returns everything touched in the window.
            await self._collect_page(
                api, site, ctx, catalogue, params={"ac": "list", "h": str(since), "pg": "1"},
                paginate=True, cap=ctx.max_titles or 0,
            )
        else:
            for cat in categories:
                if ctx.expired:
                    ctx.note_error("maccms:budget", "deadline reached; stopping catalogue walk")
                    break
                await self._collect_page(
                    api, site, ctx, catalogue,
                    params={"ac": "list", "t": cat.id, "pg": "1"},
                    paginate=True, cap=ctx.max_titles or 0,
                )

        if not catalogue:
            # Some deployments ignore `h=`; fall back to a plain first page so the site is
            # at least represented instead of silently producing an empty source.
            await self._collect_page(
                api, site, ctx, catalogue, params={"ac": "list", "pg": "1"},
                paginate=False, cap=ctx.max_titles or 0,
            )

        ids = list(catalogue.keys())
        # Detail is what carries play URLs. Prefer records that still need a play URL.
        to_detail = [vid for vid in ids if not catalogue[vid].get("vod_play_url")]
        if ctx.max_titles:
            to_detail = to_detail[: ctx.max_titles]

        for start in range(0, len(to_detail), DETAIL_BATCH):
            if ctx.expired:
                ctx.note_error("maccms:budget", "deadline reached; stopping detail fetch")
                break
            batch = to_detail[start : start + DETAIL_BATCH]
            payload = await self._fetch(
                api, site, ctx, params=self._auth_params(site, {"ac": "detail", "ids": ",".join(batch)})
            )
            if not isinstance(payload, dict):
                ctx.note_error("maccms:detail", f"non-JSON detail for ids={','.join(batch[:3])}...")
                continue
            for item in payload.get("list") or []:
                if isinstance(item, dict):
                    vid = str(item.get("vod_id") or "")
                    if vid:
                        catalogue[vid] = {**catalogue.get(vid, {}), **item}

        data.vods = [v for v in (self._to_vod(site, raw) for raw in catalogue.values()) if v]
        data.stats.update(
            {
                "catalogue_entries": len(catalogue),
                "vods": data.vod_count,
                "lines": data.line_count,
                "episodes": data.episode_count,
            }
        )
        return data

    def build_tvbox_site(self, site: SiteConfig, data: SiteData | None) -> dict[str, Any]:
        api = site.tvbox.api or self.api_url(site)
        explicit = site.tvbox.model_dump(exclude_none=True)
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
            # `spider` intentionally absent: a plain MacCMS JSON endpoint needs no jar.
        }
        entry.update(explicit)
        if entry.get("ext") in (None, ""):
            entry.pop("ext", None)
        return entry

    # -- helpers -----------------------------------------------------------------------

    def api_url(self, site: SiteConfig) -> str:
        path = site.maccms.api_path or "/api.php/provide/vod/"
        if not path.startswith("/"):
            path = "/" + path
        return site.base_url + path

    def _auth_params(self, site: SiteConfig, params: dict[str, str]) -> dict[str, str]:
        """Attach token/sign credentials. Tokens come from the environment, never the recipe."""
        auth = site.auth
        if auth.type == "none" or not auth.token_secret:
            return params

        import hashlib
        import os

        token = os.environ.get(auth.token_secret, "")
        if not token:
            return params

        if auth.type == "token":
            return {**params, auth.token_query_name: token}

        # md5_sign: parameters sorted alphabetically, concatenated, then + token -> md5.
        merged = {**params, auth.token_query_name: token}
        ordered = "".join(f"{k}={merged[k]}" for k in sorted(merged))
        merged["sign"] = hashlib.md5((ordered + token).encode()).hexdigest()
        return merged

    def _since_hours(self, site: SiteConfig, ctx: CollectContext) -> int | None:
        if ctx.mode == "full":
            return None
        if ctx.since_hours:
            return ctx.since_hours
        if ctx.mode == "incremental":
            return site.maccms.incremental_hours or 24
        return None

    async def _fetch(
        self, api: str, site: SiteConfig, ctx: CollectContext, params: dict[str, str]
    ) -> Any | None:
        if sum(s.requests for s in ctx.require_http().stats.values()) >= (ctx.max_requests or 10**9):
            ctx.note_error("maccms:budget", "request budget exhausted")
            return None
        try:
            resp = await ctx.require_http().get(
                api, site=site, params=params, tolerate=(403, 404, 500, 502, 503)
            )
        except Exception as exc:
            ctx.note_error("maccms:fetch", f"{params}: {exc}")
            return None

        if resp.status_code != 200:
            ctx.note_error("maccms:status", f"{resp.status_code} for params={params}")
            return None
        if not looks_like_json(resp):
            ctx.note_error("maccms:content-type", f"not JSON: {resp.headers.get('content-type')}")
            return None
        return safe_json(resp)

    async def _collect_page(
        self,
        api: str,
        site: SiteConfig,
        ctx: CollectContext,
        catalogue: dict[str, dict[str, Any]],
        *,
        params: dict[str, str],
        paginate: bool,
        cap: int,
    ) -> None:
        page = 1
        pagecount = 1
        while page <= min(pagecount, site.maccms.max_pages):
            if ctx.expired or (cap and len(catalogue) >= cap):
                return
            payload = await self._fetch(api, site, ctx, params=self._auth_params(site, {**params, "pg": str(page)}))
            if not isinstance(payload, dict):
                return

            for item in payload.get("list") or []:
                if not isinstance(item, dict):
                    continue
                vid = str(item.get("vod_id") or "")
                if vid:
                    catalogue[vid] = {**catalogue.get(vid, {}), **item}

            if not paginate:
                return

            try:
                pagecount = int(payload.get("pagecount") or 1)
            except (TypeError, ValueError):
                pagecount = 1
            if page >= pagecount or not (payload.get("list") or []):
                return
            page += 1

    def _categories(self, payload: dict[str, Any]) -> list[Category]:
        out: list[Category] = []
        for raw in payload.get("class") or []:
            if not isinstance(raw, dict):
                continue
            cid = str(raw.get("type_id") or raw.get("id") or "")
            name = str(raw.get("type_name") or raw.get("name") or "")
            if cid and name:
                out.append(Category(id=cid, name=name))
        return out

    def _to_vod(self, site: SiteConfig, raw: dict[str, Any]) -> Vod | None:
        vid = str(raw.get("vod_id") or "").strip()
        name = str(raw.get("vod_name") or raw.get("vod_en") or "").strip()
        if not vid or not name:
            return None

        cat_raw: list[str] = []
        for key in ("type_name", "type_name_2", "class"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                cat_raw.append(value.strip())
            elif isinstance(value, list):
                cat_raw.extend(str(v) for v in value if v)

        lines = parse_play_url(str(raw.get("vod_play_url") or ""))
        froms = [f for f in str(raw.get("vod_play_from") or "").split("$$$") if f]
        # Prefer the upstream's own line names -- they mean something to the end user
        # ("lzm3u8" vs "线路1") and cost nothing to preserve.
        for idx, line in enumerate(lines):
            if idx < len(froms):
                line.name = froms[idx]

        vod = Vod(
            site_key=site.key,
            vid=vid,
            sid=compute_sid(site.key, vid),
            name=name,
            year=extract_year(raw.get("vod_year")),
            cat_raw=cat_raw,
            remarks=str(raw.get("vod_remarks") or "").strip() or None,
            pic=str(raw.get("vod_pic") or "").strip() or None,
            actor=str(raw.get("vod_actor") or "").strip() or None,
            director=str(raw.get("vod_director") or "").strip() or None,
            area=str(raw.get("vod_area") or "").strip() or None,
            lang=str(raw.get("vod_lang") or "").strip() or None,
            content=strip_html(str(raw.get("vod_content") or "")) or None,
            lines=lines,
            source_url=None,
            updated_at=str(raw.get("vod_time") or "").strip() or None,
        )
        vod.extra["vod_play_from"] = raw.get("vod_play_from")
        return vod


def episode_line_summary(lines: list[Line]) -> str:
    return ", ".join(f"{line.name}({len(line.episodes)})" for line in lines)


async def gather_categories(adapter: MaccmsV10Adapter, site: SiteConfig, ctx: CollectContext) -> list[Category]:
    """Small helper used by the CLI to preview a site's categories without a full crawl."""
    payload = await adapter._fetch(  # noqa: SLF001 - intentional intra-package use
        adapter.api_url(site), site, ctx, params=adapter._auth_params(site, {"ac": "list", "pg": "1"})  # noqa: SLF001
    )
    if not isinstance(payload, dict):
        return []
    await asyncio.sleep(0)
    return adapter._categories(payload)  # noqa: SLF001
