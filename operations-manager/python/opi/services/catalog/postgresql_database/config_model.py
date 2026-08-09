"""Typed config models for the ``postgresql-database`` service.

This service carries config at two layers, with two different shapes:

* **Project layer** (``services[{postgresql-database}].config``): the user-facing
  ``scope`` decision plus, for a project-scoped database, the CNPG-cluster settings.
  Modelled as a Pydantic *discriminated union* on ``scope`` so that a field that only
  belongs to a dedicated cluster (``storage``, ``instances``, ...) fails loudly on a
  ``shared`` database instead of being silently ignored. ``scope`` defaults to
  ``shared``, so a bare ``postgresql-database`` entry, or one with no ``scope`` key,
  keeps today's behaviour exactly.
* **Deployment layer** (``deployments[*].services[{postgresql-database}].config``):
  clone state, written and read by ``opi/manager/revision_manager.py``, not by a user.

``config_model_for(layer)`` in the service picks the right one per layer.

The CNPG field set lives in ``catalog/shared/postgres.py`` (shared with
``namespace-postgresql-database``); this service only decides that ``scope: project``
uses it.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from opi.services.catalog.shared.postgres import DedicatedPostgresFields
from opi.services.catalog.shared.revisions import CloneState


class PostgresqlDatabaseConfig(CloneState):
    """Clone state for a PostgreSQL database, carried on the deployment layer.

    The shape is shared with ``minio-storage`` (see ``catalog/shared/revisions.py``);
    this service only decides that it uses it. The generation lives in the *database
    name*, not the schema name, which is exactly why extra schemas (RC-17) can share a
    database without disturbing generations, clones or backups.
    """


def schema_postfix_field() -> Any:
    """The ``postfix`` field, as a factory so the API can carry the same definition.

    ``POST .../schemas`` takes a postfix and a description and nothing else, so it cannot
    simply reuse ``SchemaEntry`` as its body -- but what a valid postfix looks like has to
    stay one definition, or the endpoint and the stored model drift into two rules. A
    factory rather than a shared ``FieldInfo`` instance: each model gets its own.
    """
    # Lowercase, digits, underscore, starting with a letter -- a safe identifier and
    # (uppercased) a safe env-variable suffix.
    return Field(
        pattern=r"^[a-z][a-z0-9_]*$",
        description=(
            "Short name of the extra schema. The full name becomes {project}_{deployment}_{postfix} and its "
            "connection details are exposed as DATABASE_SCHEMA_{POSTFIX}."
        ),
    )


def schema_description_field() -> Any:
    """The ``description`` field, shared with the API for the same reason."""
    return Field(default="", description="What this schema is for, for whoever reads the project file.")


class SchemaEntry(BaseModel):
    """One extra schema, project-wide (RC-17 decision 10.5).

    ``postfix`` is the user-chosen short name; the full schema becomes
    ``{project}_{deployment}_{postfix}`` per deployment and an env variable
    ``DATABASE_SCHEMA_{POSTFIX}`` is exposed. The pattern keeps the postfix a valid
    identifier fragment; uniqueness within the list, the 63-char full-name limit and
    variable-name collisions are enforced at save time (they need the project and
    deployment names, which this model does not have).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    postfix: str = schema_postfix_field()
    description: str = schema_description_field()
    # Removing a schema from the list marks it rather than dropping it, so a schema (and
    # its data) is never silently discarded on a routine save (RC-17 section 6). The
    # provisioner leaves a marked schema in place and stops exposing its variable.
    marked_for_deletion: bool = Field(
        default=False,
        alias="marked-for-deletion",
        description=(
            "Marks the schema as no longer wanted instead of dropping it: the schema and its data stay, and "
            "its variable is no longer exposed."
        ),
    )


class SharedScopeConfig(BaseModel):
    """``scope: shared`` -- a database on the shared cluster instance (the default).

    Carries no CNPG-cluster fields: ``extra="forbid"`` means putting ``storage`` or
    ``instances`` here fails validation with a message that names the offending key.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    scope: Literal["shared"] = Field(
        default="shared", description="A database on the shared cluster instance; the default."
    )
    schemas: list[SchemaEntry] = Field(
        default_factory=list, description="Extra schemas alongside the default one, shared by every deployment."
    )


class ProjectScopeConfig(DedicatedPostgresFields):
    """``scope: project`` -- one dedicated CNPG cluster per project, shared by all its
    deployments. Carries the same CNPG-cluster fields as
    ``namespace-postgresql-database``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    scope: Literal["project"] = Field(
        description="One dedicated PostgreSQL cluster for this project, shared by all its deployments."
    )
    schemas: list[SchemaEntry] = Field(
        default_factory=list, description="Extra schemas alongside the default one, shared by every deployment."
    )


class PostgresqlDatabaseProjectConfig(RootModel):
    """Project-layer config: a ``scope``-discriminated union of the two shapes above.

    ``scope`` defaults to ``shared`` when absent, so an existing project that sets no
    scope validates as a shared database and its behaviour is unchanged.
    """

    root: Annotated[
        SharedScopeConfig | ProjectScopeConfig,
        Field(
            discriminator="scope",
            description=(
                "The project's database configuration, by scope: 'shared' (a database on the shared instance) "
                "or 'project' (a dedicated cluster). Absent scope reads as 'shared'."
            ),
        ),
    ]

    @model_validator(mode="before")
    @classmethod
    def _default_scope(cls, data: object) -> object:
        # A discriminated union needs the tag present; default it to "shared" so a
        # config block that omits scope (every project today) resolves to the shared
        # member instead of failing to discriminate.
        if isinstance(data, dict) and "scope" not in data:
            return {**data, "scope": "shared"}
        return data
