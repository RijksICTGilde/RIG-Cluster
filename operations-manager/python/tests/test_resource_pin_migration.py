"""Task 4 (RC-518): the one-off migration recording resources that were set by hand
before RC-141 existed.

The core is a decision on git history: which commit last let a HUMAN change a component,
not the tuner or the OOM watcher. That decision has to be right before the dry-run could
ever write, so these tests exercise the building blocks apart from the git IO:
``is_platform_commit``, ``_last_hand_change``, ``find_pin_candidates`` and ``run``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from opi.tools.resource_pin_migration import (
    Snapshot,
    find_pin_candidates,
    is_platform_commit,
    run,
)


def _snapshot(ref: str, timestamp: str, message: str, data: dict[str, Any]) -> Snapshot:
    return Snapshot(ref=ref, timestamp=timestamp, message=message, data=data)


def _component_data(memory_limit: str, *, history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    resources: dict[str, Any] = {"requests": {"memory": "64Mi"}, "limits": {"memory": memory_limit}}
    if history is not None:
        resources["history"] = history
    return {
        "name": "sample-project",
        "components": [{"name": "api", "type": "single", "resources": resources}],
    }


# ---------------------------------------------------------------------------
# is_platform_commit
# ---------------------------------------------------------------------------


def test_an_auto_tune_commit_is_from_the_platform() -> None:
    newer = _snapshot("c2", "t2", "auto-tune: bump api to 256Mi", _component_data("256Mi"))
    older = _snapshot("c1", "t1", "initial import", _component_data("128Mi"))

    assert is_platform_commit(newer, older, "api") is True


def test_a_sanitize_commit_is_from_the_platform() -> None:
    newer = _snapshot("c2", "t2", "sanitize: clear oom disable", _component_data("256Mi"))
    older = _snapshot("c1", "t1", "irrelevant", _component_data("128Mi"))

    assert is_platform_commit(newer, older, "api") is True


def test_a_plain_commit_with_a_new_history_entry_is_also_from_the_platform() -> None:
    """The tuner once wrote onto the component itself (before RC-518); the migration
    recognises that trail through the history length, even without the message prefix."""
    history = [{"timestamp": "t2", "source": "auto-tune"}]
    newer = _snapshot("c2", "t2", "chore: regenerate manifests", _component_data("256Mi", history=history))
    older = _snapshot("c1", "t1", "irrelevant", _component_data("128Mi", history=[]))

    assert is_platform_commit(newer, older, "api") is True


def test_a_commit_made_by_a_human_is_not_a_platform_commit() -> None:
    newer = _snapshot("c2", "t2", "bump memory because of production incident", _component_data("512Mi"))
    older = _snapshot("c1", "t1", "initial import", _component_data("128Mi"))

    assert is_platform_commit(newer, older, "api") is False


# ---------------------------------------------------------------------------
# find_pin_candidates / _last_hand_change
# ---------------------------------------------------------------------------


def test_a_hand_raised_value_after_an_oom_is_found() -> None:
    """The scenario from the task: a human raised the memory by hand after an OOM kill,
    before RC-141 existed. The found commit carries the fields that changed there, at
    the value that stands TODAY."""
    snapshots = [
        _snapshot("c3", "2026-08-25T00:00:00+00:00", "chore: unrelated manifest bump", _component_data("512Mi")),
        _snapshot(
            "c2", "2026-07-10T00:00:00+00:00", "fix: bump memory after OOM in production", _component_data("512Mi")
        ),
        _snapshot("c1", "2026-06-01T00:00:00+00:00", "initial project import", _component_data("128Mi")),
    ]

    candidates = find_pin_candidates("sample-project", snapshots)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.component == "api"
    assert candidate.fields == ("limits_memory",)
    assert candidate.ref == "c2"
    assert candidate.message == "fix: bump memory after OOM in production"


def test_a_component_with_a_manual_entry_is_skipped() -> None:
    """It already says for itself what is pinned: the migration must not override that."""
    manual_history = [{"timestamp": "t", "source": "manual", "limits": {"memory": "512Mi"}}]
    snapshots = [
        _snapshot("c2", "t2", "fix: bump memory", _component_data("512Mi", history=manual_history)),
        _snapshot("c1", "t1", "initial import", _component_data("128Mi")),
    ]

    assert find_pin_candidates("sample-project", snapshots) == []


def test_a_component_born_inside_the_window_yields_no_candidate() -> None:
    """The oldest snapshot in which the component exists is its creation, not a wish."""
    data_without_component: dict[str, Any] = {"name": "sample-project", "components": []}
    snapshots = [
        _snapshot("c2", "t2", "fix: bump memory by hand", _component_data("512Mi")),
        _snapshot("c1", "t1", "add component api", data_without_component),
    ]

    assert find_pin_candidates("sample-project", snapshots) == []


def test_only_platform_commits_in_the_history_yields_no_candidate() -> None:
    """Every change came from the tuner: there is no moment where a human set anything."""
    snapshots = [
        _snapshot("c2", "t2", "auto-tune: bump api", _component_data("256Mi")),
        _snapshot("c1", "t1", "initial import", _component_data("128Mi")),
    ]

    assert find_pin_candidates("sample-project", snapshots) == []


def test_no_change_in_the_history_yields_no_candidate() -> None:
    snapshots = [
        _snapshot("c2", "t2", "chore: unrelated", _component_data("128Mi")),
        _snapshot("c1", "t1", "initial import", _component_data("128Mi")),
    ]

    assert find_pin_candidates("sample-project", snapshots) == []


def test_an_empty_snapshot_list_yields_no_candidate() -> None:
    assert find_pin_candidates("sample-project", []) == []


def test_an_older_platform_commit_under_the_hand_change_does_not_count() -> None:
    """The walk stops at the FIRST non-platform change, even with a platform change
    further down the history: only the latest hand-set state counts."""
    snapshots = [
        _snapshot("c3", "t3", "fix: bump by hand", _component_data("512Mi")),
        _snapshot("c2", "t2", "auto-tune: tune api", _component_data("256Mi")),
        _snapshot("c1", "t1", "initial import", _component_data("128Mi")),
    ]

    candidates = find_pin_candidates("sample-project", snapshots)

    assert len(candidates) == 1
    assert candidates[0].ref == "c3"


# ---------------------------------------------------------------------------
# run(): the dry-run only prints; --write pins and pushes
# ---------------------------------------------------------------------------


def _connector_with_a_project(snapshots: list[Snapshot]) -> MagicMock:
    connector = MagicMock()
    connector.ensure_repo_cloned = AsyncMock()
    connector.get_working_dir = AsyncMock(return_value="/tmp/no-repo")
    connector.read_file = AsyncMock(return_value="irrelevant")
    connector.create_or_update_file = AsyncMock()
    connector.commit_and_push = AsyncMock()
    connector.close = AsyncMock()
    connector.list_file_revisions = AsyncMock(
        return_value=[{"ref": s.ref, "timestamp": s.timestamp, "message": s.message} for s in snapshots]
    )

    async def _show_file_at(ref: str, _path: str) -> str:
        # Placeholder content, parsed back by the patched load_yaml_round_trip below.
        return next(s for s in snapshots if s.ref == ref).ref

    connector.show_file_at = AsyncMock(side_effect=_show_file_at)
    return connector


async def test_a_dry_run_writes_nothing() -> None:
    snapshots = [
        _snapshot("c2", "t2", "fix: bump by hand", _component_data("512Mi")),
        _snapshot("c1", "t1", "initial import", _component_data("128Mi")),
    ]
    connector = _connector_with_a_project(snapshots)
    by_ref = {s.ref: s.data for s in snapshots}

    with (
        patch(
            "opi.tools.resource_pin_migration.create_git_connector_for_project_files",
            AsyncMock(return_value=connector),
        ),
        patch("opi.tools.resource_pin_migration._project_filenames", AsyncMock(return_value=["sample-project.yaml"])),
        patch("opi.tools.resource_pin_migration.load_yaml_round_trip", side_effect=lambda ref: by_ref[ref]),
    ):
        result = await run(write=False, max_commits=200)

    assert result == 0
    connector.create_or_update_file.assert_not_called()
    connector.commit_and_push.assert_not_called()
    connector.close.assert_awaited_once()


async def test_write_pins_and_pushes_the_found_candidate() -> None:
    snapshots = [
        _snapshot("c2", "t2", "fix: bump by hand", _component_data("512Mi")),
        _snapshot("c1", "t1", "initial import", _component_data("128Mi")),
    ]
    connector = _connector_with_a_project(snapshots)
    by_ref = {s.ref: s.data for s in snapshots}
    # The read_file call (the file's current state) gets the newest snapshot.
    connector.read_file = AsyncMock(return_value="c2")

    with (
        patch(
            "opi.tools.resource_pin_migration.create_git_connector_for_project_files",
            AsyncMock(return_value=connector),
        ),
        patch("opi.tools.resource_pin_migration._project_filenames", AsyncMock(return_value=["sample-project.yaml"])),
        patch("opi.tools.resource_pin_migration.load_yaml_round_trip", side_effect=lambda ref: dict(by_ref[ref])),
        patch("opi.tools.resource_pin_migration.dump_yaml_to_string", return_value="irrelevant"),
    ):
        result = await run(write=True, max_commits=200)

    assert result == 0
    connector.create_or_update_file.assert_called_once()
    written_data = connector.create_or_update_file.call_args.args[1]
    assert written_data == "irrelevant"
    connector.commit_and_push.assert_awaited_once()
    assert "RC-518" in connector.commit_and_push.await_args.args[0]


async def test_write_without_candidates_pushes_nothing() -> None:
    snapshots = [
        _snapshot("c2", "t2", "auto-tune: tune api", _component_data("256Mi")),
        _snapshot("c1", "t1", "initial import", _component_data("128Mi")),
    ]
    connector = _connector_with_a_project(snapshots)
    by_ref = {s.ref: s.data for s in snapshots}

    with (
        patch(
            "opi.tools.resource_pin_migration.create_git_connector_for_project_files",
            AsyncMock(return_value=connector),
        ),
        patch("opi.tools.resource_pin_migration._project_filenames", AsyncMock(return_value=["sample-project.yaml"])),
        patch("opi.tools.resource_pin_migration.load_yaml_round_trip", side_effect=lambda ref: by_ref[ref]),
    ):
        result = await run(write=True, max_commits=200)

    assert result == 0
    connector.create_or_update_file.assert_not_called()
    connector.commit_and_push.assert_not_called()
