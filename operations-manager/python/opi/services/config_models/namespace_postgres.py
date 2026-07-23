"""Config model for the ``namespace-postgresql-database`` service (RC-5 Phase 2).

This is the first service converted to a typed config model. It faithfully mirrors
today's behaviour in ``DatabaseManager`` (the ~130 lines of ``dict.get()`` +
hand-rolled validation building ``DEFAULT_CONFIG``): same defaults, same required
fields, same privilege allow-list. Version ``1.0`` deliberately *describes reality*
rather than tightening it, so every existing project file validates unchanged; any
stricter guardrails (e.g. a storage-quantity pattern) come later as a versioned
``migrate_config`` step, never as a silent behaviour change on the current version.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class DatabasePrivilege(StrEnum):
    """PostgreSQL role privileges accepted for the namespace database user.

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


class NamespacePostgresConfig(BaseModel):
    """Typed config for a dedicated (namespace) PostgreSQL cluster.

    Field defaults reproduce ``DatabaseManager.DEFAULT_CONFIG`` so validating an
    existing (untyped) config through this model yields the same merged result the
    old ``dict.get()`` merge produced.
    """

    # extra="forbid" catches config typos (e.g. "instaces"); the field set below is
    # exactly the keys the old merge honoured, so no real project file is rejected.
    model_config = ConfigDict(extra="forbid")

    # CNPG-compatible image (has a postgres user with UID 26).
    image: str = "ghcr.io/cloudnative-pg/postgresql:17"
    # Optional named registry the image is pulled from (see project 'registries').
    registry: str | None = None
    instances: int = Field(default=1, ge=1)
    # Kubernetes storage quantity, e.g. "10Gi". Not pattern-constrained at v1.0 to
    # match today's behaviour (required, non-empty); tighten via a future version.
    storage: str = "10Gi"
    privileges: list[DatabasePrivilege] = Field(default_factory=list)
    postInitSQL: list[str] = Field(default_factory=list)
    resources: DatabaseResources = DatabaseResources()
