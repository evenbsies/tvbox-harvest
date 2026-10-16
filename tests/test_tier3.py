"""Tier-3 parsing: server-rendered HTML sites.

Tier 3 is the tier that carries "普通 HTML 站" -- the largest and most varied class of site in
the wild. It had no test coverage at all before this file, and while writing it a real gap
surfaced: most such sites do not link media from the detail page. Their playlist points at one
*intermediate play page per episode* (``/vodplay/12345-1-1.html``), and the address only exists
on that page. The original adapter required a media-looking link, so an entire class of sites
parsed to zero episodes and was reported as unharvestable when it was merely one hop away.

Both strategies are pinned here, plus the cost accounting, because the two differ by a factor
of one request per episode and the report has to say which one is in play.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harvest.adapters.base import CollectContext
from harvest.adapters.html_ssr import HtmlSsrAdapter
from harvest.models import SiteConfig

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "html-ssr"


class _Resp:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code
        self.content = text.encode("utf-8")
        self.headers: dict[str, str] = {}


class _StubHttp:
    """Serves fixtures by substring match and records every URL requested."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    async def get(self, url: str, site=None, tolerate=None, **kwargs):  # noqa: ANN001
        self.calls.append(url)
        for needle, filename in self.mapping.items():
            if needle in url:
                return _Resp((FIXTURES / filename).read_text(encoding="utf-8"))
        return _Resp("", 404)


def _site(**overrides) -> SiteConfig:
    base = {
        "key": "ssr-demo",
        "name": "示例影视",
        "base_url": "https://ssr.example.test",
        "adapter": "html_ssr",
        "enabled": True,
    }
    base.update(overrides)
    return SiteConfig(**base)


def _ctx(mapping: dict[str, str], **kwargs) -> CollectContext:
    return CollectContext(http=_StubHttp(mapping), offline=True, max_titles=10, **kwargs)


# -- strategy A: media links straight from the detail page -----------------------------------

DIRECT_SITE = _site(
    selectors={
        "detail": {
            "title": "h1.title::text",
            "episodes": [
                {
                    "type": "css_list",
                    "selector": ".playlist a",
                    "name": "::text",
                    "url": "::attr(href)",
                }
            ],
        }
    }
)

DIRECT_DETAIL = """<!DOCTYPE html><html><head><title>直接线路</title></head><body>
<h1 class="title">直接线路</h1>
<div class="playlist">
  <a href="https://cdn.ssr.example.test/a/001.m3u8">第01集</a>
  <a href="https://cdn.ssr.example.test/a/002.m3u8">第02集</a>
  <a href="/a/003.m3u8">第03集</a>
</div></body></html>"""


@pytest.mark.asyncio
async def test_direct_media_links_are_read_off_the_detail_page() -> None:
    class _Static:
        def __init__(self, text: str) -> None:
            self.text = text

        async def get(self, url: str, site=None, tolerate=None, **kwargs):  # noqa: ANN001
            return _Resp(self.text)

    ctx = CollectContext(http=_Static(DIRECT_DETAIL), offline=True, max_titles=10)
    adapter = HtmlSsrAdapter()
    # Call the parser directly: catalogue discovery is a separate concern with its own tests.
    vod = await adapter._parse_detail(DIRECT_SITE, "https://ssr.example.test/detail-direct", ctx)

    assert vod is not None
    assert vod.name == "直接线路"
    assert [e.url for e in vod.lines[0].episodes] == [
        "https://cdn.ssr.example.test/a/001.m3u8",
        "https://cdn.ssr.example.test/a/002.m3u8",
        # Relative hrefs must be resolved against the detail page, not left as-is.
        "https://ssr.example.test/a/003.m3u8",
    ]
    assert [e.name for e in vod.lines[0].episodes] == ["第01集", "第02集", "第03集"]
    assert ctx.stats.get("episode_strategy") == "direct"


