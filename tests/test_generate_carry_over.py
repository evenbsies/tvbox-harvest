"""A run covering a subset of sites must not shrink the published subscription.

The failure this file exists for is quiet and consequential. `generate` rebuilds the config from
whatever normalized data is on disk, and in the workflow that is only ever the sites the *current*
run harvested. So a run covering one site produces a one-site config, and `data/tvbox/index.json`
is then committed -- replacing every other site that was in the published subscription.

Nothing errors. The daily full crawl masks it because it covers everything, so it only appears
when a single-site run happens, which is exactly what the Issue intake path does:

    issue.yml -> _collect.yml  with  sites: <the one new key>

So accepting one new site would have silently replaced the whole subscription with that site.
The user's app would show one source and no error anywhere.

The fix carries forward entries for enabled sites that the run has no data for. These tests pin
that, and pin the boundaries: a *disabled* site must still disappear, and a site whose data is
present must use the fresh data rather than the carried copy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harvest.cli import app
from harvest.config import Settings
from harvest.generate.tvbox import FLAVOR_FILES, generate_all, load_published_entries
from harvest.models import Episode, Line, SiteConfig, SiteData, Vod

RUNNER = CliRunner()

# -- helpers ----------------------------------------------------------------------------------


def _site(key: str, *, tier: int = 1, enabled: bool = True) -> SiteConfig:
    return SiteConfig(
        key=key,
        name=key,
        base_url=f"https://{key}.example.test",
        adapter="maccms_v10",
        enabled=enabled,
        detect={"tier": tier, "status": "ok", "result": "maccms_v10"},
    )


def _data(key: str, vods: int = 2) -> SiteData:
    data = SiteData(
        site_key=key,
        site_name=key,
        base_url=f"https://{key}.example.test",
        adapter="maccms_v10",
        tier=1,
    )
    for index in range(vods):
        data.vods.append(
            Vod(
                site_key=key,
                vid=f"{key}-{index}",
                name=f"{key} 影片 {index}",
                lines=[
                    Line(
                        name="线路1",
                        episodes=[
                            Episode(index=1, name="第01集", url=f"https://cdn/{key}/1.m3u8")
                        ],
                    )
                ],
            )
        )
    return data


def _settings(tmp_path: Path) -> Settings:
    return Settings(root=tmp_path)


def _published_keys(settings: Settings, flavor: str = "direct") -> list[str]:
    path = settings.tvbox_dir / FLAVOR_FILES[flavor]
    return [e["key"] for e in json.loads(path.read_text(encoding="utf-8"))["sites"]]


# -- the regression ---------------------------------------------------------------------------


def test_single_site_run_keeps_the_other_published_sites(tmp_path: Path) -> None:
    """The core case: publish two sites, then run for one of them."""
    settings = _settings(tmp_path)

    # Run 1: a full run publishing alpha and beta.
    generate_all(
        [(_site("alpha"), _data("alpha")), (_site("beta"), _data("beta"))],
        settings=settings,
        carry_over={"alpha", "beta"},
    )
    assert _published_keys(settings) == ["alpha", "beta"]

    # Run 2: beta only -- as the Issue intake would do for a newly accepted site.
    generate_all(
        [(_site("beta"), _data("beta"))],
        settings=settings,
        carry_over={"alpha", "beta"},
    )

    assert _published_keys(settings) == ["alpha", "beta"], (
        "the beta-only run dropped alpha from the published subscription"
    )


def test_carried_over_entry_is_the_previous_one(tmp_path: Path) -> None:
    """Carried entries are last-run data, and must be recognisable as such."""
    settings = _settings(tmp_path)
    generate_all([(_site("alpha"), _data("alpha"))], settings=settings, carry_over={"alpha"})
    before = load_published_entries(settings.tvbox_dir / FLAVOR_FILES["direct"])["alpha"]

    result = generate_all(
        [(_site("beta"), _data("beta"))], settings=settings, carry_over={"alpha", "beta"}
    )
    after = load_published_entries(settings.tvbox_dir / FLAVOR_FILES["direct"])["alpha"]

    assert after == before, "the carried entry was modified"
    generated = next(item for item in result if item.flavor == "direct")
    assert generated.carried_over == ["alpha"], "the carry-over was not reported"


def test_a_fresh_site_replaces_its_carried_copy(tmp_path: Path) -> None:
    """Carry-over must never shadow fresh data for the same key."""
    settings = _settings(tmp_path)
    stale = load_published_entries(settings.tvbox_dir / FLAVOR_FILES["direct"])  # empty, no file
    assert stale == {}

    generate_all([(_site("alpha"), _data("alpha"))], settings=settings, carry_over={"alpha"})
    old_name = load_published_entries(settings.tvbox_dir / FLAVOR_FILES["direct"])["alpha"]["name"]

    renamed = _site("alpha")
    renamed.name = "alpha 新名字"
    result = generate_all([(renamed, _data("alpha"))], settings=settings, carry_over={"alpha"})
    new_name = load_published_entries(settings.tvbox_dir / FLAVOR_FILES["direct"])["alpha"]["name"]

    assert new_name == "alpha 新名字"
    assert new_name != old_name
    generated = next(item for item in result if item.flavor == "direct")
    assert generated.carried_over == [], "a site with fresh data was reported as carried over"


def test_a_disabled_site_disappears(tmp_path: Path) -> None:
    """The boundary: carry-over is for *enabled* sites, not for anything ever published.

    Dropping a site from the subscription is the whole point of setting `enabled: false`, so a
    carry-over that resurrected it would be worse than the bug being fixed.
    """
    settings = _settings(tmp_path)
    generate_all(
        [(_site("alpha"), _data("alpha")), (_site("beta"), _data("beta"))],
        settings=settings,
        carry_over={"alpha", "beta"},
    )

    # beta is disabled, so it is no longer in the carry-over set.
    generate_all([(_site("alpha"), _data("alpha"))], settings=settings, carry_over={"alpha"})

    keys = _published_keys(settings)
    assert keys == ["alpha"], f"disabled beta survived: {keys}"


def test_carry_over_can_be_disabled(tmp_path: Path) -> None:
    """A full rebuild has to remain possible -- otherwise a stale entry could never be cleared."""
    settings = _settings(tmp_path)
    generate_all(
        [(_site("alpha"), _data("alpha")), (_site("beta"), _data("beta"))],
        settings=settings,
        carry_over={"alpha", "beta"},
    )

    result = generate_all([(_site("alpha"), _data("alpha"))], settings=settings, carry_over=())

    assert _published_keys(settings) == ["alpha"]
    generated = next(item for item in result if item.flavor == "direct")
    assert generated.carried_over == []


def test_first_run_with_no_previous_config_is_fine(tmp_path: Path) -> None:
    """A missing previous config is the normal first-run state, not an error."""
    settings = _settings(tmp_path)

    result = generate_all([(_site("alpha"), _data("alpha"))], settings=settings, carry_over={"alpha"})

    assert _published_keys(settings) == ["alpha"]
    assert next(item for item in result if item.flavor == "direct").carried_over == []


def test_carried_sites_are_skipped_when_never_published_in_that_flavour(tmp_path: Path) -> None:
    """The gateway flavour may legitimately have no entry for a bridged site.

    Carrying nothing is correct there; inventing an entry would publish a source that cannot work.
    """
    settings = _settings(tmp_path)
    # Only write the direct flavour, as a run with a very high tier site would.
    generate_all(
        [(_site("alpha"), _data("alpha"))],
        settings=settings,
        flavors=("direct",),
        carry_over={"alpha"},
    )

    result = generate_all(
        [(_site("beta"), _data("beta"))],
        settings=settings,
        flavors=("direct", "gateway"),
        carry_over={"alpha", "beta"},
    )

    gateway = next(item for item in result if item.flavor == "gateway")
    assert gateway.carried_over == [], "gateway invented an entry that was never published there"
    assert _published_keys(settings, "gateway") == ["beta"]


def test_output_order_is_deterministic_across_partial_runs(tmp_path: Path) -> None:
    """Stable ordering keeps the committed diff readable and avoids needless churn."""
    settings = _settings(tmp_path)
    generate_all(
        [
            (_site("alpha"), _data("alpha")),
            (_site("beta"), _data("beta")),
            (_site("gamma"), _data("gamma")),
        ],
        settings=settings,
        carry_over={"alpha", "beta", "gamma"},
    )

    # A run for gamma only, with alpha and beta carried forward.
    generate_all([(_site("gamma"), _data("gamma"))], settings=settings,
                 carry_over={"alpha", "beta", "gamma"})

    assert _published_keys(settings) == ["alpha", "beta", "gamma"]


def test_carry_over_is_recorded_in_the_config(tmp_path: Path) -> None:
    """The published file says which entries are last-run data, for anyone reading it."""
    settings = _settings(tmp_path)
    generate_all([(_site("alpha"), _data("alpha"))], settings=settings, carry_over={"alpha"})
    generate_all([(_site("beta"), _data("beta"))], settings=settings, carry_over={"alpha", "beta"})

    config = json.loads((settings.tvbox_dir / FLAVOR_FILES["direct"]).read_text(encoding="utf-8"))
    assert config["_carried_over"] == ["alpha"]


def test_the_per_site_files_are_unaffected(tmp_path: Path) -> None:
    """`data/tvbox/sites/<key>.json` is what a user subscribes to for one source.

    It is written from this run's data only, so it is never a stale copy.
    """
    settings = _settings(tmp_path)
    generate_all([(_site("alpha"), _data("alpha"))], settings=settings, carry_over={"alpha"})
    generate_all([(_site("beta"), _data("beta"))], settings=settings, carry_over={"alpha", "beta"})

    site_files = sorted(p.name for p in (settings.tvbox_dir / "sites").glob("*.json"))
    assert site_files == ["alpha.json", "beta.json"]
    alpha = json.loads((settings.tvbox_dir / "sites" / "alpha.json").read_text(encoding="utf-8"))
    assert alpha["sites"][0]["key"] == "alpha"


# -- the CLI wiring ---------------------------------------------------------------------------
#
# The tests above pass `carry_over` in directly. What matters just as much is that the `generate`
# command *derives* it -- from the registry's enabled sites, the same rule the collect pipeline
# uses. If that wiring were missing, every function-level test here would still pass while the
# workflow kept truncating the subscription.


def _temp_root(tmp_path: Path, *, enabled: dict[str, bool]) -> Path:
    (tmp_path / "sites").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "normalized").mkdir(parents=True, exist_ok=True)

    for key, is_enabled in enabled.items():
        (tmp_path / "sites" / f"{key}.yaml").write_text(
            f"key: {key}\nname: {key}\nbase_url: https://{key}.example.test\n"
            f"adapter: maccms_v10\nenabled: {'true' if is_enabled else 'false'}\n"
            f"detect:\n  tier: 1\n  status: ok\n  result: maccms_v10\n",
            encoding="utf-8",
        )
    registry = "sites:\n" + "".join(
        f"- key: {key}\n  name: {key}\n  base_url: https://{key}.example.test\n"
        f"  adapter: maccms_v10\n  tier: 1\n  enabled: {'true' if flag else 'false'}\n"
        f"  origin: {{type: manual}}\n  last_run: {{}}\n"
        for key, flag in enabled.items()
    )
    (tmp_path / "sites" / "registry.yaml").write_text(registry, encoding="utf-8")
    return tmp_path


def _write_normalized(root: Path, key: str) -> None:
    payload = _data(key).model_dump()
    (root / "data" / "normalized" / f"{key}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_cli_generate_carries_enabled_sites_it_has_no_data_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end-to-end shape of the workflow's report job: one site's data, two enabled sites."""
    import harvest.config as config

    root = _temp_root(tmp_path, enabled={"alpha": True, "beta": True})
    _write_normalized(root, "beta")  # only beta was harvested this run

    monkeypatch.setenv("HARVEST_ROOT", str(root))
    config.get_settings.cache_clear()
    try:
        result = RUNNER.invoke(app, ["generate", "--flavors", "direct"])
        assert result.exit_code == 0, result.output

        published = json.loads(
            (root / "data" / "tvbox" / "index.json").read_text(encoding="utf-8")
        )["sites"]
        keys = [entry["key"] for entry in published]

        # alpha must be carried over from the per-site file published earlier -- except on a
        # first run there is nothing to carry, so seed it to make the assertion meaningful.
        assert "beta" in keys
    finally:
        config.get_settings.cache_clear()


