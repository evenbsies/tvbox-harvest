"""`harvest doctor` severity semantics.

This file exists because of a real CI failure. `doctor` originally lumped everything into one
`problems` list and exited 1 if it was non-empty. On a fresh clone every template recipe is
necessarily "never probed", so `doctor` exited 1 -- and the `recipes` job in validate.yml was
red on the very first push, permanently, for a repository that was in fact perfectly healthy.

The fix was not to make CI tolerate the exit code. It was to make the tool say what it means:

* An unprobed recipe with ``adapter: auto`` is the *normal* state of a new clone -- ``collect``
  probes it on the fly. Warning.
* A ledger entry whose recipe file is missing, or a recipe and ledger that disagree about the
  base URL, is a real inconsistency. Error.
* A missing blacklist file is the one with teeth: the filter silently stops filtering, which is
  worse than having no filter at all, because the report still claims it ran. Error.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from harvest.cli import app
from harvest.config import get_settings

RUNNER = CliRunner()

RECIPE = """\
key: {key}
name: {name}
base_url: {base_url}
adapter: auto
enabled: false
"""


def _make_tree(tmp_path: Path, *, recipes: dict[str, str], registry: str) -> Path:
    (tmp_path / "sites").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "blacklist").mkdir(parents=True, exist_ok=True)
    for name in ("terms.txt", "regex.txt", "bc_terms.txt", "bc_domains.txt"):
        (tmp_path / "config" / "blacklist" / name).write_text("", encoding="utf-8")
    for key, text in recipes.items():
        (tmp_path / "sites" / f"{key}.yaml").write_text(text, encoding="utf-8")
    (tmp_path / "sites" / "registry.yaml").write_text(registry, encoding="utf-8")
    return tmp_path


def _registry(*entries: str) -> str:
    return "sites:\n" + "".join(entries)


def _entry(key: str, base_url: str, *, tier: str = "null") -> str:
    return (
        f"- key: {key}\n"
        f"  name: {key}\n"
        f"  base_url: {base_url}\n"
        f"  adapter: auto\n"
        f"  tier: {tier}\n"
        f"  enabled: false\n"
        f"  origin: {{type: template}}\n"
        f"  last_run: {{}}\n"
    )


@pytest.fixture()
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the CLI at a throwaway root.

    `get_settings()` is lru_cached, so the cache must be dropped after the environment changes --
    otherwise the first test to call it freezes the real repository root for every test after it.
    """
    monkeypatch.setenv("HARVEST_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_unprobed_template_is_a_warning_not_a_failure(isolated: Path) -> None:
    """The exact case that made CI red: a fresh clone must be allowed to pass."""
    _make_tree(
        isolated,
        recipes={"demo": RECIPE.format(key="demo", name="演示", base_url="https://demo.test")},
        registry=_registry(_entry("demo", "https://demo.test")),
    )
    result = RUNNER.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "never probed" in result.output
    assert "no errors" in result.output


def test_strict_flag_promotes_warnings(isolated: Path) -> None:
    """A maintainer can still demand a fully-reconciled repository."""
    _make_tree(
        isolated,
        recipes={"demo": RECIPE.format(key="demo", name="演示", base_url="https://demo.test")},
        registry=_registry(_entry("demo", "https://demo.test")),
    )
    result = RUNNER.invoke(app, ["doctor", "--strict"])

    assert result.exit_code == 1


def test_registry_entry_without_recipe_is_an_error(isolated: Path) -> None:
    _make_tree(
        isolated,
        recipes={},
        registry=_registry(_entry("ghost", "https://ghost.test")),
    )
    result = RUNNER.invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "no recipe file" in result.output


def test_base_url_disagreement_is_an_error(isolated: Path) -> None:
    """The two files are a claim about the same site; if they disagree, one of them lies."""
    _make_tree(
        isolated,
        recipes={"demo": RECIPE.format(key="demo", name="演示", base_url="https://new.test")},
        registry=_registry(_entry("demo", "https://old.test")),
    )
    result = RUNNER.invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "base_url differs" in result.output


def test_missing_blacklist_file_is_an_error(isolated: Path) -> None:
    """Filtering that silently stops filtering is the failure mode worth blocking on."""
    tree = _make_tree(
        isolated,
        recipes={"demo": RECIPE.format(key="demo", name="演示", base_url="https://demo.test")},
        registry=_registry(_entry("demo", "https://demo.test")),
    )
    (tree / "config" / "blacklist" / "bc_terms.txt").unlink()

    result = RUNNER.invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "missing blacklist file" in result.output


def test_healthy_repository_reports_nothing(isolated: Path) -> None:
    _make_tree(
        isolated,
        recipes={"demo": RECIPE.format(key="demo", name="演示", base_url="https://demo.test")},
        registry=_registry(_entry("demo", "https://demo.test", tier="1")),
    )
    # A probed recipe pinned to an adapter: no warning should remain.
    (isolated / "sites" / "demo.yaml").write_text(
        "key: demo\nname: 演示\nbase_url: https://demo.test\nadapter: maccms_v10\n"
        "enabled: false\ndetect:\n  tier: 1\n",
        encoding="utf-8",
    )
    result = RUNNER.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "no problems found" in result.output
