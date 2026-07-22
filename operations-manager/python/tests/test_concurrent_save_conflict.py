"""Concurrent edits to the same project must not silently lose a change.

Before this, ``store.save()`` published the caller's dict unconditionally. Two
requests that each read the project, changed something and saved would leave only
the second change in git -- no error, no conflict, the first edit simply gone.
Measured, not theorised: the demo that drove this returned ``components =
['bravo']`` after adding both alpha and bravo.

Passing ``base`` (the state the caller read) turns the save into a
compare-and-swap: our change is re-applied on top of whatever landed, which is
git's model but on the parsed structure rather than on lines of text. Two writers
each adding a component both survive -- something no text merge can do, since the
two additions land on adjacent lines. Two writers editing the same field is a
genuine collision and raises ConflictError instead of silently dropping one.
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest
from opi.services.project_service import get_project_service
from opi.services.project_store import ConflictError, GitProjectStore
from opi.utils.yaml_util import dump_yaml_to_string, load_yaml_from_string

from test_project_store import (  # type: ignore[import-not-found]
    RELATIVE_PATH,
    FakeGitConnector,
    FakeRemote,
    _project,
)

PROJECT_NAME = "demo"


@pytest.fixture
def store_and_remote(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Any:
    get_project_service().clear_all_projects()
    remote = FakeRemote()
    remote.commit("initial", {RELATIVE_PATH: dump_yaml_to_string(_project())})
    connector = FakeGitConnector(remote, str(tmp_path))
    store = GitProjectStore(working_dir=str(tmp_path))

    async def _get_connector() -> FakeGitConnector:
        return connector

    monkeypatch.setattr(store, "get_connector", _get_connector)
    return store, remote


def _committed(remote: FakeRemote) -> dict[str, Any]:
    parsed = load_yaml_from_string(remote.files[RELATIVE_PATH])
    assert parsed is not None
    return parsed


async def test_without_base_the_second_save_still_wins(store_and_remote: Any) -> None:
    """The old behaviour, kept for call sites that have not adopted base yet."""
    store, remote = store_and_remote
    base = _committed(remote)

    a = copy.deepcopy(base)
    a["description"] = "van A"
    b = copy.deepcopy(base)
    b["description"] = "van B"

    await store.save(PROJECT_NAME, a, message="A", actor="A", enforce_validation=False)
    await store.save(PROJECT_NAME, b, message="B", actor="B", enforce_validation=False)

    assert _committed(remote)["description"] == "van B"


async def test_concurrent_edits_to_unrelated_parts_are_both_kept(store_and_remote: Any) -> None:
    """Edits to different parts of the document do not collide.

    Unlike a text merge, "different parts" is structural here, not typographic:
    it does not matter how close the two fields sit in the rendered YAML.
    """
    store, remote = store_and_remote
    base = _committed(remote)

    a = copy.deepcopy(base)
    a["description"] = "beschrijving van A"

    b = copy.deepcopy(base)
    b.setdefault("deployments", []).append(
        {"name": "productie", "cluster": "odcn-production", "namespace": "demo", "components": []}
    )

    await store.save(PROJECT_NAME, a, message="A", actor="A", enforce_validation=False, base=base)
    # B read the same base, so its save lands on top of A's without knowing about it.
    await store.save(PROJECT_NAME, b, message="B", actor="B", enforce_validation=False, base=base)

    landed = _committed(remote)
    assert landed["description"] == "beschrijving van A", "A's change was lost"
    assert [d["name"] for d in landed.get("deployments", [])] == ["productie"], "B's change was lost"


async def test_two_writers_each_adding_a_component_both_survive(store_and_remote: Any) -> None:
    """The case that started all of this, and the one git cannot do.

    Measured: `git merge-file`, `cherry-pick` and `rebase` all conflict here,
    because the two additions land on adjacent lines. Structurally they do not
    collide at all -- re-applying our change onto theirs keeps both.
    """
    store, remote = store_and_remote
    base = _committed(remote)

    a = copy.deepcopy(base)
    a.setdefault("components", []).append({"name": "alpha", "type": "single"})

    b = copy.deepcopy(base)
    b.setdefault("components", []).append({"name": "bravo", "type": "single"})

    await store.save(PROJECT_NAME, a, message="add alpha", actor="A", enforce_validation=False, base=base)
    await store.save(PROJECT_NAME, b, message="add bravo", actor="B", enforce_validation=False, base=base)

    names = [c["name"] for c in _committed(remote).get("components", [])]
    assert "alpha" in names, f"A's component was lost: {names}"
    assert "bravo" in names, f"B's component was lost: {names}"


async def test_two_writers_editing_the_same_field_conflict(store_and_remote: Any) -> None:
    """A real collision must be reported, never silently resolved to one side."""
    store, remote = store_and_remote
    base = _committed(remote)

    a = copy.deepcopy(base)
    a["description"] = "beschrijving van A"

    b = copy.deepcopy(base)
    b["description"] = "beschrijving van B"

    await store.save(PROJECT_NAME, a, message="A", actor="A", enforce_validation=False, base=base)

    with pytest.raises(ConflictError, match="gewijzigd sinds"):
        await store.save(PROJECT_NAME, b, message="B", actor="B", enforce_validation=False, base=base)

    # A's change stands; B's was refused rather than written over it.
    assert _committed(remote)["description"] == "beschrijving van A"


async def test_no_conflict_when_nothing_changed_in_between(store_and_remote: Any) -> None:
    """The common case must stay on the fast path."""
    store, remote = store_and_remote
    base = _committed(remote)

    updated = copy.deepcopy(base)
    updated["description"] = "rustig aan"

    await store.save(PROJECT_NAME, updated, message="solo", actor="A", enforce_validation=False, base=base)
    assert _committed(remote)["description"] == "rustig aan"


async def test_gather_of_two_saves_never_loses_a_change(store_and_remote: Any) -> None:
    """The original measurement: one of the two changes used to vanish silently."""
    store, remote = store_and_remote
    base = _committed(remote)

    a = copy.deepcopy(base)
    a.setdefault("components", []).append({"name": "alpha", "type": "single"})
    b = copy.deepcopy(base)
    b["description"] = "van B"

    results = await asyncio.gather(
        store.save(PROJECT_NAME, a, message="add alpha", actor="A", enforce_validation=False, base=base),
        store.save(PROJECT_NAME, b, message="desc", actor="B", enforce_validation=False, base=base),
        return_exceptions=True,
    )

    landed = _committed(remote)
    names = [c["name"] for c in landed.get("components", [])]

    # Either both landed, or one was refused with a ConflictError. What must never
    # happen is a success for both with one of the changes missing.
    refused = [r for r in results if isinstance(r, ConflictError)]
    if not refused:
        assert "alpha" in names, f"A's change disappeared: {names}"
        assert landed["description"] == "van B", "B's change disappeared"
