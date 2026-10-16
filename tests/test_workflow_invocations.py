"""Every CLI invocation inside the workflow files must be a valid invocation.

The workflows are the production entry point of this project, and nothing else in this repository
executes them. A wrong flag there is invisible until a scheduled run fails -- which is how this
file came to exist.

The concrete bug: `_collect.yml` ran

    python -m harvest.cli collect --verify false ...

but `--verify` is a flag pair (`--verify/--no-verify`). Click therefore treated `false` as a stray
positional argument and the whole harvest job died with

    Got unexpected extra argument(s) (false)

Locally everything was green, because nothing local runs the shell in those YAML files.

So this test loads the workflows, extracts每个 `harvest.cli <command>` invocation, and checks it
against the command tree that a real invocation builds.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import yaml
from typer.main import get_command

from harvest.cli import app

WORKFLOW_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"

# Shell tokens that end one command and begin another.
SEPARATORS = {";", "&&", "||", "|", ">", ">>"}


def _options() -> dict[str, dict[str, bool]]:
    """command -> {flag: takes_a_value}."""
    options: dict[str, dict[str, bool]] = {}
    for name, cmd in get_command(app).commands.items():
        per_command: dict[str, bool] = {}
        for param in cmd.params:
            if not param.opts:
                continue  # positional argument, handled separately
            # A flag pair (--verify/--no-verify) is boolean; a plain option consumes a value.
            # `is_flag` lives on the option params only, hence the defensive getattr.
            takes_value = not getattr(param, "is_flag", False)
            for flag in (*param.opts, *param.secondary_opts):
                per_command[flag] = takes_value
        options[name] = per_command
    return options


def _accepts_positional() -> dict[str, bool]:
    return {
        name: any(not p.opts for p in cmd.params)
        for name, cmd in get_command(app).commands.items()
    }


def _run_blocks() -> list[tuple[str, str]]:
    """Every (filename, shell text) pair from the workflows' `run:` steps."""
    blocks: list[tuple[str, str]] = []
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job in (doc.get("jobs") or {}).values():
            for step in (job or {}).get("steps") or []:
                if isinstance(step, dict) and isinstance(step.get("run"), str):
                    blocks.append((path.name, step["run"]))
    return blocks


def _invocations() -> list[tuple[str, str, list[str], bool]]:
    """(filename, command, argument tokens, truncated) for every `harvest.cli <cmd>`.

    ``truncated`` is True when scanning stopped at a ``${{ ... }}`` expression or a shell
    variable. The values of such expansions cannot be known statically, so an option left
    dangling at the end of a truncated command must not be reported as missing its value.
    """
    out: list[tuple[str, str, list[str], bool]] = []
    for filename, text in _run_blocks():
        # Join backslash-continuations so a multi-line command reads as one logical line.
        joined = text.replace("\\\n", " ")
        for line in joined.splitlines():
            for match in re.finditer(r"harvest\.cli\s+([a-z][a-z0-9-]*)", line):
                command = match.group(1)
                tokens: list[str] = []
                truncated = False
                for token in line[match.end():].split():
                    if token in SEPARATORS:
                        break
                    if "${{" in token or token.startswith("$") or token.startswith('"$'):
                        truncated = True
                        break
                    tokens.append(token)
                out.append((filename, command, tokens, truncated))
    return out


INVOCATIONS = _invocations()
OPTIONS = _options()
POSITIONAL = _accepts_positional()
IDS = [f"{f}:{c}" for f, c, _, _ in INVOCATIONS]


