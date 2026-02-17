"""
Tests for task_manager: monitoring deduplication, cleanup, completed_at tracking,
and close() idempotency on GitConnector and ProjectManager.
"""

import asyncio
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opi.core.task_manager import (
    ProjectInfo,
    Task,
    TaskProgressManager,
    TaskStatus,
    _active_app_monitoring_tasks,
    _active_monitoring_tasks,
    _cleanup_completed_projects,
    _project_managers,
    _projects,
    _start_app_monitoring_if_not_active,
    _start_monitoring_if_not_active,
    complete_task,
    create_task,
    fail_task,
)


@pytest.fixture(autouse=True)
def _clean_global_state():
    """Reset all module-level state between tests."""
    _projects.clear()
    _project_managers.clear()
    _active_monitoring_tasks.clear()
    _active_app_monitoring_tasks.clear()
    yield
    _projects.clear()
    _project_managers.clear()
    # Cancel any leftover tasks to avoid warnings
    for task in _active_monitoring_tasks.values():
        if not task.done():
            task.cancel()
    for task in _active_app_monitoring_tasks.values():
        if not task.done():
            task.cancel()
    _active_monitoring_tasks.clear()
    _active_app_monitoring_tasks.clear()


class TestTaskCreatedAtDefault:
    """Verify Task.created_at uses field(default_factory=...) not a shared default."""

    def test_separate_instances_get_different_timestamps(self):
        task1 = Task(id="1", name="first")
        # Tiny sleep to ensure time difference
        time.sleep(0.01)
        task2 = Task(id="2", name="second")
        # They should not be the exact same object
        assert task1.created_at is not task2.created_at


class TestCompletedAtTracking:
    """Verify completed_at is set in all terminal paths."""

    def test_complete_task_sets_completed_at(self):
        task_id = create_task("test-project")
        assert _projects[task_id].completed_at is None
        complete_task(task_id, {"status": "ok"})
        assert _projects[task_id].completed_at is not None
        assert isinstance(_projects[task_id].completed_at, float)

    def test_fail_task_sets_completed_at(self):
        task_id = create_task("test-project")
        assert _projects[task_id].completed_at is None
        fail_task(task_id, "something broke")
        assert _projects[task_id].completed_at is not None
        assert isinstance(_projects[task_id].completed_at, float)

    def test_progress_manager_complete_project_sets_completed_at(self):
        task_id = create_task("test-project")
        pm = TaskProgressManager(task_id, "test-project")
        pm.complete_project()
        assert _projects[task_id].completed_at is not None

    def test_progress_manager_fail_project_sets_completed_at(self):
        task_id = create_task("test-project")
        pm = TaskProgressManager(task_id, "test-project")
        pm.fail_project("error")
        assert _projects[task_id].completed_at is not None


class TestMonitoringDeduplication:
    """Verify _start_monitoring_if_not_active prevents duplicate monitoring tasks."""

    @pytest.mark.asyncio
    @patch("opi.core.task_manager._monitor_project_progress", new_callable=AsyncMock)
    async def test_start_monitoring_creates_task(self, mock_monitor):
        mock_monitor.return_value = None
        task_id = create_task("test-project")
        _projects[task_id].namespace = "test-ns"

        _start_monitoring_if_not_active(task_id)
        assert task_id in _active_monitoring_tasks
        # Allow the task to start
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    @patch("opi.core.task_manager._monitor_project_progress", new_callable=AsyncMock)
    async def test_start_monitoring_deduplicates(self, mock_monitor):
        mock_monitor.return_value = None
        task_id = create_task("test-project")
        _projects[task_id].namespace = "test-ns"

        _start_monitoring_if_not_active(task_id)
        first_task = _active_monitoring_tasks[task_id]

        _start_monitoring_if_not_active(task_id)
        second_task = _active_monitoring_tasks[task_id]

        # Should be the same task object -- not replaced
        assert first_task is second_task
        # mock should only have been wrapped in create_task once
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    @patch("opi.core.task_manager._monitor_project_progress", new_callable=AsyncMock)
    async def test_start_monitoring_replaces_done_task(self, mock_monitor):
        mock_monitor.return_value = None
        task_id = create_task("test-project")
        _projects[task_id].namespace = "test-ns"

        _start_monitoring_if_not_active(task_id)
        first_task = _active_monitoring_tasks[task_id]

        # Let the first task complete
        await asyncio.sleep(0.01)
        assert first_task.done()

        # Now a new one should be created
        _start_monitoring_if_not_active(task_id)
        second_task = _active_monitoring_tasks[task_id]
        assert first_task is not second_task
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    @patch(
        "opi.core.task_manager._monitor_project_applications_continuously",
        new_callable=AsyncMock,
    )
    async def test_start_app_monitoring_deduplicates(self, mock_monitor):
        mock_monitor.return_value = None
        task_id = create_task("test-project")
        _projects[task_id].namespace = "test-ns"

        _start_app_monitoring_if_not_active(task_id, "test-project", ["app1"])
        first_task = _active_app_monitoring_tasks[task_id]

        _start_app_monitoring_if_not_active(task_id, "test-project", ["app1"])
        second_task = _active_app_monitoring_tasks[task_id]

        assert first_task is second_task
        await asyncio.sleep(0)


