"""URL/title normalization and MacCMS play-URL round-tripping.

The play-URL tests matter more than they look: ``$$$``/``#``/``$`` are the delimiters of the
MacCMS format, so a title or URL containing one of them corrupts not just its own entry but
every entry after it in the same string.
"""

from __future__ import annotations

from harvest.clean.normalize import (
    canon_media_url,
    canon_resource_url,
    extract_year,
    fingerprint,
    is_plausible_media_url,
    normalize_title,
    swap_host,
)
from harvest.models import Episode, Line, build_play_url, parse_play_url


class TestResourceUrl:
    def test_strips_tracking_params(self):
        url = canon_resource_url("https://Example.com/vod/1.html?utm_source=x&id=7&spm=a.b")
        assert url == "https://example.com/vod/1.html?id=7"

    def test_sorts_params_for_stable_dedup(self):
        assert canon_resource_url("https://a.com/x?b=2&a=1") == canon_resource_url(
            "https://a.com/x?a=1&b=2"
        )

    def test_drops_default_port_and_fragment(self):
        assert canon_resource_url("https://a.com:443/x#frag") == "https://a.com/x"

    def test_keeps_index_vs_root(self):
        assert canon_resource_url("https://a.com/") != canon_resource_url("https://a.com/play/1")


class TestMediaUrl:
    def test_never_reorders_signed_params(self):
        url = "https://cdn.example.com/a.m3u8?token=zzz&expires=1&sign=abc"
        assert canon_media_url(url) == url

    def test_never_strips_suspicious_looking_params(self):
        # `from=` would be dropped from a page URL; on a media URL it may be part of the token.
        url = "https://cdn.example.com/a.m3u8?from=1&t=2"
        assert "from=1" in canon_media_url(url)

    def test_escapes_nothing_but_drops_fragment(self):
        assert canon_media_url("https://cdn.example.com/a.m3u8#t=5") == "https://cdn.example.com/a.m3u8"

    def test_swap_host_preserves_path_and_query(self):
        swapped = swap_host("https://a.example.com/m3u8/x/y.m3u8?k=1", "b.example.org")
        assert swapped == "https://b.example.org/m3u8/x/y.m3u8?k=1"


class TestPlausibility:
    def test_rejects_placeholders(self):
        assert not is_plausible_media_url("https://example.com/a.m3u8")
        assert not is_plausible_media_url("")
        assert not is_plausible_media_url("javascript:void(0)")
        assert not is_plausible_media_url("https://localhost/a.m3u8")

    def test_accepts_normal_media(self):
        assert is_plausible_media_url("https://cdn.a.com/x/index.m3u8")
        assert is_plausible_media_url("http://1.2.3.4:8080/live/a.m3u8")


class TestTitleAndYear:
    def test_strips_quality_tags_and_season(self):
        assert normalize_title("权力的游戏 第一季 1080P 中字") == "权力的游戏"

    def test_folds_fullwidth(self):
        assert normalize_title("ＡＢＣ　Ｄｅｆ") == "abc def"

    def test_same_fingerprint_for_equivalent_titles(self):
        assert fingerprint("流浪地球 4K 蓝光", 2019) == fingerprint("流浪地球【高清】", 2019)

    def test_different_fingerprint_across_years(self):
        assert fingerprint("同名电影", 2019) != fingerprint("同名电影", 2021)

    def test_year_priority_field_over_title(self):
        assert extract_year("2018", fallback_text="某片 (2020)") == 2018

    def test_year_from_title_when_field_missing(self):
        assert extract_year(None, fallback_text="某片 (2020)") == 2020

    def test_year_ignores_implausible_numbers(self):
        assert extract_year(fallback_text="第 1024 集") is None


