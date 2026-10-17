"""Read a TVBox config published elsewhere and pull out the entries whose name matches.

Why a separate step from harvesting: a TVBox config is a *catalogue of sources*, and the question
"does any of these carry a 4K line" is answered by reading its names, not by collecting from
them. Importing is therefore a review aid, not a pipeline stage -- it writes a candidates file
that nothing consumes until a human turns an entry into a real recipe.

Two rules that keep this honest:

* **Names only.** A site called `4K影视` or a parse called `洛雪4K` is what gets matched. The
  project has already measured three sources whose names promised 4K and delivered 1080p; this
  module does not repeat that claim, it only reports the label and where it came from.
* **Nothing is enabled.** The output lists provenance (URL + time + pattern) and is deliberately
  not read by `sites/registry.yaml`, so importing cannot silently publish anything.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import yaml

from harvest.config import Settings, get_settings
from harvest.fetch.http import HttpClient
from harvest.models import SiteConfig, utcnow_iso


@dataclass
class ImportedEntry:
    kind: str  # "site" | "parse"
    name: str
    address: str
    type: Any = None
    #: Where in the source document it came from, for a reviewer who wants to check it.
    origin: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "name": self.name, "address": self.address, "type": self.type}


@dataclass
class ImportResult:
    source_url: str
    fetched_at: str = ""
    total_sites: int = 0
    total_parses: int = 0
    match: list[str] = field(default_factory=list)
    matched: list[ImportedEntry] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def sites(self) -> list[ImportedEntry]:
        return [e for e in self.matched if e.kind == "site"]

    @property
    def parses(self) -> list[ImportedEntry]:
        return [e for e in self.matched if e.kind == "parse"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_url": self.source_url,
            "fetched_at": self.fetched_at,
            "total_sites": self.total_sites,
            "total_parses": self.total_parses,
            "match": self.match,
            "matched": [e.as_dict() for e in self.matched],
            "notes": self.notes,
        }


def _normalize(name: str) -> str:
    """Fold full-width forms so `４K` matches a `4K` pattern.

    Deliberately does *not* fold the Chinese numeral: `四K` is a different string, and silently
    treating it as `4K` would make the filter's behaviour depend on a guess. A caller who wants
    both writes `--match "4K|四K"`.
    """
    return unicodedata.normalize("NFKC", name)


def _normalize(name: str) -> str:
    """Fold full-width forms so `４K` matches a `4K` pattern.

    Deliberately does *not* fold the Chinese numeral: `四K` is a different string, and silently
    treating it as `4K` would make the filter's behaviour depend on a guess. A caller who wants
    both writes `--match "4K|四K"`.
    """
    return unicodedata.normalize("NFKC", name)


def _compile(match: list[str]) -> list[re.Pattern[str]]:
    out = []
    for raw in match:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            out.append(re.compile(text, re.IGNORECASE))
        except re.error:
            out.append(re.compile(re.escape(text), re.IGNORECASE))
    return out


def collect_entries(
    config: dict[str, Any], *, match: list[str], kind: str = "both"
) -> tuple[list[ImportedEntry], int, int]:
    """Pick entries whose **name** matches. Returns (matched, total_sites, total_parses)."""
    patterns = _compile(match)
    sites_raw = config.get("sites") or []
    parses_raw = config.get("parses") or []
    total_sites = len(sites_raw) if isinstance(sites_raw, list) else 0
    total_parses = len(parses_raw) if isinstance(parses_raw, list) else 0

    matched: list[ImportedEntry] = []

    if kind in ("both", "site") and isinstance(sites_raw, list):
        for index, entry in enumerate(sites_raw):
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("key") or "").strip()
            address = str(entry.get("api") or "").strip()
            if not name or not patterns or not any(p.search(_normalize(name)) for p in patterns):
                continue
            matched.append(
                ImportedEntry(
                    kind="site", name=name, address=address, type=entry.get("type"), origin=f"sites[{index}]"
                )
            )

    if kind in ("both", "parse") and isinstance(parses_raw, list):
        for index, entry in enumerate(parses_raw):
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            address = str(entry.get("url") or "").strip()
            if not name or not patterns or not any(p.search(_normalize(name)) for p in patterns):
                continue
            matched.append(
                ImportedEntry(
                    kind="parse", name=name, address=address, type=entry.get("type"), origin=f"parses[{index}]"
                )
            )

    return matched, total_sites, total_parses


async def import_config(
    url: str,
    *,
    match: list[str],
    kind: str = "both",
    settings: Settings | None = None,
    http: HttpClient | None = None,
) -> ImportResult:
    settings = settings or get_settings()
    result = ImportResult(source_url=url, match=list(match))

    brand = SiteConfig(key="import", name="import", base_url=url)
    own_client = http is None
    client = http or HttpClient(settings)
    try:
        resp = await client.get(url, site=brand, key="import")
        if resp.status_code != 200:
            result.notes.append(f"HTTP {resp.status_code}：地址不可读")
            return result
        try:
            config = json.loads(resp.text or "{}")
        except json.JSONDecodeError:
            result.notes.append("返回不是 JSON；TVBox 配置必须是 JSON")
            return result
        if not isinstance(config, dict):
            result.notes.append("返回的 JSON 不是对象")
            return result
    finally:
        if own_client:
            await client.aclose()

    result.fetched_at = utcnow_iso()
    result.matched, result.total_sites, result.total_parses = collect_entries(
        config, match=match, kind=kind
    )
    if not result.matched:
        result.notes.append(
            f"没有 name 匹配 {match} 的条目（共 {result.total_sites} 站点 / {result.total_parses} 解析）"
        )
    return result


def candidate_path(url: str, settings: Settings | None = None):
    """One file per source *document*, not per host.

    Two configs on the same host (raw.githubusercontent.com serving many repos) must not collapse
    into one file -- the second write would silently replace the first, which is exactly the kind
    of quiet data loss this project keeps finding.
    """
    settings = settings or get_settings()
    parsed = urlparse(url)
    slug = re.sub(r"[^a-z0-9_.-]+", "-", f"{parsed.netloc}/{parsed.path}".lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)[:110] or "imported"
    return settings.root / "sources" / "imported" / f"{slug}.yaml"


def write_candidates(result: ImportResult, settings: Settings | None = None):
    """Write the matches to `sources/imported/<host>.yaml` for review. Nothing reads it."""
    path = candidate_path(result.source_url, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "# 说明": "由 `harvest import` 生成，供人工复核；pipeline 不会自动读取本文件。",
        "provenance": {
            "source": result.source_url,
            "fetched_at": result.fetched_at,
            "match": result.match,
            "totals": {"sites": result.total_sites, "parses": result.total_parses},
        },
        "sites": [e.as_dict() for e in result.sites],
        "parses": [e.as_dict() for e in result.parses],
    }
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return path
