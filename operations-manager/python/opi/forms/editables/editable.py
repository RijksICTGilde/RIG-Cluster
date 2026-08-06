from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum, StrEnum, auto
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EditableConverter(Protocol):
    """Sync converter for YAML <-> form <-> display values.

    All methods receive an optional ``context_data`` dict — the full
    project data (or wizard session state) surrounding the field.
    Converters that don't need context simply ignore it.
    """

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        """YAML value -> form input value."""
        ...

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        """Form submission value -> YAML storage value."""
        ...

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        """YAML value -> read-only display value."""
        ...


@runtime_checkable
class EditableValidator(Protocol):
    """Sync field-level validator."""

    def validate(self, value: Any) -> list[str]:
        """Return error messages (empty list = valid)."""
        ...


@runtime_checkable
class ContextAwareEditableValidator(Protocol):
    """Sync field-level validator that also wants the surrounding context.

    Most validators judge a value on its own and implement ``EditableValidator``.
    A few need the rest of the form or project data (e.g. to check a value against
    a sibling field) and declare a second ``context`` parameter. Both shapes are
    stored in ``Editable.validator``; ``FormProcessor`` picks the call form that
    matches the validator it holds.

    Note that ``runtime_checkable`` cannot tell the two protocols apart -- it only
    checks that a ``validate`` attribute exists, not its signature -- so this type
    is for static checking and ``cast``, not for ``isinstance``.
    """

    def validate(self, value: Any, context: dict[str, Any] | None = None) -> list[str]:
        """Return error messages (empty list = valid)."""
        ...


@runtime_checkable
class EditableEnforcer(Protocol):
    """Sync business rule enforcer."""

    def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        """Enforce business rules. Raises ValueError on violation. Returns value."""
        ...


@runtime_checkable
class AsyncEditableEnforcer(Protocol):
    """Async business rule enforcer for cross-field and I/O-bound checks.

    Used by enforcers that need database queries, external API calls, or
    access to multiple fields (e.g. DomainConfigEnforcer).

    May raise:
    - ``ValueError`` for global/section-level errors
    - ``FieldError`` for errors targeted at a specific field
    - ``FieldWarning`` for warnings targeted at a specific field
    """

    async def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        """Enforce business rules asynchronously. Returns value."""
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


@runtime_checkable
class TransientValueResolver(Protocol):
    """Resolves a transient value for a field when its value is None.

    The resolved value is used during processing (validation, enforcers,
    dependent field evaluation) but is NOT persisted to the YAML output.
    """

    def resolve(self, yaml_data: dict[str, Any]) -> Any:
        """Compute the transient value from current project data."""
        ...


class FormState(Enum):
    """States in the form submission flow.

    The flow engine walks through these states sequentially. Each state
    can have hooks registered that run at that point. Based on the TAD
    editable system's state machine pattern.

    Flow: VALIDATE → PRE_SAVE → SAVE → POST_SAVE → COMPLETED
    """

    VALIDATE = auto()
    PRE_SAVE = auto()
    SAVE = auto()
    POST_SAVE = auto()
    COMPLETED = auto()

    @classmethod
    def get_next_state(cls, state: FormState) -> FormState:
        if state.value >= cls.COMPLETED.value:
            return cls.COMPLETED
        return cls(state.value + 1)

    def is_before_save(self) -> bool:
        return self in (FormState.VALIDATE, FormState.PRE_SAVE)

    def is_save(self) -> bool:
        return self == FormState.SAVE

    def is_after_save(self) -> bool:
        return self in (FormState.POST_SAVE, FormState.COMPLETED)


@runtime_checkable
class EditableHook(Protocol):
    """Hook that runs at a specific point in the form submission flow.

    Hooks are registered on editables with a ``FormState`` key. The flow
    engine collects all hooks across editables and runs them in order.
    """

    order: int
    """Execution order within a state. Lower runs first. Default 0."""

    async def execute(self, yaml_data: dict[str, Any], context: dict[str, Any]) -> None:
        """Execute the hook, mutating yaml_data in place."""
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
    """Value used when the field has none stored yet.

    A callable is allowed and receives the surrounding project data, so a default can be
    derived (the first team member's address, a text carrying the project name) instead of
    being a constant. It is invoked only when nothing is stored, so it never overwrites a
    user's input, and returning None means "no default after all".
    """
    children: list[Editable] | None = None
    min_items: int = 0
    max_items: int | None = None
    add_remove: bool = True
    depends_on: str | None = None
    show_when: dict[str, Any] | EditableCondition | None = None
    transient: bool = False
    defers_to: str | None = None
    defer_when: EditableCondition | None = None
    remove_when_none: bool = False
    """When True, the processor removes this key from YAML output if the
    (possibly converter-produced) value is empty (None, empty string,
    False, etc.).  Use on optional fields where an empty value should
    result in the key being absent."""
    transient_value_when_none: TransientValueResolver | None = None
    """When set and the field value is None, resolves a transient value
    used during processing (validation, enforcers, dependent field
    evaluation) but NOT persisted to the YAML output."""
    virtualize: tuple[str, str] | None = None
    """When set, renames a path segment in form field HTML names to avoid
    collisions with other fields sharing the same YAML path prefix.
    Tuple of (real_segment, virtual_segment).  The server reads from the
    virtual path in submitted data and writes to the real path in YAML."""
    hooks: dict[FormState, EditableHook] | None = field(default=None, repr=False)
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