class TestPlayUrlRoundTrip:
    def test_parses_lines_and_episodes(self):
        payload = (
            "高清$https://a.com/1.m3u8#第二集$https://a.com/2.m3u8"
            "$$$"
            "超清$https://b.com/1.m3u8"
        )
        lines = parse_play_url(payload)
        assert len(lines) == 2
        assert len(lines[0].episodes) == 2
        assert lines[1].episodes[0].name == "超清"
        assert lines[1].episodes[0].url == "https://b.com/1.m3u8"

    def test_splits_on_first_dollar_only(self):
        lines = parse_play_url("第01集$https://a.com/x?q=a$b")
        assert lines[0].episodes[0].url == "https://a.com/x?q=a$b"

    def test_names_missing_episode_labels(self):
        lines = parse_play_url("https://a.com/1.m3u8#https://a.com/2.m3u8")
        assert [e.name for e in lines[0].episodes] == ["第01集", "第02集"]

    def test_round_trip_is_stable(self):
        original = "线路1$https://a.com/1.m3u8$$$线路2$https://b.com/1.m3u8"
        once = build_play_url(parse_play_url(original), expand_alts_as_lines=False)
        twice = build_play_url(parse_play_url(once[1]), expand_alts_as_lines=False)
        assert once == twice

    def test_episode_name_with_delimiter_is_sanitized(self):
        lines = [
            Line(name="线路1", episodes=[Episode(index=1, name="第1集#坏名字$", url="https://a.com/1.m3u8")])
        ]
        play_from, play_url = build_play_url(lines, expand_alts_as_lines=False)
        # The rendered payload must still split back into exactly one line / one episode.
        reparsed = parse_play_url(play_url)
        assert len(reparsed) == 1 and len(reparsed[0].episodes) == 1
        assert play_from == "线路1"

    def test_url_with_hash_is_escaped(self):
        lines = [
            Line(name="L1", episodes=[Episode(index=1, name="第1集", url="https://a.com/1.m3u8#t=9")])
        ]
        _, play_url = build_play_url(lines, expand_alts_as_lines=False)
        assert "%23" in play_url
        assert len(parse_play_url(play_url)[0].episodes) == 1

    def test_alts_become_extra_lines(self):
        lines = [
            Line(
                name="主线路",
                episodes=[
                    Episode(
                        index=1,
                        name="第01集",
                        url="https://oss.a.com/m3u8/1.m3u8",
                        alts=[_alt("https://mirror.b.com/m3u8/1.m3u8")],
                    )
                ],
            )
        ]
        play_from, play_url = build_play_url(lines, expand_alts_as_lines=True)
        assert "$$$" in play_url
        assert len(play_from.split("$$$")) == 2
        assert "mirror.b.com" in play_url

    def test_empty_input_yields_nothing(self):
        assert parse_play_url("") == []
        assert build_play_url([]) == ("", "")


def _alt(url: str):
    from harvest.models import AltURL

    return AltURL(url=url, host="mirror.b.com", note="alts")


# -- display-name noise ----------------------------------------------------------------------


def test_marketing_suffix_is_stripped_from_the_display_name() -> None:
    """`伦敦合伙人 免费在线观看` is not the title of anything; it is an SEO phrase."""
    from harvest.clean.normalize import strip_name_noise

    assert strip_name_noise("伦敦合伙人 免费在线观看") == "伦敦合伙人"
    assert strip_name_noise("沙丘 高清完整版") == "沙丘"
    assert strip_name_noise("繁花 详情介绍") == "繁花"


def test_a_name_that_is_only_noise_survives() -> None:
    """Stripping must not produce an empty name -- that would break the title entirely."""
    from harvest.clean.normalize import strip_name_noise

    assert strip_name_noise("在线观看") == "在线观看"
    assert strip_name_noise("") == ""
    assert strip_name_noise(None) == ""


def test_ordinary_names_are_left_alone() -> None:
    from harvest.clean.normalize import strip_name_noise

    assert strip_name_noise("伦敦合伙人") == "伦敦合伙人"
    assert strip_name_noise("The Bear") == "The Bear"
