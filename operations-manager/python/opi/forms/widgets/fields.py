"""The field preparation every widget adapter shares.

Per field type this says WHAT is rendered: which options a select gets, how a value becomes
text, how a sequence builds its items. That is domain logic and does not change along with
the component system.

WHAT it is rendered with sits in the subclass, which picks the template directory and the
environment; today that is :class:`opi.forms.widgets.lotc.LOTCWidgetAdapter`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from opi.forms.editables.rendered_sequences import RENDERED_SEQUENCES_FIELD
from opi.forms.editables.sequence_identity import IDENTITY_KEYS
from opi.forms.widgets.base import WidgetAdapter
from opi.services.catalog.aliases.overview import alias_variabelen

if TYPE_CHECKING:
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


class FieldWidgetAdapter(WidgetAdapter):
    """The shared field preparation; a subclass supplies the rendering.

    Not usable on its own: :meth:`_render_template` picks the template directory and the
    environment, which belongs with the component system and not here.
    """

    def _render_template(self, template_name: str, ctx: dict[str, object]) -> str:
        raise NotImplementedError

    def render_text(self, field: FormField) -> str:
        return self._render_template("text.html.j2", {"field": field})

    def render_password(self, field: FormField) -> str:
        return self._render_template("text.html.j2", {"field": field, "input_type": "password"})

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

        The dependency algorithm, identical on the server and in the client: build
        ``requires_map`` (service -> its service-level deps), then the reverse map (dep ->
        the active services needing it); a service is locked when it is checked AND something
        active depends on it.
        """
        # Imported here, not at module scope: the service registry imports the catalog,
        # which imports the forms package, which imports this module. A top-level import
        # closes that cycle and breaks every import of opi.services.registry.
        from opi.services.config_location import config_hint_for_value

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

        depended_by_active: dict[str, list[str]] = {}
        for svc, deps in requires_map.items():
            if svc in selected_names:
                for dep in deps:
                    depended_by_active.setdefault(dep, []).append(svc)

        locked_values_raw = field.attributes.get("locked_values", "")
        locked_values: set[str] = set(locked_values_raw.split(",")) if locked_values_raw else set()
        cards: list[dict[str, object]] = []
        for option in options:
            value = str(option.get("value", ""))
            checked = value in selected_names
            dependents = depended_by_active.get(value, [])
            is_locked = checked and (bool(dependents) or value in locked_values)

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
            # A ticked service whose config lives on another layer shows no config screen
            # after this step, so the hint says where it IS configured. Derived from the
            # registry, so the template never names a service.
            #
            # Rendered for every service that has one, ticked or not, and revealed by CSS on
            # the selected card: the user ticks a box and moves on with Next, so a
            # server-rendered line would only reach them on a page they never revisit.
            config_hint = config_hint_for_value(value)
            cards.append(
                {
                    "value": value,
                    "label": str(option.get("label", value)),
                    "description": str(option.get("description", "")),
                    "icon": str(option.get("icon", "document")),
                    "color": str(option.get("color", "hemelblauw")),
                    "checked": checked,
                    "is_locked": is_locked,
                    # Locked is NOT disabled. ``disabled`` in HTML means two things at once,
                    # not changeable and not submitted, and only the first is meant here: a
                    # locked service has to travel along in the POST. The lock is a UI
                    # property, guarded by the JS (which reverts the change) and by the server
                    # (``apply_services_mutation`` adds a required service back).
                    "disabled": field.readonly,
                    "server_locked": value in locked_values,
                    "data_requires": json.dumps(svc_deps) if svc_deps else None,
                    "dependents_labels": dependents_labels,
                    "help_template": str(help_template) if help_template else None,
                    "config_hint": config_hint,
                    "requires_approval": bool(option.get("requires_approval")),
                    # Only filled for a replaced service, and that card only shows for a
                    # project that already has it (ServiceOptionsProvider).
                    "replaced_by": option.get("replaced_by"),
                }
            )

        return self._render_template("service_cards.html.j2", {"field": field, "cards": cards})

    def render_display_card(self, field: FormField) -> str:
        return self._render_template("display_card.html.j2", {"field": field})

    def render_key_value_editor(self, field: FormField) -> str:
        return self._render_template(
            "key_value_editor.html.j2",
            {"field": field, "completions_json": self._kv_completions_json(field)},
        )

    @staticmethod
    def _kv_completions_json(field: FormField) -> str:
        """The names the editor may suggest, as JSON, or an empty string.

        Only the aliases field asks for this (``kv_completions: "aliassen"``). The list is
        fetched HERE and not written out in the visualizer, which is a module-level constant:
        the variables are derived from the service definitions, and a fixed list would drift
        away from the validation within a release.
        """
        if field.attributes.get("kv_completions") != "aliassen":
            return ""

        return json.dumps(
            [
                {"naam": variable.naam, "dienst": service.label, "beschrijving": variable.beschrijving}
                for service in alias_variabelen()
                for variable in service.variabelen
            ]
        )

    def render_nested(self, field: FormField, children_html: list[str]) -> str:
        return self._render_template("nested.html.j2", {"field": field, "children_html": children_html})

    def render_sequence(self, field: FormField, items_html: list[str]) -> str:
        return self._render_template(
            "sequence.html.j2",
            {"field": field, "items_html": items_html, "gerenderde_reeksen_veld": RENDERED_SEQUENCES_FIELD},
        )

    def _is_simple_sequence(self, field: FormField) -> bool:
        if not field.children:
            return True
        first_item = field.children[0]
        if not first_item.children:
            return True
        return len(first_item.children) == 1 and first_item.children[0].widget_type != "sequence"

    def render_sequence_item(self, field: FormField, index: int, item_html: str) -> str:
        template = "sequence_item_inline.html.j2" if self._is_simple_sequence(field) else "sequence_item_card.html.j2"
        return self._render_template(
            template,
            {
                "field": field,
                "index": index,
                "item_html": item_html,
                "identiteitsvelden": self._identity_fields(field, index),
            },
        )

    @staticmethod
    def _identity_fields(field: FormField, index: int) -> list[dict[str, str]]:
        """The key of row *index* as a hidden field, as long as the row does not post it.

        A readonly key renders as ``disabled`` and is therefore not submitted, which leaves
        the processor pairing rows by index, and then a row behind a removed row inherits its
        neighbour's configuration. See ``opi/forms/editables/sequence_identity.py``.
        """
        items = field.children or []
        if not 0 <= index < len(items):
            return []
        return [
            {"name": kind.path, "value": str(kind.value)}
            for kind in items[index].children or []
            if kind.readonly and kind.value and kind.path.rsplit("/", 1)[-1] in IDENTITY_KEYS
        ]

    def render_row(self, row: Row, children_html: list[str]) -> str:
        return self._render_template("row.html.j2", {"row": row, "children_content": "\n".join(children_html)})

    def render_column(self, column: Column, child_html: str) -> str:
        return self._render_template("column.html.j2", {"column": column, "child_html": child_html})

    def render_fieldset(self, fieldset: Fieldset, children_html: list[str]) -> str:
        return self._render_template(
            "fieldset.html.j2", {"fieldset": fieldset, "children_content": "\n".join(children_html)}
        )

    def render_flow(self, children_html: list[str]) -> str:
        """The outer vertical stack around the fields of a step.

        On the adapter and not on the renderer because it is a COMPONENT call, and which
        component, and whether it is rendered right away, depends on the component system.
        There is no working fallback here: the subclass has to supply it.
        """
        raise NotImplementedError

    def render_div(self, div: Div, children_html: list[str]) -> str:
        return self._render_template("div.html.j2", {"div": div, "children_content": "\n".join(children_html)})

    def render_submit(self, submit: Submit) -> str:
        return self._render_template("submit.html.j2", {"submit": submit})

    def render_button_group(self, button_group: ButtonGroup, buttons_html: list[str]) -> str:
        return self._render_template(
            "button_group.html.j2",
            {"button_group": button_group, "buttons_content": "\n".join(buttons_html)},
        )

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
    csrf_token: str = "",
) -> str:
    """Render preset cards in the same visual style as the service cards.

    ``locked_presets`` maps a preset_id onto the hint text for a preset that cannot be
    toggled, for instance because a service dependency forces it. ``csrf_token`` is rendered
    into the cards' hx-post header, so the preset POST passes the central CSRF enforcement.

    The card template renders in the template environment, imported inside the function to
    avoid a cycle: that environment leans on this package through the service registry.
    """
    from opi.http.render.templates_lotc import templates_lotc

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

    template = templates_lotc.env.get_template("widgets/preset_cards.html.j2")
    return template.render(
        preset_states=preset_states,
        flow_id=flow_id,
        section_id=section_id,
        csrf_token=csrf_token,
    )


def _is_preset_applied(preset: Preset, yaml_data: dict) -> bool:
    """Check if all values in a preset are already present in yaml_data."""
    from opi.forms.editables.service_path import smart_get_value

    for path, value in preset.values.items():
        existing = smart_get_value(yaml_data, path)
        if existing is None:
            return False
        if isinstance(value, list):
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
