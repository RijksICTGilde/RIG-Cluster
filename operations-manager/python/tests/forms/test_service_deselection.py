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


# ---------------------------------------------------------------------------
# Virtualize propagation for top-level sequences
#
# When build_component_edit_section flattens nested sequences to the top level
# (e.g. PERSISTENT_STORAGE_SEQUENCE has virtualize=("services","_services-config")
# but its children do not), the renderer and processor must propagate the
# parent's virtualize to the children. Otherwise child form names use the real
# ``services{persistent-storage}/...`` path, which collides with the sibling
# service-selection list and the checkbox cannot be toggled.
# ---------------------------------------------------------------------------


_VIRT = ("services", "_services-config")

PS_NAME_VIRT = Editable(
    yaml_path="components[*]/services{persistent-storage}/config[*]/name",
    required=True,
)

PS_SEQUENCE_VIRT = Editable(
    yaml_path="components[*]/services{persistent-storage}/config",
    depends_on="components[*]/services",
    show_when={"contains": "persistent-storage"},
    virtualize=_VIRT,
    children=[PS_NAME_VIRT],
)

PS_VIS_VIRT = EditableVisualizer(
    editable=PS_SEQUENCE_VIRT,
    widget=WidgetType.SEQUENCE,
    label="Persistent storage",
    children=[
        EditableVisualizer(editable=PS_NAME_VIRT, widget=WidgetType.TEXT, label="Naam"),
    ],
)


def _materialize_virt_editables(index: int) -> list[EditableVisualizer]:
    return [materialize_wildcard_visualizer(vis, index) for vis in [NAME_VIS, SERVICES_VIS, PS_VIS_VIRT]]


def test_topleveL_sequence_renders_children_with_virtual_path() -> None:
    """In modal-edit-component, PERSISTENT_STORAGE_SEQUENCE becomes top-level.

    Children must still render their form names under ``_services-config`` so
    the selection list at ``services`` is not clobbered by config form data.
    """
    from opi.forms.renderer import FormRenderer
    from opi.forms.widgets.roos import ROOSWidgetAdapter

    editables = _materialize_virt_editables(0)
    yaml_data: dict[str, Any] = {
        "components": [
            {
                "name": "frontend",
                "services": [
                    "publish-on-web",
                    {"persistent-storage": {"config": [{"name": "data"}]}},
                ],
            },
        ],
    }
    renderer = FormRenderer(widget_adapter=ROOSWidgetAdapter())
    fields = renderer._build_fields_from_editables(  # type: ignore[attr-defined]
        editables=editables, yaml_data=yaml_data, errors=None, edit_mode=True
    )
    seq = next(f for f in fields.values() if f.widget_type == "sequence")
    assert "_services-config" in seq.path, f"Sequence form path should be virtualized: {seq.path}"

    leaf_paths = []

    def _collect(field: Any) -> None:
        if getattr(field, "children", None):
            for c in field.children:
                _collect(c)
        else:
            leaf_paths.append(field.path)

    _collect(seq)
    assert all("_services-config" in p for p in leaf_paths), (
        f"All child form paths should be virtualized, got: {leaf_paths}"
    )
    assert not any("services{persistent-storage}" in p for p in leaf_paths), (
        f"No child form path should still carry the real services{{...}} segment, got: {leaf_paths}"
    )


@pytest.mark.asyncio
async def test_topleveL_sequence_reads_children_from_virtual_path() -> None:
    """Submission under the virtual ``_services-config`` path must be read."""
    editables = _materialize_virt_editables(0)
    processor = EditableFormProcessor()

    # The frontend posts under the VIRTUAL path because the sequence carries
    # ``virtualize=("services","_services-config")``. json-enc serialises the
    # filter syntax ``_services-config{persistent-storage}/...`` into a list
    # entry, exactly mirroring how ``services{filter}/...`` is encoded. The
    # processor must read from that virtual path, not the real
    # ``services{persistent-storage}/config`` one.
    submitted = {
        "components": [
            {
                "name": "frontend",
                "services": ["publish-on-web", "persistent-storage"],
                "_services-config": [
                    {"persistent-storage": {"config": [{"name": "data"}]}},
                ],
            },
        ],
    }

    result, errors = await processor.process_json_submission(
        submitted,
        editables,
        {"components": [{"name": "frontend", "services": []}]},
        edit_mode=True,
    )

    assert not errors, f"Unexpected validation errors: {errors}"
    comp = result["components"][0]
    # The virtual key must not leak into the result
    assert "_services-config" not in comp
    # The real ``services`` list now contains the dict with config
    ps_entry = next((s for s in comp["services"] if isinstance(s, dict) and "persistent-storage" in s), None)
    assert ps_entry is not None, f"persistent-storage dict missing from services: {comp['services']}"
    assert ps_entry["persistent-storage"]["config"] == [{"name": "data"}]
