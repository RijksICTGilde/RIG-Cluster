# 00 - Editable-Driven Form Architecture

## Problem Statement

The current dynamic forms system has a powerful rendering engine (`opi/forms/`) but the proposed model-centric approach requires a custom Pydantic model, layout, `extract_data()`, and `merge_data()` for every form section. This creates significant boilerplate that doesn't scale. Each new section means:

1. A Pydantic model with `FormMeta` annotations duplicating the YAML structure
2. Custom `extract_data()` to pull fields from YAML into model format
3. Custom `merge_data()` to push validated data back into YAML
4. Per-model layout definitions
5. Alias mappings to handle YAML's kebab-case vs Python's snake_case

**The project YAML dict IS the schema.** We shouldn't be creating parallel Pydantic models for something that's already well-defined.

## Solution: Editable-Driven Forms

Inspired by TAD's editables pattern (`/Users/robbertuittenbroek/IdeaProjects/tad/amt/api/editable*.py`), each field is declared as a `ProjectEditable` that maps a YAML path directly to a rendering configuration. The YAML dict is the resource — no intermediate models needed.

### Key insight

Instead of:
```
YAML → extract_data() → Pydantic Model → FormMeta → FormField → render
```

We do:
```
YAML → get_value(path) → ProjectEditable → FormField → render
```

And for saving:
```
form_data → validator.validate() → converter.write() → set_value(path) → YAML
```

## Core Dataclass: `ProjectEditable`

```python
# opi/forms/editables/editable.py

@dataclass
class ProjectEditable:
    """
    Declarative field definition that maps a YAML path to a form widget.

    This replaces Pydantic model fields + FormMeta annotations.
    The YAML dict is the schema — each editable knows how to read from
    and write to a specific path in the project YAML.
    """
    yaml_path: str                              # e.g., "display-name", "users[*]/email"
    widget: str                                 # "text", "select", "textarea", "checkbox", etc.
    label: str                                  # i18n key
    description: str | None = None              # i18n key for help text
    placeholder: str | None = None
    options_provider: str | None = None         # Reuses existing PROVIDER_REGISTRY
    converter: EditableConverter | None = None  # Read/write/view conversion
    validator: EditableValidator | None = None  # Input validation
    enforcer: EditableEnforcer | None = None    # Business rule enforcement
    readonly: bool = False                      # Always read-only (encrypted fields)
    readonly_on_edit: bool = False              # Read-only when editing existing project
    required: bool = False
    children: list["ProjectEditable"] | None = None  # For sequence items / nested groups
    depends_on: str | None = None               # Conditional visibility
    show_when: dict[str, Any] | None = None     # Conditional display rules
    htmx_trigger: str | None = None             # HTMX dynamic behavior
    htmx_target: str | None = None
    htmx_swap: str | None = None
    min_items: int = 0                          # For sequences
    max_items: int | None = None
```

### Path conventions

Paths reference YAML dict keys (with `-` hyphens), not Python attributes. `[*]` denotes sequence items.

| YAML path | What it maps to |
|-----------|----------------|
| `name` | `project_data["name"]` |
| `display-name` | `project_data["display-name"]` |
| `clusters` | `project_data["clusters"]` (list) |
| `users[*]/email` | `project_data["users"][i]["email"]` |
| `components[*]/ports/inbound` | `project_data["components"][i]["ports"]["inbound"]` |
| `config/age-public-key` | `project_data["config"]["age-public-key"]` |
| `deployments[*]/components[*]/image` | Nested sequence within sequence |

## Path Utilities

```python
# opi/forms/editables/path.py

def get_value(data: dict, yaml_path: str) -> Any:
    """
    Extract value from a YAML dict at the given path.

    Handles nested dicts (separated by '/'), sequences ('[*]' or '[n]'),
    and hyphenated keys.

    Examples:
        get_value(data, "name")                    → data["name"]
        get_value(data, "display-name")            → data["display-name"]
        get_value(data, "config/age-public-key")   → data["config"]["age-public-key"]
        get_value(data, "users[0]/email")          → data["users"][0]["email"]
        get_value(data, "users[*]/email")           → [u["email"] for u in data["users"]]
    """

def set_value(data: dict, yaml_path: str, value: Any) -> dict:
    """
    Set a value in a YAML dict at the given path, returning updated dict.

    Creates intermediate dicts/lists as needed. For '[*]' paths, expects
    the value to be a list matching the sequence length.
    """

def resolve_path(yaml_path: str, index: int | None = None) -> str:
    """
    Replace first [*] with [index] for concrete sequence item access.

    resolve_path("users[*]/email", 2) → "users[2]/email"
    """
```

