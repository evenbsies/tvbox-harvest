"""The jsDelivr purge list must match what `generate` actually writes.

Spike S4 in the plan: jsDelivr caches a branch for up to 12 hours by default, so a successful
harvest is invisible to subscribers until something explicitly purges the cache. The purge step
is load-bearing, not an optimisation -- and it is only as good as the list of paths it purges.

That list drifted, and the way it drifted is the point of this file.

An earlier cleanup deleted `data/tvbox/index.direct.json` from the working tree, concluded it was
a stale leftover, and removed it from the purge list. It is not a leftover: `generate_all` writes
it on every run as a byte-identical copy of `index.json` under a self-describing name. A test
written to match the *assumption* -- "nothing produces this file" -- passed happily while the
alias went un-purged.

So these tests do not restate the filenames. They assert that the purge targets are *derived*
from the generator's own output list, and that the CLI uses that derivation rather than a literal.
"""

from __future__ import annotations

from pathlib import Path

from harvest.generate.tvbox import (
    DIRECT_ALIAS_FILE,
    FLAVOR_FILES,
    FLAVORS,
    default_purge_targets,
    produced_index_files,
)

CLI_SOURCE = Path(__file__).resolve().parents[1] / "src" / "harvest" / "cli.py"


def test_there_is_something_to_purge() -> None:
    """Guard against the derivation silently producing an empty list."""
    targets = default_purge_targets()

    assert targets, "no purge targets derived"
    assert all(t.startswith("data/tvbox/") for t in targets)


def test_every_flavour_output_is_covered() -> None:
    targets = set(default_purge_targets())

    for flavour in FLAVORS:
        filename = FLAVOR_FILES[flavour]
        assert f"data/tvbox/{filename}" in targets, (
            f"the {flavour} flavour is written to data/tvbox/{filename}, but the purge list does "
            f"not include it. Subscribers would keep a stale copy for up to 12 hours."
        )


def test_the_direct_alias_is_covered() -> None:
    """The alias is a real artefact. Dropping it was the actual regression."""
    assert f"data/tvbox/{DIRECT_ALIAS_FILE}" in set(default_purge_targets())


def test_the_purge_list_is_exactly_what_generate_writes() -> None:
    targets = set(default_purge_targets())
    produced = {f"data/tvbox/{name}" for name in produced_index_files()}

    assert targets == produced, (
        f"the purge list and the generated entry points disagree.\n"
        f"  only in purge list: {sorted(targets - produced)}\n"
        f"  only in generated:  {sorted(produced - targets)}"
    )


def test_written_names_are_unique() -> None:
    """Two flavours sharing a filename would silently overwrite each other."""
    names = produced_index_files()

    assert len(names) == len(set(names)), f"duplicate generated filenames: {names}"


def _purge_cdn_source() -> str:
    """The body of the `purge_cdn` command, and nothing else.

    Scoped deliberately: `verify --targets` legitimately defaults to `"data/tvbox/index.json"`,
    so a whole-file search would flag an unrelated (and correct) literal.
    """
    source = CLI_SOURCE.read_text(encoding="utf-8")
    start = source.index("def purge_cdn(")
    end = source.find("\n@app.command", start)
    return source[start : end if end != -1 else len(source)]


def test_the_cli_derives_the_list_instead_of_hardcoding_it() -> None:
    """A wiring check, deliberately reading the source.

    Testing `default_purge_targets()` alone would not notice the CLI reverting to a literal list,
    which is precisely the failure mode being guarded against. Inspecting the source is the only
    way to assert that intent.
    """
    body = _purge_cdn_source()

    assert "default_purge_targets()" in body, (
        "purge-cdn no longer derives its fallback targets from the generator; a hardcoded list "
        "will drift again"
    )
    assert "data/tvbox/index" not in body, (
        "purge_cdn contains a hardcoded data/tvbox/index*.json path; derive it instead"
    )


def test_the_wiring_check_is_scoped_to_purge_cdn() -> None:
    """Guard against the slice accidentally covering the whole file (which would be too broad)."""
    body = _purge_cdn_source()

    assert body.startswith("def purge_cdn(")
    assert "def verify(" not in body, "the slice ran past purge_cdn into a neighbouring command"
    assert len(body) < 4000, f"the slice looks too large ({len(body)} chars)"
