"""Static correctness checks for the workflow files.

The workflows are this project's production entry point and nothing local executes them, so
mistakes there surface as a failed scheduled run rather than a failed test. These checks are the
subset that can be decided by reading the YAML.

Every check here comes from a real defect:

* `echo "::notice::${{ steps.summarise.outputs.summary }}"` inside the `summarise` step itself.
  Expressions in `run` are evaluated as the step starts -- before that step has written its
  outputs -- so the notice was always empty. Nothing errored; the message was just blank.
* `--verify false` passed to a flag pair. That one is covered in
  tests/test_workflow_invocations.py, which checks CLI arguments against the real command tree.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"
WORKFLOWS = sorted(WORKFLOW_DIR.glob("*.yml"))

# `${{ steps.<id>.outputs.<name> }}`, capturing the id.
_STEP_OUTPUT_RE = re.compile(r"steps\.([A-Za-z0-9_-]+)\.outputs\.")


def _doc(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _executable(run: str) -> str:
    """Drop full-line shell comments.

    Comments in these workflows quote the mistakes they are warning about -- including the
    `::notice::${{ steps.summarise.outputs... }}` line this file exists to prevent. Scanning the
    comments would flag the documentation as the bug.
    """
    return "\n".join(
        line for line in run.splitlines() if not line.lstrip().startswith("#")
    )


def _steps(path: Path) -> list[tuple[str, dict]]:
    """(job name, step) for every step in a workflow, including reusable-workflow jobs."""
    out: list[tuple[str, dict]] = []
    for job_name, job in (_doc(path).get("jobs") or {}).items():
        for step in (job or {}).get("steps") or []:
            if isinstance(step, dict):
                out.append((job_name, step))
    return out


def test_there_are_workflows_to_check() -> None:
    assert WORKFLOWS, "no workflow files found"


@pytest.mark.parametrize("path", WORKFLOWS, ids=[p.name for p in WORKFLOWS])
def test_every_workflow_is_valid_yaml_with_jobs(path: Path) -> None:
    doc = _doc(path)

    assert doc.get("name"), f"{path.name}: workflow has no name"
    assert doc.get("jobs"), f"{path.name}: workflow has no jobs"


def _self_referencing_steps(doc: dict) -> list[str]:
    offenders: list[str] = []
    for job_name, job in (doc.get("jobs") or {}).items():
        for step in (job or {}).get("steps") or []:
            if not isinstance(step, dict):
                continue
            step_id = str(step.get("id") or "")
            run = step.get("run")
            if not step_id or not isinstance(run, str):
                continue
            if step_id in _STEP_OUTPUT_RE.findall(_executable(run)):
                offenders.append(f"{job_name}:{step_id}")
    return offenders


@pytest.mark.parametrize("path", WORKFLOWS, ids=[p.name for p in WORKFLOWS])
def test_no_step_reads_its_own_outputs(path: Path) -> None:
    """The bug that emitted an empty ::notice:: on every run."""
    offenders = _self_referencing_steps(_doc(path))

    assert not offenders, (
        "a step references its own outputs, which are not populated yet when `run` is "
        "evaluated; capture stdout in a shell variable instead: " + ", ".join(offenders)
    )


def test_the_self_reference_check_can_actually_fail() -> None:
    """A check that cannot fail is not a check."""
    broken = {
        "jobs": {
            "report": {
                "steps": [
                    {
                        "id": "summarise",
                        "run": 'echo "::notice::${{ steps.summarise.outputs.summary }}"',
                    }
                ]
            }
        }
    }
    assert _self_referencing_steps(broken) == ["report:summarise"]

    fixed = {
        "jobs": {
            "report": {
                "steps": [
                    {
                        "id": "summarise",
                        "run": 'line=$(harvest summary); echo "::notice::$line"',
                    }
                ]
            }
        }
    }
    assert _self_referencing_steps(fixed) == []


def test_comment_only_mentions_are_not_treated_as_code() -> None:
    """The workflows quote the mistakes they guard against; that must not be flagged."""
    documented = {
        "jobs": {
            "report": {
                "steps": [
                    {
                        "id": "summarise",
                        "run": (
                            "# Do NOT do this:\n"
                            '#   echo "::notice::${{ steps.summarise.outputs.summary }}"\n'
                            'line=$(harvest summary)\necho "::notice::$line"\n'
                        ),
                    }
                ]
            }
        }
    }
    assert _self_referencing_steps(documented) == []


# -- expression syntax ------------------------------------------------------------------------
#
# This is the one check that must NOT skip comments. GitHub expands `${{ ... }}` anywhere in a
# `run` value, comments included -- YAML block scalars carry comments through as ordinary text.
#
# It caught a real breakage: a comment reading
#
#     # expand it with the shell, not with `${{ }}`
#
# made GitHub reject the entire workflow file with "An expression was expected". An empty pair is
# not prose; it is a syntax error, and the failure mode is that the workflow silently stops
# existing -- `gh workflow run` refused, and a 0-second run named after the file appeared.

_EXPRESSION_RE = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)


def _expression_problems(path: Path) -> list[str]:
    problems: list[str] = []
    text = path.read_text(encoding="utf-8")

    # Unbalanced: an opening pair with no closing one.
    opens = text.count("${{")
    closes = text.count("}}")
    if opens > closes:
        problems.append(f"{path.name}: {opens} '{{{{' but only {closes} '}}}}'")

    for match in _EXPRESSION_RE.finditer(text):
        if not match.group(1).strip():
            line = text[: match.start()].count("\n") + 1
            problems.append(f"{path.name}:{line}: empty expression pair")
    return problems


@pytest.mark.parametrize("path", WORKFLOWS, ids=[p.name for p in WORKFLOWS])
def test_no_malformed_expressions_anywhere_not_even_in_comments(path: Path) -> None:
    problems = _expression_problems(path)

    assert not problems, "GitHub will refuse to load this workflow:\n" + "\n".join(problems)


def test_the_expression_check_can_actually_fail(tmp_path: Path) -> None:
    """A check that cannot fail is not a check."""
    broken = tmp_path / "broken.yml"
    broken.write_text(
        "jobs:\n  a:\n    steps:\n      - run: |\n          # expand with `${{ }}`\n",
        encoding="utf-8",
    )
    assert _expression_problems(broken), "empty expression pair not detected"

    unbalanced = tmp_path / "unbalanced.yml"
    unbalanced.write_text(
        "jobs:\n  a:\n    steps:\n      - run: echo '${{ github.sha }'\n", encoding="utf-8"
    )
    assert _expression_problems(unbalanced), "unbalanced braces not detected"

    fine = tmp_path / "fine.yml"
    fine.write_text(
        "jobs:\n  a:\n    steps:\n      - run: echo '${{ github.sha }}'\n", encoding="utf-8"
    )
    assert _expression_problems(fine) == []


@pytest.mark.parametrize("path", WORKFLOWS, ids=[p.name for p in WORKFLOWS])
def test_step_outputs_are_only_read_by_steps_that_declare_an_id(path: Path) -> None:
    """An output written by a step with no `id:` can never be referenced."""
    for job_name, step in _steps(path):
        run = step.get("run")
        if not isinstance(run, str):
            continue
        if "GITHUB_OUTPUT" not in _executable(run):
            continue
        assert step.get("id"), (
            f"{path.name}:{job_name}: a step writes to GITHUB_OUTPUT but declares no id, so its "
            f"outputs cannot be used"
        )


@pytest.mark.parametrize("path", WORKFLOWS, ids=[p.name for p in WORKFLOWS])
def test_job_dependencies_exist(path: Path) -> None:
    jobs = _doc(path).get("jobs") or {}
    names = set(jobs)

    for job_name, job in jobs.items():
        needs = (job or {}).get("needs")
        if not needs:
            continue
        wanted = [needs] if isinstance(needs, str) else list(needs)
        for dependency in wanted:
            assert dependency in names, (
                f"{path.name}: job {job_name!r} needs unknown job {dependency!r}"
            )


@pytest.mark.parametrize("path", WORKFLOWS, ids=[p.name for p in WORKFLOWS])
def test_referenced_step_ids_exist_in_the_same_job(path: Path) -> None:
    """Guards against a renamed step leaving a dangling `steps.x.outputs` behind."""
    for job_name, job in (_doc(path).get("jobs") or {}).items():
        declared = {
            str(step.get("id"))
            for step in (job or {}).get("steps") or []
            if isinstance(step, dict) and step.get("id")
        }
        for step in (job or {}).get("steps") or []:
            if not isinstance(step, dict):
                continue
            blob = yaml.safe_dump(step, allow_unicode=True)
            # Job-level `needs.<job>.outputs` is a different namespace and must not be scanned.
            for referenced in _STEP_OUTPUT_RE.findall(blob):
                assert referenced in declared, (
                    f"{path.name}:{job_name} references steps.{referenced}.outputs but no step "
                    f"with that id exists (declared: {sorted(declared)})"
                )


@pytest.mark.parametrize("path", WORKFLOWS, ids=[p.name for p in WORKFLOWS])
def test_third_party_actions_are_version_pinned(path: Path) -> None:
    """`@main` in an action reference is an unreviewed code path into CI."""
    for job_name, step in _steps(path):
        uses = step.get("uses")
        if not isinstance(uses, str) or uses.startswith("./"):
            continue
        _, _, ref = uses.partition("@")
        assert ref, f"{path.name}:{job_name}: {uses} has no version"
        assert ref not in ("main", "master"), (
            f"{path.name}:{job_name}: {uses} tracks a moving branch"
        )


# -- labels -----------------------------------------------------------------------------------
#
# Labels are *repository state*, not files. A workflow that gates on a label therefore depends on
# something a fork does not necessarily have, and nothing local can notice the gap. This
# repository had none of the four labels its own templates and workflows referred to -- including
# `site-request`, which is the gate on the entire Issue intake path. If GitHub had not created it
# from the template, the project's headline feature would simply never have fired, with no error
# anywhere.
#
# So: every label a template or workflow names must be created by something in the repository, and
# the intake gate must not rest on the label alone.

TEMPLATE_DIR = WORKFLOW_DIR.parent / "ISSUE_TEMPLATE"


def _template_labels() -> dict[str, set[str]]:
    """label name -> the template files that declare it."""
    found: dict[str, set[str]] = {}
    for path in sorted(TEMPLATE_DIR.glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for label in doc.get("labels") or []:
            found.setdefault(str(label), set()).add(path.name)
    return found


def _workflow_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in WORKFLOWS)


def _readme_text() -> str:
    return (WORKFLOW_DIR.parents[1] / "README.md").read_text(encoding="utf-8")


def _labels_applied_by_workflows() -> set[str]:
    """Label names the workflows attach to issues."""
    found: set[str] = set()
    for path in WORKFLOWS:
        for _, step in _steps(path):
            run = step.get("run")
            if not isinstance(run, str):
                continue
            found.update(re.findall(r"--add-label\s+\"?([A-Za-z0-9:_-]+)", run))
    return found


def test_templates_declare_labels() -> None:
    """Guard against the extraction breaking and the checks below passing vacuously."""
    assert _template_labels(), "no labels declared by any issue template"


def test_there_are_labels_to_check() -> None:
    assert _labels_applied_by_workflows(), "no labels are applied by any workflow"


@pytest.mark.parametrize(
    "label", sorted(set(_template_labels()) | {"collected", "needs-manual"})
)
def test_every_referenced_label_is_created_or_documented(label: str) -> None:
    """A label nothing creates and nothing documents is a label that will be missing.

    This repository referred to four labels and had none of them, because GitHub's defaults were
    all it shipped with. The gate on the Issue intake path is one of those labels, so the whole
    feature was one missing label away from never firing.

    A label counts as handled if automation creates it (`gh label create` / the workflow's
    `ensure_label` helper) or if the README gives the operator a one-time command. Labels are
    repository state, so *someone* has to create them -- the only question is whether it is
    written down.
    """
    automation = _workflow_text()
    docs = _readme_text()

    created_by_ci = (
        f'label create "{label}"' in automation
        or f"label create {label}" in automation
        or bool(re.search(rf"ensure_label\s+{re.escape(label)}\b", automation))
    )
    documented = f"gh label create {label}" in docs or f"gh label create \"{label}\"" in docs

    assert created_by_ci or documented, (
        f"label {label!r} is used by a template or workflow but nothing creates it and the "
        f"README does not document how. It will be missing in a fresh fork."
    )


def test_the_intake_gate_does_not_rest_on_a_label_alone() -> None:
    """The fallback is what makes the intake work in a fork that has no labels yet."""
    doc = _doc(WORKFLOW_DIR / "issue.yml")
    condition = (doc.get("jobs") or {}).get("intake", {}).get("if")

    assert condition, "the intake job has no condition"
    assert "site-request" in condition, "the label signal was dropped"
    assert "站点地址" in condition, (
        "the intake gate only looks at a label; a fork without that label would never process a "
        "submission and nothing would report the gap"
    )


def test_a_failing_label_does_not_skip_the_issue_close() -> None:
    """`set -e` plus a missing label used to abort before `gh issue close`.

    The observable outcome was a successfully harvested site leaving its Issue open and
    unlabelled -- the acknowledgement said "已受理" and then the trail stopped.
    """
    steps = [
        step
        for _, step in _steps(WORKFLOW_DIR / "issue.yml")
        if isinstance(step.get("run"), str) and "gh issue close" in step["run"]
    ]
    assert steps, "no step closes the issue"

    script = steps[0]["run"]
    label_lines = [ln for ln in script.splitlines() if "--add-label" in ln]

    assert label_lines, "the close step no longer labels the issue"
    for line in label_lines:
        assert "||" in line, (
            f"an unguarded label edit under `set -e` aborts the rest of the step: {line.strip()}"
        )
