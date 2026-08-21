"""Round-trip tests voor de serviceselectie in de component-bewerkmodal.

Niet de losse onderdelen maar de hele weg: init (split + base_data), submit
(verwerking, verzoening, extractie), finale merge en ``apply_modal_edit``. De
regressies die dit bestand afdekt zaten allemaal TUSSEN de onderdelen: elke stap
deed lokaal iets verdedigbaars, en de keten gooide een aangevinkte dienst weg
(send-email), zette een uitgevinkte terug (redis) en versmolt de record- en
legacy-vorm tot een dubbelvormige entry in het projectbestand.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.visualizers.flows import flow_context_from_base, get_flow
from opi.forms.wizard.mutation import apply_component_services_mutation, apply_services_mutation
from opi.forms.wizard.save import apply_modal_edit
from opi.forms.wizard.services_merge import merge_service_lists
from opi.forms.wizard.state import WizardState
from opi.services.services import service_entry_name
from opi.web.router_detail_edit import _fully_owned_list_keys, _owned_item_selection_paths, _pad_sparse_submission
from opi.web.router_wizard import _extract_section_data, _split_data_across_sections

FLOW_ID = "modal-edit-component-0"
SECTION_ID = "component-edit-0"


def _project() -> dict[str, Any]:
    """Een project naar het evenbeeld van ai1-uit: alle diensten aan, componentlijst in record-vorm."""
    return {
        "schema-version": "2.7",
        "name": "ai1-uit",
        "display-name": "Alles in 1",
        "clusters": ["sandboxed-local"],
        "repositories": [{"name": "main-repo", "url": "https://x", "branch": "main", "path": "."}],
        "services": [
            "publish-on-web",
            "keycloak",
            "metrics-scraper",
            "health-check",
            "persistent-storage",
            "temp-storage",
            "minio-storage",
            {"name": "send-email", "config": {"from-name": "Robbert", "messages-per-day": 100}},
            {"name": "redis", "config": {"acl-key-prefix": True}},
            "sleep-mode",
        ],
        "components": [
            {
                "name": "test",
                "path": [{"match": "/"}],
                "ports": {"inbound": [8080]},
                "image": "ghcr.io/minbzk/base-images/e2e-allservices:latest",
                "resources": {
                    "requests": {"memory": "64Mi", "cpu": "50m"},
                    "limits": {"memory": "256Mi", "cpu": "1"},
                },
                "services": [
                    {"reference": "publish-on-web", "config": {"tls": "standard"}},
                    "keycloak",
                    {"reference": "health-check", "config": {"port": 8080, "liveness-path": "/"}},
                    "minio-storage",
                    "redis",
                    "sleep-mode",
                ],
            }
        ],
        "deployments": [
            {
                "name": "productie",
                "cluster": "sandboxed-local",
                "namespace": "ai1-uit",
                "components": [{"reference": "test"}],
            }
        ],
        "users": [{"email": "admin@sandbox.rijksapp.dev", "role": "admin"}],
    }


def _body(services: list[str]) -> dict[str, Any]:
    """Wat de browser post: legacy-vorm config voor de eigen diensten, kale namen voor de rest."""
    return {
        "components": [
            {
                "image": "ghcr.io/minbzk/base-images/e2e-allservices:latest",
                "resources": {"requests": {"memory": "64Mi"}, "limits": {"memory": "256Mi"}},
                "ports": {"inbound": ["8080"]},
                "services": list(services),
                "path": [{"match": "/", "rewrite": ""}],
                "_services-config": [
                    {"publish-on-web": {"config": {"tls": "standard"}}},
                    {"health-check": {"config": {"scheme": "", "port": "8080", "liveness-path": "/"}}},
                ],
            }
        ],
        "_gerenderde-reeksen": ["components[0]/ports/inbound", "components[0]/path"],
    }


async def _round_trip(project: dict[str, Any], posted_services: list[str]) -> list[Any]:
    """De volledige modalweg: init, submit-verwerking, finale merge, apply_modal_edit."""
    project_data = copy.deepcopy(project)
    flow = get_flow(FLOW_ID, **dict(flow_context_from_base(FLOW_ID, project_data)))
    section = next(s for s in flow.sections if s.section_id == SECTION_ID)
    processor = EditableFormProcessor()
    for s in flow.sections:
        processor.populate_deferred_fields(project_data, s.editables)

    # modal_wizard_init
    state = WizardState(
        flow_id=FLOW_ID,
        current_step=SECTION_ID,
        active_sections=[s.section_id for s in flow.sections],
        step_data=_split_data_across_sections(flow, project_data),
    )
    state.populate_virt_mappings(flow.sections)
    owned = _fully_owned_list_keys(flow)
    state.base_data = {k: v for k, v in project_data.items() if k not in owned}
    for list_key, idx in _owned_item_selection_paths(flow):
        items = state.base_data.get(list_key)
        if isinstance(items, list) and 0 <= idx < len(items) and isinstance(items[idx], dict):
            items = list(items)
            slimmed = dict(items[idx])
            slimmed.pop("services", None)
            items[idx] = slimmed
            state.base_data[list_key] = items

    # modal_wizard_submit_step
    submitted_data = _pad_sparse_submission(_body(posted_services), flow, SECTION_ID)
    yaml_data = state.get_merged_data()
    submitted_yaml, errors = await processor.process_json_submission(
        submitted_data, section.editables, yaml_data, edit_mode=True, enforcer_context={"project_name": "ai1-uit"}
    )
    assert errors == {}
    processor.clear_hidden_depends_on(section.editables, submitted_yaml)
    apply_services_mutation(section.editables, yaml_data, submitted_yaml)
    apply_component_services_mutation(section.editables, yaml_data, submitted_yaml)
    state.step_data[SECTION_ID] = _extract_section_data(section.editables, submitted_yaml)

    # _modal_do_submit
    merged = state.get_merged_data(strip_cleared=False)
    result = await apply_modal_edit(
        copy.deepcopy(project),
        merged,
        flow=flow,
        active_sections=list(flow.sections),
        state=state,
        project_name="ai1-uit",
    )
    return result["components"][0]["services"]


PRE_EDIT = ["publish-on-web", "keycloak", "health-check", "minio-storage", "redis", "sleep-mode"]


@pytest.mark.asyncio
async def test_added_component_service_lands_in_the_file() -> None:
    """Een aangevinkte dienst zonder eigen configsectie (send-email) overleeft de save."""
    saved = await _round_trip(_project(), [*PRE_EDIT, "send-email"])
    assert "send-email" in [service_entry_name(e) for e in saved]


@pytest.mark.asyncio
async def test_unticked_component_service_leaves_the_file() -> None:
    """Een uitgevinkte dienst komt niet terug uit de basiskopie."""
    saved = await _round_trip(_project(), [s for s in PRE_EDIT if s != "redis"])
    assert "redis" not in [service_entry_name(e) for e in saved]


@pytest.mark.asyncio
async def test_not_offered_component_service_survives() -> None:
    """Een dienst die de picker niet toont (niet meer project-enabled) is niet uitgevinkt."""
    project = _project()
    project["services"] = [e for e in project["services"] if service_entry_name(e) != "sleep-mode"]
    # sleep-mode staat nog op het component maar niet in de picker; de POST draagt hem niet.
    saved = await _round_trip(project, [s for s in PRE_EDIT if s != "sleep-mode"])
    assert "sleep-mode" in [service_entry_name(e) for e in saved]


@pytest.mark.asyncio
async def test_saved_entries_keep_one_shape() -> None:
    """Record- en legacy-vorm versmelten niet: elke entry heeft precies één identiteit."""
    saved = await _round_trip(_project(), [*PRE_EDIT, "send-email"])
    for entry in saved:
        if not isinstance(entry, dict):
            continue
        name = service_entry_name(entry)
        assert not ("reference" in entry and name in entry), f"dubbelvormige entry: {entry}"
    by_name = {service_entry_name(e): e for e in saved if isinstance(e, dict)}
    assert by_name["health-check"]["config"]["liveness-path"] == "/"


def test_merge_service_lists_normalizes_mismatched_shapes() -> None:
    """De naam-merge zet de legacy-vorm om naar de record-vorm van zijn partner."""
    existing = [{"reference": "metrics-scraper", "config": {"port": 8080, "path": "/metrics"}}]
    incoming = [{"metrics-scraper": {"port": 9090, "path": "/metrics"}}]
    merged = merge_service_lists(existing, incoming)
    assert merged == [{"reference": "metrics-scraper", "config": {"port": 9090, "path": "/metrics"}}]
