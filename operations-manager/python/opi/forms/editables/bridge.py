from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opi.forms.editables.path import resolve_path
from opi.forms.editables.service_path import smart_get_value
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
    provider_context: dict[str, Any] | None = None,
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

    # 2. Extract value from YAML (fall back to default)
    raw_value = smart_get_value(yaml_data, concrete_path)
    if raw_value is None and editable.default is not None:
        raw_value = editable.default

    # 3. Apply converter for display
    display_value = raw_value
    if editable.converter:
        display_value = editable.converter.view(raw_value)

    # 4. Resolve options
    options = resolve_options_for_editable(editable, context=provider_context)

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

    # 7. Check locked_by_service: force value + readonly when the service is active
    description = editable.description
    if editable.locked_by_service and _is_service_active(editable.locked_by_service, yaml_data):
        display_value = True
        readonly = True
        description = f"Vereist door: {_service_display_name(editable.locked_by_service)}"

    # 8. Build FormField
    return FormField(
        name=concrete_path,
        path=concrete_path,
        schema_type=str,
        widget_type=editable.widget,
        label=editable.label,
        required=editable.required,
        description=description,
        placeholder=editable.placeholder,
        value=display_value,
        options=options or None,
        errors=(errors or {}).get(concrete_path, []),
        readonly=readonly,
        readonly_on_edit=editable.readonly_on_edit,
        min_items=editable.min_items,
        max_items=editable.max_items,
        htmx_attrs=htmx_attrs,
        attributes=editable.attributes or {},
        default=editable.default,
        help_text=editable.help_text,
        help_template=editable.help_template,
        examples=editable.examples,
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
    dep_value = smart_get_value(yaml_data, editable.depends_on)

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
        elif key == "contains_any":
            if not isinstance(dep_value, list) or not isinstance(expected, list):
                return False
            names = _extract_names_from_list(dep_value)
            if not any(e in names for e in expected):
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

    kwargs = _filter_provider_kwargs(editable.options_provider, context or {})
    try:
        provider = get_provider(editable.options_provider, **kwargs)
        return provider.get_options()
    except KeyError:
        return []


def _filter_provider_kwargs(
    provider_name: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Filter context kwargs to only those accepted by the provider's __init__.

    This prevents TypeError when the context contains keys meant for other
    providers (e.g. ``component_names`` passed to ``FilteredServiceOptionsProvider``
    which only accepts ``project_services``).
    """
    import inspect

    from opi.forms.providers import PROVIDER_REGISTRY

    provider_cls = PROVIDER_REGISTRY.get(provider_name)
    if not provider_cls or not context:
        return {}

    sig = inspect.signature(provider_cls.__init__)
    valid_params = set(sig.parameters.keys()) - {"self"}
    return {k: v for k, v in context.items() if k in valid_params}


def _extract_names_from_list(items: list) -> list[str]:  # type: ignore[type-arg]
    """Extract names from a mixed str/dict list (services format)."""
    names: list[str] = []
    for item in items:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            names.extend(item.keys())
    return names


def _is_service_active(service_name: str, yaml_data: dict[str, Any]) -> bool:
    """Check if a service is in the selected services list."""
    services = yaml_data.get("services", [])
    if not isinstance(services, list):
        return False
    return service_name in _extract_names_from_list(services)


# Display names for services used in locked_by_service hints
_SERVICE_DISPLAY_NAMES: dict[str, str] = {
    "authorization-wall": "Authorization Wall",
    "keycloak": "Keycloak",
    "publish-on-web": "Publiceren op het web",
}


def _service_display_name(service_name: str) -> str:
    """Get a human-readable display name for a service."""
    return _SERVICE_DISPLAY_NAMES.get(service_name, service_name)
