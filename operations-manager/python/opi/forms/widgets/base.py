"""The abstract base class for a widget adapter, so one form definition can render with
different component libraries.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

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


class WidgetAdapter(ABC):
    """Implement this class to add support for a new UI framework.

    Each method renders one type of form element to HTML.
    """

    @abstractmethod
    def render_text(self, field: FormField) -> str:
        """Render a text input field."""

    @abstractmethod
    def render_password(self, field: FormField) -> str:
        """Render a text input whose value is masked on screen."""

    @abstractmethod
    def render_textarea(self, field: FormField) -> str:
        """Render a textarea field."""

    @abstractmethod
    def render_select(self, field: FormField) -> str:
        """Render a select/dropdown field."""

    @abstractmethod
    def render_checkbox(self, field: FormField) -> str:
        """Render a single checkbox field."""

    @abstractmethod
    def render_checkbox_group(self, field: FormField) -> str:
        """Render a group of checkboxes."""

    @abstractmethod
    def render_radio(self, field: FormField) -> str:
        """Render a radio button group."""

    @abstractmethod
    def render_number(self, field: FormField) -> str:
        """Render a number input field."""

    @abstractmethod
    def render_date(self, field: FormField) -> str:
        """Render a date picker field."""

    @abstractmethod
    def render_file(self, field: FormField) -> str:
        """Render a file upload field."""

    @abstractmethod
    def render_hidden(self, field: FormField) -> str:
        """Render a hidden field."""

    @abstractmethod
    def render_service_cards(self, field: FormField) -> str:
        """Render service options as selectable cards with icons and descriptions."""

    @abstractmethod
    def render_display_card(self, field: FormField) -> str:
        """Render a read-only display card for status/encrypted fields."""

    @abstractmethod
    def render_key_value_editor(self, field: FormField) -> str:
        """Render a key-value editor with YAML/ENV format toggle."""

    @abstractmethod
    def render_nested(self, field: FormField, children_html: list[str]) -> str:
        """Render a nested model's fields together; *children_html* is already rendered."""

    @abstractmethod
    def render_sequence(self, field: FormField, items_html: list[str]) -> str:
        """Render a repeatable field with add and remove buttons; *items_html* is rendered."""

    @abstractmethod
    def render_sequence_item(self, field: FormField, index: int, item_html: str) -> str:
        """Render the wrapper of one sequence item; *item_html* is already rendered."""

    @abstractmethod
    def render_row(self, row: Row, children_html: list[str]) -> str:
        """Render a horizontal row; *children_html* is already rendered."""

    @abstractmethod
    def render_column(self, column: Column, child_html: str) -> str:
        """Render a column within a row; *child_html* is already rendered."""

    @abstractmethod
    def render_fieldset(self, fieldset: Fieldset, children_html: list[str]) -> str:
        """Render a fieldset with its legend; *children_html* is already rendered."""

    @abstractmethod
    def render_div(self, div: Div, children_html: list[str]) -> str:
        """Render a div wrapper; *children_html* is already rendered."""

    @abstractmethod
    def render_submit(self, submit: Submit) -> str:
        """Render a submit button."""

    @abstractmethod
    def render_button_group(self, button_group: ButtonGroup, buttons_html: list[str]) -> str:
        """Render a group of buttons; *buttons_html* is already rendered."""

    @abstractmethod
    def render_error(self, message: str) -> str:
        """Render a validation error message."""

    @abstractmethod
    def render_errors(self, field: FormField) -> str:
        """Render all errors for a field."""

    @abstractmethod
    def render_description(self, text: str) -> str:
        """Render a field description/help text."""

    @abstractmethod
    def render_form_start(
        self,
        form_id: str,
        action: str,
        method: str = "post",
        enctype: str | None = None,
        htmx_attrs: dict[str, str] | None = None,
    ) -> str:
        """Render the opening form tag."""

    @abstractmethod
    def render_form_end(self) -> str:
        """Render the closing form tag."""

    def render_field(self, field: FormField) -> str:
        """Dispatch to the render method that matches the field's widget type."""
        widget_type = field.widget_type.lower().replace("-", "_")

        if widget_type == "nested" and field.children:
            children_html = [self.render_field(child) for child in field.children]
            return self.render_nested(field, children_html)

        if widget_type == "sequence":
            items_html = []
            for i, child_field in enumerate(field.children or []):
                if child_field.children:
                    child_fields_html = [self.render_field(cf) for cf in child_field.children]
                    item_html = "\n".join(child_fields_html)
                else:
                    item_html = ""
                items_html.append(self.render_sequence_item(field, i, item_html))
            return self.render_sequence(field, items_html)

        render_methods = {
            "text": self.render_text,
            "textarea": self.render_textarea,
            "select": self.render_select,
            "checkbox": self.render_checkbox,
            "checkbox_group": self.render_checkbox_group,
            "radio": self.render_radio,
            "number": self.render_number,
            "date": self.render_date,
            "datetime": self.render_date,  # Can override for datetime-local
            "file": self.render_file,
            "hidden": self.render_hidden,
            "password": self.render_password,
            "service_cards": self.render_service_cards,
            "display_card": self.render_display_card,
            "key_value": self.render_key_value_editor,
        }

        render_method = render_methods.get(widget_type)
        if render_method:
            return render_method(field)

        # An unknown type falls back to a text field.
        return self.render_text(field)

    def escape_html(self, text: str | None) -> str:
        """Escape HTML special characters."""
        if text is None:
            return ""
        return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
