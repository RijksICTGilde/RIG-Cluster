"""Test that deselecting component services in the modal edit wizard persists correctly.

Reproduces the bug where persistent-storage (and temp-storage) sequence editables
re-add themselves to the component's services list even after the user deselected
them via the checkbox_group.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from opi.forms.editables.editable import Editable, WidgetType
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.editables.reindex import materialize_wildcard_visualizer
from opi.forms.visualizers.visualizer import EditableVisualizer

# ---------------------------------------------------------------------------
# Minimal editable definitions mirroring the real component editables
# ---------------------------------------------------------------------------

SERVICES_EDITABLE = Editable(
    yaml_path="components[*]/services",
    default="__all__",
)

PERSISTENT_STORAGE_NAME = Editable(
    yaml_path="components[*]/services{persistent-storage}/config[*]/name",
    required=True,
)

PERSISTENT_STORAGE_SEQUENCE = Editable(
    yaml_path="components[*]/services{persistent-storage}/config",
    depends_on="components[*]/services",
    show_when={"contains": "persistent-storage"},
    children=[PERSISTENT_STORAGE_NAME],
)

COMPONENT_NAME_EDITABLE = Editable(yaml_path="components[*]/name")

# Visualizers
SERVICES_VIS = EditableVisualizer(
    editable=SERVICES_EDITABLE,
    widget=WidgetType.CHECKBOX_GROUP,
    label="Services",
)

PERSISTENT_STORAGE_VIS = EditableVisualizer(
    editable=PERSISTENT_STORAGE_SEQUENCE,
    widget=WidgetType.SEQUENCE,
    label="Persistent storage",
    children=[
        EditableVisualizer(
            editable=PERSISTENT_STORAGE_NAME,
            widget=WidgetType.TEXT,
            label="Naam",
        ),
    ],
)

NAME_VIS = EditableVisualizer(
    editable=COMPONENT_NAME_EDITABLE,
    widget=WidgetType.TEXT,
    label="Naam",
    readonly_on_edit=True,
)


def _build_materialized_editables(index: int) -> list[EditableVisualizer]:
    """Materialize editables for a specific component index, same as build_component_edit_section."""
    children = [NAME_VIS, SERVICES_VIS, PERSISTENT_STORAGE_VIS]
    return [materialize_wildcard_visualizer(vis, index) for vis in children]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def yaml_data() -> dict[str, Any]:
    """Project data where component 0 uses all 3 services including persistent-storage."""
    return {
        "services": ["publish-on-web", "keycloak", {"persistent-storage": {"config": []}}],
        "components": [
            {
                "name": "frontend",
                "services": [
                    "publish-on-web",
                    "keycloak",
                    {"persistent-storage": {"config": [{"name": "data", "size": "1Gi", "mount-path": "/data"}]}},
                ],
            },
        ],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deselect_persistent_storage_removes_it(yaml_data: dict[str, Any]) -> None:
    """When the user deselects persistent-storage, it must NOT reappear after processing."""
    editables = _build_materialized_editables(0)
    processor = EditableFormProcessor()

    # Simulate form submission where user selected only keycloak and publish-on-web
    submitted = {
        "components": [
            {
                "name": "frontend",
                "services": ["publish-on-web", "keycloak"],
                # No persistent-storage config fields submitted
            },
        ],
    }

    result, errors = await processor.process_json_submission(
        submitted,
        editables,
        copy.deepcopy(yaml_data),
        edit_mode=True,
    )

    assert not errors, f"Unexpected validation errors: {errors}"

    comp_services = result["components"][0]["services"]
    service_names = []
    for svc in comp_services:
        if isinstance(svc, str):
            service_names.append(svc)
        elif isinstance(svc, dict):
            service_names.extend(svc.keys())

    assert "persistent-storage" not in service_names, (
        f"persistent-storage should have been removed by deselection, but services are: {comp_services}"
    )
    assert set(service_names) == {"publish-on-web", "keycloak"}


@pytest.mark.asyncio
async def test_deselect_all_services_gives_empty_list(yaml_data: dict[str, Any]) -> None:
    """When user unchecks ALL services, the result should have an empty services list."""
    editables = _build_materialized_editables(0)
    processor = EditableFormProcessor()

    # No services key at all (browser omits unchecked checkboxes)
    submitted = {
        "components": [
            {
                "name": "frontend",
            },
        ],
    }

    result, errors = await processor.process_json_submission(
        submitted,
        editables,
        copy.deepcopy(yaml_data),
        edit_mode=True,
    )

    assert not errors
    comp_services = result["components"][0]["services"]
    assert comp_services == [], f"Expected empty services, got: {comp_services}"


@pytest.mark.asyncio
async def test_keep_persistent_storage_preserves_config(yaml_data: dict[str, Any]) -> None:
    """When persistent-storage stays selected, its config should be preserved."""
    editables = _build_materialized_editables(0)
    processor = EditableFormProcessor()

    submitted = {
        "components": [
            {
                "name": "frontend",
                "services": ["publish-on-web", "keycloak", "persistent-storage"],
                # persistent-storage config submitted via {filter} paths
            },
        ],
    }

    # Simulate the config values being submitted at the filtered path
    # (json-enc puts them in the services array as a dict entry)
    from opi.forms.editables.service_path import smart_set_value

    smart_set_value(
        submitted,
        "components[0]/services{persistent-storage}/config",
        [{"name": "data", "size": "1Gi", "mount-path": "/data"}],
    )

    result, errors = await processor.process_json_submission(
        submitted,
        editables,
        copy.deepcopy(yaml_data),
        edit_mode=True,
    )

    assert not errors
    comp_services = result["components"][0]["services"]
    # persistent-storage should still be present with config
    ps_found = False
    for svc in comp_services:
        if isinstance(svc, dict) and "persistent-storage" in svc:
            ps_found = True
            assert svc["persistent-storage"]["config"] == [{"name": "data", "size": "1Gi", "mount-path": "/data"}]
    assert ps_found, f"persistent-storage with config should be present, got: {comp_services}"
