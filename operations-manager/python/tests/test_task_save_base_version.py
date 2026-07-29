"""A task that writes back a whole project file must not drop someone else's change.

The create_project task receives a finished project file, built from what the user
saw when the form was rendered. It does not read the file itself, so before this it
handed the store no compare-and-swap base and the write was last-writer-wins: a
change that landed while the user was editing -- another portal user, another
cluster, a direct git push -- was overwritten without a commit, a warning or a log
line to show for it.

The payload now carries ``base_version``, the ProjectStore token for the version the
form was rendered from. The handler resolves it back to that exact content and hands
it to the store as the base, so the submission is applied as a *change* relative to
it and the store merges the two. Only a real collision -- both sides changing the
same field to different values -- is reported, and then nothing is written.
"""

from __future__ import annotations

import copy
import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from opi.manager.project_manager import ProjectManager
from opi.services.project_service import get_project_service
from opi.services.project_store import GitProjectStore
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

    # Both the handler and ProjectManager reach for the process-wide store.
    monkeypatch.setattr("opi.services.project_store.get_project_store", lambda: store)
    monkeypatch.setattr("opi.manager.project_manager.get_project_store", lambda: store)

    # Everything after the git write is a different concern; the save is what is
    # under test here.
    async def _processed(self, *args: Any, **kwargs: Any) -> bool:
        return True

    async def _closed(self) -> None:
        return None

    async def _monitored(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(ProjectManager, "process_project_from_git", _processed)
    monkeypatch.setattr(ProjectManager, "close", _closed)
    monkeypatch.setattr("opi.core.simple_background._monitor_argocd_and_deployment", _monitored)
    monkeypatch.setattr("opi.core.config.settings.OOM_WATCHER_ENABLED", False)

    return store, remote


def _committed(remote: FakeRemote) -> dict[str, Any]:
    parsed = load_yaml_from_string(remote.files[RELATIVE_PATH])
    assert parsed is not None
    return parsed


def _external_edit(remote: FakeRemote, mutate) -> None:
    """Someone else writes the project file while our user has the form open."""
    current = _committed(remote)
    mutate(current)
    remote.commit("wijziging van iemand anders", {RELATIVE_PATH: dump_yaml_to_string(current)})


def _make_progress() -> MagicMock:
    progress = MagicMock()
    progress.add_task.side_effect = lambda name: f"task-{name}"
    progress.add_subtask.side_effect = lambda parent, name: f"subtask-{name}"
    return progress


async def _run_task(payload: dict[str, Any]) -> tuple[dict[str, Any], MagicMock]:
    from opi.core.task_handlers_project import handle_create_project

    progress = _make_progress()
    result = await handle_create_project(payload, progress)
    return result, progress


def _submission(base: dict[str, Any], version: str | None, **changes: Any) -> dict[str, Any]:
    """What the browser posts back: the whole file as the form last saw it."""
    data = copy.deepcopy(base)
    data.update(changes)
    payload: dict[str, Any] = {
        "project_name": PROJECT_NAME,
        "yaml_content": dump_yaml_to_string(data),
    }
    if version is not None:
        payload["base_version"] = version
    return payload


async def test_both_users_changes_survive(store_and_remote: Any) -> None:
    """The reported case: A and B edit different fields, B saves last.

    B's submission still carries A's old value for every field, because B's form was
    rendered before A saved. Publishing it as-is is exactly the silent lost update.
    """
    store, remote = store_and_remote

    version = await store.version_of(RELATIVE_PATH)
    assert version is not None
    as_rendered = await store.read_version(version)
    assert as_rendered is not None

    # User A saves first, straight into git.
    _external_edit(remote, lambda d: d.update({"description": "aangepast door A"}))

    # User B submits a form built before A's change existed.
    result, progress = await _run_task(_submission(as_rendered, version, **{"display-name": "aangepast door B"}))

    assert result["status"] == "success", result.get("error")
    landed = _committed(remote)
    assert landed["display-name"] == "aangepast door B", "B's change was not saved"
    assert landed["description"] == "aangepast door A", "A's change was silently overwritten"
    progress.fail_project.assert_not_called()


async def test_the_same_field_changed_twice_is_a_conflict(store_and_remote: Any) -> None:
    """Two different values for one field cannot be merged, so nothing is written."""
    store, remote = store_and_remote

    version = await store.version_of(RELATIVE_PATH)
    assert version is not None
    as_rendered = await store.read_version(version)
    assert as_rendered is not None

    _external_edit(remote, lambda d: d.update({"description": "aangepast door A"}))

    result, progress = await _run_task(_submission(as_rendered, version, description="aangepast door B"))

    assert result["status"] == "failed"
    assert "door iemand anders gewijzigd" in result["error"]
    assert "Herlaad de pagina" in result["error"]
    progress.fail_project.assert_called_once_with(result["error"])
    assert _committed(remote)["description"] == "aangepast door A", "the other change was overwritten anyway"


async def test_without_a_base_version_the_old_behaviour_is_kept(
    store_and_remote: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A caller that does not send a token still saves -- and says so in the log."""
    store, remote = store_and_remote

    version = await store.version_of(RELATIVE_PATH)
    assert version is not None
    as_rendered = await store.read_version(version)
    assert as_rendered is not None

    with caplog.at_level(logging.WARNING, logger="opi.core.task_handlers_project"):
        result, _progress = await _run_task(_submission(as_rendered, None, **{"display-name": "zonder token"}))

    assert result["status"] == "success", result.get("error")
    assert _committed(remote)["display-name"] == "zonder token"
    assert any("No base_version" in record.message for record in caplog.records), (
        "a call site that is not wired up must be visible in the log"
    )


async def test_re_saving_what_was_just_committed_is_not_a_conflict(store_and_remote: Any) -> None:
    """The modal edit saves the file itself and then hands the same content to the task.

    The second write therefore re-applies a change that is already committed. That is
    not a concurrent edit and must not be reported as one.
    """
    store, remote = store_and_remote

    version = await store.version_of(RELATIVE_PATH)
    assert version is not None
    as_rendered = await store.read_version(version)
    assert as_rendered is not None

    edited = copy.deepcopy(as_rendered)
    edited["display-name"] = "via de modal bewerkt"

    # The request handler's own save (base = what it read a moment earlier).
    await store.save(PROJECT_NAME, edited, message="modal edit", actor="tester", base=as_rendered)

    # The deployment task then writes the same content back, against the version the
    # wizard was rendered from.
    result, progress = await _run_task(_submission(edited, version))

    assert result["status"] == "success", result.get("error")
    assert _committed(remote)["display-name"] == "via de modal bewerkt"
    progress.fail_project.assert_not_called()


async def test_third_write_between_the_two_modal_saves_is_not_a_spurious_conflict(store_and_remote: Any) -> None:
    """WP4 #1 guard: the modal edit saves in-request, then the deploy task re-saves the
    SAME content against the render snapshot. Even when a third write lands *between*
    those two saves, the task's re-save must not raise a conflict for a change it already
    committed -- the three-way merge folds its own already-applied delta cleanly.

    Interleaving: save #1 commits the modal edit; user C commits a change to a different
    field on top of it; the deploy task's redundant re-save (base = the render snapshot,
    which predates both) runs last. Verified: C's change and the modal edit both survive,
    no conflict. (This is the scenario audited as a possible spurious-conflict risk; the
    store already handles it, so no code change was needed -- this pins that.)
    """
    store, remote = store_and_remote

    version = await store.version_of(RELATIVE_PATH)
    assert version is not None
    as_rendered = await store.read_version(version)
    assert as_rendered is not None

    edited = copy.deepcopy(as_rendered)
    edited["display-name"] = "via de modal bewerkt"

    # Save #1: the request handler commits the modal edit (base = what it read).
    await store.save(PROJECT_NAME, edited, message="modal edit", actor="tester", base=as_rendered)

    # A third writer changes a DIFFERENT field on top of the just-committed edit.
    _external_edit(remote, lambda d: d.update({"description": "aangepast door C"}))

    # Save #2: the deploy task re-saves the modal content against the render snapshot.
    result, progress = await _run_task(_submission(edited, version))

    assert result["status"] == "success", result.get("error")
    landed = _committed(remote)
    assert landed["display-name"] == "via de modal bewerkt", "the modal edit was lost"
    assert landed["description"] == "aangepast door C", "the third writer's change was overwritten"
    progress.fail_project.assert_not_called()