class TestCleanupCompletedProjects:
    """Verify _cleanup_completed_projects removes stale projects and cancels tasks."""

    def test_cleanup_removes_old_completed_projects(self):
        task_id = create_task("old-project")
        _projects[task_id].status = TaskStatus.COMPLETED
        _projects[task_id].completed_at = time.time() - 3600  # 1 hour ago

        _cleanup_completed_projects()
        assert task_id not in _projects

    def test_cleanup_removes_old_failed_projects(self):
        task_id = create_task("failed-project")
        _projects[task_id].status = TaskStatus.FAILED
        _projects[task_id].completed_at = time.time() - 3600

        _cleanup_completed_projects()
        assert task_id not in _projects

    def test_cleanup_keeps_recent_completed_projects(self):
        task_id = create_task("recent-project")
        _projects[task_id].status = TaskStatus.COMPLETED
        _projects[task_id].completed_at = time.time() - 60  # 1 minute ago

        _cleanup_completed_projects()
        assert task_id in _projects

    def test_cleanup_keeps_running_projects(self):
        task_id = create_task("running-project")
        _projects[task_id].status = TaskStatus.RUNNING

        _cleanup_completed_projects()
        assert task_id in _projects

    def test_cleanup_keeps_completed_without_timestamp(self):
        task_id = create_task("no-timestamp-project")
        _projects[task_id].status = TaskStatus.COMPLETED
        # completed_at is None -- should not be cleaned up
        assert _projects[task_id].completed_at is None

        _cleanup_completed_projects()
        assert task_id in _projects

    def test_cleanup_cancels_monitoring_tasks(self):
        task_id = create_task("old-project")
        _projects[task_id].status = TaskStatus.COMPLETED
        _projects[task_id].completed_at = time.time() - 3600

        # Simulate active monitoring tasks
        mock_task = MagicMock(spec=asyncio.Task)
        mock_task.done.return_value = False
        _active_monitoring_tasks[task_id] = mock_task

        mock_app_task = MagicMock(spec=asyncio.Task)
        mock_app_task.done.return_value = False
        _active_app_monitoring_tasks[task_id] = mock_app_task

        _cleanup_completed_projects()

        mock_task.cancel.assert_called_once()
        mock_app_task.cancel.assert_called_once()
        assert task_id not in _active_monitoring_tasks
        assert task_id not in _active_app_monitoring_tasks

    def test_cleanup_skips_cancel_on_already_done_tasks(self):
        task_id = create_task("old-project")
        _projects[task_id].status = TaskStatus.COMPLETED
        _projects[task_id].completed_at = time.time() - 3600

        mock_task = MagicMock(spec=asyncio.Task)
        mock_task.done.return_value = True
        _active_monitoring_tasks[task_id] = mock_task

        _cleanup_completed_projects()

        mock_task.cancel.assert_not_called()

    def test_cleanup_also_removes_project_managers(self):
        task_id = create_task("old-project")
        _project_managers[task_id] = MagicMock()
        _projects[task_id].status = TaskStatus.COMPLETED
        _projects[task_id].completed_at = time.time() - 3600

        _cleanup_completed_projects()
        assert task_id not in _project_managers

    def test_create_task_triggers_cleanup(self):
        old_id = create_task("old-project")
        _projects[old_id].status = TaskStatus.COMPLETED
        _projects[old_id].completed_at = time.time() - 3600

        # Creating a new task should trigger cleanup of the old one
        new_id = create_task("new-project")
        assert old_id not in _projects
        assert new_id in _projects


class TestGitConnectorCloseIdempotency:
    """Verify GitConnector.close() is safe to call multiple times."""

    @pytest.mark.asyncio
    async def test_close_twice_only_cleans_once(self):
        with (
            patch("opi.connectors.git.GitConnector._parse_git_url") as mock_parse,
            patch("tempfile.mkdtemp", return_value="/tmp/fake-dir"),
            patch("os.path.exists", return_value=True),
            patch("shutil.rmtree") as mock_rmtree,
            patch("opi.core.config.settings") as mock_settings,
        ):
            mock_settings.TEMP_DIR = "/tmp"
            mock_parse.return_value = MagicMock()

            from opi.connectors.git import GitConnector

            connector = GitConnector.__new__(GitConnector)
            connector.repo_url = "https://example.com/repo.git"
            connector.name = "test-repo"
            connector.project_name = "test"
            connector._GitConnector__working_dir = "/tmp/fake-dir"
            connector._repo_cloned = False
            connector._fetched_in_session = False
            connector._closed = False
            connector._git_user_configured = False
            connector.should_cleanup = True

            with patch.object(connector, "has_changes", new_callable=AsyncMock, return_value=False):
                await connector.close()
                assert connector._closed is True
                mock_rmtree.assert_called_once()

                mock_rmtree.reset_mock()
                await connector.close()
                # Second call should not attempt cleanup
                mock_rmtree.assert_not_called()


class TestProjectManagerCloseIdempotency:
    """Verify ProjectManager.close() is safe to call multiple times."""

    @pytest.mark.asyncio
    async def test_close_twice_only_cleans_once(self):
        with patch("opi.manager.project_manager.ProjectManager.__init__", return_value=None):
            from opi.manager.project_manager import ProjectManager

            pm = ProjectManager.__new__(ProjectManager)
            pm._closed = False
            pm.close_git_connector_for_project_files = AsyncMock()
            pm.close_git_connector_for_argocd = AsyncMock()
            pm.close_git_connectors_for_deployments = AsyncMock()
            pm._database_manager = None

            await pm.close()
            assert pm._closed is True
            pm.close_git_connector_for_project_files.assert_awaited_once()
            pm.close_git_connector_for_argocd.assert_awaited_once()
            pm.close_git_connectors_for_deployments.assert_awaited_once()

            # Reset mocks and call again
            pm.close_git_connector_for_project_files.reset_mock()
            pm.close_git_connector_for_argocd.reset_mock()
            pm.close_git_connectors_for_deployments.reset_mock()

            await pm.close()
            # Second call should not invoke any cleanup
            pm.close_git_connector_for_project_files.assert_not_awaited()
            pm.close_git_connector_for_argocd.assert_not_awaited()
            pm.close_git_connectors_for_deployments.assert_not_awaited()
