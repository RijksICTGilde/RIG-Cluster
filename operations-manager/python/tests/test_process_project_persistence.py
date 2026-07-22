"""Regression guard: process_project must persist project_data before committing.

The clone-from.status.completed flag and the recorded database generation are
mutated on the in-memory project_data during process_project (set_clone_status
and the clone's record_clone). If the project file is committed without first
saving that in-memory state, the mutations are lost: every reconcile then
re-reads completed=false / generation=None and re-clones, creating a fresh full
database copy each pass (the regel-k4c PR re-clone bug).

The structural (AST) guards pin the shape of process_project itself, which a full
run cannot reasonably cover (it would require mocking every manager/connector it
drives). The behavioral test then proves the pinned shape actually persists the
mutation: read, set_clone_status, save -- completed=True must be in the dict the
store receives.
"""

import ast
import copy
import inspect
from typing import TYPE_CHECKING, Any

from opi.manager.project_manager import ProjectManager

if TYPE_CHECKING:
    import pytest


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


def _save_calls(func: ast.AST) -> list[ast.Call]:
    return [
        child
        for child in ast.walk(func)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr == "save_and_commit_project"
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
    call exists and is fed the project_data dict the mutations were applied to.
    """
    func = _process_project_node()

    save_calls = _save_calls(func)
    assert save_calls, (
        "process_project must call save_and_commit_project() to persist the in-memory "
        "clone-from.status/generation updates; without it every reconcile re-reads "
        "completed=false / generation=None and re-clones"
    )

    # The save must be handed the very dict the mutations were applied to. A fresh
    # get_contents() read is NOT that dict: the store hands out a deepcopy of the
    # committed state, so the set_clone_status/record_clone mutations are absent
    # from it and a save of that read silently drops them (the regel-k4c re-clone
    # bug, reintroduced). Assert on the argument binding, not on a read existing
    # somewhere in the function.
    for call in save_calls:
        assert call.args, f"save_and_commit_project at offset {call.lineno} must be passed project_data positionally"
        first = call.args[0]
        assert isinstance(first, ast.Name), (
            f"save_and_commit_project at offset {call.lineno} must be passed the project_data "
            "dict bound from the initial get_contents() read -- not an inline expression such as "
            "a fresh read, which is a deepcopy without the in-memory mutations"
        )
        assert first.id == "project_data", (
            f"save_and_commit_project at offset {call.lineno} must be passed the project_data "
            "dict bound from the initial get_contents() read -- not a fresh read, which is a "
            "deepcopy without the in-memory set_clone_status/record_clone mutations"
        )

    # And that dict must actually be bound from the manager's own read, before any save.
    binding_lines = [
        node.lineno
        for node in ast.walk(func)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "project_data"
        and isinstance(node.value, ast.Await)
        and isinstance(node.value.value, ast.Call)
        and isinstance(node.value.value.func, ast.Attribute)
        and node.value.value.func.attr == "get_contents"
    ]
    assert binding_lines, "project_data must be bound from await self.get_contents()"
    assert min(binding_lines) < min(call.lineno for call in save_calls), "project_data must be read before it is saved"

    # And the project file must no longer be committed straight through a raw connector.
    assert not _await_call_lines(func, "commit_and_push"), (
        "process_project must not commit the project file directly; that bypasses the store's lock and validation"
    )


def test_the_final_save_compares_against_the_state_project_data_was_built_on() -> None:
    """The final save must re-register project_data's own lineage as the merge base.

    The provisioning steps read and even save through this same manager (Keycloak
    realm creation persists its generated admin credentials mid-run), and every such
    read/save moves the manager's recorded compare-and-swap base forward -- past the
    state project_data was built on. Saving project_data against that newer base
    makes the store see everything committed since (worst of all those Keycloak
    credentials, which exist nowhere else) as deleted by us and publish over it.
    process_project therefore snapshots the base while it still matches project_data's
    lineage and restores it before the final save, so the store three-way merges the
    in-memory mutations with whatever landed in between.
    """
    func = _process_project_node()

    save_lines = [call.lineno for call in _save_calls(func)]
    final_save = max(save_lines)

    snapshot_lines = [
        node.lineno
        for node in ast.walk(func)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr.endswith("__contents_as_read")
    ]
    restore_lines = [
        node.lineno
        for node in ast.walk(func)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Attribute) and target.attr.endswith("__contents_as_read") for target in node.targets
        )
    ]

    assert snapshot_lines, "process_project must snapshot the recorded base while it matches project_data's lineage"
    assert restore_lines, "process_project must restore the snapshotted base before the final save"
    assert min(snapshot_lines) < min(restore_lines) < final_save, (
        "the base must be snapshotted before the provisioning steps move it, and restored before the final save"
    )


RELATIVE_PATH = "projects/demo.yaml"

COMMITTED: dict[str, Any] = {
    "name": "demo",
    "deployments": [
        {
            "name": "deployment-1",
            "cluster": "local",
            "clone-from": {"type": "deployment", "reference": "production", "mode": "once"},
        }
    ],
}


class CapturingStore:
    """Serves the committed state and captures what save() is asked to persist."""

    def __init__(self, committed: dict[str, Any]) -> None:
        self._committed = committed
        self.saved_data: dict[str, Any] | None = None
        self.saved_base: dict[str, Any] | None = None

    async def read_path(self, relative_path: str, ref: str = "HEAD") -> dict[str, Any]:
        # A deepcopy per read, like the real store: callers mutate what they get back.
        return copy.deepcopy(self._committed)

    async def save(self, name: str, data: dict[str, Any], **kwargs: Any) -> None:
        self.saved_data = copy.deepcopy(data)
        self.saved_base = copy.deepcopy(kwargs.get("base"))


async def test_set_clone_status_reaches_the_persisted_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Behavioral proof of the guard above: mutate, save, and the mutation is in the
    dict the store is asked to persist -- with the pre-mutation read as its base."""
    store = CapturingStore(COMMITTED)
    monkeypatch.setattr("opi.manager.project_manager.get_project_store", lambda: store)
    manager = ProjectManager(project_file_relative_path=RELATIVE_PATH)

    project_data = await manager.get_contents()
    manager._project_file_handler.set_clone_status(
        project_data, "deployment-1", completed=True, timestamp="2026-07-21T00:00:00+00:00"
    )
    await manager.save_and_commit_project(project_data, "Process project demo", enforce_validation=False)

    assert store.saved_data is not None
    status = store.saved_data["deployments"][0]["clone-from"]["status"]
    assert status["completed"] is True, (
        "the persisted dict must carry the in-memory set_clone_status mutation; "
        "without it clone-from mode 'once' re-clones on every process pass"
    )
    assert store.saved_base == COMMITTED, (
        "the compare-and-swap base must be the pre-mutation read, so a concurrent "
        "write is merged with the clone-status update instead of overwritten"
    )
