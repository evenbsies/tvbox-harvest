"""The scorecard's job is to measure, not to flatter. These tests pin the difference.

Two mistakes are easy to make here and both were made on the first pass, against a real catalogue:

1. **Missing a signal that dominates the data.** The HD pattern did not include `HD`, and 289 of
   298 titles in the sample used exactly that word as their remark. The ratio came out 0.0 --
   reported with confidence, and wrong. Worse, `HD` on this class of site means "not a cam rip",
   so the honest answer is two numbers (claims of a *resolution* vs a generic HD label), not one.

2. **Confusing "not measured" with zero.** A playlist without `RESOLUTION=`, a site that could not
   be probed, a catalogue with no dates: all of those must surface as `None`, because `0` reads as
   "none of it" while `None` reads as "we did not look". That distinction is the whole reason this
   module reports numbers instead of a verdict.

Network paths are exercised manually against a real site; what is pinned here is the arithmetic,
the parsing, and the boundaries.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from harvest.models import Episode, Line, SiteConfig, SiteData, Vod
from harvest.scorecard import (
    _GENERIC_HD,
    _HIGH_RES,
    _HLS_RESOLUTION,
    _check_streams,
    _ratio,
    _recent_fraction,
    score,
)

# -- label parsing -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text", ["1080P", "2160p", "4K", "UHD", "HDR", "蓝光原盘", "BD1080"]
)
def test_resolution_claims_are_recognised(text: str) -> None:
    assert _HIGH_RES.search(text), text


@pytest.mark.parametrize("text", ["HD", "HD国语", "高清", "超清", "TC", "已完结", "DVD"])
def test_generic_labels_are_not_mistaken_for_a_resolution(text: str) -> None:
    assert not _HIGH_RES.search(text), f"{text!r} is not a resolution claim"


@pytest.mark.parametrize("text", ["HD", "HD国语", "高清", "超清"])
def test_generic_labels_are_recognised(text: str) -> None:
    assert _GENERIC_HD.search(text), text


def test_the_real_world_remark_that_broke_the_first_attempt() -> None:
    """289 of 298 titles on the reference catalogue said exactly 'HD'.

    The first version of this module only matched resolution words, so every one of them scored as
    "no HD" and the reported ratio was 0.0 with no indication anything was missing.
    """
    assert not _HIGH_RES.search("HD")
    assert _GENERIC_HD.search("HD")


# -- the None-vs-zero distinction ------------------------------------------------------------------


def test_no_sample_is_not_a_ratio_of_zero() -> None:
    """"we measured nothing" must not be rendered as "none of it"."""
    assert _ratio(0, 0) is None


def test_a_measured_zero_is_zero() -> None:
    assert _ratio(0, 10) == 0.0


def test_a_partial_ratio_rounds_predictably() -> None:
    assert _ratio(1, 4) == 0.25
    assert _ratio(1, 3) == 0.3333


# -- HLS resolution parsing ------------------------------------------------------------------------


def test_resolution_is_read_from_a_master_playlist() -> None:
    text = (
        "#EXTM3U\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080\n1080/index.m3u8\n'
        '#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720\n720/index.m3u8\n'
    )
    found = _HLS_RESOLUTION.findall(text)

    assert found == [("1920", "1080"), ("1280", "720")]


def test_a_media_playlist_declares_no_resolution() -> None:
    """Media playlists are legitimate and carry no RESOLUTION; that is "not measurable", not zero."""
    text = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\nseg0.ts\n"

    assert _HLS_RESOLUTION.findall(text) == []


# -- stream sampling -------------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, text: str, content: bytes | None = None) -> None:
        self.text = text
        self.content = content if content is not None else text.encode()
        self.status_code = 200


class _FakeHttp:
    """Records which URLs were fetched and answers with a canned body per URL.

    Bodies may be ``bytes`` for binary payloads (a TS segment carrying an SPS).
    """

    def __init__(self, bodies: dict[str, str | bytes], *, fail: set[str] | None = None) -> None:
        self.bodies = bodies
        self.fail = fail or set()
        self.calls: list[str] = []

    async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append(url)
        if url in self.fail:
            raise RuntimeError("boom")
        body = self.bodies.get(url, "#EXTM3U\n")
        if isinstance(body, bytes):
            return _FakeResponse(body.decode("latin-1"), body)
        return _FakeResponse(body)


def _data(urls: list[str], *, per_title: int = 1) -> SiteData:
    data = SiteData(site_key="s", site_name="s", base_url="https://s.test", adapter="maccms_v10")
    for index, url in enumerate(urls):
        episodes = [
            Episode(index=i + 1, name=f"e{i}", url=url if i == 0 else f"{url}-alt{i}")
            for i in range(per_title)
        ]
        data.vods.append(
            Vod(site_key="s", vid=f"v{index}", name=f"t{index}", lines=[Line(episodes=episodes)])
        )
    return data


SITE = SiteConfig(key="s", name="s", base_url="https://s.test", adapter="maccms_v10")


@pytest.mark.asyncio
async def test_one_stream_per_title_is_sampled() -> None:
    """Depth on a single title says nothing about the catalogue."""
    data = _data(
        ["https://cdn.test/a.m3u8", "https://cdn.test/b.m3u8", "https://cdn.test/c.m3u8"],
        per_title=3,
    )
    http = _FakeHttp({})

    result = await _check_streams(data, http, SITE, limit=3)

    assert result["checked"] == 3
    assert len(http.calls) == 3
    assert all(call.endswith(".m3u8") for call in http.calls)


@pytest.mark.asyncio
async def test_the_limit_is_respected() -> None:
    data = _data([f"https://cdn.test/{i}.m3u8" for i in range(10)])
    http = _FakeHttp({})

    result = await _check_streams(data, http, SITE, limit=2)

    assert result["checked"] == 2


@pytest.mark.asyncio
async def test_zero_disables_the_check() -> None:
    data = _data(["https://cdn.test/a.m3u8"])
    http = _FakeHttp({})

    result = await _check_streams(data, http, SITE, limit=0)

    assert result["checked"] == 0
    assert http.calls == []


@pytest.mark.asyncio
async def test_the_highest_resolution_wins() -> None:
    bodies = {
        "https://cdn.test/a.m3u8": "#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=1280x720\n",
        "https://cdn.test/b.m3u8": "#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=1920x1080\n",
        "https://cdn.test/c.m3u8": "#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=3840x2160\n",
    }
    data = _data(list(bodies))
    http = _FakeHttp(bodies)

    result = await _check_streams(data, http, SITE, limit=3)

    assert result["max_resolution"] == "3840x2160"
    assert result["with_resolution"] == 3
    assert result["seen"] == {"1280x720": 1, "1920x1080": 1, "3840x2160": 1}


@pytest.mark.asyncio
async def test_a_title_with_no_m3u8_is_skipped_not_counted() -> None:
    """Tier 3/4 pages may expose only an opaque player URL; that is not a stream we can read."""
    data = _data(["https://cdn.test/page.html"])
    http = _FakeHttp({})

    result = await _check_streams(data, http, SITE, limit=3)

    assert result["checked"] == 0
    assert http.calls == []


@pytest.mark.asyncio
async def test_one_unreachable_stream_does_not_lose_the_others() -> None:
    bodies = {"https://cdn.test/b.m3u8": "#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=1920x1080\n"}
    data = _data(["https://cdn.test/a.m3u8", "https://cdn.test/b.m3u8"])
    http = _FakeHttp(bodies, fail={"https://cdn.test/a.m3u8"})

    result = await _check_streams(data, http, SITE, limit=2)

    assert result["checked"] == 2
    assert result["with_resolution"] == 1
    assert result["max_resolution"] == "1920x1080"


@pytest.mark.asyncio
async def test_no_stream_resolves_to_none_not_to_zero() -> None:
    data = _data(["https://cdn.test/a.m3u8"])
    http = _FakeHttp({"https://cdn.test/a.m3u8": "#EXTM3U\n#EXTINF:6.0,\nseg.ts\n"})

    result = await _check_streams(data, http, SITE, limit=1)

    assert result["with_resolution"] == 0
    assert result["max_resolution"] is None


# -- SPS fallback ----------------------------------------------------------------------------------


def _media_playlist(name: str = "seg.ts") -> str:
    return f"#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:1,\n{name}\n"


def _ts_with_sps(hex_sps: str) -> bytes:
    return b"\x00\x00\x01\x67" + bytes.fromhex(hex_sps) + b"\x00\x00\x01\x68\xee\x3c\x80"


SP_1080P = "640028acd940780227e5c05a808080a0000003002000000641e30632c000"


@pytest.mark.asyncio
async def test_a_media_playlist_falls_back_to_the_sps() -> None:
    """Most hosts serve a media playlist; without this, "4K" is unfalsifiable."""
    data = _data(["https://cdn.test/a.m3u8"])
    http = _FakeHttp(
        {
            "https://cdn.test/a.m3u8": _media_playlist(),
            "https://cdn.test/seg.ts": _ts_with_sps(SP_1080P),
        }
    )

    result = await _check_streams(data, http, SITE, limit=1)

    assert result["with_resolution"] == 0  # nothing was declared
    assert result["measured_by_sps"] == 1  # but it was measured
    assert result["sps"] == {"1920x1080": 1}
    assert result["max_resolution"] == "1920x1080"
    assert result["is_4k"] is False
    assert "https://cdn.test/seg.ts" in http.calls


@pytest.mark.asyncio
async def test_a_master_playlist_never_fetches_a_segment() -> None:
    """One request per stream is the budget; the SPS route is only a fallback."""
    data = _data(["https://cdn.test/a.m3u8"])
    http = _FakeHttp(
        {"https://cdn.test/a.m3u8": "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=3840x2160\nhi.m3u8\n"}
    )

    result = await _check_streams(data, http, SITE, limit=1)

    assert result["with_resolution"] == 1
    assert result["measured_by_sps"] == 0
    assert result["max_resolution"] == "3840x2160"
    assert result["is_4k"] is True
    assert len(http.calls) == 1


@pytest.mark.asyncio
async def test_an_unreadable_segment_stays_unmeasured() -> None:
    """A CDN returning an error page is not evidence of a low resolution."""
    data = _data(["https://cdn.test/a.m3u8"])
    http = _FakeHttp(
        {
            "https://cdn.test/a.m3u8": _media_playlist(),
            "https://cdn.test/seg.ts": b"<html>404</html>",
        }
    )

    result = await _check_streams(data, http, SITE, limit=1)

    assert result["measured_by_sps"] == 0
    assert result["max_resolution"] is None
    assert result["is_4k"] is None


# -- freshness -------------------------------------------------------------------------------------


def _dated_vod(days_ago: int | None) -> Vod:
    stamp = None
    if days_ago is not None:
        stamp = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    return Vod(site_key="s", vid="v", name="n", updated_at=stamp)


def test_undated_catalogue_is_not_reported_as_stale() -> None:
    data = SiteData(site_key="s", site_name="s", base_url="https://s.test", adapter="maccms_v10")
    data.vods.append(_dated_vod(None))

    assert _recent_fraction(data, days=7) is None


def test_freshness_counts_only_the_recent_ones() -> None:
    data = SiteData(site_key="s", site_name="s", base_url="https://s.test", adapter="maccms_v10")
    data.vods.extend([_dated_vod(1), _dated_vod(3), _dated_vod(30)])

    assert _recent_fraction(data, days=7) == 0.6667


def test_a_zulu_timestamp_is_understood() -> None:
    """Maccms often renders `2026-10-16 12:00:00`; other feeds use ISO with a Z."""
    data = SiteData(site_key="s", site_name="s", base_url="https://s.test", adapter="maccms_v10")
    vod = Vod(site_key="s", vid="v", name="n", updated_at="2026-10-16T12:00:00Z")
    data.vods.append(vod)

    # The exact value depends on "now"; what matters is that a Z suffix does not raise.
    assert _recent_fraction(data, days=3650) in (0.0, 1.0)


# -- boundaries ------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://localhost/",
        "http://10.0.0.1/",
        "http://192.168.0.1/",
    ],
)
async def test_private_addresses_are_refused(url: str) -> None:
    """Same SSRF guard as the rest of the pipeline: this fetches an arbitrary user string."""
    with pytest.raises(ValueError):
        await score(url, sample=1)


@pytest.mark.asyncio
async def test_an_incomplete_probe_is_reported_as_unmeasured_not_as_unusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`status=unknown` means "we did not get an answer", which is not "the site is unusable".

    Observed for real: a probe that had taken 3 s came back after 99 s with `unknown`. The first
    version of this module turned that into "tier 0, not automatable" -- a verdict on the site
    derived from a failure of the network.
    """
    import harvest.scorecard as module
    from harvest.models import ProbeResult

    async def fake_probe(site, http, **kwargs):  # noqa: ANN001
        return ProbeResult(tier=0, adapter="auto", status="unknown")

    monkeypatch.setattr(module, "run_probe", fake_probe)

    card = await score("https://example.com/", sample=1)

    assert card.errors, "an incomplete probe produced no warning at all"
    assert any("探测未完成" in e for e in card.errors), card.errors
    assert not any("not automatable" in e for e in card.errors), (
        "an unanswered probe was reported as a verdict on the site"
    )
    assert card.sampled == 0


