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

    # --- Shared logic ---

    # 1. Resolve the path (real for data access, virtual for form names)
    real_path = resolve_path(yaml_path, index)
    virt = ed.virtualize or parent_virtualize
    form_path = apply_virtualize(real_path, virt) if virt else real_path

    # 2. Extract value from YAML, real path first, then the virtual one.
    #
    # The virtual fallback mirrors ``_read_submitted`` in the processor, and without it a
    # value the wizard stored was invisible when the step was rendered again: wizard state
    # keeps service CONFIG under the virtual key while the real ``services`` key holds only
    # the chosen names. The read then returned None, the default below took over, and the
    # user saw the default where their own choice should have been -- indistinguishable
    # from "my change was not saved", and it did get overwritten on the next submit.
    raw_value = smart_get_value(yaml_data, real_path)
    if raw_value is None and virt and form_path != real_path:
        raw_value = smart_get_value(yaml_data, form_path)

    # A callable default is computed from the surrounding project data, so a field can be
    # prefilled with something derived (the first team member's address, a text carrying the
    # project name) instead of a constant. It runs only when the field has no stored value,
    # so it never overwrites what a user typed, and it may return None to mean "no default
    # after all". Errors are deliberately not caught: a broken default is a bug in our own
    # code, and swallowing it would silently render an empty field instead.
    if raw_value is None and default is not None:
        raw_value = default(yaml_data) if callable(default) else default

    # 3. Apply converter for display
    # For editable widgets, use read() to convert stored value → form-compatible value
    # (e.g. dict → string for select dropdowns). Use view() for read-only display.
    display_value = raw_value
    if converter:
        if widget in ("select", "text", "textarea", "radio"):
            display_value = converter.read(raw_value, context_data=yaml_data)
        else:
            display_value = converter.view(raw_value, context_data=yaml_data)

    # 3a. Een aanvinkvakje staat aan of uit, dus zijn waarde is een ECHTE boolean.
    #
    # Hier ging het mis: een vakje is geen select/text/textarea/radio, dus het viel in de
    # tak hierboven die ``view()`` gebruikt - de MENSELIJKE weergave. Een BooleanConverter
    # levert daar "Ja" of "Nee" op, en het sjabloon toetst ``:checked="field.value"``: een
    # niet-lege tekst, dus ook "Nee" zette het vakje aan. Elk vakje stond aan, ook bij een
    # opgeslagen ``false``. Zichtbaar bij "Markeer voor verwijdering" van een databaseschema
    # en bij "Versiebeheer op de bucket".
    #
    # De waarheid staat in de opgeslagen waarde, niet in de weergave ervan; de reeks
    # hieronder is dezelfde als die BooleanConverter.write() gebruikt, zodat tonen en
    # opslaan het over hetzelfde eens zijn.
    if widget == "checkbox":
        display_value = raw_value in (True, "true", "on", "yes", "1")

    # 3b. Auto-detect KV format from stored value so the toggle matches
    if converter and hasattr(converter, "detect_format") and raw_value is not None:
        detected_fmt = converter.detect_format(raw_value)
        attributes = dict(attributes or {})
        attributes["kv_format"] = detected_fmt

    # 4. Resolve options (pass current value so providers can include tuner-set values)
    option_context = dict(provider_context or {})
    if raw_value is not None:
        option_context.setdefault("current_value", str(raw_value))
    # Pass yaml_data and resolved path so providers can do path-based lookups
    option_context["yaml_data"] = yaml_data
    option_context["yaml_path"] = real_path
    options = _resolve_options(options_provider_name, option_context)

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

    # 8. Build FormField (use form_path for name/path, look up errors by real_path)
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


