"""Run reports.

A harvest run makes hundreds of small decisions (dropped a title, skipped a site, fell back
to an inferior tier). Those decisions must be visible, or the pipeline becomes untrustworthy
the moment something goes wrong. Reports are written both as machine-readable JSON and as
Markdown, and a condensed summary is posted back to the originating Issue.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harvest.config import Settings, get_settings
from harvest.models import SiteConfig, utcnow_iso


@dataclass
class SiteReport:
    key: str
    name: str
    tier: int = 0
    adapter: str = ""
    status: str = "ok"  # ok | degraded | failed | skipped | needs_mapping | quarantined
    vods: int = 0
    lines: int = 0
    episodes: int = 0
    filtered: dict[str, int] = field(default_factory=dict)
    bc_hits: dict[str, int] = field(default_factory=dict)
    unknown_categories: dict[str, int] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    # Per-title wall-clock, Tier 4 only. It is the only number that lets an operator size the
    # batch: total elapsed_ms for a browser-driven site is just "however many titles we walked".
    per_title: list[dict[str, Any]] = field(default_factory=list)

    @property
    def filtered_total(self) -> int:
        return sum(self.filtered.values())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SiteReport:
        """Rebuild a report row from JSON.

        Needed because the workflow harvests sites in parallel and merges their artifacts, so the
        combined view has to be reconstructed from the per-site files rather than held in memory.
        Unknown keys are dropped rather than raising: a report written by a newer version must not
        make an older reader fail.
        """
        return cls(
            key=str(data.get("key") or "?"),
            name=str(data.get("name") or data.get("key") or "?"),
            tier=int(data.get("tier") or 0),
            adapter=str(data.get("adapter") or ""),
            status=str(data.get("status") or "unknown"),
            vods=int(data.get("vods") or 0),
            lines=int(data.get("lines") or 0),
            episodes=int(data.get("episodes") or 0),
            filtered={str(k): int(v) for k, v in (data.get("filtered") or {}).items()},
            bc_hits={str(k): int(v) for k, v in (data.get("bc_hits") or {}).items()},
            unknown_categories={
                str(k): int(v) for k, v in (data.get("unknown_categories") or {}).items()
            },
            errors=[e for e in (data.get("errors") or []) if isinstance(e, dict)],
            notes=[str(n) for n in (data.get("notes") or [])],
            artifacts=[str(a) for a in (data.get("artifacts") or [])],
            elapsed_ms=int(data.get("elapsed_ms") or 0),
            per_title=[r for r in (data.get("per_title") or []) if isinstance(r, dict)],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "tier": self.tier,
            "adapter": self.adapter,
            "status": self.status,
            "vods": self.vods,
            "lines": self.lines,
            "episodes": self.episodes,
            "filtered_total": self.filtered_total,
            "filtered": dict(sorted(self.filtered.items(), key=lambda kv: -kv[1])),
            "bc_hits": dict(sorted(self.bc_hits.items(), key=lambda kv: -kv[1])),
            "unknown_categories": dict(
                sorted(self.unknown_categories.items(), key=lambda kv: -kv[1])[:10]
            ),
            "errors": self.errors[:10],
            "notes": self.notes[:10],
            "artifacts": self.artifacts,
            "elapsed_ms": self.elapsed_ms,
            "per_title": self.per_title[:50],
        }


@dataclass
class RunReport:
    started_at: str = field(default_factory=utcnow_iso)
    finished_at: str | None = None
    mode: str = "full"
    run_id: str = ""
    sites: list[SiteReport] = field(default_factory=list)
    generated: list[dict[str, Any]] = field(default_factory=list)
    verification: list[dict[str, Any]] = field(default_factory=list)
    learned_bc_hosts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add_site(self, report: SiteReport) -> SiteReport:
        self.sites.append(report)
        return report

    @property
    def ok_sites(self) -> int:
        return sum(1 for s in self.sites if s.status == "ok")

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "mode": self.mode,
            "run_id": self.run_id,
            "summary": {
                "sites_total": len(self.sites),
                "sites_ok": self.ok_sites,
                "vods": sum(s.vods for s in self.sites),
                "lines": sum(s.lines for s in self.sites),
                "episodes": sum(s.episodes for s in self.sites),
                "filtered": sum(s.filtered_total for s in self.sites),
                "bc_hosts_learned": len(self.learned_bc_hosts),
            },
            "sites": [s.as_dict() for s in self.sites],
            "generated": self.generated,
            "verification": self.verification,
            "learned_bc_hosts": self.learned_bc_hosts,
            "notes": self.notes,
        }

    def to_markdown(self) -> str:
        data = self.as_dict()
        summary = data["summary"]
        lines: list[str] = []
        lines.append(f"# Harvest report {self.started_at}")
        lines.append("")
        lines.append(
            f"- mode: `{self.mode}`  run: `{self.run_id or '-'}`  finished: `{self.finished_at or '-'}`"
        )
        lines.append(
            f"- sites: **{summary['sites_ok']}/{summary['sites_total']}** ok | "
            f"vods **{summary['vods']}** | lines **{summary['lines']}** | "
            f"episodes **{summary['episodes']}** | filtered **{summary['filtered']}**"
        )
        lines.append("")

        if self.sites:
            lines.append("## Sites")
            lines.append("")
            lines.append("| key | tier | adapter | status | vods | lines | eps | filtered | BC |")
            lines.append("|---|---|---|---|---|---|---|---|---|")
            for site in self.sites:
                lines.append(
                    f"| `{site.key}` | T{site.tier} | {site.adapter} | {site.status} | "
                    f"{site.vods} | {site.lines} | {site.episodes} | {site.filtered_total} | "
                    f"{sum(site.bc_hits.values())} |"
                )
            lines.append("")

        for site in self.sites:
            detail: list[str] = []
            if site.filtered:
                top = sorted(site.filtered.items(), key=lambda kv: -kv[1])[:8]
                detail.append(
                    "  filtered: " + ", ".join(f"`{k}`×{v}" for k, v in top)
                )
            if site.bc_hits:
                top = sorted(site.bc_hits.items(), key=lambda kv: -kv[1])[:5]
                detail.append("  bc hosts: " + ", ".join(f"`{k}`×{v}" for k, v in top))
            if site.unknown_categories:
                top = sorted(site.unknown_categories.items(), key=lambda kv: -kv[1])[:6]
                detail.append("  unmapped categories: " + ", ".join(f"`{k}`×{v}" for k, v in top))
            if site.errors:
                detail.append(f"  errors: {len(site.errors)} (see JSON report)")
            # Site-level notes belong in the human-facing report. They carry the things a reader
            # would otherwise misread as breakage: a paywall, a budget cut-off, a pinned adapter.
            for note in site.notes[:6]:
                detail.append(f"  note: {note}")
            if detail:
                lines.append(f"### `{site.key}`")
                lines.extend(detail)
                lines.append("")

            if site.per_title:
                slowest = sorted(site.per_title, key=lambda r: -int(r.get("ms") or 0))[:8]
                avg = int(sum(int(r.get("ms") or 0) for r in site.per_title) / len(site.per_title))
                lines.append(
                    f"  per-title cost: avg **{avg}ms** over {len(site.per_title)} title(s); "
                    f"slowest **{int(slowest[0].get('ms') or 0)}ms**"
                )
                lines.append("")
                lines.append("| title | ms | eps walked | requested | paywalled | result |")
                lines.append("|---|---|---|---|---|---|")
                for row in slowest:
                    lines.append(
                        f"| {row.get('title', '-')} | {row.get('ms', '-')} | "
                        f"{row.get('episodes_walked', '-')} | {row.get('requested', '-')} | "
                        f"{row.get('paywalled', '-')} | {row.get('result', '-')} |"
                    )
                lines.append("")

        if self.verification:
            lines.append("## Verification")
            lines.append("")
            lines.append("| site | list | detail | search | result |")
            lines.append("|---|---|---|---|---|")
            for item in self.verification:
                lines.append(
                    f"| `{item.get('key')}` | {_mark(item.get('list'))} | {_mark(item.get('detail'))} | "
                    f"{_mark(item.get('search'))} | {item.get('status', '-')} |"
                )
            lines.append("")

        if self.learned_bc_hosts:
            lines.append("## Learned gambling hosts")
            lines.append("")
            lines.extend(f"- `{h}`" for h in self.learned_bc_hosts)
            lines.append("")

        if self.notes:
            lines.append("## Notes")
            lines.append("")
            lines.extend(f"- {n}" for n in self.notes)
            lines.append("")

        return "\n".join(lines)


def _mark(value: Any) -> str:
    if value is True:
        return "ok"
    if value is False:
        return "FAIL"
    # None means the check did not run, which is not the same as a missing value: render it as
    # an explicit "not exercised" so a reader cannot mistake silence for a pass.
    return "n/a"


def write_report(report: RunReport, settings: Settings | None = None) -> tuple[Path, Path]:
    settings = settings or get_settings()
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = (report.started_at or utcnow_iso())[:10]
    # The suffix keeps parallel per-site jobs from overwriting one another's report when their
    # artifacts are merged; see Settings.report_tag.
    suffix = settings.report_suffix
    json_path = settings.reports_dir / f"{stamp}{suffix}.json"
    md_path = settings.reports_dir / f"{stamp}{suffix}.md"

    existing: list[dict[str, Any]] = []
    if json_path.exists():
        try:
            prior = json.loads(json_path.read_text(encoding="utf-8"))
            if isinstance(prior, list):
                existing = prior
            elif isinstance(prior, dict):
                existing = [prior]
        except Exception:
            existing = []
    existing.append(report.as_dict())
    json_path.write_text(
        json.dumps(existing if len(existing) > 1 else report.as_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if md_path.exists() and len(existing) > 1:
        prior_md = md_path.read_text(encoding="utf-8")
        md_path.write_text(prior_md + "\n\n---\n\n" + report.to_markdown(), encoding="utf-8")
    else:
        md_path.write_text(report.to_markdown(), encoding="utf-8")
    return json_path, md_path


def subsites_from_configs(configs: list[SiteConfig]) -> list[str]:
    return [c.key for c in configs]


# -- aggregation ----------------------------------------------------------------------------


def _as_run_dicts(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def load_runs(reports_dir: Path) -> list[RunReport]:
    """Every run record in every report file under ``reports_dir``, oldest first.

    Mind the shape: :func:`write_report` *appends*, so one file can hold several runs, and the
    workflow writes one file per site. A reader that assumes "one file, one run" silently sees
    only part of what happened.
    """
    runs: list[RunReport] = []
    for path in sorted(reports_dir.glob("*.json")):
        # Skip our own output. Reading it back would resurrect rows for sites that a later run
        # has since dropped, making the summary refuse to shrink.
        if path.name.endswith(".combined.json"):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for record in _as_run_dicts(payload):
            sites = [s for s in (record.get("sites") or []) if isinstance(s, dict)]
            run = RunReport(
                started_at=str(record.get("started_at") or ""),
                finished_at=record.get("finished_at"),
                mode=str(record.get("mode") or "full"),
                run_id=str(record.get("run_id") or ""),
                sites=[SiteReport.from_dict(s) for s in sites],
                generated=[g for g in (record.get("generated") or []) if isinstance(g, dict)],
                verification=[v for v in (record.get("verification") or []) if isinstance(v, dict)],
                learned_bc_hosts=[str(h) for h in (record.get("learned_bc_hosts") or [])],
                notes=[str(n) for n in (record.get("notes") or [])],
            )
            runs.append(run)
    return runs


def combine(runs: list[RunReport], *, mode: str = "combined") -> RunReport:
    """Fold many per-site runs into one report: newest row per site, totals summed.

    Newest wins rather than accumulating, because a later run is the current truth about a site --
    summing would double-count episodes and make a stable site look like it was growing.
    """
    latest: dict[str, SiteReport] = {}
    for run in runs:
        for site in run.sites:
            latest[site.key] = site

    ordered = [latest[key] for key in sorted(latest)]
    combined = RunReport(
        started_at=min((r.started_at for r in runs if r.started_at), default=utcnow_iso()),
        finished_at=max((r.finished_at for r in runs if r.finished_at), default=None),
        mode=mode,
        sites=ordered,
    )

    # Verification is per-site as well; keep the newest verdict for each.
    verdicts: dict[str, dict[str, Any]] = {}
    for run in runs:
        for item in run.verification:
            verdicts[str(item.get("key") or "?")] = item
    combined.verification = [verdicts[key] for key in sorted(verdicts)]

    seen_hosts: list[str] = []
    for run in runs:
        for host in run.learned_bc_hosts:
            if host not in seen_hosts:
                seen_hosts.append(host)
    combined.learned_bc_hosts = seen_hosts

    # Generated-config info is only meaningful from the most recent run, and only once.
    if runs:
        combined.generated = runs[-1].generated
    seen_notes: list[str] = []
    for run in runs:
        for note in run.notes:
            if note not in seen_notes:
                seen_notes.append(note)
    combined.notes = seen_notes
    return combined


def combined_paths(settings: Settings, started_at: str = "") -> tuple[Path, Path]:
    stamp = (started_at or utcnow_iso())[:10]
    return (
        settings.reports_dir / f"{stamp}.combined.json",
        settings.reports_dir / f"{stamp}.combined.md",
    )


def build_run_comment(report: RunReport, subscribe_url: str = "") -> str:
    """The comment posted on an Issue after its site has been harvested.

    Written in Chinese to match the Issue form, and deliberately blunt about anything that did
    not go cleanly: a submitter who sees "采集流程已执行完成" with no numbers learns nothing.
    """
    data = report.as_dict()
    totals = data["summary"]
    lines = [
        "**采集结果**",
        "",
        f"- 站点：{totals['sites_ok']}/{totals['sites_total']} 成功",
        f"- 影片 {totals['vods']} 部 / 线路 {totals['lines']} 条 / 剧集 {totals['episodes']} 集",
        f"- 过滤条目 {totals['filtered']}（含博彩命中）",
    ]

    problem_sites = [s for s in report.sites if s.status not in ("ok", "snapshot")]
    if problem_sites:
        lines += ["", "**需要关注**", ""]
        for site in problem_sites[:5]:
            detail = "; ".join(str(e.get("error", "")) for e in site.errors[:2])
            lines.append(f"- `{site.key}` 状态 `{site.status}` {detail[:160]}".rstrip())

    # Conditions that are answers from the source rather than failures -- a paywall, a budget cap.
    for site in report.sites:
        for note in site.notes[:3]:
            lines.append(f"- `{site.key}`：{note}")

    if subscribe_url:
        lines += ["", f"订阅地址：{subscribe_url}"]
    return "\n".join(lines)
