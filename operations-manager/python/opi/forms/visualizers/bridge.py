"""Bridge: converts EditableVisualizer into FormField for rendering."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from opi.forms.editables.editable import EditableCondition, apply_virtualize
from opi.forms.editables.path import resolve_path
from opi.forms.editables.resolvers import build_resolver_map
from opi.forms.editables.service_path import smart_get_value
from opi.forms.field import FormField
from opi.forms.visualizers.providers import get_provider

if TYPE_CHECKING:
    from opi.forms.visualizers.visualizer import EditableVisualizer

logger = logging.getLogger(__name__)


def editable_to_form_field(
    editable: EditableVisualizer,
    yaml_data: dict[str, Any],
    errors: dict[str, list[str]] | None = None,
    index: int | None = None,
    edit_mode: bool = False,
    provider_context: dict[str, Any] | None = None,
    parent_virtualize: tuple[str, str] | None = None,
    warnings: dict[str, list[str]] | None = None,
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

    # The real path is for the data, the virtual one for the form field names.
    real_path = resolve_path(yaml_path, index)
    virt = ed.virtualize or parent_virtualize
    form_path = apply_virtualize(real_path, virt) if virt else real_path

    # The value comes from the real path first and then the virtual one, the same fallback
    # as ``_read_submitted`` in the processor. Wizard state keeps service CONFIG under the
    # virtual key while the real ``services`` key holds only the chosen names, so without it
    # a stored value reads as None, the default below takes over, and the user sees the
    # default where their own choice should be.
    raw_value = smart_get_value(yaml_data, real_path)
    if raw_value is None and virt and form_path != real_path:
        raw_value = smart_get_value(yaml_data, form_path)

    # A callable default is computed from the surrounding project data, so a field can be
    # prefilled with something derived instead of a constant. It runs only when the field has
    # no stored value, and it may return None to mean "no default after all". An error from it
    # is deliberately not caught: swallowing it renders an empty field instead.
    if raw_value is None and default is not None:
        raw_value = default(yaml_data) if callable(default) else default

    # An editable widget takes ``read()``, which converts the stored value into a
    # form-compatible one (a dict into a string for a select); a read-only one takes
    # ``view()``.
    display_value = raw_value
    if converter:
        if widget in ("select", "text", "password", "textarea", "radio"):
            display_value = converter.read(raw_value, context_data=yaml_data)
        else:
            display_value = converter.view(raw_value, context_data=yaml_data)

    # A checkbox is on or off, so its value has to be a REAL boolean, read from the stored
    # value and not from the display of it. A checkbox is none of the widgets above, so it
    # takes the ``view()`` branch, where a BooleanConverter yields "Ja" or "Nee"; the template
    # tests ``:checked="field.value"``, and a non-empty string ticks the box, "Nee" included.
    # The set below is the one ``BooleanConverter.write()`` uses, so showing and storing agree.
    if widget == "checkbox":
        display_value = raw_value in (True, "true", "on", "yes", "1")

    # The KV format is detected from the stored value, so the toggle matches it.
    if converter and hasattr(converter, "detect_format") and raw_value is not None:
        detected_fmt = converter.detect_format(raw_value)
        attributes = dict(attributes or {})
        attributes["kv_format"] = detected_fmt

    # The current value travels along, so a provider can include a tuner-set value.
    option_context = dict(provider_context or {})
    if raw_value is not None:
        option_context.setdefault("current_value", str(raw_value))
    # yaml_data and the resolved path, so a provider can look something up by path.
    option_context["yaml_data"] = yaml_data
    option_context["yaml_path"] = real_path
    options = _resolve_options(options_provider_name, option_context)

    htmx_attrs: dict[str, str] = {}
    if htmx_trigger:
        htmx_attrs["hx-trigger"] = htmx_trigger
    if htmx_target:
        htmx_attrs["hx-target"] = htmx_target
    if htmx_swap:
        htmx_attrs["hx-swap"] = htmx_swap

    readonly = readonly_flag or (readonly_on_edit_flag and edit_mode)

    # An active locked_by_service forces the value and makes the field read-only.
    if locked_by_service and _is_service_active(locked_by_service, yaml_data):
        display_value = True
        readonly = True
        description = f"Vereist door: {_service_display_name(locked_by_service)}"

    # The name and path are the form path, the errors are keyed by the real path.
    return FormField(
        name=form_path,
        path=form_path,
        schema_type=str,
        widget_type=widget,
        label=label,
        required=required,
        description=description,
        placeholder=placeholder,
        value=display_value,
        options=options or None,
        errors=(errors or {}).get(real_path, []),
        warnings=(warnings or {}).get(real_path, []),
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
        virtualize=virt,
    )


def evaluate_show_when(dep_value: Any, show_when: dict[str, Any] | None) -> bool:  # noqa: ANN401  # a project-file value
    """Evaluate a ``show_when`` condition against a dependency value.

    Without a *show_when* the truthiness of *dep_value* decides. The operators:

    - ``{"contains": "value"}``: dep_value is a list holding "value"
    - ``{"contains_any": [...]}``: dep_value is a list holding any of them
    - ``{"not_equals": "value"}``: dep_value is anything BUT "value"
    - ``{"field": "value"}``: dep_value equals "value"
    - ``{"field": ["v1", "v2"]}``: dep_value is in the list
    """
    if show_when is None:
        return bool(dep_value)

    for key, expected in show_when.items():
        if key == "not_equals":
            # "Everything except this one value", for an allowed set that is open-ended and
            # so cannot be listed. An absent value is not the excluded one, so it shows.
            if dep_value == expected:
                return False
        elif key == "contains":
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


def should_render_editable(
    editable: EditableVisualizer,
    yaml_data: dict[str, Any],
    index: int | None = None,
    siblings: list[EditableVisualizer] | None = None,
    edit_mode: bool = False,
) -> bool:
    """Check if an editable should be rendered based on its dependencies.

    First a gate that has nothing to do with dependencies: a field with
    ``only_when_editing`` does not appear in the create wizard. See that flag in
    :mod:`opi.forms.visualizers.visualizer`.

    Then the four dependency patterns:

    1. show_when is an EditableCondition -> evaluate against yaml_data (no depends_on needed)
    2. No depends_on -> always render (True)
    3. depends_on set, no show_when -> render if dependency value is truthy
    4. depends_on + show_when dict -> evaluate conditions (see ``evaluate_show_when``)

    With *siblings* given, the dependency value goes through the dependency field's own
    converter before the comparison. That is what a converter mapping a stored value onto a
    sentinel needs (``CustomDomainSelectConverter`` maps ``"mijnapp.nl"`` to ``"__custom__"``).
    """
    if editable.only_when_editing and not edit_mode:
        return False

    ed = editable.editable
    if ed.hidden_without_options and not _has_options(editable, yaml_data, index):
        return False
    depends_on = ed.depends_on
    show_when = ed.show_when

    if isinstance(show_when, EditableCondition):
        # The resolver map lets the condition resolve a transient default, such as the
        # base-domain when none was explicitly selected.
        if siblings and hasattr(show_when, "set_resolvers"):
            show_when.set_resolvers(build_resolver_map(siblings))
        return show_when.check(yaml_data)

    if not depends_on:
        return True

    if index is not None and "[*]" in depends_on:
        depends_on = depends_on.replace("[*]", f"[{index}]", 1)

    dep_value = smart_get_value(yaml_data, depends_on)

    # The same virtual fallback as the value read in editable_to_form_field. A dependency on
    # another service's config names the real path, but in wizard state that config lives
    # under the virtual key, so without this the condition always sees None and the field
    # stays hidden for the whole wizard.
    if dep_value is None:
        virt = editable.editable.virtualize
        if virt:
            virtual_depends_on = apply_virtualize(depends_on, virt)
            if virtual_depends_on != depends_on:
                dep_value = smart_get_value(yaml_data, virtual_depends_on)

    # Through the dependency field's converter, so show_when compares against the
    # form-compatible value ("__custom__", "DAILY") and not the raw stored one.
    if siblings and show_when and dep_value is not None:
        dep_converter = _find_converter_for_path(siblings, depends_on)
        if dep_converter:
            dep_value = dep_converter.read(dep_value, context_data=yaml_data)

    # Not in the data yet, as in a freshly added sequence row: the dependency field's
    # default makes show_when reflect the default selection on the first render.
    if dep_value is None and siblings:
        dep_value = _find_default_for_path(siblings, depends_on)
        # A computed default is resolved before the comparison: the function object itself
        # silently compares False against any show_when value.
        if callable(dep_value):
            dep_value = dep_value(yaml_data)

    return evaluate_show_when(dep_value, show_when)


def _has_options(editable: EditableVisualizer, yaml_data: dict[str, Any], index: int | None) -> bool:
    """Whether this field's own options provider has anything to offer right now.

    ``yaml_path`` travels along because a provider may phrase itself differently per layer
    (the registry picker's empty option means "automatic" on a component and "no override" on
    a deployment-component).
    """
    ed = editable.editable
    if not ed.values_provider:
        return False
    yaml_path = resolve_path(ed.yaml_path, index) if index is not None else ed.yaml_path
    return bool(_resolve_options(ed.values_provider, {"yaml_data": yaml_data, "yaml_path": yaml_path}))


def resolve_options_for_editable(
    editable: EditableVisualizer,
    context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Resolve dynamic options using the PROVIDER_REGISTRY."""
    provider_name = editable.editable.values_provider
    return _resolve_options(provider_name, context)


