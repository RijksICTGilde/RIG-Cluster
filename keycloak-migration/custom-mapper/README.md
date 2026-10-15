# Keycloak Custom Extensions

This JAR contains custom Keycloak extensions for the RIG Cluster platform.

## Contents

1. **Unrestricted XPath Attribute Mapper** - Extract ANY value from SAML assertions
2. **Require Client Role Authenticator** - Restrict access based on client roles in authentication flows
3. **Always-Clear-Session Logout Endpoint** - Broker-logout shim that terminates the Keycloak session before redirecting to an IdP logout page that does not call back. See `features/keycloak-always-clear-session-logout.md`.
4. **RIG Metrics Endpoint** - Prometheus metrics endpoint exposing realm/user/session counts.

---

# Unrestricted XPath Attribute Mapper

Custom Keycloak IDP mapper that allows extracting ANY value from a SAML assertion using XPath on the full XML document.

## Problem

The built-in `XPath Attribute Importer` only searches within `<saml:AttributeStatement>`. It cannot access `<saml:Subject>/<saml:NameID>`.

This custom mapper removes that restriction.

## Building

Using Taskfile (recommended):
```bash
task build-keycloak-custom-mapper
```

Or manually with Maven:
```bash
cd keycloak-migration/custom-mapper
mvn clean package
```

Output: `target/keycloak-saml-nameid-mapper-1.1.0.jar`

## In Keycloak krijgen

De jar gaat in de image die de Keycloak-pod draait. `images/keycloak-zad/` bouwt hem uit deze
bron en zet hem in `/opt/keycloak/providers/`, dus de pod haalt bij het starten niets op.
Bouwen, in de sandbox draaien en promoveren naar productie staat in
`features/keycloak-eigen-image.md`.

De GitHub-release van deze jar is gestopt nu de image de bron is. De bestaande `v1.1.0`
blijft staan, zodat een oud manifest of een terugval er niet op stukloopt, maar er worden
geen nieuwe assets meer op gezet.

Toetsen dat de providers geladen zijn:

```bash
kubectl exec -n rig-system deployment/keycloak -- ls -lh /opt/keycloak/providers/
kubectl logs -n rig-system deployment/keycloak | grep saml-unrestricted-xpath-idp-mapper
```

## Configuration in Keycloak UI

After deployment and restart:

1. Go to **Identity Providers → sso-rijk → Mappers**
2. Click **Add mapper**
3. Select **Unrestricted XPath Attribute Importer**
4. Configure:
   - **Name**: `NameID to sso_rijk_collab_person_id`
   - **XPath Expression**: `//*[local-name()='Subject']/*[local-name()='NameID']/text()`
   - **User Attribute Name**: `sso_rijk_collab_person_id`
   - **Sync Mode**: `FORCE`
5. Save

## XPath Examples

Extract NameID (SSO-Rijk use case):
```xpath
//*[local-name()='Subject']/*[local-name()='NameID']/text()
```

Extract SessionIndex:
```xpath
//*[local-name()='AuthnStatement']/@SessionIndex
```

Extract Issuer:
```xpath
//*[local-name()='Issuer']/text()
```

## Verification

After configuration, delete a test user and login again via SSO-Rijk:

```bash
# Check if attribute is set
curl -H "Authorization: Bearer $TOKEN" \
  "https://keycloak.apps.digilab.network/admin/realms/algoritmes/users?username=test" | \
  jq '.[0].attributes.sso_rijk_collab_person_id'
```

Should return: `["urn:collab:person:minbzk:nl:Uittenbroek"]`

## Troubleshooting

Check Keycloak logs:
```bash
kubectl logs deployment/keycloak-dpl | grep -i "UnrestrictedXPath"
```

Enable debug logging in deployment:
```yaml
env:
  - name: KC_LOG_LEVEL
    value: "INFO,nl.minbzk.rig.keycloak.mapper:DEBUG"
```

Verify JAR is loaded:
```bash
kubectl exec deployment/keycloak-dpl -- ls -la /opt/keycloak/providers/
```

---

# Require Client Role Authenticator

Custom Keycloak authenticator that restricts access based on client roles. Designed specifically for post-broker login flows.

## Problem

Keycloak's built-in conditional sub-flows don't work correctly for post-broker login flows. When using a conditional flow like:

```
Post-Broker Login Flow
└── Deny If No Role [CONDITIONAL]
    ├── Condition - User Role [REQUIRED] (negated)
    └── Deny Access [REQUIRED]
```

The flow fails with "Invalid username or password" when the user HAS the role. This happens because:
1. The condition evaluates to false (user has the role)
2. The conditional sub-flow is skipped
3. The post-broker login flow has nothing left to execute
4. Keycloak treats this as a flow failure

This is a known Keycloak limitation documented in [GitHub issue #14591](https://github.com/keycloak/keycloak/discussions/14591).

## Solution

The `RequireClientRoleAuthenticator` is a custom authenticator that explicitly handles both cases:
- **User HAS the role**: Calls `context.success()` to complete the flow
- **User LACKS the role**: Calls `context.failure()` with a custom error page

This ensures the authentication flow always completes properly.

## Configuration

### Provider ID
`require-client-role-authenticator`

### Configuration Properties

| Property | Label | Description |
|----------|-------|-------------|
| `clientId` | Client ID | The client to check the role against. If empty, uses the current authentication client. |
| `roleName` | Role Name | The client role name that the user must have to be allowed access. |
| `errorMessage` | Error Message | The error message to display when access is denied. Use `${messageKey}` format for theme messages (default: `${accessDeniedNoPermission}`). |

## Usage in Authentication Flows

### Post-Broker Login Flow (recommended)

```
Post-Broker Login Flow
└── Require Client Role [REQUIRED]
```

Configure the authenticator with:
- **Client ID**: Your application's client ID (e.g., `my-app`)
- **Role Name**: The role that grants access (e.g., `allowed-user`)
- **Error Message**: `${accessDeniedNoPermission}` (or custom theme message key)

### Important: Session Behavior

The post-broker login flow only runs when the user authenticates through the IdP, NOT on every request. This means:
- If you remove a user's role while they have an active session, they can still access until the session expires
- To immediately revoke access, invalidate their session in Keycloak Admin Console

## Verification

After configuration, check Keycloak logs:
```bash
kubectl logs deployment/keycloak -n rig-system | grep -i "RequireClientRole"
```

Successful role check:
```
DEBUG User 'john.doe' has required role 'my-app.allowed-user' - allowing access
```

Access denied:
```
INFO User 'jane.doe' does not have required role 'my-app.allowed-user' - denying access
```

## Related Documentation

- [Client Access Restriction Feature](../../features/client-access-restriction.md)
- [Keycloak YAML Configuration](../../docs/keycloak-yaml-configuration.md)