def evaluate_show_when(dep_value: Any, show_when: dict[str, Any] | None) -> bool:
    """Evaluate a ``show_when`` condition against a dependency value.

    Returns True when the condition is met (or when *show_when* is None,
    in which case truthiness of *dep_value* decides).

    Supported operators:
    - ``{"contains": "value"}`` - dep_value is a list containing "value"
    - ``{"contains_any": [...]}`` - dep_value is a list containing any value
    - ``{"not_equals": "value"}`` - dep_value is anything BUT "value"
    - ``{"field": "value"}`` - dep_value equals "value"
    - ``{"field": ["v1", "v2"]}`` - dep_value is in the list
    """
    if show_when is None:
        return bool(dep_value)

    for key, expected in show_when.items():
        if key == "not_equals":
            # "Everything except this one value" (RC-142: every peer project except the
            # wildcard). Listing the allowed values is not an option when the allowed set
            # is open-ended, and an absent value is not the excluded one, so it shows.
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

    Eerst een poort die niets met afhankelijkheden te maken heeft: een veld met
    ``alleen_bij_bewerken`` verschijnt niet in de aanmaakwizard. Zie de toelichting bij die
    vlag in :mod:`opi.forms.visualizers.visualizer`.

    Implements 4 dependency patterns:

    1. show_when is an EditableCondition -> evaluate against yaml_data (no depends_on needed)
    2. No depends_on -> always render (True)
    3. depends_on set, no show_when -> render if dependency value is truthy
    4. depends_on + show_when dict -> evaluate conditions (see ``evaluate_show_when``)

    When *siblings* is provided, the dependency value is passed through the
    dependency field's converter (if any) before comparison.  This is needed
    when a converter maps stored values to sentinel display values (e.g.
    ``CustomDomainSelectConverter`` maps ``"mijnapp.nl"`` → ``"__custom__"``).
    """
    if editable.alleen_bij_bewerken and not edit_mode:
        return False

    ed = editable.editable
    depends_on = ed.depends_on
    show_when = ed.show_when

    # Callable condition: evaluate against full yaml_data
    if isinstance(show_when, EditableCondition):
        # Provide resolver map so the condition can resolve transient
        # defaults (e.g. base-domain when not explicitly selected)
        if siblings and hasattr(show_when, "set_resolvers"):
            show_when.set_resolvers(build_resolver_map(siblings))
        return show_when.check(yaml_data)

    if not depends_on:
        return True

    # Resolve [*] wildcard in depends_on when rendering inside a sequence
    if index is not None and "[*]" in depends_on:
        depends_on = depends_on.replace("[*]", f"[{index}]", 1)

    dep_value = smart_get_value(yaml_data, depends_on)

    # Same virtual fallback as the value read in editable_to_form_field. A dependency on
    # another service's config (e.g. the invite auth methods following the keycloak
    # template) names the real path, but in wizard state that config lives under the
    # virtual key while the real ``services`` key holds only the chosen names. Without
    # this the condition always saw None and the field stayed hidden for the whole wizard.
    if dep_value is None:
        virt = editable.editable.virtualize
        if virt:
            virtual_depends_on = apply_virtualize(depends_on, virt)
            if virtual_depends_on != depends_on:
                dep_value = smart_get_value(yaml_data, virtual_depends_on)

    # Apply the dependency field's converter so show_when compares against
    # the form-compatible value (e.g. "__custom__", "DAILY") rather than
    # the raw stored value (e.g. a custom domain, an RRULE string).
    if siblings and show_when and dep_value is not None:
        dep_converter = _find_converter_for_path(siblings, depends_on)
        if dep_converter:
            dep_value = dep_converter.read(dep_value, context_data=yaml_data)

    # Not in the data yet (e.g. a freshly added sequence item): fall back to the
    # dependency field's default so show_when reflects the default selection on
    # first render (e.g. provide-as defaults to "file" -> show the path field).
    if dep_value is None and siblings:
        dep_value = _find_default_for_path(siblings, depends_on)
        # A computed default must be resolved before comparison: comparing the function
        # object itself against a show_when value silently evaluates to False.
        if callable(dep_value):
            dep_value = dep_value(yaml_data)

    return evaluate_show_when(dep_value, show_when)


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


def _find_converter_for_path(
    siblings: list[EditableVisualizer],
    yaml_path: str,
) -> Any | None:
    """Find the converter for the editable whose yaml_path matches *yaml_path*."""
    for sib in siblings:
        if sib.editable.yaml_path == yaml_path:
            return sib.editable.converter
        # Recurse into groups
        if sib.children:
            result = _find_converter_for_path(sib.children, yaml_path)
            if result is not None:
                return result
    return None


def _normalize_indices(path: str) -> str:
    """Collapse concrete sequence indices to the ``[*]`` template form for matching."""
    return re.sub(r"\[\d+\]", "[*]", path)


def _find_default_for_path(siblings: list[EditableVisualizer], yaml_path: str) -> Any | None:
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
        # dict, which carries repository passwords and every other secret in the project file.
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

    Delegates to the shared reader instead of guessing. The local version took
    ``item.keys()``, which is the legacy single-key form: for the modern record
    ``{"name": "cross-domain-access", "config": {...}}`` it yielded ``["name", "config"]``
    and the service's own name never appeared.

    That is not cosmetic. A ``show_when={"contains": <service>}`` block disappears the
    moment the list holds records rather than names, and adding a row to a service-config
    sequence rewrites the list into exactly that shape. So the first click on "Item
    toevoegen" hid the whole section it was supposed to extend.
    """
    from opi.services.services import service_entry_name

    return [name for name in (service_entry_name(item) for item in items) if name]


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
