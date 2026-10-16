"""MacCMS-compatible snapshots: the runtime data source for bridged sites.

For Tier 3 and Tier 4 sites the harvested data cannot be served by the upstream (there is no
upstream API to serve it), so it has to be materialised. This module writes it in **exactly
the shape MacCMS returns**, which is what makes the optional gateway a thin read-only layer
rather than a translator: the gateway can answer ``ac=list`` / ``ac=detail`` by slicing a
JSON file.

Two decisions worth calling out:

* **Sharding by size, not count.** Files are capped (~5 MB) to stay friendly to CDN limits;
  a single site can legitimately be enormous.
* **Alternate hosts become extra lines.** MacCMS's ``vod_play_from``/``vod_play_url`` pair
  has nowhere to express "and here is the same episode on a mirror host". Rather than lose
  that information, each mirror becomes its own switchable line -- which is precisely what a
  user wants, and what the source site's own player was doing invisibly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harvest.config import Settings, get_settings
from harvest.models import SiteConfig, SiteData, Vod, build_play_url, utcnow_iso

MAX_SHARD_BYTES = 5 * 1024 * 1024


@dataclass
class SnapshotResult:
    site_key: str
    parts: list[Path] = field(default_factory=list)
    manifest_path: Path | None = None
    total_bytes: int = 0
    vod_count: int = 0

    @property
    def part_count(self) -> int:
        return len(self.parts)


def vod_to_list_record(vod: Vod, cat_id_by_name: dict[str, str]) -> dict[str, Any]:
    """The compact form MacCMS returns for ``ac=list`` (no play URLs)."""
    type_name = vod.cat_norm or (vod.cat_raw[0] if vod.cat_raw else "")
    type_id = cat_id_by_name.get(type_name, "0")
    return {
        "vod_id": vod.sid or vod.vid,
        "vod_name": vod.name,
        "type_id": type_id,
        "type_name": type_name,
        "vod_pic": vod.pic or "",
        "vod_remarks": vod.remarks or "",
        "vod_year": str(vod.year or ""),
        "vod_time": vod.updated_at or "",
        "vod_area": vod.area or "",
        "vod_lang": vod.lang or "",
    }


def vod_to_detail_record(vod: Vod, cat_id_by_name: dict[str, str]) -> dict[str, Any]:
    """The full form MacCMS returns for ``ac=detail``, including play addresses."""
    record = vod_to_list_record(vod, cat_id_by_name)
    play_from, play_url = build_play_url(vod.lines, expand_alts_as_lines=True)
    record.update(
        {
            "vod_play_from": play_from,
            "vod_play_url": play_url,
            "vod_content": vod.content or "",
            "vod_actor": vod.actor or "",
            "vod_director": vod.director or "",
            "vod_serial": str(vod.episode_count),
            "vod_duration": "",
            "vod_score": "",
            "vod_en": vod.vid,
        }
    )
    return record


def write_snapshot(
    site: SiteConfig,
    data: SiteData,
    *,
    settings: Settings | None = None,
    max_shard_bytes: int = MAX_SHARD_BYTES,
) -> SnapshotResult:
    settings = settings or get_settings()
    out_dir = settings.snapshot_dir / site.key
    out_dir.mkdir(parents=True, exist_ok=True)

    # Clear previous parts so a shrinking catalogue does not leave orphaned shards behind
    # (the gateway would happily serve a ghost title forever).
    for old in list(out_dir.glob("*-part-*.json")) + list(out_dir.glob("part-*.json")):
        old.unlink()

    cat_id_by_name: dict[str, str] = {}
    for cat in data.categories:
        cat_id_by_name.setdefault(cat.norm or cat.name, cat.id)

    result = SnapshotResult(site_key=site.key)

    for kind, records in (
        ("list", [vod_to_list_record(v, cat_id_by_name) for v in data.vods]),
        ("detail", [vod_to_detail_record(v, cat_id_by_name) for v in data.vods]),
    ):
        shards = _shard_records(records, max_shard_bytes)
        for index, shard in enumerate(shards, start=1):
            payload = {
                "site_key": site.key,
                "kind": kind,
                "part": index,
                "parts_total": len(shards),
                "generated_at": utcnow_iso(),
                "items": shard,
            }
            path = out_dir / f"{kind}-part-{index:03d}.json"
            _write_json(path, payload)
            result.parts.append(path)
            result.total_bytes += path.stat().st_size

    sid_map = {str(v.sid or v.vid): v.vid for v in data.vods}
    manifest = {
        "site_key": site.key,
        "site_name": site.name,
        "base_url": site.base_url,
        "adapter": data.adapter,
        "tier": data.tier,
        "generated_at": utcnow_iso(),
        "counts": {
            "vods": data.vod_count,
            "lines": data.line_count,
            "episodes": data.episode_count,
            "list_parts": len([p for p in result.parts if p.name.startswith("list-")]),
            "detail_parts": len([p for p in result.parts if p.name.startswith("detail-")]),
        },
        "classes": [
            {"type_id": c.id, "type_name": c.norm or c.name, "raw_name": c.name}
            for c in data.categories
        ],
        "search_index": _search_index(data.vods),
        "sid_map": sid_map,
        "stats": data.stats,
        "errors": data.errors,
    }
    manifest_path = out_dir / "manifest.json"
    _write_json(manifest_path, manifest)
    result.manifest_path = manifest_path
    result.total_bytes += manifest_path.stat().st_size
    result.vod_count = data.vod_count
    return result


def _shard_records(records: list[dict[str, Any]], max_bytes: int) -> list[list[dict[str, Any]]]:
    shards: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for record in records:
        encoded = len(json.dumps(record, ensure_ascii=False).encode("utf-8"))
        if current and size + encoded > max_bytes:
            shards.append(current)
            current, size = [], 0
        current.append(record)
        size += encoded
    if current or not shards:
        shards.append(current)
    return shards


def _search_index(vods: list[Vod]) -> list[dict[str, str]]:
    """A compact lowercased title index so the gateway can answer ``wd=`` cheaply."""
    index: list[dict[str, str]] = []
    for vod in vods:
        index.append(
            {
                "sid": str(vod.sid or vod.vid),
                "t": vod.name.lower(),
                "y": str(vod.year or ""),
            }
        )
    return index


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=None) + "\n", encoding="utf-8")


def load_snapshot(site_key: str, settings: Settings | None = None) -> dict[str, Any]:
    """Read a snapshot for verification/reporting: returns merged list+detail records."""
    settings = settings or get_settings()
    out_dir = settings.snapshot_dir / site_key
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        return {}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(out_dir.glob("list-part-*.json")):
        for item in json.loads(path.read_text(encoding="utf-8")).get("items", []):
            records[item["vod_id"]] = dict(item)
    for path in sorted(out_dir.glob("detail-part-*.json")):
        for item in json.loads(path.read_text(encoding="utf-8")).get("items", []):
            records.setdefault(item["vod_id"], {}).update(item)
    manifest["records"] = list(records.values())
    return manifest
