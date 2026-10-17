"""The 洛雪TV single-source recipe: one `source`, one keyword list, no surprises.

The recipe exists to express a narrow intent -- "only the 4k60帧 source" -- on an interface that
does not support browsing. These tests pin the two things that would silently break that intent:
the source filter disappearing from the URL, and the keyword list being emptied (which the
interface rejects with 「请输入关键词」).
"""

from __future__ import annotations

from harvest.config import get_settings
from harvest.pipeline import load_recipe


def _recipe():
    return load_recipe("lxyy-4k60", get_settings())


def test_the_recipe_pins_the_4k_source() -> None:
    site = _recipe()
    assert site is not None
    assert "source=mj" in str(site.fields["list_url"])
    assert "source=mj" in str(site.fields["detail_url"])


def test_the_keyword_list_is_what_drives_the_harvest() -> None:
    """`action=search` needs `wd`; there is no per-source browse endpoint to fall back on."""
    site = _recipe()
    assert site is not None
    keywords = (site.probe or {}).get("categories") or []
    assert keywords, "no keyword means zero requests, which reads as an empty site"


def test_episodes_are_read_from_the_nested_detail_path() -> None:
    site = _recipe()
    assert site is not None
    # details[0].episodes[*] groups lines; the episodes live one level deeper.
    assert site.fields["episodes_path"] == "$.details[0].episodes[*].episodes[*]"


def test_the_recipe_is_disabled_by_default() -> None:
    site = _recipe()
    assert site is not None
    assert site.enabled is False
