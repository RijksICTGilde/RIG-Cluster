"""An optional list field must write nothing when it is left empty.

The start command was the case that broke a project: left empty it wrote
``command: []``, and the schema puts ``minItems: 1`` on it, so every project
with a component became unsaveable the moment the field was untouched -- which
is the normal choice. That is fixed, but the pattern is general: any editable
that writes a list can do the same, and there is no test that would notice.

So this sweeps every list-writing editable in every flow instead of pinning one
field. Where the schema forbids an empty list, the field must remove its key
when it is empty. Where the schema allows one, the field is left alone -- an
empty list is a valid value there and rewriting that behaviour is not this
test's business.

Paths the schema walk cannot reach are listed explicitly rather than silently
skipped: a new one shows up as a failure here, so it gets a decision instead of
disappearing.
"""

from __future__ import annotations

import json
from typing import Any

from opi.core.project_schema import SCHEMA_PATH
from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.flows import FLOW_REGISTRY
from opi.forms.visualizers.visualizer import EditableVisualizer

#: The widgets that write a list (or a mapping block) rather than a scalar.
LIST_WIDGETS = {
    WidgetType.SEQUENCE,
    WidgetType.MULTI_SELECT,
    WidgetType.CHECKBOX_GROUP,
    WidgetType.KEY_VALUE,
}

#: Paths that live under a service entry (``services`` is a list of strings and
#: single-key dicts in the project file, so a property walk cannot follow
#: ``services/<name>/config/...``) or under a form-only root. Their emptiness is
#: governed by the service's own config model, not by the project schema.
#: Listed here so the sweep below reports anything NEW it cannot place.
UNREACHABLE_BY_SCHEMA_WALK = {
    "components[*]/services{attachments}/config",
    "components[*]/services{persistent-storage}/config",
    "components[*]/services{temp-storage}/config",
    "deployments[*]/components[*]/services/attachments/config",
    "resource_types",
    "services/cross-domain-access/config/inbound",
    "services/cross-domain-access/config/outbound",
    "services/invite/config/active",
    "services/invite/config/active[*]/auth-methods",
    "services/invite/config/active[*]/realm-roles",
    "services/keycloak/config/additional-clients",
    "services/keycloak/config/additional-clients[*]/redirect-uris",
    "services/keycloak/config/additional_redirect_uris",
    "services/postgresql-database/config/schemas",
}


def _load_schema() -> tuple[dict[str, Any], dict[str, Any]]:
    with SCHEMA_PATH.open(encoding="utf-8") as handle:
        schema = json.load(handle)
    return schema, schema.get("$defs", {})


def _resolve(node: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in node:
        node = defs[node["$ref"].split("/")[-1]]
    return node


def _schema_node_for(path: str) -> dict[str, Any] | None:
    """The schema node an editable yaml_path points at, or None if unreachable.

    Walks ``properties`` per segment and steps into ``items`` for an indexed
    segment (``components[*]``), which is exactly the shape of the paths that
    correspond to real project fields.
    """
    schema, defs = _load_schema()
    node = _resolve(schema, defs)
    for segment in path.split("/"):
        key = segment.split("[")[0].split("{")[0]
        properties = _resolve(node, defs).get("properties", {})
        if key not in properties:
            return None
        node = _resolve(properties[key], defs)
        if ("[" in segment or "{" in segment) and node.get("type") == "array":
            node = _resolve(node.get("items", {}), defs)
    return node


def _all_list_editables() -> dict[str, EditableVisualizer]:
    """Every list-writing editable across every flow, keyed by yaml_path."""
    found: dict[str, EditableVisualizer] = {}

    def walk(visualizers: list[EditableVisualizer]) -> None:
        for visualizer in visualizers:
            if visualizer.widget in LIST_WIDGETS:
                found.setdefault(visualizer.editable.yaml_path, visualizer)
            if visualizer.children:
                walk(visualizer.children)

    for flow in FLOW_REGISTRY.values():
        for section in flow.sections:
            walk(section.editables)
    return found


def _writes_nothing_when_empty(visualizer: EditableVisualizer) -> bool:
    """Whether leaving this field empty leaves the key out of the project file."""
    editable = visualizer.editable
    if editable.remove_when_none:
        return True
    # A converter that turns an empty input into None hands the processor a None,
    # which it drops -- the route the start command takes.
    converter = editable.converter
    return converter is not None and converter.write("") is None


def test_the_sweep_actually_finds_fields() -> None:
    """A sweep over nothing would pass forever."""
    assert len(_all_list_editables()) > 10


def test_no_new_path_escapes_the_schema_walk() -> None:
    """Every list field is either checkable against the schema or explicitly listed."""
    unreachable = {path for path in _all_list_editables() if _schema_node_for(path) is None}
    new = unreachable - UNREACHABLE_BY_SCHEMA_WALK
    assert not new, (
        f"These list fields cannot be checked against the project schema and are not listed: "
        f"{sorted(new)}. Decide what empty means for them and add them to "
        f"UNREACHABLE_BY_SCHEMA_WALK with the reason."
    )
    gone = UNREACHABLE_BY_SCHEMA_WALK - unreachable
    assert not gone, f"These are checkable now (or no longer exist); drop them from the list: {sorted(gone)}"


def test_empty_writes_nothing_where_the_schema_forbids_an_empty_list() -> None:
    """The class of bug the start command was one instance of."""
    offenders: list[str] = []
    for path, visualizer in sorted(_all_list_editables().items()):
        node = _schema_node_for(path)
        if node is None or node.get("type") != "array" or node.get("minItems", 0) < 1:
            continue
        if not _writes_nothing_when_empty(visualizer):
            offenders.append(path)

    assert not offenders, (
        f"These fields write an empty list where the schema demands at least one item, so leaving "
        f"them empty makes the project file unsaveable: {offenders}. Set remove_when_none=True "
        f"(or give the field a converter that returns None when empty)."
    )


def test_the_start_command_is_covered_by_this_sweep() -> None:
    """The field this sweep came from must actually be in it, and pass.

    It writes a list but is rendered as a text field, so it is reached through its
    converter rather than through a list widget -- easy to walk past.
    """
    node = _schema_node_for("components[*]/command")
    assert node is not None and node.get("minItems") == 1

    from opi.forms.visualizers.fields.components import COMPONENT_COMMAND

    assert _writes_nothing_when_empty(COMPONENT_COMMAND)
