# User Admin CRUD

Platform-level user management through a web admin interface.

## What it does

Provides admin users with a CRUD interface to manage platform users (email + full name) stored in a `users` database table. Accessible at `/admin/users`.

## Access

Only admin users (as determined by `project_service.is_admin(email)`) can access the user admin pages. A "Gebruikersbeheer" menu item appears in the navigation bar for admin users.

## Routes

| Method | Path | Description |
|--------|------|-------------|
| GET | `/admin/users` | List all users |
| GET | `/admin/users/create` | Show create form |
| POST | `/admin/users/create` | Submit create form |
| GET | `/admin/users/{id}/edit` | Show edit form |
| POST | `/admin/users/{id}/edit` | Submit edit form |
| POST | `/admin/users/{id}/delete` | Delete user (with JS confirm) |

## Database

Table `users` with columns: `id` (UUID), `email` (unique), `full_name`, `created_at`, `updated_at`. Created via Alembic migration `003_add_users`.

## Key Files

- `opi/core/user_schema.py` - SQL schema constant
- `opi/migrations/versions/003_add_users.py` - Alembic migration
- `opi/services/user_admin_service.py` - CRUD service
- `opi/forms/editables/user_editables.py` - Form field definitions
- `opi/web/router_user_admin.py` - Web routes
- `opi/templates/admin/users.html.j2` - List page template
- `opi/templates/admin/user-form.html.j2` - Create/edit form template
- `opi/web/menu.py` - Menu item (admin-only)

## Validation

- Email: basic format validation (`EmailValidator`)
- Full name: 2-200 characters (`MinMaxLengthValidator`)
- Duplicate email: caught via unique constraint, shown as form error
