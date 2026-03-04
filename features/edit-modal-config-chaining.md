# Edit Modal Config Section Auto-Chaining

## What It Is

When editing project services through the project details page, adding new services (e.g. Keycloak, PostgreSQL) triggers a deployment. After deployment completes, the edit modal automatically opens the configuration form for each newly added service in sequence, rather than requiring the user to click a button for each one.

This mirrors the wizard's behavior where selecting a service automatically advances to its configuration step.

## How It Works

### Backend

When a service edit triggers deployment and new services need configuration, the backend sends an `X-Next-Section` response header containing a **comma-separated list** of all config section IDs that need attention:

```
X-Next-Section: keycloak-config,postgresql-config
```

This is set in `router_detail_edit.py` using the `SERVICE_CONFIG_SECTIONS` mapping.

### Frontend

The template (`project-details.html.j2`) maintains a **queue** (`editNextSections` array) of pending config sections:

1. **After deployment completes**: The first section is shifted from the queue and auto-opened after an 800ms delay (so the user briefly sees the success state).
2. **After a config section is saved** (save_only path): If more sections remain in the queue, the next one auto-opens. Otherwise, the modal closes and the page reloads.
3. **Escape hatch**: During the 800ms transition delay, a "Later configureren" button is shown. Clicking it clears the queue, closes the modal, and reloads the page.

### Flow Example (Keycloak + PostgreSQL)

```
User adds Keycloak + PostgreSQL in services edit
  -> Submit triggers deployment
  -> Deployment completes, X-Next-Section: "keycloak-config,postgresql-config"
  -> "Volgende stap: Keycloak configuratie" shown briefly
  -> Keycloak config form auto-opens
  -> User fills in and saves
  -> "Volgende stap: Database configuratie" shown briefly
  -> PostgreSQL config form auto-opens
  -> User fills in and saves
  -> No more sections -> modal closes, page reloads
```

## Key Files

| File | Role |
|------|------|
| `opi/web/router_detail_edit.py` | Sends comma-separated `X-Next-Section` header |
| `opi/templates/project-details.html.j2` | Queue-based auto-chaining logic in `showModalProgress()` and `submitEditModal()` |

## Configuration

The section ID to display name mapping is defined in the frontend JavaScript:

```javascript
{
    'keycloak-config': 'Keycloak configuratie',
    'postgresql-config': 'Database configuratie',
    'auth-wall-config': 'Authorization wall configuratie'
}
```

When adding new configurable services, add their section ID and display name to this mapping (appears in two places: the deployment completion handler and the save_only handler).
