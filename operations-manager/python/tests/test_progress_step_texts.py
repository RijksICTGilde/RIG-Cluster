"""Every step text lives in ``opi/core/progress_steps.py``, and nowhere else.

The texts used to sit in about 150 call sites across 18 files, so gelijktrekken meant
finding them all first. This scan keeps them together: it reads the source of every
module under ``opi/`` and fails on a literal step NAME passed to a progress call.

A ``subject=`` stays free: that is the deployment, component or repository the step runs
for, and it is variable by definition. So are the ``error`` arguments of ``fail_task``
and ``fail_project``, which come from exceptions.
"""

from __future__ import annotations

import ast
from pathlib import Path

OPI_ROOT = Path(__file__).resolve().parents[1] / "opi"
MODULE = "core/progress_steps.py"

# Which positional argument carries the step name, per method.
NAME_ARGUMENT = {
    "add_task": 0,
    "add_subtask": 1,
    "update_task": 1,
    "update_current_step": 0,
    "_begin_step": 0,
    "_step": 0,
}
# ``_Steps`` in the sleep-mode flow, recognised by the receiver being called ``steps``.
STEPS_METHODS = {"begin", "note", "failed"}


def _literal_step_names(tree: ast.AST) -> list[tuple[int, str]]:
    """The (line, method) pairs where a literal or f-string is used as a step name."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        method = node.func.attr
        if method in STEPS_METHODS:
            receiver = node.func.value
            if not (isinstance(receiver, ast.Name) and receiver.id == "steps"):
                continue
            index = 0
        elif method in NAME_ARGUMENT:
            index = NAME_ARGUMENT[method]
        else:
            continue
        if len(node.args) <= index:
            continue
        argument = node.args[index]
        if isinstance(argument, ast.JoinedStr) or (
            isinstance(argument, ast.Constant) and isinstance(argument.value, str)
        ):
            found.append((argument.lineno, method))
    return found


def test_step_texts_live_in_one_module() -> None:
    offenders = []
    for path in sorted(OPI_ROOT.rglob("*.py")):
        relative = path.relative_to(OPI_ROOT).as_posix()
        if relative == MODULE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders += [
            f"opi/{relative}:{line}: literal step name passed to {method}()"
            for line, method in _literal_step_names(tree)
        ]
    assert not offenders, "Deze stapteksten horen in opi/" + MODULE + ":\n" + "\n".join(offenders)
