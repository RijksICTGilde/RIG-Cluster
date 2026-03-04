# Edit Modal Wizard-Style Config Step Flow

## What It Is

When editing project services through the project details page, adding new services (e.g. Keycloak, PostgreSQL) triggers a deployment. After deployment completes, the edit modal shows a **wizard-style multi-step flow** with step indicators, back/forward navigation, and progress tracking for all config sections that need attention.

This mirrors the wizard's step-by-step experience where selecting a service automatically advances to its configuration step with visual step indicators.

## How It Works

### Backend

When a service edit triggers deployment and new services need configuration, the backend sends an `X-Next-Sections` response header containing a **JSON array** of section metadata (id, title, icon):

```
X-Next-Sections: [{"id":"services-edit","title":"Services beheren","icon":"applicatie"},{"id":"keycloak-config","title":"Keycloak configuratie","icon":"sleutel"}]
```

The first entry is always the `services-edit` section (already completed), followed by config sections for each newly added service. This metadata is sourced directly from the `FormSection` definitions in `wizard_sections.py`, so no hardcoded title mappings are needed on the frontend.

### Frontend

The template (`project-details.html.j2`) manages a **step state object** (`editStepState`) that tracks:

- `sections`: ordered list of `{id, title, icon}` objects
- `currentIndex`: index of the currently active step
- `completed`: Set of completed section IDs

Key functions:

- `initEditSteps(sections)`: Initializes the step state from the JSON metadata
- `renderEditStepIndicator()`: Renders a `<nav>` with step items, completion checkmarks, and clickable completed steps (reuses wizard CSS classes)
- `navigateEditStep(sectionId)`: Click handler for completed steps to navigate back
- `loadEditStepContent(sectionId, title)`: Loads a section form and updates the indicator
- `updateEditStepActions()`: Shows step-aware buttons (Vorige/Opslaan & volgende)

### Flow Example (Keycloak + PostgreSQL)

```
User adds Keycloak + PostgreSQL in services edit
  -> Submit triggers deployment
  -> X-Next-Sections header contains [services-edit, keycloak-config, postgresql-config]
  -> Step state initialized: services-edit (completed), keycloak-config (active), postgresql-config (pending)
  -> Deployment completes, "Volgende stap: Keycloak configuratie" shown briefly
  -> Step indicator appears: [Services (completed)] -> [Keycloak (active)] -> [Database (pending)]
  -> User fills in Keycloak config and saves
  -> Step advances: [Services (completed)] -> [Keycloak (completed)] -> [Database (active)]
  -> User fills in Database config and saves
  -> Last step done -> modal closes, page reloads
```

### Back Navigation

Completed steps in the indicator are clickable. Clicking a completed step navigates back to that step's form (re-fetched from the server with current saved values). The user can re-save and then advance forward again.

### Escape Hatch

A "Later configureren" button is shown at every step (except the last). Clicking it clears the step state, closes the modal, and reloads the page. The user can configure remaining sections later from the project details page.

## Key Files

| File | Role |
|------|------|
| `opi/web/router_detail_edit.py` | Sends `X-Next-Sections` JSON header with section metadata |
| `opi/templates/project-details.html.j2` | Step state management, indicator rendering, navigation, and submit handlers |
| `opi/forms/visualizers/wizard_sections.py` | `FormSection` definitions with title/icon metadata, `SERVICE_CONFIG_SECTIONS` mapping |
| `static/css/wizard.css` | Shared wizard step indicator CSS (`.wizard-steps` classes), plus `.edit-wizard-steps` override |

## Configuration

When adding new configurable services, add them to the `SERVICE_CONFIG_SECTIONS` mapping in `wizard_sections.py`. The frontend automatically picks up title and icon from the `FormSection` definition — no frontend changes needed.
