# Keycloak Setup and Configuration

This document describes the Keycloak setup for RIG Cluster, including SSO federation, user attribute mapping, and automatic configuration.

## Architecture Overview

```
External SSO-Rijk (Digilab Keycloak)
           ↓
    RIG Platform Realm
    (sso-rijk IDP)
           ↓
    Project Realms
    (optional federation)
```

## Bootstrap Process

The operations manager automatically configures Keycloak during startup via `opi/bootstrap/keycloak_setup.py`:

1. **Realm Setup** - Creates `rig-platform` realm if it doesn't exist
2. **External SSO** - Configures `sso-rijk` identity provider with attribute mappers
3. **Client Scopes** - Creates `custom_attributes_passthrough` scope with protocol mappers
4. **Operations Client** - Creates OIDC client for operations manager authentication

## SSO-Rijk Identity Provider

### Configuration

- **Provider Alias**: `sso-rijk`
- **Display Name**: SSO Rijk
- **Type**: OIDC (OpenID Connect)
- **Discovery URL**: From `KEYCLOAK_MASTER_OIDC_DISCOVERY_URL` environment variable
- **Client Credentials**: From `KEYCLOAK_MASTER_OIDC_CLIENT_ID` and `KEYCLOAK_MASTER_OIDC_CLIENT_SECRET`

### Authentication Flow

The platform uses **External IDP Redirector** flow to automatically redirect users to SSO-Rijk:

- **Flow**: External IDP Redirector
- **Executions**:
  1. Cookie (ALTERNATIVE) - Check for existing session
  2. Identity Provider Redirector (ALTERNATIVE) - Redirect to sso-rijk
- **Default Provider**: `sso-rijk`
- **Browser Flow**: External IDP Redirector (set as default)

This eliminates username/password prompts and redirects users directly to SSO-Rijk for authentication.

## Identity Provider Mappers

IDP mappers capture incoming claims from SSO-Rijk and store them as user attributes:

| Mapper Name | Source Claim | Target Attribute | Sync Mode |
|-------------|--------------|------------------|-----------|
| email-to-username | `email` | username | INHERIT |
| email-mapper | `email` | `email` | INHERIT |
| first-name-mapper | `given_name` | `firstName` | INHERIT |
| last-name-mapper | `family_name` | `lastName` | INHERIT |
| full-name-mapper | `name` | `displayName` | INHERIT |
| organization-number-mapper | `organization.number` | `organization.number` | INHERIT |
| organization-name-mapper | `organization.name` | `organization.name` | INHERIT |
| **sso-rijk-userid-mapper** | `sub` | `sso-rijk-userid` | **FORCE** |
| **sso-rijk-userid-lowercase-mapper** | `preferred_username` | `sso-rijk-userid-lowercase` | **FORCE** |

**Note**: SSO-Rijk mappers use FORCE sync mode to ensure attributes are always current.

## Client Scopes and Protocol Mappers

### Platform Realm (rig-platform)

The `custom_attributes_passthrough` scope is assigned as a **realm-level default** scope, ensuring all clients automatically include custom attributes in tokens.

### Group Membership Mapper

To enable group-based authorization, clients need to include user group memberships in access tokens.

**Configuration Steps**:

1. **Navigate to Client** → Select your client (e.g., `authentication-client`)
2. **Client scopes** tab → **Dedicated scopes** (or the client name link)
3. **Mappers** tab → **Add mapper** → **By configuration** → **Group Membership**
4. **Configure the mapper**:
   - **Name**: `groups`
   - **Token Claim Name**: `groups`
   - **Full group path**: **OFF** (important! otherwise groups include leading slash `/RIG` instead of `RIG`)
   - **Add to ID token**: ON
   - **Add to access token**: ON
   - **Add to userinfo**: ON
5. **Save**

**Result**: Access tokens will include a `groups` claim with user group memberships:
```json
{
  "groups": ["RIG", "ICTU"],
  ...
}
```

**Important**: Ensure "Full group path" is disabled, otherwise groups will have format `/RIG` which won't match organization codes in the database.

