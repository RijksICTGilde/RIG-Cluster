"""De keycloak-configuratie biedt de velden aan die het configmodel kent.

Het model kende drie velden die het scherm niet aanbood: ``restrict-access/role`` (een
clientrol naast de realm-rol), ``account-link`` en ``variables``. De eerste twee staan
er nu; de derde blijft er bewust buiten, en die reden wordt hier vastgezet zodat hij
niet stilletjes alsnog opduikt of stilletjes verdwijnt.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, cast

import pytest
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.editables.service_path import smart_get_value
from opi.forms.layout import layout_field_names
from opi.manager.keycloak_manager import KeycloakManager
from opi.services.catalog.base import ConfigLayer
from opi.services.catalog.keycloak.config_model import KeycloakConfig
from opi.services.registry import get_service
from opi.services.services_enums import ServiceType


def _section() -> Any:
    section = get_service(ServiceType.KEYCLOAK).config_form_section(ConfigLayer.PROJECT)
    assert section is not None
    return section


def _project() -> dict[str, Any]:
    return {
        "name": "toets",
        "services": ["publish-on-web", {"keycloak": {"config": {"template": "sso-only"}}}],
    }


@pytest.mark.parametrize(
    "pad",
    [
        "services/keycloak/config/restrict-access/role",
        "services/keycloak/config/account-link",
    ],
)
def test_het_veld_staat_op_het_scherm(pad: str) -> None:
    """Zowel geregistreerd als getekend -- de ene helft zonder de andere is de val."""
    section = _section()
    assert pad in [vis.editable.yaml_path for vis in section.editables]
    assert pad in layout_field_names(section.layout)


@pytest.mark.asyncio
async def test_clientrol_en_accountkoppeling_landen_in_het_projectbestand() -> None:
    """Wat het scherm opstuurt komt op het pad terecht dat de manager leest."""
    section = _section()
    inzending = {
        "_services-config": {
            "keycloak": {
                "config": {
                    "template": "sso-only",
                    "restrict-access": {"enabled": True, "realm-role": "allowed-user", "role": "app-beheerder"},
                    "account-link": "confirm",
                }
            }
        }
    }
    resultaat, errors = await EditableFormProcessor().process_json_submission(
        inzending, section.editables, copy.deepcopy(_project()), edit_mode=True
    )

    assert not errors, errors
    assert smart_get_value(resultaat, "services/keycloak/config/restrict-access/role") == "app-beheerder"
    assert smart_get_value(resultaat, "services/keycloak/config/account-link") == "confirm"


@pytest.mark.asyncio
async def test_lege_keuzes_laten_geen_lege_sleutels_achter() -> None:
    """Niets invullen is geen waarde: dan hoort er ook niets in het bestand te staan."""
    section = _section()
    project = _project()
    project["services"][1]["keycloak"]["config"]["account-link"] = "confirm"
    project["services"][1]["keycloak"]["config"]["restrict-access"] = {"enabled": True, "role": "oud"}

    inzending = {
        "_services-config": {
            "keycloak": {
                "config": {
                    "template": "sso-only",
                    "restrict-access": {"enabled": True, "realm-role": "allowed-user", "role": ""},
                    "account-link": "",
                }
            }
        }
    }
    resultaat, errors = await EditableFormProcessor().process_json_submission(
        inzending, section.editables, project, edit_mode=True
    )

    assert not errors, errors
    assert smart_get_value(resultaat, "services/keycloak/config/restrict-access/role") is None
    assert smart_get_value(resultaat, "services/keycloak/config/account-link") is None


def test_variables_blijft_bewust_buiten_het_scherm() -> None:
    """``variables`` overschrijft de door het platform berekende templatewaarden.

    ``KeycloakManager`` doet ``context.update(user_variables)``, en in die context staan
    ``realm_name``, ``project_realm_name``, ``platform_realm_name`` en
    ``platform_client_id``. Een vrij invulveld daarvoor geeft een projectbeheerder een
    hendel op de realm-template van het platform. Het veld blijft dus buiten de
    zelfbediening, met de reden in de dienstdocumentatie -- niet stil weggevallen.
    """
    section = _section()
    paden = [vis.editable.yaml_path for vis in section.editables]

    assert "services/keycloak/config/variables" not in paden
    assert "variables" in KeycloakConfig.model_fields
    hulp = Path(__file__).parent.parent / "opi" / "services" / "catalog" / "keycloak" / "help.md"
    assert "variables" in hulp.read_text(), "de reden hoort in de dienstdocumentatie te staan"


# ---------------------------------------------------------------------------
# De alias: een configblok met een schrijfwijze
# ---------------------------------------------------------------------------


def test_beide_schrijfwijzen_van_de_redirect_uris_worden_gelezen() -> None:
    """``additional_redirect_uris`` was het enige samengestelde veld zonder koppelteken."""
    met_underscore = KeycloakConfig.model_validate({"additional_redirect_uris": ["http://localhost:8080/*"]})
    met_koppelteken = KeycloakConfig.model_validate({"additional-redirect-uris": ["http://localhost:8080/*"]})

    assert met_underscore.additional_redirect_uris == ["http://localhost:8080/*"]
    assert met_koppelteken.additional_redirect_uris == ["http://localhost:8080/*"]


def test_de_alias_verandert_de_naam_naar_buiten_niet() -> None:
    """Geen migratie: bestaande bestanden en de API houden dezelfde sleutel."""
    service = get_service(ServiceType.KEYCLOAK)
    assert "additional_redirect_uris" in service.config_api_fields(ConfigLayer.PROJECT)
    assert "additional-redirect-uris" not in service.config_api_fields(ConfigLayer.PROJECT)


def test_de_manager_leest_de_koppeltekenvorm_ook() -> None:
    """Een alias die alleen valideert maar niet gelezen wordt, is een stille no-op."""
    project = {
        "services": [
            {"keycloak": {"config": {"template": "sso-only", "additional-redirect-uris": ["http://localhost:8080/*"]}}}
        ]
    }
    genormaliseerd = KeycloakManager._get_keycloak_service_config(cast("KeycloakManager", None), project)

    assert genormaliseerd["additional_redirect_uris"] == ["http://localhost:8080/*"]
