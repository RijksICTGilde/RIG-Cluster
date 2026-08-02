"""Tests for the generic after-sync observation runner and the system-service kind.

Covers tasks 8-10: the hook runner commits exactly once for all services, a system
service applies to every project, and it is kept out of the wizard's service picker.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.forms.visualizers.providers import ServiceOptionsProvider
from opi.services.catalog.base import ObservationOutcome, Service
from opi.services.deployment_observation import run_after_sync_observation
from opi.services.registry import get_service
from opi.services.services_enums import ServiceType


class _DummyHookService(Service):
    """A hook participant with no ServiceType, so it overrides applies_to too."""

    def __init__(self, tag: str) -> None:
        self._tag = tag

    def applies_to(self, project_data: dict, deployment_name: str) -> bool:
        return True

    async def observe_deployment(self, ctx) -> ObservationOutcome:
        ctx.project_data.setdefault("_touched", []).append(self._tag)
        return ObservationOutcome(project_data_changed=True, requeue_refresh=True)


class TestRunAfterSyncObservation:
    @patch("opi.services.deployment_observation.get_prefixed_namespace", return_value="rig-prd-ns")
    @patch("opi.manager.project_manager.ProjectManager")
    @patch("opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock)
    @patch("opi.services.deployment_observation.services_for_hook")
    @pytest.mark.asyncio
    async def test_two_hooks_produce_exactly_one_commit(self, mock_scan, mock_git, mock_pm_cls, mock_prefix):
        project_data = {"deployments": [{"name": "prod", "namespace": "ns", "cluster": "local", "components": []}]}
        mock_git.return_value = (project_data, "proj.yaml")
        mock_scan.return_value = [_DummyHookService("a"), _DummyHookService("b")]
        mock_pm = MagicMock()
        mock_pm.save_and_commit_project = AsyncMock()
        mock_pm_cls.return_value = mock_pm

        result = await run_after_sync_observation("proj", "prod", {})

        # Both hooks ran and mutated the shared project_data...
        assert project_data["_touched"] == ["a", "b"]
        # ...but the runner committed exactly once for all outcomes together.
        mock_pm.save_and_commit_project.assert_called_once()
        assert result.committed is True
        assert result.requeue_refresh is True

    @patch("opi.services.deployment_observation.get_prefixed_namespace", return_value="rig-prd-ns")
    @patch("opi.manager.project_manager.ProjectManager")
    @patch("opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock)
    @patch("opi.services.deployment_observation.services_for_hook", return_value=[])
    @pytest.mark.asyncio
    async def test_no_hooks_no_commit(self, mock_scan, mock_git, mock_pm_cls, mock_prefix):
        result = await run_after_sync_observation("proj", "prod", {})
        assert result.committed is False
        mock_git.assert_not_called()
        mock_pm_cls.assert_not_called()


class TestSystemServiceKind:
    def test_resource_tuning_applies_to_project_without_services(self):
        svc = get_service(ServiceType.RESOURCE_TUNING)
        assert svc.applies_to({"services": [], "components": []}, "prod") is True

    def test_system_services_absent_from_service_picker(self):
        values = {option["value"] for option in ServiceOptionsProvider().get_options()}
        assert "resource-tuning" not in values
        assert "platform" not in values
        # A normal user-selectable service is still shown.
        assert "keycloak" in values
