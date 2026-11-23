"""Shared building blocks for a dedicated (CNPG) PostgreSQL cluster.

These field types describe the configuration of a project-owned PostgreSQL cluster:
image, registry, instances, storage, privileges, postInitSQL and resource
requests/limits. Both ``namespace-postgresql-database`` and ``postgresql-database``'s
``scope: project`` variant (see ``postgresql_database/config_model.py``) use them, so they
live here rather than in either service package.

Defaults reproduce ``DatabaseManager.DEFAULT_CONFIG``, so an existing untyped config
validates to the same merged result.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from opi.utils.naming import CONTAINER_IMAGE_PATTERN

#: An identifier as PostgreSQL writes it: bare, or in double quotes because an extension name
#: may carry a hyphen (``"uuid-ossp"``, see ``_precreate_extensions_for_clone`` in the postgres
#: connector).
#:
#: The quoted shape lists its characters instead of excluding them: an excluding class lets a
#: carriage return through, and ``postgresql-cluster.yaml.jinja`` puts this value unquoted under
#: ``postInitApplicationSQL``, where every YAML line break in the name becomes a line of its
#: own -- an extra key in the ``Cluster`` next to ``imageName``.
_PG_NAME = r'(?:[A-Za-z_][A-Za-z0-9_$]*|"[A-Za-z0-9_$-]+")'

#: ``postInitSQL`` runs as SUPERUSER of the project's own CNPG cluster, so a free SQL line is a
#: superuser shell there. Only the shape the field is meant for: an extension, optionally with
#: the schema behind it.
#:
#: The keywords in capitals and not case insensitive: ``(?i:...)`` does not exist in the
#: ECMA-262 regex a JSON schema promises, and this pattern ends up in the schema fragment
#: clients read. Every known use already writes them this way.
#:
#: ``$`` and not ``\Z``: the rust engine pydantic runs on does not know ``\Z``. In that engine
#: ``$`` lets no trailing newline through; whoever starts using this pattern from the schema
#: fragment with the Python engine adds ``\Z``. Same weighing as ``UPSTREAM_PATTERN`` in
#: ``image_registries/config_model.py``.
POST_INIT_SQL_PATTERN = rf"^CREATE EXTENSION IF NOT EXISTS {_PG_NAME}(?: SCHEMA {_PG_NAME})?;?$"

PostInitStatement = Annotated[str, Field(pattern=POST_INIT_SQL_PATTERN)]


class DatabasePrivilege(StrEnum):
    """PostgreSQL role privileges accepted for the dedicated database user."""

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
    """CPU/memory *requests*. Per-field defaults, so a config overriding only
    ``requests.memory`` keeps the default ``cpu``."""

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
    reproduce ``DatabaseManager.DEFAULT_CONFIG``, so an existing untyped config validates
    to the same merged result.
    """

    # A closed list of allowed images (a ChoiceSetting) is impossible: production runs its own
    # CNPG image with its own tag, and features/namespace-postgresql-database.md explicitly
    # allows private images. So the SHAPE, with the character set an image reference has
    # everywhere in a project file.
    image: str = Field(
        default="ghcr.io/cloudnative-pg/postgresql:17",
        min_length=1,
        description=(
            "PostgreSQL image for the cluster. Must be CNPG-compatible (a postgres user with UID 26). "
            "Letters, digits and . _ : / @ - only, starting with a letter or digit."
        ),
    )
    registry: str | None = Field(
        default=None,
        description="Named registry from the project's 'registries' list to pull the image from; none means public.",
    )
    instances: int = Field(
        default=1, ge=1, description="Number of PostgreSQL instances; more than one gives a replicated cluster."
    )
    # The bounds live in POSTGRES_STORAGE (shared/postgres_settings.py), not here: a bound on a
    # service field is a declaration, not an ``le=`` on the model.
    # 1Gi and not more: a starting size, and a database volume can grow.
    storage: str = Field(default="1Gi", description="Size of the database volume as a Kubernetes quantity, e.g. 1Gi.")
    privileges: list[DatabasePrivilege] = Field(
        default_factory=list,
        description="Extra PostgreSQL role privileges for the project's database user, from the allowed set.",
    )
    postInitSQL: list[PostInitStatement] = Field(
        default_factory=list,
        description=(
            "Extensies die de database nodig heeft, elk als "
            "'CREATE EXTENSION IF NOT EXISTS <naam> [SCHEMA <schema>];'. Een andere vorm wordt "
            "geweigerd: deze statements draaien als superuser van het cluster."
        ),
    )
    resources: DatabaseResources = Field(
        default=DatabaseResources(), description="CPU and memory requests and limits for the database pods."
    )

    @field_validator("image")
    @classmethod
    def _image_charset(cls, value: str) -> str:
        # A ``field_validator`` and not ``pattern=``: pydantic checks with the Rust regex
        # engine, which does not know ``\Z``, and this character set would then get two texts.
        if not re.match(CONTAINER_IMAGE_PATTERN, value):
            raise ValueError(
                "Image bevat niet-toegestane tekens: gebruik letters, cijfers en . _ : / @ -, "
                "beginnend met een letter of cijfer"
            )
        return value

    @field_validator("privileges", mode="before")
    @classmethod
    def _uppercase_privileges(cls, value: object) -> object:
        # SQL role keywords are case-insensitive, so a hand-edited lowercase value still
        # validates and is normalised to the canonical uppercase form.
        if isinstance(value, list):
            return [item.upper() if isinstance(item, str) else item for item in value]
        return value
