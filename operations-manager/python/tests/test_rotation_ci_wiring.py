"""The CI wiring around the key rotation: what has to run, and what does not run yet.

The checks here read files on disk -- the workflows and the feature doc -- and need neither
``age`` nor ``sops``, and that is why they are their own module. ``test_sops_rotation_round.py``, ``test_key_rotation_project_round.py`` and
``test_key_rotation_engine.py`` all carry ``skipif(which("age") is None)`` at module level, so
with ``age`` off the runner they go quiet in one move: measured on this tree, all 168 of them
skip and nothing goes red. A guard against a silent skip may not sit behind that same skip.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_FEATURE_DOC = _REPO_ROOT / "features" / "sops-sleutel-vervangen.md"

#: The entry points that convert something or touch the cluster.
_ROTATION_SCRIPTS = ("rotate-sops-key.py", "rotate-project-keys.py", "replace-git-pat.py", "set-sops-key-secret.py")

#: The three modules that carry the rotation guards, and that skip whole without ``age``.
_ROTATION_MODULES = (
    "tests/test_sops_rotation_round.py",
    "tests/test_key_rotation_project_round.py",
    "tests/test_key_rotation_engine.py",
)


def _triggers(workflow: dict) -> dict | list | str:
    """The ``on:`` block. PyYAML reads the bare word ``on`` as the boolean ``True``."""
    return workflow[True] if True in workflow else workflow.get("on", {})


def _steps_that_run(workflow: dict, job: str | None = None) -> list[str]:
    """Every ``run:`` of a step that is not switched off by a falsy ``if``.

    Without ``job`` this walks EVERY job, which is what a sweep over the whole file wants; with a
    job name it walks that job alone, and raises ``KeyError`` if it is gone.
    """
    jobs = workflow.get("jobs", {})
    selected = [jobs[job]] if job is not None else list(jobs.values())
    return [
        step.get("run", "")
        for definition in selected
        for step in definition.get("steps", [])
        if str(step.get("if", "true")).strip().lower() not in {"false", "${{ false }}"}
    ]


def test_ci_installs_age_and_sops_so_the_rotation_guards_actually_run() -> None:
    """A skip reads as green, and the rotation guards are exactly what must not go quiet.

    Measured twice. Without ``sops`` every test that rotates a real SOPS file skipped on the
    runner while the summary said passed; without ``age`` the three rotation modules skip whole,
    this guard along with them if it lives in one of them.

    The job matters as much as the install. Measured: with both install steps moved from ``test``
    to ``license-check`` a sweep over all jobs stays green, while the test job has neither binary
    and 168 rotation tests skip under a summary that says passed.
    """
    workflow = yaml.safe_load((_WORKFLOWS / "ci.yml").read_text())
    # A step behind a falsy condition installs nothing, and reading only the "run" lines would
    # call that wired up.
    installs = _steps_that_run(workflow, job="test")

    assert any(re.search(r"\bage\b", command) for command in installs), (
        "the job 'test' must install age: without it the three rotation test modules skip whole, "
        "and a skip reads as green"
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


def _pytest_argv(workflow: dict, job: str) -> list[str]:
    """The arguments the pytest step of ``job`` hands to pytest, without the runner in front."""
    running = [
        step.get("run", "") for step in workflow["jobs"][job].get("steps", []) if "pytest" in step.get("run", "")
    ]
    assert len(running) == 1, f"job {job!r} has {len(running)} steps running pytest, so this guard reads the wrong one"
    tokens = shlex.split(running[0])
    return tokens[tokens.index("pytest") + 1 :]


def _selection(argv: list[str]) -> tuple[str, list[str]]:
    """The ``-m`` expression and the paths pytest is pointed at.

    ``-p``, ``-k`` and ``-n`` carry their value in the next token, so those are stepped over;
    everything else that does not start with a dash is a path.
    """
    expression = ""
    paths: list[str] = []
    tokens = iter(argv)
    for token in tokens:
        if token == "-m":
            expression = next(tokens, "")
        elif token in {"-p", "-k", "-n"}:
            next(tokens, "")
        elif not token.startswith("-"):
            paths.append(token)
    return expression, paths


def test_the_ci_test_job_really_collects_the_rotation_modules() -> None:
    """Installing the binaries is half of it: the job also has to SELECT these tests.

    The feature doc says under 8.24.01 that the coverage guard runs along in every CI test round,
    and the check above holds ``age`` and ``sops`` in the job that runs pytest. Nothing held the
    step's own selection. Measured: with ``pytest.mark.slow`` added to the three rotation modules
    the CI expression collects 0 of their 168 tests, the local run stays green because nothing
    here excludes ``slow``, and no test in this repo went red -- the same silent quiet the
    install check exists for, one step further down.

    Both halves are measured with the step's OWN arguments, and the marker half through a real
    collection rather than a reading of the markers.
    """
    workflow = yaml.safe_load((_WORKFLOWS / "ci.yml").read_text())
    working_directory = workflow["jobs"]["test"].get("defaults", {}).get("run", {}).get("working-directory")
    assert working_directory == "operations-manager/python", (
        f"the pytest step runs in {working_directory!r}, so its paths no longer mean what this guard reads"
    )
    expression, paths = _selection(_pytest_argv(workflow, "test"))
    assert paths, "the pytest step points at no path at all, so this guard measures nothing"

    for module in _ROTATION_MODULES:
        assert any(module == path or module.startswith(path.rstrip("/") + "/") for path in paths), (
            f"{module} sits outside the paths the CI test step runs ({paths}), so its guards go quiet in CI"
        )

    collected = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            # Its own cache, so a collection inside a run leaves the outer run's cache alone.
            "-p",
            "no:cacheprovider",
            *(["-m", expression] if expression else []),
            *_ROTATION_MODULES,
        ],
        cwd=_REPO_ROOT / working_directory,
        capture_output=True,
        text=True,
    )
    # Exit code 5 is "nothing collected", which is the deselection this measures rather than a
    # broken run, so the sharp message below gets to say it.
    assert collected.returncode in {0, 5}, (
        f"collecting the rotation modules failed:\n{collected.stdout}{collected.stderr}"
    )
    for module in _ROTATION_MODULES:
        assert f"{module}::" in collected.stdout, (
            f"CI selects with -m {expression!r} and that deselects every test in {module}: "
            f"its guards would report green without running\n{collected.stdout[-2000:]}"
        )
