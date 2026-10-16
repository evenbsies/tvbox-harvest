"""The jsDelivr purge list must match what `generate` actually writes.

Spike S4 in the plan: jsDelivr caches a branch for up to 12 hours by default, so a successful
harvest is invisible to subscribers until something explicitly purges the cache. The purge step
is therefore load-bearing, not an optimisation -- and it is only as good as the list of paths it
purges.

That list drifted. `purge-cdn`'s fallback named `data/tvbox/index.direct.json`, a file nothing
produces: the *direct* flavour is written to `index.json`. On any run whose git diff came back
empty (a re-run, a forced dispatch, a commit that touched nothing under data/), the fallback was
used and the one file users actually subscribe to was never purged.

These tests tie the two lists together so they cannot disagree again.
"""

from __future__ import annotations

import re
from pathlib import Path

from harvest.generate.tvbox import FLAVOR_FILES, FLAVORS

CLI_SOURCE = Path(__file__).resolve().parents[1] / "src" / "harvest" / "cli.py"


def _fallback_targets() -> list[str]:
    """The hardcoded list in `purge_cdn`, read out of the source.

    Read as text rather than imported because the list is a function-local literal chosen at
    runtime; there is no symbol to import.
    """
    source = CLI_SOURCE.read_text(encoding="utf-8")
    match = re.search(r"if not targets:\n(.*?)\n\n", source, re.DOTALL)
    assert match, "could not find the purge-cdn fallback list"
    return re.findall(r"\"(data/tvbox/[^\"]+)\"", match.group(1))


def test_the_fallback_list_is_non_empty() -> None:
    """Guard against the regex silently matching nothing and the test passing vacuously."""
    assert _fallback_targets(), "no fallback purge targets found in cli.py"


def test_every_flavour_output_is_purged() -> None:
    targets = set(_fallback_targets())

    for flavour in FLAVORS:
        filename = FLAVOR_FILES[flavour]
        assert f"data/tvbox/{filename}" in targets, (
            f"the {flavour} flavour is written to data/tvbox/{filename}, but the purge fallback "
            f"does not include it. Users would keep a stale {filename} for up to 12 hours. "
            f"Fallback list: {sorted(targets)}"
        )


def test_the_fallback_does_not_name_files_that_are_never_written() -> None:
    produced = {f"data/tvbox/{name}" for name in FLAVOR_FILES.values()}
    targets = set(_fallback_targets())

    extra = targets - produced
    assert not extra, (
        f"the purge fallback names files no flavour writes: {sorted(extra)}. "
        f"Produced: {sorted(produced)}"
    )
