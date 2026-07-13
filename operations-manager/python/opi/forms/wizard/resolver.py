"""Conditional step resolver for wizard flows.

Evaluates each FormSection's visibility condition against the current
wizard data and produces the ordered list of active sections.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opi.forms.wizard.services_merge import merge_service_lists

if TYPE_CHECKING:
    from collections.abc import Callable

    from opi.forms.visualizers.flows import FormFlow
    from opi.forms.visualizers.sections import FormSection


def resolve_active_sections(
    flow: FormFlow,
    step_data: dict[str, dict[str, Any]],
) -> list[FormSection]:
    """Evaluate visibility conditions and return the active sections.

    Args:
        flow: The FormFlow whose sections to evaluate.
        step_data: Merged form data from all wizard steps so far,
                   keyed by section_id.

    Returns:
        Ordered list of FormSections that should be visible.
    """
    merged = _merge_step_data(step_data)
    return [section for section in flow.sections if _is_visible(section, merged)]


def resolve_active_section_ids(
    flow: FormFlow,
    step_data: dict[str, dict[str, Any]],
) -> list[str]:
    """Convenience: return just the section_ids of active sections."""
    return [s.section_id for s in resolve_active_sections(flow, step_data)]


def get_section_metadata(
    sections: list[FormSection],
) -> dict[str, tuple[str, str | None]]:
    """Build section_id -> (title, icon) mapping for WizardState.get_steps()."""
    return {s.section_id: (s.title, s.icon) for s in sections}


def _is_visible(section: FormSection, data: dict[str, Any]) -> bool:
    """Check whether a section is visible given the current data."""
    visible = section.visible
    if isinstance(visible, bool):
        return visible
    # visible is Callable[[dict[str, Any]], bool]
    fn: Callable[[dict[str, Any]], bool] = visible
    return fn(data)


def _merge_step_data(step_data: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Merge per-step data dicts into a single flat dict.

    For list values shared across sections (e.g. ``deployments``) items are merged
    by index so fields from different sections combine. The ``services`` list is a
    selection set keyed by service name, so it is merged by name (see services_merge),
    not by index: a section still carrying the pre-edit list must not index-swap or
    duplicate services.
    """
    import copy

    def _merge_into(target: dict[str, Any], source: dict[str, Any]) -> None:
        for key, value in source.items():
            if key == "services" and isinstance(target.get(key), list) and isinstance(value, list):
                target[key] = merge_service_lists(target[key], value)
            elif key in target and isinstance(target[key], list) and isinstance(value, list):
                tgt_list = target[key]
                for i, src_item in enumerate(value):
                    if i < len(tgt_list):
                        if isinstance(tgt_list[i], dict) and isinstance(src_item, dict):
                            tgt_list[i].update(copy.deepcopy(src_item))
                        else:
                            tgt_list[i] = copy.deepcopy(src_item)
                    else:
                        tgt_list.append(copy.deepcopy(src_item))
            else:
                target[key] = copy.deepcopy(value)

    merged: dict[str, Any] = {}
    for section_data in step_data.values():
        _merge_into(merged, section_data)
    return merged
