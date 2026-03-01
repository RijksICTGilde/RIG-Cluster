# 02 - Team Members Part

## Overview

The Team Members part manages the list of users who have access to the project. Each user has an email address and a role. At least one user must have the "admin" role. This part tests the **sequence** widget with per-item editables and a part-level enforcer.

## YAML Structure

```yaml
users:
  - email: admin@rijksoverheid.nl
    role: admin
  - email: developer@rijksoverheid.nl
    role: developer
```

## Editable Definitions

```python
class ProjectEditables:

    # === Users (sequence item fields) ===

    USER_EMAIL = ProjectEditable(
        yaml_path="users[*]/email",
        widget="text",
        label="user.email",
        description="user.email.description",
        placeholder="gebruiker@rijksoverheid.nl",
        required=True,
        validator=EmailValidator(),
    )

    USER_ROLE = ProjectEditable(
        yaml_path="users[*]/role",
        widget="select",
        label="user.role",
        description="user.role.description",
        options_provider="UserRoleOptionsProvider",
        required=True,
    )
```

### Sequence structure

The `users` field is a sequence. Each sequence item contains `email` and `role`. The `[*]` in the yaml_path signals that these editables live inside a sequence. The part definition groups them:

```python
    USERS_SEQUENCE = ProjectEditable(
        yaml_path="users",
        widget="sequence",
        label="project.users",
        description="project.users.description",
        min_items=1,
        children=[
            ProjectEditables.USER_EMAIL,
            ProjectEditables.USER_ROLE,
        ],
    )
```

## Part Definition

```python
class ProjectParts:

    USERS = EditablePart(
        part_id="users",
        title="Team",
        icon="personen",
        description="Beheer de teamleden en hun rollen",
        editables=[ProjectEditables.USERS_SEQUENCE],
        layout=Fieldset(
            legend="project.team.title",
            description="project.team.description",
            children=[
                Sequence(
                    field_name="users",
                    child_layout=Row(children=[
                        Column("email", width=8),
                        Column("role", width=4),
                    ]),
                    min_items=1,
                    add_label="Gebruiker toevoegen",
                    remove_label="Verwijderen",
                ),
            ],
        ),
        in_create_wizard=True,
        wizard_step=3,
        enforcer=AdminRequiredEnforcer(),
        summary_fn=users_summary,
    )
```

### Layout and rendering

The `Sequence` layout element references field name `"users"`, which maps to the `USERS_SEQUENCE` editable. Each item renders:

```
┌─────────────────────────────────────────────────┐
│ Item 1                              [Verwijderen]│
│ ┌──────────────────────┐ ┌────────────────────┐ │
│ │ Email (8 col)        │ │ Role select (4 col)│ │
│ │ c-text-input-field   │ │ c-select-field     │ │
│ └──────────────────────┘ └────────────────────┘ │
├─────────────────────────────────────────────────┤
│ Item 2                              [Verwijderen]│
│ ┌──────────────────────┐ ┌────────────────────┐ │
│ │ Email (8 col)        │ │ Role select (4 col)│ │
│ └──────────────────────┘ └────────────────────┘ │
└─────────────────────────────────────────────────┘
  [+ Gebruiker toevoegen]
```

Each child editable renders using its `widget` type:
- `USER_EMAIL` → `render_text()` → `<c-text-input-field />`
- `USER_ROLE` → `render_select()` → `<c-select-field />` with options from `UserRoleOptionsProvider`

## Render Flow

```
GET /projects/{name}/parts/users

1. Load project YAML
2. get_value(yaml, "users") → [{"email": "admin@...", "role": "admin"}, ...]
3. For each user item (index i):
   a. editable_to_form_field(USER_EMAIL, yaml, index=i) → FormField(name="users[0]/email", value="admin@...")
   b. editable_to_form_field(USER_ROLE, yaml, index=i)  → FormField(name="users[0]/role", value="admin")
4. Build sequence FormField with children
5. Render via Sequence layout → ROOSWidgetAdapter.render_sequence()
6. Wrap in tab_panel.html.j2
```

## Save Flow

