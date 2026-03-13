# Plan: Modal Wizard — Best of Both Worlds

## Context

The edit modal on the project details page has a broken wizard flow. A previous attempt bolted on client-side step tracking (JS `editStepTracker` + `X-Next-Sections` HTTP headers) to chain config steps after service selection. This is broken because:
1. Config steps only chain for **newly added** services, not existing ones
2. Buttons use plain `<button>` instead of `<c-button>` components
3. The client-side approach duplicates what the wizard engine already does perfectly

**Goal**: Keep the modal UI but drive it with the proven server-side wizard engine (`WizardState`, `FormFlow`, `resolve_active_section_ids`, conditional sections). Even single-step edits go through a 1-step wizard — one system for everything.

## Architecture

```
[Edit button] → openEditModal(flowId)
  → GET /projects/{name}/modal-wizard/{flow_id}   (init state, return first step HTML)
  → Modal shows step content with HTMX form
  → User submits form
  → POST /projects/{name}/modal-wizard/{flow_id}/step/{section_id}  (validate, advance)
  → Server returns next step HTML (HTMX swap) or completion response
  → On final step: save + optional deployment → progress view or close
```

## Files to Modify/Create

### 1. `operations-manager/python/opi/forms/visualizers/flows.py` — Define mini-flows

Add focused FormFlows for each edit action. Reuse existing FormSection definitions.

```python
from opi.forms.visualizers.wizard_sections import (
    IDENTITY_EDIT_SECTION, SERVICES_EDIT_SECTION, COMPONENTS_EDIT_SECTION,
    KEYCLOAK_CONFIG_SECTION, POSTGRESQL_CONFIG_SECTION, AUTH_WALL_CONFIG_SECTION,
    TEAM_SECTION,
)

MODAL_EDIT_IDENTITY_FLOW = FormFlow(
    flow_id="modal-edit-identity",
    title="Projectgegevens bewerken",
    mode=FlowMode.WIZARD, show_review=False,
    sections=[IDENTITY_EDIT_SECTION],
)

MODAL_EDIT_TEAM_FLOW = FormFlow(
    flow_id="modal-edit-team",
    title="Projectleden beheren",
    mode=FlowMode.WIZARD, show_review=False,
    sections=[TEAM_SECTION],
)

MODAL_EDIT_COMPONENTS_FLOW = FormFlow(
    flow_id="modal-edit-components",
    title="Components beheren",
    mode=FlowMode.WIZARD, show_review=False,
    sections=[COMPONENTS_EDIT_SECTION],
)

MODAL_EDIT_SERVICES_FLOW = FormFlow(
    flow_id="modal-edit-services",
    title="Services beheren",
    mode=FlowMode.WIZARD, show_review=False,
    sections=[SERVICES_EDIT_SECTION, KEYCLOAK_CONFIG_SECTION, POSTGRESQL_CONFIG_SECTION, AUTH_WALL_CONFIG_SECTION],
)

# Individual config edit flows (for per-service edit buttons)
MODAL_EDIT_KEYCLOAK_FLOW = FormFlow(
    flow_id="modal-edit-keycloak-config", ..., sections=[KEYCLOAK_CONFIG_SECTION],
)
MODAL_EDIT_POSTGRESQL_FLOW = FormFlow(
    flow_id="modal-edit-postgresql-config", ..., sections=[POSTGRESQL_CONFIG_SECTION],
)
MODAL_EDIT_AUTH_WALL_FLOW = FormFlow(
    flow_id="modal-edit-auth-wall-config", ..., sections=[AUTH_WALL_CONFIG_SECTION],
)
```

Register all in `FLOW_REGISTRY`. Add a lookup dict mapping old section_id → flow_id for backwards compat.

### 2. `operations-manager/python/opi/forms/wizard/session.py` — Separate session key for modal

Add `MODAL_SESSION_KEY = "modal_wizard_token"` with corresponding `get_modal_wizard_state`, `save_modal_wizard_state`, `init_modal_wizard_state`, `clear_modal_wizard_state` functions. These are thin wrappers reusing the same file-based storage pattern, just with a different session key. This prevents modal wizard state from colliding with a full-page create wizard.

### 3. `operations-manager/python/opi/web/router_detail_edit.py` — New modal wizard endpoints

Replace the current GET/POST edit endpoints with wizard-driven modal endpoints. Reuse heavily from `router_wizard.py`.

**`GET /projects/{name}/modal-wizard/{flow_id}`** — Initialize and return first step:
- Load project data, split across sections via `_split_data_across_sections()` (reuse from router_wizard)
- Resolve active sections via `resolve_active_section_ids()`
- Init `WizardState` with `project_name` set (edit mode) using modal session key
- Render first step with `_render_step_html()` (reuse from router_wizard)
- Return `modal_wizard_step.html.j2` template

**`GET /projects/{name}/modal-wizard/{flow_id}/step/{section_id}`** — Load step (for back-navigation):
- Load state from modal session
- Update `current_step`
- Render step, return template

