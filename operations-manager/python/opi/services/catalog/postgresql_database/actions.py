"""The deployment-card buttons of postgresql-database: the shared database buttons plus
the per-deployment connection limit (RC-201)."""

from __future__ import annotations

from typing import Any

from opi.services.catalog.shared.postgres_pages import database_actions
from opi.services.services import DeploymentAction, deployment_modal_action
from opi.services.services_enums import ServiceType


def postgresql_database_actions(project_data: dict[str, Any], deployment_name: str) -> list[DeploymentAction]:
    actions = database_actions(project_data, deployment_name)
    limit = deployment_modal_action(
        project_data,
        deployment_name,
        service=ServiceType.POSTGRESQL_DATABASE,
        modal_prefix="modal-edit-postgresql-deployment-",
        label="Connectielimiet",
        icon="database",
    )
    return [*actions, limit] if limit else actions