def _walk(filename: str, command: str, tokens: list[str], truncated: bool) -> list[str]:
    """Return the messages for every structural problem in one invocation.

    Walks the arguments the way the shell would, consuming each value-taking option together
    with its value. Every remaining bare token is a stray positional argument.
    """
    declared = OPTIONS[command]
    problems: list[str] = []

    index = 0
    while index < len(tokens):
        token = tokens[index]

        if not token.startswith("-"):
            problems.append(
                f"{filename}: `{command}` received a bare argument {token!r}; it accepts none."
            )
            index += 1
            continue

        if "=" in token:
            index += 1
            continue

        if not token.startswith("--"):
            index += 1
            continue

        takes_value = declared.get(token)
        if takes_value is None:
            # An undeclared flag is reported by its own test; do not double-report here.
            index += 1
            continue

        following = tokens[index + 1] if index + 1 < len(tokens) else None

        if takes_value:
            if following is None:
                if not truncated:
                    problems.append(
                        f"{filename}: `{command} {token}` expects a value but the command ends there."
                    )
            elif following.startswith("--"):
                problems.append(
                    f"{filename}: `{command} {token}` expects a value but is followed by the "
                    f"option {following!r}."
                )
            index += 2
        else:
            if following is not None and not following.startswith("-"):
                problems.append(
                    f"{filename}: `{command} {token}` is a boolean flag but is given the value "
                    f"{following!r}. Use the negative form (e.g. --no-verify) or drop the argument."
                )
            index += 1

    return problems


def test_the_workflows_actually_invoke_the_cli() -> None:
    """Guard against this whole file passing because the extraction silently broke."""
    commands = {command for _, command, _, _ in INVOCATIONS}

    assert INVOCATIONS, "no harvest.cli invocations found in the workflows"
    for expected in ("plan", "collect", "generate", "verify", "prune", "purge-cdn"):
        assert expected in commands, f"no invocation of {expected!r} found"


@pytest.mark.parametrize(("filename", "command", "tokens", "truncated"), INVOCATIONS, ids=IDS)
def test_invocation_is_structurally_valid(
    filename: str, command: str, tokens: list[str], truncated: bool
) -> None:
    """The core assertion.

    `--verify false` fails here: `--verify` is a boolean flag, so `false` is a bare argument the
    command does not accept. An option whose value comes from a `${{ ... }}` expression at the
    end of the command is tolerated, because its value cannot be known statically.
    """
    assert command in OPTIONS, f"{filename}: unknown command {command!r}"

    problems = _walk(filename, command, tokens, truncated)
    if POSITIONAL.get(command):
        # `probe` takes a URL as a positional argument, so a bare token there is legitimate.
        problems = [p for p in problems if "bare argument" not in p]

    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(("filename", "command", "tokens", "truncated"), INVOCATIONS, ids=IDS)
def test_invocation_uses_declared_flags(
    filename: str, command: str, tokens: list[str], truncated: bool
) -> None:
    declared = OPTIONS[command]
    for token in tokens:
        if not token.startswith("--"):
            continue
        flag = token.split("=", 1)[0]
        assert flag in declared, (
            f"{filename}: `{command} {flag}` is not a declared option of {command!r}. "
            f"Declared: {sorted(declared)}"
        )


def test_the_walker_actually_catches_the_original_bug() -> None:
    """A test that cannot fail is not a test.

    Feed the walker the exact invocation that broke the harvest job and require a complaint.
    """
    problems = _walk("_collect.yml", "collect", ["--site", "x", "--mode", "full", "--verify", "false"], False)

    assert problems, "the walker did not flag `--verify false`"
    assert any("boolean flag" in p for p in problems), problems

    # And the corrected form must be accepted.
    assert not _walk("_collect.yml", "collect", ["--site", "x", "--mode", "full", "--no-verify"], False)


def test_workflow_commands_are_shell_splittable() -> None:
    """A stray quote in a run block would change how the shell parses the whole command."""
    for filename, text in _run_blocks():
        for line in text.replace("\\\n", " ").splitlines():
            if "harvest.cli" not in line:
                continue
            try:
                shlex.split(line)
            except ValueError as exc:  # unbalanced quote
                pytest.fail(f"{filename}: unparsable shell line {line!r}: {exc}")
