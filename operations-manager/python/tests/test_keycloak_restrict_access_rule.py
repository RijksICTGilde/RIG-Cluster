"""``restrict-access`` aan zetten kan alleen mét een rol, en dat staat in het schema.

DE MELDING (zad-cli, punt 6)

``RestrictAccessConfig`` had ``enabled``, ``role``, ``realm-role`` en ``error-message`` en
geen enkele ``required``. Toch faalde de UITROL met "restrict-access.role or
restrict-access.realm-role is required" zodra ``enabled: true`` zonder rol stond. Die eis
stond uitgeschreven in ``KeycloakManager._get_keycloak_service_config`` en nergens anders.

WAAROM DAT MEER IS DAN EEN VERKEERDE PLEK

De zad-cli valideert een body tegen het schema vóór hij hem verstuurt, juist om een
gefaalde uitrol te besparen. Een regel die niet in het schema staat, ziet hij niet -- en
hem in de CLI naprogrammeren betekent een dienstnaam in de CLI bakken. Dus: één plek (het
model), in twee vormen. De ``anyOf`` beschrijft de regel voor wie het schema leest, de
``model_validator`` dwingt hem af voor wie hem negeert. Een validator alléén is niet
genoeg: die verschijnt niet in het gegenereerde schema en dus niet in het OpenAPI-document.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from opi.core.project_schema import ProjectIntegrityError
from opi.manager.keycloak_manager import KeycloakManager
from opi.manager.project_validation import validate_service_configs
from opi.services.catalog.keycloak.config_model import RestrictAccessConfig
from pydantic import ValidationError

FRAGMENT = Path(__file__).parent.parent / "opi" / "services" / "catalog" / "keycloak" / "keycloak.v1.0.json"

#: De melding, als patroon: de punten in de veldnamen zijn letterlijk bedoeld.
MELDING = re.escape("restrict-access.role or restrict-access.realm-role is required")

#: De vier standen die de regel beschrijft, met het oordeel dat erbij hoort.
GELDIG = [
    pytest.param({}, id="niets ingevuld"),
    pytest.param({"enabled": False}, id="uit"),
    pytest.param({"enabled": True, "role": "toegelaten"}, id="aan met clientrol"),
    pytest.param({"enabled": True, "realm-role": "toegelaten"}, id="aan met realmrol"),
]
ONGELDIG = [
    pytest.param({"enabled": True}, id="aan zonder rol"),
    pytest.param({"enabled": True, "role": None, "realm-role": None}, id="aan met lege rollen"),
    pytest.param({"enabled": True, "role": ""}, id="aan met een lege rol"),
]


# --- Het model: de regel wordt afgedwongen ------------------------------------------


@pytest.mark.parametrize("config", GELDIG)
def test_het_model_laat_een_geldige_stand_door(config: dict[str, Any]) -> None:
    RestrictAccessConfig.model_validate(config)


@pytest.mark.parametrize("config", ONGELDIG)
def test_het_model_weigert_aan_zonder_rol(config: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match=MELDING):
        RestrictAccessConfig.model_validate(config)


# --- Het schema: dezelfde regel, leesbaar voor een client ----------------------------


def _fragment_schema() -> dict[str, Any]:
    """Het vastgelegde fragment, met de RestrictAccessConfig-def als hoofdschema.

    Dit is het bestand dat naast de dienst staat en dat de zad-cli uitleest, dus de test
    valideert tegen precies die bytes en niet tegen een opnieuw gerenderd model.
    """
    fragment = json.loads(FRAGMENT.read_text(encoding="utf-8"))
    return {"$defs": fragment["$defs"], "$ref": "#/$defs/RestrictAccessConfig"}


@pytest.mark.parametrize("config", GELDIG)
def test_het_schema_laat_een_geldige_stand_door(config: dict[str, Any]) -> None:
    Draft202012Validator(_fragment_schema()).validate(config)


@pytest.mark.parametrize("config", ONGELDIG)
def test_het_schema_weigert_aan_zonder_rol(config: dict[str, Any]) -> None:
    """Wat het model weigert, weigert een clientvalidator ook -- zonder de dienst te kennen."""
    errors = list(Draft202012Validator(_fragment_schema()).iter_errors(config))
    assert errors, "een client die het schema leest ziet niets dat hem tegenhoudt"


def test_het_openapi_document_draagt_de_eis() -> None:
    """De zad-cli leest ``/openapi.json``, niet het bestand naast de dienst."""
    from opi.server import app

    schema = app.openapi()["components"]["schemas"]["RestrictAccessConfig"]

    assert schema["anyOf"] == [
        {"properties": {"enabled": {"const": False}}},
        {"properties": {"role": {"type": "string", "minLength": 1}}, "required": ["role"]},
        {"properties": {"realm-role": {"type": "string", "minLength": 1}}, "required": ["realm-role"]},
    ]


# --- Eén regel, alle momenten --------------------------------------------------------


def _project(restrict_access: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": "test-project",
        "services": [{"name": "keycloak", "config": {"template": "sso-only", "restrict-access": restrict_access}}],
    }


def test_het_opslaan_weigert_aan_zonder_rol() -> None:
    """De schrijfkant (API en wizard) loopt door ``validate_service_configs``."""
    with pytest.raises(ProjectIntegrityError, match=MELDING):
        validate_service_configs(_project({"enabled": True}))


def test_de_uitrol_weigert_aan_zonder_rol() -> None:
    """En de uitrol blijft hem weigeren, nu via hetzelfde model in plaats van een eigen kopie."""
    manager = KeycloakManager(None)

    with pytest.raises(ValueError, match=MELDING):
        manager._get_keycloak_service_config(_project({"enabled": True}))


def test_de_uitrol_leest_een_geldige_stand_gewoon() -> None:
    manager = KeycloakManager(None)

    config = manager._get_keycloak_service_config(_project({"enabled": True, "realm-role": "toegelaten"}))

    assert config["restrict_access"] == {
        "enabled": True,
        "role": None,
        "realm_role": "toegelaten",
        "error_message": "${accessDeniedNoPermission}",
    }
