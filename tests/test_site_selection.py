"""Which sites a run actually harvests.

Two files carry an `enabled` flag -- `sites/<key>.yaml` (the recipe) and `sites/registry.yaml`
(the ledger) -- and both are documented as the way to switch a site off:

* README's "adding a site" section describes the recipe as the per-site definition, with
  `enabled: false` as the default.
* COMPLIANCE.md's removal flow tells a rights-holder request to be honoured by setting
  `enabled: false` in the ledger and removing the recipe.

The code only ever consulted the ledger. So a user who followed the README and set
`enabled: false` in the recipe kept getting that site harvested, with nothing to indicate why.
Verified directly: recipe `false` + ledger `true` still produced the site in `harvest plan`.

The rule is now that harvesting requires *both* to agree the site is on. That direction is
deliberate: for a disable request, either edit must be sufficient, because a rights-holder
removal silently ignored on the grounds that "the other file still says true" is the worst
outcome available. Enabling by hand therefore needs both files, and `doctor` reports the
disagreement so the ledger cannot quietly drift out of date.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harvest.config import Settings
from harvest.models import ProbeResult
from harvest.pipeline import (
    RunOptions,
    load_recipe,
    load_registry,
    registry_entry_from,
    resolve_sites,
    save_recipe,
    save_registry,
)


def _settings(tmp_path: Path) -> Settings:
    (tmp_path / "sites").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    return Settings(root=tmp_path)


def _add(tmp_path: Path, key: str, *, recipe_enabled: bool, ledger_enabled: bool) -> Settings:
    """Create a site where the two `enabled` flags can be set independently."""
    settings = _settings(tmp_path)
    site = resolve_target(key, enabled=recipe_enabled)
    save_recipe(site, settings)

    registry = load_registry(settings)
    entry = registry_entry_from(site, origin={"type": "manual"})
    entry.enabled = ledger_enabled
    registry.add_or_update(entry)
    save_registry(registry, settings)
    return settings


def resolve_target(key: str, *, enabled: bool):
    from harvest.models import SiteConfig

    return SiteConfig(
        key=key,
        name=key,
        base_url=f"https://{key}.example.test",
        adapter="maccms_v10",
        enabled=enabled,
        detect={"tier": 1, "status": "ok", "result": "maccms_v10"},
    )


def _selected(settings: Settings, sites: list[str] | None = None) -> list[str]:
    return [s.key for s in resolve_sites(RunOptions(sites=sites or [], mode="full"), settings)]


# -- the rule ----------------------------------------------------------------------------------


def test_both_enabled_is_harvested(tmp_path: Path) -> None:
    settings = _add(tmp_path, "both", recipe_enabled=True, ledger_enabled=True)

    assert _selected(settings) == ["both"]


def test_both_disabled_is_not_harvested(tmp_path: Path) -> None:
    settings = _add(tmp_path, "neither", recipe_enabled=False, ledger_enabled=False)

    assert _selected(settings) == []


def _disable_in_recipe(tmp_path: Path, key: str) -> None:
    """Turn a site off the way the docs describe: edit the recipe file.

    Note this deliberately does not go through `save_recipe`. That function merges only
    MACHINE_OWNED_KEYS -- `adapter`, `detect`, `maccms`, `browser`, `selectors`, `probe` -- and
    leaves everything else byte-for-byte intact, so it will not persist a changed `enabled`.
    That is correct: `enabled` is a human decision, and a probe writing detection results back
    must never flip it.
    """
    path = tmp_path / "sites" / f"{key}.yaml"
    text = path.read_text(encoding="utf-8")
    assert "enabled: true" in text, text
    path.write_text(text.replace("enabled: true", "enabled: false"), encoding="utf-8")


def test_recipe_disabled_is_enough(tmp_path: Path) -> None:
    """The documented path: edit the recipe. It used to have no effect at all."""
    settings = _add(tmp_path, "rcp-off", recipe_enabled=False, ledger_enabled=True)

    assert _selected(settings) == [], (
        "setting enabled: false in the recipe did not stop the site being harvested"
    )


def test_a_recipe_flip_is_not_undone_by_a_machine_write_back(tmp_path: Path) -> None:
    """`enabled` is not machine-owned, so a probe write-back cannot resurrect a parked site."""
    from harvest.pipeline import apply_probe_patch

    settings = _add(tmp_path, "parked", recipe_enabled=True, ledger_enabled=True)
    assert _selected(settings) == ["parked"]

    _disable_in_recipe(tmp_path, "parked")
    assert _selected(settings) == []

    # A later probe of the same site rewrites the machine-owned blocks only.
    site = load_recipe("parked", settings)
    save_recipe(apply_probe_patch(site, ProbeResult(url="https://parked.example.test", tier=1)),
                settings)

    assert _selected(settings) == [], "a probe write-back flipped enabled back on"
    assert load_recipe("parked", settings).enabled is False


def test_ledger_disabled_is_enough(tmp_path: Path) -> None:
    """The compliance path: the ledger is where a removal request lands."""
    settings = _add(tmp_path, "led-off", recipe_enabled=True, ledger_enabled=False)

    assert _selected(settings) == []


# -- boundaries --------------------------------------------------------------------------------


def test_an_explicitly_named_site_is_honoured_even_when_disabled(tmp_path: Path) -> None:
    """That is how a quarantined site gets retried by hand, and it must keep working."""
    settings = _add(tmp_path, "quarantined", recipe_enabled=False, ledger_enabled=False)

    assert _selected(settings) == []
    assert _selected(settings, ["quarantined"]) == ["quarantined"]


def test_a_site_only_in_the_ledger_follows_the_ledger(tmp_path: Path) -> None:
    """`resolve_sites` tolerates a ledger entry with no recipe, so the ledger is the only vote."""
    settings = _settings(tmp_path)
    from harvest.models import RegistryEntry

    registry = load_registry(settings)
    registry.add_or_update(
        RegistryEntry(
            key="ledger-only",
            name="ledger-only",
            base_url="https://lo.example.test",
            adapter="maccms_v10",
            enabled=True,
        )
    )
    save_registry(registry, settings)

    assert _selected(settings) == ["ledger-only"]

    registry = load_registry(settings)
    registry.find("ledger-only").enabled = False
    save_registry(registry, settings)
    assert _selected(settings) == []


def test_disabling_does_not_require_the_recipe_to_be_deleted(tmp_path: Path) -> None:
    """A convenience for anyone switching a site off: the recipe stays, the site does not run."""
    settings = _add(tmp_path, "parked", recipe_enabled=True, ledger_enabled=True)
    assert _selected(settings) == ["parked"]

    _disable_in_recipe(tmp_path, "parked")

    assert _selected(settings) == []
    assert (tmp_path / "sites" / "parked.yaml").is_file(), "the recipe was removed"


def test_the_decision_happens_after_the_recipe_is_loaded(tmp_path: Path) -> None:
    """Guards the ordering, which is the bug.

    The original code decided from the ledger while iterating the ledger, so the recipe's flag
    could never influence the outcome no matter what it said.
    """
    settings = _add(tmp_path, "flip", recipe_enabled=True, ledger_enabled=True)

    _disable_in_recipe(tmp_path, "flip")

    # The ledger still says true; the site must still be excluded.
    assert load_registry(settings).find("flip").enabled is True
    assert _selected(settings) == []


@pytest.mark.parametrize(("recipe", "ledger", "expected"), [
    (True, True, True),
    (True, False, False),
    (False, True, False),
    (False, False, False),
])
def test_the_full_truth_table(tmp_path: Path, recipe: bool, ledger: bool, expected: bool) -> None:
    settings = _add(tmp_path, "t", recipe_enabled=recipe, ledger_enabled=ledger)

    assert bool(_selected(settings)) is expected
