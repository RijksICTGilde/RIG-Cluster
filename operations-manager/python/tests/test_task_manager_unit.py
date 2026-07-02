"""Unit tests for opi.core.task_manager module-level functions and TaskProgressManager class."""

import pytest


@pytest.fixture(autouse=True)
def clean_task_state():
    """Clean up task manager state between tests."""
    from opi.core import task_manager

    task_manager._projects.clear()
    task_manager._project_managers.clear()
    yield
    task_manager._projects.clear()
    task_manager._project_managers.clear()


# ---------------------------------------------------------------------------
# Module-level function tests
# ---------------------------------------------------------------------------


class TestCreateTask:
    """Tests for the create_task module-level function."""

    def test_create_task_returns_string_id(self):
        from opi.core.task_manager import create_task

        project_id = create_task("my-project")
        assert isinstance(project_id, str)
        assert len(project_id) > 0

    def test_create_task_stores_project_info(self):
        from opi.core.task_manager import TaskStatus, _projects, create_task

        project_id = create_task("my-project")
        assert project_id in _projects
        info = _projects[project_id]
        assert info.project_name == "my-project"
        assert info.status == TaskStatus.RUNNING
        assert info.id == project_id

    def test_create_task_unique_ids(self):
        from opi.core.task_manager import create_task

        id1 = create_task("project-a")
        id2 = create_task("project-b")
        assert id1 != id2

    def test_create_task_default_current_step(self):
        from opi.core.task_manager import _projects, create_task

        project_id = create_task("my-project")
        assert _projects[project_id].current_step == "Starting..."


class TestGetTask:
    """Tests for the get_task module-level function."""

    def test_get_existing_task(self):
        from opi.core.task_manager import create_task, get_task

        project_id = create_task("my-project")
        result = get_task(project_id)
        assert result is not None
        assert result.project_name == "my-project"

    def test_get_nonexistent_task_returns_none(self):
        from opi.core.task_manager import get_task

        result = get_task("nonexistent-id")
        assert result is None


class TestUpdateProgress:
    """Tests for the update_progress module-level function."""

    def test_update_progress_changes_current_step(self):
        from opi.core.task_manager import _projects, create_task, update_progress

        project_id = create_task("my-project")
        update_progress(project_id, 50, "Halfway done")
        assert _projects[project_id].current_step == "Halfway done"

    def test_update_progress_nonexistent_task_no_error(self):
        from opi.core.task_manager import update_progress

        # Should not raise
        update_progress("nonexistent-id", 50, "Step")

    def test_update_progress_overwrites_previous_step(self):
        from opi.core.task_manager import _projects, create_task, update_progress

        project_id = create_task("my-project")
        update_progress(project_id, 25, "Step 1")
        update_progress(project_id, 75, "Step 2")
        assert _projects[project_id].current_step == "Step 2"


class TestCompleteTask:
    """Tests for the complete_task module-level function."""

    def test_complete_task_sets_status(self):
        from opi.core.task_manager import TaskStatus, _projects, complete_task, create_task

        project_id = create_task("my-project")
        complete_task(project_id, {"key": "value"})
        assert _projects[project_id].status == TaskStatus.COMPLETED

    def test_complete_task_sets_current_step_to_completed(self):
        from opi.core.task_manager import _projects, complete_task, create_task

        project_id = create_task("my-project")
        complete_task(project_id, {})
        assert _projects[project_id].current_step == "Completed"

    def test_complete_task_nonexistent_no_error(self):
        from opi.core.task_manager import complete_task

        # Should not raise
        complete_task("nonexistent-id", {"result": "data"})


