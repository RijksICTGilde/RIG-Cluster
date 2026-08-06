"""Shared building blocks for a dedicated (CNPG) PostgreSQL cluster.

These field types describe the configuration of a project-owned PostgreSQL cluster:
image, registry, instances, storage, privileges, postInitSQL and resource
requests/limits. They were introduced for ``namespace-postgresql-database`` and now
also back ``postgresql-database``'s ``scope: project`` variant (see
``postgresql_database/config_model.py``), so they live here rather than in either
service package. ``namespace-postgresql-database`` is destined for removal once the
projects that use it are migrated to ``scope: project`` (plan RC-17, decision 10.1);
keeping these blocks in ``catalog/shared`` means that removal drops the old service
without taking the field definitions with it.

Defaults reproduce the historical ``DatabaseManager.DEFAULT_CONFIG`` so validating an
existing (untyped) config through a model built on these types yields the same merged
result the old ``dict.get()`` merge produced.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DatabasePrivilege(StrEnum):
    """PostgreSQL role privileges accepted for the dedicated database user.

    Matches the allow-list previously enforced inline in DatabaseManager.
    """

    SUPERUSER = "SUPERUSER"
    NOSUPERUSER = "NOSUPERUSER"
    CREATEDB = "CREATEDB"
    NOCREATEDB = "NOCREATEDB"
    CREATEROLE = "CREATEROLE"
    NOCREATEROLE = "NOCREATEROLE"
    LOGIN = "LOGIN"
    NOLOGIN = "NOLOGIN"
    REPLICATION = "REPLICATION"
    NOREPLICATION = "NOREPLICATION"
    BYPASSRLS = "BYPASSRLS"
    NOBYPASSRLS = "NOBYPASSRLS"


class RequestsQuantities(BaseModel):
    """CPU/memory *requests*. Per-field defaults preserve the old deep-merge: a
    config overriding only ``requests.memory`` keeps the default ``cpu``."""

    model_config = ConfigDict(extra="forbid")

    memory: str = Field(default="256Mi", description="Memory the database pod requests, e.g. 256Mi.")
    cpu: str = Field(default="100m", description="CPU the database pod requests, e.g. 100m.")


class LimitsQuantities(BaseModel):
    """CPU/memory *limits* (defaults differ from requests, as in DEFAULT_CONFIG)."""

    model_config = ConfigDict(extra="forbid")

    memory: str = Field(default="512Mi", description="Memory ceiling for the database pod, e.g. 512Mi.")
    cpu: str = Field(default="500m", description="CPU ceiling for the database pod, e.g. 500m.")


class DatabaseResources(BaseModel):
    """requests/limits pair, mirroring DEFAULT_CONFIG['resources']."""

    model_config = ConfigDict(extra="forbid")

    requests: RequestsQuantities = Field(
        default=RequestsQuantities(), description="What the database pod asks for; used for scheduling."
    )
    limits: LimitsQuantities = Field(
        default=LimitsQuantities(), description="What the database pod may use at most; exceeding memory kills it."
    )


class DedicatedPostgresFields(BaseModel):
    """The CNPG-cluster settings for a project-owned PostgreSQL database.

    Mixed into both ``NamespacePostgresConfig`` (the standalone service) and the
    ``scope: project`` member of ``postgresql-database``'s config. Field defaults
    reproduce ``DatabaseManager.DEFAULT_CONFIG`` so an existing (untyped) config
    validates to the same merged result the old ``dict.get()`` merge produced.
    """

    image: str = Field(
        default="ghcr.io/cloudnative-pg/postgresql:17",
        description="PostgreSQL image for the cluster. Must be CNPG-compatible (a postgres user with UID 26).",
    )
    registry: str | None = Field(
        default=None,
        description="Named registry from the project's 'registries' list to pull the image from; none means public.",
    )
    instances: int = Field(
        default=1, ge=1, description="Number of PostgreSQL instances; more than one gives a replicated cluster."
    )
    # Not pattern-constrained, matching today's behaviour (required, non-empty).
    storage: str = Field(default="10Gi", description="Size of the database volume as a Kubernetes quantity, e.g. 10Gi.")
    privileges: list[DatabasePrivilege] = Field(
        default_factory=list,
        description="Extra PostgreSQL role privileges for the project's database user, from the allowed set.",
    )
    postInitSQL: list[str] = Field(
        default_factory=list,
        description="SQL statements run once, when the database is first created (extensions, dictionaries).",
    )
    resources: DatabaseResources = Field(
        default=DatabaseResources(), description="CPU and memory requests and limits for the database pods."
    )

    @field_validator("privileges", mode="before")
    @classmethod
    def _uppercase_privileges(cls, value: object) -> object:
        # Preserve the old case-insensitive behaviour (privilege.upper()): SQL role
        # keywords are case-insensitive, so a hand-edited lowercase value still
        # validates and is normalised to the canonical uppercase form.
        if isinstance(value, list):
            return [item.upper() if isinstance(item, str) else item for item in value]
        return value
