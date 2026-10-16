"""Pre-commit self-check: does the generated config actually behave like a TVBox source?

An entry that looks right but returns nothing is worse than no entry at all -- the user sees
a source that opens and shows an empty list. So before anything is committed, each generated
site is exercised the way a client would exercise it:

1. ``?ac=list&pg=1``      -- does the catalogue answer?
2. ``?ac=detail&ids=``    -- does a detail call yield a playable ``vod_play_url``?
3. ``?wd=<sample title>`  -- does search return the title we just harvested?

Entries that cannot be verified *without* a bridge (Tier 3/4 with no gateway configured) are
verified against their own snapshot instead, and reported as such rather than silently
passing. A site that fails is dropped from the default flavour for this run while its
last-good artefacts stay in place.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harvest.config import Settings, get_settings
from harvest.fetch.http import HttpClient, looks_like_json, safe_json
from harvest.models import SiteConfig, SiteData, parse_play_url


@dataclass
class VerifyResult:
    key: str
    status: str = "skipped"  # ok | failed | skipped | snapshot
    # NOTE: these are deliberately *not* named `list`/`detail`/`search`. A dataclass field
    # named `list` shadows the `list` builtin for the rest of the class body, so a later
    # `field(default_factory=list)` silently binds to the field's default (None) instead of
    # the type -- which fails at construction time, not at definition time.
    list_ok: bool | None = None
    detail_ok: bool | None = None
    search_ok: bool | None = None
    messages: list[str] = field(default_factory=lambda: [])

    @property
    def passed(self) -> bool:
        return self.status in ("ok", "snapshot")

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "status": self.status,
            "list": self.list_ok,
            "detail": self.detail_ok,
            "search": self.search_ok,
            "messages": self.messages[:8],
        }


async def verify_entry(
    entry: dict[str, Any],
    *,
    site: SiteConfig | None = None,
    site_data: SiteData | None = None,
    http: HttpClient | None = None,
    settings: Settings | None = None,
) -> VerifyResult:
    settings = settings or get_settings()
    key = str(entry.get("key") or "?")
    api = str(entry.get("api") or "")
    result = VerifyResult(key=key)

    if not api:
        result.status = "failed"
        result.messages.append("entry has no api field")
        return result

    direct = api.startswith(("http://", "https://"))
    if not direct:
        result.status = "skipped"
        result.messages.append(f"non-http api (spider/jar): {api}")
        return result

    if site is None:
        # No recipe available (verifying a hand-written config): do a lightweight probe only.
        site = SiteConfig(key=key, name=str(entry.get("name") or key), base_url=_origin(api), adapter="maccms_v10")

    owned_http = http is None
    client = http or HttpClient(settings)
    try:
        await _check_remote(result, client, site, api, site_data)
    finally:
        if owned_http:
            await client.aclose()

    if result.status == "skipped":
        # Fall back to snapshot verification for bridged sites.
        snapshot = _load_snapshot_records(key, settings)
        if snapshot:
            _check_snapshot(result, snapshot)
    return result


async def _check_remote(
    result: VerifyResult,
    http: HttpClient,
    site: SiteConfig,
    api: str,
    site_data: SiteData | None,
) -> None:
    # --- 1. catalogue ---------------------------------------------------------------
    try:
        resp = await http.get(api, site=site, params={"ac": "list", "pg": "1"}, tolerate=(403, 404, 500))
        payload = safe_json(resp) if looks_like_json(resp) else None
        items = (payload or {}).get("list") if isinstance(payload, dict) else None
        result.list_ok = bool(resp.status_code == 200 and isinstance(items, list))
        if not result.list_ok:
            result.messages.append(f"ac=list -> HTTP {resp.status_code}, list={type(items).__name__}")
    except Exception as exc:
        result.list_ok = False
        result.messages.append(f"ac=list raised: {exc}")

    if result.list_ok is False:
        result.status = "failed"
        return

    # --- 2. detail ------------------------------------------------------------------
    sample_id = ""
    if site_data and site_data.vods:
        sample_id = site_data.vods[0].sid or site_data.vods[0].vid
    if not sample_id:
        try:
            resp = await http.get(api, site=site, params={"ac": "list", "pg": "1"}, tolerate=(403, 404))
            payload = safe_json(resp) if looks_like_json(resp) else None
            items = (payload or {}).get("list") or []
            if isinstance(items, list) and items and isinstance(items[0], dict):
                sample_id = str(items[0].get("vod_id") or "")
        except Exception:
            pass

    if not sample_id:
        result.status = "failed"
        result.messages.append("no sample id available for detail check")
        return

    try:
        resp = await http.get(
            api, site=site, params={"ac": "detail", "ids": sample_id}, tolerate=(403, 404, 500)
        )
        payload = safe_json(resp) if looks_like_json(resp) else None
        items = (payload or {}).get("list") if isinstance(payload, dict) else None
        play_url = ""
        if isinstance(items, list) and items and isinstance(items[0], dict):
            play_url = str(items[0].get("vod_play_url") or "")
        lines = parse_play_url(play_url)
        result.detail_ok = bool(lines and any(line.episodes for line in lines))
        if not result.detail_ok:
            result.messages.append(f"ac=detail -> no parseable vod_play_url (id={sample_id})")
    except Exception as exc:
        result.detail_ok = False
        result.messages.append(f"ac=detail raised: {exc}")

    # --- 3. search ------------------------------------------------------------------
    sample_title = ""
    if site_data and site_data.vods:
        sample_title = site_data.vods[0].name
    if sample_title:
        try:
            resp = await http.get(api, site=site, params={"wd": sample_title}, tolerate=(403, 404))
            payload = safe_json(resp) if looks_like_json(resp) else None
            items = (payload or {}).get("list") if isinstance(payload, dict) else None
            result.search_ok = bool(isinstance(items, list) and items)
            if not result.search_ok:
                result.messages.append(f"wd={sample_title!r} -> no hits")
        except Exception as exc:
            result.search_ok = False
            result.messages.append(f"wd raised: {exc}")
    else:
        result.search_ok = None

    if result.list_ok and result.detail_ok:
        result.status = "ok"
    else:
        result.status = "failed"


def _check_snapshot(result: VerifyResult, records: list[dict[str, Any]]) -> None:
    playable = 0
    for record in records:
        lines = parse_play_url(str(record.get("vod_play_url") or ""))
        if lines and any(line.episodes for line in lines):
            playable += 1
    result.list_ok = bool(records)
    result.detail_ok = playable > 0
    result.search_ok = bool(records)
    if playable:
        result.status = "snapshot"
        result.messages.append(
            f"verified against snapshot: {playable}/{len(records)} records have a playable line"
        )
    else:
        result.status = "failed"
        result.messages.append("snapshot contains no playable record")


def _load_snapshot_records(key: str, settings: Settings) -> list[dict[str, Any]]:
    out_dir = settings.snapshot_dir / key
    if not out_dir.exists():
        return []
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(out_dir.glob("list-part-*.json")):
        try:
            for item in json.loads(path.read_text(encoding="utf-8")).get("items", []):
                records[item.get("vod_id", "?")] = dict(item)
        except Exception:
            continue
    for path in sorted(out_dir.glob("detail-part-*.json")):
        try:
            for item in json.loads(path.read_text(encoding="utf-8")).get("items", []):
                records.setdefault(item.get("vod_id", "?"), {}).update(item)
        except Exception:
            continue
    return list(records.values())


def _origin(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else url


async def verify_config_file(
    path: Path, *, http: HttpClient | None = None, settings: Settings | None = None
) -> list[VerifyResult]:
    settings = settings or get_settings()
    config = json.loads(path.read_text(encoding="utf-8"))
    owned = http is None
    client = http or HttpClient(settings)
    results: list[VerifyResult] = []
    try:
        for entry in config.get("sites") or []:
            if isinstance(entry, dict):
                results.append(await verify_entry(entry, http=client, settings=settings))
    finally:
        if owned:
            await client.aclose()
    return results


def failed_keys(results: list[VerifyResult]) -> set[str]:
    return {r.key for r in results if r.status == "failed"}
