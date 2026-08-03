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

    memory: str = "256Mi"
    cpu: str = "100m"


class LimitsQuantities(BaseModel):
    """CPU/memory *limits* (defaults differ from requests, as in DEFAULT_CONFIG)."""

    model_config = ConfigDict(extra="forbid")

    memory: str = "512Mi"
    cpu: str = "500m"


class DatabaseResources(BaseModel):
    """requests/limits pair, mirroring DEFAULT_CONFIG['resources']."""

    model_config = ConfigDict(extra="forbid")

    requests: RequestsQuantities = RequestsQuantities()
    limits: LimitsQuantities = LimitsQuantities()


class DedicatedPostgresFields(BaseModel):
    """The CNPG-cluster settings for a project-owned PostgreSQL database.

    Mixed into both ``NamespacePostgresConfig`` (the standalone service) and the
    ``scope: project`` member of ``postgresql-database``'s config. Field defaults
    reproduce ``DatabaseManager.DEFAULT_CONFIG`` so an existing (untyped) config
    validates to the same merged result the old ``dict.get()`` merge produced.
    """

    # CNPG-compatible image (has a postgres user with UID 26).
    image: str = "ghcr.io/cloudnative-pg/postgresql:17"
    # Optional named registry the image is pulled from (see project 'registries').
    registry: str | None = None
    instances: int = Field(default=1, ge=1)
    # Kubernetes storage quantity, e.g. "10Gi". Not pattern-constrained to match
    # today's behaviour (required, non-empty); tighten via a future version.
    storage: str = "10Gi"
    privileges: list[DatabasePrivilege] = Field(default_factory=list)
    postInitSQL: list[str] = Field(default_factory=list)
    resources: DatabaseResources = DatabaseResources()

    @field_validator("privileges", mode="before")
    @classmethod
    def _uppercase_privileges(cls, value: object) -> object:
        # Preserve the old case-insensitive behaviour (privilege.upper()): SQL role
        # keywords are case-insensitive, so a hand-edited lowercase value still
        # validates and is normalised to the canonical uppercase form.
        if isinstance(value, list):
            return [item.upper() if isinstance(item, str) else item for item in value]
        return value
