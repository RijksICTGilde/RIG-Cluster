"""Tests for rollout=false: save the change, skip the rollout (RC-46).

Three things have to hold, and each is measured rather than assumed:

1. Nothing changes for callers who do not pass the flag. Every handler still
   processes exactly as before.
2. With the flag, the handler does not process at all -- no call to
   ``process_project_from_git``, and no processing step in the task progress.
3. The task result says so: ``processing.status == "skipped"`` with a reason that
   distinguishes "you asked for this" from "there was nothing to do".
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core.task_rollout import (
    DEFERRABLE_TASK_TYPES,
    NON_DEFERRABLE_REASONS,
    SKIPPED_REASON,
    SKIPPED_STEP_LABEL,
    rollout_requested,
    skipped_processing,
)

PM_PATH = "opi.manager.project_manager.ProjectManager"


def _make_progress():
    progress = MagicMock()
    progress.add_task.side_effect = lambda name: f"task-{name}"
    progress.add_subtask.side_effect = lambda parent, name: f"subtask-{name}"
    return progress


def _step_labels(progress) -> list[str]:
    return [call.args[0] for call in progress.add_task.call_args_list]


# ---------------------------------------------------------------------------
# The flag itself
# ---------------------------------------------------------------------------


class TestRolloutFlag:
    def test_absent_flag_means_roll_out(self):
        assert rollout_requested({}) is True

    def test_true_means_roll_out(self):
        assert rollout_requested({"rollout": True}) is True

    def test_false_means_defer(self):
        assert rollout_requested({"rollout": False}) is False

    def test_skipped_processing_names_the_reason(self):
        processing = skipped_processing()
        assert processing["status"] == "skipped"
        assert processing["reason"] == SKIPPED_REASON

    def test_every_task_type_is_classified_exactly_once(self):
        """Each of the seventeen v2 task types either may defer or says why it may not.

        A new task type that is neither is a task type whose ``rollout=false`` would be
        silently ignored, which is the thing this feature must not do.
        """
        task_types = {
            "create_project",
            "upsert_deployment",
            "refresh_project",
            "delete_deployment",
            "delete_component",
            "update_image",
            "clone_database",
            "clone_bucket",
            "refresh_deployment",
            "add_component",
            "update_component",
            "add_component_to_deployment",
            "add_service",
            "configure_service",
            "configure_service_values",
            "manage_database_schemas",
            "configure_attachment",
        }
        classified = DEFERRABLE_TASK_TYPES | set(NON_DEFERRABLE_REASONS)
        assert classified == task_types
        assert not (DEFERRABLE_TASK_TYPES & set(NON_DEFERRABLE_REASONS))


# ---------------------------------------------------------------------------
# handle_add_component
# ---------------------------------------------------------------------------


class TestAddComponentRollout:
    @pytest.fixture
    def payload(self):
        return {
            "project_name": "test-project",
            "name": "web",
            "image": "web:v1",
            "deployment_names": ["dev"],
        }

    def _pm(self):
        pm = AsyncMock()
        pm.add_component = AsyncMock(return_value={"success": True, "deployments_updated": ["dev"]})
        pm.process_project_from_git = AsyncMock(return_value=True)
        pm.get_deployment_results = MagicMock(return_value={})
        pm.get_processing_error = MagicMock(return_value=None)
        pm.get_component_failures = MagicMock(return_value=None)
        pm.close = AsyncMock()
        return pm

    @pytest.mark.asyncio
    async def test_default_still_processes(self, payload):
        from opi.core.task_handlers_components import handle_add_component

        pm = self._pm()
        progress = _make_progress()
        with patch(PM_PATH, return_value=pm):
            result = await handle_add_component(payload, progress)

        pm.process_project_from_git.assert_called_once()
        assert result["processing"]["status"] == "completed"
        assert SKIPPED_STEP_LABEL not in _step_labels(progress)

    @pytest.mark.asyncio
    async def test_rollout_false_skips_processing(self, payload):
        from opi.core.task_handlers_components import handle_add_component

        pm = self._pm()
        progress = _make_progress()
        with patch(PM_PATH, return_value=pm):
            result = await handle_add_component({**payload, "rollout": False}, progress)

        # The component was still written to the project file...
        pm.add_component.assert_awaited_once()
        # ...but nothing was processed.
        pm.process_project_from_git.assert_not_called()
        assert result["status"] == "success"
        assert result["processing"]["status"] == "skipped"
        assert result["processing"]["reason"] == SKIPPED_REASON

    @pytest.mark.asyncio
    async def test_rollout_false_replaces_the_processing_step(self, payload):
        from opi.core.task_handlers_components import handle_add_component

        pm = self._pm()
        progress = _make_progress()
        with patch(PM_PATH, return_value=pm):
            await handle_add_component({**payload, "rollout": False}, progress)

        labels = _step_labels(progress)
        assert "Project verwerken" not in labels
        assert SKIPPED_STEP_LABEL in labels


# ---------------------------------------------------------------------------
# handle_update_component / handle_add_component_to_deployment
# ---------------------------------------------------------------------------


class TestComponentHandlersRollout:
    @pytest.mark.asyncio
    async def test_update_component_defers(self):
        from opi.core.task_handlers_components import handle_update_component

        pm = AsyncMock()
        pm.update_component = AsyncMock(return_value={"success": True})
        pm.process_project_from_git = AsyncMock(return_value=True)
        pm.get_processing_error = MagicMock(return_value=None)
        pm.get_component_failures = MagicMock(return_value=None)
        pm.close = AsyncMock()

        progress = _make_progress()
        with patch(PM_PATH, return_value=pm):
            result = await handle_update_component(
                {"project_name": "test-project", "name": "web", "rollout": False}, progress
            )

        pm.process_project_from_git.assert_not_called()
        assert result["processing"]["reason"] == SKIPPED_REASON

    @pytest.mark.asyncio
    async def test_update_component_forwards_add_and_remove_services(self):
        from opi.core.task_handlers_components import handle_update_component

        pm = AsyncMock()
        pm.update_component = AsyncMock(return_value={"success": True})
        pm.process_project_from_git = AsyncMock(return_value=True)
        pm.get_processing_error = MagicMock(return_value=None)
        pm.get_component_failures = MagicMock(return_value=None)
        pm.close = AsyncMock()

        progress = _make_progress()
        with patch(PM_PATH, return_value=pm):
            await handle_update_component(
                {
                    "project_name": "test-project",
                    "name": "web",
                    "add_services": ["redis"],
                    "remove_services": ["attachments"],
                    "rollout": False,
                },
                progress,
            )

        call_kwargs = pm.update_component.call_args[1]
        assert call_kwargs["add_services"] == ["redis"]
        assert call_kwargs["remove_services"] == ["attachments"]
        assert call_kwargs["services"] is None

    @pytest.mark.asyncio
    async def test_add_component_to_deployment_defers(self):
        from opi.core.task_handlers_components import handle_add_component_to_deployment

        pm = AsyncMock()
        pm.add_component_to_deployment = AsyncMock(return_value={"success": True})
        pm.process_project_from_git = AsyncMock(return_value=True)
        pm.get_deployment_results = MagicMock(return_value={})
        pm.get_processing_error = MagicMock(return_value=None)
        pm.get_component_failures = MagicMock(return_value=None)
        pm.close = AsyncMock()

        progress = _make_progress()
        with patch(PM_PATH, return_value=pm):
            result = await handle_add_component_to_deployment(
                {
                    "project_name": "test-project",
                    "deployment_name": "dev",
                    "component_name": "web",
                    "image": "web:v1",
                    "rollout": False,
                },
                progress,
            )

        pm.process_project_from_git.assert_not_called()
        assert result["status"] == "success"
        assert result["processing"]["reason"] == SKIPPED_REASON
        assert result["urls"] == {}


# ---------------------------------------------------------------------------
# handle_add_service / handle_configure_service -- these already had "skipped"
# ---------------------------------------------------------------------------


class TestServiceHandlersRollout:
    @pytest.mark.asyncio
    async def test_add_service_defers(self):
        from opi.core.task_handlers_components import handle_add_service

        pm = AsyncMock()
        pm.add_service = AsyncMock(return_value={"success": True, "services_added": ["keycloak"]})
        pm.process_project_from_git = AsyncMock(return_value=True)
        pm.get_processing_error = MagicMock(return_value=None)
        pm.get_component_failures = MagicMock(return_value=None)
        pm.close = AsyncMock()

        progress = _make_progress()
        with patch(PM_PATH, return_value=pm):
            result = await handle_add_service(
                {"project_name": "test-project", "service": "keycloak", "rollout": False}, progress
            )

        pm.process_project_from_git.assert_not_called()
        assert result["processing"]["reason"] == SKIPPED_REASON

    @pytest.mark.asyncio
    async def test_nothing_added_stays_a_plain_skip(self):
        """'Nothing to do' must remain distinguishable from 'you deferred it'."""
        from opi.core.task_handlers_components import handle_add_service

        pm = AsyncMock()
        pm.add_service = AsyncMock(return_value={"success": True, "services_added": []})
        pm.process_project_from_git = AsyncMock(return_value=True)
        pm.get_processing_error = MagicMock(return_value=None)
        pm.get_component_failures = MagicMock(return_value=None)
        pm.close = AsyncMock()

        progress = _make_progress()
        with patch(PM_PATH, return_value=pm):
            result = await handle_add_service({"project_name": "test-project", "service": "keycloak"}, progress)

        assert result["processing"]["status"] == "skipped"
        assert "reason" not in result["processing"]

    @pytest.mark.asyncio
    async def test_configure_service_defers(self):
        from opi.core.task_handlers_components import handle_configure_service

        pm = AsyncMock()
        pm.configure_service = AsyncMock(return_value={"success": True})
        pm.process_project_from_git = AsyncMock(return_value=True)
        pm.get_processing_error = MagicMock(return_value=None)
        pm.close = AsyncMock()

        progress = _make_progress()
        with patch(PM_PATH, return_value=pm):
            result = await handle_configure_service(
                {
                    "project_name": "test-project",
                    "service": "keycloak",
                    "target": "project",
                    "operation": "upsert",
                    "config": {"template": "x"},
                    "rollout": False,
                },
                progress,
            )

        # The config was still written and committed.
        pm.configure_service.assert_awaited_once()
        pm.process_project_from_git.assert_not_called()
        assert result["status"] == "success"
        assert result["processing"]["reason"] == SKIPPED_REASON

    @pytest.mark.asyncio
    async def test_configure_service_patch_forwards_add_and_remove_and_reports_counts(self):
        from opi.core.task_handlers_components import handle_configure_service

        pm = AsyncMock()
        pm.patch_service_config_list = AsyncMock(return_value={"success": True, "added": 1, "updated": 0, "removed": 2})
        pm.process_project_from_git = AsyncMock(return_value=True)
        pm.get_processing_error = MagicMock(return_value=None)
        pm.close = AsyncMock()

        progress = _make_progress()
        with patch(PM_PATH, return_value=pm):
            result = await handle_configure_service(
                {
                    "project_name": "test-project",
                    "service": "persistent-storage",
                    "target": "component",
                    "operation": "patch",
                    "add": [{"name": "data3", "size": "1Gi", "mount-path": "/data3"}],
                    "remove": ["data1", "data2"],
                    "component": "web",
                    "rollout": False,
                },
                progress,
            )

        call_kwargs = pm.patch_service_config_list.call_args[1]
        assert call_kwargs["add"] == [{"name": "data3", "size": "1Gi", "mount-path": "/data3"}]
        assert call_kwargs["remove"] == ["data1", "data2"]
        assert call_kwargs["component_name"] == "web"
        pm.configure_service.assert_not_called()
        pm.clear_service_config.assert_not_called()
        pm.process_project_from_git.assert_not_called()
        assert result["status"] == "success"
        assert result["added"] == 1
        assert result["updated"] == 0
        assert result["removed"] == 2


# ---------------------------------------------------------------------------
# handle_upsert_deployment
# ---------------------------------------------------------------------------


class TestUpsertDeploymentRollout:
    @pytest.mark.asyncio
    async def test_defers(self):
        from opi.core.task_handlers_project import handle_upsert_deployment

        pm = AsyncMock()
        pm.upsert_deployment = AsyncMock(return_value={"success": True, "created": True})
        pm.process_project_from_git = AsyncMock(return_value=True)
        pm.get_deployment_results = MagicMock(return_value={})
        pm.get_processing_error = MagicMock(return_value=None)
        pm.get_component_failures = MagicMock(return_value=None)
        pm.close = AsyncMock()

        progress = _make_progress()
        with patch(PM_PATH, return_value=pm):
            result = await handle_upsert_deployment(
                {
                    "project_name": "test-project",
                    "deployment_name": "dev",
                    "components": [{"reference": "web", "image": "web:v1"}],
                    "rollout": False,
                },
                progress,
            )

        pm.process_project_from_git.assert_not_called()
        assert result["status"] == "success"
        assert result["deployment"]["created"] is True
        assert result["processing"]["reason"] == SKIPPED_REASON
        # No OOM watcher and no web addresses: nothing ran.
        progress.update_component_web_address.assert_not_called()


# ---------------------------------------------------------------------------
# handle_update_image -- the manager does the write and the rollout in one call,
# so the split lives there.
# ---------------------------------------------------------------------------


class TestUpdateImageRollout:
    @pytest.mark.asyncio
    async def test_passes_the_flag_to_the_manager(self):
        from opi.core.task_handlers_deployment import handle_update_image

        pm = AsyncMock()
        pm.update_image_and_regenerate = AsyncMock(return_value={"status": "success"})
        pm.close = AsyncMock()

        progress = _make_progress()
        with patch(PM_PATH, return_value=pm):
            result = await handle_update_image(
                {
                    "project_name": "test-project",
                    "deployment_name": "dev",
                    "component_name": "web",
                    "image": "web:v2",
                    "rollout": False,
                },
                progress,
            )

        assert pm.update_image_and_regenerate.await_args.kwargs["rollout"] is False
        assert result["processing"]["reason"] == SKIPPED_REASON
        assert SKIPPED_STEP_LABEL in _step_labels(progress)

    @pytest.mark.asyncio
    async def test_default_rolls_out_and_reports_no_processing_block(self):
        from opi.core.task_handlers_deployment import handle_update_image

        pm = AsyncMock()
        pm.update_image_and_regenerate = AsyncMock(return_value={"status": "success"})
        pm.close = AsyncMock()

        progress = _make_progress()
        with patch(PM_PATH, return_value=pm):
            result = await handle_update_image(
                {
                    "project_name": "test-project",
                    "deployment_name": "dev",
                    "component_name": "web",
                    "image": "web:v2",
                },
                progress,
            )

        assert pm.update_image_and_regenerate.await_args.kwargs["rollout"] is True
        assert "processing" not in result


# ---------------------------------------------------------------------------
# The manager split itself: write and commit happen, processing does not.
# ---------------------------------------------------------------------------


def _image_project() -> dict:
    from opi.core.config import settings

    return {
        "name": "proj",
        "components": [{"name": "web"}],
        "deployments": [
            {
                "name": "productie",
                "cluster": settings.CLUSTER_MANAGER,
                "namespace": "proj",
                "components": [{"reference": "web", "image": "ghcr.io/example/web:1.0"}],
            }
        ],
    }


def _image_manager():
    import copy

    from opi.manager.project_manager import ProjectManager

    pm = ProjectManager(project_file_relative_path="projects/proj.yaml")
    pm.get_name = AsyncMock(return_value="proj")
    pm.get_contents = AsyncMock(side_effect=lambda *a, **k: copy.deepcopy(_image_project()))
    pm._project_file_handler = MagicMock()
    pm.save_and_commit_project = AsyncMock()
    pm.process_project = AsyncMock(return_value=True)
    return pm


class TestUpdateImageManagerSplit:
    @pytest.mark.asyncio
    async def test_rollout_false_commits_but_does_not_process(self):
        pm = _image_manager()

        result = await pm.update_image_and_regenerate("productie", "web", "ghcr.io/example/web:2.0", rollout=False)

        pm.save_and_commit_project.assert_awaited_once()
        pm.process_project.assert_not_called()
        assert result["status"] == "success"
        assert result["actions_performed"] == ["image_update"]
        assert result["updates"]["image"]["new"] == "ghcr.io/example/web:2.0"

    @pytest.mark.asyncio
    async def test_default_still_processes(self):
        """Control: without the flag the same call goes on to process the deployment."""
        pm = _image_manager()

        class _Reached(Exception):
            pass

        pm.process_project = AsyncMock(side_effect=_Reached)

        with pytest.raises(_Reached):
            await pm.update_image_and_regenerate("productie", "web", "ghcr.io/example/web:2.0")

        pm.save_and_commit_project.assert_awaited_once()