## Converter/Validator/Enforcer Protocols

```python
# opi/forms/editables/editable.py

class EditableConverter(Protocol):
    """Bidirectional conversion between YAML storage and form display."""
    def read(self, value: Any) -> Any:
        """Convert from YAML storage to form value."""
        ...
    def write(self, value: Any) -> Any:
        """Convert from form value to YAML storage."""
        ...
    def view(self, value: Any) -> Any:
        """Convert for read-only display (may differ from read)."""
        ...

class EditableValidator(Protocol):
    """Validates a single field value."""
    def validate(self, value: Any) -> list[str]:
        """Return error messages, empty list if valid."""
        ...

class EditableEnforcer(Protocol):
    """Enforces business rules that may modify values."""
    def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        """Apply business rule, return (possibly modified) value."""
        ...
```

Note: These are synchronous by default. The existing async `Converter`/`Validator` protocols in `field.py` and `hooks.py` are used by the `FormRenderer` pipeline. The editable-level protocols are simpler — they operate on single values, not form state.

## Grouping: `EditablePart`

Groups editables into logical UI sections (tabs/wizard steps):

```python
# opi/forms/editables/part.py

@dataclass
class EditablePart:
    """
    Groups related editables into a logical UI section.

    Replaces per-part FormPart implementations + Pydantic models.
    """
    part_id: str                                # URL-safe identifier
    title: str                                  # Dutch display title
    icon: str | None = None                     # ROOS icon name
    description: str | None = None
    editables: list[ProjectEditable]            # Fields in this part
    layout: LayoutElement | None = None         # Reuses existing layout system
    in_create_wizard: bool = True               # Show in create wizard?
    wizard_step: int | None = None              # Step order in wizard
    is_readonly: bool = False                   # Entire part is display-only?
    summary_fn: Callable[[dict], str] | None = None  # For tab labels
    enforcer: EditableEnforcer | None = None    # Part-level business rules
```

## Composition: `FormFlow`

Composes parts into wizard or tabs:

```python
# opi/forms/editables/flow.py

class FlowMode(Enum):
    WIZARD = "wizard"      # Multi-step with progress tracker
    TABS = "tabs"          # Tabbed sections for editing

@dataclass
class FormFlow:
    flow_id: str
    title: str
    mode: FlowMode
    parts: list[EditablePart]
    show_review: bool = True         # Wizard: show review step at end
    htmx_base_url: str = ""          # Tabs: base URL for HTMX loading
    save_per_part: bool = True       # Tabs: each part saves independently
```

Two standard flows:
- **Create flow** (`FlowMode.WIZARD`): Identity → Services → Team → Components → Review
- **Edit flow** (`FlowMode.TABS`): All parts as HTMX-loaded tab panels

## Data Flow

### Render (GET)

```
1. Load project YAML dict from file
2. Find EditablePart by part_id
3. For each editable in part:
   a. get_value(yaml_dict, editable.yaml_path) → current value
   b. If converter: converter.view(value) → display value
   c. If options_provider: resolve options via PROVIDER_REGISTRY
   d. editable_to_form_field(editable, yaml_dict, errors) → FormField
4. Render using existing FormRenderer.render_fields() + layout + ROOSWidgetAdapter
5. Wrap in tab_panel.html.j2 or wizard_step.html.j2
```

### Save (POST)

```
1. Parse form data from request
2. Find EditablePart by part_id
3. For each editable in part (non-readonly):
   a. Extract new value from form data (keyed by yaml_path)
   b. If validator: validator.validate(new_value) — collect errors
   c. If part.enforcer: enforcer.enforce(values, context) — collect errors
   d. If converter: converter.write(new_value) → storage value
   e. set_value(yaml_dict, editable.yaml_path, storage_value)
4. Write updated YAML, commit to git
5. Return success HTML or error HTML
```