def _find_converter_for_path(
    siblings: list[EditableVisualizer],
    yaml_path: str,
) -> Any | None:  # noqa: ANN401  # a project-file value
    """Find the converter for the editable whose yaml_path matches *yaml_path*."""
    for sib in siblings:
        if sib.editable.yaml_path == yaml_path:
            return sib.editable.converter
        if sib.children:
            result = _find_converter_for_path(sib.children, yaml_path)
            if result is not None:
                return result
    return None


def _normalize_indices(path: str) -> str:
    """Collapse concrete sequence indices to the ``[*]`` template form for matching."""
    return re.sub(r"\[\d+\]", "[*]", path)


def _find_default_for_path(siblings: list[EditableVisualizer], yaml_path: str) -> Any | None:  # noqa: ANN401  # a project-file value
    """Find the ``default`` of the editable whose yaml_path matches *yaml_path*.

    ``depends_on`` is resolved to a concrete ``[index]`` inside a sequence while the
    sibling editables carry the ``[*]`` template path, so compare with indices
    normalized to ``[*]``.
    """
    target = _normalize_indices(yaml_path)
    for sib in siblings:
        if _normalize_indices(sib.editable.yaml_path) == target:
            return sib.editable.default
        if sib.children:
            result = _find_default_for_path(sib.children, yaml_path)
            if result is not None:
                return result
    return None


