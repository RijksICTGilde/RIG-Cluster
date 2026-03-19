from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
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


@runtime_checkable
class EditableCondition(Protocol):
    """Condition check for deferred field behavior."""

    def check(self, value: Any) -> bool:
        """Return True when the condition is met."""
        ...


@runtime_checkable
class EditableGenerator(Protocol):
    """Generates computed values at submit time.

    Editables with a generator are not rendered in forms - their values
    are computed from the merged YAML data during final submission.
    """

    def generate(self, yaml_data: dict[str, Any]) -> Any:
        """Compute a value from the current project data."""
        ...


class WidgetType(StrEnum):
    """Enumeration of available widget types - no magic strings."""

    TEXT = "text"
    TEXTAREA = "textarea"
    SELECT = "select"
    CHECKBOX = "checkbox"
    CHECKBOX_GROUP = "checkbox_group"
    RADIO = "radio"
    NUMBER = "number"
    DATE = "date"
    DATETIME = "datetime"
    FILE = "file"
    HIDDEN = "hidden"
    SERVICE_CARDS = "service_cards"
    DISPLAY_CARD = "display_card"
    KEY_VALUE = "key_value"
    MULTI_SELECT = "multi_select"
    SEQUENCE = "sequence"
    GROUP = "group"


@dataclass
class Editable:
    """Reusable field logic - pure data, no UI concerns.

    Defines *what* the data is: where it lives in YAML, how to validate,
    convert, enforce, and generate values. Extractable to a standalone
    package with zero UI dependencies.
    """

    yaml_path: str
    validator: EditableValidator | None = None
    converter: EditableConverter | None = None
    enforcer: EditableEnforcer | None = None
    generator: EditableGenerator | None = None
    values_provider: str | None = None
    required: bool = False
    default: Any = None
    children: list[Editable] | None = None
    min_items: int = 0
    max_items: int | None = None
    depends_on: str | None = None
    show_when: dict[str, Any] | None = None
    transient: bool = False
    defers_to: str | None = None
    defer_when: EditableCondition | None = None
    remove_when_none: bool = False
    """When True, the processor removes this key from YAML output if the
    (possibly converter-produced) value is empty (None, empty string,
    False, etc.).  Use on optional fields where an empty value should
    result in the key being absent."""
    virtualize: tuple[str, str] | None = None
    """When set, renames a path segment in form field HTML names to avoid
    collisions with other fields sharing the same YAML path prefix.
    Tuple of (real_segment, virtual_segment).  The server reads from the
    virtual path in submitted data and writes to the real path in YAML."""
    hooks: dict[str, Any] | None = field(default=None, repr=False)
    rename_targets: list[str] | None = field(default=None, repr=False)
    """Paths that reference this field's value and must be updated on rename.

    Each path may contain ``[*]`` wildcards for iteration. The propagation
    logic walks all matching items and replaces old_name → new_name:
    - Direct string fields (e.g. ``deployments[*]/components[*]/reference``)
    - List membership fields (e.g. ``components[*]/uses-components``)
    """


# ---------------------------------------------------------------------------
# Virtualize helpers
# ---------------------------------------------------------------------------

_SEGMENT_KEY_RE = re.compile(r"^([^\[\{]+)")


def apply_virtualize(path: str, virtualize: tuple[str, str] | None) -> str:
    """Replace the first matching segment name in *path* with the virtual name.

    Only the key part of a segment (before any ``[`` or ``{``) is compared and
    replaced; index/filter suffixes are preserved.
    """
    if not virtualize:
        return path
    real, virtual = virtualize
    segments = path.split("/")
    for i, seg in enumerate(segments):
        m = _SEGMENT_KEY_RE.match(seg)
        if m and m.group(1) == real:
            segments[i] = virtual + seg[m.end() :]
            break
    return "/".join(segments)


def reverse_virtualize(path: str, virtualize: tuple[str, str] | None) -> str:
    """Inverse of :func:`apply_virtualize` - virtual name back to real."""
    if not virtualize:
        return path
    real, virtual = virtualize
    return apply_virtualize(path, (virtual, real))
