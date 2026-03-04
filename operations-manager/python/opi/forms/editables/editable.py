from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EditableConverter(Protocol):
    """Sync converter for YAML <-> form <-> display values."""

    def read(self, value: Any) -> Any:
        """YAML value -> form input value."""
        ...

    def write(self, value: Any) -> Any:
        """Form submission value -> YAML storage value."""
        ...

    def view(self, value: Any) -> Any:
        """YAML value -> read-only display value."""
        ...


@runtime_checkable
class EditableValidator(Protocol):
    """Sync field-level validator."""

    def validate(self, value: Any) -> list[str]:
        """Return error messages (empty list = valid)."""
        ...


@runtime_checkable
class EditableEnforcer(Protocol):
    """Sync business rule enforcer."""

    def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        """Enforce business rules. Raises ValueError on violation. Returns value."""
        ...


@dataclass
class ProjectEditable:
    """
    Declarative mapping from a YAML path to a form widget.

    Replaces per-field Pydantic model + FormMeta annotations.
    The YAML dict IS the schema — no model boilerplate needed.
    """

    yaml_path: str
    widget: str
    label: str
    description: str | None = None
    placeholder: str | None = None
    options_provider: str | None = None
    converter: EditableConverter | None = None
    validator: EditableValidator | None = None
    enforcer: EditableEnforcer | None = None
    readonly: bool = False
    readonly_on_edit: bool = False
    required: bool = False
    children: list[ProjectEditable] | None = None
    depends_on: str | None = None
    show_when: dict[str, Any] | None = None
    htmx_trigger: str | None = None
    htmx_target: str | None = None
    htmx_swap: str | None = None
    min_items: int = 0
    max_items: int | None = None