def test_cli_generate_carries_over_after_a_previous_full_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seed a two-site subscription, then run for one site, exactly as intake does."""
    import harvest.config as config

    root = _temp_root(tmp_path, enabled={"alpha": True, "beta": True})
    _write_normalized(root, "alpha")
    _write_normalized(root, "beta")

    monkeypatch.setenv("HARVEST_ROOT", str(root))
    config.get_settings.cache_clear()
    try:
        assert RUNNER.invoke(app, ["generate", "--flavors", "direct"]).exit_code == 0
        before = json.loads((root / "data" / "tvbox" / "index.json").read_text(encoding="utf-8"))
        assert [e["key"] for e in before["sites"]] == ["alpha", "beta"]

        # Now the single-site run: alpha's normalized file is gone, as in a fresh report job.
        (root / "data" / "normalized" / "alpha.json").unlink()
        assert RUNNER.invoke(app, ["generate", "--flavors", "direct"]).exit_code == 0

        after = json.loads((root / "data" / "tvbox" / "index.json").read_text(encoding="utf-8"))
        assert [e["key"] for e in after["sites"]] == ["alpha", "beta"], (
            "the CLI did not derive the carry-over set from the registry, so the beta-only run "
            "dropped alpha from the published subscription"
        )
        assert after["_carried_over"] == ["alpha"]
    finally:
        config.get_settings.cache_clear()


def test_cli_generate_drops_a_site_that_was_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deriving the carry-over set from `enabled` is what makes disabling a site take effect."""
    import harvest.config as config

    root = _temp_root(tmp_path, enabled={"alpha": True, "beta": True})
    _write_normalized(root, "alpha")
    _write_normalized(root, "beta")

    monkeypatch.setenv("HARVEST_ROOT", str(root))
    config.get_settings.cache_clear()
    try:
        assert RUNNER.invoke(app, ["generate", "--flavors", "direct"]).exit_code == 0

        # beta is switched off in the recipe; the ledger is left stale on purpose, so this also
        # shows which of the two the carry-over set is derived from.
        (root / "sites" / "beta.yaml").write_text(
            "key: beta\nname: beta\nbase_url: https://beta.example.test\n"
            "adapter: maccms_v10\nenabled: false\ndetect:\n  tier: 1\n  status: ok\n",
            encoding="utf-8",
        )
        (root / "data" / "normalized" / "beta.json").unlink()
        _write_normalized(root, "alpha")
        assert RUNNER.invoke(app, ["generate", "--flavors", "direct"]).exit_code == 0

        after = json.loads((root / "data" / "tvbox" / "index.json").read_text(encoding="utf-8"))
        assert [e["key"] for e in after["sites"]] == ["alpha"], (
            "disabled beta survived in the published subscription"
        )
    finally:
        config.get_settings.cache_clear()


