"""The sleep-mode deployment action: the "wake" button in the UI.

Bound onto the service's ServiceDefinition (see ``__init__.py``) so the generic
deployment-actions template collects it like any other service's buttons, instead of the
template deriving the condition itself.
"""

from __future__ import annotations

from typing import Any

from opi.services.catalog.sleep_mode.state import STATE_AWAKE, read
from opi.services.services import DeploymentAction


def sleep_actions(project_data: dict[str, Any], deployment_name: str) -> list[DeploymentAction]:
    """One button, shown only when the deployment is not awake: wake it now.

    The endpoint is the session-authenticated web route; the button is hidden while the
    deployment is awake (nothing to wake).
    """
    state = read(project_data, deployment_name).state
    if state == STATE_AWAKE:
        return []
    project_name = project_data.get("name", "")
    return [
        DeploymentAction(
            label="Applicatie wekken",
            icon="uitvoering",
            kind="primary",
            endpoint=f"/projects/{project_name}/deployments/{deployment_name}/wake",
            # Quote-free: rendered inline in a single-quoted JS string in the template.
            confirm_message=f"Deployment {deployment_name} wekken uit de slaapstand?",
            visible=True,
        )
    ]
