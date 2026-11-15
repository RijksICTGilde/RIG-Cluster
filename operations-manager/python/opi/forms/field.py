"""The ``FormField`` dataclass: a field resolved from its schema and ready for rendering."""

from dataclasses import dataclass, field
from typing import Any, Protocol


class Converter(Protocol):
    """Protocol for value converters (e.g., AGE encryption)."""

    async def to_form(self, value: Any) -> Any: ...  # noqa: ANN401  # a project-file value

    async def from_form(self, value: Any) -> Any: ...  # noqa: ANN401  # a project-file value


class Validator(Protocol):
    """Protocol for custom validators beyond Pydantic."""

    async def validate(self, value: Any, context: dict[str, Any]) -> list[str]:  # noqa: ANN401  # a project-file value
        """The error messages for *value*, empty when it is valid."""
        ...


@dataclass
class FormField:
    """Everything a widget adapter needs to render one field, children included."""

    name: str
    path: str
    #: None when the source model declares no annotation for the field.
    schema_type: type | None
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
        """Whether this field or any of its children has an error."""
        if self.errors:
            return True
        return any(child.has_errors() for child in self.children)

    def get_error_messages(self) -> list[str]:
        """Every error message, this field's and its children's."""
        all_errors = list(self.errors)
        for child in self.children:
            all_errors.extend(child.get_error_messages())
        return all_errors
