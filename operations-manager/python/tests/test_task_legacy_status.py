"""Tests for legacy in-memory task status sync.

Successful async tasks previously never updated the legacy ``_projects`` dict
(only ``fail_project`` and the backup handler did), so every successful task
resurfaced two hours later as a scary "stuck RUNNING" warning from the
periodic cleanup. The worker now calls ``mark_legacy_completed`` /
``mark_legacy_failed`` on the progress manager in its terminal paths.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from opi.core.persistent_task_progress import PersistentTaskProgressManager
from opi.core.task_manager import (
    TaskStatus,
    _cleanup_completed_projects,
    _projects,
)


@pytest.fixture(autouse=True)
def clean_projects():
    """Isolate the global legacy _projects dict per test."""
    _projects.clear()
    yield
    _projects.clear()


def _make_progress(task_id: str = "task-1") -> PersistentTaskProgressManager:
    return PersistentTaskProgressManager(
        task_id=task_id,
        project_name="demo",
        task_service=AsyncMock(),
    )


class TestMarkLegacyStatus:
    async def test_init_registers_running_entry(self):
        _make_progress()
        assert _projects["task-1"].status == TaskStatus.RUNNING

    async def test_mark_legacy_completed(self):
        progress = _make_progress()
        progress.mark_legacy_completed()

        assert _projects["task-1"].status == TaskStatus.COMPLETED
        assert _projects["task-1"].completed_at is not None

    async def test_mark_legacy_failed(self):
        progress = _make_progress()
        progress.mark_legacy_failed("boom")

        assert _projects["task-1"].status == TaskStatus.FAILED
        assert _projects["task-1"].completed_at is not None
        assert _projects["task-1"].error == "boom"

    async def test_mark_legacy_completed_without_entry_is_noop(self):
        progress = _make_progress()
        _projects.clear()
        progress.mark_legacy_completed()  # must not raise


class TestStuckSweep:
    async def test_completed_old_task_is_not_reported_stuck(self, caplog):
        """A task completed long after creation must not hit the stuck-RUNNING path."""
        progress = _make_progress()
        _projects["task-1"].created_at = datetime.now(tz=UTC) - timedelta(hours=3)
        progress.mark_legacy_completed()

        with caplog.at_level("WARNING"):
            _cleanup_completed_projects()

        assert "stuck RUNNING" not in caplog.text
        # Recently completed: stays for the 30-minute progress-page window
        assert "task-1" in _projects

    async def test_running_old_task_is_swept_with_project_name(self, caplog):
        _make_progress()
        _projects["task-1"].created_at = datetime.now(tz=UTC) - timedelta(hours=3)

        with caplog.at_level("WARNING"):
            _cleanup_completed_projects()

        assert "stuck RUNNING" in caplog.text
        assert "'demo'" in caplog.text
        assert "task-1" not in _projects