# -- strategy B: one intermediate play page per episode ---------------------------------------

NESTED_SITE = _site(
    selectors={
        "detail": {
            "title": "h1.title::text",
            "episodes": [
                {
                    "type": "css_list",
                    "selector": ".stui-content__playlist a",
                    "name": "::text",
                    "url": "::attr(href)",
                }
            ],
        }
    }
)


@pytest.mark.asyncio
async def test_playlist_links_that_are_pages_are_followed() -> None:
    """The regression this file exists for.

    Before the fix this returned ``None``: the playlist links are ``/vodplay/...`` pages, so the
    media-suffix filter rejected all three and the title was counted as unparseable.
    """
    html = (FIXTURES / "detail_series_links.html").read_text(encoding="utf-8")

    class _PerEpisode:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get(self, url: str, site=None, tolerate=None, **kwargs):  # noqa: ANN001
            self.calls.append(url)
            if "-1-1.html" in url:
                return _Resp((FIXTURES / "play_page_ep1.html").read_text(encoding="utf-8"))
            if "-1-2.html" in url:
                return _Resp(
                    '<script>var player_aaaa={"url":"https:\\/\\/cdn.ssr.example.test\\/hls\\/12345\\/002\\/index.m3u8"}</script>'
                )
            if "-1-3.html" in url:
                return _Resp(
                    '<script>var player_aaaa={"url":"https:\\/\\/cdn.ssr.example.test\\/hls\\/12345\\/003\\/index.m3u8"}</script>'
                )
            return _Resp(html)

    http = _PerEpisode()
    ctx = CollectContext(http=http, offline=True, max_titles=10)
    adapter = HtmlSsrAdapter()

    episodes = await adapter._episodes_via_play_pages(
        NESTED_SITE,
        "https://ssr.example.test/voddetail/12345.html",
        html,
        NESTED_SITE.selectors["detail"],
        ctx,
    )

    assert [e.url for e in episodes] == [
        "https://cdn.example-ssr.test/hls/12345/index.m3u8",
        "https://cdn.ssr.example.test/hls/12345/002/index.m3u8",
        "https://cdn.ssr.example.test/hls/12345/003/index.m3u8",
    ]
    # Episode numbering and naming must come from the detail page's link text, in order -- the
    # play page for episode 2 is not asked what number it is.
    assert [e.name for e in episodes] == ["第01集", "第02集", "第03集"]
    assert [e.index for e in episodes] == [1, 2, 3]
    assert ctx.stats["episode_strategy"] == "play_pages"
    assert ctx.stats["play_pages_fetched"] == 3


@pytest.mark.asyncio
async def test_nested_play_page_walk_is_capped_and_reported() -> None:
    """A 200-episode series must not silently multiply the run's request count by 200."""
    links = "".join(
        f'<a href="/vodplay/999-1-{n}.html">第{n:02d}集</a>' for n in range(1, 31)
    )
    html = f'<h1 class="title">长剧</h1><div class="stui-content__playlist"><ul>{links}</ul></div>'

    site = _site(
        selectors={
            "detail": {
                "title": "h1.title::text",
                "max_play_pages": 5,
                "episodes": [
                    {
                        "type": "css_list",
                        "selector": ".stui-content__playlist a",
                        "name": "::text",
                        "url": "::attr(href)",
                    }
                ],
            }
        }
    )

    class _Http:
        async def get(self, url: str, site=None, tolerate=None, **kwargs):  # noqa: ANN001
            # Distinct address per page: identical addresses are de-duplicated on purpose, so a
            # stub that returns one URL for every page would collapse to a single episode.
            number = url.rsplit("-", 1)[-1].split(".")[0]
            return _Resp(
                '<script>var player_aaaa={"url":"https:\\/\\/cdn.t\\/'
                + number
                + '.m3u8"}</script>'
            )

    ctx = CollectContext(http=_Http(), offline=True, max_titles=10)
    episodes = await HtmlSsrAdapter()._episodes_via_play_pages(
        site, "https://ssr.example.test/voddetail/999.html", html, site.selectors["detail"], ctx
    )

    assert len(episodes) == 5
    # The truncation is visible, with both numbers, so an operator can decide to raise the cap.
    assert ctx.stats["play_page_truncated"] == [
        {"detail": "https://ssr.example.test/voddetail/999.html", "available": 30, "walked": 5}
    ]


