"""The deployment-card button for the per-deployment cross-domain patch (RC-42).

The service owns its own button. A ``{% if 'cross-domain-access' in ... %}`` in the general
project-details templates would be this service's knowledge written down somewhere it cannot
be kept true (``test_the_general_templates_name_no_service`` refuses exactly that), so the
condition lives here, next to the form it opens.

The button loads the modal-wizard's first step -- the same URL ``openEditModal`` fetches --
into the shared modal shell, so the patch form is opened by the same route as every other
wizard modal.
"""

from __future__ import annotations

from typing import Any

from opi.services.services import DeploymentAction, service_entry_name
from opi.services.services_enums import ServiceType


def cross_domain_actions(project_data: dict[str, Any], deployment_name: str) -> list[DeploymentAction]:
    """The patch button, or nothing when this project does not use cross-domain access."""
    names = [service_entry_name(entry) for entry in project_data.get("services") or []]
    if ServiceType.CROSS_DOMAIN_ACCESS.value not in names:
        return []

    deployments = project_data.get("deployments") or []
    index = next(
        (
            i
            for i, deployment in enumerate(deployments)
            if isinstance(deployment, dict) and deployment.get("name") == deployment_name
        ),
        None,
    )
    if index is None:
        return []

    project_name = project_data.get("name", "")
    return [
        DeploymentAction(
            label="Cross-domain toegang",
            icon="netwerk",
            kind="secondary",
            modal_endpoint=(f"/projects/{project_name}/modal-wizard/modal-edit-cross-domain-deployment-{index}"),
            modal_title=f"Cross-domain toegang - {deployment_name}",
            visible=True,
        )
    ]
