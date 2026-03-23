# User Admin CRUD - Implementation Plan

## Goal
Add a simple admin page to the Operations Manager for managing users (email + full name) in a database table, using the existing editable/wizard/HTMX stack.

## Context
- Users are currently defined in project YAML files. We want a separate `users` database table for platform-level user management.
- The existing editable system (Editable + EditableVisualizer + FormSection + FormFlow + EditableFormProcessor) provides all the form logic we need.
- The existing asyncpg pool pattern (acquire/execute/release) is used for all DB operations -- no SQLAlchemy ORM needed.
- ROOS components provide the UI (tables, cards, buttons, forms).

## Architecture Reference

### Existing Patterns to Follow
- **DB CRUD**: `opi/connectors/subdomain.py` (SubdomainConnector) and `opi/services/marked_for_deletion_service.py`
- **Editables**: `opi/forms/editables/` (Editable, EditableVisualizer, FormSection, FormFlow)
- **Validators**: `opi/forms/editables/validators.py` (EmailValidator, MinMaxLengthValidator)
- **Processor**: `opi/forms/editables/processor.py` (EditableFormProcessor)
- **Web routes with modals**: `opi/web/router_detail_edit.py`
- **Template listing pattern**: `opi/templates/project-details/section-*.html.j2`
- **Auth**: `opi/core/auth_decorators.py` (@requires_sso) + role checks
- **Schema constants**: `opi/core/async_task_schema.py`, `opi/core/marked_for_deletion_schema.py`
- **Migrations**: `opi/migrations/versions/001_baseline.py`, `002_add_marked_for_deletion.py`

## Implementation Steps

### 1. Database Table (`opi/core/user_schema.py`)
Create SQL constant for the `users` table:
```sql
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT UNIQUE NOT NULL,
    full_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_users_email ON users (email);
```

### 2. Alembic Migration (`opi/migrations/versions/003_add_users.py`)
- Import `USERS_TABLE_SQL` from `opi.core.user_schema`
- `upgrade()`: `op.execute(USERS_TABLE_SQL)`
- `downgrade()`: `op.execute("DROP TABLE IF EXISTS users;")`

### 3. User Admin Service (`opi/services/user_admin_service.py`)
CRUD operations using asyncpg pool pattern:
- `list_users() -> list[dict]` -- SELECT all, ordered by full_name
- `get_user(user_id: str) -> dict | None` -- SELECT by UUID
- `get_user_by_email(email: str) -> dict | None` -- SELECT by email
- `create_user(email: str, full_name: str) -> dict` -- INSERT RETURNING
- `update_user(user_id: str, email: str, full_name: str) -> dict | None` -- UPDATE RETURNING (also updates `updated_at`)
- `delete_user(user_id: str) -> bool` -- DELETE, return True if deleted

Pattern: `pool = get_database_pool("main")` → `conn = await pool.acquire()` → try/finally release.

### 4. Editable Definitions (`opi/forms/editables/user_editables.py`)

```python
# Editables (data logic)
USER_EMAIL_EDITABLE = Editable(
    yaml_path="email",  # flat path, not YAML but reusing the mechanism
    required=True,
    validator=EmailValidator(),
)

USER_FULL_NAME_EDITABLE = Editable(
    yaml_path="full_name",
    required=True,
    validator=MinMaxLengthValidator(min_length=2, max_length=200),
)

# Visualizers (UI)
USER_EMAIL_VISUALIZER = EditableVisualizer(
    editable=USER_EMAIL_EDITABLE,
    widget=WidgetType.TEXT,
    label="E-mailadres",
    placeholder="gebruiker@example.nl",
)

USER_FULL_NAME_VISUALIZER = EditableVisualizer(
    editable=USER_FULL_NAME_EDITABLE,
    widget=WidgetType.TEXT,
    label="Volledige naam",
    placeholder="Jan de Vries",
)

# Section
USER_SECTION = FormSection(
    section_id="user-details",
    title="Gebruiker",
    editables=[USER_EMAIL_VISUALIZER, USER_FULL_NAME_VISUALIZER],
    layout=Fieldset(legend="Gebruiker", children=[
        Row(children=[
            Column(width=6, children=[USER_EMAIL_VISUALIZER]),
            Column(width=6, children=[USER_FULL_NAME_VISUALIZER]),
        ])
    ]),
)

# Flows
CREATE_USER_FLOW = FormFlow(
    flow_id="create-user",
    sections=[USER_SECTION],
    show_review=False,
)

EDIT_USER_FLOW = FormFlow(
    flow_id="edit-user",
    sections=[USER_SECTION],
    show_review=False,
)
```

### 5. Web Routes (`opi/web/router_user_admin.py`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/admin/users` | List all users in ROOS table |
| GET | `/admin/users/create` | Show create form (wizard single-step) |
| POST | `/admin/users/create` | Process create form, insert into DB |
| GET | `/admin/users/{user_id}/edit` | Show edit form pre-filled |
| POST | `/admin/users/{user_id}/edit` | Process edit form, update DB |
| POST | `/admin/users/{user_id}/delete` | Delete user (with confirmation) |

All routes protected with `@requires_sso` + admin/platform_admin role check.

Use `EditableFormProcessor.process_json_submission()` for validation on create/edit.
- On success: redirect to `/admin/users` with success flash
- On validation error: re-render form with errors

### 6. Templates

**`templates/admin/users.html.j2`** -- List page:
- Extends `base.html.j2`
- ROOS table with columns: Naam, E-mailadres, Aangemaakt, Acties
- "Gebruiker toevoegen" button linking to create form
- Per-row Edit button (link to edit form) and Delete button (with confirmation modal)
- Empty state message when no users

**`templates/admin/user-form.html.j2`** -- Create/Edit form:
- Extends `base.html.j2`
- Renders the wizard step using the existing widget/renderer pipeline
- Form submits via HTMX POST
- Shows validation errors inline (same pattern as wizard steps)
- Cancel button returns to list

### 7. Wire Up

**`opi/server.py`**:
- Import and include `user_admin_router`

**Menu** (in template context or menu helper):
- Add "Gebruikersbeheer" menu item for admin users, linking to `/admin/users`

## What We Don't Need
- No SQLAlchemy ORM -- follow existing raw asyncpg pattern
- No Pydantic model for DB entity -- just dicts from asyncpg Records
- No complex wizard session state -- single-step form, use processor directly
- No new dependencies

## Key Files to Create
1. `opi/core/user_schema.py`
2. `opi/migrations/versions/003_add_users.py`
3. `opi/services/user_admin_service.py`
4. `opi/forms/editables/user_editables.py`
5. `opi/web/router_user_admin.py`
6. `opi/templates/admin/users.html.j2`
7. `opi/templates/admin/user-form.html.j2`

## Key Files to Modify
1. `opi/server.py` -- register router
2. Menu/navigation -- add admin link
