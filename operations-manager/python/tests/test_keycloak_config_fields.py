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
    """``variables`` raakt de door het platform berekende templatewaarden.

    In die context staan ``realm_name``, ``project_realm_name``, ``platform_realm_name`` en
    ``platform_client_id``. Sinds RC-159 legt ``merge_user_variables`` de projectwaarden
    ERONDER in plaats van erover, dus een project kan de doelrealm niet meer verzetten --
    maar een vrij invulveld hiervoor blijft een hendel op de realm-template van het
    platform, en het veld blijft dus buiten de zelfbediening, met de reden in de
    dienstdocumentatie. Niet stil weggevallen.
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


# ---------------------------------------------------------------------------
# De template verzint zichzelf niet
# ---------------------------------------------------------------------------
#
# Het veld droeg ``default="sso-support"`` terwijl het configmodel, het API-schema en
# ``KeycloakManager`` alle drie sso-only als default hebben. Een projectbestand zonder
# ``template`` liet het scherm dus een andere blauwdruk zien dan het platform bouwde, en
# opslaan schreef dat verzinsel ook nog in het bestand. Er is nu geen default meer: het veld
# toont wat er staat, en anders niets.


def _template_veld(project: dict[str, Any]) -> Any:
    from opi.forms.visualizers.bridge import editable_to_form_field
    from opi.services.catalog.keycloak.visualizers import KEYCLOAK_TEMPLATE

    return editable_to_form_field(KEYCLOAK_TEMPLATE, project)


def test_de_opgeslagen_template_staat_geselecteerd() -> None:
    assert _template_veld(_project()).value == "sso-only"


def test_zonder_template_verzint_het_scherm_er_geen() -> None:
    project = _project()
    project["services"][1]["keycloak"]["config"] = {}

    veld = _template_veld(project)

    assert veld.value is None, f"scherm toont {veld.value!r} terwijl het projectbestand niets zegt"
    assert veld.required, "zonder default moet het veld een keuze afdwingen"
    assert veld.placeholder, "zonder lege optie kiest de browser de eerste, en dat is weer een verzinsel"


@pytest.mark.asyncio
async def test_een_lege_template_wordt_geweigerd() -> None:
    """De keuze overslaan mag niet stilzwijgend een blauwdruk opleveren."""
    section = _section()
    inzending = {"_services-config": {"keycloak": {"config": {"template": ""}}}}

    _resultaat, errors = await EditableFormProcessor().process_json_submission(
        inzending, section.editables, _project(), edit_mode=True
    )

    assert "services/keycloak/config/template" in errors


def _gerenderde_keuzelijst(project: dict[str, Any]) -> str:
    from opi.forms.widgets.lotc import LOTCWidgetAdapter

    return LOTCWidgetAdapter().render_select(_template_veld(project))


def test_de_keuzelijst_begint_met_een_lege_optie() -> None:
    """De lege optie IS de "nog niets gekozen"-stand.

    Een keuzelijst heeft altijd iets geselecteerd, dus zonder lege optie bovenaan kiest de
    browser de eerste echte - en dan toont het scherm weer een blauwdruk die nergens staat.
    Deze toets zit op de gerenderde HTML omdat de bedrading ervan (``:placeholder`` in
    ``widgets/select.html.j2``) nergens anders in de applicatie gebruikt wordt en dus stil
    kan verdwijnen.
    """
    project = _project()
    project["services"][1]["keycloak"]["config"] = {}

    html = _gerenderde_keuzelijst(project)

    assert '<option value="">Kies een template</option>' in html
    assert "selected" not in html, "niets gekozen betekent niets geselecteerd"


def test_de_opgeslagen_waarde_staat_geselecteerd_in_de_html() -> None:
    html = _gerenderde_keuzelijst(_project())

    assert '<option value="sso-only" selected>' in html
    assert '<option value="sso-support">' in html


def test_het_formulier_heeft_geen_eigen_mening_over_de_default() -> None:
    """De bug in één regel: het formulier kende sso-support, de rest van het platform sso-only.

    Twee defaults voor hetzelfde veld betekent dat het scherm iets anders toont dan het
    platform doet, en dat een opslag dat verschil ook nog uitschrijft. Er is er daarom nog
    maar één, en die staat niet in de formulierlaag.
    """
    from opi.manager.keycloak_manager import SSO_ONLY_TEMPLATE
    from opi.services.catalog.keycloak.editables import KEYCLOAK_TEMPLATE_EDITABLE

    assert KEYCLOAK_TEMPLATE_EDITABLE.default is None, (
        "een default in het formulier gaat een eigen leven leiden naast die van het platform"
    )
    assert KeycloakConfig.model_fields["template"].default == SSO_ONLY_TEMPLATE, (
        "configmodel en manager horen dezelfde blauwdruk te bedoelen; het API-schema volgt het model"
    )


@pytest.mark.asyncio
async def test_de_manager_bouwt_de_blauwdruk_van_het_configmodel_als_het_bestand_zwijgt() -> None:
    """De derde kant van dezelfde waarheid: wat er gebeurt als niemand iets zegt."""
    project: dict[str, Any] = {"name": "toets", "services": ["keycloak"]}

    config = KeycloakManager._get_keycloak_service_config(cast("KeycloakManager", None), project)

    assert config["template"] == KeycloakConfig.model_fields["template"].default
