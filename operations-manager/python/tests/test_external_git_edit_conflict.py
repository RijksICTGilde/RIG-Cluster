"""An edit that lands on the REMOTE must not be clobbered either.

The compare-and-swap in save() protects against writers inside this process: it
compares the caller's base against the local warm copy's HEAD. A change pushed
straight to git -- by a person, or by another cluster -- is not in the warm copy
yet, so that check passes and the push is what discovers the divergence.

At that point the store resets to the remote and retries. The question these
tests pin down: does the retry re-apply our change on top of what it just
fetched, or does it republish the original dict over it?
"""

from __future__ import annotations

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
    return store, remote, connector


def _committed(remote: FakeRemote) -> dict[str, Any]:
    parsed = load_yaml_from_string(remote.files[RELATIVE_PATH])
    assert parsed is not None
    return parsed


def _external_edit(remote: FakeRemote, mutate) -> None:
    """Someone commits straight to git, bypassing ZAD entirely."""
    current = _committed(remote)
    mutate(current)
    remote.commit("handmatige bewerking in git", {RELATIVE_PATH: dump_yaml_to_string(current)})


async def test_external_git_edit_survives_a_zad_write(store_and_remote: Any) -> None:
    """The scenario: someone edits in git while a ZAD user has the project open."""
    store, remote, connector = store_and_remote
    base = _committed(remote)

    ours = copy.deepcopy(base)
    ours.setdefault("components", []).append({"name": "vanzad", "type": "single"})

    # Lands on the remote after we read, and before we push. The warm copy knows
    # nothing about it, so only the push can discover it.
    _external_edit(
        remote,
        lambda d: d.setdefault("deployments", []).append(
            {"name": "vangit", "cluster": "odcn-production", "namespace": "demo", "components": []}
        ),
    )

    await store.save(PROJECT_NAME, ours, message="add vanzad", actor="zad", enforce_validation=False, base=base)

    landed = _committed(remote)
    assert [c["name"] for c in landed.get("components", [])] == ["vanzad"], "our change was lost"
    assert [d["name"] for d in landed.get("deployments", [])] == ["vangit"], (
        "the edit made directly in git was clobbered by ZAD"
    )


async def test_writing_one_project_picks_up_another_changed_in_git(store_and_remote: Any) -> None:
    """Committing project B must not leave project A stale in the cache.

    Resolving the rejected push fetches the whole repo, so A's new content is on
    disk anyway. Without reloading it, the cache would keep serving the old A until
    someone triggered a reconcile -- which, now that the 30-second poll is gone,
    might be never.
    """
    store, remote, connector = store_and_remote
    await store.bootstrap()

    # Another project, edited only in git.
    other = _project()
    other["name"] = "ander"
    other["display-name"] = "in git gewijzigd"
    files = dict(remote.files)
    files["projects/ander.yaml"] = dump_yaml_to_string(other)
    remote.commit("handmatig: nieuw project ander", files)

    # Now write our own project. Its push is rejected (the remote moved), which is
    # what pulls the other project in.
    base = _committed(remote)
    ours = copy.deepcopy(base)
    ours["description"] = "gewijzigd via ZAD"
    await store.save(PROJECT_NAME, ours, message="desc", actor="zad", enforce_validation=False, base=base)

    cached = store.get("ander")
    assert cached is not None, "the project changed in git never reached the cache"
    assert cached.data is not None
    assert cached.data["display-name"] == "in git gewijzigd"


async def test_external_edit_to_the_same_field_is_reported(store_and_remote: Any) -> None:
    """A genuine collision with an external edit must surface, not be overwritten."""
    store, remote, connector = store_and_remote
    base = _committed(remote)

    ours = copy.deepcopy(base)
    ours["description"] = "gewijzigd via ZAD"

    _external_edit(remote, lambda d: d.update({"description": "gewijzigd in git"}))

    with pytest.raises(ConflictError):
        await store.save(PROJECT_NAME, ours, message="desc", actor="zad", enforce_validation=False, base=base)

    assert _committed(remote)["description"] == "gewijzigd in git", "the external edit was overwritten"
