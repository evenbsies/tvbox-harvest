"""Tier-4 interpretation tests.

The paywall parsing is the subtlest logic in the project and the easiest to get quietly wrong.
Both failure modes are bad in different ways:

* Treating a members-only quality as playable emits a URL that is literally ``"1"`` -- the
  placeholder the API returns for locked entries. The source then looks present but plays
  nothing.
* Treating a refusal as "we didn't find anything" loses the information that the site is gated,
  so the run reports a mysterious shortfall instead of a clear reason.

Fixtures below are the real response shapes, captured live.
"""

from __future__ import annotations

import json

from harvest.adapters.playwright_sniff import PlaywrightSniffAdapter, parse_play_payload

FREE_AND_VIP = json.dumps(
    {
        "code": 200,
        "message": "OK",
        "data": {
            "play_id": 39063,
            "subtitle_url": "",
            "current_quality": 1,
            "quality_urls": [
                {"mtype": "m3u8", "bitrate": 2160000, "title": "4K", "description": "蓝光",
                 "isvip": True, "locked": True, "url": "1"},
                {"mtype": "m3u8", "bitrate": 1080, "title": "1080p", "description": "超清",
                 "isvip": False, "locked": False,
                 "url": "https://oss.douyinbit.com/m3u8/6947a0c4542ee808d29c35e10742a3f5.m3u8"},
            ],
        },
    },
    ensure_ascii=False,
)

GATED = json.dumps(
    {
        "code": 401,
        "message": "请先登录并开通VIP会员",
        "data": {"play_id": 0, "subtitle_url": "", "current_quality": 0, "quality_urls": None},
    },
    ensure_ascii=False,
)


class TestParsePlayPayload:
    def test_selects_the_free_quality(self):
        parsed = parse_play_payload(FREE_AND_VIP)
        assert parsed["code"] == 200
        assert parsed["playable"] == [
            "https://oss.douyinbit.com/m3u8/6947a0c4542ee808d29c35e10742a3f5.m3u8"
        ]

    def test_locked_placeholder_url_is_never_emitted(self):
        """The VIP entry's url is the literal string "1". Emitting it would produce a source
        that appears to work and plays nothing."""
        parsed = parse_play_payload(FREE_AND_VIP)
        assert "1" not in parsed["playable"]
        assert all(u.startswith("http") for u in parsed["playable"])
        assert parsed["locked_qualities"] == 1

    def test_prefers_highest_bitrate_among_free_qualities(self):
        payload = json.dumps(
            {
                "code": 200,
                "data": {
                    "quality_urls": [
                        {"bitrate": 480, "isvip": False, "locked": False, "url": "https://a/x-480.m3u8"},
                        {"bitrate": 1080, "isvip": False, "locked": False, "url": "https://a/x-1080.m3u8"},
                        {"bitrate": 720, "isvip": False, "locked": False, "url": "https://a/x-720.m3u8"},
                    ]
                },
            }
        )
        assert parse_play_payload(payload)["playable"][0] == "https://a/x-1080.m3u8"

    def test_gated_response_reports_a_refusal(self):
        parsed = parse_play_payload(GATED)
        assert parsed["code"] == 401
        assert parsed["playable"] == []
        assert "请先登录" in parsed["message"]

    def test_garbage_is_not_mistaken_for_a_payload(self):
        assert parse_play_payload("not json") == {}
        assert parse_play_payload("") == {}

    def test_response_without_quality_list_is_not_structured_payload(self):
        parsed = parse_play_payload(json.dumps({"code": 200, "data": {}}))
        assert parsed["playable"] == []


class TestInterpret:
    """`_interpret` merges structured payloads with raw sniffed URLs per episode."""

    def _interpret(self, result, episodes):
        return PlaywrightSniffAdapter()._interpret(result, episodes)  # noqa: SLF001

    def test_structured_payload_wins_and_paywall_is_counted(self):
        result = {
            "episode_bodies": {"1": [FREE_AND_VIP], "2": [GATED]},
            "episodes": {},
        }
        parsed = self._interpret(result, [1, 2])
        assert sorted(parsed["playable"]) == [1]
        assert parsed["paywalled"] == 1
        assert "请先登录" in parsed["message"]

    def test_falls_back_to_raw_sniff_when_no_structured_payload(self):
        result = {
            "episode_bodies": {"1": ["<html>not json</html>"]},
            "episodes": {"1": ["https://cdn.example.net/a/1.m3u8"]},
        }
        parsed = self._interpret(result, [1])
        assert parsed["playable"][1] == ["https://cdn.example.net/a/1.m3u8"]
        assert parsed["paywalled"] == 0

    def test_episode_with_no_answer_is_not_counted_as_paywalled(self):
        """A missing response means "we did not get one", which is different from a refusal."""
        parsed = self._interpret({"episode_bodies": {}, "episodes": {}}, [1, 2])
        assert parsed["paywalled"] == 0
        assert parsed["playable"] == {}

    def test_locked_qualities_are_tallied_across_episodes(self):
        result = {"episode_bodies": {"1": [FREE_AND_VIP], "2": [FREE_AND_VIP]}, "episodes": {}}
        assert self._interpret(result, [1, 2])["locked_qualities"] == 2

    def test_integer_episode_keys_still_resolve(self):
        """Regression: the sniffer once keyed this dict by int, the adapter read ``str(ep)``.

        Every sniffed URL was therefore dropped, the title was reported as "empty", and the
        report said the site had no playable episodes -- for a site that demonstrably did. The
        bug only appeared against a live browser; the unit tests all used string keys.
        """
        result = {
            "episode_bodies": {},
            "episodes": {1: ["https://cdn.example.net/a/1.m3u8"], 2: ["https://cdn.example.net/a/2.m3u8"]},
        }
        parsed = self._interpret(result, [1, 2])
        assert parsed["playable"][1] == ["https://cdn.example.net/a/1.m3u8"]
        assert parsed["playable"][2] == ["https://cdn.example.net/a/2.m3u8"]

    def test_the_sniffer_keys_episodes_by_string(self):
        """The source of that bug, pinned where it is produced rather than where it hurt."""
        import inspect

        from harvest.fetch import browser

        source = inspect.getsource(browser)
        assert 'out["episodes"][str(ep)] = found' in source