def _resolve_options(
    provider_name: str | None,
    context: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Resolve options by provider name."""
    if not provider_name:
        return []

    kwargs = _filter_provider_kwargs(provider_name, context or {})
    try:
        # Key names only, never values: one of the kwargs is ``yaml_data``, the whole project
        # dict, which carries repository passwords and every other secret.
        logger.debug(f"_resolve_options: provider={provider_name!r}, kwargs={sorted(kwargs)}")
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
    """Extract service names from a services list, in every shape it may hold.

    Through the shared reader, not by guessing: reading ``item.keys()`` yields
    ``["name", "config"]`` for a modern record and never the service's own name. A
    ``show_when={"contains": <service>}`` block then disappears the moment the list holds
    records rather than names, which is what adding a row to a service-config sequence does.
    """
    from opi.services.services import service_entry_name

    return [name for name in (service_entry_name(item) for item in items) if name]


def _is_service_active(service_name: str, yaml_data: dict[str, Any]) -> bool:
    """Check if a service is in the selected services list."""
    services = yaml_data.get("services", [])
    if not isinstance(services, list):
        return False
    return service_name in _extract_names_from_list(services)


_SERVICE_DISPLAY_NAMES: dict[str, str] = {
    "authorization-wall": "Authorization Wall",
    "keycloak": "Keycloak",
    "publish-on-web": "Publiceren op het web",
}


def _service_display_name(service_name: str) -> str:
    """Get a human-readable display name for a service."""
    return _SERVICE_DISPLAY_NAMES.get(service_name, service_name)