**Protocol Mappers**:

| Mapper Name | User Attribute | Token Claim | Purpose |
|-------------|----------------|-------------|---------|
| Organization Name Passthrough | `organization.name` | `organization.name` | Custom claim |
| Organization Number Passthrough | `organization.number` | `organization.number` | Custom claim |
| **SSO-Rijk UserID Override (sub)** | `sso-rijk-userid` | `sub` | **Override standard sub claim** |
| **SSO-Rijk UserID Lowercase Override** | `sso-rijk-userid-lowercase` | `preferred_username` | **Override standard username** |
| SSO-Rijk UserID Passthrough | `sso-rijk-userid` | `sso-rijk-userid` | Custom claim |
| SSO-Rijk UserID Lowercase Passthrough | `sso-rijk-userid-lowercase` | `sso-rijk-userid-lowercase` | Custom claim |

**Key Behavior**: Platform realm **overrides** standard `sub` and `preferred_username` claims with SSO-Rijk values for transparent SSO.

### Project Realms

Project realms use the same `custom_attributes_passthrough` scope but **without override mappers**:

- Organization attributes: Passed through as custom claims
- SSO-Rijk attributes: Passed through as custom claims only
- Standard claims (`sub`, `preferred_username`): Use Keycloak's internal values

## Transparent SSO Pattern

The platform implements **transparent SSO** to enable seamless migration:

### Current Flow
```
SSO-Rijk → Digilab Keycloak → RIG Platform Keycloak → Applications
                                (overrides sub/username)
```

### Token Claims
Applications receive tokens with:
- `sub`: SSO-Rijk NameID (e.g., `urn:collab:person:minbzk:nl:Uittenbroek`)
- `preferred_username`: Lowercase version (e.g., `urn:collab:person:minbzk:nl:uittenbroek`)
- `sso-rijk-userid`: Custom claim with original value
- `sso-rijk-userid-lowercase`: Custom claim with lowercase value

### Migration Path
When removing the Digilab intermediary, applications continue working without changes:

```
Before: SSO-Rijk → Digilab → RIG Keycloak → Apps
After:  SSO-Rijk → RIG Keycloak → Apps  (same token claims!)
```

This eliminates the need for user data migration or application changes.

## Client Creation

### Operations Manager Client

- **Client ID**: `rig-platform-operations-manager`
- **Type**: Confidential
- **Redirect URIs**: Based on `OWN_DOMAIN` environment variable
- **Credentials**: Stored in `operations-manager-keycloak` Kubernetes secret
- **Auto-configured**: Credentials automatically updated in settings during bootstrap

### Project Clients

Project clients are created via `keycloak_manager.py`:

- **Naming**: `{project-name}-{deployment-name}`
- **Redirect URIs**: From project ingress hosts + localhost for development
- **Client Scope**: Automatically includes `custom_attributes_passthrough`
- **Credentials**: Stored in project-specific Kubernetes secrets

## Environment Variables

Required environment variables for Keycloak configuration:

```bash
# Keycloak Instance
KEYCLOAK_URL=http://keycloak.kind
KEYCLOAK_ADMIN_USERNAME=admin
KEYCLOAK_ADMIN_PASSWORD=<admin-password>

# RIG Platform Realm
KEYCLOAK_DEFAULT_REALM=rig-platform
KEYCLOAK_DEFAULT_REALM_DISPLAY_NAME="RIG Platform"

# External SSO-Rijk IDP (Digilab Keycloak)
KEYCLOAK_MASTER_OIDC_CLIENT_ID=<client-id>
KEYCLOAK_MASTER_OIDC_CLIENT_SECRET=<client-secret>
KEYCLOAK_MASTER_OIDC_DISCOVERY_URL=https://keycloak.apps.digilab.network/realms/algoritmes/.well-known/openid-configuration

# Operations Manager OIDC (auto-configured)
OIDC_CLIENT_ID=rig-platform-operations-manager
OIDC_CLIENT_SECRET=<auto-generated>
OIDC_DISCOVERY_URL=https://keycloak.kind/realms/rig-platform/.well-known/openid-configuration
```

