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

## Publishing to GitHub

### Prerequisites
- GitHub CLI (`gh`) installed and authenticated
- Repository must be public for wget downloads to work
- Push permissions to the repository

### Quick Publish

Using Taskfile (recommended):
```bash
task publish-keycloak-custom-mapper
```

This will:
1. Build the JAR (if not already built)
2. Create or update the GitHub release (currently v1.1.0 — see Taskfile `VERSION=`)
3. Upload the JAR as a release asset
4. Verify the download URL

The JAR will be available at:
`https://github.com/RijksICTGilde/RIG-Cluster/releases/download/v1.1.0/keycloak-saml-nameid-mapper-1.1.0.jar`

### Manual Publishing Steps

If you prefer to publish manually:

1. **Build the JAR**:
   ```bash
   task build-keycloak-custom-mapper
   ```

2. **Create GitHub Release**:
   ```bash
   cd keycloak-migration/custom-mapper
   gh release create v1.1.0 \
     --repo "RijksICTGilde/RIG-Cluster" \
     --title "Keycloak SAML NameID Mapper v1.1.0" \
     --notes "Adds always-clear-session logout endpoint (broker logout shim)" \
     target/keycloak-saml-nameid-mapper-1.1.0.jar
   ```

3. **Verify Release**:
   ```bash
   curl -sI https://github.com/RijksICTGilde/RIG-Cluster/releases/download/v1.1.0/keycloak-saml-nameid-mapper-1.1.0.jar
   # Should return HTTP 200 or 302
   ```

## Deployment

The JAR needs to be in `/opt/keycloak/providers/` when Keycloak starts.

### Option 1: Init Container Download (Recommended)

Update your Keycloak deployment's init container to download the custom mapper from the GitHub release:

```yaml
initContainers:
  - name: keycloak-theme-puller
    command:
      - sh
      - -c
      - |
        cd /tmp
        # Download theme
        wget https://github.com/MinBZK/keycloak-theme/releases/download/v1.2.1/keycloak-nl-design-system.jar
        cp keycloak-nl-design-system.jar /opt/keycloak/providers/

        # Download custom mapper
        wget https://github.com/RijksICTGilde/RIG-Cluster/releases/download/v1.1.0/keycloak-saml-nameid-mapper-1.1.0.jar
        cp keycloak-saml-nameid-mapper-1.1.0.jar /opt/keycloak/providers/
    image: busybox:1.37.0
    securityContext:
      runAsUser: 0
    volumeMounts:
      - mountPath: /opt/keycloak/providers/
        name: keycloak-provider
```

**Apply the change**:
```bash
# Update the deployment YAML in your GitOps repo
# Commit and push the change
git add infrastructure/bootstrap/infrastructure/keycloak/controller/base/deployment.yaml
git commit -m "Add custom SAML NameID mapper to Keycloak"
git push

# If using ArgoCD, sync the application
# Or manually apply:
kubectl apply -f infrastructure/bootstrap/infrastructure/keycloak/controller/base/deployment.yaml

# Restart Keycloak to load the new JAR
kubectl rollout restart deployment/keycloak-dpl -n keycloak
```

**Verify the JAR is loaded**:
```bash
# Check JAR is in the pod
kubectl exec deployment/keycloak-dpl -n keycloak -- ls -lh /opt/keycloak/providers/

# Check Keycloak logs for mapper registration
kubectl logs deployment/keycloak-dpl -n keycloak | grep "saml-unrestricted-xpath-idp-mapper"
# Should show: "KC-SERVICES0047: saml-unrestricted-xpath-idp-mapper (nl.minbzk.rig.keycloak.mapper.UnrestrictedXPathAttributeMapper) is implementing the internal SPI"
```

### Option 2: Host JAR in Git Repository

1. Create `custom-providers` directory in your infra repo
2. Add the JAR file to git (or git LFS)
3. Use ConfigMap or init container to copy it

Example with ConfigMap:

```bash
# Create ConfigMap from JAR
kubectl create configmap keycloak-custom-mapper \
  --from-file=keycloak-saml-nameid-mapper.jar=target/keycloak-saml-nameid-mapper-1.1.0.jar \
  -n keycloak

# Mount in deployment
volumeMounts:
  - name: custom-mapper
    mountPath: /opt/keycloak/providers/keycloak-saml-nameid-mapper.jar
    subPath: keycloak-saml-nameid-mapper.jar

volumes:
  - name: custom-mapper
    configMap:
      name: keycloak-custom-mapper
```

### Option 3: Manual Deployment (Testing)

```bash
# Build JAR
mvn clean package

# Copy to running Keycloak pod
kubectl cp target/keycloak-saml-nameid-mapper-1.1.0.jar \
  keycloak-dpl-xxx:/opt/keycloak/providers/ -n keycloak

# Restart Keycloak
kubectl rollout restart deployment/keycloak-dpl -n keycloak
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

- [Client Access Restriction Feature](/features/client-access-restriction.md)
- [Keycloak YAML Configuration](/docs/keycloak-yaml-configuration.md)
