# Keycloak Unrestricted XPath Attribute Mapper

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

Output: `target/keycloak-saml-nameid-mapper-1.0.0.jar`

## Publishing to GitHub

```bash
task publish-keycloak-custom-mapper
```

This will create a GitHub release and upload the JAR to:
`https://github.com/minbzk/base-images/releases/download/v1.0.0/keycloak-saml-nameid-mapper-1.0.0.jar`

## Deployment

The JAR needs to be in `/opt/keycloak/providers/` when Keycloak starts.

### Option 1: Extend Existing Init Container (Recommended)

Update your Keycloak deployment's init container to also download the custom mapper:

```yaml
initContainers:
  - name: keycloak-theme-puller
    command:
      - sh
      - -c
      - |
        # Download theme
        wget https://github.com/MinBZK/keycloak-theme/releases/download/v1.2.1/keycloak-nl-design-system.jar \
          -O /opt/keycloak/providers/keycloak-nl-design-system.jar

        # Download custom mapper (after publishing with task publish-keycloak-custom-mapper)
        wget https://github.com/minbzk/base-images/releases/download/v1.0.0/keycloak-saml-nameid-mapper-1.0.0.jar \
          -O /opt/keycloak/providers/keycloak-saml-nameid-mapper.jar
    image: busybox:1.37.0
    securityContext:
      runAsUser: 0
    volumeMounts:
      - mountPath: /opt/keycloak/providers/
        name: keycloak-provider
```

Then restart Keycloak:
```bash
kubectl rollout restart deployment/keycloak-dpl -n keycloak
```

### Option 2: Host JAR in Git Repository

1. Create `custom-providers` directory in your infra repo
2. Add the JAR file to git (or git LFS)
3. Use ConfigMap or init container to copy it

Example with ConfigMap:

```bash
# Create ConfigMap from JAR
kubectl create configmap keycloak-custom-mapper \
  --from-file=keycloak-saml-nameid-mapper.jar=target/keycloak-saml-nameid-mapper-1.0.0.jar \
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
kubectl cp target/keycloak-saml-nameid-mapper-1.0.0.jar \
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