### Bridge: `ProjectEditable` → `FormField`

```python
# opi/forms/editables/bridge.py

def editable_to_form_field(
    editable: ProjectEditable,
    yaml_data: dict,
    errors: dict[str, list[str]] | None = None,
    index: int | None = None,
) -> FormField:
    """
    Convert a ProjectEditable + YAML data into a FormField for rendering.

    This bridges the editable system into the existing rendering pipeline.
    The FormRenderer + ROOSWidgetAdapter + Layout system are reused unchanged.
    """
    path = resolve_path(editable.yaml_path, index) if index is not None else editable.yaml_path
    value = get_value(yaml_data, path)

    if editable.converter:
        display = editable.converter.view(value)
    else:
        display = value

    return FormField(
        name=editable.yaml_path,
        path=path,
        schema_type=type(value) if value is not None else str,
        widget_type=editable.widget,
        label=editable.label,
        description=editable.description,
        placeholder=editable.placeholder,
        value=display,
        options=[],  # Resolved later by provider
        errors=errors.get(path, []) if errors else [],
        readonly=editable.readonly,
        readonly_on_edit=editable.readonly_on_edit,
        required=editable.required,
        min_items=editable.min_items,
        max_items=editable.max_items,
        attributes={
            "options_provider": editable.options_provider,
        } if editable.options_provider else {},
        htmx_attrs={
            k: v for k, v in {
                "hx-trigger": editable.htmx_trigger,
                "hx-target": editable.htmx_target,
                "hx-swap": editable.htmx_swap,
            }.items() if v is not None
        },
    )
```

## HTMX URL Structure

```
# Create wizard
GET  /projects/new                              -> Wizard step 1 (full page)
GET  /projects/new/step/{part_id}               -> Load wizard step (HTMX fragment)
POST /projects/new/step/{part_id}               -> Validate & advance step

# Edit tabs
GET  /projects/{name}/edit                      -> Full page with tab navigation
GET  /projects/{name}/parts/{part_id}           -> Load tab content (HTMX fragment)
POST /projects/{name}/parts/{part_id}           -> Save tab content (HTMX fragment)

# Service config sub-forms
GET  /projects/parts/services/config/{service}  -> Service config sub-form (HTMX fragment)
```

## Reuse Strategy

### Keep unchanged
- `FormField` (field.py) — rendering unit, used via bridge
- Layout elements (layout.py) — Row, Column, Fieldset, Sequence, Div, HTML, etc.
- `FormRenderer.render_fields()` — renders part content without form wrapper
- `FormRenderer._render_layout_element()` — layout traversal
- All existing providers (providers.py) — `PROVIDER_REGISTRY` reused by name
- All existing converters (converters.py) — `CONVERTER_REGISTRY` available
- i18n system (i18n.py)
- Hooks system (hooks.py) — `FormProcessor`, `FormState` for part-level processing
- `ROOSWidgetAdapter` — renders `FormField` to ROOS HTML

### Extend
- `ROOSWidgetAdapter` — add `display-card` widget type for read-only encrypted fields
- `providers.py` — add `StorageSizeOptionsProvider`, `KeycloakTemplateOptionsProvider`
- `converters.py` — new editable-level converters in `opi/forms/editables/converters.py`
- `i18n.py` — extend translations dict with new field keys

### Replace
- `ProjectFormModel` (Pydantic) → `ProjectEditables` registry (declarative)
- `ProjectFileModel` (Pydantic) → same registry, filtered by `in_create_wizard`
- Per-model `FormMeta` annotations → `ProjectEditable` definitions
- `get_project_form_layout()` → `EditablePart.layout` per part
- `get_project_file_form_layout()` → `EditablePart.layout` per part
- Per-part `extract_data()` / `merge_data()` → generic `get_value()` / `set_value()` path utilities
- Per-part Pydantic model validation → `EditableValidator` per editable + `EditableEnforcer` per part
- `FormMeta` (schema.py) — no longer the primary field definition source; `ProjectEditable` replaces it

## Directory Structure

