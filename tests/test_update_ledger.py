"""The ledger must record what each run actually did.

`sites/registry.yaml` carries a header line that says "`last_run` is refreshed each run". That was
untrue: the collect job updated an in-memory `Registry` and uploaded only `data/reports/`, so the
update went out with the workspace. The observable damage:

* `last_run` was permanently empty, so the Issue form's duplicate reply said
  "上次采集时间：未知" for a site that had been harvested many times -- which reads as "this has
  never run".
* `adapter`/`tier` kept whatever the intake had written (`auto` / `0`) even after a probe resolved
  them. The ledger then disagreed with the recipe about the site's own tier, and nothing checked
  that pair: `doctor` verifies `base_url` and `enabled`, not these two.
* Reading the ledger to find out whether a site works gave an answer that could be days stale.

The report job merges every site's report, and the reports already carry adapter, tier, status and
counts. So `update-ledger` runs there: after the merge, before the commit.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from harvest.cli import app
from harvest.config import Settings, get_settings
from harvest.generate.report import RunReport, SiteReport, write_report

RUNNER = CliRunner()


def _root(tmp_path: Path, *, keys: list[str], recipe_names: dict[str, str] | None = None) -> Settings:
    (tmp_path / "sites").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    recipe_names = recipe_names or {}

    lines = ["# Site ledger.", "sites:"]
    for key in keys:
        name = recipe_names.get(key, key)
        (tmp_path / "sites" / f"{key}.yaml").write_text(
            f"key: {key}\nname: {name}\nbase_url: https://{key}.example.test\n"
            f"adapter: maccms_v10\nenabled: true\ndetect:\n  tier: 1\n",
            encoding="utf-8",
        )
        lines += [
            f"- key: {key}",
            f"  name: {key}",
            f"  base_url: https://{key}.example.test",
            "  adapter: auto",  # as the Issue intake writes it, before any probe
            "  tier: 0",
            "  enabled: true",
            "  origin: {type: issue, issue: 1}",
            "  last_run: {}",
        ]
    (tmp_path / "sites" / "registry.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return Settings(root=tmp_path)


def _report(settings: Settings, entries: list[SiteReport]) -> None:
    report = RunReport(mode="full")
    for entry in entries:
        report.add_site(entry)
    write_report(report, settings)


def _ledger(settings: Settings) -> dict:
    return yaml.safe_load((settings.sites_dir / "registry.yaml").read_text(encoding="utf-8"))


def _run(settings: Settings, tmp_path: Path, monkeypatch, *args: str):
    monkeypatch.setenv("HARVEST_ROOT", str(tmp_path))
    get_settings.cache_clear()
    result = RUNNER.invoke(app, ["update-ledger", *args])
    get_settings.cache_clear()
    return result


# -- the reported outcome ------------------------------------------------------------------------


def test_last_run_records_the_outcome(tmp_path: Path, monkeypatch) -> None:
    settings = _root(tmp_path, keys=["alpha"])
    _report(settings, [SiteReport(key="alpha", name="alpha", status="ok", vods=7, lines=9,
                                  filtered={"adult": 2})])

    result = _run(settings, tmp_path, monkeypatch)

    assert result.exit_code == 0, result.output
    entry = _ledger(settings)["sites"][0]
    assert entry["last_run"]["status"] == "ok"
    assert entry["last_run"]["vods"] == 7
    assert entry["last_run"]["lines"] == 9
    assert entry["last_run"]["filtered"] == 2
    assert entry["last_run"]["at"], "no timestamp; the duplicate reply would say 未知"


def test_a_failed_site_is_recorded_as_failed(tmp_path: Path, monkeypatch) -> None:
    """The ledger has to be able to say 'this stopped working'."""
    settings = _root(tmp_path, keys=["alpha"])
    _report(settings, [SiteReport(key="alpha", name="alpha", status="failed", vods=0, lines=0)])

    _run(settings, tmp_path, monkeypatch)

    assert _ledger(settings)["sites"][0]["last_run"]["status"] == "failed"


def test_a_skipped_site_is_recorded_too(tmp_path: Path, monkeypatch) -> None:
    settings = _root(tmp_path, keys=["alpha"])
    _report(settings, [SiteReport(key="alpha", name="alpha", status="skipped")])

    _run(settings, tmp_path, monkeypatch)

    assert _ledger(settings)["sites"][0]["last_run"]["status"] == "skipped"


# -- the resolver's verdict ----------------------------------------------------------------------


def test_adapter_and_tier_are_reconciled_with_the_recipe(tmp_path: Path, monkeypatch) -> None:
    """The intake writes adapter: auto / tier: 0; a probe later resolves them. The ledger must follow."""
    settings = _root(tmp_path, keys=["alpha"])
    _report(settings, [SiteReport(key="alpha", name="alpha", adapter="maccms_v10", tier=1)])

    _run(settings, tmp_path, monkeypatch)

    entry = _ledger(settings)["sites"][0]
    assert entry["adapter"] == "maccms_v10", "the ledger still claims the adapter is unresolved"
    assert entry["tier"] == 1, "the ledger still claims tier 0"


def test_an_unknown_adapter_does_not_wipe_a_known_one(tmp_path: Path, monkeypatch) -> None:
    """A report with no adapter must not reset the ledger to empty."""
    settings = _root(tmp_path, keys=["alpha"])
    _report(settings, [SiteReport(key="alpha", name="alpha", adapter="", tier=0)])

    _run(settings, tmp_path, monkeypatch)

    entry = _ledger(settings)["sites"][0]
    assert entry["adapter"] == "auto"  # unchanged
    assert entry["tier"] == 0


def test_the_name_follows_the_recipe(tmp_path: Path, monkeypatch) -> None:
    """The recipe defines the site; the ledger mirrors it.

    This is how an Issue submitted under a terse name (`validation-lziapi`) ends up with the
    recipe's display name in the ledger instead of the submitted string.
    """
    settings = _root(tmp_path, keys=["alpha"], recipe_names={"alpha": "示例影视站"})
    _report(settings, [SiteReport(key="alpha", name="alpha")])

    _run(settings, tmp_path, monkeypatch)

    assert _ledger(settings)["sites"][0]["name"] == "示例影视站"


# -- boundaries ----------------------------------------------------------------------------------


def test_enabled_is_never_touched(tmp_path: Path, monkeypatch) -> None:
    """Switching a site off is a human decision; a run must not undo it."""
    settings = _root(tmp_path, keys=["alpha"])
    path = settings.sites_dir / "registry.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("  enabled: true", "  enabled: false"),
        encoding="utf-8",
    )
    _report(settings, [SiteReport(key="alpha", name="alpha", status="ok")])

    _run(settings, tmp_path, monkeypatch)

    assert _ledger(settings)["sites"][0]["enabled"] is False


def test_base_url_drift_is_left_for_doctor_to_report(tmp_path: Path, monkeypatch) -> None:
    """Reconciling it here would erase the warning that exists to catch the drift."""
    settings = _root(tmp_path, keys=["alpha"])
    path = settings.sites_dir / "registry.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "https://alpha.example.test", "https://elsewhere.example.test"
        ),
        encoding="utf-8",
    )
    _report(settings, [SiteReport(key="alpha", name="alpha")])

    _run(settings, tmp_path, monkeypatch)

    assert _ledger(settings)["sites"][0]["base_url"] == "https://elsewhere.example.test"


def test_no_reports_leaves_the_ledger_untouched(tmp_path: Path, monkeypatch) -> None:
    settings = _root(tmp_path, keys=["alpha"])
    before = (settings.sites_dir / "registry.yaml").read_text(encoding="utf-8")

    result = _run(settings, tmp_path, monkeypatch)

    assert result.exit_code == 0, result.output
    assert (settings.sites_dir / "registry.yaml").read_text(encoding="utf-8") == before


def test_a_report_for_an_unledgered_site_does_not_invent_an_entry(tmp_path: Path, monkeypatch) -> None:
    """An entry nobody registered is a hand edit; inventing one would hide it."""
    settings = _root(tmp_path, keys=["alpha"])
    _report(settings, [SiteReport(key="ghost", name="ghost")])

    result = _run(settings, tmp_path, monkeypatch)

    assert result.exit_code == 0, result.output
    keys = [e["key"] for e in _ledger(settings)["sites"]]
    assert keys == ["alpha"]
    assert "ghost" in result.output


def test_multiple_sites_are_all_updated(tmp_path: Path, monkeypatch) -> None:
    settings = _root(tmp_path, keys=["alpha", "beta"])
    _report(settings, [
        SiteReport(key="alpha", name="alpha", status="ok", vods=1),
        SiteReport(key="beta", name="beta", status="failed"),
    ])

    _run(settings, tmp_path, monkeypatch)

    by_key = {e["key"]: e for e in _ledger(settings)["sites"]}
    assert by_key["alpha"]["last_run"]["status"] == "ok"
    assert by_key["beta"]["last_run"]["status"] == "failed"


def test_the_workflow_runs_it_before_committing() -> None:
    """Order is the whole point: after the merge, before `git add`."""
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "_collect.yml"
    ).read_text(encoding="utf-8")

    assert "update-ledger" in workflow, "the ledger is never refreshed in CI"
    assert workflow.index("update-ledger") < workflow.index("git add data sites config"), (
        "the ledger is refreshed after the commit, so the update would not be in it"
    )


def test_the_ledger_header_claim_is_now_true(tmp_path: Path, monkeypatch) -> None:
    """Guard the premise: the file really does claim last_run is refreshed."""
    header = (
        Path(__file__).resolve().parents[1] / "sites" / "registry.yaml"
    ).read_text(encoding="utf-8").splitlines()[0]

    assert "last_run" in header and "refresh" in header.lower(), header
