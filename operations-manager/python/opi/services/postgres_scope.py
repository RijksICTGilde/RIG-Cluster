"""Where a project's PostgreSQL database is placed: shared instance or dedicated cluster.

Two services can ask for a dedicated (project-owned CNPG) cluster today:

* ``namespace-postgresql-database`` -- the standalone service, destined for removal
  once its projects migrate (plan RC-17, decision 10.1);
* ``postgresql-database`` with ``scope: project`` -- the same placement expressed as a
  field on the one PostgreSQL service.

Both mean the same thing to every consumer (a dedicated cluster in the project's
infrastructure namespace), so the "which placement?" decision and "read the cluster
config" live here, once, instead of being re-derived at each gate. ``scope`` defaults
to ``shared``, so a project that sets nothing gets the shared instance exactly as
before.
"""

from __future__ import annotations

from typing import Any, Literal

from opi.services.project import Project
from opi.services.registry import get_service
from opi.services.services_enums import ServiceType

PostgresScope = Literal["shared", "project"]


def postgres_scope(project_data: dict[str, Any]) -> PostgresScope | None:
    """The effective placement of this project's PostgreSQL database.

    ``"project"`` for a dedicated cluster, ``"shared"`` for the shared instance, or
    ``None`` when the project uses no PostgreSQL service at all. The legacy
    ``namespace-postgresql-database`` service always means ``"project"``.
    """
    view = Project(project_data)
    if view.uses_service(ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value):
        return "project"
    if view.uses_service(ServiceType.POSTGRESQL_DATABASE.value):
        model = view.service_config_model(ServiceType.POSTGRESQL_DATABASE.value)
        if model is None:
            return "shared"
        # PostgresqlDatabaseProjectConfig is a RootModel; the member carries `scope`.
        return model.root.scope  # type: ignore[attr-defined,no-any-return]
    return None


def project_uses_dedicated_postgres(project_data: dict[str, Any]) -> bool:
    """Whether the project wants a dedicated (project-scoped) PostgreSQL cluster."""
    return postgres_scope(project_data) == "project"


def get_dedicated_postgres_config(project_data: dict[str, Any]) -> dict[str, Any]:
    """The validated CNPG-cluster config (image/instances/storage/...) for a
    project-scoped database, read from whichever service declares it.

    Returns the same field set regardless of source (``namespace-postgresql-database``
    or ``postgresql-database`` with ``scope: project``), so downstream cluster
    generation does not care which service asked. Must only be called when
    :func:`project_uses_dedicated_postgres` is True.
    """
    view = Project(project_data)
    if view.uses_service(ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value):
        provider = get_service(ServiceType.NAMESPACE_POSTGRESQL_DATABASE)
        return provider.validate_config(
            view.service_config(ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value)
        ).model_dump(mode="json")
    model = view.service_config_model(ServiceType.POSTGRESQL_DATABASE.value)
    if model is None:
        raise ValueError("get_dedicated_postgres_config called for a project without a dedicated PostgreSQL database")
    # RootModel member is ProjectScopeConfig (the CNPG fields + scope); drop the scope
    # tag so the result matches NamespacePostgresConfig's field set exactly.
    config = model.root.model_dump(mode="json")  # type: ignore[attr-defined]
    config.pop("scope", None)
    return config
