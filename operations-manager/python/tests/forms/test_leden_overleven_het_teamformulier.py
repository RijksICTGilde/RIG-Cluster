"""Een token blijft bij het lid dat hem bezit, welk teamformulier er ook opslaat.

``users`` draagt sinds schemaversie 2.12 de tokenhashes van de leden en de service
accounts van het project, terwijl het teamformulier alleen het e-mailadres en de rol van
een MENS rendert en toch het pad ``users`` als geheel bezit. De merge van een stap is per
INDEX, dus zonder voorziening schuift het token van een weggehaald lid naar de rij die zijn
plaats inneemt: dat lid is dan niet ingetrokken maar OMGEKAT, en legitimeert zich daarna als
degene op wiens rij zijn hash beland is.

Dit toetst die eigenschap op BEIDE formulierwegen: de modaal op de detailpagina
(``apply_modal_edit``) en de volledige bewerkwizard (``/forms/wizard/edit-project``).

De twee helften staan los. De gedragstoetsen zetten de echte stapmerge van die flow neer en
leggen vast dat de hashes op E-MAILADRES terugkomen en niet op index; dat de ROUTES die
stappen ook echt doen staat in de laatste toets, die de bron van elke formulierweg leest.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from opi.api.principal import NotRecognised, recognise_principal
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.visualizers.flows import get_flow
from opi.forms.wizard.leden import leden_voor_formulier, zet_verborgen_leden_terug
from opi.forms.wizard.save import apply_modal_edit
from opi.forms.wizard.state import WizardState
from opi.services.project_leden import hash_van
from opi.services.project_service import get_project_service
from opi.services.user_service import get_user_service
from opi.web.project_edit_security import apply_form_data_to_project
from opi.web.router_detail_edit import _fully_owned_list_keys
from opi.web.router_wizard import _extract_section_data, _split_data_across_sections

PROJECT = "netjes"
DEV_TOKEN = "zad_devtoken_example_value_0001"
BAAS_TOKEN = "zad_baastoken_example_value_002"
SA_TOKEN = "zad_satoken_example_value_00003"


def _token(naam: str, waarde: str) -> dict[str, Any]:
    return {
        "name": naam,
        "hash": hash_van(waarde),
        "expires": "2030-01-01T00:00:00+00:00",
        "created": "2026-01-01T00:00:00+00:00",
    }


def _project() -> dict[str, Any]:
    """Twee mensen met elk een token, en een service account met de projectsleutel."""
    return {
        "schema-version": "2.12",
        "name": PROJECT,
        "display-name": "Netjes",
        "description": "x",
        "users": [
            {"email": "dev@rijksoverheid.nl", "roles": ["developer"], "tokens": [_token("dev-laptop", DEV_TOKEN)]},
            {"email": "baas@rijksoverheid.nl", "roles": ["admin", "ops"], "tokens": [_token("baas-cli", BAAS_TOKEN)]},
            {
                "type": "service-account",
                "name": "project-api",
                "roles": ["admin", "ops"],
                "tokens": [_token("projectsleutel", SA_TOKEN)],
            },
        ],
        "clusters": ["sandboxed-local"],
        "services": ["publish-on-web"],
        "components": [],
        "deployments": [],
        "config": {"age-public-key": "age1xxx"},
    }


#: Wat de browser post als de beheerder de bovenste rij (``dev@``) weghaalt.
BODY_DEV_WEG = {"users": [{"email": "baas@rijksoverheid.nl", "roles": ["admin", "ops"]}]}


# --- de twee functies zelf, op hun randen -----------------------------------


def test_leden_voor_formulier_laat_data_zonder_ledenlijst_ongewijzigd() -> None:
    """``users`` kan ontbreken (een nieuw project) of geen lijst zijn; dan is er niets te
    scheiden en komt de data onveranderd terug, in plaats van een AttributeError."""
    data = {"name": "leeg"}

    assert leden_voor_formulier(data) is data or leden_voor_formulier(data) == data


def test_zet_verborgen_leden_terug_doet_niets_zonder_ledenlijst() -> None:
    merged = {"name": "x"}
    stored = {"name": "x", "users": [{"email": "a@b.nl", "roles": ["admin", "ops"]}]}

    zet_verborgen_leden_terug(merged, stored)

    assert "users" not in merged


def test_zet_verborgen_leden_terug_laat_een_niet_dict_lid_ongemoeid() -> None:
    """Geknoeide invoer (een lid dat geen dict is) crasht niet en reist gewoon mee."""
    merged = {"users": ["geen-dict"]}
    stored = {"users": []}

    zet_verborgen_leden_terug(merged, stored)

    assert merged["users"] == ["geen-dict"]


def test_zet_verborgen_leden_terug_negeert_een_service_account_in_het_ingediende() -> None:
    """Het formulier maakt zelf geen service account; komt er toch een in de inzending
    terecht, dan telt alleen het exemplaar uit het bestand, niet een tweede ernaast."""
    merged = {
        "users": [
            {"email": "a@b.nl", "roles": ["admin", "ops"]},
            {"type": "service-account", "name": "indringer", "roles": ["admin", "ops"]},
        ]
    }
    stored = {
        "users": [
            {"email": "a@b.nl", "roles": ["admin", "ops"]},
            {"type": "service-account", "name": "bouwstraat", "roles": ["developer"]},
        ]
    }

    zet_verborgen_leden_terug(merged, stored)

    namen = [lid.get("name") for lid in merged["users"] if lid.get("type") == "service-account"]
    assert namen == ["bouwstraat"]


async def _teamstap(flow_id: str, *, modaal: bool) -> dict[str, Any]:
    """Doorloop de teamstap van *flow_id* zoals zijn route dat doet, en geef het resultaat."""
    opgeslagen = _project()
    flow = get_flow(flow_id)
    section = next(s for s in flow.sections if "users" in [v.editable.yaml_path for v in s.editables])
    owned = _fully_owned_list_keys(flow)

    # De prefill, zoals ``modal_wizard_init`` en ``wizard_edit_page`` hem maken.
    form_data = leden_voor_formulier(opgeslagen) if "users" in owned else opgeslagen
    state = WizardState(flow_id=flow_id, current_step=section.section_id, project_name=PROJECT)
    state.step_data = _split_data_across_sections(flow, form_data)
    state.active_sections = [s.section_id for s in flow.sections]
    state.populate_virt_mappings(flow.sections)
    state.base_data = {k: v for k, v in copy.deepcopy(form_data).items() if k not in owned}

    submitted_yaml, errors = await EditableFormProcessor().process_json_submission(
        copy.deepcopy(BODY_DEV_WEG),
        section.editables,
        state.get_merged_data(),
        edit_mode=True,
        enforcer_context={"project_name": PROJECT, "edit_mode": True},
    )
    assert not errors
    state.store_step_data(section.section_id, _extract_section_data(section.editables, submitted_yaml))
    final = state.get_merged_data(strip_cleared=False)

    if modaal:
        return await apply_modal_edit(
            copy.deepcopy(opgeslagen),
            final,
            flow=flow,
            active_sections=[section],
            state=state,
            project_name=PROJECT,
        )
    resultaat = apply_form_data_to_project(copy.deepcopy(opgeslagen), {"users": final.get("users")})
    zet_verborgen_leden_terug(resultaat, opgeslagen)
    return resultaat


@pytest.mark.parametrize(("flow_id", "modaal"), [("modal-edit-team", True), ("edit-project", False)])
@pytest.mark.asyncio
async def test_een_lid_weghalen_schuift_zijn_token_niet_naar_een_ander(flow_id: str, modaal: bool) -> None:
    resultaat = await _teamstap(flow_id, modaal=modaal)
    per_naam = {lid.get("email") or lid.get("name"): lid for lid in resultaat["users"]}

    assert "dev@rijksoverheid.nl" not in per_naam
    assert [t["name"] for t in per_naam["baas@rijksoverheid.nl"]["tokens"]] == ["baas-cli"]
    assert [t["name"] for t in per_naam["project-api"]["tokens"]] == ["projectsleutel"]


@pytest.mark.parametrize(("flow_id", "modaal"), [("modal-edit-team", True), ("edit-project", False)])
@pytest.mark.asyncio
async def test_het_token_van_het_weggehaalde_lid_legitimeert_niemand(flow_id: str, modaal: bool) -> None:
    """De hash mag niet op de rij van een ANDER lid belanden: dat is omkatten, niet intrekken."""
    resultaat = await _teamstap(flow_id, modaal=modaal)

    service = get_project_service()
    service.clear_all_projects()
    assert service.load_project_from_data(resultaat, f"{PROJECT}.yaml") is True
    project = service.get_project(PROJECT)
    assert project is not None
    get_user_service().add_allowed_emails(["baas@rijksoverheid.nl"])

    with pytest.raises(NotRecognised):
        await recognise_principal(DEV_TOKEN, project)

    assert (await recognise_principal(BAAS_TOKEN, project)).name == "baas@rijksoverheid.nl"
    assert (await recognise_principal(SA_TOKEN, project)).name == "project-api"


#: De twee formulierwegen die ``users`` als geheel schrijven, met de plek waar ze de
#: prefill maken en de plek waar ze opslaan. Een derde weg hoort hier bij te komen voordat
#: hij schrijft, want zonder deze twee stappen schuift hij de tokens tussen de rijen.
FORMULIERWEGEN = {
    "opi/web/router_detail_edit.py": "de bewerkmodaal op de detailpagina (prefill)",
    "opi/forms/wizard/save.py": "de bewerkmodaal op de detailpagina (opslaan)",
    "opi/web/router_wizard.py": "de volledige bewerkwizard (prefill en opslaan)",
}


@pytest.mark.parametrize("bestand", sorted(FORMULIERWEGEN))
def test_elke_formulierweg_scheidt_de_leden_en_zet_ze_terug(bestand: str) -> None:
    """De scheiding mist geen weg: anders is de ene modaal veilig en de andere niet."""
    tekst = (Path(__file__).resolve().parents[2] / bestand).read_text()
    assert "leden_voor_formulier" in tekst or "zet_verborgen_leden_terug" in tekst, (
        f"{bestand} ({FORMULIERWEGEN[bestand]}) schrijft 'users' als geheel maar gebruikt "
        "opi/forms/wizard/leden.py niet; de tokens van een weggehaald lid schuiven dan naar "
        "de rij die zijn plaats inneemt"
    )
