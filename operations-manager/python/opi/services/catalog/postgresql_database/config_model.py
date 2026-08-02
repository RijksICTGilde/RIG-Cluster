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

from typing import Annotated, Literal

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


class SharedScopeConfig(BaseModel):
    """``scope: shared`` -- a database on the shared cluster instance (the default).

    Carries no CNPG-cluster fields: ``extra="forbid"`` means putting ``storage`` or
    ``instances`` here fails validation with a message that names the offending key.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    scope: Literal["shared"] = "shared"


class ProjectScopeConfig(DedicatedPostgresFields):
    """``scope: project`` -- one dedicated CNPG cluster per project, shared by all its
    deployments. Carries the same CNPG-cluster fields as
    ``namespace-postgresql-database``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    scope: Literal["project"]


class PostgresqlDatabaseProjectConfig(RootModel):
    """Project-layer config: a ``scope``-discriminated union of the two shapes above.

    ``scope`` defaults to ``shared`` when absent, so an existing project that sets no
    scope validates as a shared database and its behaviour is unchanged.
    """

    root: Annotated[SharedScopeConfig | ProjectScopeConfig, Field(discriminator="scope")]

    @model_validator(mode="before")
    @classmethod
    def _default_scope(cls, data: object) -> object:
        # A discriminated union needs the tag present; default it to "shared" so a
        # config block that omits scope (every project today) resolves to the shared
        # member instead of failing to discriminate.
        if isinstance(data, dict) and "scope" not in data:
            return {**data, "scope": "shared"}
        return data
