# 08 - Flows: Create Wizard & Edit Tabs

## Overview

This spec describes how individual `EditablePart`s compose into two user-facing flows:

1. **Create Wizard** — A multi-step form guiding users through new project creation
2. **Edit Tabs** — A tabbed interface for editing existing projects

Both flows use the same `EditablePart` definitions, rendered differently via `FormFlow`.

---

## Create Wizard

### Steps

| Step | Part (`EditablePart`) | Title | Description |
|------|----------------------|-------|-------------|
| 1 | `ProjectParts.IDENTITY` | "Uw project" | Naam, beschrijving, clusters |
| 2 | `ProjectParts.SERVICES` | "Services" | Selecteer benodigde services |
| 3 | `ProjectParts.USERS` | "Team" | Voeg teamleden toe |
| 4 | `ProjectParts.COMPONENTS` | "Componenten" | Definieer applicatie-componenten |
| 5 | *(auto-generated)* | "Controleren" | Review all entered data before submit |

### Flow Composition

```python
def get_create_flow() -> FormFlow:
    return FormFlow(
        flow_id="create-project",
        title="Project Aanmaken",
        mode=FlowMode.WIZARD,
        parts=[
            ProjectParts.IDENTITY,
            ProjectParts.SERVICES,
            ProjectParts.USERS,
            ProjectParts.COMPONENTS,
        ],
        show_review=True,
    )
```

### How each step renders

Each wizard step:
1. Gets the `EditablePart` for the current step
2. For each editable in the part:
   - Checks `should_render_editable()` for dependency-gated fields
   - Calls `editable_to_form_field()` to create `FormField` instances
   - The `FormField.widget_type` determines which `ROOSWidgetAdapter` method renders it
3. Applies the part's `layout` (Row/Column/Fieldset/Sequence) to arrange the fields
4. Wraps in `wizard_step.html.j2` template

### Wizard state and cross-step dependencies

Between steps, validated data is stored in the server-side session. This enables cross-part dependencies:

```
Step 2 (Services) saves to session:
  session["create_wizard"]["services"] = {"selected": ["publish-on-web", "keycloak"], ...}

Step 4 (Components) reads from session:
  project_services = session["create_wizard"]["services"]["selected"]
  → FilteredServiceOptionsProvider gets context: {"project_services": project_services}
  → COMPONENT_USES_SERVICES checkbox-group only shows services from step 2
  → COMPONENT_PUBLISH_ON_WEB visibility depends on "publish-on-web" in project_services
  → COMPONENT_SSO_RIJK visibility depends on "keycloak" in project_services
```

### Wizard Template Structure

```html
<!-- wizard_base.html.j2 -->
<div class="rvo-form rvo-wizard">
    <c-layout-flow gap="xl">
        <c-link content="Terug naar overzicht" href="/projects" showIcon="before" icon="terug" />

        <c-heading type="h1" textContent="Project Aanmaken" />

        <!-- Progress tracker -->
        <c-progress-tracker>
            {% for part in flow.parts %}
            <c-progress-tracker-step
                state="{{ 'completed' if step_index > loop.index0 else 'doing' if step_index == loop.index0 else 'incomplete' }}"
                label="{{ part.title }}"
                size="md"
                line="{{ 'straight' if not loop.last else 'none' }}"
            />
            {% endfor %}
            {% if flow.show_review %}
            <c-progress-tracker-step
                state="{{ 'doing' if step_index == flow.parts|length else 'incomplete' }}"
                label="Controleren"
                size="md"
                line="none"
            />
            {% endif %}
        </c-progress-tracker>

        <div id="wizard-content">
            {% block step_content %}{% endblock %}
        </div>
    </c-layout-flow>
</div>
```

### Step Template

