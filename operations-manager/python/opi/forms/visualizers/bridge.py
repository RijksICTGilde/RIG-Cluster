"""Bridge: converts EditableVisualizer into FormField for rendering."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opi.forms.editables.path import resolve_path
from opi.forms.editables.service_path import smart_get_value
from opi.forms.field import FormField
from opi.forms.visualizers.providers import get_provider

if TYPE_CHECKING:
    from opi.forms.visualizers.visualizer import EditableVisualizer


def editable_to_form_field(
    editable: EditableVisualizer,
    yaml_data: dict[str, Any],
    errors: dict[str, list[str]] | None = None,
    index: int | None = None,
    edit_mode: bool = False,
    provider_context: dict[str, Any] | None = None,
) -> FormField:
    """Convert an EditableVisualizer + YAML data into a FormField.

    Bridges the visualizer layer into the existing FormField -> WidgetAdapter pipeline.
    """
    ed = editable.editable
    yaml_path = ed.yaml_path
    converter = ed.converter
    default = ed.default
    required = ed.required
    min_items = ed.min_items
    max_items = ed.max_items
    options_provider_name = ed.values_provider

    widget = str(editable.widget)
    label = editable.label
    description = editable.description
    placeholder = editable.placeholder
    readonly_flag = editable.readonly
    readonly_on_edit_flag = editable.readonly_on_edit
    locked_by_service = editable.locked_by_service
    htmx_trigger = editable.htmx_trigger
    htmx_target = editable.htmx_target
    htmx_swap = editable.htmx_swap
    attributes = editable.attributes
    help_text = editable.help_text
    help_template = editable.help_template
    examples = editable.examples

    # --- Shared logic ---

    # 1. Resolve the path
    concrete_path = resolve_path(yaml_path, index)

    # 2. Extract value from YAML (fall back to default)
    raw_value = smart_get_value(yaml_data, concrete_path)
    if raw_value is None and default is not None:
        raw_value = default

    # 3. Apply converter for display
    display_value = raw_value
    if converter:
        display_value = converter.view(raw_value)

    # 4. Resolve options
    options = _resolve_options(options_provider_name, provider_context)

    # 5. Build HTMX attrs dict
    htmx_attrs: dict[str, str] = {}
    if htmx_trigger:
        htmx_attrs["hx-trigger"] = htmx_trigger
    if htmx_target:
        htmx_attrs["hx-target"] = htmx_target
    if htmx_swap:
        htmx_attrs["hx-swap"] = htmx_swap

    # 6. Determine readonly
    readonly = readonly_flag or (readonly_on_edit_flag and edit_mode)

    # 7. Check locked_by_service: force value + readonly when the service is active
    if locked_by_service and _is_service_active(locked_by_service, yaml_data):
        display_value = True
        readonly = True
        description = f"Vereist door: {_service_display_name(locked_by_service)}"

    # 8. Build FormField
    return FormField(
        name=concrete_path,
        path=concrete_path,
        schema_type=str,
        widget_type=widget,
        label=label,
        required=required,
        description=description,
        placeholder=placeholder,
        value=display_value,
        options=options or None,
        errors=(errors or {}).get(concrete_path, []),
        readonly=readonly,
        readonly_on_edit=readonly_on_edit_flag,
        min_items=min_items,
        max_items=max_items,
        htmx_attrs=htmx_attrs,
        attributes=attributes or {},
        default=default,
        help_text=help_text,
        help_template=help_template,
        examples=examples,
    )


def should_render_editable(
    editable: EditableVisualizer,
    yaml_data: dict[str, Any],
    index: int | None = None,
) -> bool:
    """Check if an editable should be rendered based on its dependencies.

    Implements 3 dependency patterns:

    1. No depends_on -> always render (True)
    2. depends_on set, no show_when -> render if dependency value is truthy
    3. depends_on + show_when -> evaluate conditions:
       - {"contains": "value"} -> dep_value is list and "value" in dep_value
       - {"contains_any": [...]} -> dep_value is list and any match
       - {"field": ["val1", "val2"]} -> dep_value in ["val1", "val2"]
       - {"field": "value"} -> dep_value == "value"
    """
    ed = editable.editable
    depends_on = ed.depends_on
    show_when = ed.show_when

    if not depends_on:
        return True

    dep_value = smart_get_value(yaml_data, depends_on)

    if show_when is None:
        return bool(dep_value)

    for key, expected in show_when.items():
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
            elif dep_value != expected:
                return False

    return True


def resolve_options_for_editable(
    editable: EditableVisualizer,
    context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Resolve dynamic options using the PROVIDER_REGISTRY."""
    provider_name = editable.editable.values_provider
    return _resolve_options(provider_name, context)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_options(
    provider_name: str | None,
    context: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Resolve options by provider name."""
    if not provider_name:
        return []

    kwargs = _filter_provider_kwargs(provider_name, context or {})
    try:
        provider = get_provider(provider_name, **kwargs)
        return provider.get_options()
    except KeyError:
        return []


def _filter_provider_kwargs(
    provider_name: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Filter context kwargs to only those accepted by the provider's __init__."""
    import inspect

    from opi.forms.visualizers.providers import PROVIDER_REGISTRY

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
