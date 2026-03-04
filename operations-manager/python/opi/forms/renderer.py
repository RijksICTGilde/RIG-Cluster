"""
Form renderer that orchestrates form generation.

The FormRenderer combines schema extraction, layout processing,
and widget adaptation to produce complete form HTML.
"""

from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from opi.forms.extractor import extract_fields_from_model
from opi.forms.field import FormField
from opi.forms.layout import (
    HTML,
    ButtonGroup,
    Column,
    Div,
    Fieldset,
    Hidden,
    LayoutElement,
    Row,
    Sequence,
    Submit,
)
from opi.forms.widgets.base import WidgetAdapter


class Translator(Protocol):
    """Protocol for translation functions."""

    def __call__(self, key: str) -> str:
        """Translate a key to the current locale."""
        ...


class OptionsProvider(Protocol):
    """Protocol for dynamic options providers."""

    def get_options(self) -> list[dict[str, Any]]:
        """Get options for a select/radio field."""
        ...


class IdentityTranslator:
    """Default translator that returns keys unchanged."""

    def __call__(self, key: str) -> str:
        return key


class FormRenderer:
    """
    Renders complete forms from schema + layout + data.

    The renderer:
    1. Extracts FormField instances from Pydantic schema
    2. Resolves dynamic options from providers
    3. Translates labels/descriptions
    4. Renders using the widget adapter and layout
    """

    def __init__(
        self,
        widget_adapter: WidgetAdapter,
        translator: Translator | None = None,
        options_providers: dict[str, OptionsProvider] | None = None,
    ) -> None:
        """
        Initialize the form renderer.

        Args:
            widget_adapter: Widget adapter for HTML rendering
            translator: Optional translator for i18n
            options_providers: Dict of provider name -> OptionsProvider
        """
        self.adapter = widget_adapter
        self.translator = translator or IdentityTranslator()
        self.providers = options_providers or {}
        self._edit_mode = False

    def render(
        self,
        schema: type[BaseModel],
        layout: LayoutElement | list[LayoutElement] | None = None,
        data: dict[str, Any] | None = None,
        errors: dict[str, list[str]] | None = None,
        form_id: str = "form",
        action: str = "",
        method: str = "post",
        enctype: str | None = None,
        htmx_attrs: dict[str, str] | None = None,
        edit_mode: bool = False,
    ) -> str:
        """
        Render a complete form to HTML.

        Args:
            schema: Pydantic model class defining the form structure
            layout: Optional layout definition (auto-generated if None)
            data: Current field values
            errors: Validation errors per field path
            form_id: HTML form ID
            action: Form action URL
            method: HTTP method
            enctype: Form enctype (e.g., 'multipart/form-data')
            htmx_attrs: Optional HTMX attributes for the form
            edit_mode: If True, fields with readonly_on_edit will be readonly

        Returns:
            Complete form HTML string
        """
        self._edit_mode = edit_mode
        data = data or {}
        errors = errors or {}

        # Extract fields from schema
        fields = extract_fields_from_model(schema, data, errors)

        # Build field lookup by name
        fields_by_name: dict[str, FormField] = {f.name: f for f in fields}

        # Resolve options for select fields
        self._resolve_options(fields)

        # Translate field labels/descriptions
        self._translate_fields(fields)

        # Apply edit mode constraints (readonly_on_edit -> readonly)
        if self._edit_mode:
            self._apply_edit_mode(fields)

        # Generate layout if not provided
        if layout is None:
            layout = self._generate_default_layout(fields)

        # Render form content using layout
        if isinstance(layout, list):
            content_parts = [self._render_layout_element(elem, fields_by_name) for elem in layout]
            content_html = "\n".join(content_parts)
        else:
            content_html = self._render_layout_element(layout, fields_by_name)

        # Wrap in form tags
        form_start = self.adapter.render_form_start(form_id, action, method, enctype, htmx_attrs)
        form_end = self.adapter.render_form_end()

        return f"{form_start}\n{content_html}\n{form_end}"

    def render_fields(
        self,
        schema: type[BaseModel],
        layout: LayoutElement | list[LayoutElement] | None = None,
        data: dict[str, Any] | None = None,
        errors: dict[str, list[str]] | None = None,
        edit_mode: bool = False,
    ) -> str:
        """
        Render form fields without the form wrapper.

        Useful for HTMX partial updates or embedding in existing forms.

        Args:
            schema: Pydantic model class defining the form structure
            layout: Optional layout definition
            data: Current field values
            errors: Validation errors per field path
            edit_mode: If True, fields with readonly_on_edit will be readonly

        Returns:
            Form fields HTML string (no <form> wrapper)
        """
        self._edit_mode = edit_mode
        data = data or {}
        errors = errors or {}

        # Extract fields from schema
        fields = extract_fields_from_model(schema, data, errors)

        # Build field lookup by name
        fields_by_name: dict[str, FormField] = {f.name: f for f in fields}

        # Resolve options for select fields
        self._resolve_options(fields)

        # Translate field labels/descriptions
        self._translate_fields(fields)

        # Apply edit mode constraints (readonly_on_edit -> readonly)
        if self._edit_mode:
            self._apply_edit_mode(fields)

        # Generate layout if not provided
        if layout is None:
            layout = self._generate_default_layout(fields)

        # Render using layout
        if isinstance(layout, list):
            content_parts = [self._render_layout_element(elem, fields_by_name) for elem in layout]
            return "\n".join(content_parts)
        else:
            return self._render_layout_element(layout, fields_by_name)

    def render_field(
        self,
        field: FormField,
    ) -> str:
        """
        Render a single field.

        Args:
            field: FormField to render

        Returns:
            Field HTML string
        """
        return self.adapter.render_field(field)

    def validate(
        self,
        schema: type[BaseModel],
        data: dict[str, Any],
    ) -> tuple[BaseModel | None, dict[str, list[str]]]:
        """
        Validate form data against the schema.

        Args:
            schema: Pydantic model class
            data: Form data to validate

        Returns:
            Tuple of (parsed_model or None, errors_dict)
        """
        try:
            parsed = schema.model_validate(data)
            return parsed, {}
        except ValidationError as e:
            return None, self._format_validation_errors(e)

    def _resolve_options(self, fields: list[FormField]) -> None:
        """Resolve dynamic options for select/radio fields."""
        for field in fields:
            # Check if field has an options_provider attribute
            provider_name = field.attributes.get("options_provider")
            if provider_name and provider_name in self.providers:
                field.options = self.providers[provider_name].get_options()

            # Recursively process children
            if field.children:
                self._resolve_options(field.children)

    def _translate_fields(self, fields: list[FormField]) -> None:
        """Translate field labels and descriptions."""
        for field in fields:
            # Translate label if it looks like an i18n key
            if field.label and "." in field.label:
                field.label = self.translator(field.label)

            # Translate description
            if field.description and "." in field.description:
                field.description = self.translator(field.description)

            # Translate errors
            field.errors = [self.translator(e) for e in field.errors]

            # Recursively translate children
            if field.children:
                self._translate_fields(field.children)

    def _apply_edit_mode(self, fields: list[FormField]) -> None:
        """Apply edit mode constraints to fields.

        Sets readonly=True for fields marked with readonly_on_edit=True.
        """
        for field in fields:
            if field.readonly_on_edit:
                field.readonly = True

            # Recursively apply to children
            if field.children:
                self._apply_edit_mode(field.children)

    def _generate_default_layout(self, fields: list[FormField]) -> list[LayoutElement | str]:
        """Generate a simple default layout from fields."""
        # Just return field names for simple vertical layout
        return [f.name for f in fields]

    def _render_layout_element(
        self,
        element: LayoutElement | str,
        fields: dict[str, FormField],
    ) -> str:
        """
        Render a layout element or field reference.

        Args:
            element: Layout element or field name string
            fields: Field lookup by name

        Returns:
            Rendered HTML
        """
        # String = field name reference
        if isinstance(element, str):
            field = fields.get(element)
            if field:
                return self.adapter.render_field(field)
            return f"<!-- Unknown field: {element} -->"

        # Row layout
        if isinstance(element, Row):
            children_html = [self._render_layout_element(child, fields) for child in element.children]
            return self.adapter.render_row(element, children_html)

        # Column layout
        if isinstance(element, Column):
            child_html = self._render_layout_element(element.child, fields)
            return self.adapter.render_column(element, child_html)

        # Fieldset layout
        if isinstance(element, Fieldset):
            children_html = [self._render_layout_element(child, fields) for child in element.children]
            return self.adapter.render_fieldset(element, children_html)

        # Div wrapper
        if isinstance(element, Div):
            children_html = [self._render_layout_element(child, fields) for child in element.children]
            return self.adapter.render_div(element, children_html)

        # Sequence (repeatable fields)
        if isinstance(element, Sequence):
            field = fields.get(element.field_name)
            if field and field.children:
                items_html = []
                for i, child_field in enumerate(field.children):
                    # Render each child's fields
                    child_fields_html = [self.adapter.render_field(cf) for cf in child_field.children]
                    item_html = "\n".join(child_fields_html)
                    items_html.append(self.adapter.render_sequence_item(field, i, item_html))
                return self.adapter.render_sequence(field, items_html)
            elif field:
                return self.adapter.render_sequence(field, [])
            return f"<!-- Unknown sequence field: {element.field_name} -->"

        # Hidden field
        if isinstance(element, Hidden):
            field = fields.get(element.field_name)
            if field:
                return self.adapter.render_hidden(field)
            return f"<!-- Unknown hidden field: {element.field_name} -->"

        # Raw HTML
        if isinstance(element, HTML):
            return element.content

        # Submit button
        if isinstance(element, Submit):
            return self.adapter.render_submit(element)

        # Button group
        if isinstance(element, ButtonGroup):
            buttons_html = [self._render_layout_element(btn, fields) for btn in element.buttons]
            return self.adapter.render_button_group(element, buttons_html)

        # Unknown element type
        return f"<!-- Unknown layout element: {type(element).__name__} -->"

    def _format_validation_errors(self, error: ValidationError) -> dict[str, list[str]]:
        """Format Pydantic validation errors to field-keyed dict."""
        errors: dict[str, list[str]] = {}

        for err in error.errors():
            # Build field path from location
            loc = err.get("loc", ())
            path = ".".join(str(part) for part in loc)

            # Get error message
            msg = err.get("msg", "Validation error")

            if path not in errors:
                errors[path] = []
            errors[path].append(msg)

        return errors