```html
<!-- wizard_step.html.j2 -->
<form hx-post="/projects/new/step/{{ current_part.part_id }}"
      hx-target="#wizard-content"
      hx-swap="innerHTML">

    <c-layout-flow gap="md">
        <c-heading type="h2" textContent="{{ current_part.title }}" />
        {% if current_part.description %}
        <p class="rvo-text--lg">{{ current_part.description }}</p>
        {% endif %}

        {% if errors %}
        <c-alert type="error" heading="Corrigeer de volgende fouten:">
            <ul>{% for msg in error_messages %}<li>{{ msg }}</li>{% endfor %}</ul>
        </c-alert>
        {% endif %}

        <!-- Rendered from editables via bridge → FormField → ROOSWidgetAdapter -->
        {{ part_html | safe }}

        <c-action-group>
            {% if previous_part %}
            <c-button type="button" kind="secondary" label="Vorige"
                hx-get="/projects/new/step/{{ previous_part.part_id }}"
                hx-target="#wizard-content" hx-swap="innerHTML" />
            {% endif %}
            <c-button type="submit" kind="primary"
                label="{{ 'Volgende' if next_part else 'Controleren' }}" />
        </c-action-group>
    </c-layout-flow>
</form>
```

### Review Step

The review step renders each part's data in read-only mode using the `summary_fn`:

```html
<!-- wizard_review.html.j2 -->
<c-layout-flow gap="lg">
    <c-heading type="h2" textContent="Controleer uw project" />

    {% for part in flow.parts %}
    <c-card padding="md" outline>
        <c-layout-flow gap="sm">
            <div class="review-section-header">
                <c-heading type="h3" textContent="{{ part.title }}" />
                <c-button type="button" kind="tertiary" size="sm" label="Wijzigen"
                    hx-get="/projects/new/step/{{ part.part_id }}"
                    hx-target="#wizard-content" hx-swap="innerHTML" />
            </div>
            {{ part_summaries[part.part_id] | safe }}
        </c-layout-flow>
    </c-card>
    {% endfor %}

    <form hx-post="/projects/new/confirm" hx-target="#wizard-content" hx-swap="innerHTML">
        <c-action-group>
            <c-button type="button" kind="secondary" label="Vorige"
                hx-get="/projects/new/step/{{ flow.parts[-1].part_id }}"
                hx-target="#wizard-content" hx-swap="innerHTML" />
            <c-button type="submit" kind="primary" label="Project Aanmaken" />
        </c-action-group>
    </form>
</c-layout-flow>
```

### Wizard Completion

When the user confirms on the review step:

1. Assemble full project YAML from all wizard step data in session
2. For each part, use the editables + `set_value()` to build the YAML dict
3. Call `project_service.create_project(project_data)` which:
   - Generates AGE key pair
   - Creates repository credentials
   - Writes the project YAML file
   - Commits and pushes to git
4. Redirect to project details page with success message
5. Clear wizard state from session

---

## Edit Tabs

### Tab Layout

| Tab | Part (`EditablePart`) | Always shown | Notes |
|-----|----------------------|-------------|-------|
| Algemeen | `ProjectParts.IDENTITY` | Yes | Name (readonly), description, clusters |
| Team | `ProjectParts.USERS` | Yes | Users and roles |
| Services | `ProjectParts.SERVICES` | Yes | Service selection + config sub-forms |
| Componenten | `ProjectParts.COMPONENTS` | Yes | Application components |
| Broncode | `ProjectParts.SOURCE_CODE` | If repos exist | Repositories + registries |
| Deployments | `ProjectParts.DEPLOYMENTS` | If deployments exist | Deployment configurations |
| Configuratie | `ProjectParts.CONFIG` | If config exists | Read-only encrypted config |

### Flow Composition

```python
def get_edit_flow(project_data: dict) -> FormFlow:
    parts = [
        ProjectParts.IDENTITY,
        ProjectParts.USERS,
        ProjectParts.SERVICES,
        ProjectParts.COMPONENTS,
    ]

    if get_value(project_data, "repositories") or get_value(project_data, "registries"):
        parts.append(ProjectParts.SOURCE_CODE)
    if get_value(project_data, "deployments"):
        parts.append(ProjectParts.DEPLOYMENTS)
    if get_value(project_data, "config"):
        parts.append(ProjectParts.CONFIG)

    return FormFlow(
        flow_id="edit-project",
        title="Project Bewerken",
        mode=FlowMode.TABS,
        parts=parts,
        htmx_base_url=f"/projects/{project_data['name']}/parts",
        save_per_part=True,
    )
```

