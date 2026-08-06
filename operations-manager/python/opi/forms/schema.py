"""
Form schema annotations for Pydantic models.

This module provides the FormMeta class that can be used with
Python's Annotated type hints to attach form metadata to Pydantic fields.

Example:
    class ProjectForm(BaseModel):
        display_name: Annotated[str, FormMeta(
            label="project.display_name",
            description="project.display_name.help",
            widget="text"
        )] = Field(min_length=3, max_length=100)
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FormMeta:
    """
    Metadata for form fields, attached via Annotated.

    This class provides all the configuration needed to render a form field,
    including widget type, labels, validation hints, and dynamic behavior.

    Attributes:
        widget: Widget type ("text", "select", "textarea", "checkbox", "sequence", etc.)
        label: Translatable label key (e.g., "project.display_name")
        description: Help text key (e.g., "project.display_name.help")
        placeholder: Placeholder text for input fields
        options_provider: Class name for dynamic options (for select/radio widgets)
        converter: Class name for value conversion (e.g., AGE encryption)
        validator: Class name for custom validation beyond Pydantic
        depends_on: Path to dependent field (for conditional display)
        show_when: Conditional display rules as {field: value} dict
        css_class: Additional CSS classes for the field wrapper
        attributes: Extra HTML attributes as {attr: value} dict
        order: Display order within a section (lower = first)
        section: Section identifier for grouping fields
        readonly: Whether the field should be read-only (always)
        readonly_on_edit: Whether the field should be read-only when editing (not on create)
        htmx_trigger: HTMX trigger for dynamic updates (e.g., "change", "keyup")
        htmx_target: HTMX target selector for updates
        htmx_swap: HTMX swap strategy (e.g., "innerHTML", "outerHTML")
        min_items: Minimum number of items for sequence fields (default 0)
        max_items: Maximum number of items for sequence fields (None = unlimited)
    """

    widget: str | None = None
    label: str | None = None
    description: str | None = None
    placeholder: str | None = None
    options_provider: str | None = None
    converter: str | None = None
    validator: str | None = None
    depends_on: str | None = None
    show_when: dict[str, Any] | None = None
    css_class: str | None = None
    attributes: dict[str, str] | None = field(default_factory=dict)
    order: int = 0
    section: str | None = None
    readonly: bool = False
    readonly_on_edit: bool = False
    htmx_trigger: str | None = None
    htmx_target: str | None = None
    htmx_swap: str | None = None
    min_items: int = 0
    max_items: int | None = None

    def __post_init__(self) -> None:
        """Initialize default attributes dict if None."""
        if self.attributes is None:
            self.attributes = {}


def get_form_meta(field_info: Any) -> FormMeta | None:
    """
    Extract FormMeta from a Pydantic field's metadata.

    Args:
        field_info: Pydantic FieldInfo object

    Returns:
        FormMeta instance if found, None otherwise
    """
    if not hasattr(field_info, "metadata"):
        return None

    for meta in field_info.metadata:
        if isinstance(meta, FormMeta):
            return meta
    return None


def infer_widget_type(python_type: type | None, form_meta: FormMeta | None) -> str:
    """
    Infer the widget type from Python type and FormMeta.

    Args:
        python_type: The Python type of the field, or None when the model does not
            declare one (pydantic's ``FieldInfo.annotation`` is optional)
        form_meta: Optional FormMeta with explicit widget setting

    Returns:
        Widget type string
    """
    # If explicitly set, use that
    if form_meta and form_meta.widget:
        return form_meta.widget

    # Type-based inference
    type_name = getattr(python_type, "__name__", str(python_type))

    # Handle generic types (list, dict, etc.)
    origin = getattr(python_type, "__origin__", None)
    if origin is list:
        return "sequence"
    if origin is dict:
        return "mapping"

    # Simple type mapping
    type_widget_map = {
        "str": "text",
        "int": "number",
        "float": "number",
        "bool": "checkbox",
        "date": "date",
        "datetime": "datetime",
        "time": "time",
    }

    return type_widget_map.get(type_name, "text")
