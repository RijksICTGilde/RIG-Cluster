"""Every flow may only change what it was opened for.

One edit through one flow must leave the rest of the project file exactly as
it was - same values, same key order, byte for byte. Four separate bugs got
in through that gap (a cleared field that came back, a project-level service
config that vanished, a second service-config edit that vanished, encrypted
values that lost their block form), each found and fixed on its own. This
test is the net that catches the next one before it ships.

The harness drives the real save path (``apply_modal_edit``) on plain dicts:
seed a wizard session from a project file the way the modal wizard does,
change exactly one field, save, and compare the dumped YAML against the same
project with only that one field changed by hand.
"""

from __future__ import annotations

import copy
import re
from typing import Any

import pytest
from opi.forms.editables.service_path import smart_get_value, smart_set_value
from opi.forms.visualizers.flows import get_flow
from opi.forms.wizard.resolver import resolve_active_sections
from opi.forms.wizard.save import apply_modal_edit
from opi.forms.wizard.state import WizardState
from opi.utils.yaml_util import dump_yaml_to_string
from opi.web.router_detail_edit import _fully_owned_list_keys
from opi.web.router_wizard import _extract_section_data, _split_data_across_sections

PROJECT_NAME = "netjes"

_INDEX_RE = re.compile(r"\[\d+\]")


def build_project() -> dict[str, Any]:
    """A project file with something in every corner a flow could tread on.

    Deliberately not minimal: services with and without config, two
    components, two deployments, an OPI-managed ``config`` block, and a
    registry. Anything a flow leaves alone here must come back unchanged.
    """
    return {
        "schema-version": 2,
        "name": PROJECT_NAME,
        "display-name": "Netjes Project",
        "description": "Blijft heel bij elke bewerking",
        "users": [
            {"email": "admin@rijksoverheid.nl", "role": "admin"},
            {"email": "dev@rijksoverheid.nl", "role": "developer"},
        ],
        "clusters": ["sandboxed-local"],
        "services": [
            "publish-on-web",
            {
                "name": "keycloak",
                "schema-version": "1.0",
                "config": {
                    "template": "sso-only",
                    "additional_redirect_uris": ["http://localhost:8080/*"],
                },
            },
            {
                "name": "namespace-postgresql-database",
                "schema-version": "1.0",
                "config": {"instances": 1, "storage": "1Gi"},
            },
        ],
        "registries": [
            {
                "name": "github-registry",
                "url": "ghcr.io",
                "username": "someuser",
                "password": "-----BEGIN AGE ENCRYPTED FILE-----\nabc\n-----END AGE ENCRYPTED FILE-----\n",
            }
        ],
        "repositories": [
            {
                "name": "main-repo",
                "url": "https://example.invalid/app.git",
                "branch": "main",
                "path": ".",
                "project_name": PROJECT_NAME,
            }
        ],
        "components": [
            {
                "name": "frontend",
                "type": "frontend",
                "ports": {"inbound": [3000], "outbound": [443]},
                "path": "/",
                "uses-components": [],
                "resources": {"cpu": "1", "limits": {"memory": "256Mi"}},
                "services": ["publish-on-web", "keycloak"],
            },
            {
                "name": "backend",
                "type": "single",
                "ports": {"inbound": [8000], "outbound": [443]},
                "path": [{"match": "/api"}],
                "uses-components": [],
                "resources": {"cpu": "1", "limits": {"memory": "512Mi"}},
                "services": ["publish-on-web", "namespace-postgresql-database"],
            },
        ],
        "deployments": [
            {
                "name": "productie",
                "cluster": "sandboxed-local",
                "namespace": PROJECT_NAME,
                "repository": "main-repo",
                "domain-format": "subdomain",
                "base-domain": "sandbox.rijksapp.dev",
                "subdomain": "netjes",
                "issuer": "letsencrypt",
                "components": [
                    {"reference": "frontend", "image": "example.invalid/frontend:1.0"},
                    {"reference": "backend", "image": "example.invalid/backend:1.0"},
                ],
            },
            {
                "name": "acceptatie",
                "cluster": "sandboxed-local",
                "namespace": f"{PROJECT_NAME}-acc",
                "repository": "main-repo",
                "domain-format": "subdomain",
                "base-domain": "sandbox.rijksapp.dev",
                "subdomain": "netjes-acc",
                "issuer": "letsencrypt",
                "components": [
                    {"reference": "frontend", "image": "example.invalid/frontend:0.9"},
                ],
            },
        ],
        "config": {
            "age-public-key": "age1d489e9c48pmwam6603vecp7y29zz9fx5cgpe9uk6cu9l7asfzg9sx5s0tq",
            "api-key": "-----BEGIN AGE ENCRYPTED FILE-----\ndef\n-----END AGE ENCRYPTED FILE-----\n",
        },
    }