**`POST /projects/{name}/modal-wizard/{flow_id}/step/{section_id}`** — Submit step:
- Load state, validate with `EditableFormProcessor`
- If errors → re-render current step with errors
- If valid → store data, re-resolve active sections, stash inactive
- If more steps → render next step, return HTML
- If last step → call `_modal_do_submit()` (see below)

**`POST /projects/{name}/modal-wizard/{flow_id}/skip`** — "Later configureren":
- Load state, determine which steps have data
- Save accumulated data, trigger deployment, return progress HTML

**`_modal_do_submit()`** — Final submission:
- Merge all step data via `state.get_merged_data()`
- Save to project file (like `_save_existing_project`)
- Determine `post_save_action`:
  - If any active section has `process_project` → trigger deployment background task, return progress HTML with `data-task-id`
  - If all `save_only` → git commit in background, return success HTML

Keep existing `/projects/{name}/edit/{section_id}/sequence` endpoint for add/remove list items (it's used by wizard.js sequence dispatch in modal context).

### 4. `operations-manager/python/opi/templates/wizard/modal_wizard_step.html.j2` — New template

Adapted from `wizard_step.html.j2` for modal context:

```jinja
{# Step indicator (only for multi-step flows) #}
{% if steps.count > 1 %}
<div class="edit-step-indicator" id="modal-wizard-steps">
    {% include "wizard/wizard_steps_indicator.html.j2" %}
</div>
{% endif %}

{# Step content #}
<div class="wizard-step" id="modal-wizard-step-inner">
    <div class="wizard-step__header">
        <c-heading type="h2">
            {% if section.icon %}<c-icon icon="{{ section.icon }}" size="xl" />{% endif %}
            {{ section.title }}
        </c-heading>
        {% if section.description %}
        <p class="wizard-step__description">{{ section.description }}</p>
        {% endif %}
    </div>

    {% if global_errors %}
    <c-alert kind="error" heading="Fouten">...</c-alert>
    {% endif %}

    <form id="modal-wizard-form"
          autocomplete="off"
          hx-ext="json-enc"
          hx-post="/projects/{{ project_name }}/modal-wizard/{{ flow_id }}/step/{{ section.section_id }}"
          hx-target="#edit-section-inner"
          hx-swap="innerHTML">

        <div class="wizard-step__fields" id="edit-section-content">
            {{ step_html | process_components }}
        </div>

        <div class="wizard-step__actions" id="edit-section-actions">
            {% if not steps.is_first %}
            <c-button kind="secondary" showIcon="before" icon="delta-naar-links"
                      hx-get="/projects/{{ project_name }}/modal-wizard/{{ flow_id }}/step/{{ steps.prev }}"
                      hx-target="#edit-section-inner" hx-swap="innerHTML">
                Vorige
            </c-button>
            {% else %}
            <c-button kind="tertiary" @click="closeEditModal()">Annuleren</c-button>
            {% endif %}

            <div class="wizard-step__actions-right">
                {% if not steps.is_last and steps.count > 1 %}
                <c-button kind="tertiary"
                          hx-post="/projects/{{ project_name }}/modal-wizard/{{ flow_id }}/skip"
                          hx-target="#edit-section-inner" hx-swap="innerHTML">
                    Later configureren
                </c-button>
                {% endif %}

                {% if steps.is_last %}
                <c-button type="submit" kind="primary" showIcon="before" icon="publicatie">
                    Opslaan
                </c-button>
                {% else %}
                <c-button type="submit" kind="primary" showIcon="after" icon="delta-naar-rechts">
                    Volgende
                </c-button>
                {% endif %}
            </div>
        </div>
    </form>
</div>

{# OOB swap for step indicator (on HTMX step transitions) #}
{% if not embedded and steps.count > 1 %}
<div id="modal-wizard-steps" hx-swap-oob="outerHTML">
    {% include "wizard/wizard_steps_indicator.html.j2" %}
</div>
{% endif %}
```

Key differences from wizard_step.html.j2:
- `hx-target="#edit-section-inner"` (modal container, not full page)
- No `hx-push-url` (stay on same page)
- "Annuleren" button on first step (instead of empty span)
- "Later configureren" button on non-last steps
- OOB swap targets `#modal-wizard-steps` (not `#wizard-steps`)
- Uses `project_name` and `flow_id` in URLs
- Template context includes `step_base_url` and `step_target` for indicator template

### 5. `operations-manager/python/opi/templates/wizard/modal_wizard_progress.html.j2` — Progress template

Returned when deployment is triggered. Contains the progress UI that JS will poll:

```jinja
<div class="edit-progress-view" data-task-id="{{ task_id }}">
    <div class="edit-progress-bar-wrapper">
        <div class="edit-progress-bar" id="edit-progress-bar" style="width: 0%"></div>
    </div>
    <p class="edit-progress-step" id="edit-progress-step">Verwerking gestart...</p>
    <div class="edit-progress-tasks" id="edit-progress-tasks"></div>
    <div class="edit-progress-actions" id="edit-progress-actions" style="display: none;"></div>
</div>
```

JS picks up `data-task-id` and starts polling (bridges HTMX → JS for progress).

### 6. `operations-manager/python/opi/templates/wizard/modal_wizard_success.html.j2` — Success template

Returned for save_only completion:

```jinja
<div class="edit-section-success">
    <c-icon icon="vinkje" size="xl" color="groen" />
    <p>Wijzigingen opgeslagen</p>
    <div class="edit-section-actions" style="margin-top: 1rem;">
        <c-button kind="primary" @click="closeEditModalAndReload()">Sluiten</c-button>
    </div>
</div>
```

### 7. `operations-manager/python/opi/templates/project-details.html.j2` — Simplify JS

**`openEditModal(flowId, title)`** — Simplified:
- Show modal (backdrop + modal classes)
- Set title
- Fetch `GET /projects/{name}/modal-wizard/{flowId}`
- Set `#edit-section-inner` innerHTML to response
- Process HTMX on new content (`htmx.process()`)
- No more client-side step tracking

**Remove**:
- `editStepTracker`, `initEditStepTracker`, `buildStepQueryParams`, `loadEditStep`
- `submitEditModal()` — HTMX handles form submission
- `collectFormData()`, `parseKeyToSegments`, `setNestedValue`, `cleanArrays`, `buildNestedFromFlat` — form data collected by `hx-ext="json-enc"`
- The broken `X-Next-Sections` header handling

**Keep**:
- `closeEditModal()`, `closeEditModalAndReload()`, `handleEditBackdropClick()`
- `showModalProgress()` / `pollEditProgress()` — but triggered by observing `data-task-id` on HTMX swap

**Add HTMX afterSwap listener** to detect when progress HTML is swapped in:
```javascript
document.addEventListener('htmx:afterSwap', function(evt) {
    var progressEl = evt.detail.target.querySelector('[data-task-id]');
    if (progressEl) {
        var taskId = progressEl.dataset.taskId;
        showModalProgress(taskId);  // Reuse existing progress polling
    }
});
```

**Update edit buttons** throughout the template:
- `openEditModal('identity-edit', '...')` → `openEditModal('modal-edit-identity', '...')`
- `openEditModal('services-edit', '...')` → `openEditModal('modal-edit-services', '...')`
- etc.

### 8. Delete `operations-manager/python/opi/templates/wizard/edit_step.html.j2`

Replaced by `modal_wizard_step.html.j2`. The old template was part of the broken client-side approach.

## Deployment Handling Details

**Determining what action to take on final submission:**
```python
def _determine_flow_action(flow: FormFlow, active_sections: list[FormSection]) -> str:
    """Return 'process_project' if any active section needs deployment, else 'save_only'."""
    for section in active_sections:
        if section.post_save_action == "process_project":
            return "process_project"
    return "save_only"
```

**For `process_project`**: Merge data → save file → create background task → return progress template
**For `save_only`**: Merge data → save file → git commit in background → return success template

## "Later configureren" Flow

When user clicks "Later configureren" on a config step:
1. POST to `/projects/{name}/modal-wizard/{flow_id}/skip`
2. Server loads state, merges accumulated step data (services step at minimum is complete)
3. Saves to project file
4. Since services-edit has `process_project`, triggers deployment
5. Returns progress HTML
6. User sees deployment progress, then close button

This deploys the service additions without the config — config can be edited later via individual config edit buttons on the details page.

## Functions to Reuse from `router_wizard.py`

These should be extracted to shared helpers or imported:
- `_split_data_across_sections(flow, project_data)` — pre-fill step data from project
- `_render_step_html(section, yaml_data, errors, edit_mode)` — render form fields
- `_get_section_from_flow(flow_id, section_id)` — lookup section in flow
- `resolve_active_section_ids`, `resolve_active_sections`, `get_section_metadata` — already in `wizard.resolver`
- `EditableFormProcessor` — already in `forms.editables.processor`

## Sequence Actions (Add/Remove List Items)

The existing `/projects/{name}/edit/{section_id}/sequence` endpoint stays, but update `wizard.js` `_sequenceDispatch` to use the correct form ID (`modal-wizard-form` instead of `wizard-step-form`) when in modal context. Or keep both form IDs and check for either.

## Verification

1. **Single-step edit (identity)**: Open modal → form shows → edit → save → modal closes, page reloads with changes
2. **Single-step edit (components)**: Open modal → form shows → edit → save → progress shows → deployment completes → close
3. **Multi-step edit (services + keycloak)**: Open modal → services form → select keycloak → "Volgende" → keycloak config step appears with step indicator → fill config → "Opslaan" → progress → deployment → close
4. **Multi-step with skip**: Services → select keycloak → "Volgende" → keycloak config → "Later configureren" → deployment runs without config → close
5. **Back navigation**: Services → select keycloak → Volgende → keycloak config → "Vorige" → back to services step
6. **Validation errors**: Submit empty required field → error shown on current step
7. **Individual config edit**: Click keycloak config edit button → single-step wizard → save
8. **Escape key / backdrop click**: Closes modal without saving
