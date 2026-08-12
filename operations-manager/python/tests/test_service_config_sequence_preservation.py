"""Een lijst in dienstconfiguratie kan niet meer per ongeluk leeg.

Elke wizardsectie draagt een volledige kopie van de projectgegevens, en de deep merge
laat een lijst in zijn geheel vervangen. Een sectie die een lijst niet toonde maar hem
wel meedroeg, schreef daarmee ``[]`` over een gevulde lijst. In productie zijn zo
``additional-clients``-vermeldingen verdwenen.

De vraag die de schrijfkant moet beantwoorden is: hoe onderscheid je "de gebruiker
heeft de laatste regel verwijderd" van "deze sectie ging er niet over". Het formulier
zegt het zelf -- elke gerenderde reeks stuurt haar pad mee. Deze toetsen meten dat
onderscheid op de OPGESLAGEN gegevens, niet op een tussenfunctie: een toets die alleen
de merge-functie aanroept had dit nooit gevonden, want die doet precies wat zijn
docstring belooft.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.editables.rendered_sequences import GERENDERDE_REEKSEN_VELD
from opi.forms.renderer import FormRenderer
from opi.forms.widgets.lotc import LOTCWidgetAdapter
from opi.forms.wizard.write_set import apply_write_paths, flow_write_paths
from opi.services.catalog.base import ConfigLayer
from opi.services.registry import get_service
from opi.services.services_enums import ServiceType
from opi.web.router_wizard import _extract_section_data

REDIRECT_URIS_PAD = "services/keycloak/config/additional_redirect_uris"
CLIENTS_PAD = "services/keycloak/config/additional-clients"

#: Waaronder het formulier ze post. Dienstconfiguratie is gevirtualiseerd: het formulier
#: gebruikt ``_services-config`` zodat het niet botst met de dienstSELECTIE onder
#: ``services``. Het merk draagt dus het virtuele pad, en de verwerker herkent beide.
VIRT_REDIRECT_URIS_PAD = "_services-config/keycloak/config/additional_redirect_uris"
VIRT_CLIENTS_PAD = "_services-config/keycloak/config/additional-clients"


def _keycloak_section() -> Any:
    section = get_service(ServiceType.KEYCLOAK).config_form_section(ConfigLayer.PROJECT)
    assert section is not None
    return section


def _project() -> dict[str, Any]:
    """Een project met twee extra clients en een extra redirect URI."""
    return {
        "name": "toets",
        "services": [
            "publish-on-web",
            {
                "keycloak": {
                    "config": {
                        "template": "sso-only",
                        "additional_redirect_uris": ["http://localhost:8080/*"],
                        "additional-clients": [
                            {"name": "eerste", "redirect-uris": ["https://een.nl/*"]},
                            {"name": "tweede", "redirect-uris": ["https://twee.nl/*"]},
                        ],
                    }
                }
            },
        ],
        "users": [{"email": "a@b.nl", "role": "admin"}],
    }


def _keycloak_config(project_data: dict[str, Any]) -> dict[str, Any]:
    for entry in project_data["services"]:
        if isinstance(entry, dict) and "keycloak" in entry:
            return entry["keycloak"]["config"]
    raise AssertionError("geen keycloak-vermelding in het project")


async def _save(submission: dict[str, Any]) -> dict[str, Any]:
    """De hele weg van "de gebruiker drukte op opslaan" tot het opgeslagen bestand.

    Dezelfde stappen als de modaalbewerking: verwerk de inzending tegen de
    projectgegevens, prune tot wat de sectie bezit, en schrijf alleen de paden die de
    stroom declareert terug op het opgeslagen project.
    """
    section = _keycloak_section()
    opgeslagen = _project()
    processor = EditableFormProcessor()
    verwerkt, errors = await processor.process_json_submission(
        submission,
        section.editables,
        copy.deepcopy(opgeslagen),
        edit_mode=True,
    )
    assert not errors, errors
    sectiegegevens = _extract_section_data(section.editables, verwerkt)
    # Wat de sectie bewaart is virtueel; devirtualiseer zoals get_merged_data doet.
    from opi.forms.wizard.state import WizardState

    state = WizardState(flow_id="modal-edit-keycloak-config", current_step=section.section_id)
    state.base_data = copy.deepcopy(opgeslagen)
    state.active_sections = [section.section_id]
    state.populate_virt_mappings([section])
    state.store_step_data(section.section_id, sectiegegevens)
    samengevoegd = state.get_merged_data(strip_cleared=False)

    return apply_write_paths(opgeslagen, samengevoegd, flow_write_paths([section]))


def _inzending(*getekende_reeksen: str, **config: Any) -> dict[str, Any]:
    """Wat de browser post: de ingevulde velden plus welke reeksen op het scherm stonden.

    In de gevirtualiseerde vorm waarin het formulier dienstconfiguratie verstuurt.
    """
    body: dict[str, Any] = {"_services-config": {"keycloak": {"config": {"template": "sso-only", **config}}}}
    if getekende_reeksen:
        body[GERENDERDE_REEKSEN_VELD] = list(getekende_reeksen)
    return body


# ---------------------------------------------------------------------------
# De uitkomst, gemeten op het opgeslagen projectbestand
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_een_niet_getoonde_lijst_blijft_staan() -> None:
    """Een formulier dat alleen de redirect-URI's tekende laat de clients met rust."""
    resultaat = await _save(_inzending(VIRT_REDIRECT_URIS_PAD, additional_redirect_uris=["http://localhost:8080/*"]))

    config = _keycloak_config(resultaat)
    assert [client["name"] for client in config["additional-clients"]] == ["eerste", "tweede"]
    assert config["additional-clients"][0]["redirect-uris"] == ["https://een.nl/*"]


