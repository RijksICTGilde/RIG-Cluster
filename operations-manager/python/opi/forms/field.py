"""
Resolved form field representation.

This module provides the FormField dataclass which represents a fully
resolved field ready for rendering. It bridges the gap between the
schema definition (Pydantic + FormMeta) and the widget adapter.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol


class Converter(Protocol):
    """Protocol for value converters (e.g., AGE encryption)."""

    async def to_form(self, value: Any) -> Any:
        """Convert value for form display."""
        ...

    async def from_form(self, value: Any) -> Any:
        """Convert form value back to storage format."""
        ...


class Validator(Protocol):
    """Protocol for custom validators beyond Pydantic."""

    async def validate(self, value: Any, context: dict[str, Any]) -> list[str]:
        """
        Validate value and return list of error messages.

        Returns empty list if valid.
        """
        ...


@dataclass
class FormField:
    """
    Resolved field ready for rendering.

    This class contains all the information a widget adapter needs
    to render a form field, including metadata, current value,
    validation errors, and nested children for complex fields.

    Attributes:
        name: Field name (e.g., "display_name")
        path: Full path for nested fields (e.g., "components[0].name")
        schema_type: Python type of the field
        widget_type: Widget type string ("text", "select", etc.)
        label: Translated label text
        description: Translated help text
        placeholder: Placeholder text
        required: Whether the field is required
        value: Current field value
        options: List of options for select/radio widgets
        errors: List of validation error messages
        children: Nested fields for sequences/mappings
        converter: Optional value converter
        validator: Optional custom validator
        css_class: CSS classes for field wrapper
        attributes: Extra HTML attributes
        readonly: Whether field is read-only (always)
        readonly_on_edit: Whether field is read-only when editing
        min_items: Minimum items for sequence fields
        max_items: Maximum items for sequence fields
        htmx_attrs: HTMX attributes for dynamic behavior
    """

    name: str
    path: str
    schema_type: type
    widget_type: str
    label: str
    required: bool = False
    description: str | None = None
    placeholder: str | None = None
    value: Any = None
    default: Any = None
    options: list[dict[str, Any]] | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    children: list[FormField] = field(default_factory=list)
    converter: Converter | None = None
    validator: Validator | None = None
    css_class: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)
    readonly: bool = False
    readonly_on_edit: bool = False
    min_items: int = 0
    max_items: int | None = None
    add_remove: bool = True
    htmx_attrs: dict[str, str] = field(default_factory=dict)
    help_text: str | None = None
    edit_mode: bool = False
    help_template: str | None = None
    examples: list[str] | None = None
    virtualize: tuple[str, str] | None = None

    def has_errors(self) -> bool:
        """Check if this field or any children have errors."""
        if self.errors:
            return True
        return any(child.has_errors() for child in self.children)

    def get_error_messages(self) -> list[str]:
        """Get all error messages including from children."""
        all_errors = list(self.errors)
        for child in self.children:
            all_errors.extend(child.get_error_messages())
        return all_errors

    def get_htmx_attributes(self) -> str:
        """Generate HTMX attribute string for HTML rendering."""
        if not self.htmx_attrs:
            return ""
        return " ".join(f'{k}="{v}"' for k, v in self.htmx_attrs.items())

    def get_html_attributes(self) -> str:
        """Generate HTML attribute string for additional attributes."""
        if not self.attributes:
            return ""
        return " ".join(f'{k}="{v}"' for k, v in self.attributes.items())