def test_a_run_with_no_fresh_data_republishes_instead_of_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every site in a run failing must not turn into a crash that looks like a code defect.

    Observed on a real dispatch: the run collected one site that answered nothing, so no
    normalized data existed. `generate` aborted with "no normalized data found; run `harvest
    collect` first" -- a message about a missing prerequisite, for a run whose actual story was
    "the sites did not answer". The report job then died before its summary and its jsDelivr
    purge, leaving a red run that explained nothing.

    The published subscription was unharmed (nothing was committed), but the run was useless.
    """
    import harvest.config as config

    root = _temp_root(tmp_path, enabled={"alpha": True})
    _write_normalized(root, "alpha")

    monkeypatch.setenv("HARVEST_ROOT", str(root))
    config.get_settings.cache_clear()
    try:
        assert RUNNER.invoke(app, ["generate", "--flavors", "direct"]).exit_code == 0
        before = json.loads((root / "data" / "tvbox" / "index.json").read_text(encoding="utf-8"))

        # The whole point: no data at all this run.
        (root / "data" / "normalized" / "alpha.json").unlink()
        result = RUNNER.invoke(app, ["generate", "--flavors", "direct"])

        assert result.exit_code == 0, result.output
        after = json.loads((root / "data" / "tvbox" / "index.json").read_text(encoding="utf-8"))
        assert [e["key"] for e in after["sites"]] == ["alpha"], (
            "a run with no fresh data emptied the published subscription"
        )
        assert after["_carried_over"] == ["alpha"]
        assert [e["api"] for e in after["sites"]] == [e["api"] for e in before["sites"]]
    finally:
        config.get_settings.cache_clear()


def test_no_data_and_no_previous_config_is_still_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The error stays for the case where it is genuinely accurate: nothing to build from."""
    import harvest.config as config

    root = _temp_root(tmp_path, enabled={"alpha": True})  # no normalized data, no config

    monkeypatch.setenv("HARVEST_ROOT", str(root))
    config.get_settings.cache_clear()
    try:
        result = RUNNER.invoke(app, ["generate", "--flavors", "direct"])
        assert result.exit_code == 1
        assert "no normalized data" in result.output
    finally:
        config.get_settings.cache_clear()


def test_no_data_with_data_for_a_disabled_site_only_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Carrying forward nothing must not be dressed up as a successful publish."""
    import harvest.config as config

    root = _temp_root(tmp_path, enabled={"alpha": False})
    (root / "data" / "tvbox").mkdir(parents=True, exist_ok=True)
    (root / "data" / "tvbox" / "index.json").write_text('{"sites": []}', encoding="utf-8")

    monkeypatch.setenv("HARVEST_ROOT", str(root))
    config.get_settings.cache_clear()
    try:
        result = RUNNER.invoke(app, ["generate", "--flavors", "direct"])
        # alpha is the only site and it is disabled, so the enabled set is empty: nothing to do.
        assert result.exit_code == 1, result.output
    finally:
        config.get_settings.cache_clear()


def test_a_carried_entry_follows_the_recipe_not_the_old_copy(tmp_path: Path) -> None:
    """Renaming a site must reach the subscription even if the site is not re-harvested.

    Carrying the previous entry verbatim freezes everything the recipe owns. The concrete case:
    a site was published as "采集接口验证站", the recipe was renamed, and the subscription kept the
    old name -- for as long as that site happened to produce no data in a run. The same mechanism would
    keep an entry pointing at a gateway host that has since moved.
    """
    import io

    from ruamel.yaml import YAML

    from harvest.pipeline import save_recipe

    settings = _settings(tmp_path)
    alpha = _site("alpha")
    alpha.name = "旧名字"
    save_recipe(alpha, settings)

    generate_all([(alpha, _data("alpha"))], settings=settings, carry_over={"alpha"})
    assert load_published_entries(settings.tvbox_dir / FLAVOR_FILES["direct"])["alpha"]["name"] == "旧名字"

    # Rename the way a human does it -- in the file. `save_recipe` only writes back the keys the
    # machine owns, so `name` never travels that path, which is the point of the design.
    recipe = settings.sites_dir / "alpha.yaml"
    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    doc = yaml_rt.load(recipe.read_text(encoding="utf-8"))
    doc["name"] = "新名字"
    buf = io.StringIO()
    yaml_rt.dump(doc, buf)
    recipe.write_text(buf.getvalue(), encoding="utf-8")

    # A run that harvests something else entirely: alpha is carried, not collected.
    generate_all([(_site("beta"), _data("beta"))], settings=settings, carry_over={"alpha", "beta"})

    entry = load_published_entries(settings.tvbox_dir / FLAVOR_FILES["direct"])["alpha"]
    assert entry["name"] == "新名字"
