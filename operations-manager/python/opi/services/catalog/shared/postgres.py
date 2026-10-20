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

import re
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from opi.utils.naming import CONTAINER_IMAGE_PATTERN

#: Een identifier zoals PostgreSQL hem schrijft: kaal, of tussen dubbele aanhalingstekens
#: omdat een extensienaam een streepje mag dragen (``"uuid-ossp"``, zie
#: ``_precreate_extensions_for_clone`` in de postgres-connector).
#:
#: De gequote vorm somt zijn tekens op in plaats van ze uit te sluiten. Een uitsluitende
#: klasse liet een carriage return door, en ``postgresql-cluster.yaml.jinja`` zet deze
#: waarde ongequote onder ``postInitApplicationSQL``: elke YAML-regelovergang in de naam
#: wordt daar een eigen regel, dus een extra sleutel in de ``Cluster`` naast
#: ``imageName``.
_PG_NAME = r'(?:[A-Za-z_][A-Za-z0-9_$]*|"[A-Za-z0-9_$-]+")'

#: ``postInitSQL`` draait als SUPERUSER van het eigen CNPG-cluster, dus een vrije SQL-regel
#: is daar een superuser-shell. Alleen de vorm die het veld bedoeld is: een extensie, met
#: eventueel het schema erachter (RC-285).
#:
#: De sleutelwoorden in hoofdletters en niet case-insensitief: ``(?i:...)`` bestaat niet in
#: de ECMA-262-regex die een JSON-schema belooft, en dit patroon komt in het
#: schemafragment terecht dat clients lezen. Elk bekend gebruik schrijft ze al zo.
#:
#: ``$`` en niet ``\Z``: de rust-engine waar pydantic op draait kent ``\Z`` niet. In die
#: engine laat ``$`` geen afsluitende newline door; wie dit patroon uit het schemafragment
#: met de Python-engine gaat gebruiken, zet er ``\Z`` bij. Zelfde afweging als
#: ``UPSTREAM_PATTERN`` in ``image_registries/config_model.py``.
POST_INIT_SQL_PATTERN = rf"^CREATE EXTENSION IF NOT EXISTS {_PG_NAME}(?: SCHEMA {_PG_NAME})?;?$"

PostInitStatement = Annotated[str, Field(pattern=POST_INIT_SQL_PATTERN)]


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

    # Een gesloten lijst toegestane images (een ChoiceSetting) kan niet: productie draait
    # een eigen CNPG-image met een eigen tag, en features/namespace-postgresql-database.md
    # laat eigen images uitdrukkelijk toe. Dus de VORM, met de tekenset die een
    # image-verwijzing in een projectbestand overal heeft.
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
    # De grenzen staan in POSTGRES_STORAGE (shared/postgres_settings.py), niet hier: een
    # grens op een dienstveld is een declaratie, geen ``le=`` op het model (RC-168).
    # 1Gi en niet 10Gi: de keuzelijst van het formulier (StorageSizeOptionsProvider) loopt tot
    # 1Gi, dus de oude standaardwaarde stond niet in de lijst waaruit je hem kon kiezen. Dat is
    # de bovenkant van de lijst, want een database is de plek waar ruimte het eerst opraakt.
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
        # Een ``field_validator`` en geen ``pattern=``: pydantic toetst met de
        # Rust-regexengine, die ``\Z`` niet kent, en dan zou deze tekenset twee teksten
        # krijgen.
        if not re.match(CONTAINER_IMAGE_PATTERN, value):
            raise ValueError(
                "Image bevat niet-toegestane tekens: gebruik letters, cijfers en . _ : / @ -, "
                "beginnend met een letter of cijfer"
            )
        return value

    @field_validator("privileges", mode="before")
    @classmethod
    def _uppercase_privileges(cls, value: object) -> object:
        # Preserve the old case-insensitive behaviour (privilege.upper()): SQL role
        # keywords are case-insensitive, so a hand-edited lowercase value still
        # validates and is normalised to the canonical uppercase form.
        if isinstance(value, list):
            return [item.upper() if isinstance(item, str) else item for item in value]
        return value
