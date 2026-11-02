"""De componentenmodal: verwijderen volgt dezelfde regel als de API, en een selectie.

Twee dingen worden hier gemeten (RC-378):

- een component dat je in de modal uit de lijst haalt, verdwijnt ook uit elke deployment
  die het noemt. Dat liep eerder stuk op "Invalid component references in deployment",
  want alleen de API-route ruimde die verwijzingen op;
- een modal die op een selectie is geopend draagt alleen de geselecteerde componenten, en
  het opslaan laat de rest van de lijst ongemoeid, inclusief de volgorde.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from opi.core.project_schema import ProjectIntegrityError
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.editables.rendered_sequences import GERENDERDE_REEKSEN_VELD
from opi.forms.visualizers.flows import MODAL_EDIT_COMPONENTS_FLOW, build_deployment_edit_flow
from opi.forms.wizard.save import apply_modal_edit
from opi.forms.wizard.state import WizardState
from opi.handlers.project_file_handler import remove_component
from opi.web.router_detail_edit import _render_section_html


def _project() -> dict[str, Any]:
    return {
        "schema-version": "2.9",
        "name": "proef",
        "clusters": ["sandboxed-local"],
        "components": [
            {"name": "alpha", "type": "single", "ports": {"inbound": [8000]}},
            {"name": "beta", "type": "single", "ports": {"inbound": [8001]}},
            {"name": "gamma", "type": "single", "ports": {"inbound": [8002]}, "uses-components": ["beta"]},
        ],
        "deployments": [
            {
                "name": "d1",
                "cluster": "sandboxed-local",
                "components": [
                    {"reference": "alpha", "image": "img/alpha:1"},
                    {"reference": "beta", "image": "img/beta:1"},
                    {"reference": "gamma", "image": "img/gamma:1"},
                ],
            },
            {
                "name": "d2",
                "cluster": "sandboxed-local",
                "components": [
                    {"reference": "beta", "image": "img/beta:1"},
                    {"reference": "gamma", "image": "img/gamma:1"},
                ],
            },
        ],
    }


async def _save(flow: Any, merged: dict[str, Any], project: dict[str, Any], selection: list[str] | None = None) -> dict:
    """Wat ``apply_modal_edit`` van deze inzending maakt."""
    state = WizardState(
        flow_id=flow.flow_id,
        current_step=flow.sections[0].section_id,
        active_sections=[section.section_id for section in flow.sections],
        selection=list(selection or []),
    )
    return await apply_modal_edit(
        copy.deepcopy(project),
        copy.deepcopy(merged),
        flow=flow,
        active_sections=list(flow.sections),
        state=state,
        project_name="proef",
    )


def _refs(data: dict[str, Any], deployment: str) -> list[str]:
    for dep in data["deployments"]:
        if dep["name"] == deployment:
            return [c["reference"] for c in dep.get("components") or []]
    raise AssertionError(f"deployment '{deployment}' weg")


def _names(data: dict[str, Any]) -> list[str]:
    return [c["name"] for c in data.get("components") or []]


@pytest.mark.asyncio
async def test_modal_verwijdert_component_en_zijn_verwijzingen() -> None:
    project = _project()
    zonder_beta = [c for c in project["components"] if c["name"] != "beta"]

    resultaat = await _save(MODAL_EDIT_COMPONENTS_FLOW, {"components": zonder_beta}, project)

    assert _names(resultaat) == ["alpha", "gamma"]
    assert _refs(resultaat, "d1") == ["alpha", "gamma"]
    assert _refs(resultaat, "d2") == ["gamma"]
    assert resultaat["components"][1]["uses-components"] == []


@pytest.mark.asyncio
async def test_modalweg_en_apiweg_geven_hetzelfde_bestand() -> None:
    project = _project()
    zonder_beta = [c for c in project["components"] if c["name"] != "beta"]

    via_modal = await _save(MODAL_EDIT_COMPONENTS_FLOW, {"components": zonder_beta}, project)

    via_api = copy.deepcopy(project)
    remove_component(via_api, "beta", confirm_in_use=True)

    assert via_modal["components"] == via_api["components"]
    assert via_modal["deployments"] == via_api["deployments"]


@pytest.mark.asyncio
async def test_component_onder_een_webadres_wordt_ook_in_de_modal_geweigerd() -> None:
    project = _project()
    project["deployments"][0]["services"] = [
        {"reference": "publish-on-web", "config": {"root-component": "beta"}},
    ]
    zonder_beta = [c for c in project["components"] if c["name"] != "beta"]

    with pytest.raises(ProjectIntegrityError, match="webadres"):
        await _save(MODAL_EDIT_COMPONENTS_FLOW, {"components": zonder_beta}, project)


@pytest.mark.asyncio
async def test_selectie_van_een_component_laat_de_rest_byte_gelijk() -> None:
    project = _project()
    alleen_beta = copy.deepcopy(project["components"][1])
    alleen_beta["ports"] = {"inbound": [9000]}

    resultaat = await _save(MODAL_EDIT_COMPONENTS_FLOW, {"components": [alleen_beta]}, project, selection=["beta"])

    assert _names(resultaat) == ["alpha", "beta", "gamma"]
    assert resultaat["components"][1]["ports"] == {"inbound": [9000]}
    assert resultaat["components"][0] == project["components"][0]
    assert resultaat["components"][2] == project["components"][2]


@pytest.mark.asyncio
async def test_selectie_van_twee_componenten_behoudt_de_volgorde() -> None:
    project = _project()
    selectie = [copy.deepcopy(project["components"][0]), copy.deepcopy(project["components"][2])]
    selectie[0]["ports"] = {"inbound": [9100]}

    resultaat = await _save(MODAL_EDIT_COMPONENTS_FLOW, {"components": selectie}, project, selection=["alpha", "gamma"])

    assert _names(resultaat) == ["alpha", "beta", "gamma"]
    assert resultaat["components"][0]["ports"] == {"inbound": [9100]}
    assert resultaat["components"][1] == project["components"][1]


@pytest.mark.asyncio
async def test_een_component_uit_de_selectie_halen_verwijdert_alleen_dat_component() -> None:
    project = _project()

    resultaat = await _save(MODAL_EDIT_COMPONENTS_FLOW, {"components": []}, project, selection=["beta"])

    assert _names(resultaat) == ["alpha", "gamma"]
    assert _refs(resultaat, "d2") == ["gamma"]


@pytest.mark.asyncio
async def test_selectie_op_een_naam_die_verdween_weigert_de_opslag() -> None:
    project = _project()
    gewijzigd = copy.deepcopy(project)
    gewijzigd["components"] = [c for c in gewijzigd["components"] if c["name"] != "beta"]

    with pytest.raises(ProjectIntegrityError, match="beta"):
        await _save(
            MODAL_EDIT_COMPONENTS_FLOW,
            {"components": [copy.deepcopy(project["components"][1])]},
            gewijzigd,
            selection=["beta"],
        )


@pytest.mark.asyncio
async def test_deploymentmodal_met_selectie_raakt_alleen_dat_component() -> None:
    project = _project()
    flow = build_deployment_edit_flow(0, component_count=3)
    gewijzigd = {"deployments": [{"name": "d1", "components": [{"reference": "beta", "image": "img/beta:2"}]}]}

    resultaat = await _save(flow, gewijzigd, project, selection=["beta"])

    d1 = resultaat["deployments"][0]["components"]
    assert [c["reference"] for c in d1] == ["alpha", "beta", "gamma"]
    assert d1[1]["image"] == "img/beta:2"
    assert d1[0] == project["deployments"][0]["components"][0]
    assert d1[2] == project["deployments"][0]["components"][2]
    assert resultaat["deployments"][1] == project["deployments"][1]


# ---------------------------------------------------------------------------
# Het echte formulierpad: de inzending door de processor, en dan opslaan.
#
# De toetsen hierboven geven ``apply_modal_edit`` een zelf samengestelde merged mee, en
# slaan daarmee precies de stap over die een rij kon laten verschuiven. Hieronder loopt
# de inzending wel door ``process_json_submission`` met ``edit_mode=True``, met een
# verwijderde rij die NIET de laatste is.
# ---------------------------------------------------------------------------


def _vier_componenten() -> dict[str, Any]:
    return {
        "schema-version": "2.9",
        "name": "proef",
        "clusters": ["sandboxed-local"],
        "components": [
            {"name": "web", "type": "single", "ports": {"inbound": [8000]}},
            {"name": "alpha", "type": "single", "ports": {"inbound": [8001]}},
            {"name": "beta", "type": "single", "ports": {"inbound": [8002]}},
            {"name": "gamma", "type": "single", "ports": {"inbound": [8003]}},
        ],
        "deployments": [
            {
                "name": "d1",
                "cluster": "sandboxed-local",
                "components": [{"reference": naam, "image": f"img/{naam}:1"} for naam in _VIER],
            },
        ],
    }


_VIER = ("web", "alpha", "beta", "gamma")


async def _save_via_formulier(project: dict[str, Any], getoonde_rijen: list[dict[str, Any]]) -> dict[str, Any]:
    """Wat het opslaan maakt van een inzending die precies *getoonde_rijen* draagt.

    Zoals de browser hem stuurt: elke rij draagt haar sleutel als verborgen veld (de
    naam is readonly en komt dus niet als tekstveld mee), plus het pad van de reeks die
    op het scherm stond.
    """
    flow = MODAL_EDIT_COMPONENTS_FLOW
    section = flow.sections[0]
    # Zoals modal_wizard_init: de lijst die een editable helemaal bezit staat in
    # step_data en NIET in base_data (anders groeit hij bij het mergen weer aan).
    state = WizardState(
        flow_id=flow.flow_id,
        current_step=section.section_id,
        active_sections=[section.section_id],
        base_data={k: v for k, v in copy.deepcopy(project).items() if k != "components"},
    )
    state.store_step_data(section.section_id, {"components": copy.deepcopy(project["components"])})

    submitted = {"components": copy.deepcopy(getoonde_rijen), GERENDERDE_REEKSEN_VELD: ["components"]}
    merged, errors = await EditableFormProcessor().process_json_submission(
        submitted,
        section.editables,
        state.get_merged_data(),
        edit_mode=True,
    )
    assert errors == {}
    state.store_step_data(section.section_id, {"components": merged["components"]})

    return await apply_modal_edit(
        copy.deepcopy(project),
        state.get_merged_data(strip_cleared=False),
        flow=flow,
        active_sections=[section],
        state=state,
        project_name="proef",
    )


def _poorten(data: dict[str, Any]) -> list[tuple[str, list[int]]]:
    return [(c["name"], c["ports"]["inbound"]) for c in data["components"]]


@pytest.mark.asyncio
async def test_een_rij_uit_het_midden_weghalen_laat_de_rest_op_zijn_plek() -> None:
    project = _vier_componenten()
    zonder_beta = [c for c in project["components"] if c["name"] != "beta"]

    resultaat = await _save_via_formulier(project, zonder_beta)

    assert _poorten(resultaat) == [("web", [8000]), ("alpha", [8001]), ("gamma", [8003])]
    assert _refs(resultaat, "d1") == ["web", "alpha", "gamma"]


@pytest.mark.asyncio
async def test_de_inzending_kan_de_naam_van_een_rij_niet_wijzigen() -> None:
    """De sleutel bepaalt WELKE rij dit is, en is geen waarde die de inzending zet."""
    project = _vier_componenten()
    rijen = copy.deepcopy(project["components"])
    rijen[1]["name"] = "omgedoopt"

    resultaat = await _save_via_formulier(project, rijen)

    assert _names(resultaat) == list(_VIER)


@pytest.mark.asyncio
async def test_een_rij_wijzigen_zonder_er_een_weg_te_halen() -> None:
    project = _vier_componenten()
    rijen = copy.deepcopy(project["components"])
    rijen[1]["ports"]["inbound"] = [9001]

    resultaat = await _save_via_formulier(project, rijen)

    assert _poorten(resultaat) == [("web", [8000]), ("alpha", [9001]), ("beta", [8002]), ("gamma", [8003])]
    assert _refs(resultaat, "d1") == list(_VIER)


@pytest.mark.asyncio
async def test_het_formulier_draagt_de_sleutel_van_elke_rij() -> None:
    """Waarop de toetsen hierboven leunen: de rij stuurt haar naam mee.

    Het naamveld is readonly en rendert als ``disabled``, dus de browser stuurt het niet
    mee. Zonder dit verborgen veld paart de verwerker weer op index.
    """
    html = _render_section_html(MODAL_EDIT_COMPONENTS_FLOW.sections[0], _vier_componenten(), locked_services=None)

    for index, naam in enumerate(_VIER):
        assert f'<input type="hidden" name="components[{index}]/name" value="{naam}">' in html
