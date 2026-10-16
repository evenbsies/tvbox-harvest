"""Report rendering.

The report is the only artefact a human reads after an automated run. Its job is to make
conditions that *look* like failure distinguishable from failure itself: a paywall, a budget
cut-off, a title that yielded nothing while the rest of the site was fine. If those vanish
during rendering, the pipeline starts to lie by omission.
"""

from __future__ import annotations

import json

from harvest.generate.report import RunReport, SiteReport, build_issue_comment


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


def test_issue_comment_carries_notes() -> None:
    comment = build_issue_comment(_t4_site(), subscribe_url="https://example.invalid/index.json")

    # The submitter is the only one who can act on a paywall or a budget cut-off.
    assert "paywalled episodes" in comment
    assert "订阅地址" in comment


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