```
opi/forms/editables/
    __init__.py
    editable.py              # ProjectEditable dataclass + protocols
    part.py                  # EditablePart dataclass
    registry.py              # ProjectEditables + ProjectParts static definitions
    path.py                  # get_value(), set_value(), resolve_path() utilities
    bridge.py                # editable_to_form_field() conversion
    converters.py            # ServiceListConverter, EncryptedDisplayConverter, TruncateConverter
    validators.py            # SlugValidator, EmailValidator, MinMaxLengthValidator
    enforcers.py             # AdminRequiredEnforcer, UniqueNameEnforcer, etc.
    flow.py                  # FormFlow, FlowMode

opi/templates/parts/
    wizard_base.html.j2
    wizard_step.html.j2
    wizard_review.html.j2
    tabs_base.html.j2
    tab_panel.html.j2
    readonly_card.html.j2
    service_config.html.j2

opi/web/
    router_form_parts.py     # FastAPI routes for form parts
```

## Comparison: Model-Centric vs Editable-Centric

| Aspect | Model-Centric (old) | Editable-Centric (new) |
|--------|---------------------|----------------------|
| Field definition | Pydantic field + `FormMeta` annotation | `ProjectEditable` dataclass |
| Schema source | Pydantic model | YAML dict (the resource IS the schema) |
| Data extraction | Custom `extract_data()` per part | Generic `get_value(data, path)` |
| Data merge | Custom `merge_data()` per part | Generic `set_value(data, path, value)` |
| Validation | Pydantic type + model_validator | `EditableValidator` per field + `EditableEnforcer` per part |
| Value conversion | `Converter` protocol (async) | `EditableConverter` protocol (sync, simpler) |
| Layout | Per-part function returning `LayoutElement` | `EditablePart.layout` attribute |
| Adding a new section | New model + implementation + extract + merge | New editables + part definition |
| Boilerplate per section | ~100-200 lines | ~20-40 lines |

## Incremental Build Order

| Phase | Deliverable | What it proves |
|-------|------------|---------------|
| **1** | Rewrite all 9 specs with editable pattern | Architecture documented |
| **2** | `ProjectEditable` + `EditablePart` + path utilities + bridge | Core framework |
| **3** | Identity part (4 editables) working in tab + wizard | End-to-end: editable → FormField → render → save → YAML |
| **4** | Users part with sequence + AdminRequiredEnforcer | Sequences work with editables |
| **5** | Services part with ServiceAdapter + ServiceListConverter + HTMX config sub-forms | Most complex piece |
| **6** | Components part with cross-part filtering | Nested editables + dependencies |
| **7** | Full wizard + tabs flows | Composition works |
| **8** | Read-only parts: config, source-code, deployments | Display-card widget, encrypted fields |
| **9** | Polish: feedback, confirmations, translations | Production readiness |

## Validation Strategy

### Per-editable validation
Each editable validates independently using its `EditableValidator`:
1. `SlugValidator` — pattern `^[a-z][a-z0-9-]*$`
2. `EmailValidator` — valid email format
3. `MinMaxLengthValidator` — string length constraints
4. `RequiredValidator` — non-empty check (also via `editable.required`)
5. Errors displayed inline below fields + summary alert at top

### Per-part validation
Part-level `EditableEnforcer` handles cross-field business rules:
- `AdminRequiredEnforcer` — at least one admin user
- `UniqueNamesEnforcer` — no duplicate component/deployment names
- `ServiceDependencyEnforcer` — uses-services matches project services

### Cross-part validation
Some rules span parts (e.g., component `uses-services` must match project services). Handled at `FormFlow` level during create wizard review step or on edit save.

## Field Dependencies and Conditional Visibility

Fields don't exist in isolation. Many fields only make sense when another field has a certain value, or their options are filtered by another field's selection. The editable system handles this at three levels.

### Level 1: Static conditional visibility (`depends_on` + `show_when`)

A field that should only appear when another field has a specific value. Evaluated at **render time** — the bridge function checks the dependency and skips the field if the condition is not met.