class TestBuildLine:
    def _adapter(self):
        return PlaywrightSniffAdapter()

    def test_episodes_and_alt_lines_are_built(self):
        parsed = {
            "playable": {1: ["https://oss.douyinbit.com/m3u8/a1.m3u8"],
                         2: ["https://oss.douyinbit.com/m3u8/a2.m3u8"]},
            "literals": [],
        }
        line = self._adapter()._build_line(parsed, ["oss.douyinbit.com", "myoss.douyinbit.top"])
        assert line is not None
        assert len(line.episodes) == 2
        assert [e.name for e in line.episodes] == ["第01集", "第02集"]
        # The failover host becomes a switchable alternate on each episode.
        assert line.episodes[0].alts
        assert line.episodes[0].alts[0].url == "https://myoss.douyinbit.top/m3u8/a1.m3u8"
        assert line.episodes[0].alts[0].note == "alts"

    def test_relative_media_path_is_resolved_against_the_first_host(self):
        parsed = {"playable": {1: ["/m3u8/abc.m3u8"]}, "literals": []}
        line = self._adapter()._build_line(parsed, ["oss.douyinbit.com"])
        assert line is not None
        assert line.episodes[0].url == "https://oss.douyinbit.com/m3u8/abc.m3u8"

    def test_relative_path_without_any_host_is_dropped(self):
        parsed = {"playable": {1: ["/m3u8/abc.m3u8"]}, "literals": []}
        assert self._adapter()._build_line(parsed, []) is None

    def test_no_playable_episodes_yields_no_line(self):
        assert self._adapter()._build_line({"playable": {}, "literals": []}, ["a.example"]) is None


class TestSegmentVersusPlaylist:
    """A one-second fragment is not an episode address.

    Measured on zhuiying3.cc: alongside the playlist the player also fetched
    `/video/lundunhehuoren/.../0000002.ts`. That response matches on content-type
    (``video/mp2t``) even though it matches none of the URL patterns, so an unfiltered
    ``_extract_media`` returned it as episode 3 -- a title whose third episode would play for
    exactly one second before ending.
    """

    @staticmethod
    def _item(url: str, content_type: str):
        from harvest.fetch.browser import SniffedResponse

        return SniffedResponse(url=url, status=200, content_type=content_type, resource_type="xhr", method="GET")

    def test_a_playlist_wins_over_a_segment(self):
        import re

        from harvest.fetch.browser import BrowserSniffer

        matched = [
            self._item("https://cdn.test/a/1.m3u8", "application/vnd.apple.mpegurl"),
            self._item("https://cdn.test/a/0000002.ts", "video/mp2t"),
        ]
        urls = BrowserSniffer._extract_media(matched, [re.compile(r"\.m3u8")])  # noqa: SLF001
        assert urls == ["https://cdn.test/a/1.m3u8"]

    def test_a_segment_is_kept_when_it_is_all_there_is(self):
        """Some hosts serve progressive mp4/ts only; dropping those would lose the site."""
        import re

        from harvest.fetch.browser import BrowserSniffer

        matched = [self._item("https://cdn.test/a/full.ts", "video/mp2t")]
        urls = BrowserSniffer._extract_media(matched, [re.compile(r"\.m3u8")])  # noqa: SLF001
        assert urls == ["https://cdn.test/a/full.ts"]


def test_vid_drops_a_page_extension():
    """`/video/XWpZYC61BZ.html` is the page; the id is `XWpZYC61BZ`."""
    from harvest.adapters.playwright_sniff import PlaywrightSniffAdapter

    adapter = PlaywrightSniffAdapter()
    assert adapter._vid_from_url("https://s.test/video/XWpZYC61BZ.html", "s") == "XWpZYC61BZ"  # noqa: SLF001
    assert adapter._vid_from_url("https://s.test/v/abc123", "s") == "abc123"  # noqa: SLF001
    assert adapter._vid_from_url("https://s.test/", "s") == "s"  # noqa: SLF001


def test_a_ts_fragment_is_not_an_episode_address():
    """A playlist or a container file is an address; a numbered fragment is not."""
    from harvest.adapters.playwright_sniff import _is_episode_address

    assert _is_episode_address("https://cdn.test/a/1.m3u8?sign=x")
    assert _is_episode_address("https://cdn.test/a/full.mp4")
    assert _is_episode_address("https://cdn.test/a/full.mkv")
    assert not _is_episode_address("https://cdn.test/video/x/0000002.ts")
    assert not _is_episode_address("https://cdn.test/video/x/seg.m4s")


def test_build_line_drops_an_episode_whose_only_address_is_a_fragment():
    """Six walked, one unusable: the line keeps five episodes rather than shipping a 2s one."""
    from harvest.adapters.playwright_sniff import PlaywrightSniffAdapter

    parsed = {
        "playable": {
            1: ["https://del.test/Vtche/BF/4069376166.m3u8"],
            2: ["https://cdn.test/video/x/0000002.ts"],
            3: ["https://del.test/Vtche/BF/3513919350.m3u8"],
        }
    }
    line = PlaywrightSniffAdapter()._build_line(parsed, ["bfeng11.com", "del.test"])  # noqa: SLF001
    assert line is not None
    assert [e.index for e in line.episodes] == [1, 3]
