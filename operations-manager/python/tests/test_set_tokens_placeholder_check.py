"""staging:set-tokens spots a PLACEHOLDER password behind a lot of sops output too.

`grep -q` stops at the first match and closes its end of the pipe; when `sops --decrypt` keeps
sending after that, its write breaks with SIGPIPE and `pipefail` turns the whole condition
non-zero. The task then takes the else branch and leaves the PAT on PLACEHOLDER, even though the
match really was there. Without `-q` grep reads on and that does not happen.

The task itself wants a real key, a terminal and credentials this test does not have, so what is
isolated here is only the one line this touches, with its own stub for `sops` that returns the
output the test hands it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from tests.programma import echt_programma

REPO_ROOT = Path(__file__).resolve().parents[3]
TASKFILE = REPO_ROOT / "Taskfile.yaml"

#: Well above the pipe buffer (64KiB on Linux) and below the 128KiB limit on a single environment
#: string (`MAX_ARG_STRLEN`), so the SIGPIPE from `grep -q` is a forced failure and not a race that
#: strikes some of the time.
_PADDING = "x" * 100_000


@pytest.fixture(scope="module")
def placeholder_line() -> str:
    taskfile = yaml.safe_load(TASKFILE.read_text())
    shell = next(cmd for cmd in taskfile["tasks"]["staging:set-tokens"]["cmds"] if isinstance(cmd, str))
    return next(line for line in shell.splitlines() if "password: PLACEHOLDER" in line)


def _condition_branch(line: str, sops_output: str) -> str:
    """MATCHED or UNMATCHED: the branch the condition from the Taskfile really takes."""
    script = f'set -o pipefail\nsops() {{ printf "%s" "$SOPS_OUTPUT"; }}\nf=dummy\n{line.strip()}\n  echo MATCHED\nelse\n  echo UNMATCHED\nfi\n'
    proc = subprocess.run(
        [echt_programma("bash"), "-c", script],
        env={"SOPS_OUTPUT": sops_output, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_placeholder_behind_a_lot_of_output_is_still_seen(placeholder_line: str) -> None:
    sops_output = f"stringData:\n  password: PLACEHOLDER\n{_PADDING}\n"
    assert _condition_branch(placeholder_line, sops_output) == "MATCHED"


def test_real_password_is_not_seen_as_placeholder(placeholder_line: str) -> None:
    sops_output = f"stringData:\n  password: a-real-token\n{_PADDING}\n"
    assert _condition_branch(placeholder_line, sops_output) == "UNMATCHED"
