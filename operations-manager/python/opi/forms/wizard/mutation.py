"""The rule for "absent": a form submission is a mutation, not a replacement.

A form posts what it RENDERS. A collapsed section, a step the user never opened,
a service the picker does not show, a field the browser refused to submit -- all
arrive as "not there". Reading that as "the user removed it" is how services
disappeared from project files on 6 August 2026, four times over.

So the rule, in one place:

    A key that is absent from a submission is UNCHANGED.
    A removal is EXPLICIT.

For a scalar field the explicit form already existed: ``CLEARED_FIELD``, the
tombstone ``_extract_section_data`` stores for a field the form owns and the user
emptied. This module carries the same rule to SELECTION lists, where it was
missing and where it cost the most.

For a selection the explicit form is "the form offered it and the user did not
tick it". That is why ``apply_selection_mutation`` needs the OFFERED set: it is
what separates "unticked" from "never shown". A service the picker never showed
(``hidden=True``, e.g. ``namespace-postgresql-database``) cannot have been
unticked, so it survives -- previously it was silently dropped from the project
by any services-step save.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from opi.forms.editables.service_path import smart_get_value, smart_set_value
from opi.forms.visualizers.bridge import resolve_options_for_editable
from opi.services.services import ServiceAdapter, service_entry_name

#: The project-wide service selection.
SERVICES_PATH = "services"

#: A component's own service selection: the checklist editable's yaml_path, either
#: materialized (``components[0]/services``) or the wildcard template form.
_COMPONENT_SERVICES_PATH = re.compile(r"^components\[(\d+|\*)\]/services$")


def _walk(editables: list[Any]) -> list[Any]:
    """Every visualizer in *editables*, including nested children."""
    found: list[Any] = []
    for vis in editables:
        found.append(vis)
        children = getattr(vis, "children", None)
        if children:
            found.extend(_walk(children))
    return found


def offered_selection_values(editables: list[Any], yaml_path: str) -> set[str] | None:
    """The values the section OFFERS for the selection at *yaml_path*.

    Returns None when the section carries no such field at all -- then the
    submission says nothing about that selection and nothing may be removed
    from it.

    Resolved from the editable's ``values_provider``, the same source the
    renderer draws the options from, so "offered" means what the user saw.
    """
    for vis in _walk(editables):
        editable = getattr(vis, "editable", None)
        if editable is None or editable.yaml_path != yaml_path:
            continue
        if not editable.values_provider:
            # A selection without a provider offers whatever it renders, which we
            # cannot know here. Treat it as "offers nothing" rather than guess.
            return set()
        options = resolve_options_for_editable(vis, None)
        return {str(option["value"]) for option in options if option.get("value")}
    return None


def apply_selection_mutation(
    base: list[Any],
    submitted: list[Any],
    offered: set[str],
) -> list[Any]:
    """Apply a submitted selection on top of *base*, honoring the "absent" rule.

    - a name in the submission is selected;
    - a name in *base* that the form offered and the submission omits was
      unticked: it is removed;
    - a name in *base* that the form did NOT offer is untouched: it survives.

    Entries keep their base form (string or config-carrying dict), so a surviving
    service keeps its configuration.
    """
    submitted_names = {name for entry in submitted if (name := service_entry_name(entry)) is not None}
    result = list(submitted)
    for entry in base:
        name = service_entry_name(entry)
        if name is None or name in submitted_names or name in offered:
            continue
        result.append(copy.deepcopy(entry))
    return result


def apply_services_mutation(
    editables: list[Any],
    base_data: dict[str, Any],
    submitted_yaml: dict[str, Any],
) -> None:
    """Reconcile a submitted project service selection with its base, in place.

    The single home of two rules that used to be duplicated -- and applied under
    different conditions -- in the wizard router and the modal-edit router:

    1. absent-is-unchanged for services the form never offered;
    2. a service required by a selected service is added back.

    Rule 2 is what keeps a LOCKED service alive: the card is locked precisely
    because something else requires it, so the server can put it back without
    the browser having to send it. That is the whole reason the lock no longer
    needs a hidden input travelling alongside the checkbox.

    Does nothing when this section does not SHOW the selection. A processed
    submission is a copy of the merged project data with the submitted fields
    written into it, so ``services`` is present in every flow's result -- also in
    a flow about environment variables. Reconciling there would let an unrelated
    edit add a service to the project, which is the mirror image of the bug this
    module exists for: it is still a step speaking about something it never showed.
    """
    offered = offered_selection_values(editables, SERVICES_PATH)
    if offered is None:
        return

    submitted = submitted_yaml.get(SERVICES_PATH)
    if not isinstance(submitted, list):
        return

    base = base_data.get(SERVICES_PATH)
    if isinstance(base, list):
        submitted = apply_selection_mutation(base, submitted, offered)

    submitted_yaml[SERVICES_PATH] = ServiceAdapter.resolve_service_dependencies(submitted)


def apply_component_services_mutation(
    editables: list[Any],
    base_data: dict[str, Any],
    submitted_yaml: dict[str, Any],
) -> None:
    """Reconcile a component's submitted service selection with its base, in place.

    The component form's checklist (``components[i]/services``) is the same kind of
    selection as the project-wide one, and follows the same rule: a name the picker
    offered and the submission omits was unticked and goes; a name the picker never
    showed (a service the project disabled meanwhile, a hidden entry) survives from
    the base. Does nothing for sections that do not carry the checklist -- a config
    section speaking only through ``services{X}/...`` paths says nothing about the
    selection.

    The offered set comes from the same provider the renderer used
    (``FilteredServiceOptionsProvider``), fed with the base's project services, so
    "offered" means what the user actually saw.
    """
    for vis in _walk(editables):
        editable = getattr(vis, "editable", None)
        if editable is None:
            continue
        match = _COMPONENT_SERVICES_PATH.match(editable.yaml_path)
        if not match:
            continue

        project_service_names = ServiceAdapter.extract_service_names_from_project_services(
            base_data.get(SERVICES_PATH) or []
        )
        options = resolve_options_for_editable(vis, {"project_services": project_service_names})
        offered = {str(option["value"]) for option in options if option.get("value")}

        components = submitted_yaml.get("components")
        if not isinstance(components, list):
            return
        indexes = range(len(components)) if match.group(1) == "*" else [int(match.group(1))]
        for idx in indexes:
            path = f"components[{idx}]/services"
            submitted = smart_get_value(submitted_yaml, path)
            if not isinstance(submitted, list):
                continue
            base = smart_get_value(base_data, path)
            if isinstance(base, list):
                submitted = apply_selection_mutation(base, submitted, offered)
            smart_set_value(submitted_yaml, path, submitted)
        return
