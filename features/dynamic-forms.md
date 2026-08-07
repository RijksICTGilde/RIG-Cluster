# Dynamic Forms (Wizard)

## What it is

The dynamic forms system provides a wizard-style interface for creating and editing projects in the Operations Manager. Users navigate through logical sections (chapters) where each section groups related configuration fields. The wizard dynamically shows or hides sections based on user choices - for example, selecting Keycloak as a service reveals a Keycloak configuration section.

The same underlying architecture powers both the "create project" and "edit project" workflows, with different section orderings and behaviors per flow.

## How it works

### Architecture

The system uses a three-layer separation:

```
Layer 1 - Field Definition:  Editable (YAML path + validators + converters + providers)
Layer 2 - Layout:            LayoutElement tree (Fieldset / Row / Column / Sequence)
Layer 3 - Rendering:         WidgetAdapter -> ROOSWidgetAdapter (concrete HTML)

Composition:  FormSection groups editables + layout into a logical section
              FormFlow orders sections into a wizard; step sequence = sections list order
```

**Key design decisions:**
- Step ordering is determined by the `sections` list in `FormFlow`, NOT by any property on the section
- Sections are identified by `section_id` (string), not by numbers
- HTMX handles all dynamic behavior server-side - no client-side JavaScript framework
- Wizard state is stored in the Starlette session (server-side)
- Partial validation runs per step; full validation runs at review/submit

### Conditional sections

Sections can define a `visible` callable that receives the merged wizard data and returns a boolean:

```python
KEYCLOAK_CONFIG_SECTION = FormSection(
    section_id="keycloak-config",
    title="Keycloak configuratie",
    visible=lambda data: "keycloak" in extract_services(data),
    editables=[KEYCLOAK_TEMPLATE, KEYCLOAK_REDIRECT_URIS, ...],
)
```

The resolver (`opi/forms/wizard/resolver.py`) evaluates these conditions after each step submission and updates the active section list. When services are selected or deselected, the step indicator updates via HTMX OOB (out-of-band) swaps.

### HTMX interaction flow

```
1. User loads /forms/wizard/create-project
   → Full page with step indicator + first section loaded

2. User fills fields and clicks "Volgende" (Next)
   → POST /forms/wizard/create-project/step/identity
   → Server validates, stores data in session, resolves active sections
   → Returns next step HTML + OOB step indicator update

3. User selects services (e.g., keycloak)
   → POST /forms/wizard/create-project/step/services
   → Resolver detects keycloak-config section should now be visible
   → Step indicator updates to show the new section

4. User navigates through all sections
   → GET /forms/wizard/create-project/review
   → Shows summary of all sections with "edit" links per section

5. User clicks "Project aanmaken" (Create project)
   → POST /forms/wizard/create-project/submit
   → Full validation + project creation
```

## How to use it

### Defining a new editable field

Add a `Editable` to the appropriate file in `opi/forms/editables/fields/`:

```python
# opi/forms/editables/fields/identity.py
MY_FIELD_EDITABLE = Editable(
    yaml_path="my-field",
    required=True,
    validator=some_validator,
)

# opi/forms/visualizers/fields/identity.py
MY_FIELD = EditableVisualizer(
    editable=MY_FIELD_EDITABLE,
    widget=WidgetType.TEXT,
    label="My Field",
    description="Short description",
    help_text="Extended explanation shown in a collapsible section",
)
```

### Creating a new section

Add a `FormSection` to `opi/forms/visualizers/wizard_sections.py`:

```python
MY_SECTION = FormSection(
    section_id="my-section",
    title="My Section",
    icon="document-blanco",  # Must be a valid ROOS icon
    description="What this section configures",
    editables=[MY_FIELD, OTHER_FIELD],
    layout=Fieldset(
        legend="My Section",
        children=["my-field", "other-field"],
    ),
)
```

### Adding a conditional section

Use a `visible` callable:

```python
MY_CONDITIONAL_SECTION = FormSection(
    section_id="my-conditional",
    title="Conditional Config",
    visible=lambda data: "some-service" in data.get("services", []),
    editables=[...],
    layout=...,
)
```

### Adding a section to a flow

Edit `opi/forms/visualizers/flows.py`. The position in the `sections` list determines the step order:

```python
CREATE_FLOW = FormFlow(
    flow_id="create-project",
    sections=[
        IDENTITY_SECTION,       # Step 1
        SERVICES_SECTION,       # Step 2
        MY_CONDITIONAL_SECTION, # Appears after services if condition met
        TEAM_SECTION,           # Next step
        ...
    ],
    show_review=True,
)
```

### Custom summary rendering

Sections can define a `summary_fn` for the review page. It returns `(label, value)`
pairs of plain text — never HTML; the summary builders add the markup and escape it:

```python
MY_SECTION = FormSection(
    section_id="my-section",
    summary_fn=lambda data: [("Naam", str(data.get("name", "n/a")))],
    ...
)
```

See `features/wizard-samenvatting-weergave.md` for why it returns data.

## Configuration

### Available flows

| Flow ID | Purpose | Review step | Save per section |
|---------|---------|-------------|------------------|
| `create-project` | New project wizard | Yes | No (submit at end) |
| `edit-project` | Edit existing project | No | Yes |

### Available sections

| Section ID | Title | Conditional | Condition |
|-----------|-------|-------------|-----------|
| `identity` | Projectgegevens | No | - |
| `services` | Services | No | - |
| `keycloak-config` | Keycloak configuratie | Yes | `keycloak` in services |
| `postgresql-config` | Database configuratie | Yes | `namespace-postgresql-database` in services |
| `auth-wall-config` | Authorization wall configuratie | Yes | `authorization-wall` in services |
| `team` | Projectleden | No | - |
| `components` | Componenten | No | - |
| `domains` | Webadres | No | - |
| `deployment` | Deployment | No | - |
| `deployments` | Deployments | No | - |
| `config` | Configuratie | No (read-only) | - |

### HTMX routes

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/forms/wizard/{flow_id}` | Load wizard page (create) |
| GET | `/forms/wizard/{flow_id}/edit/{project_name}` | Load wizard page (edit) |
| GET | `/forms/wizard/{flow_id}/step/{section_id}` | Load a step (HTMX fragment) |
| POST | `/forms/wizard/{flow_id}/step/{section_id}` | Validate + advance step |
| GET | `/forms/wizard/{flow_id}/review` | Review summary page |
| POST | `/forms/wizard/{flow_id}/submit` | Final submission |

## Key files

| File | Purpose |
|------|---------|
| `opi/forms/editables/editable.py` | `Editable` dataclass |
| `opi/forms/editables/fields/*.py` | Field definitions by domain |
| `opi/forms/visualizers/visualizer.py` | `EditableVisualizer` dataclass |
| `opi/forms/visualizers/fields/*.py` | Visualizer definitions by domain |
| `opi/forms/visualizers/sections.py` | `FormSection` dataclass |
| `opi/forms/visualizers/flows.py` | `FormFlow` dataclass + flow registry |
| `opi/forms/visualizers/wizard_sections.py` | Section and flow instances |
| `opi/forms/wizard/state.py` | `WizardState` + `WizardSteps` |
| `opi/forms/wizard/session.py` | Session storage helpers |
| `opi/forms/wizard/resolver.py` | Conditional section resolution |
| `opi/web/router_wizard.py` | HTMX routes |
| `opi/templates/wizard/*.html.j2` | Wizard templates |

## Dependencies

- **HTMX**: Client-side library for server-driven interactivity
- **Starlette SessionMiddleware**: Server-side wizard state storage (already configured)
- **ROOS web components**: `jinja-roos-components` for UI widgets and icons
- **EditableFormProcessor**: Existing form processing infrastructure for validation and YAML application

## Troubleshooting

### Section not appearing after selecting a service

1. Verify the section's `visible` callable matches the service name exactly (e.g., `"namespace-postgresql-database"`, not just `"postgresql"`)
2. Check that the section is included in the flow's `sections` list
3. Verify the services field data structure - services can be a list of strings or dicts with a `name` key

### Step indicator not updating

The step indicator updates via HTMX OOB swap (`hx-swap-oob="outerHTML"` on `#wizard-steps`). Ensure:
1. The step response includes the `wizard_steps_indicator.html.j2` partial
2. The `id="wizard-steps"` element exists on the page

### Validation errors not showing

Step validation errors are returned in the same step HTML fragment with an alert banner. Check that the `errors` variable is passed to the template context.
