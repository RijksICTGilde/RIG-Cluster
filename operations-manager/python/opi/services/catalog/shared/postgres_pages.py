"""The database-console and job-runner modals, owned by the PostgreSQL services (RC-24).

Both modals exist because the deployment has a database: the console attaches a tool to
it, and a job Pod is launched wired with the deployment's database connection
(migrations). They used to be hardcoded in ``section-deployment-actions.html.j2`` -- one
of them behind ``'postgresql-database' in service_values or 'namespace-...' in ...``,
service knowledge in the general page.

Both PostgreSQL services share this module: each returns the same buttons and the same
routers, and the collectors keep one of each, so a project that has both does not get
double buttons or double routes.
"""

from __future__ import annotations

from typing import Any

from opi.services.services import DeploymentAction, service_entry_name
from opi.services.services_enums import ServiceType

#: The services that put a database on a deployment.
DATABASE_SERVICES = (ServiceType.POSTGRESQL_DATABASE.value, ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value)


def _uses_a_database(project_data: dict[str, Any]) -> bool:
    entries = list(project_data.get("services", []) or [])
    for component in project_data.get("components", []) or []:
        entries.extend(component.get("services", []) or [])
    return any(service_entry_name(entry) in DATABASE_SERVICES for entry in entries)


def database_actions(project_data: dict[str, Any], deployment_name: str) -> list[DeploymentAction]:
    """The console and job buttons for a deployment, or nothing without a database.

    ``collect_deployment_actions`` asks every service with an ``actions_provider``, so
    the check whether the project uses a database lives here -- which is the point: the
    page no longer knows the two service names.
    """
    if not _uses_a_database(project_data):
        return []
    project_name = project_data.get("name", "")
    return [
        DeploymentAction(
            label="Databaseconsole",
            icon="database",
            kind="secondary",
            modal_endpoint=f"/projects/{project_name}/db-console/{deployment_name}/modal",
            modal_title=f"Databaseconsole - {deployment_name}",
        ),
        DeploymentAction(
            label="Job uitvoeren",
            icon="uitvoering",
            kind="secondary",
            modal_endpoint=f"/projects/{project_name}/jobs/{deployment_name}/modal",
            modal_title=f"Job uitvoeren - {deployment_name}",
        ),
    ]


class DatabasePagesMixin:
    """Mixed into both PostgreSQL services: it brings the two modals' routes.

    Imported lazily so the catalog is not pulled into the manager imports those route
    modules need; the collector mounts each distinct router once. Cooperative
    (``super()``), because these services also carry ``BackupsPageMixin``.
    """

    def web_routers(self) -> list[Any]:
        from opi.services.catalog.shared.db_console import db_console_router
        from opi.services.catalog.shared.jobs import jobs_router

        return [*super().web_routers(), db_console_router, jobs_router]  # type: ignore[misc]
