# Invite System

The invite system allows project administrators to invite users to their project's Keycloak realm through shareable invite links. Users can join via SSO or by creating a local account.

**Sample Configuration**: See `projects/amt-136.yaml` for a working example with two invite configurations.

## Overview

- **Storage**: YAML-based (invites configured in project YAML files)
- **Scope**: Project-level (grants access to project's Keycloak realm)
- **Email**: No email service (admins manually share invite links)
- **Admin UI**: YAML-configured only (no web admin interface)

## Configuration

Add an `invites` section to your project YAML file:

```yaml
# projects/my-project.yaml
name: my-project
display-name: My Project
# ... existing config ...

invites:
  settings:
    allow_sso: true              # Enable SSO authentication path
    allow_local: true            # Enable local account creation
    default_language: nl         # Default language (nl or en)

  active:
    - key: "welcome-team"        # Shareable key (URL: /invite/welcome-team)
      roles: ["developer"]       # Realm roles to assign
      groups: ["developers"]     # Groups to add user to (optional)
      application_url: "https://app.example.com"  # Link shown after success
      expires_at: "2026-02-01"   # Optional: expiration date
      restrict_domain: "@example.org"  # Optional: restrict email domain

      # Multi-language messages
      message:
        nl: "Welkom bij het project!"
        en: "Welcome to the project!"

      # Optional: localized success page content
      success_title:
        nl: "Account aangemaakt"
        en: "Account created"
      success_button:
        nl: "Ga naar applicatie"
        en: "Go to application"
```

## Configuration Options

### Settings

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `allow_sso` | boolean | `true` | Enable SSO authentication option |
| `allow_local` | boolean | `true` | Enable local account creation option |
| `default_language` | string | `nl` | Default language (`nl` or `en`) |

### Invite Properties

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `key` | string | Yes | Unique identifier for the invite URL |
| `roles` | list | No | Keycloak realm roles to assign |
| `groups` | list | No | Keycloak groups to add user to |
| `application_url` | string | No | URL shown on success page |
| `expires_at` | date | No | Expiration date (YYYY-MM-DD format) |
| `restrict_domain` | string | No | Required email domain (e.g., `@example.org`) |
| `auth_methods` | list | No | Override allowed methods (`["sso"]` or `["local"]` or both) |
| `message` | string/dict | No | Welcome message (string or `{nl: ..., en: ...}`) |
| `success_title` | string/dict | No | Success page title |
| `success_button` | string/dict | No | Success button text |

## Usage

### Creating an Invite

1. Add the invite configuration to your project YAML file
2. Commit and push the changes (GitOps workflow)
3. Share the invite URL with users: `https://ops.example.com/invite/{key}`

### User Flow

#### Landing Page
Users visit `/invite/{key}` and see:
- Project name and welcome message
- Authentication options (SSO and/or Local Account based on config)

#### SSO Flow
1. User clicks "Login with SSO"
2. Redirected to SSO provider (Keycloak/SSO-Rijk)
3. After authentication, user is created/found in project realm
4. Configured roles and groups are assigned
5. Success page with link to application

#### Local Account Flow
1. User clicks "Create Local Account"
2. Fill in registration form (email, name, password)
3. Account created in project realm
4. Configured roles and groups are assigned
5. Success page with link to application

### Password Requirements (Local Accounts)

- Minimum 12 characters
- At least one uppercase letter
- At least one lowercase letter
- At least one digit

## Examples

### Basic Developer Invite

```yaml
invites:
  active:
    - key: "join-dev-team"
      roles: ["developer"]
      application_url: "https://app.example.com"
      message:
        nl: "Word lid van ons ontwikkelteam"
        en: "Join our development team"
```

### Admin Invite (SSO Only)

```yaml
invites:
  active:
    - key: "admin-access"
      roles: ["admin", "developer"]
      groups: ["administrators"]
      auth_methods: ["sso"]  # SSO only for admin invites
      restrict_domain: "@rijksoverheid.nl"
      application_url: "https://admin.example.com"
      message:
        nl: "Admin toegang uitnodiging"
        en: "Admin access invitation"
```

### Time-Limited Invite

```yaml
invites:
  active:
    - key: "workshop-2026"
      roles: ["participant"]
      expires_at: "2026-03-01"
      application_url: "https://workshop.example.com"
      message:
        nl: "Uitnodiging voor de workshop"
        en: "Workshop invitation"
```

### Domain-Restricted Invite

```yaml
invites:
  active:
    - key: "internal-only"
      roles: ["employee"]
      restrict_domain: "@company.nl"
      message:
        nl: "Alleen voor medewerkers van Company"
        en: "For Company employees only"
```

## Language Detection

Language is determined in this order:
1. URL parameter: `?lang=en` or `?lang=nl`
2. Browser `Accept-Language` header
3. Project's `default_language` setting
4. Falls back to `nl`

## Error Handling

| Error | User Message (NL) | User Message (EN) |
|-------|-------------------|-------------------|
| Invalid key | Uitnodiging niet gevonden | Invitation not found |
| Expired | Uitnodiging verlopen | Invitation expired |
| Domain mismatch | E-mailadres voldoet niet aan de vereisten | Email does not meet requirements |
| User exists | Account bestaat al | Account already exists |
| SSO not allowed | SSO login niet beschikbaar | SSO login not available |
| Local not allowed | Account aanmaken niet beschikbaar | Account creation not available |

## Security Considerations

1. **Invite Keys**: Use descriptive but not easily guessable keys
2. **Expiration**: Set appropriate expiration dates for time-sensitive invites
3. **Domain Restrictions**: Use `restrict_domain` to limit access to specific organizations
4. **Auth Method Control**: Use `auth_methods: ["sso"]` for sensitive roles to enforce SSO
5. **No User Enumeration**: The system does not expose user lists or validate email existence

## Routes Reference

| Method | Route | Description |
|--------|-------|-------------|
| GET | `/invite/{key}` | Landing page with auth options |
| GET | `/invite/{key}/sso` | Initiate SSO flow |
| GET | `/invite/{key}/sso/callback` | SSO callback handler |
| GET | `/invite/{key}/register` | Local registration form |
| POST | `/invite/{key}/register` | Submit registration |
| GET | `/invite/{key}/success` | Success confirmation |
| GET | `/invite/{key}/error` | Error page |

## Dependencies

- Keycloak realm must exist for the project
- Project must have at least one deployment (for cluster/realm determination)

## Technical Setup

### OAuth Client (Automatic)

The invite system requires an OAuth client in each project's Keycloak realm. This client is **automatically created** when:
- A new project realm is created
- An existing project is refreshed (idempotent - skips if exists)

The client configuration:
```yaml
clientId: "operations-manager-invites"  # Configurable via INVITE_CLIENT_ID
publicClient: true                       # No secret required
pkceCodeChallengeMethod: "S256"          # PKCE for security
redirectUris:
  - "{{ operations_manager_url }}/invite/*"
webOrigins:
  - "{{ operations_manager_url }}"
```

### Why PKCE?
The invite flow uses PKCE (Proof Key for Code Exchange) instead of a client secret because:
- The OAuth flow happens in the user's browser (public client)
- PKCE provides equivalent security without needing to manage secrets across multiple realms
- Each invite flow generates a unique code verifier/challenge pair

### Configuration (Operations Manager)

The following settings control the invite OAuth client:

| Setting | Default | Description |
|---------|---------|-------------|
| `INVITE_CLIENT_ID` | `operations-manager-invites` | Client ID for invite OAuth flow |
| `OWN_DOMAIN` | - | Operations manager URL (used for redirect URIs)  |

### Keycloak Template Integration

The invite client is defined in the Keycloak YAML templates:
- `configs/keycloak/sso-only.yaml` - For SSO-only realms
- `configs/keycloak/sso-support.yaml` - For realms with SSO + local accounts

To add the invite client to a custom template, include:
```yaml
clients:
  - clientId: "{{ invite_client_id }}"
    name: "Operations Manager Invite Flow"
    enabled: true
    publicClient: true
    protocol: "openid-connect"
    redirectUris:
      - "{{ operations_manager_url }}/invite/*"
    webOrigins:
      - "{{ operations_manager_url }}"
    standardFlowEnabled: true
    implicitFlowEnabled: false
    directAccessGrantsEnabled: false
    serviceAccountsEnabled: false
    pkceCodeChallengeMethod: "S256"
```

### Refreshing Projects

If the invite client is missing from an existing project realm, simply refresh the project:
- The keycloak YAML handler will create any missing clients defined in the template
- This is idempotent - existing clients are skipped without errors

## Troubleshooting

### Invite Not Found
- Verify the invite key exists in the project YAML
- Check that the project has been synced (GitOps)
- Ensure the project data is loaded in memory

### SSO Callback Errors
- Check OAuth client configuration
- Verify Keycloak realm accessibility
- Check session middleware is configured

### User Creation Fails
- Verify Keycloak admin credentials
- Check realm exists and is accessible
- Review Keycloak logs for detailed errors

### Domain Restriction Issues
- Domain comparison is case-insensitive
- Include the `@` symbol in the domain (e.g., `@example.org`)
- User's email domain must exactly match the restriction
