"""De OTAP-markering door de echte formulierweg: zetten, en terug op de standaard (RC-330).

Het randgeval dat stil blijft als het stuk gaat, is de laatste: zet iemand een deployment
terug op de standaard, dan moet ``remove_when_none`` de sleutel WEGHALEN. Blijft hij staan
met een lege waarde, dan meldt niets dat er iets mis is.

Hier wordt de weg van het formulier doorlopen, niet alleen de merge: het lichaam dat de
browser zou posten gaat door ``process_json_submission`` (daar woont ``remove_when_none``)
en daarna door ``apply_modal_edit``. De deploymentmodal is niet in de lokale browser te
meten: zijn opslag doet ``process_project`` en zonder takenservice geeft die route een 503.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.editables.service_path import smart_set_value
from opi.forms.visualizers.flows import get_flow
from opi.forms.wizard.save import apply_modal_edit
from opi.web.router_detail_edit import _pad_sparse_submission
from opi.web.router_wizard import _extract_section_data
from tests.forms.test_flow_write_isolation import PROJECT_NAME, _seed_state, build_project
from tests.forms.test_modal_noop_roundtrip import _faithful_body


async def _save_door_het_formulier(
    project_data: dict[str, Any],
    flow_id: str,
    yaml_path: str,
    waarde: Any,
    **flow_context: Any,
) -> dict[str, Any]:
    """Post het formulier zoals de browser dat doet, met dit ene veld gezet."""
    flow = get_flow(flow_id, **flow_context)
    assert len(flow.sections) == 1, f"{flow_id} heeft meer dan een sectie; dit harnas post er een"
    section = flow.sections[0]
    state = _seed_state(flow, flow_id, project_data)

    body = _faithful_body(section, state.get_merged_data())
    smart_set_value(body, yaml_path, waarde)

    processor = EditableFormProcessor()
    submitted_yaml, errors = await processor.process_json_submission(
        _pad_sparse_submission(body, flow, section.section_id),
        section.editables,
        state.get_merged_data(),
        edit_mode=True,
        enforcer_context={"project_name": PROJECT_NAME},
    )
    assert errors == {}, f"{flow_id}: {errors}"
    state.store_step_data(section.section_id, _extract_section_data(section.editables, submitted_yaml))

    return await apply_modal_edit(
        copy.deepcopy(project_data),
        state.get_merged_data(strip_cleared=False),
        flow=flow,
        active_sections=[section],
        state=state,
        project_name=PROJECT_NAME,
    )


@pytest.mark.asyncio
async def test_de_deploymentmarkering_komt_in_het_bestand() -> None:
    result = await _save_door_het_formulier(
        build_project(), "modal-edit-deployment-0", "deployments[0]/environment", "acceptatie", component_count=2
    )

    assert result["deployments"][0]["environment"] == "acceptatie"
    assert "environment" not in result["deployments"][1], "de bewerking raakte de andere deployment"


@pytest.mark.asyncio
async def test_terug_op_de_standaard_haalt_de_sleutel_weg() -> None:
    """Leeg betekent ontwikkeling, en dan hoort de sleutel niet als lege waarde te blijven."""
    project_data = build_project()
    project_data["deployments"][0]["environment"] = "productie"

    result = await _save_door_het_formulier(
        project_data, "modal-edit-deployment-0", "deployments[0]/environment", "", component_count=2
    )

    assert "environment" not in result["deployments"][0]
