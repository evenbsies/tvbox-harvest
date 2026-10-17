"""Filtering play lines by name.

The distinction this file protects: a *line* labelled 4K and a *title* labelled 4K are different
claims. Sites offer 闪电 / 非凡 / 量子 for the same film, and sometimes one line is named `4K高清`.
Only the line name is evidence for "this stream is 4K"; the title's `vod_remarks` is not, and an
unnamed line is not either.
"""

from __future__ import annotations

from harvest.clean.lines import filter_lines, line_names
from harvest.models import Episode, Line, SiteData, Vod


def _site(*specs: tuple[str, list[str]]) -> SiteData:
    data = SiteData(site_key="s", site_name="s", base_url="https://s.test", adapter="maccms_v10")
    for name, line_names_ in specs:
        lines = [
            Line(name=ln, episodes=[Episode(index=1, name="第01集", url=f"https://cdn.test/{i}.m3u8")])
            for i, ln in enumerate(line_names_, start=1)
        ]
        data.vods.append(Vod(site_key="s", vid=name, name=name, lines=lines))
    return data


def test_only_the_named_line_survives() -> None:
    data = _site(("沙丘", ["shandian", "feifan", "4K高清"]))

    result = filter_lines(data, keep=["4K"])

    assert result.kept_titles == 1
    assert result.dropped_lines == 2
    assert [line.name for line in data.vods[0].lines] == ["4K高清"]
    assert result.names == {"4K高清": 1}


def test_a_title_without_a_matching_line_is_dropped_not_emptied() -> None:
    """Keeping a title with zero lines would publish a film that cannot be played."""
    data = _site(("沙丘", ["shandian", "4K"]), ("繁花", ["lzm3u8", "ffm3u8"]))

    result = filter_lines(data, keep=["4K"])

    assert [vod.name for vod in data.vods] == ["沙丘"]
    assert result.dropped_titles == ["繁花"]
    assert result.dropped_title_count == 1


def test_an_unnamed_line_is_never_assumed_to_match() -> None:
    """A site that does not label its lines cannot support a name filter, and should not."""
    data = _site(("伦敦合伙人", ["", ""]))

    result = filter_lines(data, keep=["4K"])

    assert data.vod_count == 0
    assert result.dropped_lines == 2
    assert result.names == {}


def test_matching_is_case_insensitive_and_accepts_a_regex() -> None:
    data = _site(("沙丘", ["4k", "4K高清", "2160P"]))

    assert filter_lines(data, keep=["4k"]).kept_titles == 1
    assert len(data.vods[0].lines) == 2  # 4k + 4K高清, not 2160P

    data = _site(("沙丘", ["4k", "2160P"]))
    filter_lines(data, keep=[r"4k|2160"])
    assert len(data.vods[0].lines) == 2


def test_drop_runs_without_a_keep_filter() -> None:
    data = _site(("沙丘", ["广告线路", "feifan"]))

    result = filter_lines(data, drop=["广告"])

    assert [line.name for line in data.vods[0].lines] == ["feifan"]
    assert result.dropped_lines == 1


def test_no_filter_is_a_no_op() -> None:
    data = _site(("沙丘", ["a", "b"]))
    result = filter_lines(data)
    assert result.kept_titles == 1
    assert result.dropped_lines == 0
    assert len(data.vods[0].lines) == 2
    assert result.names == {"a": 1, "b": 1}


def test_line_names_reports_what_the_site_calls_them() -> None:
    data = _site(("沙丘", ["feifan", "4K高清"]), ("繁花", ["feifan"]))
    assert line_names(data) == {"feifan": 2, "4K高清": 1}


def test_a_broken_pattern_falls_back_to_a_literal() -> None:
    """`4K(` is a typo, not a reason to abort a run."""
    data = _site(("沙丘", ["4K(高清)", "feifan"]))
    filter_lines(data, keep=["4K("])
    assert [line.name for line in data.vods[0].lines] == ["4K(高清)"]


# -- reading the names off an HTML template -----------------------------------------------------


def _tabs_selector() -> dict:
    return {
        "lines": {
            "item": "span.source-tab-item",
            "pane_attr": "data-target",
            "pane": "#{id}",
            "link": "a[href*='/vodplay/']",
        }
    }


def _fixture_html() -> str:
    from pathlib import Path

    return (Path(__file__).parent / "fixtures" / "html-tabs" / "detail.html").read_text(encoding="utf-8")


def test_tier3_reads_one_line_per_tab_with_the_site_names() -> None:
    """`备用线路 / 奇异 / 4K高清` are three streams of one film, not one stream called 线路1."""
    from harvest.adapters.html_ssr import HtmlSsrAdapter

    lines = HtmlSsrAdapter()._lines_from_tabs(  # noqa: SLF001
        _fixture_html(), "https://s.test/voddetail/900.html", _tabs_selector()
    )

    assert lines is not None
    assert [line.name for line in lines] == ["4K高清", "奇异", "备用线路"]
    assert [len(line.episodes) for line in lines] == [2, 1, 2]
    assert lines[0].episodes[1].url == "https://s.test/vodplay/900-1-2.html"


def test_tier3_lines_feed_the_same_filter_as_tier1() -> None:
    """The point of naming lines is that `--lines-keep 4K` can now act on a HTML site."""
    from harvest.adapters.html_ssr import HtmlSsrAdapter
    from harvest.models import SiteData

    lines = HtmlSsrAdapter()._lines_from_tabs(_fixture_html(), "https://s.test/", _tabs_selector())  # noqa: SLF001
    data = SiteData(site_key="s", site_name="s", base_url="https://s.test", adapter="html_ssr")
    data.vods.append(Vod(site_key="s", vid="900", name="示例剧集", lines=lines or []))

    result = filter_lines(data, keep=["4K"])

    assert result.kept_titles == 1
    assert [line.name for line in data.vods[0].lines] == ["4K高清"]


def test_a_tab_without_a_pane_reference_is_skipped_not_guessed() -> None:
    from harvest.adapters.html_ssr import HtmlSsrAdapter

    html = '<span class="source-tab-item">4K高清</span>'
    assert HtmlSsrAdapter()._lines_from_tabs(html, "https://s.test/", _tabs_selector()) is None  # noqa: SLF001


def test_observed_names_are_reported_even_when_nothing_matches() -> None:
    """`kept nothing` is only actionable if the report also says which lines do exist."""
    data = _site(("沙丘", ["奇异", "优酷"]), ("繁花", ["奇异"]))

    result = filter_lines(data, keep=["4K"])

    assert result.kept_titles == 0
    assert result.names == {}
    assert result.observed == {"奇异": 2, "优酷": 1}
