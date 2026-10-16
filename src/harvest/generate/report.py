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
    return "-"


def write_report(report: RunReport, settings: Settings | None = None) -> tuple[Path, Path]:
    settings = settings or get_settings()
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = (report.started_at or utcnow_iso())[:10]
    json_path = settings.reports_dir / f"{stamp}.json"
    md_path = settings.reports_dir / f"{stamp}.md"

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


def build_issue_comment(report: SiteReport, subscribe_url: str = "") -> str:
    """The condensed summary posted back onto the submitting Issue."""
    lines = [
        f"采集完成：`{report.key}`",
        "",
        f"- 适配器 / 层级：`{report.adapter}` / **T{report.tier}**",
        f"- 影片 **{report.vods}** 部，线路 **{report.lines}** 条，剧集 **{report.episodes}** 集",
        f"- 过滤条目 **{report.filtered_total}**（博彩命中 host {sum(report.bc_hits.values())} 次）",
    ]
    if report.status != "ok":
        lines.append(f"- 状态：**{report.status}**")
    if report.errors:
        lines.append(f"- 错误 {len(report.errors)} 条：")
        lines.extend(f"  - `{e.get('where')}`: {e.get('error')}" for e in report.errors[:5])
    if report.unknown_categories:
        top = sorted(report.unknown_categories.items(), key=lambda kv: -kv[1])[:5]
        lines.append("- 未识别分类（可用于补充映射表）：" + ", ".join(f"`{k}`×{v}" for k, v in top))
    # Notes explain conditions that look like failure but are answers from the source itself.
    # They must reach the Issue: the submitter is the one who can act on them.
    for note in report.notes[:6]:
        lines.append(f"- {note}")
    if subscribe_url:
        lines += ["", f"订阅地址：{subscribe_url}"]
    return "\n".join(lines)


def subsites_from_configs(configs: list[SiteConfig]) -> list[str]:
    return [c.key for c in configs]
