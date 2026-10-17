"""`prune` must remove artefacts belonging to sites that are no longer published.

That is not housekeeping, it is the removal path. `data/tvbox/sites/<key>.json` is a subscription
URL in its own right -- someone can point TVBox at a single site's file -- and nothing ever deleted
those. A site switched off (or renamed) therefore kept serving its last contents from that URL
indefinitely, while COMPLIANCE.md promises it "disappears from every artefact". Snapshots are the
audit copy of what was harvested, so they go for the same reason.

The rule is deliberately derived from the *generated configs* rather than from the registry: the
question is "what is in the product right now". A site that is enabled but produced nothing this
run is still named by the config, because carry-over kept its entry, so it is not stale.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from harvest.cli import app
from harvest.config import Settings, get_settings
from harvest.generate.tvbox import produced_index_files

RUNNER = CliRunner()


def _root(tmp_path: Path) -> Settings:
    settings = Settings(root=tmp_path)
    for path in (
        settings.tvbox_dir / "sites",
        settings.snapshot_dir,
        settings.raw_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)
    return settings


def _publish(settings: Settings, keys: list[str], *, flavour_file: str = "index.json") -> None:
    """Write a generated config naming exactly `keys`, as `generate` would."""
    config = {"sites": [{"key": k, "name": k, "type": 1, "api": f"https://{k}.test/api"} for k in keys]}
    (settings.tvbox_dir / flavour_file).write_text(json.dumps(config), encoding="utf-8")


def _per_site(settings: Settings, key: str) -> Path:
    path = settings.tvbox_dir / "sites" / f"{key}.json"
    path.write_text('{"sites": []}', encoding="utf-8")
    return path


def _snapshot(settings: Settings, key: str) -> Path:
    path = settings.snapshot_dir / key
    path.mkdir(parents=True, exist_ok=True)
    (path / "manifest.json").write_text("{}", encoding="utf-8")
    return path


def _run(tmp_path: Path, monkeypatch, *args: str) -> None:
    monkeypatch.setenv("HARVEST_ROOT", str(tmp_path))
    get_settings.cache_clear()
    result = RUNNER.invoke(app, ["prune", *args])
    get_settings.cache_clear()
    assert result.exit_code == 0, result.output


# -- the removal promise ------------------------------------------------------------------------


def test_artefacts_of_an_unpublished_site_are_removed(tmp_path: Path, monkeypatch) -> None:
    settings = _root(tmp_path)
    _publish(settings, ["kept"])
    kept_file, stale_file = _per_site(settings, "kept"), _per_site(settings, "gone")
    kept_snap, stale_snap = _snapshot(settings, "kept"), _snapshot(settings, "gone")

    _run(tmp_path, monkeypatch)

    assert kept_file.is_file() and kept_snap.is_dir()
    assert not stale_file.exists(), "a switched-off site kept serving its own subscription URL"
    assert not stale_snap.exists(), "the audit snapshot of a removed site survived"


def test_pruning_is_reported(tmp_path: Path, monkeypatch) -> None:
    """A removal that happens silently is hard to trust; the run log should name the site."""
    settings = _root(tmp_path)
    _publish(settings, [])
    _per_site(settings, "gone")

    monkeypatch.setenv("HARVEST_ROOT", str(tmp_path))
    get_settings.cache_clear()
    result = RUNNER.invoke(app, ["prune"])
    get_settings.cache_clear()

    assert "gone" in result.output


# -- boundaries ----------------------------------------------------------------------------------


def test_nothing_is_removed_without_a_generated_config(tmp_path: Path, monkeypatch) -> None:
    """Guessing that everything is stale is the worst available behaviour."""
    settings = _root(tmp_path)
    pristine = _per_site(settings, "someone")
    snapshot = _snapshot(settings, "someone")

    _run(tmp_path, monkeypatch)

    assert pristine.is_file(), "artefacts were deleted with no config to justify it"
    assert snapshot.is_dir()


def test_a_site_named_by_any_flavour_is_kept(tmp_path: Path, monkeypatch) -> None:
    """A Tier 3/4 site without a gateway is absent from `direct` but still published."""
    settings = _root(tmp_path)
    _publish(settings, ["direct-site"], flavour_file="index.json")
    _publish(settings, ["direct-site", "experimental-site"], flavour_file="index.experimental.json")
    experimental = _per_site(settings, "experimental-site")

    _run(tmp_path, monkeypatch)

    assert experimental.is_file(), "a site published only in the experimental flavour was pruned"


def test_the_alias_file_does_not_make_everything_published(tmp_path: Path, monkeypatch) -> None:
    """`index.direct.json` duplicates `index.json`; reading it must not change the outcome."""
    settings = _root(tmp_path)
    _publish(settings, ["kept"])
    # The alias is written by `generate`, so its presence is normal.
    (settings.tvbox_dir / "index.direct.json").write_text(
        (settings.tvbox_dir / "index.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    stale = _per_site(settings, "gone")

    _run(tmp_path, monkeypatch)

    assert not stale.exists()


def test_stale_pruning_can_be_skipped(tmp_path: Path, monkeypatch) -> None:
    settings = _root(tmp_path)
    _publish(settings, [])
    stale = _per_site(settings, "gone")

    _run(tmp_path, monkeypatch, "--no-stale")

    assert stale.is_file()


# -- the original job ----------------------------------------------------------------------------


def test_raw_files_beyond_the_keep_count_are_trimmed(tmp_path: Path, monkeypatch) -> None:
    settings = _root(tmp_path)
    _publish(settings, ["s"])
    site_raw = settings.raw_dir / "s"
    site_raw.mkdir(parents=True, exist_ok=True)
    for index in range(5):
        path = site_raw / f"{index}.html"
        path.write_text("x", encoding="utf-8")
        # Distinct mtimes so "newest N" is deterministic.
        import os

        os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))

    _run(tmp_path, monkeypatch, "--keep", "2")

    assert len(list(site_raw.glob("*"))) == 2
    assert not (site_raw / "0.html").exists(), "the oldest file should have been trimmed first"


def test_every_generated_entry_point_is_considered(tmp_path: Path, monkeypatch) -> None:
    """Guard the premise: the published set is read from all flavour files `generate` writes."""
    settings = _root(tmp_path)
    for name in produced_index_files():
        (settings.tvbox_dir / name).write_text('{"sites": []}', encoding="utf-8")
    assert len(produced_index_files()) >= 2
