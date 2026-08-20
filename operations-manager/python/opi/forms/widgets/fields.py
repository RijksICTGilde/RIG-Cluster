"""De veldvoorbereiding die elke widgetadapter deelt.

Per veldtype staat hier WAT er gerenderd wordt: welke opties een keuzelijst krijgt, hoe
een waarde in tekst komt, hoe een reeks zijn items opbouwt. Dat is bedrijfslogica en
verandert niet mee met het componentensysteem.

WAAR het mee gerenderd wordt, staat in de subklasse: die kiest de sjabloonmap en de
omgeving. Vandaag is dat er een - :class:`opi.forms.widgets.lotc.LOTCWidgetAdapter`.
Dit bestand heette ``roos.py`` en droeg daarnaast een eigen kale Jinja-omgeving op
``opi/templates/``; die is met de roos-bouwlijn verdwenen.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from opi.forms.editables.rendered_sequences import GERENDERDE_REEKSEN_VELD
from opi.forms.widgets.base import WidgetAdapter
from opi.services.catalog.aliases.overzicht import alias_variabelen

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
    """De gedeelde veldvoorbereiding; een subklasse levert de render.

    Zelf niet bruikbaar: :meth:`_render_template` kiest de sjabloonmap en de omgeving en
    hoort daarom bij het componentensysteem, niet hier.
    """

    def _render_template(self, template_name: str, ctx: dict[str, object]) -> str:
        raise NotImplementedError

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
        locked_values_raw = field.attributes.get("locked_values", "")
        locked_values: set[str] = set(locked_values_raw.split(",")) if locked_values_raw else set()
        cards: list[dict[str, object]] = []
        for option in options:
            value = str(option.get("value", ""))
            checked = value in selected_names
            dependents = depended_by_active.get(value, [])
            is_locked = checked and (bool(dependents) or value in locked_values)

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
            # A ticked service whose config lives on another layer shows no config screen
            # after this step. Say where it IS configured instead of leaving the user with
            # nothing happening (RC-33). Derived from the registry, so the template never
            # names a service.
            #
            # Rendered for every service that has one, ticked or not, and revealed by CSS
            # on the selected card: the user ticks a box and moves on with Next, so a
            # server-rendered line only reaches them on a page they would never revisit.
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
                    # Vergrendeld is NIET disabled. ``disabled`` in HTML betekent twee
                    # dingen tegelijk -- niet aanpasbaar en niet versturen -- en wij
                    # bedoelen alleen het eerste. Een vergrendelde dienst moet juist
                    # meekomen in de POST; het slot is een UI-eigenschap, bewaakt door de
                    # JS (de verandering wordt teruggedraaid) en door de server
                    # (``apply_services_mutation`` vult een vereiste dienst aan).
                    "disabled": field.readonly,
                    "server_locked": value in locked_values,
                    "data_requires": json.dumps(svc_deps) if svc_deps else None,
                    "dependents_labels": dependents_labels,
                    "help_template": str(help_template) if help_template else None,
                    "config_hint": config_hint,
                    "requires_approval": bool(option.get("requires_approval")),
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
        """De namen die de editor mag voorstellen, als JSON, of een lege tekst.

        Alleen het aliassenveld vraagt hierom (``kv_completions: "aliassen"``). De lijst
        wordt HIER opgehaald en niet in de visualizer opgeschreven: die is een constante
        op modulehoogte, en de variabelen worden afgeleid uit de dienstdefinities. Een
        vaste lijst zou binnen een release uit de pas lopen met de validatie.
        """
        if field.attributes.get("kv_completions") != "aliassen":
            return ""

        return json.dumps(
            [
                {"naam": variabele.naam, "dienst": dienst.label, "beschrijving": variabele.beschrijving}
                for dienst in alias_variabelen()
                for variabele in dienst.variabelen
            ]
        )

    def render_nested(self, field: FormField, children_html: list[str]) -> str:
        return self._render_template("nested.html.j2", {"field": field, "children_html": children_html})

    def render_sequence(self, field: FormField, items_html: list[str]) -> str:
        return self._render_template(
            "sequence.html.j2",
            {"field": field, "items_html": items_html, "gerenderde_reeksen_veld": GERENDERDE_REEKSEN_VELD},
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

    def render_flow(self, children_html: list[str]) -> str:
        """De buitenste verticale stapel om de velden van een stap.

        Hoort bij de adapter en niet bij de renderer omdat het een COMPONENTaanroep is;
        WELKE component, en of hij meteen gerenderd wordt, hangt van het componentensysteem
        af. Hier stond de kale ``<c-layout-flow>``-tag die de roos-adapter opleverde en die
        ``process_components`` later omzette. Die weg is er niet meer, dus dit is geen
        werkende ondergrens meer maar een val: de subklasse hoort hem te leveren.
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
    csrf_token: str = "",
) -> str:
    """Render preset cards using the same visual style as service cards.

    Args:
        presets: Available presets for this section.
        flow_id: Current wizard flow ID.
        section_id: Current section ID.
        yaml_data: Current YAML data for detecting applied state.
        locked_presets: Map of preset_id -> hint text for presets that
            cannot be toggled (e.g. forced by a service dependency).
        csrf_token: CSRF token rendered into the cards' hx-post header so
            the preset POST passes central CSRF enforcement.

    Het kaarttemplate rendert in de templateomgeving. Die import staat binnenin om een
    kringloop te vermijden: de omgeving leunt via de dienstenregistry op deze pakketmap.
    """
    from opi.core.templates_lotc import templates_lotc

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
