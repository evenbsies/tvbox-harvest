"""Tier-2 adapter behaviour: keyword discovery and field backfill.

Both features exist because of a real site (tv.lxyy.club): its search endpoint returns rows
with an empty `pic`, and its only catalogue entry point is a keyword search -- so without these
two behaviours the published subscription shows no artwork and 2 titles.
"""

from __future__ import annotations

import json

from harvest.adapters.base import CollectContext
from harvest.adapters.extractors import jp_get
from harvest.adapters.json_custom import JsonCustomAdapter
from harvest.models import SiteConfig


class FakeResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.status_code = status
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self) -> object:
        return json.loads(self.text)


class FakeHttp:
    """Routes by substring, so a test reads as "this URL shape answers this payload"."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    async def get(self, url: str, **_kwargs: object) -> FakeResponse:
        self.calls.append(url)
        for needle, payload in self.routes.items():
            if needle in url:
                return FakeResponse(payload)
        return FakeResponse({"ok": False}, status=404)


def make_ctx(http: FakeHttp) -> CollectContext:
    ctx = CollectContext(http=http)  # type: ignore[arg-type]
    ctx.max_titles = 50
    ctx.max_requests = 200
    return ctx


def lxyy_like_site(**probe_overrides: object) -> SiteConfig:
    probe = {
        "categories": ["庆余年"],
        "keyword_source": {
            "url": "{base_url}/home",
            "path": "$.categories[*][*].name",
            "limit": 3,
        },
        **probe_overrides,
    }
    return SiteConfig(
        key="t2",
        name="示例",
        base_url="https://demo.test",
        adapter="json_custom",
        probe=probe,
        fields={
            "list_url": "{base_url}/search?wd={cat}",
            "list_path": "$.results[*]",
            "id": "$.id",
            "name": "$.name",
            "pic": "$.details[0].pic",
            "detail_url": "{base_url}/detail?id={id}",
            "episodes_path": "$.details[0].episodes[*].episodes[*]",
            "episode_name": "$.name",
            "episode_url": "$.url",
        },
    )


class TestJsonPathDictWildcard:
    def test_wildcard_flattens_dict_values(self) -> None:
        """`[*]` on a dict yields its values -- how a chart keyed by category is reachable."""
        payload = {
            "categories": {
                "国产剧": [{"name": "今日宜偏爱"}, {"name": "征途"}],
                "动作片": [{"name": "一英里"}],
            }
        }
        assert jp_get(payload, "$.categories[*][*].name") == ["今日宜偏爱", "征途", "一英里"]

    def test_wildcard_on_dict_does_not_match_a_key(self) -> None:
        payload = {"categories": {"国产剧": []}}
        assert jp_get(payload, "$.categories[*].nope") == []


class TestKeywordSource:
    async def test_chart_titles_become_search_terms(self) -> None:
        home = {
            "categories": {
                "国产剧": [{"name": "今日宜偏爱"}, {"name": "征途"}],
                "动作片": [{"name": "一英里"}],
            }
        }
        http = FakeHttp({"action=home": home, "/search": {"results": []}})
        site = lxyy_like_site()
        site.probe["keyword_source"]["url"] = "{base_url}/api/proxy.php?action=home"

        await JsonCustomAdapter().collect(site, make_ctx(http))

        searched = [u for u in http.calls if "/search" in u]
        # Seeds first, then the chart: a hand-written keyword must never be crowded out.
        assert "wd=%E5%BA%86%E4%BD%99%E5%B9%B4" in searched[0] or "庆余年" in searched[0]
        joined = " ".join(searched)
        for title in ("今日宜偏爱", "征途", "一英里"):
            assert title in joined

    async def test_limit_caps_the_discovered_list(self) -> None:
        home = {"categories": {"x": [{"name": f"标题{i}"} for i in range(10)]}}
        http = FakeHttp({"action=home": home, "/search": {"results": []}})
        site = lxyy_like_site()
        site.probe["keyword_source"]["url"] = "{base_url}/api/proxy.php?action=home"

        await JsonCustomAdapter().collect(site, make_ctx(http))
        assert sum(1 for u in http.calls if "/search" in u) == 4  # 1 seed + 3 discovered

    async def test_non_string_match_is_an_error_not_a_keyword(self) -> None:
        """A truncated path resolves to a parent object; that must not become a search term.

        `jp_tokens` drops an unparseable tail, so `$.categories.*[*].name` tokenises to
        `['categories']` and returns the whole dict. Without this guard the dict travels into
        the request URL percent-encoded -- a nonsense request that looks like a working run.
        """
        home = {"categories": {"国产剧": [{"name": "今日宜偏爱"}]}}
        http = FakeHttp({"action=home": home, "/search": {"results": []}})
        site = lxyy_like_site()
        site.probe["keyword_source"] = {
            "url": "{base_url}/api/proxy.php?action=home",
            "path": "$.categories",  # the shape a typo collapses to
        }
        ctx = make_ctx(http)

        await JsonCustomAdapter().collect(site, ctx)

        assert any(e["where"] == "json_custom:keywords" for e in ctx.errors)
        assert not any("国产剧" in u for u in http.calls if "/search" in u)

    async def test_missing_chart_degrades_to_seed_keywords(self) -> None:
        http = FakeHttp({"/search": {"results": []}})
        site = lxyy_like_site()
        site.probe["keyword_source"]["url"] = "{base_url}/api/proxy.php?action=home"
        ctx = make_ctx(http)

        await JsonCustomAdapter().collect(site, ctx)

        # The chart 404s; the run still searches the hand-written seed.
        assert sum(1 for u in http.calls if "/search" in u) == 1


class TestDetailBackfill:
    async def test_cover_comes_from_the_detail_when_the_list_omits_it(self) -> None:
        search = {
            "results": [{"id": "1", "name": "庆余年 第一季", "pic": ""}],
        }
        detail = {
            "details": [
                {
                    "id": "1",
                    "name": "庆余年 第一季",
                    "pic": "https://img.test/poster.jpg",
                    "episodes": [
                        {"episodes": [{"name": "第01集", "url": "https://cdn.test/1.m3u8"}]}
                    ],
                }
            ]
        }
        http = FakeHttp({"action=home": {"categories": {}}, "/search": search, "/detail": detail})
        site = lxyy_like_site()
        site.probe["keyword_source"]["url"] = "{base_url}/api/proxy.php?action=home"

        data = await JsonCustomAdapter().collect(site, make_ctx(http))

        assert data.vods
        vod = data.vods[0]
        assert vod.pic == "https://img.test/poster.jpg"
        assert vod.lines and vod.lines[0].episodes[0].url == "https://cdn.test/1.m3u8"


class TestBrowseWholeCatalogue:
    """Walking a paginated catalogue that mixes several sources into one list.

    The real site behind these tests (tv.lxyy.club) answers `action=search&wd=%` with the whole
    catalogue -- 160 rows, 20 per source -- and only 20 of them are the source being harvested.
    Two failures came out of that shape, both silent: the walk stopped because the payload's
    `total` is the page size, and the budget counted rows returned rather than rows kept.
    """

    def _site(self, *, max_pages: int = 4) -> SiteConfig:
        return SiteConfig(
            key="t2",
            name="示例",
            base_url="https://demo.test",
            adapter="json_custom",
            probe={},
            fields={
                "list_url": "{base_url}/list?page={page}",
                "list_path": "$.results[*]",
                "page_stop": "empty",
                "max_pages": max_pages,
                "require": {"source": "mj"},
                "id": "$.id",
                "name": "$.name",
            },
        )

    @staticmethod
    def _page(page: int) -> dict:
        rows = [
            {"source": "qilin", "id": f"q{page}{i}", "name": f"其他 {page}-{i}"} for i in range(8)
        ] + [{"source": "mj", "id": f"m{page}{i}", "name": f"目标 {page}-{i}"} for i in range(2)]
        return {"results": rows, "total": len(rows)}  # `total` is the page size, not the count

    def test_walks_past_the_first_page_when_the_site_reports_no_page_count(self) -> None:
        pages = {f"page={n}": self._page(n) for n in range(1, 5)}
        http = FakeHttp(pages)
        ctx = make_ctx(http)
        ctx.max_titles = 6
        site = self._site(max_pages=4)

        result = __import__("asyncio").run(JsonCustomAdapter().collect(site, ctx))

        assert [v.vid for v in result.vods] == ["m10", "m11", "m20", "m21", "m30", "m31"]
        pages_fetched = [u for u in http.calls if "/list?page=" in u]
        assert len(pages_fetched) >= 3, (
            f"the walk stopped after {len(pages_fetched)} page(s); `total` is the page size, "
            "so a page count taken from it ends the walk at page 1"
        )

    def test_budget_counts_kept_rows_not_rows_returned(self) -> None:
        pages = {f"page={n}": self._page(n) for n in range(1, 5)}
        http = FakeHttp(pages)
        ctx = make_ctx(http)
        ctx.max_titles = 3
        site = self._site(max_pages=4)

        result = __import__("asyncio").run(JsonCustomAdapter().collect(site, ctx))

        # Eight rows a page are the wrong source; a budget that counted those would stop at page 1.
        assert len(result.vods) == 3
        assert [v.vid for v in result.vods] == ["m10", "m11", "m20"]

    def test_a_captcha_challenge_is_an_error_not_an_empty_catalogue(self) -> None:
        http = FakeHttp(
            {
                "page=1": self._page(1),
                "page=2": {"success": False, "need_captcha": True, "results": []},
            }
        )
        site = self._site(max_pages=4)
        ctx = make_ctx(http)
        ctx.max_titles = 10

        result = __import__("asyncio").run(JsonCustomAdapter().collect(site, ctx))

        assert len(result.vods) == 2, "the first page should still be published"
        assert any(e["where"] == "json_custom:ratelimit" for e in ctx.errors), (
            "a rate-limit challenge must be reported: otherwise the run looks like a short "
            "catalogue rather than a truncated one"
        )
