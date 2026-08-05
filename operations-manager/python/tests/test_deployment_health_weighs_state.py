"""The health check weighs what the services report before it judges (RC-28 step 3).

Two things this locks, both from the same live incident:

* A component with no application pods is explained by the service that scaled it to
  zero ("slaapt"), instead of being reported as a rollout that is still starting. A
  deployment that is NOT asleep and has no pods still reports that, because nobody
  claimed responsibility for the silence.
* ``describe_components_waiting`` matched pods on the bare ``app`` label. Sleep-mode's
  waker carries that label on purpose (it takes over the component's Service), so this
  function read the WAKER's ImagePullBackOff as the component's reason -- literally the
  message the incident was about, and the half of the bug that the label fix in
  ``4b86aed7`` did not reach.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.services.catalog.base import SERVICE_ROLE_LABEL_KEY
from opi.services.catalog.sleep_mode.state import STATE_SLEEPING, SleepState, write
from opi.services.deployment_state import DeploymentState, collect_deployment_state
from opi.services.oom_watcher import describe_components_waiting

_APP = "productie-frontend"


def _pod(name: str, *, role: str | None = None, waiting_reason: str | None = None) -> dict:
    labels = {"app": _APP}
    if role:
        labels[SERVICE_ROLE_LABEL_KEY] = role
    container: dict = {"name": "app", "ready": False, "state": {}}
    if waiting_reason:
        container["state"] = {"waiting": {"reason": waiting_reason, "message": "zad-waker:latest not found"}}
    else:
        container["state"] = {"running": {}}
    return {
        "metadata": {"name": name, "labels": labels},
        "status": {"phase": "Running", "containerStatuses": [container]},
    }


def _kubectl_returning(pods: list[dict]) -> MagicMock:
    connector = MagicMock()
    connector.run_command = AsyncMock(return_value=(json.dumps({"items": pods}), "", 0))
    return connector


def _sleeping_project() -> dict:
    project_data: dict = {
        "name": "productie",
        "services": ["sleep-mode"],
        "components": [{"name": "frontend"}],
        "deployments": [{"name": "productie", "cluster": "odcn-production", "namespace": "productie"}],
    }
    write(project_data, "productie", SleepState(state=STATE_SLEEPING))
    return project_data


@pytest.mark.asyncio
class TestAbsentPodsAreExplainedByTheServiceThatCausedThem:
    @patch("opi.services.oom_watcher.KubectlConnector")
    async def test_a_sleeping_deployment_reports_that_it_sleeps(self, mock_kubectl) -> None:
        mock_kubectl.isConnected = True
        mock_kubectl.return_value = _kubectl_returning([])
        state = collect_deployment_state(_sleeping_project(), "productie")

        statuses = await describe_components_waiting("rig-productie", [_APP], {_APP: "frontend"}, state)

        assert len(statuses) == 1
        ref, reason = statuses[0]
        assert ref == "frontend"
        assert "slaapt" in reason
        assert "pods worden aangemaakt" not in reason

    @patch("opi.services.oom_watcher.KubectlConnector")
    async def test_a_deployment_that_is_not_sleeping_still_reports_missing_pods(self, mock_kubectl) -> None:
        """The other half of the plan's step-3 check: no claim, no excuse."""
        mock_kubectl.isConnected = True
        mock_kubectl.return_value = _kubectl_returning([])

        statuses = await describe_components_waiting("rig-productie", [_APP], {_APP: "frontend"}, DeploymentState())

        assert statuses == [("frontend", "pods worden aangemaakt")]


@pytest.mark.asyncio
class TestOnlyTheApplicationsOwnPodsAreDescribed:
    @patch("opi.services.oom_watcher.KubectlConnector")
    async def test_a_service_owned_pod_is_not_read_as_the_component(self, mock_kubectl) -> None:
        """The waker's ImagePullBackOff must not become "frontend: image ophalen mislukt"."""
        mock_kubectl.isConnected = True
        mock_kubectl.return_value = _kubectl_returning(
            [_pod("productie-frontend-waker-abc", role="waker", waiting_reason="ImagePullBackOff")]
        )
        state = collect_deployment_state(_sleeping_project(), "productie")

        statuses = await describe_components_waiting("rig-productie", [_APP], {_APP: "frontend"}, state)

        assert len(statuses) == 1
        _, reason = statuses[0]
        assert "image ophalen mislukt" not in reason
        assert "slaapt" in reason

    @patch("opi.services.oom_watcher.KubectlConnector")
    async def test_a_real_application_pod_is_still_described(self, mock_kubectl) -> None:
        """The exclusion is about the label, not about sleeping: an application pod with a
        problem is reported even while a service claims the deployment sleeps."""
        mock_kubectl.isConnected = True
        mock_kubectl.return_value = _kubectl_returning(
            [_pod("productie-frontend-7c9", waiting_reason="ImagePullBackOff")]
        )
        state = collect_deployment_state(_sleeping_project(), "productie")

        statuses = await describe_components_waiting("rig-productie", [_APP], {_APP: "frontend"}, state)

        assert len(statuses) == 1
        _, reason = statuses[0]
        assert "image ophalen mislukt" in reason
