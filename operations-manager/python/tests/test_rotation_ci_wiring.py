"""The CI wiring around the key rotation: what has to run, and what does not run yet.

Both checks here read workflow files and need no binary, and that is why they are their own
module. ``test_sops_rotation_round.py``, ``test_key_rotation_project_round.py`` and
``test_key_rotation_engine.py`` all carry ``skipif(which("age") is None)`` at module level, so
with ``age`` off the runner they go quiet in one move: measured on this tree, all 167 of them
skip and nothing goes red. A guard against a silent skip may not sit behind that same skip.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_FEATURE_DOC = _REPO_ROOT / "features" / "sops-sleutel-vervangen.md"

#: The entry points that convert something or touch the cluster.
_ROTATION_SCRIPTS = ("rotate-sops-key.py", "rotate-project-keys.py", "replace-git-pat.py", "set-sops-key-secret.py")


def _triggers(workflow: dict) -> dict | list | str:
    """The ``on:`` block. PyYAML reads the bare word ``on`` as the boolean ``True``."""
    return workflow[True] if True in workflow else workflow.get("on", {})


def _steps_that_run(workflow: dict) -> list[str]:
    """Every ``run:`` of a step that is not switched off by a falsy ``if``."""
    return [
        step.get("run", "")
        for job in workflow.get("jobs", {}).values()
        for step in job.get("steps", [])
        if str(step.get("if", "true")).strip().lower() not in {"false", "${{ false }}"}
    ]


def test_ci_installs_age_and_sops_so_the_rotation_guards_actually_run() -> None:
    """A skip reads as green, and the rotation guards are exactly what must not go quiet.

    Measured twice. Without ``sops`` every test that rotates a real SOPS file skipped on the
    runner while the summary said passed; without ``age`` the three rotation modules skip whole,
    this guard along with them if it lives in one of them.
    """
    workflow = yaml.safe_load((_WORKFLOWS / "ci.yml").read_text())
    # Steps that really run: a step behind a falsy condition installs nothing, and reading only
    # the "run" lines would call that wired up.
    installs = _steps_that_run(workflow)

    assert any(re.search(r"\bage\b", command) for command in installs), (
        "without age the three rotation test modules skip whole, and a skip reads as green"
    )
    assert any("sops" in command and "chmod +x" in command for command in installs)
    dockerfile = (_REPO_ROOT / "operations-manager" / "Dockerfile").read_text()
    pinned = re.search(r"ARG SOPS_VERSION=(v[\d.]+)", dockerfile)
    assert pinned is not None
    assert any(pinned.group(1) in command for command in installs), (
        f"CI must install the same sops as the image ({pinned.group(1)})"
    )


def test_the_monthly_exercise_is_not_running_yet_and_the_doc_still_says_so() -> None:
    """The exercise is a proposal: two places in the feature doc and two test docstrings say so.

    It would run PREPARE and VERIFY-1 on a throwaway key, on a schedule. Until it exists, the
    boundary it will lean on rests on those tests alone, and that is what their docstrings tell
    the next reader (``test_a_round_commits_but_pushes_nothing`` and the four-phase check).
    Build it, and all four sentences are stale the same day -- so this holds the two halves
    together: nothing runs the round on a schedule, and the doc keeps it on the list of what is
    still to come.
    """
    scheduled: dict[str, list[str]] = {}
    for path in sorted(_WORKFLOWS.glob("*.y*ml")):
        workflow = yaml.safe_load(path.read_text())
        triggers = _triggers(workflow)
        if "schedule" not in triggers:
            continue
        runs = [
            command for command in _steps_that_run(workflow) if any(script in command for script in _ROTATION_SCRIPTS)
        ]
        if runs:
            scheduled[path.name] = runs

    assert scheduled == {}, (
        f"the rotation runs on a schedule now: {scheduled}. The docs and the two docstrings that "
        'say "it would run" are stale -- see "De oefenronde" in features/sops-sleutel-vervangen.md'
    )

    future_work = _FEATURE_DOC.read_text().split("## Wat hierna komt", 1)
    assert len(future_work) == 2, "the feature doc lost its list of what is still to come"
    # The bold lead of each bullet, not the prose under it: that prose points at "De oefenronde"
    # by name, so a search over the whole section stays green on a list the item has left.
    still_to_come = re.findall(r"^- \*\*(.+?)[:*]", future_work[1], re.MULTILINE)
    assert still_to_come, "the future-work list lost its shape, so this checks nothing"
    assert any("oefenronde" in item.lower() for item in still_to_come), (
        f"the exercise left the future-work list while nothing runs it: {still_to_come}"
    )
