# Edit Modal Config Chaining (Modal Wizard)

## What It Is

When editing project services through the project details page, adding new services (e.g. Keycloak, PostgreSQL) triggers a deployment. After deployment completes, the edit modal continues as a **server-driven modal wizard** that chains through the config sections for newly added services, with step indicators, back/forward navigation, and progress tracking.

This mirrors the full wizard's step-by-step experience where selecting a service automatically advances to its configuration step.

## How It Works

### Architecture

The modal wizard is entirely **server-driven** - step state, navigation, and rendering all happen on the backend. The frontend receives complete HTML fragments via HTMX and swaps them into the modal.

State is persisted in a file-based session store (not browser cookies) to avoid the ~4 KB cookie size limit and survive page reloads.

### Routes

All routes are in `opi/web/router_detail_edit.py`:

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/projects/{name}/modal-wizard/{flow_id}` | Initialize wizard, return first step |
| GET | `/projects/{name}/modal-wizard/{flow_id}/step/{section_id}` | Load a specific step (back-navigation) |
| POST | `/projects/{name}/modal-wizard/{flow_id}/step/{section_id}` | Validate and advance step |
| POST | `/projects/{name}/modal-wizard/{flow_id}/skip` | Save and trigger deployment (skip remaining) |

### Flow Example (Keycloak + PostgreSQL)

```
User adds Keycloak + PostgreSQL in services edit modal
  → Submit validates and stores services data
  → Re-resolve active sections: keycloak-config and postgresql-config now visible
  → Render keycloak-config step with step indicator
  → Step indicator: [Services ✓] → [Keycloak (active)] → [Database (pending)]
  → User fills Keycloak config, submits
  → Advance: [Services ✓] → [Keycloak ✓] → [Database (active)]
  → User fills Database config, submits
  → Last step: merge all data, save YAML, trigger deployment
  → Progress template shown in modal until deployment completes
```

### Step Resolution (Config Chaining)

The chaining mechanism uses `resolve_active_section_ids()` from `opi/forms/wizard/resolver.py`:

1. After each step submission, all step data is merged
2. Each `FormSection`'s `visible` callable is evaluated against merged data
3. Active sections list is updated - new service configs appear, removed ones disappear
4. Data for hidden sections is stashed (preserved if re-activated later)

### Service Protection

Services that existed before the wizard started are "locked" - the user cannot remove them. If a submission tries to remove locked services, the form re-renders with an error and the original services restored. This prevents breaking existing deployments.

### Final Submission

When the last active step is submitted:

1. All section data is merged via `state.get_merged_data()`
2. `apply_modal_edit()` writes only the paths this flow's editables declare onto the
   stored project - see [wizard-write-set.md](wizard-write-set.md)
3. Saved to project YAML file
4. If any section has `post_save_action == "process_project"`: triggers background deployment and shows progress template
5. If save-only: commits to git and shows success template

### Back Navigation & Skip

- Completed steps are clickable in the step indicator for back-navigation
- A "Later configureren" (skip) button is available to save current progress and close

## Session State

State is managed via `opi/forms/wizard/session.py`:

- `init_modal_wizard_state()` - Create new state, save to file store
- `get_modal_wizard_state()` - Load from file store
- `save_modal_wizard_state()` - Persist changes
- `clear_modal_wizard_state()` - Remove state and cookie

State is stored as JSON files under `{TEMP_DIR}/wizard-sessions/{token}.json`.

## Key Files

| File | Role |
|------|------|
| `opi/web/router_detail_edit.py` | Modal wizard routes and submission logic |
| `opi/forms/wizard/save.py` | `apply_modal_edit()` - the whole save path, no I/O |
| `opi/forms/wizard/write_set.py` | Which paths a flow may write, derived from its editables |
| `opi/forms/wizard/resolver.py` | `resolve_active_section_ids()` - conditional section resolution |
| `opi/forms/wizard/session.py` | Modal wizard session state (file-based store) |
| `opi/forms/wizard/state.py` | `WizardState` dataclass with step tracking and data merging |
| `opi/forms/visualizers/wizard_sections.py` | `FormSection` definitions, `SERVICE_CONFIG_SECTIONS` mapping |
| `opi/templates/wizard/modal_wizard_step.html.j2` | Step container with form, navigation, step indicator |
| `opi/templates/wizard/modal_wizard_progress.html.j2` | Deployment progress display |
| `opi/templates/wizard/modal_wizard_success.html.j2` | Save-only completion confirmation |
| `static/css/wizard.css` | Shared wizard step indicator CSS |

## Configuration

When adding new configurable services, add them to the `SERVICE_CONFIG_SECTIONS` mapping in `wizard_sections.py`. The modal wizard automatically picks up the section when the service is selected - no frontend changes needed.
