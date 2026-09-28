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

from opi.services.services import DeploymentAction, deployment_modal_action
from opi.services.services_enums import ServiceType


def cross_domain_actions(project_data: dict[str, Any], deployment_name: str) -> list[DeploymentAction]:
    """The patch button, or nothing when this project does not use cross-domain access."""
    action = deployment_modal_action(
        project_data,
        deployment_name,
        service=ServiceType.CROSS_DOMAIN_ACCESS,
        modal_prefix="modal-edit-cross-domain-deployment-",
        label="Cross-domain toegang",
        icon="netwerk",
    )
    return [action] if action else []
