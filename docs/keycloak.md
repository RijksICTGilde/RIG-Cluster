# Keycloak Setup and Configuration

## Architecture

```
SSO-Rijk → RIG Platform Realm (sso-rijk IDP) → Project Realms (optional federation)
```

Bootstrap is automatic via `opi/bootstrap/keycloak_setup.py` on startup: creates `rig-platform` realm, configures SSO-Rijk IDP, creates client scopes and protocol mappers, creates the operations manager OIDC client. All operations are idempotent.

## SSO-Rijk Identity Provider

- **Alias**: `sso-rijk`, Type: OIDC
- **Discovery URL**: From `KEYCLOAK_MASTER_OIDC_DISCOVERY_URL` env var
- **Auth flow**: External IDP Redirector — auto-redirects to SSO-Rijk (no username/password prompt)
- Platform realm uses this flow as default browser flow; project realms use standard "browser" flow

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

## Transparent SSO

Platform realm **overrides** `sub` and `preferred_username` claims with SSO-Rijk values, so apps get consistent token claims regardless of whether Digilab is in the chain:

- `sub`: SSO-Rijk NameID (e.g., `urn:collab:person:minbzk:nl:Uittenbroek`)
- `preferred_username`: Lowercase version

## Clients

- **Operations Manager**: `rig-platform-operations-manager` (confidential, auto-configured during bootstrap)
- **Project clients**: `{project-name}-{deployment-name}`, created by `keycloak_manager.py`, credentials stored in K8s secrets

## Key Environment Variables

```bash
KEYCLOAK_URL=http://keycloak.kind
KEYCLOAK_ADMIN_USERNAME=admin
KEYCLOAK_ADMIN_PASSWORD=<admin-password>
KEYCLOAK_DEFAULT_REALM=rig-platform
KEYCLOAK_MASTER_OIDC_CLIENT_ID=<client-id>
KEYCLOAK_MASTER_OIDC_CLIENT_SECRET=<client-secret>
KEYCLOAK_MASTER_OIDC_DISCOVERY_URL=<discovery-url>
```

## Key Files

- `opi/bootstrap/keycloak_setup.py` — automatic configuration on startup
- `opi/connectors/keycloak.py` — API client
- `opi/manager/keycloak_manager.py` — project client management

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
