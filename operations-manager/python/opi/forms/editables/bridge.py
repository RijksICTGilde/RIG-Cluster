from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opi.forms.editables.path import get_value, resolve_path
from opi.forms.field import FormField
from opi.forms.providers import get_provider

if TYPE_CHECKING:
    from opi.forms.editables.editable import ProjectEditable


def editable_to_form_field(
    editable: ProjectEditable,
    yaml_data: dict[str, Any],
    errors: dict[str, list[str]] | None = None,
    index: int | None = None,
    edit_mode: bool = False,
) -> FormField:
    """
    Convert a ProjectEditable + YAML data into a FormField for rendering.

    This bridges the editable system into the existing FormField -> WidgetAdapter pipeline.

    Args:
        editable: The field definition.
        yaml_data: Full project YAML dict.
        errors: Validation errors keyed by resolved path.
        index: Sequence index for [*] paths.
        edit_mode: Whether we're editing an existing project.

    Returns:
        A FormField ready for rendering.
    """
    # 1. Resolve the path
    concrete_path = resolve_path(editable.yaml_path, index)

    # 2. Extract value from YAML
    raw_value = get_value(yaml_data, concrete_path)

    # 3. Apply converter for display
    display_value = raw_value
    if editable.converter:
        display_value = editable.converter.view(raw_value)

    # 4. Resolve options
    options = resolve_options_for_editable(editable)

    # 5. Build HTMX attrs dict
    htmx_attrs: dict[str, str] = {}
    if editable.htmx_trigger:
        htmx_attrs["hx-trigger"] = editable.htmx_trigger
    if editable.htmx_target:
        htmx_attrs["hx-target"] = editable.htmx_target
    if editable.htmx_swap:
        htmx_attrs["hx-swap"] = editable.htmx_swap

    # 6. Determine readonly
    readonly = editable.readonly or (editable.readonly_on_edit and edit_mode)

    # 7. Build FormField
    return FormField(
        name=concrete_path,
        path=concrete_path,
        schema_type=str,
        widget_type=editable.widget,
        label=editable.label,
        required=editable.required,
        description=editable.description,
        placeholder=editable.placeholder,
        value=display_value,
        options=options or None,
        errors=(errors or {}).get(concrete_path, []),
        readonly=readonly,
        readonly_on_edit=editable.readonly_on_edit,
        min_items=editable.min_items,
        max_items=editable.max_items,
        htmx_attrs=htmx_attrs,
    )


def should_render_editable(
    editable: ProjectEditable,
    yaml_data: dict[str, Any],
    index: int | None = None,
) -> bool:
    """
    Check if an editable should be rendered based on its dependencies.

    Implements 3 dependency patterns:

    1. No depends_on -> always render (True)
    2. depends_on set, no show_when -> render if dependency value is truthy
    3. depends_on + show_when -> evaluate conditions:
       - {"contains": "value"} -> dep_value is list and "value" in dep_value
       - {"field": ["val1", "val2"]} -> dep_value in ["val1", "val2"]
       - {"field": "value"} -> dep_value == "value"
    """
    if not editable.depends_on:
        return True

    # Get the dependency value
    dep_value = get_value(yaml_data, editable.depends_on)

    if editable.show_when is None:
        return bool(dep_value)

    # Evaluate show_when conditions
    for key, expected in editable.show_when.items():
        if key == "contains":
            if not isinstance(dep_value, list):
                return False
            names = _extract_names_from_list(dep_value)
            if expected not in names:
                return False
        else:
            if isinstance(expected, list):
                if dep_value not in expected:
                    return False
            else:
                if dep_value != expected:
                    return False

    return True


def resolve_options_for_editable(
    editable: ProjectEditable,
    context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Resolve dynamic options using the PROVIDER_REGISTRY.

    Args:
        editable: The editable whose options to resolve.
        context: Optional kwargs to pass to the provider constructor.

    Returns:
        List of option dicts, or empty list if no provider.
    """
    if not editable.options_provider:
        return []

    kwargs = context or {}
    try:
        provider = get_provider(editable.options_provider, **kwargs)
        return provider.get_options()
    except KeyError:
        return []


def _extract_names_from_list(items: list) -> list[str]:  # type: ignore[type-arg]
    """Extract names from a mixed str/dict list (services format)."""
    names: list[str] = []
    for item in items:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            names.extend(item.keys())
    return names