@pytest.mark.asyncio
async def test_geen_enkele_getoonde_lijst_laat_beide_lijsten_staan() -> None:
    """Een sectie zonder reeksen op het scherm mag geen van beide lijsten aanraken."""
    resultaat = await _save(_inzending("een/andere/reeks"))

    config = _keycloak_config(resultaat)
    assert len(config["additional-clients"]) == 2
    assert config["additional_redirect_uris"] == ["http://localhost:8080/*"]


@pytest.mark.asyncio
async def test_een_getoonde_lijst_die_de_gebruiker_leegmaakt_wordt_leeg() -> None:
    """Het andere geval: wie de laatste regel weghaalt, krijgt wel degelijk een lege lijst."""
    resultaat = await _save(_inzending(VIRT_REDIRECT_URIS_PAD, VIRT_CLIENTS_PAD))

    config = _keycloak_config(resultaat)
    assert config["additional-clients"] == []
    assert config["additional_redirect_uris"] == []


@pytest.mark.asyncio
async def test_een_getoonde_lijst_met_regels_wordt_gewoon_geschreven() -> None:
    """De gewone bewerking: een client erbij."""
    resultaat = await _save(
        _inzending(
            VIRT_REDIRECT_URIS_PAD,
            VIRT_CLIENTS_PAD,
            f"{VIRT_CLIENTS_PAD}[0]/redirect-uris",
            additional_redirect_uris=["http://localhost:8080/*"],
            **{"additional-clients": [{"name": "derde", "redirect-uris": ["https://drie.nl/*"]}]},
        )
    )

    config = _keycloak_config(resultaat)
    assert [client["name"] for client in config["additional-clients"]] == ["derde"]


@pytest.mark.asyncio
async def test_zonder_uitspraak_blijft_het_oude_gedrag() -> None:
    """Draagt de inzending het veld niet, dan is er niets gezegd.

    De eindinzending van de wizard geeft de samengevoegde projectgegevens door als
    "inzending"; die kent geen formuliervelden en heeft de lijsten al in de juiste
    toestand staan. Daar mag deze regel niets veranderen.
    """
    resultaat = await _save({"_services-config": {"keycloak": {"config": {"template": "sso-only"}}}})

    config = _keycloak_config(resultaat)
    assert config["additional-clients"] == []


# ---------------------------------------------------------------------------
# De formulierkant: het gerenderde scherm draagt het merk ook echt
# ---------------------------------------------------------------------------


def test_het_gerenderde_formulier_meldt_elke_reeks() -> None:
    """Zonder dit verborgen veld in de HTML zegt de inzending niets en werkt de regel niet."""
    section = _keycloak_section()
    renderer = FormRenderer(widget_adapter=LOTCWidgetAdapter())
    html = renderer.render_fields_from_editables(
        editables=section.editables,
        yaml_data=_project(),
        layout=section.layout,
    )

    for pad in (VIRT_REDIRECT_URIS_PAD, VIRT_CLIENTS_PAD):
        assert f'name="{GERENDERDE_REEKSEN_VELD}[]" value="{pad}"' in html, f"reeks {pad} meldt zich niet"
