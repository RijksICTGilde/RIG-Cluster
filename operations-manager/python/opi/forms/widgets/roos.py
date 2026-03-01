"""
ROOS Widget Adapter for form rendering.

Renders FormField instances to ROOS (RVO Open Source) component library HTML
using Jinja2 templates from ``templates/widgets/``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from opi.forms.widgets.base import WidgetAdapter

if TYPE_CHECKING:
    from jinja2 import Environment

    from opi.forms.field import FormField
    from opi.forms.layout import (
        ButtonGroup,
        Column,
        Div,
        Fieldset,
        Row,
        Submit,
    )
    from opi.forms.presets.loader import Preset


def _attr_escape(value: object) -> str:
    """Escape a value for use inside a double-quoted HTML attribute.

    Unlike Jinja2's built-in ``|e`` filter, this does NOT escape single
    quotes.  ROOS web components read attribute values as plain strings,
    so ``&#39;`` would appear literally in the UI.  Since attributes are
    always double-quoted in our templates, escaping ``'`` is unnecessary.
    """
    s = str(value)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _get_widget_env() -> Environment:
    """Get a plain Jinja2 environment for widget templates.

    Widget templates output ROOS component tags (``<c-text-input-field>``, etc.)
    as raw strings.  These are later processed by the ``process_components``
    filter in ``wizard_step.html.j2``.  Using the main environment (which has
    ``ComponentExtension``) would cause the preprocessor to fail on Jinja2
    expressions inside component attributes, so we use a plain environment that
    shares the same template directory.
    """
    from jinja2 import Environment, FileSystemLoader

    from opi.core.templates import TEMPLATES_DIR

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=False,
    )
    env.filters["attr_escape"] = _attr_escape
    return env


class ROOSWidgetAdapter(WidgetAdapter):
    """
    ROOS component library widget adapter.

    Renders form fields using Jinja2 templates that emit ROOS web component
    tags (c-text-input-field, c-select-field, etc.).
    """

    def __init__(self) -> None:
        self._env = _get_widget_env()

    def _render_template(self, template_name: str, ctx: dict[str, object]) -> str:
        template = self._env.get_template(f"widgets/{template_name}")
        return template.render(**ctx)

    # ------------------------------------------------------------------
    # Field rendering methods
    # ------------------------------------------------------------------

    def render_text(self, field: FormField) -> str:
        return self._render_template("text.html.j2", {"field": field})

    def render_textarea(self, field: FormField) -> str:
        return self._render_template("textarea.html.j2", {"field": field})

    def render_select(self, field: FormField) -> str:
        options: list[dict[str, object]] = list(field.options) if field.options else []
        options_json = json.dumps(options).replace('"', "'")

        raw_value: object = field.value
        if isinstance(raw_value, list):
            value_list: list[object] = raw_value  # type: ignore[assignment]
            if len(value_list) == 1:
                value_str = str(value_list[0]) if value_list[0] is not None else ""
            else:
                value_str = str(value_list) if value_list else ""
        else:
            value_str = str(raw_value) if raw_value is not None else ""

        return self._render_template(
            "select.html.j2",
            {"field": field, "options_json": options_json, "value_str": value_str},
        )

    def render_checkbox(self, field: FormField) -> str:
        return self._render_template("checkbox.html.j2", {"field": field})

    def render_checkbox_group(self, field: FormField) -> str:
        options = [
            {"value": str(o.get("value", "")), "label": str(o.get("label", o.get("value", "")))}
            for o in (field.options or [])
        ]
        raw_value: object = field.value
        if (not raw_value or raw_value == "__all__") and field.default == "__all__":
            selected = [o["value"] for o in options]
        else:
            selected = [str(v) for v in (raw_value if isinstance(raw_value, list) else [])]  # type: ignore[union-attr]
        return self._render_template(
            "checkbox_group.html.j2",
            {"field": field, "options": options, "selected_values": selected},
        )

    def render_radio(self, field: FormField) -> str:
        options = []
        for o in field.options or []:
            value = str(o.get("value", ""))
            options.append(
                {
                    "value": value,
                    "label": str(o.get("label", value)),
                    "description": str(o["description"]) if o.get("description") else None,
                    "checked": str(value) == str(field.value) if field.value else False,
                }
            )
        return self._render_template("radio.html.j2", {"field": field, "options": options})

    def render_number(self, field: FormField) -> str:
        return self._render_template("number.html.j2", {"field": field})

    def render_date(self, field: FormField) -> str:
        return self._render_template("date.html.j2", {"field": field})

    def render_file(self, field: FormField) -> str:
        return self._render_template("file.html.j2", {"field": field})

    def render_hidden(self, field: FormField) -> str:
        return self._render_template("hidden.html.j2", {"field": field})

    def render_service_cards(self, field: FormField) -> str:
        """Render service options as selectable cards with dependency logic.

        The dependency algorithm (identical on server and client):
        1. Build requires_map: service -> service-level deps
        2. Build reverse_deps: dep -> [active services that need it]
        3. locked = checked AND has active reverse deps
        """
        options: list[dict[str, object]] = list(field.options) if field.options else []
        raw_value: object = field.value
        selected_values: list[object]
        if isinstance(raw_value, list):  # noqa: SIM108
            selected_values = raw_value  # type: ignore[assignment]
        else:
            selected_values = []

        selected_names: set[str] = set()
        for val in selected_values:
            if isinstance(val, str):
                selected_names.add(val)
            elif isinstance(val, dict):
                val_dict: dict[str, object] = val  # type: ignore[assignment]
                selected_names.update(val_dict.keys())

        # Step 1: requires_map
        requires_map: dict[str, list[str]] = {}
        for option in options:
            requires_raw = option.get("requires")
            if isinstance(requires_raw, list):
                svc_deps = [
                    r.removeprefix("services/")
                    for r in requires_raw
                    if isinstance(r, str) and r.startswith("services/") and r.count("/") == 1
                ]
                if svc_deps:
                    requires_map[str(option.get("value", ""))] = svc_deps

        # Step 2: reverse_deps
        depended_by_active: dict[str, list[str]] = {}
        for svc, deps in requires_map.items():
            if svc in selected_names:
                for dep in deps:
                    depended_by_active.setdefault(dep, []).append(svc)

        # Build card data
        cards: list[dict[str, object]] = []
        for option in options:
            value = str(option.get("value", ""))
            checked = value in selected_names
            dependents = depended_by_active.get(value, [])
            is_locked = checked and bool(dependents)

            # Resolve dependent labels for the hint
            dependents_labels: list[str] = []
            if is_locked:
                for dep_val in dependents:
                    for opt in options:
                        if str(opt.get("value", "")) == dep_val:
                            dependents_labels.append(str(opt.get("label", dep_val)))
                            break
                    else:
                        dependents_labels.append(dep_val)

            svc_deps = requires_map.get(value, [])
            help_template = option.get("help_template")
            cards.append(
                {
                    "value": value,
                    "label": str(option.get("label", value)),
                    "description": str(option.get("description", "")),
                    "icon": str(option.get("icon", "document")),
                    "color": str(option.get("color", "hemelblauw")),
                    "checked": checked,
                    "is_locked": is_locked,
                    "disabled": field.readonly or is_locked,
                    "data_requires": json.dumps(svc_deps) if svc_deps else None,
                    "dependents_labels": dependents_labels,
                    "help_template": str(help_template) if help_template else None,
                }
            )

        return self._render_template("service_cards.html.j2", {"field": field, "cards": cards})

    def render_display_card(self, field: FormField) -> str:
        return self._render_template("display_card.html.j2", {"field": field})

    def render_key_value_editor(self, field: FormField) -> str:
        return self._render_template("key_value_editor.html.j2", {"field": field})

    def render_nested(self, field: FormField, children_html: list[str]) -> str:
        return self._render_template("nested.html.j2", {"field": field, "children_html": children_html})

    def render_sequence(self, field: FormField, items_html: list[str]) -> str:
        return self._render_template("sequence.html.j2", {"field": field, "items_html": items_html})

    def _is_simple_sequence(self, field: FormField) -> bool:
        if not field.children:
            return True
        first_item = field.children[0]
        if not first_item.children:
            return True
        return len(first_item.children) == 1 and first_item.children[0].widget_type != "sequence"

    def render_sequence_item(self, field: FormField, index: int, item_html: str) -> str:
        template = "sequence_item_inline.html.j2" if self._is_simple_sequence(field) else "sequence_item_card.html.j2"
        return self._render_template(template, {"field": field, "index": index, "item_html": item_html})

    # ------------------------------------------------------------------
    # Layout rendering methods
    # ------------------------------------------------------------------

    def render_row(self, row: Row, children_html: list[str]) -> str:
        return self._render_template("row.html.j2", {"row": row, "children_content": "\n".join(children_html)})

    def render_column(self, column: Column, child_html: str) -> str:
        return self._render_template("column.html.j2", {"column": column, "child_html": child_html})

    def render_fieldset(self, fieldset: Fieldset, children_html: list[str]) -> str:
        return self._render_template(
            "fieldset.html.j2", {"fieldset": fieldset, "children_content": "\n".join(children_html)}
        )

    def render_div(self, div: Div, children_html: list[str]) -> str:
        return self._render_template("div.html.j2", {"div": div, "children_content": "\n".join(children_html)})

    def render_submit(self, submit: Submit) -> str:
        return self._render_template("submit.html.j2", {"submit": submit})

    def render_button_group(self, button_group: ButtonGroup, buttons_html: list[str]) -> str:
        return self._render_template(
            "button_group.html.j2",
            {"button_group": button_group, "buttons_content": "\n".join(buttons_html)},
        )

    # ------------------------------------------------------------------
    # Error and message rendering
    # ------------------------------------------------------------------

    def render_error(self, message: str) -> str:
        return f'<span class="rvo-form-field__error-text">{self.escape_html(message)}</span>'

    def render_errors(self, field: FormField) -> str:
        if not field.errors:
            return ""
        return self._render_template("errors.html.j2", {"errors": field.errors})

    def render_description(self, text: str) -> str:
        if not text:
            return ""
        return f'<span class="rvo-form-field__helper-text">{self.escape_html(text)}</span>'

    # ------------------------------------------------------------------
    # Form wrapper
    # ------------------------------------------------------------------

    def render_form_start(
        self,
        form_id: str,
        action: str,
        method: str = "post",
        enctype: str | None = None,
        htmx_attrs: dict[str, str] | None = None,
    ) -> str:
        return self._render_template(
            "form_start.html.j2",
            {
                "form_id": form_id,
                "action": action,
                "method": method,
                "enctype": enctype,
                "htmx_attrs": htmx_attrs or {},
            },
        )

    def render_form_end(self) -> str:
        return self._render_template("form_end.html.j2", {})


def render_preset_cards(
    presets: list[Preset],
    flow_id: str,
    section_id: str,
    yaml_data: dict | None = None,
    locked_presets: dict[str, str] | None = None,
) -> str:
    """Render preset cards using the same visual style as service cards.

    Args:
        presets: Available presets for this section.
        flow_id: Current wizard flow ID.
        section_id: Current section ID.
        yaml_data: Current YAML data for detecting applied state.
        locked_presets: Map of preset_id -> hint text for presets that
            cannot be toggled (e.g. forced by a service dependency).
    """
    if not presets:
        return ""

    locked_presets = locked_presets or {}

    preset_states: list[dict[str, Any]] = []
    for preset in presets:
        applied = _is_preset_applied(preset, yaml_data) if yaml_data else False
        locked = preset.id in locked_presets
        preset_states.append(
            {
                "preset": preset,
                "applied": applied or locked,
                "locked": locked,
                "locked_hint": locked_presets.get(preset.id, ""),
            }
        )

    env = _get_widget_env()
    template = env.get_template("widgets/preset_cards.html.j2")
    return template.render(
        preset_states=preset_states,
        flow_id=flow_id,
        section_id=section_id,
    )


def _is_preset_applied(preset: Preset, yaml_data: dict) -> bool:
    """Check if all values in a preset are already present in yaml_data."""
    from opi.forms.editables.service_path import smart_get_value

    for path, value in preset.values.items():
        existing = smart_get_value(yaml_data, path)
        if existing is None:
            return False
        if isinstance(value, list):
            # For lists: check that all preset items exist in the existing list
            if not isinstance(existing, list):
                return False
            for item in value:
                if isinstance(item, dict):
                    name = item.get("name")
                    if name and not any(isinstance(e, dict) and e.get("name") == name for e in existing):
                        return False
                elif item not in existing:
                    return False
        elif isinstance(value, bool):
            if existing != value:
                return False
        elif str(existing) != str(value):
            return False
    return True
