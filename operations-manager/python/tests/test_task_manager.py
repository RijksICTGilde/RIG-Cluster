"""
Tests for task_manager: monitoring deduplication, cleanup, completed_at tracking,
close() idempotency, and integration-level monitoring storm prevention.
"""

import asyncio
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opi.core.task_manager import (
    Task,
    TaskProgressManager,
    TaskStatus,
    _active_app_monitoring_tasks,
    _active_monitoring_tasks,
    _cleanup_completed_projects,
    _project_managers,
    _projects,
    complete_task,
    create_task,
    fail_task,
    get_task,
    start_periodic_cleanup,
    stop_periodic_cleanup,
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
    """Verify completed_at is set as datetime in all terminal paths."""

    def test_complete_task_sets_completed_at(self):
        task_id = create_task("test-project")
        project = get_task(task_id)
        assert project is not None
        assert project.completed_at is None
        complete_task(task_id, {"status": "ok"})
        project = get_task(task_id)
        assert project is not None
        assert project.completed_at is not None
        assert isinstance(project.completed_at, datetime)

    def test_fail_task_sets_completed_at(self):
        task_id = create_task("test-project")
        fail_task(task_id, "something broke")
        project = get_task(task_id)
        assert project is not None
        assert project.completed_at is not None
        assert isinstance(project.completed_at, datetime)

    def test_progress_manager_complete_project_sets_completed_at(self):
        task_id = create_task("test-project")
        pm = TaskProgressManager(task_id, "test-project")
        pm.complete_project()
        project = get_task(task_id)
        assert project is not None
        assert project.completed_at is not None
        assert isinstance(project.completed_at, datetime)

    def test_progress_manager_fail_project_sets_completed_at(self):
        task_id = create_task("test-project")
        pm = TaskProgressManager(task_id, "test-project")
        pm.fail_project("error")
        project = get_task(task_id)
        assert project is not None
        assert project.completed_at is not None
        assert isinstance(project.completed_at, datetime)


class TestMonitoringDeduplication:
    """Verify monitoring deduplication prevents duplicate tasks."""

    @pytest.mark.asyncio
    @patch("opi.core.task_manager._monitor_project_progress", new_callable=AsyncMock)
    async def test_start_monitoring_via_progress_manager(self, mock_monitor):
        """Test that TaskProgressManager.start_monitoring() creates a tracked task."""
        mock_monitor.return_value = None
        task_id = create_task("test-project")
        pm = TaskProgressManager(task_id, "test-project")
        pm.set_namespace("test-ns")
        # set_namespace calls start_monitoring internally
        assert task_id in _active_monitoring_tasks
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    @patch("opi.core.task_manager._monitor_project_progress", new_callable=AsyncMock)
    async def test_multiple_set_namespace_calls_only_one_task(self, mock_monitor):
        """Calling set_namespace multiple times should not create multiple tasks."""
        mock_monitor.return_value = None
        task_id = create_task("test-project")
        pm = TaskProgressManager(task_id, "test-project")
        pm.set_namespace("test-ns")
        first_task = _active_monitoring_tasks[task_id]

        pm.set_namespace("test-ns-2")
        second_task = _active_monitoring_tasks[task_id]

        # Should be the same task object -- not replaced
        assert first_task is second_task
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    @patch("opi.core.task_manager._monitor_project_progress", new_callable=AsyncMock)
    async def test_replaces_done_monitoring_task(self, mock_monitor):
        """After a monitoring task completes, a new one can be started."""
        mock_monitor.return_value = None
        task_id = create_task("test-project")
        pm = TaskProgressManager(task_id, "test-project")
        pm.set_namespace("test-ns")
        first_task = _active_monitoring_tasks[task_id]

        # Let the first task complete
        await asyncio.sleep(0.01)
        assert first_task.done()

        # Calling start_monitoring again should create a new task
        pm.start_monitoring()
        second_task = _active_monitoring_tasks[task_id]
        assert first_task is not second_task
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    @patch(
        "opi.core.task_manager._monitor_project_applications_continuously",
        new_callable=AsyncMock,
    )
    async def test_app_monitoring_deduplicates(self, mock_monitor):
        """Application monitoring also deduplicates per project."""
        from opi.core.task_manager import _start_app_monitoring_if_not_active

        mock_monitor.return_value = None
        task_id = create_task("test-project")
        _projects[task_id].namespace = "test-ns"

        _start_app_monitoring_if_not_active(task_id, "test-project", ["app1"])
        first_task = _active_app_monitoring_tasks[task_id]

        _start_app_monitoring_if_not_active(task_id, "test-project", ["app1"])
        second_task = _active_app_monitoring_tasks[task_id]

        assert first_task is second_task
        await asyncio.sleep(0)


class TestMonitoringStormPrevention:
    """Integration test: simulate the actual bug scenario where multiple callers
    trigger monitoring and verify only one monitoring task runs at a time."""

    @pytest.mark.asyncio
    @patch("opi.core.task_manager._monitor_project_progress", new_callable=AsyncMock)
    async def test_multiple_callers_produce_single_monitoring_task(self, mock_monitor):
        """Simulate the storm: set_namespace, start_monitoring, and start_task_monitoring
        all try to start monitoring concurrently. Only one task should exist."""
        from opi.core.task_manager import start_task_monitoring

        mock_monitor.return_value = None
        task_id = create_task("test-project")
        pm = TaskProgressManager(task_id, "test-project")

        # Caller 1: set_namespace (which calls start_monitoring internally)
        pm.set_namespace("test-ns")
        first_task = _active_monitoring_tasks.get(task_id)
        assert first_task is not None

        # Caller 2: explicit start_monitoring
        pm.start_monitoring()
        assert _active_monitoring_tasks[task_id] is first_task

        # Caller 3: start_task_monitoring (used from process_project_background)
        await start_task_monitoring(task_id)
        assert _active_monitoring_tasks[task_id] is first_task

        # Only one task should have been created total
        assert mock_monitor.call_count == 1
        await asyncio.sleep(0)


class TestCleanupCompletedProjects:
    """Verify cleanup removes stale projects and cancels tasks."""

    def _make_old_completed_project(self) -> str:
        """Helper: create a project completed 1 hour ago."""
        task_id = create_task("old-project")
        _projects[task_id].status = TaskStatus.COMPLETED
        _projects[task_id].completed_at = datetime.now() - timedelta(hours=1)
        return task_id

    def test_cleanup_removes_old_completed_projects(self):
        task_id = self._make_old_completed_project()
        _cleanup_completed_projects()
        assert task_id not in _projects

    def test_cleanup_removes_old_failed_projects(self):
        task_id = create_task("failed-project")
        _projects[task_id].status = TaskStatus.FAILED
        _projects[task_id].completed_at = datetime.now() - timedelta(hours=1)
        _cleanup_completed_projects()
        assert task_id not in _projects

    def test_cleanup_keeps_recent_completed_projects(self):
        task_id = create_task("recent-project")
        _projects[task_id].status = TaskStatus.COMPLETED
        _projects[task_id].completed_at = datetime.now() - timedelta(minutes=1)
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
        assert _projects[task_id].completed_at is None
        _cleanup_completed_projects()
        assert task_id in _projects

    def test_cleanup_cancels_monitoring_tasks(self):
        task_id = self._make_old_completed_project()

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
        task_id = self._make_old_completed_project()

        mock_task = MagicMock(spec=asyncio.Task)
        mock_task.done.return_value = True
        _active_monitoring_tasks[task_id] = mock_task

        _cleanup_completed_projects()
        mock_task.cancel.assert_not_called()

    def test_cleanup_also_removes_project_managers(self):
        task_id = self._make_old_completed_project()
        _project_managers[task_id] = MagicMock()
        _cleanup_completed_projects()
        assert task_id not in _project_managers

    def test_create_task_triggers_cleanup(self):
        old_id = self._make_old_completed_project()
        new_id = create_task("new-project")
        assert old_id not in _projects
        assert new_id in _projects

    def test_get_task_does_not_trigger_cleanup(self):
        old_id = self._make_old_completed_project()
        # get_task should NOT trigger cleanup (only create_task and periodic loop do)
        get_task("nonexistent")
        assert old_id in _projects

    def test_cleanup_removes_stuck_running_projects(self):
        task_id = create_task("stuck-project")
        _projects[task_id].status = TaskStatus.RUNNING
        _projects[task_id].created_at = datetime.now() - timedelta(hours=3)
        _cleanup_completed_projects()
        assert task_id not in _projects

    def test_cleanup_keeps_recent_running_projects(self):
        task_id = create_task("active-project")
        _projects[task_id].status = TaskStatus.RUNNING
        _projects[task_id].created_at = datetime.now() - timedelta(minutes=30)
        _cleanup_completed_projects()
        assert task_id in _projects


class TestPeriodicCleanup:
    """Verify the periodic cleanup task lifecycle."""

    @pytest.mark.asyncio
    async def test_start_and_stop_periodic_cleanup(self):
        from opi.core.task_manager import _cleanup_task

        start_periodic_cleanup()
        from opi.core import task_manager

        assert task_manager._cleanup_task is not None
        assert not task_manager._cleanup_task.done()

        stop_periodic_cleanup()
        assert task_manager._cleanup_task is None

    @pytest.mark.asyncio
    async def test_start_periodic_cleanup_idempotent(self):
        start_periodic_cleanup()
        from opi.core import task_manager

        first = task_manager._cleanup_task

        start_periodic_cleanup()
        second = task_manager._cleanup_task
        assert first is second

        stop_periodic_cleanup()


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
