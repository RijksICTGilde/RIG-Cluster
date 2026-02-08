"""
ROOS Widget Adapter for form rendering.

Renders FormField instances to ROOS (RVO Open Source) component library HTML.
Uses components like c-text-input-field, c-select-field, c-textarea-field, etc.
"""

import json
from typing import TYPE_CHECKING

from opi.forms.widgets.base import WidgetAdapter

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


class ROOSWidgetAdapter(WidgetAdapter):
    """
    ROOS component library widget adapter.

    Renders form fields using ROOS web components (c-text-input-field,
    c-select-field, etc.) which are based on RVO design patterns.
    """

    # Field rendering methods

    def render_text(self, field: "FormField") -> str:
        """Render a text input field using c-text-input-field."""
        attrs = self._build_common_attrs(field)
        attrs["type"] = "text"

        if field.placeholder:
            attrs["placeholder"] = field.placeholder

        htmx = self._build_htmx_attrs(field)
        extra = self._build_extra_attrs(field)

        return f"""<c-text-input-field
    id="{self.escape_html(field.path)}"
    name="{self.escape_html(field.path)}"
    label="{self.escape_html(field.label)}"
    {self._format_attr("helperText", field.description)}
    {self._format_attr("placeholder", field.placeholder)}
    {self._format_bool_attr("required", field.required)}
    {self._format_bool_attr("disabled", field.readonly)}
    {self._format_attr("value", self._format_value(field.value))}
    {self._format_attr("invalid", "true" if field.errors else None)}
    {self._format_attr("errorText", field.errors[0] if field.errors else None)}
    {htmx}
    {extra}
/>"""

    def render_textarea(self, field: "FormField") -> str:
        """Render a textarea field using c-textarea-field."""
        rows = field.attributes.get("rows", "4")

        htmx = self._build_htmx_attrs(field)
        extra = self._build_extra_attrs(field)

        return f"""<c-textarea-field
    id="{self.escape_html(field.path)}"
    name="{self.escape_html(field.path)}"
    label="{self.escape_html(field.label)}"
    {self._format_attr("helperText", field.description)}
    {self._format_attr("placeholder", field.placeholder)}
    {self._format_bool_attr("required", field.required)}
    {self._format_bool_attr("disabled", field.readonly)}
    rows="{self.escape_html(str(rows))}"
    {self._format_attr("invalid", "true" if field.errors else None)}
    {self._format_attr("errorText", field.errors[0] if field.errors else None)}
    {htmx}
    {extra}
>{self.escape_html(str(field.value) if field.value else "")}</c-textarea-field>"""

    def render_select(self, field: "FormField") -> str:
        """Render a select/dropdown field using c-select-field."""
        options: list[dict[str, object]] = list(field.options) if field.options else []
        # Use single quotes in JSON to avoid conflicts with HTML attribute quotes
        options_json = json.dumps(options).replace('"', "'")

        htmx = self._build_htmx_attrs(field)
        extra = self._build_extra_attrs(field)

        # For value, convert to string if it's a simple value
        raw_value: object = field.value
        value_str: str
        if isinstance(raw_value, list):
            value_list: list[object] = raw_value  # type: ignore[assignment]
            if len(value_list) == 1:
                value_str = str(value_list[0]) if value_list[0] is not None else ""
            else:
                value_str = str(value_list) if value_list else ""
        else:
            value_str = str(raw_value) if raw_value is not None else ""

        return f"""<c-select-field
    id="{self.escape_html(field.path)}"
    name="{self.escape_html(field.path)}"
    label="{self.escape_html(field.label)}"
    {self._format_attr("helperText", field.description)}
    {self._format_bool_attr("required", field.required)}
    {self._format_bool_attr("disabled", field.readonly)}
    :options="{options_json}"
    {self._format_attr("value", value_str)}
    {self._format_attr("invalid", "true" if field.errors else None)}
    {self._format_attr("errorText", field.errors[0] if field.errors else None)}
    {htmx}
    {extra}
/>"""

    def render_checkbox(self, field: "FormField") -> str:
        """Render a single checkbox field using c-checkbox."""
        checked = bool(field.value)

        htmx = self._build_htmx_attrs(field)
        extra = self._build_extra_attrs(field)

        return f"""<div class="rvo-form-group">
    <c-checkbox
        id="{self.escape_html(field.path)}"
        name="{self.escape_html(field.path)}"
        label="{self.escape_html(field.label)}"
        {self._format_bool_attr("checked", checked)}
        {self._format_bool_attr("disabled", field.readonly)}
        {htmx}
        {extra}
    />
    {self._render_helper_text(field.description)}
    {self.render_errors(field)}
</div>"""

    def render_checkbox_group(self, field: "FormField") -> str:
        """Render a group of checkboxes."""
        options: list[dict[str, object]] = list(field.options) if field.options else []
        raw_value: object = field.value
        selected_values: list[object]
        if isinstance(raw_value, list):  # noqa: SIM108
            selected_values = raw_value  # type: ignore[assignment]
        else:
            selected_values = []

        checkboxes_html: list[str] = []
        for option in options:
            value = option.get("value", "")
            label = option.get("label", value)
            checked = value in selected_values

            checkboxes_html.append(f"""<c-checkbox
    id="{self.escape_html(field.path)}-{self.escape_html(str(value))}"
    name="{self.escape_html(field.path)}[]"
    value="{self.escape_html(str(value))}"
    label="{self.escape_html(str(label))}"
    {self._format_bool_attr("checked", checked)}
    {self._format_bool_attr("disabled", field.readonly)}
/>""")

        return f"""<div class="rvo-form-group">
    <span class="utrecht-form-label">{self.escape_html(field.label)}</span>
    {self._render_helper_text(field.description)}
    <c-layout-flow gap="md">
        {"".join(checkboxes_html)}
    </c-layout-flow>
    {self.render_errors(field)}
</div>"""

    def render_radio(self, field: "FormField") -> str:
        """Render a radio button group."""
        options: list[dict[str, object]] = list(field.options) if field.options else []
        selected_value = field.value

        radios_html: list[str] = []
        for option in options:
            value = option.get("value", "")
            label = option.get("label", value)
            checked = str(value) == str(selected_value) if selected_value else False

            radios_html.append(f"""<c-radio
    id="{self.escape_html(field.path)}-{self.escape_html(str(value))}"
    name="{self.escape_html(field.path)}"
    value="{self.escape_html(str(value))}"
    label="{self.escape_html(str(label))}"
    {self._format_bool_attr("checked", checked)}
    {self._format_bool_attr("disabled", field.readonly)}
/>""")

        return f"""<div class="rvo-form-group">
    <span class="utrecht-form-label">{self.escape_html(field.label)}</span>
    {self._render_helper_text(field.description)}
    <c-layout-flow gap="md">
        {"".join(radios_html)}
    </c-layout-flow>
    {self.render_errors(field)}
</div>"""

    def render_service_cards(self, field: "FormField") -> str:
        """Render service options as selectable cards with icons and descriptions.

        Uses c-checkbox from ROOS with additional styling for card layout.
        """
        options: list[dict[str, object]] = list(field.options) if field.options else []
        raw_value: object = field.value
        selected_values: list[object]
        if isinstance(raw_value, list):  # noqa: SIM108
            selected_values = raw_value  # type: ignore[assignment]
        else:
            selected_values = []

        # Extract selected service names (handles both string and dict formats)
        selected_service_names: set[str] = set()
        for val in selected_values:
            if isinstance(val, str):
                selected_service_names.add(val)
            elif isinstance(val, dict):
                val_dict: dict[str, object] = val  # type: ignore[assignment]
                selected_service_names.update(val_dict.keys())

        cards_html: list[str] = []
        for option in options:
            value = str(option.get("value", ""))
            label = str(option.get("label", value))
            description = str(option.get("description", ""))
            icon = str(option.get("icon", "document"))
            color = str(option.get("color", "hemelblauw"))
            checked = value in selected_service_names

            checked_class = "service-card--selected" if checked else ""

            cards_html.append(f"""
<div class="service-card {checked_class}">
    <c-checkbox
        id="{self.escape_html(field.path)}-{self.escape_html(value)}"
        name="{self.escape_html(field.path)}[]"
        value="{self.escape_html(value)}"
        {self._format_bool_attr("checked", checked)}
        {self._format_bool_attr("disabled", field.readonly)}
    />
    <label class="service-card__content" for="{self.escape_html(field.path)}-{self.escape_html(value)}">
        <div class="service-card__header">
            <c-icon icon="{self.escape_html(icon)}" color="{self.escape_html(color)}" size="lg" />
            <span class="service-card__title">{self.escape_html(label)}</span>
        </div>
        <p class="service-card__description">{self.escape_html(description)}</p>
    </label>
</div>""")

        return f"""<div class="rvo-form-group">
    <span class="utrecht-form-label">{self.escape_html(field.label)}</span>
    {self._render_helper_text(field.description)}
    <div class="service-cards-grid">
        {"".join(cards_html)}
    </div>
    {self.render_errors(field)}
</div>

<style>
.service-cards-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 1rem;
    margin-top: 0.5rem;
}}

.service-card {{
    display: flex;
    align-items: flex-start;
    gap: 0.75rem;
    padding: 1rem;
    border: 2px solid var(--rvo-color-grijs-300, #e5e5e5);
    border-radius: 8px;
    background: white;
    transition: border-color 0.2s, background-color 0.2s;
}}

.service-card:hover {{
    border-color: var(--rvo-color-hemelblauw, #007BC7);
}}

.service-card--selected {{
    border-color: var(--rvo-color-hemelblauw, #007BC7);
    background: var(--rvo-color-hemelblauw-100, #E5F0F9);
}}

.service-card__content {{
    flex: 1;
    cursor: pointer;
}}

.service-card__header {{
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin-bottom: 0.5rem;
}}

.service-card__title {{
    font-weight: 600;
    color: var(--rvo-color-zwart, #000);
}}

.service-card__description {{
    font-size: 0.875rem;
    color: var(--rvo-color-grijs-700, #666);
    margin: 0;
    line-height: 1.4;
}}
</style>"""

    def render_display_card(self, field: "FormField") -> str:
        """Render a read-only display card using ROOS c-card component.

        Used for encrypted fields, configuration status, and other
        display-only information. The field.value should already be
        a display string (via converter.view()).

        Icon and color can be customized via field.attributes:
        - attributes["icon"] — ROOS icon name (default: "sleutel")
        - attributes["icon_color"] — ROOS color (default: "blauw")
        """
        icon = field.attributes.get("icon", "sleutel")
        icon_color = field.attributes.get("icon_color", "blauw")

        value_html = ""
        if field.value:
            value_str = self.escape_html(str(field.value))
            # Detect status messages from EncryptedDisplayConverter
            if value_str in ("Versleuteld opgeslagen", "Geconfigureerd"):
                value_html = f'<c-tag type="success" size="sm">{value_str}</c-tag>'
            elif value_str == "Niet geconfigureerd":
                value_html = f'<c-tag type="warning" size="sm">{value_str}</c-tag>'
            else:
                # Plain display value (e.g., truncated key)
                value_html = f'<span class="rvo-text--sm rvo-text--subtle">{value_str}</span>'

        description_html = ""
        if field.description:
            description_html = self._render_helper_text(field.description)

        return (
            f'<c-card padding="md" outline>\n'
            f'    <c-layout-flow gap="xs">\n'
            f'        <div class="rvo-display-field__header">\n'
            f'            <c-icon icon="{self.escape_html(icon)}" '
            f'size="md" color="{self.escape_html(icon_color)}" />\n'
            f'            <span class="utrecht-form-label">'
            f"{self.escape_html(field.label)}</span>\n"
            f"        </div>\n"
            f"        {value_html}\n"
            f"        {description_html}\n"
            f"    </c-layout-flow>\n"
            f"</c-card>"
        )

    def render_nested(self, field: "FormField", children_html: list[str]) -> str:
        """Render a nested model's fields grouped together."""
        children_content = "\n".join(children_html)

        # Use a simple fieldset-like grouping with the parent field's label
        return f"""<div class="rvo-nested-fields" id="{self.escape_html(field.path)}-group">
    <span class="utrecht-form-label">{self.escape_html(field.label)}</span>
    {self._render_helper_text(field.description)}
    <c-layout-row gap="md">
        {children_content}
    </c-layout-row>
    {self.render_errors(field)}
</div>"""

    def render_number(self, field: "FormField") -> str:
        """Render a number input field."""
        htmx = self._build_htmx_attrs(field)
        extra = self._build_extra_attrs(field)

        min_val = field.attributes.get("min", "")
        max_val = field.attributes.get("max", "")
        step = field.attributes.get("step", "")

        return f"""<c-text-input-field
    id="{self.escape_html(field.path)}"
    name="{self.escape_html(field.path)}"
    label="{self.escape_html(field.label)}"
    type="number"
    {self._format_attr("helperText", field.description)}
    {self._format_attr("placeholder", field.placeholder)}
    {self._format_bool_attr("required", field.required)}
    {self._format_bool_attr("disabled", field.readonly)}
    {self._format_attr("value", self._format_value(field.value))}
    {self._format_attr("min", min_val if min_val else None)}
    {self._format_attr("max", max_val if max_val else None)}
    {self._format_attr("step", step if step else None)}
    {self._format_attr("invalid", "true" if field.errors else None)}
    {self._format_attr("errorText", field.errors[0] if field.errors else None)}
    {htmx}
    {extra}
/>"""

    def render_date(self, field: "FormField") -> str:
        """Render a date picker field."""
        htmx = self._build_htmx_attrs(field)
        extra = self._build_extra_attrs(field)

        # Determine input type based on widget_type
        input_type = "datetime-local" if field.widget_type == "datetime" else "date"

        return f"""<c-text-input-field
    id="{self.escape_html(field.path)}"
    name="{self.escape_html(field.path)}"
    label="{self.escape_html(field.label)}"
    type="{input_type}"
    {self._format_attr("helperText", field.description)}
    {self._format_bool_attr("required", field.required)}
    {self._format_bool_attr("disabled", field.readonly)}
    {self._format_attr("value", self._format_value(field.value))}
    {self._format_attr("invalid", "true" if field.errors else None)}
    {self._format_attr("errorText", field.errors[0] if field.errors else None)}
    {htmx}
    {extra}
/>"""

    def render_file(self, field: "FormField") -> str:
        """Render a file upload field."""
        htmx = self._build_htmx_attrs(field)
        extra = self._build_extra_attrs(field)

        accept = str(field.attributes.get("accept", ""))
        multiple = bool(field.attributes.get("multiple", False))

        return f"""<div class="rvo-form-group">
    <label class="utrecht-form-label" for="{self.escape_html(field.path)}">
        {self.escape_html(field.label)}
        {' <span class="rvo-required">*</span>' if field.required else ""}
    </label>
    {self._render_helper_text(field.description)}
    <input
        type="file"
        id="{self.escape_html(field.path)}"
        name="{self.escape_html(field.path)}"
        class="rvo-file-input"
        {self._format_attr("accept", accept if accept else None)}
        {self._format_bool_attr("multiple", multiple)}
        {self._format_bool_attr("required", field.required)}
        {self._format_bool_attr("disabled", field.readonly)}
        {htmx}
        {extra}
    />
    {self.render_errors(field)}
</div>"""

    def render_hidden(self, field: "FormField") -> str:
        """Render a hidden field."""
        return f"""<input
    type="hidden"
    id="{self.escape_html(field.path)}"
    name="{self.escape_html(field.path)}"
    value="{self.escape_html(self._format_value(field.value))}"
/>"""

    def render_sequence(self, field: "FormField", items_html: list[str]) -> str:
        """Render a sequence/repeatable field with add/remove buttons."""
        # Use field attributes directly, fallback to attributes dict for backwards compatibility
        min_items = field.min_items or field.attributes.get("min_items", 0)
        max_items = field.max_items or field.attributes.get("max_items")
        add_label = field.attributes.get("add_label", "Item toevoegen")

        items_content = "\n".join(items_html) if items_html else ""
        current_count = len(items_html)

        # Disable add button if max items reached
        add_disabled = max_items is not None and current_count >= max_items

        max_items_str = str(max_items) if max_items else ""
        container_id = f"{self.escape_html(field.path)}-container"
        return f"""<div class="rvo-sequence" id="{container_id}" \
data-min-items="{min_items}" data-max-items="{max_items_str}" data-current-count="{current_count}">
    <span class="utrecht-form-label">{self.escape_html(field.label)}</span>
    {self._render_helper_text(field.description)}
    <div class="rvo-sequence__items" id="{self.escape_html(field.path)}-items">
        {items_content}
    </div>
    <div class="rvo-sequence__actions">
        <c-button
            type="button"
            kind="tertiary"
            size="sm"
            showIcon="before"
            icon="plus"
            label="{self.escape_html(add_label)}"
            @click="addSequenceItem('{self.escape_html(field.path)}')"
            {self._format_bool_attr("disabled", add_disabled)}
        />
    </div>
    {self.render_errors(field)}
</div>"""

    def render_sequence_item(self, field: "FormField", index: int, item_html: str) -> str:
        """Render a single sequence item wrapper."""
        remove_label = field.attributes.get("remove_label", "Verwijderen")

        # Get min_items from field to determine if remove should be disabled
        min_items = field.min_items or field.attributes.get("min_items", 0)
        current_count = len(field.children) if field.children else 0

        # Disable remove if we're at minimum items
        remove_disabled = current_count <= min_items

        return f"""<div class="rvo-sequence__item rvo-card rvo-card--outline rvo-card--padding-md" data-index="{index}">
    <div class="rvo-sequence__item-header">
        <c-heading type="h4" textContent="Item {index + 1}" />
        <c-button
            type="button"
            kind="quaternary"
            size="sm"
            showIcon="before"
            icon="verwijderen"
            label="{self.escape_html(remove_label)}"
            @click="removeSequenceItem(this)"
            {self._format_bool_attr("disabled", remove_disabled)}
        />
    </div>
    <div class="rvo-sequence__item-content">
        <c-layout-flow gap="md">
            {item_html}
        </c-layout-flow>
    </div>
</div>"""

    # Layout rendering methods

    def render_row(self, row: "Row", children_html: list[str]) -> str:
        """Render a horizontal row using c-layout-row."""
        gap = row.gap if hasattr(row, "gap") else "md"
        css_class = row.css_class or ""
        attrs = self.render_html_attributes(row.attributes) if row.attributes else ""

        children_content = "\n".join(children_html)

        return f"""<c-layout-row gap="{self.escape_html(gap)}" class="{self.escape_html(css_class)}" {attrs}>
    {children_content}
</c-layout-row>"""

    def render_column(self, column: "Column", child_html: str) -> str:
        """Render a column within a row using c-layout-column."""
        # ROOS uses size attributes like "md-6" for responsive widths
        width = column.width if hasattr(column, "width") else 12
        size = f"md-{width}"
        css_class = column.css_class or ""
        attrs = self.render_html_attributes(column.attributes) if column.attributes else ""

        return f"""<c-layout-column size="{self.escape_html(size)}" class="{self.escape_html(css_class)}" {attrs}>
    {child_html}
</c-layout-column>"""

    def render_fieldset(self, fieldset: "Fieldset", children_html: list[str]) -> str:
        """Render a fieldset with legend using c-fieldset."""
        legend = fieldset.legend if hasattr(fieldset, "legend") else ""
        css_class = fieldset.css_class or ""
        attrs = self.render_html_attributes(fieldset.attributes) if fieldset.attributes else ""
        description = fieldset.description if hasattr(fieldset, "description") else ""

        children_content = "\n".join(children_html)

        description_html = ""
        if description:
            description_html = f'<p class="rvo-text--subtle">{self.escape_html(description)}</p>'

        return f"""<c-fieldset legend="{self.escape_html(legend)}" class="{self.escape_html(css_class)}" {attrs}>
    <c-layout-flow gap="md">
        {description_html}
        {children_content}
    </c-layout-flow>
</c-fieldset>"""

    def render_div(self, div: "Div", children_html: list[str]) -> str:
        """Render a div wrapper."""
        css_class = div.css_class or ""
        attrs = self.render_html_attributes(div.attributes) if div.attributes else ""

        children_content = "\n".join(children_html)

        return f"""<div class="{self.escape_html(css_class)}" {attrs}>
    {children_content}
</div>"""

    def render_submit(self, submit: "Submit") -> str:
        """Render a submit button using c-button."""
        css_class = submit.css_class or ""
        attrs = self.render_html_attributes(submit.attributes) if submit.attributes else ""

        icon_attr = ""
        if submit.icon:
            icon_attr = f'showIcon="{submit.icon_position}" icon="{self.escape_html(submit.icon)}"'

        return f"""<c-button
    type="submit"
    kind="{self.escape_html(submit.kind)}"
    size="{self.escape_html(submit.size)}"
    label="{self.escape_html(submit.label)}"
    {icon_attr}
    class="{self.escape_html(css_class)}"
    {attrs}
/>"""

    def render_button_group(self, button_group: "ButtonGroup", buttons_html: list[str]) -> str:
        """Render a group of buttons using c-action-group."""
        css_class = button_group.css_class or ""
        attrs = self.render_html_attributes(button_group.attributes) if button_group.attributes else ""

        buttons_content = "\n".join(buttons_html)

        align_class = f"rvo-action-group--{self.escape_html(button_group.alignment)}"
        gap_style = f"display: flex; gap: var(--rvo-space-{self.escape_html(button_group.gap)});"
        return f"""<div class="rvo-action-group {align_class} {self.escape_html(css_class)}" \
style="{gap_style}" {attrs}>
    {buttons_content}
</div>"""

    # Error and message rendering

    def render_error(self, message: str) -> str:
        """Render a validation error message."""
        return f'<span class="rvo-form-field__error-text">{self.escape_html(message)}</span>'

    def render_errors(self, field: "FormField") -> str:
        """Render all errors for a field."""
        if not field.errors:
            return ""

        errors_html = [self.render_error(error) for error in field.errors]
        return f'<div class="rvo-form-field__errors">{"".join(errors_html)}</div>'

    def render_description(self, text: str) -> str:
        """Render a field description/help text."""
        if not text:
            return ""
        return f'<span class="rvo-form-field__helper-text">{self.escape_html(text)}</span>'

    # Form wrapper

    def render_form_start(
        self,
        form_id: str,
        action: str,
        method: str = "post",
        enctype: str | None = None,
        htmx_attrs: dict[str, str] | None = None,
    ) -> str:
        """Render the opening form tag."""
        enctype_attr = f'enctype="{self.escape_html(enctype)}"' if enctype else ""
        htmx_str = ""
        if htmx_attrs:
            htmx_parts = [f'{self.escape_html(k)}="{self.escape_html(v)}"' for k, v in htmx_attrs.items()]
            htmx_str = " ".join(htmx_parts)

        form_id_esc = self.escape_html(form_id)
        method_esc = self.escape_html(method)
        action_esc = self.escape_html(action)
        return f"""<form id="{form_id_esc}" method="{method_esc}" action="{action_esc}" \
{enctype_attr} {htmx_str} autocomplete="off">
    <c-layout-flow gap="xl">"""

    def render_form_end(self) -> str:
        """Render the closing form tag."""
        return """    </c-layout-flow>
</form>"""

    # Helper methods

    def _build_common_attrs(self, field: "FormField") -> dict[str, str]:
        """Build common HTML attributes for a field."""
        attrs = {
            "id": field.path,
            "name": field.path,
        }
        if field.css_class:
            attrs["class"] = field.css_class
        return attrs

    def _build_htmx_attrs(self, field: "FormField") -> str:
        """Build HTMX attribute string for a field."""
        if not field.htmx_attrs:
            return ""
        return " ".join(f'{self.escape_html(k)}="{self.escape_html(v)}"' for k, v in field.htmx_attrs.items())

    def _build_extra_attrs(self, field: "FormField") -> str:
        """Build extra attribute string for a field."""
        # Filter out attributes that are handled separately or are internal
        handled_attrs = {
            "rows",
            "min",
            "max",
            "step",
            "accept",
            "multiple",
            "min_items",
            "max_items",
            "add_label",
            "remove_label",
            # Internal attributes - not HTML attributes
            "options_provider",
            "converter",
            "validator",
        }
        extra = {k: v for k, v in field.attributes.items() if k not in handled_attrs}
        if not extra:
            return ""
        return " ".join(f'{self.escape_html(k)}="{self.escape_html(str(v))}"' for k, v in extra.items())

    def _format_attr(self, name: str, value: str | None) -> str:
        """Format an optional attribute."""
        if value is None or value == "":
            return ""
        return f'{name}="{self.escape_html(value)}"'

    def _format_bool_attr(self, name: str, value: bool) -> str:
        """Format a boolean attribute in Vue/ROOS style."""
        if not value:
            return ""
        return f':{name}="true"'

    def _format_value(self, value: str | int | float | bool | list | None) -> str:
        """Format a field value for output."""
        if value is None:
            return ""
        if isinstance(value, list):
            # Convert lists to comma-separated strings
            return ", ".join(str(v) for v in value)
        return str(value)

    def _render_helper_text(self, text: str | None) -> str:
        """Render helper text if present."""
        if not text:
            return ""
        return f'<span class="rvo-form-field__helper-text">{self.escape_html(text)}</span>'
