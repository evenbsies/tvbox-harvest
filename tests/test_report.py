"""Report rendering.

The report is the only artefact a human reads after an automated run. Its job is to make
conditions that *look* like failure distinguishable from failure itself: a paywall, a budget
cut-off, a title that yielded nothing while the rest of the site was fine. If those vanish
during rendering, the pipeline starts to lie by omission.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harvest.cli import app
from harvest.config import Settings, get_settings
from harvest.generate.report import (
    RunReport,
    SiteReport,
    build_run_comment,
    combine,
    load_runs,
    write_report,
)

RUNNER = CliRunner()


def _t4_site() -> SiteReport:
    return SiteReport(
        key="4kvm",
        name="4KVM",
        tier=4,
        adapter="playwright_sniff",
        status="degraded",
        vods=3,
        lines=3,
        episodes=3,
        elapsed_ms=144642,
        notes=[
            "paywalled episodes: 2 of 6 walked (source requires login/membership; "
            "not bypassed by design)",
            "per-title cost: avg 48000ms, slowest 51000ms (`功夫女足`)",
        ],
        per_title=[
            {
                "title": "蜘蛛侠：崭新之日",
                "ms": 45000,
                "episodes_walked": 2,
                "requested": 2,
                "available": 2,
                "paywalled": 0,
                "playable_eps": 1,
                "result": "ok",
            },
            {
                "title": "功夫女足",
                "ms": 51000,
                "episodes_walked": 2,
                "requested": 2,
                "available": 6,
                "paywalled": 1,
                "playable_eps": 0,
                "result": "empty",
            },
        ],
    )


def test_markdown_renders_site_notes() -> None:
    report = RunReport(mode="full", finished_at="2026-10-16T15:06:23+00:00")
    report.add_site(_t4_site())

    md = report.to_markdown()

    # The paywall disclosure is the whole point of the note: an operator reading only the
    # Markdown must never conclude that we silently dropped episodes.
    assert "paywalled episodes: 2 of 6 walked" in md
    assert "not bypassed by design" in md


def test_markdown_renders_per_title_cost_table() -> None:
    report = RunReport(mode="full")
    report.add_site(_t4_site())

    md = report.to_markdown()

    assert "per-title cost: avg **48000ms** over 2 title(s)" in md
    assert "| title | ms | eps walked | requested | paywalled | result |" in md
    # Slowest first -- the table exists to answer "what is eating the budget".
    assert md.index("功夫女足") < md.index("蜘蛛侠")


def test_per_title_survives_json_round_trip() -> None:
    report = RunReport(mode="full")
    report.add_site(_t4_site())

    data = json.loads(json.dumps(report.as_dict()))

    rows = data["sites"][0]["per_title"]
    assert len(rows) == 2
    assert rows[1]["ms"] == 51000
    assert rows[1]["result"] == "empty"


def test_run_comment_carries_notes() -> None:
    """The submitter is the only one who can act on a paywall or a budget cut-off."""
    report = RunReport(mode="full")
    report.add_site(_t4_site())

    comment = build_run_comment(report, subscribe_url="https://example.invalid/index.json")

    assert "paywalled episodes" in comment
    assert "订阅地址" in comment
    # A degraded site must be called out, not buried under a cheerful headline.
    assert "需要关注" in comment
    assert "`4kvm`" in comment


def test_run_comment_reports_a_clean_run_without_a_problems_block() -> None:
    report = RunReport(mode="full")
    report.add_site(
        SiteReport(key="ok-site", name="ok", tier=1, adapter="maccms_v10", status="ok", vods=298)
    )

    comment = build_run_comment(report)

    assert "1/1 成功" in comment
    assert "需要关注" not in comment


def test_markdown_omits_per_title_section_for_tier1() -> None:
    report = RunReport(mode="full")
    report.add_site(
        SiteReport(
            key="validation-lziapi",
            name="lziapi",
            tier=1,
            adapter="maccms_v10",
            status="ok",
            vods=198,
            lines=396,
            episodes=608,
        )
    )

    md = report.to_markdown()

    assert "per-title cost" not in md
    assert "| 198 |" in md or "| validation-lziapi | T1" in md


# -- per-site report files, and combining them ----------------------------------------------
#
# The workflow harvests sites in parallel: one matrix job each, every job writing today's report.
# Without a per-site tag they all land on data/reports/<date>.json, and merging the artifacts
# keeps only one -- which is why both the run summary and the Issue comment came back empty.


def _settings(tmp_path: Path, tag: str = "") -> Settings:
    return Settings(root=tmp_path, report_tag=tag)


def _site(key: str, **overrides) -> SiteReport:
    base = {
        "key": key,
        "name": key,
        "tier": 1,
        "adapter": "maccms_v10",
        "status": "ok",
        "vods": 10,
        "lines": 20,
        "episodes": 30,
    }
    base.update(overrides)
    return SiteReport(**base)


def test_report_tag_keeps_parallel_sites_apart(tmp_path: Path) -> None:
    for key in ("alpha", "beta", "gamma"):
        report = RunReport(mode="full")
        report.add_site(_site(key))
        write_report(report, _settings(tmp_path, tag=key))

    names = sorted(p.name for p in (tmp_path / "data" / "reports").glob("*.json"))
    assert len(names) == 3, names
    assert all(any(key in n for key in ("alpha", "beta", "gamma")) for n in names), names


def test_load_runs_reads_every_file_and_every_run_inside_one(tmp_path: Path) -> None:
    """`write_report` appends, so one file can carry several runs."""
    settings = _settings(tmp_path)
    for _ in range(3):
        report = RunReport(mode="full")
        report.add_site(_site("alpha", vods=10))
        write_report(report, settings)

    runs = load_runs(tmp_path / "data" / "reports")

    assert len(runs) == 3


def test_combine_keeps_the_newest_row_per_site(tmp_path: Path) -> None:
    """A later run is the current truth; summing would double-count and fake growth."""
    settings = _settings(tmp_path)
    first = RunReport(mode="full")
    first.add_site(_site("alpha", vods=10))
    first.add_site(_site("beta", vods=5))
    write_report(first, settings)

    second = RunReport(mode="full")
    second.add_site(_site("alpha", vods=298))  # alpha grew
    write_report(second, settings)

    combined = combine(load_runs(tmp_path / "data" / "reports"))
    rows = {s.key: s for s in combined.sites}

    assert rows["alpha"].vods == 298
    assert rows["beta"].vods == 5
    assert combined.as_dict()["summary"]["vods"] == 303


def test_load_runs_skips_the_combined_file_it_wrote(tmp_path: Path) -> None:
    """`combine` unions rows across files, so feeding it its own output would double-count."""
    settings = _settings(tmp_path)
    alpha = RunReport(mode="full")
    alpha.add_site(_site("alpha"))
    write_report(alpha, settings)
    beta = RunReport(mode="full")
    beta.add_site(_site("beta"))
    write_report(beta, settings)

    runs = load_runs(tmp_path / "data" / "reports")
    assert len(runs) == 2

    combined = combine(runs)
    (tmp_path / "data" / "reports" / "2026-10-16.combined.json").write_text(
        json.dumps(combined.as_dict(), ensure_ascii=False), encoding="utf-8"
    )

    # The combined file records 2 sites; if it were read back, this would be 3 runs.
    assert len(load_runs(tmp_path / "data" / "reports")) == 2


def test_combine_unions_per_site_files(tmp_path: Path) -> None:
    """The workflow writes one report per matrix job, so combining must union, not overwrite."""
    for key in ("alpha", "beta", "gamma"):
        report = RunReport(mode="full")
        report.add_site(_site(key, vods=100))
        write_report(report, _settings(tmp_path, tag=key))

    combined = combine(load_runs(tmp_path / "data" / "reports"))

    assert {s.key for s in combined.sites} == {"alpha", "beta", "gamma"}
    assert combined.as_dict()["summary"]["vods"] == 300


def test_summary_command_writes_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end through the CLI, which is how the workflow calls it."""
    settings = _settings(tmp_path)
    report = RunReport(mode="full")
    report.add_site(_site("alpha", vods=298, lines=596, episodes=818))
    report.add_site(_site("beta", status="failed", vods=0, lines=0, episodes=0))
    write_report(report, settings)
    reports_dir = tmp_path / "data" / "reports"

    monkeypatch.setenv("HARVEST_ROOT", str(tmp_path))
    get_settings.cache_clear()

    gh_output = tmp_path / ".ci" / "out.txt"
    comment = tmp_path / ".ci" / "comment.md"
    gh_output.parent.mkdir(parents=True, exist_ok=True)
    result = RUNNER.invoke(
        app,
        [
            "summary",
            "--reports",
            str(reports_dir),
            "--github-output",
            str(gh_output),
            "--comment",
            str(comment),
            "--subscribe-url",
            "https://example.invalid/index.json",
        ],
    )

    assert result.exit_code == 0, result.output
    written = gh_output.read_text(encoding="utf-8")
    # The workflow reads these two keys from GITHUB_OUTPUT; an empty summary is the bug fixed here.
    assert "summary=sites 1/2, vods 298, lines 596" in written
    assert "sites_ok=1" in written
    assert "**采集结果**" in comment.read_text(encoding="utf-8")
    assert "需要关注" in comment.read_text(encoding="utf-8")

    get_settings.cache_clear()


def test_summary_command_survives_an_empty_report_dir(tmp_path: Path) -> None:
    """The report job must not fail just because a run produced nothing."""
    empty = tmp_path / "nothing"
    empty.mkdir()
    output = tmp_path / "out.txt"

    result = RUNNER.invoke(
        app, ["summary", "--reports", str(empty), "--github-output", str(output)]
    )

    assert result.exit_code == 0, result.output
    assert "summary=no report produced" in output.read_text(encoding="utf-8")
