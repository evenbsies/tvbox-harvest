"""Tier-detection signal tests, run against recorded fixtures.

These are the regression net for the riskiest part of the system: the decision of *how* to
harvest a site. When an upstream site changes shape, the fix is to record a new fixture and
make these tests describe the new reality -- so the change is visible in review rather than
discovered as an empty source in someone's player.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from harvest.probe import signals

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(site: str, name: str) -> dict:
    return json.loads((FIXTURES / site / f"{name}.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------
# Tier 4 fixture: 4kvm.tv
# --------------------------------------------------------------------------------------


class TestTier4Fixture:
    @pytest.fixture()
    def home(self) -> str:
        return load("4kvm", "homepage")["body"]

    @pytest.fixture()
    def series(self) -> str:
        """A series detail page: 8 episodes, all present as static episode attributes."""
        return load("4kvm", "detail_series")["body"]

    @pytest.fixture()
    def movie(self) -> str:
        """A single-episode detail page. Same player, same absence of addresses."""
        return load("4kvm", "detail_movie")["body"]

    def test_go_404_body_is_recognised(self):
        assert signals.GO_DEFAULT_404 in load("4kvm", "robots_404")["body"]

    def test_api_php_returns_go_404_not_cakephp(self):
        """Rules out Tier 1 for this site: a PHP/MacCMS install would not answer like this."""
        response = load("4kvm", "api_php_404")
        assert response["status"] == 404
        assert signals.GO_DEFAULT_404 in response["body"]
        assert signals.looks_like_html(response["body"]) is False

    def test_homepage_yields_a_dominant_detail_cluster(self, home):
        clusters = signals.cluster_detail_links(home, "https://www.4kvm.tv/")
        assert clusters, "expected at least one link cluster"
        top = clusters[0]
        assert top.pattern == "/play/{id}"
        assert top.dominant, f"cluster too small to trust: {top.count}"
        assert top.samples and top.samples[0].startswith("https://www.4kvm.tv/play/")

    def test_static_assets_are_not_mistaken_for_detail_links(self, home):
        patterns = {c.pattern for c in signals.cluster_detail_links(home, "https://www.4kvm.tv/")}
        assert not any("/static/" in p for p in patterns)

    def test_homepage_has_no_usable_api_hint(self, home):
        """No /api.php in the markup -- corroborates the Go-404 finding."""
        hints = signals.extract_api_hints(home)
        assert not any("provide/vod" in h for h in hints)

    @pytest.mark.parametrize("fixture", ["detail_series", "detail_movie"])
    def test_detail_page_has_zero_static_media_urls(self, fixture):
        """The defining Tier-4 signal: a player exists but the address is not in the bytes."""
        detail = load("4kvm", fixture)["body"]
        assert signals.count_m3u8(detail) == 0
        assert signals.has_player_container(detail), "expected player markers"

    def test_series_detail_exposes_all_episode_slots(self, series):
        """Episode *structure* is readable statically -- which is what makes the browser walk
        bounded and cheap rather than exploratory."""
        assert len(signals.EPISODE_ATTR_RE.findall(series)) == 8
        assert len(signals.EPISODE_COUNT_RE.findall(series)) == 1
        assert signals.EPISODE_COUNT_RE.findall(series)[0] == "8"

    def test_movie_detail_exposes_a_single_episode_slot(self, movie):
        assert len(signals.EPISODE_ATTR_RE.findall(movie)) == 1

    def test_episode_program_ids_are_static(self, series):
        """`dataid` is the per-episode program id the signed play call needs, and it is present
        in the static HTML -- so the episode count is knowable before opening a browser."""
        ids = re.findall(r'dataid="(\d+)"', series)
        assert ids == ["39063", "39064", "39065", "39066", "39067", "39068", "39069", "39070"]

    def test_pdf_global_decodes_to_the_observed_hosts(self, series):
        hosts = signals.decode_base64_global(series, "_pdf")
        assert hosts == ["oss.douyinbit.com", "myoss.douyinbit.top"]

    def test_maccms_markers_absent(self, home, series):
        assert signals.maccms_markers(home) == []
        assert signals.maccms_markers(series) == []

    def test_recipe_fallback_hosts_match_the_recorded_evidence(self, series, repo_root):
        """Cross-check the recipe against the recorded page.

        This exists because of a real mistake: the failover host list in the recipe was first
        transcribed by hand from terminal output, and the second host came out misspelled
        ("douinbit" instead of "douyinbit"). Nothing would have caught it -- the resulting line
        would simply never have resolved in a player. Now the recipe and the evidence must agree.
        """
        import yaml

        recipe = yaml.safe_load((repo_root / "sites" / "4kvm.yaml").read_text(encoding="utf-8"))
        assert recipe["browser"]["fallback_hosts"] == signals.decode_base64_global(series, "_pdf")


# --------------------------------------------------------------------------------------
# Tier 1 fixture: standard MacCMS
# --------------------------------------------------------------------------------------


class TestTier1Fixture:
    def test_list_shape_is_recognised(self):
        payload = json.loads(load("maccms-demo", "api_list")["body"])
        assert signals.is_maccms_class_list(payload) is True

    def test_detail_shape_is_recognised(self):
        payload = json.loads(load("maccms-demo", "api_detail")["body"])
        assert signals.is_maccms_detail(payload) is True

    def test_class_table_extracted(self):
        payload = json.loads(load("maccms-demo", "api_list")["body"])
        names = [c["type_name"] for c in payload["class"]]
        assert names == ["电影", "连续剧", "动漫"]

    def test_envelope_without_records_is_not_mistaken_for_maccms(self):
        assert signals.is_maccms_class_list({"code": 400, "message": "请求路径格式错误", "data": None}) is False

    def test_bare_list_is_not_mistaken_for_maccms(self):
        assert signals.is_maccms_class_list([{"vod_id": 1}]) is False


# --------------------------------------------------------------------------------------
# Tier 2 fixture: custom JSON
# --------------------------------------------------------------------------------------


class TestTier2Fixture:
    def test_custom_payload_scores_as_promising(self):
        payload = json.loads(load("custom-json", "list")["body"])
        assert signals.is_maccms_class_list(payload) is False, "must not be mistaken for Tier 1"

        score = signals.field_fingerprint(payload)
        assert score.score > 0
        assert score.promising, f"expected a promising score, got {score.score}"

    def test_recognised_field_paths_are_reported_for_the_recipe(self):
        payload = json.loads(load("custom-json", "list")["body"])
        paths = signals.field_fingerprint(payload).sample_paths
        assert any("play_list" in p for p in paths)


# --------------------------------------------------------------------------------------
# Layout heuristics
# --------------------------------------------------------------------------------------


class TestLayoutHeuristics:
    def test_spa_shell_detected(self):
        shell = '<html><body><div id="app"></div><script src="/a.js"></script><script src="/b.js"></script></body></html>'
        assert signals.is_spa_skeleton(shell) is True

    def test_content_rich_page_is_not_a_spa(self):
        page = "<html><body>" + "<p>内容</p>" * 4000 + '<div id="app"></div><script src="/a.js"></script><script src="/b.js"></script></body></html>'
        assert signals.is_spa_skeleton(page) is False

    def test_genericise_path_groups_same_shaped_urls(self):
        """What matters is that same-shaped paths collapse onto one pattern, not the exact
        spelling of the placeholder -- a cluster is only useful if equivalents collide."""
        a = signals._generalize_path("/vodshow/1--------1---.html")
        b = signals._generalize_path("/vodshow/27--------3---.html")
        assert a == b, f"equivalent paths produced different patterns: {a} vs {b}"
        assert "{slug}" in a or "{n}" in a or "{id}" in a

    def test_genericise_path_distinguishes_different_shapes(self):
        assert signals._generalize_path("/play/ch4l5b3mn") != signals._generalize_path("/playlist/652")

    def test_genericise_path_handles_short_ids(self):
        assert signals._generalize_path("/play/ch4l5b3mn") == "/play/{id}"
        assert signals._generalize_path("/playlist/652") == "/playlist/{n}"

    def test_genericise_path_skips_static_assets(self):
        for path in ("/static/js/app.js", "/assets/a.css", "/img/p.png"):
            assert signals._generalize_path(path) is None
