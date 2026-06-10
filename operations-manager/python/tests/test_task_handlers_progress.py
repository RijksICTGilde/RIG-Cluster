"""Tests for structured progress tracking across all task handlers.

Verifies that each handler properly calls add_task, complete_task, fail_task,
fail_project, update_component_web_address, and passes progress to
process_project_from_git where applicable.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PM_PATH = "opi.manager.project_manager.ProjectManager"
CREATE_PM_PATH = "opi.manager.project_manager.create_project_manager"


def _make_progress():
    """Create a mock progress manager with all expected methods."""
    progress = MagicMock()
    progress.add_task.side_effect = lambda name: f"task-{name}"
    progress.add_subtask.side_effect = lambda parent, name: f"subtask-{name}"
    progress.complete_task = MagicMock()
    progress.fail_task = MagicMock()
    progress.fail_project = MagicMock()
    progress.update_current_step = MagicMock()
    progress.update_component_web_address = MagicMock()
    return progress


# ---------------------------------------------------------------------------
# handle_update_image
# ---------------------------------------------------------------------------


class TestHandleUpdateImage:
    @pytest.fixture
    def payload(self):
        return {
            "project_name": "test-project",
            "deployment_name": "dev",
            "component_name": "web",
            "image": "registry.example.com/web:v2",
        }

    @pytest.mark.asyncio
    async def test_success_calls_add_task_and_complete(self, payload):
        from opi.core.task_handlers_deployment import handle_update_image

        progress = _make_progress()

        mock_pm = AsyncMock()
        mock_pm.update_image_and_regenerate = AsyncMock(return_value={"updates": {"previous_image": "web:v1"}})
        mock_pm.close = AsyncMock()

        with patch(PM_PATH, return_value=mock_pm):
            result = await handle_update_image(payload, progress)

        assert progress.add_task.call_count == 2
        progress.add_task.assert_any_call("Initializing project")
        progress.add_task.assert_any_call("Updating component image")
        assert progress.complete_task.call_count == 2
        assert result["component"] == "web"

    @pytest.mark.asyncio
    async def test_error_calls_fail_task_and_fail_project(self, payload):
        from opi.core.task_handlers_deployment import handle_update_image

        progress = _make_progress()

        mock_pm = AsyncMock()
        mock_pm.update_image_and_regenerate = AsyncMock(side_effect=RuntimeError("image pull failed"))
        mock_pm.close = AsyncMock()

        with patch(PM_PATH, return_value=mock_pm), pytest.raises(RuntimeError, match="image pull failed"):
            await handle_update_image(payload, progress)

        progress.fail_task.assert_called_once()
        progress.fail_project.assert_called_once()


# ---------------------------------------------------------------------------
# handle_delete_deployment
# ---------------------------------------------------------------------------


class TestHandleDeleteDeployment:
    @pytest.fixture
    def payload(self):
        return {
            "project_name": "test-project",
            "deployment_name": "dev",
        }

    @pytest.mark.asyncio
    async def test_success_calls_add_task_and_complete(self, payload):
        from opi.core.task_handlers_deployment import handle_delete_deployment

        progress = _make_progress()

        mock_pm = AsyncMock()
        mock_pm.delete_deployment = AsyncMock(return_value={"success": True, "namespace": {"success": True}})
        mock_pm.close = AsyncMock()

        with patch(CREATE_PM_PATH, return_value=mock_pm):
            result = await handle_delete_deployment(payload, progress)

        assert progress.add_task.call_count == 2
        progress.add_task.assert_any_call("Initializing project manager")
        progress.add_task.assert_any_call("Deleting deployment resources")
        assert progress.complete_task.call_count == 2
        assert result["deletion_results"]["namespace"]["success"] is True

    @pytest.mark.asyncio
    async def test_deletion_not_success_raises_and_fails_task(self, payload):
        # A partially-failed delete must FAIL the task (not return a "partial" success),
        # so the caller / nightly cleaner retries instead of treating it as done.
        from opi.core.task_handlers_deployment import handle_delete_deployment

        progress = _make_progress()

        mock_pm = AsyncMock()
        mock_pm.delete_deployment = AsyncMock(return_value={"success": False, "errors": ["boom"]})
        mock_pm.close = AsyncMock()

        with patch(CREATE_PM_PATH, return_value=mock_pm), pytest.raises(RuntimeError):
            await handle_delete_deployment(payload, progress)

        progress.fail_task.assert_called_once()
        progress.fail_project.assert_called_once()

    @pytest.mark.asyncio
    async def test_error_calls_fail_project(self, payload):
        from opi.core.task_handlers_deployment import handle_delete_deployment

        progress = _make_progress()

        mock_pm = AsyncMock()
        mock_pm.delete_deployment = AsyncMock(side_effect=RuntimeError("k8s unreachable"))
        mock_pm.close = AsyncMock()

        with patch(CREATE_PM_PATH, return_value=mock_pm), pytest.raises(RuntimeError, match="k8s unreachable"):
            await handle_delete_deployment(payload, progress)

        progress.fail_task.assert_called_once()
        progress.fail_project.assert_called_once()


# ---------------------------------------------------------------------------
# handle_clone_database
# ---------------------------------------------------------------------------


class TestHandleCloneDatabase:
    @pytest.fixture
    def payload(self):
        return {
            "project_name": "test-project",
            "deployment_name": "dev",
            "source_database": "mydb",
            "source_schema": "public",
            "source_username": "user",
            "source_password": "pass",
            "source_host": "db.example.com",
            "source_port": 5432,
        }

    @pytest.mark.asyncio
    async def test_success_direct(self, payload):
        from opi.core.task_handlers_operations import handle_clone_database

        progress = _make_progress()

        mock_pm = AsyncMock()
        mock_pm.clone_database_from_external_direct = AsyncMock(
            return_value={
                "success": True,
                "source": "db.example.com",
                "target": {},
                "operations": {"rows_copied": 100},
            }
        )
        mock_pm.close = AsyncMock()

        with patch(PM_PATH, return_value=mock_pm):
            result = await handle_clone_database(payload, progress)

        assert progress.add_task.call_count == 2
        progress.add_task.assert_any_call("Initializing project manager")
        progress.add_task.assert_any_call("Cloning database via direct connection")
        assert progress.complete_task.call_count == 2
        assert result["rows_copied"] == 100

    @pytest.mark.asyncio
    async def test_clone_failure_calls_fail_project(self, payload):
        from opi.core.task_handlers_operations import handle_clone_database

        progress = _make_progress()

        mock_pm = AsyncMock()
        mock_pm.clone_database_from_external_direct = AsyncMock(return_value={"success": False, "errors": ["timeout"]})
        mock_pm.close = AsyncMock()

        with patch(PM_PATH, return_value=mock_pm), pytest.raises(RuntimeError, match="Database clone failed"):
            await handle_clone_database(payload, progress)

        progress.fail_task.assert_called_once()
        progress.fail_project.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_host_calls_fail_project(self):
        from opi.core.task_handlers_operations import handle_clone_database

        payload = {
            "project_name": "test-project",
            "deployment_name": "dev",
            "source_database": "mydb",
            "source_schema": "public",
            "source_username": "user",
            "source_password": "pass",
        }
        progress = _make_progress()

        mock_pm = AsyncMock()
        mock_pm.close = AsyncMock()

        with patch(PM_PATH, return_value=mock_pm), pytest.raises(ValueError, match="source_host and source_port"):
            await handle_clone_database(payload, progress)

        progress.fail_task.assert_called_once()
        progress.fail_project.assert_called_once()

    @pytest.mark.asyncio
    async def test_tunnel_clone_uses_tunnel_task_name(self):
        from opi.core.task_handlers_operations import handle_clone_database

        payload = {
            "project_name": "test-project",
            "deployment_name": "dev",
            "source_database": "mydb",
            "source_schema": "public",
            "source_username": "user",
            "source_password": "pass",
            "tunnel": {
                "server_url": "https://tunnel.example.com",
                "username": "tun_user",
                "password": "tun_pass",
                "remote_host": "db.internal",
                "remote_port": 5432,
            },
        }
        progress = _make_progress()

        mock_pm = AsyncMock()
        mock_pm.clone_database_from_external_with_tunnel = AsyncMock(
            return_value={"success": True, "source": "db.internal", "operations": {}}
        )
        mock_pm.close = AsyncMock()

        with patch(PM_PATH, return_value=mock_pm):
            await handle_clone_database(payload, progress)

        progress.add_task.assert_any_call("Cloning database via Chisel tunnel")


# ---------------------------------------------------------------------------
# handle_clone_bucket
# ---------------------------------------------------------------------------


class TestHandleCloneBucket:
    @pytest.fixture
    def payload(self):
        return {
            "project_name": "test-project",
            "deployment_name": "dev",
            "source_bucket": "mybucket",
            "source_access_key": "access",
            "source_secret_key": "secret",
            "source_host": "minio.example.com",
            "source_port": 9000,
        }

    @pytest.mark.asyncio
    async def test_success_direct(self, payload):
        from opi.core.task_handlers_operations import handle_clone_bucket

        progress = _make_progress()

        mock_pm = AsyncMock()
        mock_pm.clone_minio_bucket_from_external_direct = AsyncMock(
            return_value={
                "success": True,
                "source": "minio.example.com",
                "target": {},
                "operations": {"objects_copied": 42},
            }
        )
        mock_pm.close = AsyncMock()

        with patch(PM_PATH, return_value=mock_pm):
            result = await handle_clone_bucket(payload, progress)

        assert progress.add_task.call_count == 2
        progress.add_task.assert_any_call("Initializing project manager")
        progress.add_task.assert_any_call("Cloning bucket")
        assert progress.complete_task.call_count == 2
        assert result["objects_copied"] == 42

    @pytest.mark.asyncio
    async def test_clone_failure_calls_fail_project(self, payload):
        from opi.core.task_handlers_operations import handle_clone_bucket

        progress = _make_progress()

        mock_pm = AsyncMock()
        mock_pm.clone_minio_bucket_from_external_direct = AsyncMock(
            return_value={"success": False, "errors": ["access denied"]}
        )
        mock_pm.close = AsyncMock()

        with patch(PM_PATH, return_value=mock_pm), pytest.raises(RuntimeError, match="Bucket clone failed"):
            await handle_clone_bucket(payload, progress)

        progress.fail_task.assert_called_once()
        progress.fail_project.assert_called_once()


# ---------------------------------------------------------------------------
# handle_refresh_deployment
# ---------------------------------------------------------------------------


class TestHandleRefreshDeployment:
    @pytest.fixture
    def payload(self):
        return {
            "project_name": "test-project",
            "deployment_name": "dev",
        }

    @pytest.mark.asyncio
    async def test_success_passes_progress_and_reports_urls(self, payload):
        from opi.core.task_handlers_operations import handle_refresh_deployment

        progress = _make_progress()

        mock_project = MagicMock()
        mock_project.filename = "test-project.yaml"

        mock_dep_result = MagicMock()
        mock_dep_result.cluster = "prod"
        mock_dep_result.urls = {"web": "https://web.example.com"}

        mock_pm = AsyncMock()
        mock_pm.process_project_from_git = AsyncMock(return_value=True)
        mock_pm.get_deployment_results = MagicMock(return_value={"dev": mock_dep_result})
        mock_pm.close = AsyncMock()

        mock_project_service = MagicMock()
        mock_project_service.get_project.return_value = mock_project

        with (
            patch(
                "opi.utils.project_utils.validate_project_name",
                return_value=True,
            ),
            patch(
                "opi.services.project_service.get_project_service",
                return_value=mock_project_service,
            ),
            patch(CREATE_PM_PATH, return_value=mock_pm),
        ):
            result = await handle_refresh_deployment(payload, progress)

        # Verify progress was passed to process_project_from_git
        mock_pm.process_project_from_git.assert_called_once_with(
            "projects/test-project.yaml",
            task_progress_manager=progress,
            deployment_name="dev",
            force_clone=False,
        )

        assert progress.add_task.call_count == 2
        progress.add_task.assert_any_call("Looking up project")
        progress.add_task.assert_any_call("Processing deployment")
        assert progress.complete_task.call_count == 2

        # Web addresses reported
        progress.update_component_web_address.assert_called_once_with("web", "https://web.example.com")

        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_project_not_found_calls_fail_project(self, payload):
        from opi.core.task_handlers_operations import handle_refresh_deployment

        progress = _make_progress()

        mock_project_service = MagicMock()
        mock_project_service.get_project.return_value = None

        with (
            patch(
                "opi.utils.project_utils.validate_project_name",
                return_value=True,
            ),
            patch(
                "opi.services.project_service.get_project_service",
                return_value=mock_project_service,
            ),
            pytest.raises(ValueError, match="not found"),
        ):
            await handle_refresh_deployment(payload, progress)

        progress.fail_task.assert_called_once()
        progress.fail_project.assert_called_once()

    @pytest.mark.asyncio
    async def test_processing_failed_calls_fail_project(self, payload):
        from opi.core.task_handlers_operations import handle_refresh_deployment

        progress = _make_progress()

        mock_project = MagicMock()
        mock_project.filename = "test-project.yaml"

        mock_pm = AsyncMock()
        mock_pm.process_project_from_git = AsyncMock(return_value=False)
        mock_pm.get_processing_error = MagicMock(return_value=None)
        mock_pm.close = AsyncMock()

        mock_project_service = MagicMock()
        mock_project_service.get_project.return_value = mock_project

        with (
            patch(
                "opi.utils.project_utils.validate_project_name",
                return_value=True,
            ),
            patch(
                "opi.services.project_service.get_project_service",
                return_value=mock_project_service,
            ),
            patch(CREATE_PM_PATH, return_value=mock_pm),
        ):
            result = await handle_refresh_deployment(payload, progress)

        assert result["status"] == "failed"
        progress.fail_task.assert_called_once()
        progress.fail_project.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_project_name_calls_fail_project(self):
        from opi.core.task_handlers_operations import handle_refresh_deployment

        payload = {
            "project_name": "INVALID!!",
            "deployment_name": "dev",
        }
        progress = _make_progress()

        with (
            patch(
                "opi.utils.project_utils.validate_project_name",
                return_value=False,
            ),
            pytest.raises(ValueError, match="Invalid project name"),
        ):
            await handle_refresh_deployment(payload, progress)

        progress.fail_project.assert_called_once()


# ---------------------------------------------------------------------------
# handle_upsert_deployment
# ---------------------------------------------------------------------------


class TestHandleUpsertDeployment:
    @pytest.fixture
    def payload(self):
        return {
            "project_name": "test-project",
            "deployment_name": "dev",
            "components": [{"reference": "web", "image": "web:v1"}],
        }

    @pytest.mark.asyncio
    async def test_success_passes_progress_to_process(self, payload):
        from opi.core.task_handlers_project import handle_upsert_deployment

        progress = _make_progress()

        mock_dep_result = MagicMock()
        mock_dep_result.cluster = "prod"
        mock_dep_result.urls = {"web": "https://web.example.com"}

        mock_pm = AsyncMock()
        mock_pm.upsert_deployment = AsyncMock(return_value={"success": True, "created": True})
        mock_pm.process_project_from_git = AsyncMock(return_value=True)
        mock_pm.get_deployment_results = MagicMock(return_value={"dev": mock_dep_result})
        mock_pm.close = AsyncMock()

        with (
            patch(
                "opi.utils.project_utils.validate_project_name",
                return_value=True,
            ),
            patch(
                "opi.utils.naming.sanitize_kubernetes_name",
                return_value="dev",
            ),
            patch(
                "opi.utils.project_utils.normalize_container_image",
                return_value=("web:v1", None),
            ),
            patch(PM_PATH, return_value=mock_pm),
        ):
            result = await handle_upsert_deployment(payload, progress)

        # Verify progress passed to process_project_from_git
        mock_pm.process_project_from_git.assert_called_once_with(
            "projects/test-project.yaml",
            task_progress_manager=progress,
            deployment_name="dev",
            force_clone=False,
            argocd_resources_changed=True,
        )

        # Web addresses reported
        progress.update_component_web_address.assert_called_once_with("web", "https://web.example.com")

        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_processing_failed_marks_deploy_task_failed(self, payload):
        from opi.core.task_handlers_project import handle_upsert_deployment

        progress = _make_progress()

        mock_pm = AsyncMock()
        mock_pm.upsert_deployment = AsyncMock(return_value={"success": True, "created": True})
        mock_pm.process_project_from_git = AsyncMock(return_value=False)
        mock_pm.get_deployment_results = MagicMock(return_value={})
        mock_pm.close = AsyncMock()

        with (
            patch(
                "opi.utils.project_utils.validate_project_name",
                return_value=True,
            ),
            patch(
                "opi.utils.naming.sanitize_kubernetes_name",
                return_value="dev",
            ),
            patch(
                "opi.utils.project_utils.normalize_container_image",
                return_value=("web:v1", None),
            ),
            patch(PM_PATH, return_value=mock_pm),
        ):
            result = await handle_upsert_deployment(payload, progress)

        progress.fail_task.assert_called_once()
        progress.fail_project.assert_called()
        assert result["status"] == "failed"


# ---------------------------------------------------------------------------
# handle_create_project
# ---------------------------------------------------------------------------


class TestHandleCreateProject:
    """The create_project task is reused for single-deployment edits (e.g. a
    webadres change via the modal). It must forward the payload's
    deployment_name to process_project_from_git so only that deployment is
    redeployed/refreshed - not every deployment in the project.
    """

    def _mocks(self, process_result: bool = True):
        mock_git = AsyncMock()
        mock_git.create_or_update_file = AsyncMock()
        mock_git.file_exists = AsyncMock(return_value=False)

        mock_pm = AsyncMock()
        mock_pm.process_project_from_git = AsyncMock(return_value=process_result)
        mock_pm.close = AsyncMock()
        mock_pm.get_processing_error = MagicMock(return_value=None)
        mock_pm.get_component_failures = MagicMock(return_value=None)
        return mock_git, mock_pm

    @pytest.mark.asyncio
    async def test_forwards_deployment_name_when_present(self):
        from opi.core.task_handlers_project import handle_create_project

        progress = _make_progress()
        mock_git, mock_pm = self._mocks()

        payload = {
            "project_name": "test-project",
            "yaml_content": "name: test-project\n",
            "deployment_name": "dev",
        }

        with (
            patch("opi.utils.project_utils.validate_project_name", return_value=True),
            patch("opi.connectors.git.GitConnector", return_value=mock_git),
            patch(PM_PATH, return_value=mock_pm),
            patch("opi.core.simple_background._monitor_argocd_and_deployment", new=AsyncMock()),
            patch("opi.core.config.settings.OOM_WATCHER_ENABLED", False),
        ):
            result = await handle_create_project(payload, progress)

        mock_pm.process_project_from_git.assert_called_once_with(
            "projects/test-project.yaml", progress, deployment_name="dev", deployment_names=None
        )
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_deployment_name_none_when_absent(self):
        from opi.core.task_handlers_project import handle_create_project

        progress = _make_progress()
        mock_git, mock_pm = self._mocks()

        payload = {
            "project_name": "test-project",
            "yaml_content": "name: test-project\n",
        }

        with (
            patch("opi.utils.project_utils.validate_project_name", return_value=True),
            patch("opi.connectors.git.GitConnector", return_value=mock_git),
            patch(PM_PATH, return_value=mock_pm),
            patch("opi.core.simple_background._monitor_argocd_and_deployment", new=AsyncMock()),
            patch("opi.core.config.settings.OOM_WATCHER_ENABLED", False),
        ):
            await handle_create_project(payload, progress)

        mock_pm.process_project_from_git.assert_called_once_with(
            "projects/test-project.yaml", progress, deployment_name=None, deployment_names=None
        )

    @pytest.mark.asyncio
    async def test_forwards_deployment_names_list(self):
        """Domain approval supplies an explicit list of affected deployments."""
        from opi.core.task_handlers_project import handle_create_project

        progress = _make_progress()
        mock_git, mock_pm = self._mocks()

        payload = {
            "project_name": "test-project",
            "yaml_content": "name: test-project\n",
            "deployment_names": ["dev", "staging"],
        }

        with (
            patch("opi.utils.project_utils.validate_project_name", return_value=True),
            patch("opi.connectors.git.GitConnector", return_value=mock_git),
            patch(PM_PATH, return_value=mock_pm),
            patch("opi.core.simple_background._monitor_argocd_and_deployment", new=AsyncMock()),
            patch("opi.core.config.settings.OOM_WATCHER_ENABLED", False),
        ):
            await handle_create_project(payload, progress)

        mock_pm.process_project_from_git.assert_called_once_with(
            "projects/test-project.yaml", progress, deployment_name=None, deployment_names=["dev", "staging"]
        )

    @pytest.mark.asyncio
    async def test_empty_deployment_names_forwarded_as_empty(self):
        """An empty list (approval affecting zero deployments) must NOT collapse
        to None - it means 'deploy nothing', not 'deploy everything'.
        """
        from opi.core.task_handlers_project import handle_create_project

        progress = _make_progress()
        mock_git, mock_pm = self._mocks()

        payload = {
            "project_name": "test-project",
            "yaml_content": "name: test-project\n",
            "deployment_names": [],
        }

        with (
            patch("opi.utils.project_utils.validate_project_name", return_value=True),
            patch("opi.connectors.git.GitConnector", return_value=mock_git),
            patch(PM_PATH, return_value=mock_pm),
            patch("opi.core.simple_background._monitor_argocd_and_deployment", new=AsyncMock()),
            patch("opi.core.config.settings.OOM_WATCHER_ENABLED", False),
        ):
            await handle_create_project(payload, progress)

        mock_pm.process_project_from_git.assert_called_once_with(
            "projects/test-project.yaml", progress, deployment_name=None, deployment_names=[]
        )


# ---------------------------------------------------------------------------
# PersistentTaskProgressManager.update_task
# ---------------------------------------------------------------------------


class TestUpdateTaskMethod:
    def test_persistent_update_task(self):
        """Test that update_task updates subtask name and marks dirty."""
        with patch(
            "opi.core.persistent_task_progress.asyncio.get_running_loop",
            side_effect=RuntimeError,
        ):
            from opi.core.persistent_task_progress import PersistentTaskProgressManager

            pm = PersistentTaskProgressManager(
                task_id="test-123",
                project_name="test-project",
                task_service=MagicMock(),
            )

        task_id = pm.add_task("Original name")
        pm._dirty = False  # reset after add_task

        pm.update_task(task_id, "Updated name")

        assert pm._subtasks[task_id]["name"] == "Updated name"
        assert pm._dirty is True
        assert pm._current_step == "Updated name"

    def test_in_memory_update_task(self):
        """Test that TaskProgressManager.update_task works."""
        from opi.core.task_manager import TaskProgressManager

        pm = TaskProgressManager(project_id="test-123", project_name="test-project")
        task_id = pm.add_task("Original name")
        pm.update_task(task_id, "Updated name")

        assert pm.tasks[task_id].name == "Updated name"
