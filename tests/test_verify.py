"""Pre-commit verification of a generated TVBox entry.

The verify step is the last thing standing between a broken entry and a user's TVBox. Its whole
value is that it exercises the entry the way a client would, so the checks it *claims* to run must
actually run.

They did not. In the workflow the report job calls `harvest verify --targets index.json`, which
goes through `verify_config_file` -> `verify_entry` with no `site_data`. The detail check had a
fallback (take the source's own first record), but the search check had none, so it silently
skipped and reported `search: None` alongside `status: ok`. Three documented checks, two run, and
the report read as a pass. These tests pin the fallback.
"""

from __future__ import annotations

import json

import pytest

from harvest.config import Settings
from harvest.generate.verify import verify_entry
from harvest.models import SiteConfig

API = "https://cj.example.test/api.php/provide/vod/"

LIST_BODY = {
    "code": 1,
    "msg": "数据列表",
    "page": 1,
    "pagecount": 5,
    "total": 100,
    "list": [
        {
            "vod_id": 991,
            "vod_name": "示例影片",
            "type_name": "动作片",
        }
    ],
}

DETAIL_BODY = {
    "code": 1,
    "list": [
        {
            "vod_id": 991,
            "vod_name": "示例影片",
            "vod_play_from": "线路1",
            "vod_play_url": "第01集$https://cdn.example.test/a/1.m3u8#第02集$https://cdn.example.test/a/2.m3u8",
        }
    ],
}


class _Resp:
    def __init__(self, payload, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False) if payload is not None else ""
        self.content = self.text.encode("utf-8")
        self.headers = {"content-type": "text/html; charset=utf-8"}  # the usual misconfiguration

    def json(self):
        return json.loads(self.text)


class _StubHttp:
    """Answers the three calls verify makes, keyed by the `ac`/`wd` parameter."""

    def __init__(self, *, list_payload=LIST_BODY, detail_payload=DETAIL_BODY, search_payload=None):
        self.list_payload = list_payload
        self.detail_payload = detail_payload
        self.search_payload = search_payload
        self.calls: list[dict] = []

    async def get(self, url, site=None, params=None, tolerate=None, **kwargs):  # noqa: ANN001
        params = params or {}
        self.calls.append(dict(params))
        if "wd" in params:
            if self.search_payload is None:
                return _Resp({"code": 1, "list": [{"vod_id": 991, "vod_name": "示例影片"}]})
            return _Resp(self.search_payload)
        if params.get("ac") == "detail":
            return _Resp(self.detail_payload)
        return _Resp(self.list_payload)


def _entry() -> dict:
    return {"key": "demo", "name": "示例站", "api": API, "type": 1, "searchable": 1}


def _site() -> SiteConfig:
    return SiteConfig(
        key="demo", name="示例站", base_url="https://cj.example.test", adapter="maccms_v10"
    )


@pytest.mark.asyncio
async def test_search_is_exercised_without_harvest_data(tmp_path) -> None:
    """The regression: no `site_data` must not mean "skip search"."""
    http = _StubHttp()

    result = await verify_entry(
        _entry(), site=_site(), http=http, settings=Settings(root=tmp_path)
    )

    assert result.status == "ok"
    assert result.list_ok is True
    assert result.detail_ok is True
    # The title comes from the source's own first record.
    assert result.search_ok is True, result.messages
    assert any("wd" in str(c) for c in http.calls), "search was never requested"


@pytest.mark.asyncio
async def test_sample_identity_falls_back_to_the_source_record(tmp_path) -> None:
    """Both checks use the same fallback record, so the list call is made once each."""
    http = _StubHttp()

    await verify_entry(_entry(), site=_site(), http=http, settings=Settings(root=tmp_path))

    list_calls = [c for c in http.calls if c.get("ac") == "list"]
    assert len(list_calls) == 1, "the list response should be fetched once, not per check"


@pytest.mark.asyncio
async def test_no_sample_title_says_so_instead_of_reporting_a_bare_none(tmp_path) -> None:
    """Silence must be labelled, not left to read like a pass."""
    http = _StubHttp(list_payload={"code": 1, "list": [{"vod_id": 991}]})  # no vod_name

    result = await verify_entry(
        _entry(), site=_site(), http=http, settings=Settings(root=tmp_path)
    )

    assert result.search_ok is None
    assert any("not exercised" in m for m in result.messages), result.messages


@pytest.mark.asyncio
async def test_search_miss_is_reported(tmp_path) -> None:
    http = _StubHttp(search_payload={"code": 1, "list": []})

    result = await verify_entry(
        _entry(), site=_site(), http=http, settings=Settings(root=tmp_path)
    )

    assert result.search_ok is False
    assert any("no hits" in m for m in result.messages), result.messages
    # A search miss does not fail the entry: the entry is still playable.
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_unplayable_detail_fails_the_entry(tmp_path) -> None:
    """An entry that opens but plays nothing is the failure this step exists to catch."""
    http = _StubHttp(detail_payload={"code": 1, "list": [{"vod_id": 991, "vod_play_url": ""}]})

    result = await verify_entry(
        _entry(), site=_site(), http=http, settings=Settings(root=tmp_path)
    )

    assert result.status == "failed"
    assert result.detail_ok is False


@pytest.mark.asyncio
async def test_dead_api_fails_without_raising(tmp_path) -> None:
    class _Dead:
        async def get(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("connection refused")

    result = await verify_entry(
        _entry(), site=_site(), http=_Dead(), settings=Settings(root=tmp_path)
    )

    assert result.status == "failed"
    assert result.list_ok is False


@pytest.mark.asyncio
async def test_spider_entry_is_skipped_not_passed(tmp_path) -> None:
    """A non-http api cannot be verified over the network; say skipped, not ok."""
    result = await verify_entry(
        {"key": "demo", "name": "示例站", "api": "csp_AppYsV2"},
        http=_StubHttp(),
        settings=Settings(root=tmp_path),
    )

    assert result.status == "skipped"
    assert result.passed is False
