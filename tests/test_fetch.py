"""Fetch-layer tests, including the regression that matters most.

The bug these tests exist to prevent: real MacCMS deployments frequently serve valid JSON
under ``Content-Type: text/html;charset=utf-8``. Treating a wrong content-type as a veto meant
Tier-1 detection failed on a large fraction of genuine targets -- two live sites that both
answered ``?ac=list`` with a complete 150k-record catalogue were classified "unknown".

Both functions therefore trust the *body* and only use the declared type as a tie-breaker.
"""

from __future__ import annotations

import httpx
import pytest

from harvest.fetch.http import is_html_response, looks_like_json, safe_json

MACCMS_BODY = (
    '{"code":1,"msg":"数据列表","page":"1","pagecount":7970,"limit":"20","total":159394,'
    '"list":[{"vod_id":120245,"vod_name":"超级冰冰Show","type_name":"港台综艺"}],'
    '"class":[{"type_id":1,"type_name":"电影片"}]}'
)


def make_response(body: str, content_type: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        headers={"content-type": content_type},
        content=body.encode("utf-8"),
    )


class TestLooksLikeJson:
    def test_accepts_json_with_correct_content_type(self):
        assert looks_like_json(make_response(MACCMS_BODY, "application/json")) is True

    def test_accepts_json_mislabelled_as_html(self):
        """The regression: this exact combination broke Tier-1 detection on live sites."""
        assert looks_like_json(make_response(MACCMS_BODY, "text/html;charset=utf-8")) is True

    def test_accepts_json_mislabelled_as_plain_text(self):
        assert looks_like_json(make_response(MACCMS_BODY, "text/plain")) is True

    def test_rejects_html_page(self):
        html = "<!DOCTYPE html><html><head><title>x</title></head><body>hello</body></html>"
        assert looks_like_json(make_response(html, "text/html;charset=utf-8")) is False

    def test_content_type_alone_is_not_enough(self):
        """A correct content-type must not launder a body that is not actually JSON -- the
        declared type is not consulted at all, so this stays False."""
        assert looks_like_json(make_response("not json at all", "application/json")) is False
        assert looks_like_json(make_response("<html><body>oops</body></html>", "application/json")) is False

    def test_rejects_go_default_404(self):
        assert looks_like_json(make_response("404 page not found\n", "text/plain", 404)) is False

    def test_accepts_json_array(self):
        assert looks_like_json(make_response('[{"a":1}]', "text/html")) is True

    def test_json_body_survives_parsing(self):
        payload = safe_json(make_response(MACCMS_BODY, "text/html;charset=utf-8"))
        assert payload is not None
        assert payload["total"] == 159394


class TestIsHtmlResponse:
    def test_html_body_is_html(self):
        html = "<!DOCTYPE html><html><head></head><body><a href='/play/1'>x</a></body></html>"
        assert is_html_response(make_response(html, "text/html;charset=utf-8")) is True

    def test_json_body_mislabelled_as_html_is_not_html(self):
        """Otherwise the link-clustering stage runs over a JSON catalogue and invents a
        Tier-3 verdict for an API-only site."""
        assert is_html_response(make_response(MACCMS_BODY, "text/html;charset=utf-8")) is False

    def test_json_array_is_not_html(self):
        assert is_html_response(make_response("[1,2,3]", "text/html")) is False


class TestApiOnlyDomainScenario:
    """Both functions must agree for the shape that actually broke: an API-only domain whose
    ``/`` and ``/api.php`` both answer with the same JSON video list."""

    @pytest.mark.parametrize("content_type", ["text/html;charset=utf-8", "application/json"])
    def test_api_root_is_json_not_html(self, content_type):
        resp = make_response(MACCMS_BODY, content_type)
        assert looks_like_json(resp) is True
        assert is_html_response(resp) is False
