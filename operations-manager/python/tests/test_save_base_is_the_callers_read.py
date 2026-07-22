"""The compare-and-swap base must be the state the SAVED dict was built on.

ProjectManager records what get_contents() handed out and passes it to the store as
the base for a compare-and-swap. That works only if the recorded state is the one
the caller's change was applied to.

The hazard is that get_contents() is not called only by the caller that saves.
Projection helpers read the project file too, and they run between the caller's read
and its save -- in almost every write method, on the very next line:

    project_data = await self.get_contents()   # the dict we will save
    project_name = await self.get_name()       # reads the file again

If that second read also recorded, the base would become NEWER than the state
project_data was built on. The store would then re-apply our change on top of a
concurrent write and, because the delta was computed against the newer base,
*revert* that write rather than merge with it -- and the result validates fine, so
nothing catches it. A silent lost update, in the one path built to prevent it.

Projections therefore read with get_contents(record_base=False).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from opi.manager.project_manager import ProjectManager

RELATIVE_PATH = "projects/demo.yaml"

V1: dict[str, Any] = {"name": "demo", "components": [{"name": "alpha"}]}
# A concurrent writer added 'bravo' between our two reads.
V2: dict[str, Any] = {"name": "demo", "components": [{"name": "alpha"}, {"name": "bravo"}]}


class RecordingStore:
    """Serves a different version on each read and captures the base handed to save."""

    def __init__(self, versions: list[dict[str, Any]]) -> None:
        self._versions = versions
        self.reads = 0
        self.saved_base: dict[str, Any] | None = None
        self.save_called = False

    async def read_path(self, relative_path: str, ref: str = "HEAD") -> dict[str, Any]:
        # Hand out the next version, then keep serving the last one.
        version = self._versions[min(self.reads, len(self._versions) - 1)]
        self.reads += 1
        return copy.deepcopy(version)

    async def save(self, name: str, data: dict[str, Any], **kwargs: Any) -> None:
        self.save_called = True
        self.saved_base = kwargs.get("base")


@pytest.fixture
def manager_and_store(monkeypatch: pytest.MonkeyPatch) -> tuple[ProjectManager, RecordingStore]:
    store = RecordingStore([V1, V2])
    monkeypatch.setattr("opi.manager.project_manager.get_project_store", lambda: store)
    # Validation is not what this test is about; the store is faked out anyway.
    monkeypatch.setattr(
        "opi.manager.project_manager.validate_project_schema",
        lambda data: None,
    )
    manager = ProjectManager(project_file_relative_path=RELATIVE_PATH)
    return manager, store


async def test_a_projection_read_does_not_become_the_base(
    manager_and_store: tuple[ProjectManager, RecordingStore],
) -> None:
    """get_name() reads the file, but must not move the compare-and-swap base."""
    manager, store = manager_and_store

    project_data = await manager.get_contents()  # V1 -- what we will save
    assert [c["name"] for c in project_data["components"]] == ["alpha"]

    project_name = await manager.get_name()  # reads again, now sees V2
    assert project_name == "demo"
    assert store.reads == 2, "get_name must actually re-read, or this test proves nothing"

    project_data["components"].append({"name": "ours"})
    await manager.save_and_commit_project(project_data, "add ours", enforce_validation=False)

    assert store.save_called
    assert store.saved_base == V1, (
        "the base must be the state the saved dict was read at, not the newer state "
        "a projection helper happened to observe"
    )


async def test_the_base_is_still_recorded_for_the_caller_read(
    manager_and_store: tuple[ProjectManager, RecordingStore],
) -> None:
    """Regression guard: the compare-and-swap must not silently switch itself off.

    A fix that simply stopped recording would also make this test's sibling pass, so
    pin that a base IS handed over for an ordinary read-modify-write.
    """
    manager, store = manager_and_store

    project_data = await manager.get_contents()
    project_data["components"].append({"name": "ours"})
    await manager.save_and_commit_project(project_data, "add ours", enforce_validation=False)

    assert store.saved_base == V1


async def test_a_dict_the_manager_never_read_has_no_base(
    manager_and_store: tuple[ProjectManager, RecordingStore],
) -> None:
    """Create: nothing was read, so there is no earlier state to preserve."""
    manager, store = manager_and_store

    await manager.save_and_commit_project({"name": "demo", "components": []}, "create", enforce_validation=False)

    assert store.saved_base is None