@pytest.mark.asyncio
async def test_movie_share_link_is_followed_and_named() -> None:
    """Films have a single playlist entry whose text is not an episode number."""
    html = (FIXTURES / "detail_movie_links.html").read_text(encoding="utf-8")

    class _Http:
        async def get(self, url: str, site=None, tolerate=None, **kwargs):  # noqa: ANN001
            return _Resp((FIXTURES / "play_page_ep1.html").read_text(encoding="utf-8"))

    ctx = CollectContext(http=_Http(), offline=True, max_titles=10)
    site = _site(
        selectors={
            "detail": {
                "title": "h1.title::text",
                "episodes": [
                    {
                        "type": "css_list",
                        "selector": ".stui-content__playlist a",
                        "name": "::text",
                        "url": "::attr(href)",
                    }
                ],
            }
        }
    )

    episodes = await HtmlSsrAdapter()._episodes_via_play_pages(
        site, "https://ssr.example.test/voddetail/12346.html", html, site.selectors["detail"], ctx
    )

    assert len(episodes) == 1
    # "正片" -- the link text wins over a generated 第01集, and it is what the user sees in TVBox.
    assert episodes[0].name == "正片"
    assert episodes[0].url.endswith("/hls/12345/index.m3u8")


@pytest.mark.asyncio
async def test_no_play_pages_and_no_media_returns_empty() -> None:
    """Say nothing rather than invent an address: a title with no media is not a title."""
    html = '<h1 class="title">空</h1><div class="stui-content__playlist"><ul></ul></div>'

    class _Http:
        async def get(self, url: str, site=None, tolerate=None, **kwargs):  # noqa: ANN001
            return _Resp("")

    ctx = CollectContext(http=_Http(), offline=True, max_titles=10)
    site = _site(selectors={"detail": {"title": "h1.title::text"}})

    episodes = await HtmlSsrAdapter()._episodes_via_play_pages(
        site, "https://ssr.example.test/voddetail/1.html", html, site.selectors["detail"], ctx
    )

    assert episodes == []


@pytest.mark.asyncio
async def test_offsite_play_links_are_not_followed() -> None:
    """SSRF hygiene at the parse layer: a playlist link to another host is not ours to fetch."""
    html = (
        '<h1 class="title">外链</h1><div class="stui-content__playlist"><ul>'
        '<a href="https://evil.example.com/steal">第01集</a>'
        '<a href="https://ssr.example.test/vodplay/1-1-1.html">第02集</a>'
        "</ul></div>"
    )

    class _Http:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get(self, url: str, site=None, tolerate=None, **kwargs):  # noqa: ANN001
            self.calls.append(url)
            return _Resp('<script>var player_aaaa={"url":"https:\\/\\/cdn.t\\/ok.m3u8"}</script>')

    http = _Http()
    ctx = CollectContext(http=http, offline=True, max_titles=10)
    site = _site(
        selectors={
            "detail": {
                "title": "h1.title::text",
                "episodes": [
                    {
                        "type": "css_list",
                        "selector": ".stui-content__playlist a",
                        "name": "::text",
                        "url": "::attr(href)",
                    }
                ],
            }
        }
    )

    episodes = await HtmlSsrAdapter()._episodes_via_play_pages(
        site, "https://ssr.example.test/voddetail/1.html", html, site.selectors["detail"], ctx
    )

    assert all("evil.example.com" not in url for url in http.calls)
    assert len(episodes) == 1
