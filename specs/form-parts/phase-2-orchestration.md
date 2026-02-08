# Phase 2: Implementation Orchestration

## Goal

Build `opi/forms/editables/` — a new Python package that maps YAML paths to form widgets via `ProjectEditable` dataclasses, grouped into `EditablePart`s, eliminating the need for Pydantic model boilerplate.

**Root directory:** `/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/operations-manager/python/`

## Architecture

```
ProjectEditable (yaml_path → widget)
    → editable_to_form_field() [bridge]
    → FormField (existing, opi/forms/field.py)
    → ROOSWidgetAdapter.render_field() (existing, opi/forms/widgets/roos.py)
    → HTML

New protocols (sync, NOT async):
  EditableConverter: read() / write() / view()
  EditableValidator: validate() → list[str]
  EditableEnforcer:  enforce(value, context) → value
```

## Sub-parts and Dependency Graph

```
LAYER 0 — No Dependencies (run in parallel)
  ├─ Sub-part A: Core Dataclasses    → impl-A-core-dataclasses.md  [DONE]
  ├─ Sub-part B: Path Utilities      → impl-B-path-utilities.md    [DONE]
  └─ Sub-part C: Providers           → impl-C-providers.md         [DONE]

LAYER 1 — Depends on Layer 0 (run in parallel after Layer 0)
  ├─ Sub-part D: Converters          → impl-D-converters.md      [DONE]
  ├─ Sub-part E: Validators          → impl-E-validators.md      (needs A)
  └─ Sub-part F: Enforcers           → impl-F-enforcers.md       (needs A)

LAYER 2 — Depends on Layer 0+1 (run in parallel)
  ├─ Sub-part G: Bridge              → impl-G-bridge.md          (needs A, B)
  └─ Sub-part H: Widget Extension    → impl-H-widget-extension.md (no internal deps)

LAYER 3 — Assembly (sequential, after all above)
  └─ Sub-part I: Package Init        → impl-I-package-init.md    (needs all)
```

## Execution Phases

### Phase A: Foundation (3 agents in parallel)

| Agent | Sub-part | Spec | Output files |
|-------|----------|------|-------------|
| 1 | **A** Core Dataclasses | `impl-A-core-dataclasses.md` | `editables/editable.py`, `editables/part.py`, `editables/flow.py`, `tests/test_editables_core.py` |
| 2 | **B** Path Utilities | `impl-B-path-utilities.md` | `editables/path.py`, `tests/test_editables_path.py` |
| 3 | **C** Providers | `impl-C-providers.md` | modify `providers.py`, `tests/test_editables_providers.py` |

### Phase B: Components (3 agents in parallel, after Phase A)

| Agent | Sub-part | Spec | Output files |
|-------|----------|------|-------------|
| 1 | **D** Converters | `impl-D-converters.md` | `editables/converters.py`, `tests/test_editables_converters.py` |
| 2 | **E+F** Validators + Enforcers | `impl-E-validators.md` + `impl-F-enforcers.md` | `editables/validators.py`, `editables/enforcers.py`, test files |
| 3 | **H** Widget Extension | `impl-H-widget-extension.md` | modify `widgets/base.py`, `widgets/roos.py`, `tests/test_editables_widget.py` |

### Phase C: Integration (1 agent, sequential, after Phase B)

| Agent | Sub-part | Spec | Output files |
|-------|----------|------|-------------|
| 1 | **G+I** Bridge + Package Init | `impl-G-bridge.md` + `impl-I-package-init.md` | `editables/bridge.py`, `editables/__init__.py`, `tests/test_editables_bridge.py`, `tests/test_editables_integration.py` |

### Phase D: Verification (1 agent, after Phase C)

```bash
cd /Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/operations-manager/python
uv run pytest tests/test_editables_*.py -v
uv run ruff check opi/forms/editables/ --fix
uv run ruff format opi/forms/editables/
uv run pyright opi/forms/editables/
```

## Files Summary

### New files (all under `operations-manager/python/`)

| File | Sub-part |
|------|----------|
| `opi/forms/editables/__init__.py` | I |
| `opi/forms/editables/editable.py` | A |
| `opi/forms/editables/part.py` | A |
| `opi/forms/editables/flow.py` | A |
| `opi/forms/editables/path.py` | B |
| `opi/forms/editables/converters.py` | D |
| `opi/forms/editables/validators.py` | E |
| `opi/forms/editables/enforcers.py` | F |
| `opi/forms/editables/bridge.py` | G |
| `tests/test_editables_core.py` | A |
| `tests/test_editables_path.py` | B |
| `tests/test_editables_providers.py` | C |
| `tests/test_editables_converters.py` | D |
| `tests/test_editables_validators.py` | E |
| `tests/test_editables_enforcers.py` | F |
| `tests/test_editables_bridge.py` | G |
| `tests/test_editables_widget.py` | H |
| `tests/test_editables_integration.py` | I |

### Modified files

| File | Sub-part | Change |
|------|----------|--------|
| `opi/forms/widgets/base.py` | H | Add abstract `render_display_card`, add to dispatch dict |
| `opi/forms/widgets/roos.py` | H | Add concrete `render_display_card` |
| `opi/forms/providers.py` | C | Add 7 new provider classes + registry entries |

### Existing files (read-only reference)

| File | Used by |
|------|---------|
| `opi/forms/field.py` | G (imports FormField) |
| `opi/forms/layout.py` | A (imports LayoutElement) |
| `opi/forms/providers.py` | G (imports PROVIDER_REGISTRY, get_provider) |
| `opi/services/services.py` | C, D (imports ServiceAdapter) |

## Verification Checklist

- [ ] `from opi.forms.editables import ProjectEditable, EditablePart, FormFlow` — no import errors
- [ ] `from opi.forms.editables import get_value, set_value, resolve_path` — no import errors
- [ ] `from opi.forms.editables import editable_to_form_field, should_render_editable` — no import errors
- [ ] All 9 test files pass: `uv run pytest tests/test_editables_*.py -v`
- [ ] `ruff check opi/forms/editables/` — no errors
- [ ] `ruff format opi/forms/editables/` — no changes needed
- [ ] `pyright opi/forms/editables/` — no errors
- [ ] Existing tests still pass: `uv run pytest tests/ -v --ignore=tests/test_editables_*.py`
- [ ] `render_field()` dispatches `display_card` widget type correctly
