"""Tests for the generic after-sync observation runner and the system-service kind.

Covers tasks 8-10: the event runner commits exactly once for all services, a system
service applies to every project, and it is kept out of the wizard's service picker.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.forms.visualizers.providers import ServiceOptionsProvider
from opi.services.catalog.base import ObservationOutcome, Service
from opi.services.catalog.events import on
from opi.services.deployment_observation import run_after_sync_observation
from opi.services.registry import get_service
from opi.services.services_enums import ActionEvent, ServiceType


class _DummyHookService(Service):
    """An AFTER_SYNC listener with no ServiceType, so it overrides applies_to too."""

    def __init__(self, tag: str) -> None:
        self._tag = tag

    def applies_to(self, project_data: dict, deployment_name: str) -> bool:
        return True

    @on(ActionEvent.AFTER_SYNC)
    async def touch(self, ctx) -> list[ObservationOutcome]:
        ctx.project_data.setdefault("_touched", []).append(self._tag)
        return [ObservationOutcome(project_data_changed=True, requeue_refresh=True)]


class TestRunAfterSyncObservation:
    @patch("opi.services.deployment_observation.get_prefixed_namespace", return_value="rig-prd-ns")
    @patch("opi.manager.project_manager.ProjectManager")
    @patch("opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock)
    @patch("opi.services.deployment_observation.listeners")
    @pytest.mark.asyncio
    async def test_two_listeners_produce_exactly_one_commit(self, mock_scan, mock_git, mock_pm_cls, mock_prefix):
        project_data = {"deployments": [{"name": "prod", "namespace": "ns", "cluster": "local", "components": []}]}
        mock_git.return_value = (project_data, "proj.yaml")
        mock_scan.return_value = [_DummyHookService("a"), _DummyHookService("b")]
        mock_pm = MagicMock()
        mock_pm.save_and_commit_project = AsyncMock()
        mock_pm_cls.return_value = mock_pm

        result = await run_after_sync_observation("proj", "prod", {})

        # Both handlers ran and mutated the shared project_data...
        assert project_data["_touched"] == ["a", "b"]
        # ...but the runner committed exactly once for all outcomes together.
        mock_pm.save_and_commit_project.assert_called_once()
        assert result.committed is True
        assert result.requeue_refresh is True

    @patch("opi.services.deployment_observation.get_prefixed_namespace", return_value="rig-prd-ns")
    @patch("opi.manager.project_manager.ProjectManager")
    @patch("opi.services.resource_tuning_service.get_project_data_from_git", new_callable=AsyncMock)
    @patch("opi.services.deployment_observation.listeners", return_value=[])
    @pytest.mark.asyncio
    async def test_no_listeners_no_commit(self, mock_scan, mock_git, mock_pm_cls, mock_prefix):
        result = await run_after_sync_observation("proj", "prod", {})
        assert result.committed is False
        mock_git.assert_not_called()
        mock_pm_cls.assert_not_called()


class TestResourceTuningCeilingMessage:
    """The refusal at the growth ceiling gets its own, honest message.

    "auto-tune could not determine new limits" is misleading there: a limit WAS
    determined, it was refused for being past the ceiling. The old text sent people
    looking for missing metrics.
    """

    @staticmethod
    def _project(override_limit: str) -> dict:
        return {
            "name": "my-project",
            "components": [
                {
                    "name": "api",
                    "resources": {
                        "requests": {"memory": "45Mi", "cpu": "50m"},
                        "limits": {"memory": "45Mi", "cpu": "1000m"},
                    },
                }
            ],
            "deployments": [
                {
                    "name": "production",
                    "namespace": "my-project",
                    "cluster": "odcn-production",
                    "components": [
                        {
                            "reference": "api",
                            "resources": {
                                "requests": {"memory": override_limit},
                                "limits": {"memory": override_limit},
                            },
                        }
                    ],
                }
            ],
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("override_limit", "expect_ceiling_message"),
        [("360Mi", True), ("90Mi", False)],
    )
    async def test_message_names_the_ratio_at_the_ceiling(self, override_limit, expect_ceiling_message):
        from opi.services.catalog.base import ComponentHealth, DeploymentObservationContext
        from opi.services.registry import get_service

        service = get_service(ServiceType.RESOURCE_TUNING)
        ctx = DeploymentObservationContext(
            project_name="my-project",
            deployment_name="production",
            project_data=self._project(override_limit),
            cluster="odcn-production",
            namespace="rig-prd-my-project",
            component_health={"api": ComponentHealth(oom_detected=True)},
        )

        with patch(
            "opi.services.resource_tuning_service.apply_resource_tuning",
            new_callable=AsyncMock,
        ) as mock_apply:
            mock_apply.return_value = ([], ["api"])
            outcomes = await service.tune_after_oom(ctx)

        assert len(outcomes) == 1
        message = outcomes[0].failures[0]
        if expect_ceiling_message:
            assert "auto-tune ceiling" in message.lower()
            assert "360Mi" in message
            assert "45Mi" in message
            assert "by hand" in message
        else:
            assert message.endswith("but auto-tune could not determine new limits")

    @pytest.mark.asyncio
    async def test_opt_out_above_the_ceiling_does_not_get_the_ceiling_message(self):
        """Above the factor, but tuning is switched off: that is the reason, not the ceiling.

        The ratio on its own does not explain why no change came out. A component with
        ``auto-tune-resources: false`` returns before the ceiling is ever evaluated,
        so blaming the ceiling would point at the wrong knob.
        """
        from opi.services.catalog.base import ComponentHealth, DeploymentObservationContext
        from opi.services.registry import get_service

        project_data = self._project("360Mi")
        project_data["deployments"][0]["components"][0]["auto-tune-resources"] = False

        service = get_service(ServiceType.RESOURCE_TUNING)
        ctx = DeploymentObservationContext(
            project_name="my-project",
            deployment_name="production",
            project_data=project_data,
            cluster="odcn-production",
            namespace="rig-prd-my-project",
            component_health={"api": ComponentHealth(oom_detected=True)},
        )

        with patch(
            "opi.services.resource_tuning_service.apply_resource_tuning",
            new_callable=AsyncMock,
        ) as mock_apply:
            mock_apply.return_value = ([], ["api"])
            outcomes = await service.tune_after_oom(ctx)

        message = outcomes[0].failures[0]
        assert "ceiling" not in message.lower()
        assert message.endswith("but auto-tune could not determine new limits")


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
