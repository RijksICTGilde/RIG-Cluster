"""What a flow is allowed to write, derived from the flow itself.

An ``Editable`` already says where its field lives in the project file
(``yaml_path``). The editables of a flow therefore describe, without anything
being added, exactly which paths that flow may touch. Everything else is by
definition not editable through it, and so does not need to be "preserved"
either - it is never written in the first place.

That default-deny rule is not new: it is what ``StripTransientsHook`` and the
service-config fold already do for their own corner. Naming the fields to
protect one by one does not hold - four separate leaks proved that. Deriving
the permitted set from the flow does.
"""

from __future__ import annotations

import copy
import re
from typing import TYPE_CHECKING, Any

from opi.forms.editables.service_path import (
    is_service_config_path,
    parse_service_path,
    smart_delete_value,
    smart_get_value,
    smart_set_value,
)
from opi.forms.wizard.state import CLEARED_FIELD

if TYPE_CHECKING:
    from opi.forms.visualizers.sections import FormSection

_WILDCARD_SEGMENT = re.compile(r"\[\*\]")


def _writable_path(yaml_path: str) -> str:
    """The path actually written for an editable, truncated at its first wildcard.

    ``components[*]/name`` writes the whole ``components`` list: there is no
    single position to write, and the list value in the merged data already
    carries every item. ``deployments[0]/components[*]/reference`` likewise
    writes ``deployments[0]/components``.
    """
    segments = yaml_path.split("/")
    for i, segment in enumerate(segments):
        if "[*]" in segment:
            head = [*segments[:i], _WILDCARD_SEGMENT.sub("", segment)]
            return "/".join(part for part in head if part)
    return yaml_path


def _collect(vis_list: list[Any], found: list[str]) -> None:
    for vis in vis_list:
        path = _writable_path(vis.editable.yaml_path)
        if path:
            found.append(path)
        if vis.children:
            _collect(vis.children, found)


def flow_write_paths(sections: list[FormSection]) -> list[str]:
    """The paths the given sections may write, shallowest first.

    A path nested inside another declared path is dropped: writing the outer
    one already carries it, and writing twice would only add a chance to
    disagree with itself.
    """
    collected: list[str] = []
    for section in sections:
        _collect(list(section.editables), collected)

    ordered = sorted(dict.fromkeys(collected), key=lambda p: (p.count("/"), p))
    kept: list[str] = []
    for path in ordered:
        if any(path == parent or path.startswith(parent + "/") for parent in kept):
            continue
        kept.append(path)
    return kept


def _has_submitted_ancestor(merged_data: dict[str, Any], yaml_path: str) -> bool:
    """Whether some container above *yaml_path* was submitted as a dict.

    That is what tells "the user emptied this field" apart from "this flow
    never carried this field": the container is there, the key inside it is
    not. Without the distinction, a cleared value comes back on the next save
    - the oldest of the four leaks this module replaces.
    """
    segments = yaml_path.split("/")
    return any(isinstance(smart_get_value(merged_data, "/".join(segments[:i])), dict) for i in range(1, len(segments)))


def _deletable_target(yaml_path: str) -> bool:
    """Whether removing *yaml_path* removes a field rather than a whole service.

    ``services/keycloak`` names the service entry itself; dropping it would
    deselect the service, which is a different decision than emptying one of
    its config fields.
    """
    return not (is_service_config_path(yaml_path) and parse_service_path(yaml_path)[1] is None)


def _may_delete(yaml_path: str) -> bool:
    """Whether absence of *yaml_path* in the submission may remove it.

    Top-level keys are left alone: a flow that does not carry ``services``
    this time must not drop the project's services.
    """
    return "/" in yaml_path and _deletable_target(yaml_path)


def apply_write_paths(
    existing_data: dict[str, Any],
    merged_data: dict[str, Any],
    paths: list[str],
) -> dict[str, Any]:
    """Write only *paths* from *merged_data* onto *existing_data*, in place.

    A path the submission carries is written. A path it does not carry, while
    a container above it was submitted, is removed - the user emptied it.
    Anything not in *paths* is not touched at all.
    """
    for path in paths:
        value = smart_get_value(merged_data, path)
        if value == CLEARED_FIELD:
            # An explicit tombstone: the user emptied a field they may edit.
            if _deletable_target(path):
                smart_delete_value(existing_data, path)
        elif value is not None:
            smart_set_value(existing_data, path, copy.deepcopy(value))
        elif _may_delete(path) and _has_submitted_ancestor(merged_data, path):
            smart_delete_value(existing_data, path)
    return existing_data
