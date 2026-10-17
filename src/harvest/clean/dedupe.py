"""De-duplication and the cleaning pipeline that turns raw ``SiteData`` into something
safe to render.

Ordering inside :func:`clean_site_data` matters and is intentional:

1. Structural pruning (parse + plausibility) -- cheapest, removes the obvious junk.
2. Per-item cleaning (title/year normalization, category mapping, address filtering,
   auto-numbering episodes, collapsing duplicate episode URLs into ``alts``).
3. Blacklists (adult, then gambling) -- run after normalization so matchers see clean text.
4. Intra-site de-duplication by ``(vid)`` and by fingerprint.

Cross-site merging is deliberately **not** done here. TVBox itself presents one search
result row per site, so collapsing duplicates across sites would only destroy working
lines. Instead we emit a merge *report* (see ``generate/report.py``) and leave an
aggregation gateway as a future extension.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from harvest.clean.blacklist import adult_matcher
from harvest.clean.classify import load_category_map
from harvest.clean.gambling import check_entry, check_manifest, scan_site_html
from harvest.clean.normalize import (
    apply_title_normalization,
    canon_media_url,
    canon_resource_url,
    fingerprint,
    host_of,
    is_plausible_media_url,
)
from harvest.models import AltURL, Category, Episode, Line, SiteData, Vod


@dataclass
class DropLog:
    """Why things were removed, counted by reason. Surfaced verbatim in reports."""

    counts: Counter = field(default_factory=Counter)
    examples: dict[str, list[str]] = field(default_factory=dict)
    bc_hosts: Counter = field(default_factory=Counter)

    def add(self, reason: str, example: str = "") -> None:
        self.counts[reason] += 1
        if example:
            bucket = self.examples.setdefault(reason, [])
            if len(bucket) < 8:
                bucket.append(example)

    def as_dict(self) -> dict:
        return {
            "total": sum(self.counts.values()),
            "by_reason": dict(self.counts.most_common()),
            "examples": self.examples,
        }


@dataclass
class CleanResult:
    site_data: SiteData
    dropped: DropLog
    unknown_categories: Counter = field(default_factory=Counter)
    bc_site_scan: dict = field(default_factory=dict)
    merged_fingerprints: int = 0


def clean_site_data(
    site_data: SiteData,
    *,
    page_html: str | None = None,
    bc_filter: bool = True,
    adult_filter: bool = True,
    quarantine_bc_site: bool = False,
) -> CleanResult:
    dropped = DropLog()
    cmap = load_category_map()
    unknown: Counter = Counter()

    if quarantine_bc_site and page_html:
        scan = scan_site_html(page_html, site_data.base_url)
        if scan.quarantined:
            dropped.add("bc_site_quarantined")
            return CleanResult(
                site_data=SiteData(
                    site_key=site_data.site_key,
                    site_name=site_data.site_name,
                    base_url=site_data.base_url,
                    adapter=site_data.adapter,
                    tier=site_data.tier,
                    categories=[],
                    vods=[],
                    stats=site_data.stats,
                    errors=site_data.errors,
                ),
                dropped=dropped,
                bc_site_scan=scan.as_evidence(),
            )
        site_data.stats["bc_site_scan"] = scan.as_evidence()

    kept: list[Vod] = []
    seen_vids: set[str] = set()
    seen_fps: dict[str, str] = {}

    for vod in site_data.vods:
        # --- structural -------------------------------------------------------------
        if not vod.vid or not vod.name:
            dropped.add("missing_id_or_name")
            continue
        if vod.vid in seen_vids:
            dropped.add("duplicate_vid", vod.vid)
            continue

        # --- per-item cleaning ------------------------------------------------------
        apply_title_normalization(vod)
        vod.cat_raw = [c for c in vod.cat_raw if c and c.strip()]
        decision = cmap.resolve(vod.cat_raw)
        if decision.dropped_reason and adult_filter:
            dropped.add(decision.dropped_reason, vod.name)
            continue
        vod.cat_norm = decision.target
        # Only a category that matched *nothing* is a real gap. One that was deliberately
        # listed under 其他 is an explicit decision, so counting it would bury the signal.
        if not decision.resolved and vod.cat_raw:
            for raw in vod.cat_raw:
                unknown[raw] += 1

        cleaned_lines: list[Line] = []
        for line in vod.lines:
            episodes: list[Episode] = []
            seen_urls: set[str] = set()
            for ep in line.episodes:
                url = canon_media_url(ep.url)
                if not is_plausible_media_url(url):
                    dropped.add("implausible_media_url", f"{vod.name}:{ep.url[:60]}")
                    continue
                if url in seen_urls:
                    # Same address twice in one line: fold into alts rather than losing it.
                    if episodes:
                        episodes[-1].alts.append(_alt(url, "dup"))
                    dropped.add("duplicate_episode_url")
                    continue
                seen_urls.add(url)
                episodes.append(
                    Episode(
                        index=len(episodes) + 1,
                        name=ep.name or f"第{len(episodes) + 1:02d}集",
                        url=url,
                        # Carry mirror/failover addresses through: they are the whole reason a
                        # Tier-4 site is usable at all, and re-deriving them later is impossible.
                        alts=[_alt(a.url, a.note or "alts") for a in ep.alts],
                    )
                )

            # Re-index and fill missing names so the rendered MacCMS payload is well formed.
            for i, ep in enumerate(episodes, start=1):
                ep.index = i
                if not ep.name or ep.name.startswith("http"):
                    ep.name = f"第{i:02d}集"

            if episodes:
                cleaned_lines.append(Line(name=line.name or f"线路{len(cleaned_lines) + 1}", episodes=episodes))
            else:
                dropped.add("empty_line", vod.name)

        vod.lines = cleaned_lines
        if not vod.playable:
            dropped.add("no_playable_line", vod.name)
            continue

        # --- blacklists -------------------------------------------------------------
        if adult_filter:
            hits = adult_matcher().match(vod.name, vod.remarks, vod.content)
            if hits:
                dropped.add(f"adult:{','.join(hits[:3])}", vod.name)
                continue

        if bc_filter:
            verdict = check_entry(vod, policy_bc_filter=True)
            if verdict.dropped:
                for reason in verdict.reasons:
                    dropped.add(reason.split(":")[0], vod.name)
                for host in verdict.bad_hosts:
                    dropped.bc_hosts[host] += 1
                continue

        # --- intra-site dedup -------------------------------------------------------
        fp = vod.fingerprint or fingerprint(vod.name, vod.year)
        vod.fingerprint = fp
        if fp in seen_fps:
            # Prefer the entry with more episodes; merge the loser's lines in as extras.
            prev_vid = seen_fps[fp]
            other = next((v for v in kept if v.vid == prev_vid), None)
            if other is not None and other.episode_count < vod.episode_count:
                kept[kept.index(other)] = vod
                seen_fps[fp] = vod.vid
                seen_vids.discard(prev_vid)
            else:
                if other is not None:
                    existing_names = {ln.name for ln in other.lines}
                    for line in vod.lines:
                        if line.name not in existing_names:
                            other.lines.append(line)
            dropped.add("duplicate_fingerprint", vod.name)
            continue

        seen_fps[fp] = vod.vid
        seen_vids.add(vod.vid)
        kept.append(vod)

    site_data.vods = kept
    site_data.categories = _norm_categories(site_data, cmap)

    return CleanResult(
        site_data=site_data,
        dropped=dropped,
        unknown_categories=unknown,
        bc_site_scan=site_data.stats.get("bc_site_scan", {}),
        merged_fingerprints=sum(1 for r in dropped.counts if r == "duplicate_fingerprint"),
    )


def _alt(url: str, note: str) -> AltURL:
    return AltURL(url=url, host=host_of(url), note=note)


def _norm_categories(site_data: SiteData, cmap) -> list[Category]:
    out = []
    for cat in site_data.categories:
        norm = cmap.resolve([cat.name, cat.norm]).target
        out.append(Category(id=cat.id, name=cat.name, norm=norm))
    if not out:
        seen: dict[str, str] = {}
        for vod in site_data.vods:
            for raw in vod.cat_raw or [vod.cat_norm]:
                seen.setdefault(raw, vod.cat_norm)
        out = [Category(id=str(i), name=name, norm=norm) for i, (name, norm) in enumerate(seen.items())]
    return out


def dedupe_episode_urls(vod: Vod) -> int:
    """Collapse identical URLs inside a single line into ``alts``. Returns count removed."""
    removed = 0
    for line in vod.lines:
        seen: dict[str, Episode] = {}
        keep: list[Episode] = []
        for ep in line.episodes:
            if ep.url in seen:
                seen[ep.url].alts.append(_alt(ep.url, "dup-line"))
                removed += 1
                continue
            seen[ep.url] = ep
            keep.append(ep)
        for i, ep in enumerate(keep, start=1):
            ep.index = i
        line.episodes = keep
    return removed


def verify_manifest_urls(site_data: SiteData, manifests: dict[str, str]) -> DropLog:
    """Optional: scan already-fetched HLS manifests for injected ad domains."""
    log = DropLog()
    for vod in site_data.vods:
        for line in vod.lines:
            for ep in line.episodes:
                text = manifests.get(ep.url)
                if not text:
                    continue
                verdict = check_manifest(text)
                if verdict.dropped:
                    for reason in verdict.reasons:
                        log.add(reason.split(":")[0], f"{vod.name}:{ep.url[:60]}")
    return log


def resource_url_canon(url: str) -> str:
    return canon_resource_url(url)