# -- recipes and the probe gate ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_pinned_recipe_is_measured_even_when_the_probe_is_inconclusive(monkeypatch) -> None:
    """A recipe that names its adapter must not be gated on a probe it does not need.

    Observed for real: zhuiying3.cc probes as `unknown` (the play address is created at runtime
    and the detail page opens no player), while its recipe collects fine. Reporting "not
    automatable" would have declared a working source unusable.
    """
    from harvest import scorecard as sc
    from harvest.models import ProbeResult

    async def fake_probe(site, http, **kwargs):  # noqa: ANN001, ARG001
        return ProbeResult(tier=0, adapter="auto", status="unknown", evidence={})

    collected = {"ran": False}

    async def fake_collect(site, http, settings, **kwargs):  # noqa: ANN001, ARG001
        collected["ran"] = True
        from harvest.adapters.base import CollectContext
        from harvest.models import SiteData

        data = SiteData(site_key=site.key, site_name=site.name, base_url=site.base_url, adapter="playwright_sniff")
        return data, CollectContext(settings=settings, http=http)

    monkeypatch.setattr(sc, "run_probe", fake_probe)
    monkeypatch.setattr(sc, "run_collect", fake_collect)
    monkeypatch.setattr(sc, "_recipe_for_host", lambda base, settings: SITE.model_copy(update={"adapter": "playwright_sniff"}))
    monkeypatch.setattr(sc, "scan_site_html", lambda text, base: _FakeScan())

    card = await sc.score("https://s.test/", sample=1, streams=0)

    assert collected["ran"] is True
    assert not any("not automatable" in e for e in card.errors), card.errors
    assert any("配方" in note for note in card.notes)


class _FakeScan:
    signals: list[str] = []
    kinds: list[str] = []

    def as_dict(self):
        return {}