```python
# Example: "path" field only shown when component type is "frontend" or "single"
COMPONENT_PATH = ProjectEditable(
    yaml_path="components[*]/path",
    widget="text",
    label="component.path",
    depends_on="components[*]/type",
    show_when={"type": ["single", "frontend"]},
)

# Example: keycloak config only shown when keycloak is in services list
KEYCLOAK_TEMPLATE = ProjectEditable(
    yaml_path="services/keycloak/config/template",
    widget="select",
    label="service.keycloak.template",
    depends_on="services",
    show_when={"contains": "keycloak"},
)
```

**Implementation in bridge.py:**

```python
def should_render_editable(
    editable: ProjectEditable,
    yaml_data: dict,
    index: int | None = None,
) -> bool:
    """Check if an editable should be rendered based on its dependencies."""
    if not editable.depends_on:
        return True

    dep_path = resolve_path(editable.depends_on, index) if index is not None else editable.depends_on
    dep_value = get_value(yaml_data, dep_path)

    if editable.show_when is None:
        # Just check existence
        return dep_value is not None and dep_value != "" and dep_value != []

    # Check specific conditions
    for condition_key, condition_value in editable.show_when.items():
        if condition_key == "contains":
            # Check if dep_value (list) contains the specified item
            if isinstance(dep_value, list):
                if isinstance(condition_value, list):
                    return any(cv in dep_value for cv in condition_value)
                return condition_value in dep_value
            return False
        elif isinstance(condition_value, list):
            # dep_value must be one of the listed values
            return dep_value in condition_value
        else:
            # Exact match
            return dep_value == condition_value

    return True
```

### Level 2: HTMX-driven dynamic visibility

Fields that need to show/hide **without a full page reload** when the user changes a controlling field. Uses HTMX to load/clear sub-sections.

```python
# The controlling field triggers an HTMX request on change
SERVICES = ProjectEditable(
    yaml_path="services",
    widget="service-cards",
    label="project.services",
    htmx_trigger="change",    # When any service card checkbox changes
    htmx_target="#service-configs-container",
    htmx_swap="innerHTML",
)

# When keycloak checkbox is checked:
#   hx-get="/projects/parts/services/config/keycloak"
#   hx-target="#service-config-keycloak"
# When unchecked:
#   hx-get="/projects/parts/services/config/empty"
#   hx-target="#service-config-keycloak"
```

This pattern is used for:
- **Service config sub-forms**: selecting a configurable service (keycloak, postgresql) dynamically loads its config form
- **Component type changes**: changing component type could show/hide type-specific fields
- **Deployment additions**: adding a new deployment could load its template

### Level 3: Cross-part option filtering (context injection)

A field's **options** are filtered based on data from another part. This happens at render time by passing context to the options provider.

```python
# Component's uses-services checkbox group should only show
# services that are enabled at the project level
COMPONENT_USES_SERVICES = ProjectEditable(
    yaml_path="components[*]/uses-services",
    widget="checkbox-group",
    label="component.uses_services",
    options_provider="FilteredServiceOptionsProvider",
    # The provider receives project-level services as context
)
```

**How context flows:**

```
In edit mode:
  1. Load full project YAML
  2. Extract project-level services: get_value(yaml, "services") → ["publish-on-web", "keycloak", ...]
  3. Pass as context to the component part renderer
  4. FilteredServiceOptionsProvider.get_options(context={"project_services": [...]})
  5. Only returns options matching project-level services

In create wizard:
  1. Services part data stored in session from step 2
  2. Components part (step 4) reads session["services"] for context
  3. Same filtering applies
```

### Complete Dependency Map

| Field | Depends On | Type | Effect |
|-------|-----------|------|--------|
| `components[*]/uses-services` | `services` | Cross-part option filter | Only shows project-level services as options |
| `components[*]/uses-components` | Other `components[*]/name` | Self-reference filter | Shows other component names as options |
| `components[*]/publish-on-web` | `services` contains `publish-on-web` | Static visibility | Only shown if publish-on-web service is enabled |
| `components[*]/sso-rijk` | `services` contains `keycloak` | Static visibility | Only shown if keycloak service is enabled |
| `components[*]/path` | `components[*]/type` | Static visibility | Only shown for single/frontend types |
| Service config sub-forms | `services` | HTMX dynamic | Loaded when service checkbox is checked |
| `deployments[*]/components[*]/reference` | `components[*]/name` | Cross-part option filter | Shows component names as options |
| `deployments[*]/repository` | `repositories[*]/name` | Cross-part option filter | Shows repository names as options |

