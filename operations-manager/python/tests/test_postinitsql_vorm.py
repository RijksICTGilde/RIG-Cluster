"""``postInitSQL`` laat alleen een extensie toe (RC-285).

Het veld draait als superuser van het eigen CNPG-cluster, dus een vrije SQL-regel is daar
een superuser-shell. Gemeten op het configmodel, op het vastgelegde schemafragment (daar
leest een client de vorm) en op de projectvalidatie, want daar komt de melding langs die een
gebruiker ziet.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from opi.core.project_schema import ProjectIntegrityError
from opi.manager.project_validation import validate_service_configs
from opi.services.catalog.shared.postgres import POST_INIT_SQL_PATTERN
from opi.services.config_schema import fragment_path
from opi.services.registry import get_service
from opi.services.services_enums import ServiceType
from pydantic import ValidationError

#: De vormen die in deze repo voorkomen of in een extensienaam met streepje nodig zijn.
GOED = [
    "CREATE EXTENSION IF NOT EXISTS vector;",
    "CREATE EXTENSION IF NOT EXISTS postgis",
    'CREATE EXTENSION IF NOT EXISTS "uuid-ossp";',
    "CREATE EXTENSION IF NOT EXISTS unaccent SCHEMA public;",
]

FOUT = [
    "CREATE EXTENSION IF NOT EXISTS x; DROP TABLE y",
    "CREATE EXTENSION IF NOT EXISTS x;\nDROP TABLE y",
    # Elke YAML-regelovergang, niet alleen \n: de naam komt ongequote in de Cluster terecht,
    # dus een \r erin zet er een eigen regel naast imageName.
    'CREATE EXTENSION IF NOT EXISTS "x\r  imageName: evil/image";',
    'CREATE EXTENSION IF NOT EXISTS vector SCHEMA "s\r  imageName: evil/image";',
    # Een dubbele punt of een hekje in de naam breekt de plain scalar van dezelfde regel.
    'CREATE EXTENSION IF NOT EXISTS "x: y";',
    # Een afsluitende regelovergang: zonder die eis laat de Python-engine hem door.
    "CREATE EXTENSION IF NOT EXISTS vector;\n",
    'CREATE EXTENSION IF NOT EXISTS "x"; DROP TABLE y; --"',
    "CREATE EXTENSION IF NOT EXISTS unaccent SCHEMA public; DROP TABLE y",
    # Zonder IF NOT EXISTS: het veld draait bij elke verwerking opnieuw.
    "CREATE EXTENSION vector;",
    "GRANT ALL ON DATABASE postgres TO PUBLIC",
    "",
]


def _project(post_init: list[str]) -> dict[str, Any]:
    return {
        "name": "proj",
        "services": [{"name": "postgresql-database", "config": {"scope": "project", "postInitSQL": post_init}}],
        "deployments": [{"name": "test", "cluster": "local", "services": ["postgresql-database"]}],
    }


def _validate(post_init: list[str]) -> None:
    get_service(ServiceType.POSTGRESQL_DATABASE).validate_config({"scope": "project", "postInitSQL": post_init})


@pytest.mark.parametrize("statement", GOED)
def test_een_extensieregel_valideert(statement: str) -> None:
    _validate([statement])
    validate_service_configs(_project([statement]))


def test_alle_extensieregels_samen_valideren() -> None:
    _validate(GOED)


@pytest.mark.parametrize("statement", FOUT)
def test_elke_andere_vorm_wordt_geweigerd(statement: str) -> None:
    with pytest.raises(ValidationError):
        _validate([statement])


def test_de_projectvalidatie_noemt_het_veld_en_de_vorm() -> None:
    with pytest.raises(ProjectIntegrityError) as geweigerd:
        validate_service_configs(_project(["DROP TABLE klanten"]))

    melding = str(geweigerd.value)
    assert "postInitSQL" in melding
    assert "CREATE EXTENSION IF NOT EXISTS" in melding


def test_het_schemafragment_draagt_het_patroon() -> None:
    """Een client leest de vorm uit het fragment, dus die mag niet achterlopen."""
    # Het patroon staat JSON-ontsnapt in het bestand; de buitenste aanhalingstekens eraf.
    ontsnapt = json.dumps(POST_INIT_SQL_PATTERN)[1:-1]
    for service in (ServiceType.POSTGRESQL_DATABASE, ServiceType.NAMESPACE_POSTGRESQL_DATABASE):
        assert ontsnapt in Path(fragment_path(get_service(service))).read_text(encoding="utf-8")


def test_het_patroon_blijft_ecma_262() -> None:
    """``(?i:`` en ``\\Z`` bestaan niet in de regex die een JSON-schema belooft."""
    assert "(?i" not in POST_INIT_SQL_PATTERN
    assert "\\Z" not in POST_INIT_SQL_PATTERN