## Implementation Files

- **Bootstrap**: `operations-manager/python/opi/bootstrap/keycloak_setup.py` - Automatic configuration
- **Connector**: `operations-manager/python/opi/connectors/keycloak.py` - API operations and realm types
- **Manager**: `operations-manager/python/opi/manager/keycloak_manager.py` - Project client management
- **Migration**: `keycloak-migration/` - Historical migration tools and investigation notes

## Idempotency

All Keycloak operations are **idempotent** - the bootstrap can be run multiple times safely:

- Existing resources are detected and skipped or updated as needed
- Mappers are created only if they don't exist
- Client scopes are assigned only if not already assigned
- SSO redirect flow is updated if configuration changes

This ensures reliable startup even after pod restarts or configuration changes.

## Troubleshooting

### Check Bootstrap Logs
```bash
kubectl logs -n rig-system deployment/operations-manager | grep "Step"
```

### Verify SSO Redirect Flow
Check that the External IDP Redirector flow is configured:
```bash
# In Keycloak Admin UI:
# 1. Select rig-platform realm
# 2. Authentication → Flows → External IDP Redirector
# 3. Verify "Identity Provider Redirector" has config with defaultProvider: sso-rijk
```

### Change Browser Flow Binding

**Symptom**: Realm shows "Invalid username or password" without displaying login form, or automatically redirects to external SSO when you want local authentication.

**Cause**: The realm's browser flow is bound to "External IDP Redirector" instead of the standard "browser" flow.

**Solution**: Change the browser flow binding in Keycloak Admin UI:

1. Login to Keycloak master realm at `http://keycloak.kind/admin/master/console/`
2. Switch to the target realm (top-left dropdown)
3. Navigate to **Authentication** in the left sidebar
4. Select the **"browser"** flow from the list
5. Click the **Actions** menu (⋮) for that flow
6. Click **"Bind flow"**
7. Confirm the binding

**When to Use**:
- Project realms should use "browser" flow for local username/password authentication
- Platform realm (`rig-platform`) uses "External IDP Redirector" for automatic SSO redirect to SSO-Rijk

### Test Authentication
Try accessing the operations manager - it should redirect directly to SSO-Rijk without showing a username/password prompt.

### Check Client Scope Assignment
In Keycloak Admin UI:
```
Realm Settings → Client Scopes → Default Client Scopes
→ Verify "custom_attributes_passthrough" is in the "Assigned default client scopes" list
```

### Nginx Ingress "Bad Gateway" (502) Errors

**Symptom**: Application returns "502 Bad Gateway" intermittently, especially during OAuth authentication flow.

**Root Cause**: OAuth authentication generates large response headers that exceed nginx's default buffer size (4k-8k):
- CSRF tokens
- OAuth state cookies
- Session identifiers
- User attribute cookies
- Application custom headers

**Why Intermittent**:
1. First visit: Small headers → works fine
2. OAuth callback: Multiple cookies set simultaneously → headers exceed buffer → 502 error
3. Browser accumulates cookies across requests, compounding the problem
4. `curl` requests work (no cookies) while browser requests fail (cookies persist)

**Check the Error**:
```bash
kubectl logs -n ingress-nginx deployment/ingress-nginx-controller | grep "upstream sent too big header"
```

**Solution**: Increase nginx buffer sizes via ingress annotations (already configured in `manifests/ingress.yaml.jinja`):
```yaml
annotations:
  nginx.ingress.kubernetes.io/proxy-buffer-size: "16k"
  nginx.ingress.kubernetes.io/proxy-buffers-number: "4"
```

**Temporary Fix for Existing Ingress**:
```bash
kubectl annotate ingress <ingress-name> \
  nginx.ingress.kubernetes.io/proxy-buffer-size="16k" \
  nginx.ingress.kubernetes.io/proxy-buffers-number="4" \
  --overwrite
```
