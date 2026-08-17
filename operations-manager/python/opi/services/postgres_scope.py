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


def database_generation_service_type(project_data: dict[str, Any]) -> str:
    """The deployment-level services entry that carries this project's database generation.

    A project expresses "dedicated PostgreSQL" through either of two service names, and
    the generation is stored under whichever one the project actually declares. Three
    copies of this if/else had grown (restore_router twice, database_manager); they must
    agree or a restore writes the generation under a name nobody reads back.
    """
    if Project(project_data).uses_service(ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value):
        return ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value
    return ServiceType.POSTGRESQL_DATABASE.value


def schema_is_marked(entry: dict[str, Any]) -> bool:
    """Whether a raw schema entry is marked for deletion.

    Reads the entry as it stands in the project file, where the key is the alias
    ``marked-for-deletion``; the field name ``marked_for_deletion`` is accepted too
    because ``SchemaEntry`` accepts both and a file may carry either.
    """
    return bool(entry.get("marked-for-deletion") or entry.get("marked_for_deletion"))


def get_postgres_schemas(project_data: dict[str, Any], include_marked: bool = False) -> list[dict[str, Any]]:
    """The project-wide extra schemas ({postfix, description, ...}) for postgresql-database.

    Schemas are strictly project-wide (RC-17 decision 10.5) and live on the
    ``postgresql-database`` service config, so the same list applies to every
    deployment. Empty for the legacy ``namespace-postgresql-database`` service or a
    project without PostgreSQL. By default schemas marked for deletion are excluded, so
    provisioning stops exposing them while leaving the schema (and its data) in place.
    """
    view = Project(project_data)
    if not view.uses_service(ServiceType.POSTGRESQL_DATABASE.value):
        return []
    model = view.service_config_model(ServiceType.POSTGRESQL_DATABASE.value)
    if model is None:
        return []
    schemas = [entry.model_dump() for entry in model.root.schemas]  # type: ignore[attr-defined]
    if include_marked:
        return schemas
    return [entry for entry in schemas if not entry.get("marked_for_deletion")]


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
    # RootModel member is ProjectScopeConfig (the CNPG fields + scope + schemas); drop
    # the non-cluster fields so the result matches NamespacePostgresConfig's field set
    # exactly (schemas are handled separately, not by the cluster generator).
    config = model.root.model_dump(mode="json")  # type: ignore[attr-defined]
    config.pop("scope", None)
    config.pop("schemas", None)
    return config
