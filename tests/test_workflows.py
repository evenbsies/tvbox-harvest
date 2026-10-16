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