```
POST /projects/{name}/parts/users

1. Parse form data:
   - "users[0]/email" = "admin@rijksoverheid.nl"
   - "users[0]/role"  = "admin"
   - "users[1]/email" = "developer@rijksoverheid.nl"
   - "users[1]/role"  = "developer"
2. Reconstruct list: [{"email": "admin@...", "role": "admin"}, {"email": "developer@...", "role": "developer"}]
3. Per-item validation:
   - EmailValidator.validate("admin@rijksoverheid.nl") → [] (valid)
   - Required check on role → "admin" is non-empty → valid
4. Part-level enforcement:
   - AdminRequiredEnforcer.enforce(users_list) → checks at least one role == "admin" → passes
5. set_value(yaml, "users", validated_list)
6. Write YAML, commit to git
7. Return updated tab_panel.html.j2
```

## Enforcer

### AdminRequiredEnforcer

```python
class AdminRequiredEnforcer:
    """Ensures at least one user has the admin role."""

    def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        """
        Check that the users list contains at least one admin.

        Args:
            value: The full users list (from the part's collected data)
            context: Rendering context with part data

        Returns:
            The unmodified value. Raises ValueError if no admin found.
        """
        users = context.get("users", [])
        if not users:
            return value

        has_admin = any(
            u.get("role") == "admin"
            for u in users
            if isinstance(u, dict)
        )
        if not has_admin:
            raise ValueError("Ten minste een gebruiker moet de 'admin' rol hebben")
        return value
```

## Validator

### EmailValidator

```python
class EmailValidator:
    """Validates email address format."""

    def validate(self, value: Any) -> list[str]:
        if not value:
            return []
        value_str = str(value)
        # Simple email validation
        if "@" not in value_str or "." not in value_str.split("@")[-1]:
            return ["Voer een geldig e-mailadres in"]
        return []
```

## UX Behavior

### Create wizard (Step 3)
- Pre-populated with one row: the current logged-in user's email as admin
- User can add more team members with the "Gebruiker toevoegen" button
- Cannot remove the last row (min_items=1)
- Role defaults to "developer" for new rows

### Edit tab ("Team")
- Shows all current users
- Can add/remove users
- Cannot remove the last admin (enforcer prevents saving)
- "Opslaan" saves via HTMX POST

### Sequence widget behavior
- Each row renders as a card with `email` (8-col) + `role` select (4-col) + remove button
- "Gebruiker toevoegen" button adds a new empty row
- Remove button on each row (disabled when min_items would be violated)

### How a sequence item is rendered

For each item at index `i`:
1. Resolve `USER_EMAIL` path: `resolve_path("users[*]/email", i)` → `"users[0]/email"`
2. Get value: `get_value(yaml, "users[0]/email")` → `"admin@rijksoverheid.nl"`
3. Build FormField: `FormField(name="users[0]/email", widget_type="text", value="admin@...", ...)`
4. Render: `ROOSWidgetAdapter.render_text(field)` → `<c-text-input-field id="users[0]/email" ... />`

Same for `USER_ROLE`:
1. Resolve path → `"users[0]/role"`
2. Get value → `"admin"`
3. Build FormField with `widget_type="select"`, `options` from `UserRoleOptionsProvider`
4. Render: `ROOSWidgetAdapter.render_select(field)` → `<c-select-field :options="[...]" value="admin" />`

## Display Summary

```python
def users_summary(data: dict) -> str:
    users = get_value(data, "users") or []
    admins = sum(1 for u in users if isinstance(u, dict) and u.get("role") == "admin")
    count = len(users)
    return f"{count} gebruiker{'s' if count != 1 else ''}, {admins} admin{'s' if admins != 1 else ''}"
```

## Validation Rules

1. At least one user must exist (`min_items=1`)
2. At least one user must have the "admin" role (`AdminRequiredEnforcer`)
3. Email must not be empty and must be a valid format (`EmailValidator`)
4. Role must be one of: admin, developer, operator (enforced by select options)
5. Duplicate emails should show a warning (not blocking)

## Acceptance Criteria

- [ ] Users sequence renders with add/remove buttons
- [ ] Each row shows email (text field) + role (select dropdown) side by side
- [ ] On create, pre-populated with logged-in user as admin
- [ ] Cannot save without at least one admin (enforcer error shown)
- [ ] Add button creates new row with default role "developer"
- [ ] Remove button removes the row (disabled if last item)
- [ ] HTMX save persists users to YAML correctly
- [ ] Validation errors shown inline per row (e.g., invalid email on specific row)
- [ ] Summary shows user count and admin count
- [ ] `resolve_path()` correctly indexes into sequence items
