"""Every CLI subcommand must be constructible.

This is the cheapest possible guard against a whole class of dependency drift, and it was written
because that class actually bit us.

`harvest doctor` failed on the first CI run with:

    TypeError: Secondary flag is not valid for non-boolean flag.

Nothing was wrong with `doctor`. The declared environment was wrong: requirements.txt pinned
`typer~=0.12.5` (an old, click-driven typer) with an unbounded `click>=8.0.0`, and CI resolved
click 8.5.0, which rejects the option declarations that typer 0.12.5 emits. The developer machine
had typer 0.27.3, which does not use click at all, so every local run passed.

The lesson is not "test more commands" -- it is that a rebuilt command tree touches every option
declaration at once, so building it is a far better probe than anything that exercises behaviour.
`typer.main.get_command(app)` is the exact call that raises, so it is the exact call to test.
"""

from __future__ import annotations

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from harvest.cli import app

RUNNER = CliRunner()


def _command_names() -> list[str]:
    return sorted(get_command(app).commands)


def test_command_tree_builds_at_all() -> None:
    """If an option declaration is invalid for the installed click, this raises."""
    tree = get_command(app)

    assert tree.commands, "no subcommands registered"
    # The command set is part of the public interface of the tool; a silent disappearance would
    # break the workflows that call these by name.
    for expected in ("probe", "collect", "generate", "verify", "plan", "doctor"):
        assert expected in tree.commands, f"missing subcommand: {expected}"


@pytest.mark.parametrize("name", _command_names())
def test_subcommand_help_renders(name: str) -> None:
    """Help output forces every parameter through conversion and formatting."""
    result = RUNNER.invoke(app, [name, "--help"])

    assert result.exit_code == 0, f"{name} --help failed:\n{result.output}"
    assert name in result.output or "Usage" in result.output
