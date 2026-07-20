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


def test_process_project_persists_in_memory_state_with_the_project_file_commit() -> None:
    """The clone-status/generation updates must be committed, in one locked operation.

    This used to be two steps -- save_project_data() to write the file, then
    commit_and_push() -- and the guard checked their order. That pair had a silent-loss
    window of its own: the write sat uncommitted in the shared warm working copy across
    the awaits in between, where a concurrent reconcile (`reset --hard` + `git clean -fd`,
    on a 30s TTL) discarded it; the commit then found nothing to commit and still
    reported success.

    Both steps are now a single save_and_commit_project() call, so the guard is that the
    call exists and is fed the current in-memory state.
    """
    func = _process_project_node()

    save_lines = _await_call_lines(func, "save_and_commit_project")
    assert save_lines, (
        "process_project must call save_and_commit_project() to persist the in-memory "
        "clone-from.status/generation updates; without it every reconcile re-reads "
        "completed=false / generation=None and re-clones"
    )

    # It must be fed the live project data, not a stale dict captured earlier.
    contents_lines = _await_call_lines(func, "get_contents")
    assert contents_lines, "save_and_commit_project() must be given the current get_contents() state"

    # And the project file must no longer be committed straight through a raw connector.
    assert not _await_call_lines(func, "commit_and_push"), (
        "process_project must not commit the project file directly; that bypasses the store's lock and validation"
    )