class TestFailTask:
    """Tests for the fail_task module-level function."""

    def test_fail_task_sets_status(self):
        from opi.core.task_manager import TaskStatus, _projects, create_task, fail_task

        project_id = create_task("my-project")
        fail_task(project_id, "Something went wrong")
        assert _projects[project_id].status == TaskStatus.FAILED

    def test_fail_task_sets_current_step_to_failed(self):
        from opi.core.task_manager import _projects, create_task, fail_task

        project_id = create_task("my-project")
        fail_task(project_id, "Error occurred")
        assert _projects[project_id].current_step == "Failed"

    def test_fail_task_nonexistent_no_error(self):
        from opi.core.task_manager import fail_task

        # Should not raise
        fail_task("nonexistent-id", "error")


class TestAddSubtask:
    """Tests for the add_subtask module-level (legacy) function."""

    def test_add_subtask_returns_subtask_id(self):
        from opi.core.task_manager import add_subtask, create_task

        project_id = create_task("my-project")
        subtask_id = add_subtask(project_id, "deploy")
        assert isinstance(subtask_id, str)
        assert project_id in subtask_id
        assert "deploy" in subtask_id

    def test_add_subtask_format(self):
        from opi.core.task_manager import add_subtask

        subtask_id = add_subtask("task-123", "build")
        assert subtask_id == "task-123-build"


# ---------------------------------------------------------------------------
# TaskProgressManager class tests
# ---------------------------------------------------------------------------


