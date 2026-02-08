# Sub-part A: Core Dataclasses

**Layer:** 0 (no dependencies)
**Files to create:**
- `opi/forms/editables/editable.py`
- `opi/forms/editables/part.py`
- `opi/forms/editables/flow.py`
- `tests/test_editables_core.py`

**Root directory:** `/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/operations-manager/python/`

---

## editable.py

Define three sync protocols and the main `ProjectEditable` dataclass.

**IMPORTANT:** These protocols are **synchronous** (no async/await). The existing `Converter` and `Validator` protocols in `opi/forms/field.py` are async — these are intentionally different because YAML dict operations don't need async.

```python
# opi/forms/editables/editable.py
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
```

### Field reference

| Field | Type | Purpose |
|-------|------|---------|
| `yaml_path` | `str` | Path into YAML dict (e.g., `"users[*]/email"`, `"config/age-public-key"`) |
| `widget` | `str` | Widget type: `text`, `textarea`, `select`, `checkbox`, `checkbox-group`, `radio`, `service-cards`, `number`, `sequence`, `display-card`, `nested` |
| `label` | `str` | i18n key for field label |
| `description` | `str \| None` | i18n key for help text |
| `placeholder` | `str \| None` | Placeholder text |
| `options_provider` | `str \| None` | Provider name from PROVIDER_REGISTRY |
| `converter` | `EditableConverter \| None` | Sync read/write/view converter |
| `validator` | `EditableValidator \| None` | Sync field validation |
| `enforcer` | `EditableEnforcer \| None` | Sync business rule enforcement |
| `readonly` | `bool` | Always read-only (encrypted fields) |
| `readonly_on_edit` | `bool` | Read-only when editing existing project |
| `required` | `bool` | Field is required |
| `children` | `list[ProjectEditable] \| None` | Child editables for sequence items |
| `depends_on` | `str \| None` | YAML path this field depends on for visibility |
| `show_when` | `dict[str, Any] \| None` | Conditions for showing this field |
| `htmx_trigger` | `str \| None` | HTMX hx-trigger attribute |
| `htmx_target` | `str \| None` | HTMX hx-target attribute |
| `htmx_swap` | `str \| None` | HTMX hx-swap attribute |
| `min_items` | `int` | Minimum items for sequences |
| `max_items` | `int \| None` | Maximum items for sequences |

---

## part.py

```python
# opi/forms/editables/part.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from opi.forms.editables.editable import EditableEnforcer, ProjectEditable
from opi.forms.layout import LayoutElement


@dataclass
class EditablePart:
    """Groups related editables into a logical UI section (tab or wizard step)."""

    part_id: str
    title: str
    icon: str | None = None
    description: str | None = None
    editables: list[ProjectEditable] = field(default_factory=list)
    layout: LayoutElement | None = None
    in_create_wizard: bool = True
    wizard_step: int | None = None
    is_readonly: bool = False
    summary_fn: Callable[[dict[str, Any]], str] | None = None
    enforcer: EditableEnforcer | None = None
```

**Import note:** `LayoutElement` comes from `opi/forms/layout.py` (existing file). It's the base class for `Row`, `Column`, `Fieldset`, `Sequence`, etc.

---

## flow.py

```python
# opi/forms/editables/flow.py
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from opi.forms.editables.part import EditablePart


class FlowMode(Enum):
    WIZARD = "wizard"
    TABS = "tabs"


@dataclass
class FormFlow:
    """Composes EditableParts into a wizard or tabbed interface."""

    flow_id: str
    title: str
    mode: FlowMode
    parts: list[EditablePart] = field(default_factory=list)
    show_review: bool = True
    htmx_base_url: str = ""
    save_per_part: bool = True
```

---

## Tests: test_editables_core.py

Create `tests/test_editables_core.py`:

```python
class TestProjectEditable:
    def test_minimal_instantiation(self):
        """Only required fields: yaml_path, widget, label."""
        editable = ProjectEditable(yaml_path="name", widget="text", label="Naam")
        assert editable.yaml_path == "name"
        assert editable.widget == "text"
        assert editable.label == "Naam"
        assert editable.readonly is False
        assert editable.required is False
        assert editable.children is None

    def test_all_fields_populated(self):
        """All optional fields set."""
        # Create with every field non-default, verify all attributes

    def test_children_list(self):
        """Children can hold nested ProjectEditables."""
        child = ProjectEditable(yaml_path="users[*]/email", widget="text", label="Email")
        parent = ProjectEditable(
            yaml_path="users", widget="sequence", label="Gebruikers",
            children=[child],
        )
        assert len(parent.children) == 1
        assert parent.children[0].yaml_path == "users[*]/email"


class TestEditablePart:
    def test_minimal_instantiation(self):
        """Only required fields: part_id, title."""
        part = EditablePart(part_id="identity", title="Project")
        assert part.part_id == "identity"
        assert part.editables == []
        assert part.in_create_wizard is True

    def test_with_editables(self):
        """Part with populated editables list."""

    def test_with_layout(self):
        """Part with a layout element from opi.forms.layout."""


class TestFormFlow:
    def test_wizard_mode(self):
        flow = FormFlow(flow_id="create", title="Aanmaken", mode=FlowMode.WIZARD)
        assert flow.mode == FlowMode.WIZARD
        assert flow.show_review is True

    def test_tabs_mode(self):
        flow = FormFlow(flow_id="edit", title="Bewerken", mode=FlowMode.TABS)
        assert flow.mode == FlowMode.TABS


class TestProtocols:
    def test_converter_is_runtime_checkable(self):
        """A class implementing read/write/view satisfies EditableConverter."""
        class MyConverter:
            def read(self, value): return value
            def write(self, value): return value
            def view(self, value): return str(value)

        assert isinstance(MyConverter(), EditableConverter)

    def test_validator_is_runtime_checkable(self):
        class MyValidator:
            def validate(self, value): return []

        assert isinstance(MyValidator(), EditableValidator)

    def test_enforcer_is_runtime_checkable(self):
        class MyEnforcer:
            def enforce(self, value, context): return value

        assert isinstance(MyEnforcer(), EditableEnforcer)
```

## Code Style

- Use lowercase type hints: `dict`, `list`, not `Dict`, `List`
- Use `|` for unions: `str | None`, not `Optional[str]`
- Use `from __future__ import annotations` in all files
- All docstrings explain purpose concisely
- Run `ruff check --fix && ruff format` after implementation
- Run `pyright` for type checking
