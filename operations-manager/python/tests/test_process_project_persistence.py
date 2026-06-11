"""Regression guard: process_project must persist project_data before committing.

The clone-from.status.completed flag and the recorded database generation are
mutated on the in-memory project_data during process_project (set_clone_status
and the clone's record_clone). If the project file is committed without first
saving that in-memory state, the mutations are lost: every reconcile then
re-reads completed=false / generation=None and re-clones, creating a fresh full
database copy each pass (the regel-k4c PR re-clone bug).

This is asserted structurally (AST) rather than via a full process_project run,
which would require mocking every manager/connector it drives.
"""

import ast
import inspect

from opi.manager.project_manager import ProjectManager


def _process_project_node() -> ast.AsyncFunctionDef:
    source = inspect.getsource(ProjectManager.process_project)
    module = ast.parse(source.strip())
    node = module.body[0]
    assert isinstance(node, ast.AsyncFunctionDef)
    return node


def _await_call_lines(func: ast.AST, attr_name: str) -> list[int]:
    """Line offsets of every ``await ...<attr_name>(...)`` call in the function."""
    return [
        child.lineno
        for child in ast.walk(func)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr == attr_name
    ]


def test_process_project_saves_before_committing_project_file() -> None:
    func = _process_project_node()

    save_lines = _await_call_lines(func, "save_project_data")
    commit_lines = _await_call_lines(func, "commit_and_push")

    assert save_lines, "process_project must call save_project_data() to persist clone-status/generation updates"
    assert commit_lines, "expected a commit_and_push() in process_project"

    # The persist must happen before the project-file commit, otherwise the
    # in-memory clone-status/generation mutations never reach the committed file.
    assert min(save_lines) < max(commit_lines), (
        "save_project_data() must run before commit_and_push() in process_project; "
        "otherwise clone-from.status/generation updates are committed-less and every "
        "reconcile re-clones"
    )