### How each tab renders

Each tab panel:
1. Gets the `EditablePart` for the tab
2. Loads the full project YAML (gives context for cross-part dependencies)
3. For each editable in the part:
   - Checks `should_render_editable()` — dependencies resolve against full YAML
   - Calls `editable_to_form_field()` → `FormField` with current values
   - Options providers receive context from full project data
4. Applies the part's `layout`
5. If `part.is_readonly`: wraps in read-only template (no form, no save)
6. Otherwise: wraps in `tab_panel.html.j2` with form + save button

### Tabs Template

```html
<!-- tabs_base.html.j2 -->
<div class="rvo-form rvo-project-edit">
    <c-layout-flow gap="xl">
        <c-link content="Terug naar project" href="/projects/details/{{ project_name }}" />
        <c-heading type="h1" textContent="{{ project_display_name }} bewerken" />

        <div class="rvo-tabs" role="tablist">
            {% for part in flow.parts %}
            <button role="tab"
                class="rvo-tab {{ 'rvo-tab--active' if loop.first }}"
                id="tab-{{ part.part_id }}"
                aria-controls="panel-{{ part.part_id }}"
                aria-selected="{{ 'true' if loop.first else 'false' }}"
                hx-get="{{ flow.htmx_base_url }}/{{ part.part_id }}"
                hx-target="#tab-content"
                hx-swap="innerHTML"
                hx-on::after-request="activateTab(this)">
                {% if part.icon %}<c-icon icon="{{ part.icon }}" size="sm" />{% endif %}
                {{ part.title }}
                <span class="rvo-tab__summary">{{ part_summaries[part.part_id] }}</span>
            </button>
            {% endfor %}
        </div>

        <div id="tab-content" role="tabpanel">
            {{ initial_tab_html | safe }}
        </div>
    </c-layout-flow>
</div>
```

### Tab Panel Template

```html
<!-- tab_panel.html.j2 -->
<div class="rvo-tab-panel" id="panel-{{ part.part_id }}">
    {% if part.is_readonly %}
        {{ readonly_html | safe }}
    {% else %}
        <form hx-post="{{ save_url }}" hx-target="#tab-content" hx-swap="innerHTML">
            {% if saved %}
            <c-alert type="success" heading="Opgeslagen" :dismissable="true">
                Wijzigingen zijn opgeslagen.
            </c-alert>
            {% endif %}

            {% if errors %}
            <c-alert type="error" heading="Corrigeer de volgende fouten:">
                <ul>{% for msg in error_messages %}<li>{{ msg }}</li>{% endfor %}</ul>
            </c-alert>
            {% endif %}

            {{ part_html | safe }}

            <c-action-group>
                <c-button type="submit" kind="primary" label="Opslaan" />
                <c-button type="button" kind="secondary" label="Annuleren"
                    hx-get="{{ load_url }}" hx-target="#tab-content" hx-swap="innerHTML" />
            </c-action-group>
        </form>
    {% endif %}
</div>
```

### Per-Tab Save Flow

```
User edits fields in "Services" tab
    → Clicks "Opslaan"
    → HTMX POST /projects/{name}/parts/services
    → Server:
        1. Parse form data (keyed by yaml_path)
        2. Find EditablePart by part_id
        3. For each non-readonly editable:
           a. validator.validate(value) → collect errors
           b. If part.enforcer: enforcer.enforce(values, context)
           c. If converter: converter.write(value) → storage format
        4a. Invalid: return tab panel with errors
        4b. Valid:
           5. Load current project YAML
           6. For each editable: set_value(yaml, editable.yaml_path, value)
           7. Write updated YAML, commit to git
           8. Return tab panel with success feedback
    → HTMX swaps tab content
```

---

## FastAPI Routes

