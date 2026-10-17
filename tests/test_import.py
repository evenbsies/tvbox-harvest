"""Importing entries from someone else's TVBox config.

Two properties matter and both are easy to lose:

1. The filter is on **name**, and a name is a label. Nothing here claims the stream behind a
   `4K` label is 4K; measurement is `scorecard`'s job.
2. Importing writes candidates and enables nothing. Two configs on the same host must not
   overwrite each other's output -- that would lose the first import silently.
"""

from __future__ import annotations

import yaml

from harvest.config import Settings
from harvest.import_config import (
    ImportedEntry,
    ImportResult,
    candidate_path,
    collect_entries,
    write_candidates,
)

CONFIG = {
    "sites": [
        {"key": "a", "name": "普通影视", "type": 1, "api": "https://a.example/api.php/provide/vod/"},
        {"key": "b", "name": "4K专线", "type": 1, "api": "https://b.example/api.php/provide/vod/"},
        {"key": "c", "name": "玩偶｜4K", "type": 3, "api": "csp_Wogg"},
        {"key": "d", "name": "无地址", "type": 1},
    ],
    "parses": [
        {"name": "默认", "type": 0, "url": "https://p.example/?url="},
        {"name": "4KTV", "type": 1, "url": "https://jx.4kdv.com/?url="},
    ],
}


def test_only_names_containing_4k_survive() -> None:
    matched, sites, parses = collect_entries(CONFIG, match=["4K"])

    assert sites == 4 and parses == 2
    assert [e.name for e in matched] == ["4K专线", "玩偶｜4K", "4KTV"]
    assert {e.kind for e in matched} == {"site", "parse"}


def test_matching_is_case_insensitive() -> None:
    matched, _, _ = collect_entries(CONFIG, match=["4k"])
    assert [e.name for e in matched] == ["4K专线", "玩偶｜4K", "4KTV"]


def test_without_a_pattern_nothing_is_matched() -> None:
    """An empty pattern must mean "nothing", never "everything" -- importing 1659 sites by
    accident is not a mistake a caller should be able to make quietly."""
    matched, _, _ = collect_entries(CONFIG, match=[])
    assert matched == []


def test_kind_can_be_narrowed() -> None:
    sites_only, _, _ = collect_entries(CONFIG, match=["4K"], kind="site")
    assert {e.kind for e in sites_only} == {"site"}
    parses_only, _, _ = collect_entries(CONFIG, match=["4K"], kind="parse")
    assert {e.kind for e in parses_only} == {"parse"}


def test_a_type_is_carried_through_so_a_reviewer_can_see_what_it_is() -> None:
    """`type: 3` means a spider, not an API -- the difference decides whether it can be harvested."""
    matched, _, _ = collect_entries(CONFIG, match=["玩偶"])
    assert matched[0].type == 3
    assert matched[0].address == "csp_Wogg"


def test_two_configs_on_one_host_do_not_collide(tmp_path) -> None:
    settings = Settings(root=tmp_path)
    first = candidate_path("https://raw.githubusercontent.com/owner1/repo/tvbox.json", settings)
    second = candidate_path("https://raw.githubusercontent.com/owner2/repo/merged.json", settings)
    assert first != second
    assert first.parent == second.parent


def test_written_candidates_carry_provenance_and_enable_nothing(tmp_path) -> None:
    settings = Settings(root=tmp_path)
    result = ImportResult(
        source_url="https://raw.githubusercontent.com/owner/repo/list.json",
        fetched_at="2026-10-17T04:00:00+00:00",
        total_sites=4,
        total_parses=2,
        match=["4K"],
        matched=[
            ImportedEntry(kind="site", name="4K专线", address="https://b.example/", type=1),
            ImportedEntry(kind="parse", name="4KTV", address="https://jx.example/?url=", type=1),
        ],
    )

    path = write_candidates(result, settings)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert data["provenance"]["source"] == result.source_url
    assert data["provenance"]["match"] == ["4K"]
    assert data["provenance"]["totals"] == {"sites": 4, "parses": 2}
    assert [s["name"] for s in data["sites"]] == ["4K专线"]
    assert [p["name"] for p in data["parses"]] == ["4KTV"]
    # No `enabled` key at all: the file is not a recipe and the pipeline does not read it.
    assert "enabled" not in data


def test_a_fullwidth_four_matches_because_it_is_the_same_digit() -> None:
    matched, _, _ = collect_entries({"sites": [{"name": "４K专区", "api": "x"}]}, match=["4K"])
    assert [e.name for e in matched] == ["４K专区"]


def test_the_chinese_numeral_is_not_assumed_to_mean_the_same_thing() -> None:
    """`四K` is a different string. Folding it would make the filter depend on a guess;
    a caller who wants both writes `--match "4K|四K"`."""
    matched, _, _ = collect_entries({"sites": [{"name": "四K专区", "api": "x"}]}, match=["4K"])
    assert matched == []

    matched, _, _ = collect_entries({"sites": [{"name": "四K专区", "api": "x"}]}, match=["4K|四K"])
    assert [e.name for e in matched] == ["四K专区"]
