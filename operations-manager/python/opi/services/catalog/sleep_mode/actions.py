"""The sleep-mode deployment actions: the wake/sleep toggle in the UI.

Bound onto the service's ServiceDefinition (see ``__init__.py``) so the generic
deployment-actions template collects it like any other service's buttons, instead of the
template deriving the condition itself. Exactly one button is shown, depending on the
deployment's sleep state: "Deployment slapen" while awake, "Applicatie wekken" otherwise.
"""

from __future__ import annotations

from typing import Any

from opi.services.catalog.sleep_mode import config as sleep_config
from opi.services.catalog.sleep_mode.config import SleepModeConfigError
from opi.services.catalog.sleep_mode.state import STATE_AWAKE, read
from opi.services.services import DeploymentAction


def _find_deployment(project_data: dict[str, Any], deployment_name: str) -> dict[str, Any] | None:
    for deployment in project_data.get("deployments", []) or []:
        if deployment.get("name") == deployment_name:
            return deployment
    return None


def sleep_actions(project_data: dict[str, Any], deployment_name: str) -> list[DeploymentAction]:
    """The wake/sleep toggle for a deployment, or no button when out of scope.

    Shown only when sleep-mode is enabled for the deployment's cluster and the deployment
    is in the ``match`` scope -- otherwise the buttons would do nothing when clicked. While
    awake, offer a manual sleep; while sleeping or waking, offer a wake. Both target the
    session-authenticated web routes (admin/owner), so a manual sleep and a manual wake are
    the two halves of one toggle.
    """
    deployment = _find_deployment(project_data, deployment_name)
    if deployment is None:
        return []
    try:
        config = sleep_config.load(project_data, deployment.get("cluster", ""))
    except SleepModeConfigError:
        # A broken sleep-mode config should not crash the details page; show no button.
        return []
    if config is None or not config.matches(deployment_name):
        return []

    project_name = project_data.get("name", "")
    base = f"/projects/{project_name}/deployments/{deployment_name}"
    if read(project_data, deployment_name).state == STATE_AWAKE:
        return [
            DeploymentAction(
                label="Deployment slapen",
                icon="klok",
                kind="secondary",
                endpoint=f"{base}/sleep",
                # Quote-free: rendered inline in a single-quoted JS string in the template.
                confirm_message=f"Deployment {deployment_name} handmatig in slaapstand zetten?",
                visible=True,
            )
        ]
    return [
        DeploymentAction(
            label="Applicatie wekken",
            icon="uitvoering",
            kind="primary",
            endpoint=f"{base}/wake",
            confirm_message=f"Deployment {deployment_name} wekken uit de slaapstand?",
            visible=True,
        )
    ]