class TestTaskProgressManager:
    """Tests for the TaskProgressManager class."""

    def test_init_creates_project_info(self):
        from opi.core.task_manager import TaskProgressManager, TaskStatus, _projects

        TaskProgressManager("proj-1", "test-project")
        assert "proj-1" in _projects
        info = _projects["proj-1"]
        assert info.project_name == "test-project"
        assert info.status == TaskStatus.RUNNING
        assert info.id == "proj-1"

    def test_init_stores_manager_instance(self):
        from opi.core.task_manager import TaskProgressManager, _project_managers

        mgr = TaskProgressManager("proj-1", "test-project")
        assert "proj-1" in _project_managers
        assert _project_managers["proj-1"] is mgr

    def test_add_task_returns_task_id(self):
        from opi.core.task_manager import TaskProgressManager

        mgr = TaskProgressManager("proj-1", "test-project")
        task_id = mgr.add_task("Build image")
        assert isinstance(task_id, str)
        assert len(task_id) > 0

    def test_add_task_creates_running_task(self):
        from opi.core.task_manager import TaskProgressManager, TaskStatus

        mgr = TaskProgressManager("proj-1", "test-project")
        task_id = mgr.add_task("Build image")
        assert task_id in mgr.tasks
        assert mgr.tasks[task_id].status == TaskStatus.RUNNING
        assert mgr.tasks[task_id].name == "Build image"

    def test_add_task_updates_current_step(self):
        from opi.core.task_manager import TaskProgressManager, _projects

        mgr = TaskProgressManager("proj-1", "test-project")
        mgr.add_task("Deploy service")
        assert _projects["proj-1"].current_step == "Deploy service"

    def test_complete_task_marks_completed(self):
        from opi.core.task_manager import TaskProgressManager, TaskStatus

        mgr = TaskProgressManager("proj-1", "test-project")
        task_id = mgr.add_task("Build")
        mgr.complete_task(task_id)
        assert mgr.tasks[task_id].status == TaskStatus.COMPLETED
        assert mgr.tasks[task_id].completed_at is not None

    def test_complete_task_nonexistent_no_error(self):
        from opi.core.task_manager import TaskProgressManager

        mgr = TaskProgressManager("proj-1", "test-project")
        # Should not raise
        mgr.complete_task("nonexistent-task")

    def test_fail_task_marks_failed_with_error(self):
        from opi.core.task_manager import TaskProgressManager, TaskStatus

        mgr = TaskProgressManager("proj-1", "test-project")
        task_id = mgr.add_task("Build")
        mgr.fail_task(task_id, "Build failed")
        assert mgr.tasks[task_id].status == TaskStatus.FAILED
        assert mgr.tasks[task_id].error == "Build failed"
        assert mgr.tasks[task_id].completed_at is not None

    def test_fail_task_nonexistent_no_error(self):
        from opi.core.task_manager import TaskProgressManager

        mgr = TaskProgressManager("proj-1", "test-project")
        # Should not raise
        mgr.fail_task("nonexistent-task", "error")

    def test_add_subtask_returns_id_with_parent(self):
        from opi.core.task_manager import TaskProgressManager, TaskStatus

        mgr = TaskProgressManager("proj-1", "test-project")
        parent_id = mgr.add_task("Parent task")
        subtask_id = mgr.add_subtask(parent_id, "Child task")
        assert subtask_id in mgr.tasks
        assert mgr.tasks[subtask_id].parent_id == parent_id
        assert mgr.tasks[subtask_id].status == TaskStatus.RUNNING
        assert mgr.tasks[subtask_id].name == "Child task"

    def test_add_subtask_updates_current_step(self):
        from opi.core.task_manager import TaskProgressManager, _projects

        mgr = TaskProgressManager("proj-1", "test-project")
        parent_id = mgr.add_task("Parent")
        mgr.add_subtask(parent_id, "Child step")
        assert _projects["proj-1"].current_step == "Child step"

    def test_update_current_step(self):
        from opi.core.task_manager import TaskProgressManager, _projects

        mgr = TaskProgressManager("proj-1", "test-project")
        mgr.update_current_step("Deploying pods")
        assert _projects["proj-1"].current_step == "Deploying pods"

    def test_complete_project(self):
        from opi.core.task_manager import TaskProgressManager, TaskStatus, _projects

        mgr = TaskProgressManager("proj-1", "test-project")
        mgr.complete_project()
        assert _projects["proj-1"].status == TaskStatus.COMPLETED

    def test_fail_project(self):
        from opi.core.task_manager import TaskProgressManager, TaskStatus, _projects

        mgr = TaskProgressManager("proj-1", "test-project")
        mgr.fail_project("Fatal error")
        assert _projects["proj-1"].status == TaskStatus.FAILED

    def test_add_logs(self):
        from opi.core.task_manager import TaskProgressManager, _projects

        mgr = TaskProgressManager("proj-1", "test-project")
        mgr.add_logs(["log line 1", "log line 2"])
        assert _projects["proj-1"].logs == ["log line 1", "log line 2"]

    def test_add_events(self):
        from opi.core.task_manager import TaskProgressManager, _projects

        mgr = TaskProgressManager("proj-1", "test-project")
        events = [{"type": "Normal", "message": "Created pod"}]
        mgr.add_events(events)
        assert _projects["proj-1"].events == events

    def test_update_component_web_address(self):
        from opi.core.task_manager import TaskProgressManager, _projects

        mgr = TaskProgressManager("proj-1", "test-project")
        mgr.update_component_web_address("frontend", "https://example.com")
        assert _projects["proj-1"].web_addresses == {"frontend": "https://example.com"}

    def test_update_component_web_address_multiple(self):
        from opi.core.task_manager import TaskProgressManager, _projects

        mgr = TaskProgressManager("proj-1", "test-project")
        mgr.update_component_web_address("frontend", "https://front.example.com")
        mgr.update_component_web_address("backend", "https://back.example.com")
        assert _projects["proj-1"].web_addresses == {
            "frontend": "https://front.example.com",
            "backend": "https://back.example.com",
        }

    def test_multiple_tasks_tracked_independently(self):
        from opi.core.task_manager import TaskProgressManager, TaskStatus

        mgr = TaskProgressManager("proj-1", "test-project")
        t1 = mgr.add_task("Task A")
        t2 = mgr.add_task("Task B")
        mgr.complete_task(t1)
        mgr.fail_task(t2, "error")
        assert mgr.tasks[t1].status == TaskStatus.COMPLETED
        assert mgr.tasks[t2].status == TaskStatus.FAILED