def _seed_state(flow: Any, flow_id: str, project_data: dict[str, Any]) -> WizardState:
    """Seed a wizard session from *project_data*, as ``modal_wizard_init`` does."""
    step_data = _split_data_across_sections(flow, project_data)
    state = WizardState(
        flow_id=flow_id,
        current_step=flow.sections[0].section_id,
        project_name=PROJECT_NAME,
    )
    state.step_data = step_data
    state.active_sections = [section.section_id for section in flow.sections]
    state.populate_virt_mappings(flow.sections)
    owned = _fully_owned_list_keys(flow)
    state.template_data = {k: v for k, v in copy.deepcopy(project_data).items() if k not in owned}
    return state


def _generalize(yaml_path: str) -> str:
    """``deployments[0]/components[1]/image`` -> ``deployments[*]/components[*]/image``."""
    return _INDEX_RE.sub("[*]", yaml_path)


def _assert_flow_declares(flow: Any, yaml_path: str) -> None:
    """Guard against typos in the table below: the flow must own the path it edits.

    Matched ignoring concrete list indices: a sequence editable declares
    ``components[*]/name`` while the edit targets ``components[1]/name``, and
    a group declares only its own prefix.
    """
    wanted = _generalize(yaml_path)

    def declares(vis_list: list[Any]) -> bool:
        for vis in vis_list:
            declared = _generalize(vis.editable.yaml_path)
            if wanted == declared or wanted.startswith((declared + "/", declared + "[")):
                return True
            if vis.children and declares(vis.children):
                return True
        return False

    if not any(declares(section.editables) for section in flow.sections):
        msg = f"No editable in flow {flow.flow_id} declares {yaml_path}"
        raise AssertionError(msg)


def _active_sections(flow: Any, state: WizardState) -> list[Any]:
    """The sections the router would treat as active for this flow."""
    sections = list(flow.sections) if len(flow.sections) == 1 else list(resolve_active_sections(flow, state.step_data))
    state.active_sections = [section.section_id for section in sections]
    return sections


async def run_flow_edit(
    project_data: dict[str, Any],
    flow_id: str,
    yaml_path: str,
    new_value: Any,
    **flow_context: Any,
) -> dict[str, Any]:
    """Change one field through one flow and return the project that would be saved."""
    flow = get_flow(flow_id, **flow_context)
    state = _seed_state(flow, flow_id, project_data)
    _assert_flow_declares(flow, yaml_path)
    active_sections = _active_sections(flow, state)

    # Walk the whole flow the way a user does: every step is submitted, and
    # every step but one submits back exactly what it was shown.
    submitted = state.get_merged_data()
    smart_set_value(submitted, yaml_path, new_value)
    for section in active_sections:
        state.store_step_data(section.section_id, _extract_section_data(section.editables, submitted))

    merged_data = state.get_merged_data(strip_cleared=False)
    return await apply_modal_edit(
        copy.deepcopy(project_data),
        merged_data,
        flow=flow,
        active_sections=active_sections,
        state=state,
        project_name=PROJECT_NAME,
    )


