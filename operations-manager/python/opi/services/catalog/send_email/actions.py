"""The deployment button of send-email: that deployment's sender and daily budget."""

from __future__ import annotations

from typing import Any

from opi.services.services import DeploymentAction, deployment_modal_action
from opi.services.services_enums import ServiceType


def send_email_actions(project_data: dict[str, Any], deployment_name: str) -> list[DeploymentAction]:
    action = deployment_modal_action(
        project_data,
        deployment_name,
        service=ServiceType.SEND_EMAIL,
        modal_prefix="modal-edit-send-email-deployment-",
        label="E-mail",
        icon="envelop",
    )
    return [action] if action else []
