"""No-op round trips door elke modalflow: opslaan zonder te wijzigen mag niets kwijtraken.

De detector voor de hele klasse "stille drops": voor iedere modalflow wordt de volledige
weg doorlopen (init met split en base_data, per sectie een submit met precies wat het
gerenderde formulier zou posten, finale merge, ``apply_modal_edit``), en daarna mag het
resultaat niets MISSEN of WIJZIGEN ten opzichte van het origineel. Toevoegingen zijn
toegestaan: een save mag defaults en goedkeuringsblokken uitschrijven, maar wat er stond
is van de gebruiker en verdwijnt niet.

Gevonden met precies deze sweep (2026-08-20): de services-modal gooide config weg van
services zonder eigen configsectie in de flow (de domeingoedkeuringen onder
publish-on-web), waarna een hook het al goedgekeurde domein opnieuw aanvroeg; en de
componentmodal flipte een attachments-record naar de legacy-vorm. Beide door
``apply_selection_mutation`` dat zijn eigen contract ("een overlevende service houdt
zijn basisvorm") niet nakwam.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.visualizers.flows import FLOW_REGISTRY, flow_context_from_base, get_flow
from opi.forms.wizard.mutation import apply_component_services_mutation, apply_services_mutation
from opi.forms.wizard.save import apply_modal_edit
from opi.forms.wizard.state import WizardState
from opi.handlers.project_file_handler import extract_attachment_catalog
from opi.services.schema_migration import normalize_service_entries
from opi.services.services import service_entry_name
from opi.web.router_detail_edit import (
    _extract_services,
    _fully_owned_list_keys,
    _owned_item_selection_paths,
    _pad_sparse_submission,
    _strip_attachment_content,
)
from opi.web.router_wizard import _extract_section_data, _split_data_across_sections


def _project() -> dict[str, Any]:
    """Rijk, realistisch project: record-vorm entries, goedkeuringen, secrets, bijlagen."""
    return {
        "schema-version": "2.7",
        "name": "sweep",
        "display-name": "Sweep",
        "description": "sweeptest",
        "clusters": ["sandboxed-local"],
        "repositories": [{"name": "main-repo", "url": "https://x", "branch": "main", "path": "."}],
        "services": [
            {
                "name": "publish-on-web",
                "config": {
                    "domains": {
                        "allowed-domains": [{"domain": "rijksapp.dev", "status": "approved", "history": []}],
                        "allowed-subdomains": [{"name": "sweep", "status": "approved", "history": []}],
                    }
                },
            },
            {
                "name": "keycloak",
                "config": {
                    "template": "sso-support",
                    "restrict-access": {"enabled": True, "realm-role": "allowed-user"},
                    "realms": [{"host": "https://kc", "realm": "sweep", "username": "u", "password": "GEHEIM"}],
                },
            },
            "metrics-scraper",
            "health-check",
            "persistent-storage",
            "temp-storage",
            {"name": "postgresql-database", "config": {"schemas": [{"postfix": "extra", "description": "d"}]}},
            "minio-storage",
            {
                "name": "send-email",
                "config": {
                    "from-name": "Sweep",
                    "messages-per-day": 100,
                    "approval": {"status": "approved", "history": []},
                },
            },
            {"name": "redis", "config": {"acl-key-prefix": True}},
            {"attachments": {"data": [{"id": "att1", "filename": "cert.pem", "content": "VERSLEUTELD"}]}},
            "sleep-mode",
            {"name": "invite", "config": {"active": [], "default-language": "nl"}},
            {"name": "cross-domain-access", "config": {"share": [{"name": "regel", "with": "ander/dep"}]}},
            {"name": "authorization-wall", "config": {"banner": "welkom"}},
        ],
        "components": [
            {
                "name": "web",
                "path": [{"match": "/"}],
                "ports": {"inbound": [8080]},
                "image": "ghcr.io/x/web:latest",
                "resources": {
                    "requests": {"memory": "64Mi", "cpu": "50m"},
                    "limits": {"memory": "256Mi", "cpu": "1"},
                },
                "services": [
                    {"reference": "publish-on-web", "config": {"tls": "standard"}},
                    "keycloak",
                    {"reference": "health-check", "config": {"port": 8080, "liveness-path": "/"}},
                    {
                        "reference": "persistent-storage",
                        "config": [{"name": "data", "size": "100Mi", "mount-path": "/d"}],
                    },
                    "minio-storage",
                    "redis",
                    "send-email",
                    {"reference": "attachments", "config": [{"reference": "att1", "provide-as": "file", "path": "/c"}]},
                ],
            }
        ],
        "deployments": [
            {
                "name": "productie",
                "cluster": "sandboxed-local",
                "namespace": "sweep",
                "components": [{"reference": "web", "image": "ghcr.io/x/web:v1"}],
                "services": [
                    {
                        "reference": "publish-on-web",
                        "config": {"base-domain": "rijksapp.dev", "subdomain": "sweep", "domain-format": "subdomain"},
                    },
                    {"reference": "cross-domain-access", "config": {"use": [{"rule": "regel", "deployment": "dep"}]}},
                ],
                "backup": {"schedule": "FREQ=DAILY;BYHOUR=3"},
            }
        ],
        "users": [{"email": "admin@sandbox.rijksapp.dev", "role": "admin"}],
    }


def _faithful_body(section: Any, merged: dict[str, Any]) -> dict[str, Any]:
    """Wat het gerenderde formulier van deze sectie zou posten: alle zichtbare,
    niet-readonly velden met hun huidige waarde (checkbox-semantiek incluis)."""
    from opi.forms.editables.editable import apply_virtualize
    from opi.forms.editables.service_path import smart_get_value, smart_set_value
    from opi.forms.visualizers.bridge import evaluate_show_when

    body: dict[str, Any] = {}
    rendered_seqs: list[str] = []

    def visit(vis_list: list[Any], parent_virt: Any = None) -> None:
        for vis in vis_list:
            widget = str(vis.widget)
            if widget == "group":
                visit(vis.children or [], parent_virt)
                continue
            ed = vis.editable
            if getattr(vis, "readonly", False):
                continue
            if ed.depends_on and "[*]" not in ed.depends_on:
                dep = smart_get_value(merged, ed.depends_on)
                if not evaluate_show_when(dep, ed.show_when):
                    continue
            if "[*]" in ed.yaml_path and widget != "sequence":
                continue
            virt = ed.virtualize or parent_virt
            form_path = apply_virtualize(ed.yaml_path, virt) if virt else ed.yaml_path
            value = smart_get_value(merged, ed.yaml_path)
            if value is None:
                default = ed.default
                value = default(merged) if callable(default) else default
            if widget == "sequence":
                rendered_seqs.append(form_path)
                smart_set_value(body, form_path, copy.deepcopy(value) if isinstance(value, list) else [])
                continue
            if widget == "checkbox":
                value = value in (True, "true", "on", "yes", "1")
            elif ed.converter is not None:
                value = ed.converter.read(value, context_data=merged)
            if value is None:
                continue
            smart_set_value(body, form_path, copy.deepcopy(value))

    visit(list(section.editables))
    if rendered_seqs:
        body["_gerenderde-reeksen"] = rendered_seqs
    return body


def _losses(expected: Any, actual: Any, path: str = "") -> list[tuple[str, Any, Any]]:
    """Verwijderingen en waardewijzigingen; toevoegingen zijn toegestaan."""
    out: list[tuple[str, Any, Any]] = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key, value in expected.items():
            sub = f"{path}/{key}" if path else str(key)
            if key not in actual:
                out.append((sub, value, "<verwijderd>"))
            else:
                out.extend(_losses(value, actual[key], sub))
    elif isinstance(expected, list) and isinstance(actual, list):
        if len(actual) < len(expected):
            out.append((f"{path}[len]", len(expected), len(actual)))
        for i, (exp_item, act_item) in enumerate(zip(expected, actual, strict=False)):
            out.extend(_losses(exp_item, act_item, f"{path}[{i}]"))
    elif expected != actual:
        out.append((path, expected, actual))
    return out


def _allowed(path: str, before: Any, after: Any) -> bool:
    # Equivalente RRULE-normalisatie door de scheduleconverter.
    if path.endswith("/backup/schedule") and isinstance(before, str) and after == f"{before};BYMINUTE=0":
        return True
    # Een kale selectie mag tot een record met defaultconfig promoveren (sleep-mode);
    # er stond geen config, dus er gaat niets verloren.
    return bool(isinstance(before, str) and isinstance(after, dict) and service_entry_name(after) == before)


async def _roundtrip(flow_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """(verwacht, resultaat) van een no-op save door *flow_id*."""
    original = _project()
    project_data = copy.deepcopy(original)
    flow = get_flow(flow_id, **dict(flow_context_from_base(flow_id, project_data)))

    processor = EditableFormProcessor()
    for section in flow.sections:
        processor.populate_deferred_fields(project_data, section.editables)

    session_data = _strip_attachment_content(project_data)
    step_data = _split_data_across_sections(flow, session_data)
    state = WizardState(
        flow_id=flow_id,
        current_step=flow.sections[0].section_id,
        active_sections=[s.section_id for s in flow.sections],
        step_data=step_data,
    )
    state.populate_virt_mappings(flow.sections)
    state.locked_services = _extract_services(project_data)
    owned = _fully_owned_list_keys(flow)
    state.base_data = {k: v for k, v in session_data.items() if k not in owned}
    for list_key, idx in _owned_item_selection_paths(flow):
        items = state.base_data.get(list_key)
        if isinstance(items, list) and 0 <= idx < len(items) and isinstance(items[idx], dict):
            items = list(items)
            slimmed = dict(items[idx])
            slimmed.pop("services", None)
            items[idx] = slimmed
            state.base_data[list_key] = items

    for section in flow.sections:
        body = _faithful_body(section, state.get_merged_data())
        submitted_data = _pad_sparse_submission(body, flow, section.section_id)
        yaml_data = state.get_merged_data()
        submitted_yaml, errors = await processor.process_json_submission(
            submitted_data, section.editables, yaml_data, edit_mode=True, enforcer_context={"project_name": "sweep"}
        )
        assert errors == {}, f"{flow_id}/{section.section_id}: {errors}"
        processor.clear_hidden_depends_on(section.editables, submitted_yaml)
        apply_services_mutation(section.editables, yaml_data, submitted_yaml)
        apply_component_services_mutation(section.editables, yaml_data, submitted_yaml)
        state.step_data[section.section_id] = _extract_section_data(section.editables, submitted_yaml)

    merged = state.get_merged_data(strip_cleared=False)
    original_content = {
        att_id: entry.get("content")
        for att_id, entry in extract_attachment_catalog(original).items()
        if isinstance(entry, dict) and entry.get("content")
    }
    result = await apply_modal_edit(
        copy.deepcopy(original),
        merged,
        flow=flow,
        active_sections=list(flow.sections),
        state=state,
        project_name="sweep",
        original_attachment_content=original_content,
    )
    expected = copy.deepcopy(original)
    normalize_service_entries(expected)
    return expected, result


# modal-edit-domain-N ontbreekt bewust: zijn enforcer doet een echte DNS-lookup en kan
# dus niet offline draaien; de flow is gedekt door tests/e2e/test_edit_wizard.py.
MODAL_FLOWS = [f for f in FLOW_REGISTRY if f.startswith("modal-") and f != "modal-backup"] + [
    "modal-edit-component-0",
    "modal-edit-deployment-0",
    "modal-edit-backup-schedule-0",
    "modal-edit-cross-domain-deployment-0",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("flow_id", MODAL_FLOWS)
async def test_noop_save_loses_nothing(flow_id: str) -> None:
    expected, result = await _roundtrip(flow_id)
    losses = [(p, a, b) for p, a, b in _losses(expected, result) if not _allowed(p, a, b)]
    assert losses == [], f"{flow_id} raakt data kwijt bij een save zonder wijzigingen: {losses}"


@pytest.mark.asyncio
async def test_services_modal_keeps_domain_approvals() -> None:
    """De regressie die deze sweep vond: config van een service zonder eigen
    configsectie in de flow (publish-on-web domains) overleeft de services-save."""
    _expected, result = await _roundtrip("modal-edit-services")
    pow_entry = next(e for e in result["services"] if service_entry_name(e) == "publish-on-web")
    domains = pow_entry["config"]["domains"]
    assert domains["allowed-domains"][0]["status"] == "approved"
    assert domains["allowed-subdomains"][0]["name"] == "sweep"


@pytest.mark.asyncio
async def test_component_modal_keeps_attachments_record_shape() -> None:
    """De attachments-koppeling op het component blijft in de record-vorm staan."""
    _expected, result = await _roundtrip("modal-edit-component-0")
    entry = next(e for e in result["components"][0]["services"] if service_entry_name(e) == "attachments")
    assert entry.get("reference") == "attachments"
    assert entry["config"] == [{"reference": "att1", "provide-as": "file", "path": "/c"}]