# (flow_id, yaml_path, new value, extra flow-builder context)
FLOW_EDITS: list[tuple[str, str, Any, dict[str, Any]]] = [
    ("modal-edit-identity", "display-name", "Netjes Project v2", {}),
    ("modal-edit-identity", "description", "Andere omschrijving", {}),
    ("modal-edit-keycloak-config", "services/keycloak/config/template", "algoritmeregister", {}),
    (
        "modal-edit-postgresql-config",
        "services/namespace-postgresql-database/config/storage",
        "2Gi",
        {},
    ),
    ("modal-edit-component-0", "components[0]/resources/limits/memory", "1Gi", {}),
    ("modal-edit-component-1", "components[1]/resources/limits/memory", "1Gi", {}),
    ("modal-edit-domain-0", "deployments[0]/subdomain", "netjes-nieuw", {}),
    ("modal-edit-domain-1", "deployments[1]/subdomain", "netjes-acc2", {}),
    (
        "modal-edit-deployment-0",
        "deployments[0]/components[0]/image",
        "example.invalid/frontend:2.0",
        {"component_count": 2},
    ),
    (
        "modal-edit-deployment-1",
        "deployments[1]/components[0]/image",
        "example.invalid/frontend:2.0",
        {"component_count": 2},
    ),
    ("modal-edit-team", "users[1]/role", "admin", {}),
    ("modal-edit-components", "components[1]/resources/limits/memory", "1Gi", {}),
    ("modal-edit-services", "services/keycloak/config/template", "algoritmeregister", {}),
    ("modal-edit-backup-schedule-0", "deployments[0]/backup/schedule", "0 3 * * *", {}),
]


@pytest.mark.parametrize(
    ("flow_id", "yaml_path", "new_value", "flow_context"),
    FLOW_EDITS,
    ids=[f"{f}:{p}" for f, p, _v, _c in FLOW_EDITS],
)
@pytest.mark.asyncio
async def test_edit_touches_only_its_own_field(
    flow_id: str,
    yaml_path: str,
    new_value: Any,
    flow_context: dict[str, Any],
) -> None:
    project_data = build_project()

    expected = copy.deepcopy(project_data)
    smart_set_value(expected, yaml_path, new_value)

    result = await run_flow_edit(project_data, flow_id, yaml_path, new_value, **flow_context)

    assert smart_get_value(result, yaml_path) == new_value, f"{flow_id} did not apply its own edit"
    assert dump_yaml_to_string(result) == dump_yaml_to_string(expected), f"{flow_id} changed more than {yaml_path}"


NO_CHANGE_FLOWS = list(
    dict.fromkeys((flow_id, path, tuple(sorted(ctx.items()))) for flow_id, path, _v, ctx in FLOW_EDITS)
)


@pytest.mark.parametrize(
    ("flow_id", "yaml_path", "flow_context"),
    NO_CHANGE_FLOWS,
    ids=[f"{f}:{p}" for f, p, _c in NO_CHANGE_FLOWS],
)
@pytest.mark.asyncio
async def test_opening_and_saving_without_changing_anything_changes_nothing(
    flow_id: str,
    yaml_path: str,
    flow_context: tuple[tuple[str, Any], ...],
) -> None:
    """Walking a flow and pressing save must leave the file exactly as it was.

    This is the return trip for every field a flow shows but the user may not
    type in: a readonly name, an image the form only displays. They are still
    written back, so what goes out must equal what came in - down to the key
    order.
    """
    project_data = build_project()
    current = smart_get_value(project_data, yaml_path)

    result = await run_flow_edit(project_data, flow_id, yaml_path, current, **dict(flow_context))

    assert dump_yaml_to_string(result) == dump_yaml_to_string(build_project()), f"{flow_id} changed something by itself"


@pytest.mark.asyncio
async def test_cleared_field_stays_cleared_and_takes_nothing_with_it() -> None:
    """Emptying a field the user may edit removes it, and only it.

    A cleared field is the one case the merge cannot express by absence, so
    it travels as a tombstone. The tombstone must reach exactly one key.
    """
    project_data = build_project()
    result = await run_flow_edit(project_data, "modal-edit-identity", "description", "")

    assert result.get("description") in (None, ""), "cleared description came back"

    expected = copy.deepcopy(project_data)
    expected.pop("description", None)
    stripped = {k: v for k, v in result.items() if k != "description"}
    assert dump_yaml_to_string(stripped) == dump_yaml_to_string(expected)


@pytest.mark.asyncio
async def test_unrelated_flow_leaves_every_other_top_level_key_untouched() -> None:
    """A single-field identity edit may not rewrite services, config or deployments.

    Spelled out separately from the parametrised comparison because these are
    the keys that actually went missing in the field: the OPI-managed
    ``config`` block and the ``services`` list.
    """
    project_data = build_project()
    result = await run_flow_edit(project_data, "modal-edit-identity", "display-name", "Anders")

    for key in ("services", "config", "registries", "repositories", "components", "deployments", "users"):
        assert result[key] == project_data[key], f"identity edit changed {key}"