---

## Widget Rendering Catalog

Each `ProjectEditable.widget` string maps to a specific rendering method in `ROOSWidgetAdapter`. This catalog defines exactly how each widget type renders to ROOS HTML.

### `text` — Single-line text input

**ROOS component:** `<c-text-input-field />`
**Used for:** name, display-name, email, path, image, subdomain, etc.

```html
<c-text-input-field
    id="{yaml_path}"
    name="{yaml_path}"
    label="{label}"
    helperText="{description}"
    placeholder="{placeholder}"
    :required="true"
    :disabled="{readonly}"
    value="{current_value}"
    invalid="{has_errors}"
    errorText="{first_error}"
    {htmx_attrs}
/>
```

### `textarea` — Multi-line text input

**ROOS component:** `<c-textarea-field />`
**Used for:** description, aliases (KEY=VALUE format), redirect URIs

```html
<c-textarea-field
    id="{yaml_path}"
    name="{yaml_path}"
    label="{label}"
    helperText="{description}"
    placeholder="{placeholder}"
    rows="4"
    :required="true"
    :disabled="{readonly}"
    invalid="{has_errors}"
    errorText="{first_error}"
>{current_value}</c-textarea-field>
```

### `select` — Dropdown selector

**ROOS component:** `<c-select-field />`
**Used for:** component type, cluster, resource limits, storage size, keycloak template, image pull policy

```html
<c-select-field
    id="{yaml_path}"
    name="{yaml_path}"
    label="{label}"
    helperText="{description}"
    :required="true"
    :disabled="{readonly}"
    :options="{options_json}"
    value="{current_value}"
    invalid="{has_errors}"
    errorText="{first_error}"
/>
```

Options are resolved from `PROVIDER_REGISTRY` using the editable's `options_provider` name.

### `checkbox` — Single boolean toggle

**ROOS component:** `<c-checkbox />`
**Used for:** publish-on-web flag, sso-rijk flag

```html
<div class="rvo-form-group">
    <c-checkbox
        id="{yaml_path}"
        name="{yaml_path}"
        label="{label}"
        :checked="{current_value}"
        :disabled="{readonly}"
    />
    <span class="rvo-form-field__helper-text">{description}</span>
</div>
```

### `checkbox-group` — Multiple selection from options

**ROOS component:** Multiple `<c-checkbox />` in `<c-layout-flow />`
**Used for:** clusters, uses-services, uses-components

```html
<div class="rvo-form-group">
    <span class="utrecht-form-label">{label}</span>
    <span class="rvo-form-field__helper-text">{description}</span>
    <c-layout-flow gap="md">
        <!-- One checkbox per option -->
        <c-checkbox
            id="{yaml_path}-{option_value}"
            name="{yaml_path}[]"
            value="{option_value}"
            label="{option_label}"
            :checked="{is_selected}"
        />
        <!-- ... more options ... -->
    </c-layout-flow>
</div>
```

### `service-cards` — Selectable cards with icons

**ROOS component:** Custom grid of `<c-checkbox />` with card styling
**Used for:** services selection (the main services field)

Each service from `ServiceAdapter.SERVICE_DEFINITIONS` renders as a card with:
- Checkbox toggle
- Icon (from `ServiceDefinition.icon`)
- Color accent (from `ServiceDefinition.color`)
- Name and description
- HTMX trigger for configurable services (loads config sub-form on check)

```html
<div class="service-cards-grid">
    <div class="service-card {service-card--selected if checked}">
        <c-checkbox name="services[]" value="{service_name}" :checked="{is_selected}" />
        <label class="service-card__content">
            <div class="service-card__header">
                <c-icon icon="{icon}" color="{color}" size="lg" />
                <span class="service-card__title">{display_name}</span>
            </div>
            <p class="service-card__description">{description}</p>
        </label>
    </div>
</div>
<!-- Config sub-form container for each configurable service -->
<div id="service-config-{service_name}">
    <!-- Loaded via HTMX when checkbox is checked -->
</div>
```

