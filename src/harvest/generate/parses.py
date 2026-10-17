"""Collect "parse routes" (解析线路) into a TVBox `parses[]` array.

The third kind of "line", and the one that is easiest to confuse with the other two:

* a **collection line** (`vod_play_from`) belongs to a site and is published inside `sites[]`;
* a **parse route** belongs to a player: you hand it a page URL and it hands back a stream.
  洛雪TV calls these `official_routes`; 片库-style templates call the same idea 播放器.

Filtering them matters for the same reason as collection lines: a site may offer 默认解析 /
闪电 / TXNP / 洛雪4K, and "I only want 4K" is a filter on the route's *name*, not on the film.

COMPLIANCE NOTE, and it is a real difference: publishing a `parses` entry means the client's play
requests go through that third party's server. It is not "index only" any more. That is why this
is opt-in per source in `config/parses.yaml` and why every discovered endpoint is reported, never
hard-coded into a generated entry the operator did not ask for.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import yaml

from harvest.config import Settings, get_settings
from harvest.fetch.http import HttpClient
from harvest.models import SiteConfig

#: Player kind -> TVBox `parses[].type`. 0 = a normal page/iframe parser, 1 = a JSON parser.
DEFAULT_TYPE_MAP = {"iframe": 0, "web": 0, "json": 1, "json_api": 1}


@dataclass
class ParseSource:
    """One `config/parses.yaml` entry: where a site keeps its route list, and what to keep."""

    name: str
    config: str
    routes_key: str = "official_routes"
    keep: list[str] = field(default_factory=list)
    drop: list[str] = field(default_factory=list)
    type_map: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_TYPE_MAP))
    #: Field holding the player URL template. Kept configurable: one site calls it `api`,
    #: another `player_api`.
    url_field: str = "api"


@dataclass
class ParseResult:
    entries: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Routes seen before filtering, per source -- so "kept nothing" is explainable.
    observed: dict[str, list[str]] = field(default_factory=dict)


def load_parse_sources(settings: Settings | None = None) -> list[ParseSource]:
    settings = settings or get_settings()
    path = settings.config_dir / "parses.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sources: list[ParseSource] = []
    for raw in data.get("parses") or []:
        if not isinstance(raw, dict) or not raw.get("name") or not raw.get("config"):
            continue
        sources.append(
            ParseSource(
                name=str(raw["name"]),
                config=str(raw["config"]),
                routes_key=str(raw.get("routes_key") or "official_routes"),
                keep=[str(x) for x in (raw.get("keep") or [])],
                drop=[str(x) for x in (raw.get("drop") or [])],
                type_map={**DEFAULT_TYPE_MAP, **{str(k): int(v) for k, v in (raw.get("type_map") or {}).items()}},
                url_field=str(raw.get("url_field") or "api"),
            )
        )
    return sources


def _compile(patterns: list[str]) -> list[re.Pattern[str]]:
    out = []
    for raw in patterns:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            out.append(re.compile(text, re.IGNORECASE))
        except re.error:
            out.append(re.compile(re.escape(text), re.IGNORECASE))
    return out


def select_routes(
    routes: list[dict[str, Any]], source: ParseSource, *, base_url: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Filter a site's announced routes and convert them into TVBox `parses` entries."""
    keep = _compile(source.keep)
    drop = _compile(source.drop)
    entries: list[dict[str, Any]] = []
    seen: list[str] = []
    for route in routes:
        if not isinstance(route, dict):
            continue
        name = str(route.get("name") or "").strip()
        if not name:
            continue
        seen.append(name)
        if keep and not any(rx.search(name) for rx in keep):
            continue
        if drop and any(rx.search(name) for rx in drop):
            continue
        raw_url = str(route.get(source.url_field) or "").strip()
        if not raw_url:
            continue
        kind = str(route.get("type") or "").strip().lower()
        entry: dict[str, Any] = {
            "name": name,
            "type": source.type_map.get(kind, 1),
            # Route templates are often site-relative (`/json-player.php?...&url=`); a client
            # needs an absolute address or it will resolve it against its own host.
            "url": urljoin(base_url, raw_url),
        }
        ext: dict[str, Any] = {}
        if route.get("player_codes"):
            ext["player_codes"] = list(route["player_codes"])
        if route.get("official_only"):
            ext["official_only"] = True
        if ext:
            entry["ext"] = ext
        entries.append(entry)
    return entries, seen


async def collect_parses(
    settings: Settings | None = None,
    http: HttpClient | None = None,
) -> ParseResult:
    """Fetch every configured route list and keep only the routes the operator asked for."""
    settings = settings or get_settings()
    sources = load_parse_sources(settings)
    result = ParseResult()
    if not sources:
        return result

    own_client = http is None
    client = http or HttpClient(settings)
    try:
        for source in sources:
            base = source.config.split("/api/")[0].rstrip("/")
            site = SiteConfig(key=source.name, name=source.name, base_url=base)
            try:
                resp = await client.get(source.config, site=site, key="parses")
                payload = json.loads(resp.text or "{}")
            except Exception as exc:
                result.notes.append(f"解析线路 {source.name}: 读取失败 {type(exc).__name__}")
                continue
            routes = payload.get(source.routes_key) if isinstance(payload, dict) else None
            if not isinstance(routes, list):
                result.notes.append(
                    f"解析线路 {source.name}: 返回里没有 {source.routes_key}，跳过"
                )
                continue
            entries, seen = select_routes(routes, source, base_url=base)
            result.observed[source.name] = seen
            kept_names = [e["name"] for e in entries]
            result.notes.append(
                f"解析线路 {source.name}: {len(routes)} 条中保留 {len(entries)} 条 "
                f"({', '.join(kept_names) or '无'})；站上有 {', '.join(seen)}"
            )
            result.entries.extend(entries)
    finally:
        if own_client:
            await client.aclose()
    return result
