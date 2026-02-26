"""Conditional step resolver for wizard flows.

Evaluates each FormSection's visibility condition against the current
wizard data and produces the ordered list of active sections.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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

    The "services" section is merged last so its ``services`` key
    (the authoritative list of selected services) is not overwritten
    by config sections that share the same top-level key (e.g.
    keycloak-config stores its data under ``{"services": [...]}}``
    because its editables' yaml_paths start with ``services/``).
    """
    merged: dict[str, Any] = {}
    services_data: dict[str, Any] | None = None
    for section_id, section_data in step_data.items():
        if section_id == "services":
            services_data = section_data
            continue
        merged.update(section_data)
    if services_data is not None:
        merged.update(services_data)
    return merged
