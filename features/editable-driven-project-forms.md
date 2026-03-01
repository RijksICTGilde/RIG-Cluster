# Editable-Driven Project Forms

## What It Is

A dynamic form system for creating and editing projects. Forms are generated from declarative definitions that map directly to the project YAML structure. Adding or modifying form fields is a matter of changing a Python definition rather than rewriting HTML and JavaScript.

## Architecture

### Two-Layer Type System

The form system uses a clean separation between data logic and UI concerns:

| Layer | Type | Location | Responsibility |
|-------|------|----------|----------------|
| **Data** | `Editable` | `opi/forms/editables/editable.py` | YAML path, validator, converter, generator, enforcer, default, required, depends_on |
| **UI** | `EditableVisualizer` | `opi/forms/visualizers/visualizer.py` | Widget type, label, description, readonly, children, HTMX attrs, help text |

An `EditableVisualizer` wraps an `Editable`:

```python
# Data layer: what the field IS
DISPLAY_NAME_EDITABLE = Editable(
    yaml_path="display-name",
    validator=SlugValidator(),
    required=True,
)

# UI layer: how the field LOOKS
DISPLAY_NAME = EditableVisualizer(
    editable=DISPLAY_NAME_EDITABLE,
    widget=WidgetType.TEXT,
    label="Weergavenaam",
    description="De naam zoals die in het portaal verschijnt.",
)
```

### Current Rendering Pipeline

```
EditableVisualizer
  -> editable_to_form_field() bridge (visualizers/bridge.py)
  -> FormField (forms/field.py)
  -> ROOSWidgetAdapter renders HTML
```

The bridge resolves values from YAML, applies converter.view(), resolves options from providers, handles defaults, locked_by_service, readonly logic, and HTMX attributes. It produces a `FormField` — a flat bag of resolved values that the widget adapter can render.

### Submission Pipeline

```
JSON/form data
  -> EditableFormProcessor.process_json_submission() or parse_form_data()
  -> EditableFormProcessor.validate_editables()
  -> EditableFormProcessor.apply_to_yaml() (deep-copies, skips readonly, applies converters)
  -> EditableFormProcessor.apply_generators() (AGE keys, computed values)
```

### Key Files

| File | Purpose |
|------|---------|
| `editables/editable.py` | `Editable` dataclass + protocols (Converter, Validator, Enforcer, Generator) |
| `editables/fields/*.py` | All `Editable` constants (data definitions) |
| `editables/converters.py` | Converter implementations (EnsureList, ServiceList, KeyValue, AGE, etc.) |
| `editables/validators.py` | Validator implementations (Slug, Email, etc.) |
| `editables/processor.py` | Form submission handling (parse, validate, apply to YAML) |
| `visualizers/visualizer.py` | `EditableVisualizer` dataclass |
| `visualizers/fields/*.py` | All `EditableVisualizer` constants (UI definitions) |
| `visualizers/bridge.py` | `editable_to_form_field()` — converts EditableVisualizer to FormField |
| `visualizers/sections.py` | `FormSection` — groups editables into wizard steps |
| `visualizers/flows.py` | `FormFlow` — defines wizard flows with ordered sections |
| `visualizers/wizard_sections.py` | All section/flow definitions |
| `visualizers/project_registry.py` | `get_all_project_editables()` — flat list for edit form |
| `visualizers/providers.py` | Dynamic options providers (clusters, services, roles, etc.) |
| `forms/field.py` | `FormField` dataclass (intermediate render type) |
| `forms/renderer.py` | `FormRenderer` — orchestrates layout + widget rendering |
| `forms/widgets/roos.py` | `ROOSWidgetAdapter` — renders FormField to ROOS HTML |
| `web/router_wizard.py` | Wizard routes (create flow) |
| `web/router_project_form.py` | Edit form routes |

## How to Use It

### Adding a New Field

1. Define the `Editable` in the appropriate `editables/fields/*.py`:
```python
MY_FIELD_EDITABLE = Editable(
    yaml_path="components[*]/my-field",
    validator=SlugValidator(),
)
```

2. Define the `EditableVisualizer` in the matching `visualizers/fields/*.py`:
```python
MY_FIELD = EditableVisualizer(
    editable=MY_FIELD_EDITABLE,
    widget=WidgetType.TEXT,
    label="Mijn veld",
    description="Beschrijving van het veld.",
)
```

3. Add it to the appropriate section in `visualizers/wizard_sections.py` and/or the layout in `visualizers/project_registry.py`.

No template changes needed.

### Key Concepts

- **`default="__all__"`**: Sentinel value for checkbox_group fields — tells the widget to select all options when the YAML value is absent.
- **`depends_on` + `show_when`**: Conditional visibility — fields hidden when their dependency isn't met. Hidden fields are cleared from YAML on save.
- **`locked_by_service`**: Forces a checkbox on + readonly when the named service is active.
- **`values_provider`**: String name of an `OptionsProvider` class that provides dynamic select/checkbox options.
- **`EnsureListConverter`**: Generic converter for fields whose YAML value is always a list. Handles HTMX's single-string delivery for checkbox groups.

## Known Technical Debt: The Bridge and FormField

### The Problem

The rendering pipeline has an unnecessary intermediate type:

```
EditableVisualizer -> bridge -> FormField -> WidgetAdapter -> HTML
```

`FormField` (`forms/field.py`) is a legacy type from the original Pydantic-model-based form system. The bridge (`visualizers/bridge.py`) converts `EditableVisualizer` into `FormField` by resolving values, options, and display logic. The widget adapter (`ROOSWidgetAdapter`) then renders `FormField` to HTML.

This means:
- Value resolution, options resolution, converter application, and display logic live in the bridge — **not** on the visualizer
- `FormField` duplicates many fields that already exist on `EditableVisualizer` (label, description, readonly, widget_type, etc.)
- The widget adapter depends on `FormField` instead of the canonical type
- Adding a new field to `EditableVisualizer` requires updating `FormField` and the bridge too

### The Solution: ResolvedEditableVisualizer

Replace `FormField` and the bridge with a `ResolvedEditableVisualizer` — an `EditableVisualizer` enriched with resolved runtime data (value, options, errors, concrete path). The widget adapter would render `ResolvedEditableVisualizer` directly.

```
# Current (3 types, 2 conversions):
EditableVisualizer -> bridge -> FormField -> WidgetAdapter -> HTML

# Target (2 types, 1 resolution):
EditableVisualizer -> resolve() -> ResolvedEditableVisualizer -> WidgetAdapter -> HTML
```

This would:
- Eliminate `FormField` and the bridge entirely
- Move value/options resolution to a simple `resolve()` function
- Let the widget adapter work with the canonical type
- Reduce the number of places to update when adding fields

This refactor is not urgent — the current system works — but should be done when the widget adapter or bridge needs significant changes.

## Testing

```bash
cd operations-manager/python

# Editable/visualizer tests
uv run pytest tests/test_editables_*.py tests/test_editable_*.py -v

# Wizard tests
uv run pytest tests/forms/test_wizard_*.py -v

# ROOS component validation (ensures all sections produce valid HTML)
uv run pytest tests/forms/test_roos_component_validation.py -v
```