```python
# opi/web/router_form_parts.py

form_parts_router = APIRouter(tags=["form-parts"])

# === Create Wizard ===

@form_parts_router.get("/projects/new")
async def create_project_wizard(request: Request):
    """Full page: wizard with step 1 loaded."""

@form_parts_router.get("/projects/new/step/{part_id}")
async def get_wizard_step(request: Request, part_id: str):
    """HTMX fragment: load a wizard step."""

@form_parts_router.post("/projects/new/step/{part_id}")
async def submit_wizard_step(request: Request, part_id: str):
    """HTMX fragment: validate step, return next step or errors."""

@form_parts_router.post("/projects/new/confirm")
async def confirm_create_project(request: Request):
    """Create the project from wizard data, redirect to details."""

# === Edit Tabs ===

@form_parts_router.get("/projects/{name}/edit")
async def edit_project_page(request: Request, name: str):
    """Full page: tabs with first tab loaded."""

@form_parts_router.get("/projects/{name}/parts/{part_id}")
async def get_part_content(request: Request, name: str, part_id: str):
    """HTMX fragment: load tab content."""

@form_parts_router.post("/projects/{name}/parts/{part_id}")
async def save_part_content(request: Request, name: str, part_id: str):
    """HTMX fragment: validate and save, return updated tab or errors."""

# === Service Config Sub-Forms ===

@form_parts_router.get("/projects/parts/services/config/{service_name}")
async def get_service_config_form(request: Request, service_name: str):
    """HTMX fragment: service-specific config sub-form."""

@form_parts_router.get("/projects/parts/services/config/empty")
async def get_empty_service_config(request: Request):
    """HTMX fragment: empty div (used when deselecting a service)."""
```

---

## Cross-Part Data Dependencies in Flows

| Consumer Part | Needs from | Data needed | How it gets it |
|--------------|-----------|-------------|----------------|
| Components: `uses-services` | Services | Selected service names | Edit: from full YAML. Wizard: from session step 2 |
| Components: `publish-on-web` | Services | `publish-on-web` in services | Edit: from full YAML. Wizard: from session step 2 |
| Components: `sso-rijk` | Services | `keycloak` in services | Edit: from full YAML. Wizard: from session step 2 |
| Components: `uses-components` | Components | Other component names | Self-reference within same part data |
| Deployments: `components.reference` | Components | Component names | From full YAML (edit-only part) |
| Deployments: `repository` | Source Code | Repository names | From full YAML (edit-only part) |

---

## Tab Activation JavaScript

```javascript
function activateTab(clickedTab) {
    document.querySelectorAll('.rvo-tab').forEach(tab => {
        tab.classList.remove('rvo-tab--active');
        tab.setAttribute('aria-selected', 'false');
    });
    clickedTab.classList.add('rvo-tab--active');
    clickedTab.setAttribute('aria-selected', 'true');
}
```

---

## Accessibility

- Progress tracker steps have proper `aria-` attributes
- Tab buttons use `role="tab"`, `aria-controls`, `aria-selected`
- Tab content uses `role="tabpanel"`
- Form fields use proper `<label>` elements (handled by ROOS components)
- Error messages linked to fields via `aria-describedby` (ROOS errorText)
- Focus management: after HTMX swap, focus moves to the first field or error

## Acceptance Criteria

### Create Wizard
- [ ] Progress tracker shows all steps with correct state (completed/doing/incomplete)
- [ ] Step navigation works: next, previous, jump to completed step
- [ ] Each step validates before allowing next
- [ ] State persists across steps (going back preserves entered data)
- [ ] Cross-step dependencies work (services from step 2 filter options in step 4)
- [ ] Review step shows read-only summary of all entered data
- [ ] "Wijzigen" links on review jump back to the correct step
- [ ] Confirm creates the project and redirects to details
- [ ] Error on creation shows error message, does not lose data

### Edit Tabs
- [ ] Tab navigation loads content via HTMX without full page reload
- [ ] Active tab is visually indicated
- [ ] Each tab saves independently via HTMX POST
- [ ] Cross-part dependencies resolve from full project YAML
- [ ] Success feedback shown after save
- [ ] Validation errors shown inline and as summary alert
- [ ] Read-only tabs (Config) render without form/save button
- [ ] Tab summaries show current state (e.g. "3 gebruikers, 1 admin")
- [ ] Conditional tabs (Source Code, Deployments, Config) only shown when data exists
