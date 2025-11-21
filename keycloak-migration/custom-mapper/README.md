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
2. Create or update the GitHub release v1.0.0
3. Upload the JAR as a release asset
4. Verify the download URL

The JAR will be available at:
`https://github.com/RijksICTGilde/RIG-Cluster/releases/download/v1.0.0/keycloak-saml-nameid-mapper-1.0.0.jar`

### Manual Publishing Steps

If you prefer to publish manually:

1. **Build the JAR**:
   ```bash
   task build-keycloak-custom-mapper
   ```

2. **Create GitHub Release**:
   ```bash
   cd keycloak-migration/custom-mapper
   gh release create v1.0.0 \
     --repo "RijksICTGilde/RIG-Cluster" \
     --title "Keycloak SAML NameID Mapper v1.0.0" \
     --notes "Custom mapper for extracting SAML NameID to user attributes" \
     target/keycloak-saml-nameid-mapper-1.0.0.jar
   ```

3. **Verify Release**:
   ```bash
   curl -sI https://github.com/RijksICTGilde/RIG-Cluster/releases/download/v1.0.0/keycloak-saml-nameid-mapper-1.0.0.jar
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
        wget https://github.com/RijksICTGilde/RIG-Cluster/releases/download/v1.0.0/keycloak-saml-nameid-mapper-1.0.0.jar
        cp keycloak-saml-nameid-mapper-1.0.0.jar /opt/keycloak/providers/
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
