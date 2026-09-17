"""The deployment-card buttons of postgresql-database: the shared database buttons plus
the per-deployment connection limit (RC-201). Same shape as cross-domain-access."""

from __future__ import annotations

from typing import Any

from opi.services.catalog.shared.postgres_pages import database_actions
from opi.services.services import DeploymentAction, service_entry_name
from opi.services.services_enums import ServiceType


def postgresql_database_actions(project_data: dict[str, Any], deployment_name: str) -> list[DeploymentAction]:
    actions = database_actions(project_data, deployment_name)
    names = [service_entry_name(entry) for entry in project_data.get("services") or []]
    if ServiceType.POSTGRESQL_DATABASE.value not in names:
        return actions

    deployments = project_data.get("deployments") or []
    index = next(
        (i for i, d in enumerate(deployments) if isinstance(d, dict) and d.get("name") == deployment_name),
        None,
    )
    if index is None:
        return actions

    project_name = project_data.get("name", "")
    return [
        *actions,
        DeploymentAction(
            label="Connectielimiet",
            icon="database",
            kind="secondary",
            modal_endpoint=f"/projects/{project_name}/modal-wizard/modal-edit-postgresql-deployment-{index}",
            modal_title=f"Connectielimiet - {deployment_name}",
            visible=True,
        ),
    ]
