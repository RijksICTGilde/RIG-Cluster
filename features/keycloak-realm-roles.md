# Keycloak Realm Roles

This feature enables realm-level roles for unified access control across multiple applications sharing the same Keycloak realm.

## What It Is

Realm roles are roles defined at the realm level (not tied to a specific client). When multiple applications share a realm, a single realm role can grant access to all of them, providing unified access control.

**Client Roles vs Realm Roles:**
- **Client roles**: Tied to a specific OIDC client, grants access to one application
- **Realm roles**: Tied to the realm, can grant access to multiple applications

## When to Use

Use realm roles when:
- Multiple applications share the same Keycloak realm
- A single user invitation should grant access to all apps
- You want unified access management across applications

Use client roles when:
- Each application has its own isolated access control
- Different users should have different app access combinations
- Fine-grained per-application permissions are needed

## Configuration

### Creating Realm Roles

Define realm roles in the project file's keycloak service config:

```yaml
services:
- keycloak:
    config:
      template: sso-support
      realm-roles:
        - name: allowed-user
          description: Access to all MijnBureau applications
        - name: mijnbureau-admin
          description: Admin access to MijnBureau applications
```

### Using Realm Roles for Access Restriction

Use `realm-role` instead of `role` in `restrict_access`:

```yaml
services:
- keycloak:
    config:
      template: sso-support
      realm-roles:
        - name: allowed-user
          description: Access to MijnBureau applications
      restrict-access:
        enabled: true
        realm-role: allowed-user  # Uses realm role instead of client role
        error-message: ${accessDeniedNoPermission}
```

### Assigning Realm Roles via Invites

Use `realm_roles` in invite configuration:

```yaml
invites:
  active:
    - key: welcome-to-mijnbureau
      realm_roles:
        - allowed-user
      application_url: https://docs.rijksapp.nl
      allowed_auth_methods:
        - sso
```

**Note:** The legacy `roles` field also assigns realm roles and continues to work. `realm_roles` is the new preferred field for clarity.

## Configuration Fields

### realm-roles (Service Config)

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Role name (e.g., `allowed-user`) |
| `description` | No | Human-readable description |

### restrict_access

| Field | Description |
|-------|-------------|
| `enabled` | Must be `true` to enable access restriction |
| `realm-role` | Realm role name for access check (takes precedence over `role`) |
| `role` | Client role name (used if `realm-role` not specified) |
| `error_message` | Theme message key for access denied |

### Invite Configuration

| Field | Description |
|-------|-------------|
| `realm_roles` | List of realm roles to assign (new, preferred) |
| `roles` | List of realm roles to assign (legacy, still works) |

## How It Works

### Role Creation

1. When the project is deployed, realm roles are created in the Keycloak realm
2. Roles are created idempotently - re-deployment doesn't create duplicates
3. Existing roles are preserved

### Access Restriction Flow

When `realm-role` is specified in `restrict_access`:

1. **Browser flow created**: A restricted browser flow checks for the realm role
2. **Flow set on client**: The client uses this flow for authentication
3. **Role check**: Users without the realm role see an access denied message

### Invite Flow

When a user accepts an invite:

1. User authenticates (SSO or local)
2. `realm_roles` from invite are assigned to the user
3. User can now access all applications requiring those roles

## Example: Complete MijnBureau Setup

### Owner Project Configuration

```yaml
# mb-docs-helmfile.yaml
services:
- keycloak:
    config:
      template: sso-support

      # Define realm roles
      realm-roles:
        - name: allowed-user
          description: Access to all MijnBureau applications
        - name: mijnbureau-editor
          description: Editor access to MijnBureau

      # Create clients for other apps
      additional-clients:
        - name: mb-grist-helmfile-production
          redirect-uris:
            - https://grist.rijksapp.nl/*

      # Restrict access using realm role
      restrict-access:
        enabled: true
        realm-role: allowed-user

# Invite assigns realm role
invites:
  active:
    - key: join-mijnbureau
      realm_roles:
        - allowed-user
      application_url: https://docs.rijksapp.nl
      allowed_auth_methods:
        - sso
```

### Dependent Project Configuration

```yaml
# mb-grist-helmfile.yaml
services:
- keycloak:
    type: external
    config:
      host: https://keycloak.rig.prd1.gn2.quattro.rijksapps.nl
      realm: mb-docs-helmfile-odcn-production
      client-id: mb-grist-helmfile-production
      client-secret: <AGE encrypted client secret>
```

### User Journey

1. User receives invite link to docs application
2. User authenticates via SSO
3. User receives `allowed-user` realm role
4. User can access docs (owner app)
5. User can also access grist (dependent app) - same realm role works!

## Comparison with Client Roles

### Client Role Setup (Old Way)

Each app needs separate role assignment:

```yaml
# App 1
restrict-access:
  enabled: true
  role: app1-user  # Client role specific to app1

# App 2
restrict-access:
  enabled: true
  role: app2-user  # Different client role for app2
```

Invite needs to assign multiple roles:
```yaml
client_roles:
  app1-client-id:
    - app1-user
  app2-client-id:
    - app2-user
```

### Realm Role Setup (New Way)

All apps use the same realm role:

```yaml
# Both apps
restrict-access:
  enabled: true
  realm-role: allowed-user  # Same realm role for all apps
```

Invite assigns one role that works everywhere:
```yaml
realm_roles:
  - allowed-user
```

## Verifying Realm Roles

Check realm roles exist in Keycloak:

```bash
# Using the Keycloak Admin API (via curl)
# Or check via Keycloak Admin Console:
# Realm Settings > Realm Roles
```

Check user has realm role:

```bash
# Via Keycloak Admin Console:
# Users > Select User > Role Mappings > Realm Roles
```

## Troubleshooting

### Role Not Assigned

Check invite configuration uses `realm_roles` (not `client_roles`):
```yaml
# Correct
realm_roles:
  - allowed-user

# Wrong - this assigns client roles
client_roles:
  client-id:
    - allowed-user
```

### Access Denied Despite Having Role

Ensure all applications use `realm-role` (not `role`) in `restrict_access`:
```yaml
restrict-access:
  enabled: true
  realm-role: allowed-user  # NOT: role: allowed-user
```

### Role Not Found Error

Ensure the realm role is created by the owner project before it's used:
1. Deploy owner project first (creates realm roles)
2. Then deploy dependent projects

## Related Features

- [keycloak-external-provider.md](keycloak-external-provider.md) - Using credentials from another project
- [keycloak-additional-clients.md](keycloak-additional-clients.md) - Creating clients for other projects
- [client-access-restriction.md](client-access-restriction.md) - Restricting access to users with specific roles
- [invite-system.md](invite-system.md) - User invitation system
