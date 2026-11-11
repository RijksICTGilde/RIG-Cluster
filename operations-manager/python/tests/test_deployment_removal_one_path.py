"""The decisions of RC-484: every entrance of a deployment removal runs the same steps.

Two implementations used to stand next to each other: an explicit delete and the removal of
a deployment that left the project file. They differed in retention, order, Redis, backup
data, the force sweep and the outcome. These measure the merged behaviour, one test per
decision of the plan.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.http.task_helpers import require_removable_deployment
from opi.manager.delete_project_manager import DeleteProjectManager, StuckApplicationOutcome
from opi.manager.project.deployment_removal import DeploymentRemoval, RemovalTrigger, remove_deployment
from tests.deployment_removal_helper import remove_from_project_file_edit
from tests.test_delete_project_manager import _make_deployment, _make_project_data, _make_project_manager_mock


def _argo(application_exists: bool = False, deleted: bool = True) -> AsyncMock:
    argo = AsyncMock()
    argo.refresh_application = AsyncMock(return_value=True)
    argo.application_exists = AsyncMock(return_value=application_exists)
    argo.wait_for_application_deletion = AsyncMock(return_value=deleted)
    return argo


def _store() -> MagicMock:
    store = MagicMock()
    store.get.return_value = MagicMock(filename="test-project.yaml")
    return store


def _uses_everything(mock_pm: MagicMock) -> None:
    """Let every service check say yes, so every service branch runs."""
    mock_pm._project_file_handler.deployment_uses_service = MagicMock(return_value=True)
    for manager in (mock_pm._minio_manager, mock_pm._seaweedfs_manager, mock_pm._redis_manager, mock_pm._mail_manager):
        manager.handle_service_removal = AsyncMock(return_value={"operations": [], "errors": []})
    mock_pm._ensure_database_manager.return_value.handle_service_removal = AsyncMock(
        return_value={"operations": [], "errors": []}
    )


async def _explicit_delete(mock_pm: MagicMock, marked: Any, argo: AsyncMock | None = None) -> dict[str, Any]:
    """An explicit delete of ``pr-42``, with ``marked`` as the marking service."""
    deployment = _make_deployment()
    mock_pm.get_contents = AsyncMock(return_value=_make_project_data(deployments=[deployment]))
    mock_pm.get_deployment_by_name = AsyncMock(return_value=deployment)
    with (
        patch("opi.manager.project.deployment_removal.create_argo_connector", return_value=argo or _argo()),
        patch("opi.manager.project.deployment_removal.get_project_store", return_value=_store()),
        patch("opi.manager.project.deployment_removal.marking_service", AsyncMock(return_value=marked)),
        patch("os.path.exists", return_value=False),
    ):
        return await DeleteProjectManager(mock_pm).delete_deployment("test-project", "pr-42")


class TestD3SameServicesOnBothPaths:
    """D3: an explicit delete cleans up the Redis ACL user and marks the backup data too."""

    @pytest.mark.asyncio
    async def test_explicit_delete_removes_the_redis_acl_user(self) -> None:
        mock_pm = _make_project_manager_mock()
        _uses_everything(mock_pm)

        await _explicit_delete(mock_pm, marked=AsyncMock())

        mock_pm._redis_manager.handle_service_removal.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_explicit_delete_marks_the_backup_data(self) -> None:
        mock_pm = _make_project_manager_mock()
        _uses_everything(mock_pm)
        marked = AsyncMock()

        with patch("opi.manager.backup.base.BaseBackupManager._derive_backup_key", AsyncMock(return_value="geheim")):
            await _explicit_delete(mock_pm, marked=marked)

        types = {call.kwargs["resource_type"] for call in marked.mark_resource.await_args_list}
        assert "backup_data" in types, types


class TestD4ForceOnBothPaths:
    """D4: the project-file path sweeps a stuck application away and re-measures, as the
    explicit delete does."""

    @pytest.mark.asyncio
    async def test_a_stuck_application_is_swept(self) -> None:
        mock_pm = _make_project_manager_mock()
        manager = DeleteProjectManager(mock_pm)
        manager._force_delete_or_await_cascade = AsyncMock(
            return_value=StuckApplicationOutcome(application_gone=True, finalizer_removed=True)
        )

        with (
            patch(
                "opi.manager.project.deployment_removal.create_argo_connector",
                return_value=_argo(application_exists=True, deleted=False),
            ),
            patch("os.path.exists", return_value=False),
        ):
            await remove_from_project_file_edit(
                manager, "test-project", _make_deployment(), _make_project_data(deployments=[])
            )

        manager._force_delete_or_await_cascade.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_outcome_comes_from_a_remeasurement(self) -> None:
        mock_pm = _make_project_manager_mock()
        mock_pm._kubectl_connector.argocd_application_exists = AsyncMock(return_value=True)

        with (
            patch("opi.manager.project.deployment_removal.create_argo_connector", return_value=_argo()),
            patch("os.path.exists", return_value=False),
        ):
            result = await remove_from_project_file_edit(
                DeleteProjectManager(mock_pm), "test-project", _make_deployment(), _make_project_data(deployments=[])
            )

        assert result["success"] is False, result
        assert any("still stands" in error for error in result["errors"]), result["errors"]


class TestD5TheProjectFileEditReadsTheResult:
    """D5, kaartje #402: a half-failed removal used to end as a green round, because only an
    exception reached the caller and the removal catches nearly everything itself."""

    @pytest.mark.asyncio
    async def test_a_failed_removal_becomes_a_failure_of_the_round(self) -> None:
        from opi.manager.project_manager import ProjectManager

        project_manager = ProjectManager.__new__(ProjectManager)
        project_manager._begin_step = MagicMock(return_value=None)
        project_manager._end_step = MagicMock()
        project_manager._delete_project_manager = MagicMock()
        analysis = {"current_yaml": {"name": "p"}, "previous_yaml": {"name": "p"}}

        failed = MagicMock(success=False, error_detail="namespace bleef staan")
        with patch("opi.manager.project_manager.remove_deployment", AsyncMock(return_value=failed)):
            failures = await project_manager._remove_deployment_from_edit("p", {"name": "pr-42"}, analysis)

        assert failures == ["Failed to delete removed deployment 'pr-42': namespace bleef staan"]


class TestD7AndD8TheEntranceChecks:
    """D7 and D8: every entrance answers 404 straight away, filtered on CLUSTER_MANAGER."""

    def test_a_deployment_of_another_cluster_is_not_found(self) -> None:
        store = MagicMock()
        store.get.return_value = MagicMock(
            data={"name": "demo", "deployments": [{"name": "prod", "cluster": "een-ander-cluster"}]}
        )

        with patch("opi.http.task_helpers.get_project_store", return_value=store), pytest.raises(HTTPException) as e:
            require_removable_deployment("demo", "prod")

        assert e.value.status_code == 404

    def test_an_unknown_project_is_not_found(self) -> None:
        store = MagicMock()
        store.get.return_value = None

        with patch("opi.http.task_helpers.get_project_store", return_value=store), pytest.raises(HTTPException) as e:
            require_removable_deployment("bestaat-niet", "prod")

        assert e.value.status_code == 404


class TestD9AFailedNamespaceDeleteIsAnError:
    """D9: the project-file path reported a failed namespace delete as "not_found"."""

    @pytest.mark.asyncio
    async def test_a_refused_namespace_delete_fails_the_removal(self) -> None:
        mock_pm = _make_project_manager_mock()
        mock_pm._kubectl_connector.delete_namespace = AsyncMock(return_value=False)

        with (
            patch("opi.manager.project.deployment_removal.create_argo_connector", return_value=_argo()),
            patch("os.path.exists", return_value=False),
        ):
            result = await remove_from_project_file_edit(
                DeleteProjectManager(mock_pm), "test-project", _make_deployment(), _make_project_data(deployments=[])
            )

        namespace_ops = [op for op in result["operations"] if op["type"] == "namespace_deletion"]
        assert [op["status"] for op in namespace_ops] == ["error"], namespace_ops
        assert any("rig-test-project" in error for error in result["errors"]), result["errors"]


class TestBothTriggersRunTheSameSteps:
    """The point of the merge: the step list no longer depends on who asked."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("trigger", [RemovalTrigger.EXPLICIT, RemovalTrigger.PROJECT_FILE_EDIT])
    async def test_the_services_run_before_the_namespace(self, trigger: RemovalTrigger) -> None:
        mock_pm = _make_project_manager_mock()
        _uses_everything(mock_pm)
        order: list[str] = []
        mock_pm._keycloak_manager.delete_resources_for_deployment = AsyncMock(
            side_effect=lambda *_a, **_k: order.append("services") or {"operations": [], "errors": []}
        )
        mock_pm._kubectl_connector.delete_namespace = AsyncMock(
            side_effect=lambda *_a, **_k: order.append("namespace") or True
        )
        deployment = _make_deployment()
        mock_pm.get_contents = AsyncMock(return_value=_make_project_data(deployments=[deployment]))
        mock_pm.get_deployment_by_name = AsyncMock(return_value=deployment)
        request = DeploymentRemoval(
            project_name="test-project",
            deployment_name="pr-42",
            trigger=trigger,
            deployment=deployment if trigger is RemovalTrigger.PROJECT_FILE_EDIT else None,
            project_data=_make_project_data(deployments=[]) if trigger is RemovalTrigger.PROJECT_FILE_EDIT else None,
            force=True,
        )

        with (
            patch("opi.manager.project.deployment_removal.create_argo_connector", return_value=_argo()),
            patch("opi.manager.project.deployment_removal.get_project_store", return_value=_store()),
            patch("opi.manager.project.deployment_removal.marking_service", AsyncMock(return_value=None)),
            patch("os.path.exists", return_value=False),
        ):
            await remove_deployment(request, DeleteProjectManager(mock_pm))

        assert order == ["services", "namespace"], order