### `number` — Numeric input

**ROOS component:** `<c-text-input-field type="number" />`
**Used for:** instances count

```html
<c-text-input-field type="number" ... min="{min}" max="{max}" step="{step}" />
```

### `sequence` — Repeatable group with add/remove

**ROOS component:** Custom container with `<c-button />` for add/remove
**Used for:** users list, components list, storage volumes, deployments, deployment components

A sequence editable has `children` that define the fields within each item. Each item renders as a card with a remove button.

```html
<div class="rvo-sequence" data-min-items="{min}" data-max-items="{max}">
    <span class="utrecht-form-label">{label}</span>
    <div class="rvo-sequence__items">
        <!-- Per item: -->
        <div class="rvo-sequence__item rvo-card rvo-card--outline rvo-card--padding-md">
            <div class="rvo-sequence__item-header">
                <c-heading type="h4" textContent="Item {index + 1}" />
                <c-button kind="quaternary" size="sm" icon="verwijderen" label="Verwijderen"
                    @click="removeSequenceItem(this)" />
            </div>
            <div class="rvo-sequence__item-content">
                <!-- Child fields rendered here using their layouts -->
            </div>
        </div>
    </div>
    <c-button kind="tertiary" size="sm" icon="plus" label="{add_label}"
        @click="addSequenceItem('{yaml_path}')" />
</div>
```

### `display-card` — Read-only encrypted/status display (NEW)

**ROOS component:** `<c-card />` with status indicators
**Used for:** encrypted passwords, AGE keys, API keys, configuration blobs, clone-from status

This widget is **new** and needs to be added to `ROOSWidgetAdapter`. It renders a read-only card that shows status information without exposing sensitive data.

```html
<c-card padding="md" outline>
    <c-layout-flow gap="xs">
        <div class="rvo-display-field__header">
            <c-icon icon="{icon}" size="md" color="blauw" />
            <span class="utrecht-form-label">{label}</span>
        </div>
        <!-- Value from converter.view() determines what's shown -->
        <span class="rvo-text--sm rvo-text--subtle">{display_value}</span>
        <c-tag type="success" size="sm">{status_text}</c-tag>
    </c-layout-flow>
</c-card>
```

Variants:
- **Encrypted field:** Shows "Versleuteld opgeslagen" with lock icon
- **Truncated field:** Shows first N characters + "..." (for public keys)
- **Status field:** Shows completion status (for clone-from)
- **Empty/pending:** Shows "Wordt aangemaakt bij eerste deployment"

### `nested` — Grouped sub-fields (no sequence)

**ROOS component:** `<div class="rvo-nested-fields" />` with label
**Used for:** ports (inbound/outbound), resources (cpu/memory)

Groups related fields without the add/remove behavior of a sequence.

```html
<div class="rvo-nested-fields" id="{yaml_path}-group">
    <span class="utrecht-form-label">{label}</span>
    <c-layout-row gap="md">
        <!-- Child fields rendered inline -->
    </c-layout-row>
</div>
```

### Widget dispatch in `WidgetAdapter.render_field()`

The `ROOSWidgetAdapter.render_field()` method dispatches based on `FormField.widget_type`:

```python
# In base.py — existing dispatch (extended for new widgets)
render_methods = {
    "text": self.render_text,
    "textarea": self.render_textarea,
    "select": self.render_select,
    "checkbox": self.render_checkbox,
    "checkbox_group": self.render_checkbox_group,
    "radio": self.render_radio,
    "number": self.render_number,
    "date": self.render_date,
    "hidden": self.render_hidden,
    "service_cards": self.render_service_cards,
    "display_card": self.render_display_card,    # NEW
}
```

Note: Widget type normalization replaces hyphens with underscores (`"display-card"` → `"display_card"`).

---

## Error Display Pattern

```html
{% if errors %}
<c-alert type="error" heading="Corrigeer de volgende fouten:">
    <ul>
        {% for field_path, messages in errors.items() %}
        {% for msg in messages %}
        <li>{{ msg }}</li>
        {% endfor %}
        {% endfor %}
    </ul>
</c-alert>
{% endif %}
```

Individual field errors rendered by existing ROOS widget adapter `errorText` attribute.
