"""The 洛雪TV single-source recipe: one `source`, walked through the whole catalogue.

The recipe exists to express a narrow intent -- "only the 4k60帧 source" -- on an interface that
mixes eight sources into one list. These tests pin the three things that would silently break that
intent: the source filter disappearing, the walk stopping at page one because the site's `total`
is the page size, and the recipe drifting back to keyword search (which covered roughly 6% of the
catalogue and made the source look nearly empty).
"""

from __future__ import annotations

from harvest.config import get_settings
from harvest.pipeline import load_recipe, load_registry


def _recipe():
    return load_recipe("lxyy-4k60", get_settings())


def test_the_recipe_pins_the_4k_source() -> None:
    """The list is a mixed eight-source feed; the filter is what makes it a single-source recipe."""
    site = _recipe()
    assert site is not None
    assert (site.fields.get("require") or {}).get("source") == "mj"
    assert "source=mj" in str(site.fields["detail_url"])


def test_the_walk_uses_the_whole_catalogue_form() -> None:
    """`wd=%` is the site's "all rows" query. Anything else here means keyword search again."""
    site = _recipe()
    assert site is not None
    assert "wd=%25" in str(site.fields["list_url"]), (
        "the list URL stopped asking for the whole catalogue; keyword search only reaches the "
        "titles someone happened to write down"
    )
    assert str(site.fields.get("page_stop") or "") == "empty", (
        "the site reports no page count (its `total` is the page size), so the walk must run to "
        "max_pages or the first empty page"
    )
    assert not (site.probe or {}).get("keyword_source"), (
        "keyword discovery is the fallback for interfaces without a catalogue listing"
    )


def test_episodes_are_read_from_the_nested_detail_path() -> None:
    site = _recipe()
    assert site is not None
    # details[0].episodes[*] groups lines; the episodes live one level deeper.
    assert site.fields["episodes_path"] == "$.details[0].episodes[*].episodes[*]"


def test_enabled_state_agrees_between_recipe_and_ledger() -> None:
    """Enabling needs both files; disagreement is how a site silently stops being harvested.

    `_harvest_enabled` requires the recipe *and* the ledger to say yes. Setting only the recipe
    leaves the site out of `harvest plan`, so the scheduled run never touches it -- while an
    explicit `--site <key>` still works, which masks the problem completely. That exact pair was
    true here for a day.
    """
    site = _recipe()
    assert site is not None
    ledger = load_registry(get_settings()).find("lxyy-4k60")
    assert ledger is not None
    assert site.enabled is ledger.enabled
