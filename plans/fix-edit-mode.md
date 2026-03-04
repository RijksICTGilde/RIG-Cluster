# Plan: Fix Detail Page Edit Modal

## Context

The project details page has an edit button that opens a modal with form fields. Two problems:

1. **Looks terrible** — the modal injects the same rendered HTML the wizard produces (ROOS component tags processed through `process_components`), but `wizard.css` is **not loaded** on the details page. All sequence item layout, nested fieldset card styling, helper-group spacing, etc. are missing. The fix is simply adding `wizard.css` to the details page.

2. **Wrong section first** — `services-edit` is the most complex section (service cards, config modals chained). Only two edit buttons exist (`identity-edit`, `services-edit`). The simpler sections — **Team (members)** and **Components** — have no edit buttons yet and no `EDIT_SECTIONS` entries.

The goal of this plan: fix the styling, then add working edit buttons for **identity** (already exists, verify), **team/members**, and **components** — following the exact same pattern as the wizard.

---

## Root Causes

| Problem | Root cause |
|---|---|
| Form fields look wrong in modal | `wizard.css` not loaded on `project-details.html.j2` |
| Modal too narrow for complex forms | `max-width: 640px` in `.edit-section-modal` CSS — needs to be wider |
| Team section has no edit button | No `team-edit` in `EDIT_SECTIONS`, no button in template |
| Components section has no edit button | No `components-edit` in `EDIT_SECTIONS`, no button in template |

---

## Critical Files

| File | Role |
|---|---|
| `operations-manager/python/opi/templates/project-details.html.j2` | Details page template — add CSS link, edit buttons, widen modal |
| `operations-manager/python/opi/forms/visualizers/wizard_sections.py` | Where `EDIT_SECTIONS` dict lives — add `team-edit` and `components-edit` |
| `operations-manager/python/opi/web/router_detail_edit.py` | Already patched to apply `process_components` — no further changes needed |
| `operations-manager/python/opi/templates/wizard/wizard_step.html.j2` | Reference: shows correct pattern with `process_components` filter |

### Reference: existing section objects (already defined in `wizard_sections.py`)

- `TEAM_SECTION` — the wizard team step with `USERS_SEQUENCE` editable; reuse as-is for `team-edit`
- `COMPONENTS_SECTION` — the wizard components step with nested sequence; reuse for `components-edit`
- `IDENTITY_EDIT_SECTION` — already exists (`identity-edit`), only has DESCRIPTION editable

---

## Implementation Steps

### Step 1 — Load `wizard.css` on the details page

In `project-details.html.j2`, find the `{% block additional_styles %}` block and add the link **before** the existing `<style>` block:

```html
{% block additional_styles %}
<link rel="stylesheet" href="/static/css/wizard.css">
<style>
    ... existing styles ...
</style>
```

### Step 2 — Widen the edit modal

In the inline `<style>` block inside `project-details.html.j2`, find `.edit-section-modal` and change `max-width`:

```css
.edit-section-modal {
    ...
    max-width: 760px;   /* was 640px — wider for complex forms */
    max-height: 90vh;
    ...
}
```

### Step 3 — Add `team-edit` to EDIT_SECTIONS

In `wizard_sections.py`, inside the `EDIT_SECTIONS` dict, add:

```python
EDIT_SECTIONS: dict[str, FormSection] = {
    "identity-edit": IDENTITY_EDIT_SECTION,
    "team-edit": TEAM_SECTION,           # <-- add this
    "components-edit": COMPONENTS_SECTION, # <-- add this
    "services-edit": SERVICES_EDIT_SECTION,
    "keycloak-config": KEYCLOAK_CONFIG_SECTION,
    "postgresql-config": POSTGRESQL_CONFIG_SECTION,
    "auth-wall-config": AUTH_WALL_CONFIG_SECTION,
}
```

`TEAM_SECTION` and `COMPONENTS_SECTION` are already defined in the same file — no new code needed.

**Important:** `TEAM_SECTION` and `COMPONENTS_SECTION` currently have `post_save_action = "save_only"` or `"process_project"` — verify what makes sense for edits. Members likely `save_only`; components likely `process_project` (needs re-deployment). Check `FormSection.post_save_action` values on each and update in `router_detail_edit.py` `submit_edit_section` if needed — currently it always triggers `process_project_yaml_background` regardless of section. That's fine for now.

### Step 4 — Add edit button to Team section in the template

In `project-details.html.j2`, find the **Team & Toegang** section heading (around line 722, look for `groep-3-personen` icon or "Team" heading). Add an edit button next to it, following the exact same pattern as the existing identity-edit button:

```html
{% if user_role in ["admin", "owner"] %}
<button type="button" class="edit-section-btn"
        onclick="openEditModal('team-edit', 'Projectleden beheren')"
        title="Leden bewerken">
    <c-icon icon="bewerken" size="sm" color="hemelblauw" />
</button>
{% endif %}
```

### Step 5 — Add edit button to Components section in the template

In `project-details.html.j2`, find the **Components** section heading (around line 1225, look for `puzzel` icon). Add an edit button:

```html
{% if user_role in ["admin", "owner"] %}
<button type="button" class="edit-section-btn"
        onclick="openEditModal('components-edit', 'Components beheren')"
        title="Components bewerken">
    <c-icon icon="bewerken" size="sm" color="hemelblauw" />
</button>
{% endif %}
```

---

## What to NOT do

- Do NOT touch `services-edit` — it is complex (add-only enforcement, chained service config sections). Leave as-is.
- Do NOT change `router_detail_edit.py` further — the `process_components` fix is already in place.
- Do NOT create new `FormSection` objects — reuse `TEAM_SECTION` and `COMPONENTS_SECTION` directly.
- Do NOT change the wizard templates or wizard CSS.

---

## Verification

After implementation:

Add verification for jinja template output.. actually try to render the templates to detect
processing errors and check if the output does not contain jinja components.
