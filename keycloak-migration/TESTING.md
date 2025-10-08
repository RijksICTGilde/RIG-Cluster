# Testing the Custom Keycloak Mapper

## Quick Test with Docker (Recommended)

The easiest way to verify the custom mapper is loaded by Keycloak:

### 1. Build and Test

```bash
# Build the JAR and run tests
task test-keycloak-custom-mapper

# Start Keycloak in Docker with the custom mapper
task test-keycloak-custom-mapper-docker
```

This will:
- Build the JAR file
- Start Keycloak 26.0.0 in Docker
- Mount the JAR into `/opt/keycloak/providers/`
- Wait for Keycloak to start

### 2. Verify in Keycloak UI

1. Open http://localhost:8080
2. Login with `admin` / `admin`
3. Create a test realm (e.g., "test-realm")
4. Go to **Identity Providers** → **Add provider** → **SAML v2.0**
5. Fill in minimal config:
   - **Alias**: `test-saml`
   - **Service Provider Entity ID**: `http://localhost:8080/realms/test-realm`
   - **Single Sign-On Service URL**: `http://example.com/sso` (dummy value)
6. Save
7. Go to the **Mappers** tab
8. Click **Add mapper**
9. **Check the dropdown list** - you should see:
   - ✅ **Unrestricted XPath Attribute Importer** ← Your custom mapper!
   - Standard mappers (Attribute Importer, Username Template Importer, etc.)

### 3. Test the Mapper Configuration

Create a test mapper:

- **Name**: Test NameID Extraction
- **Sync Mode Override**: FORCE
- **XPath Expression**: `//*[local-name()='Subject']/*[local-name()='NameID']/text()`
- **User Attribute Name**: `test_nameid`

If the mapper saves successfully, the JAR is working! ✅

### 4. Clean Up

```bash
docker rm -f keycloak-test
```

## Testing with Local Kind Cluster

To test with the full GitOps setup (ArgoCD deployment):

### Option A: Standalone Docker Test First

Always test with Docker first (see above) before integrating with Kind cluster.

### Option B: Integration with Kind Cluster

For this, you need to:

1. **Publish the JAR** to GitHub releases:
   ```bash
   task publish-keycloak-custom-mapper
   ```

2. **Update the deployment** in `infrastructure/bootstrap/infrastructure/keycloak/controller/base/deployment.yaml`:
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
           wget https://github.com/minbzk/base-images/releases/download/v1.0.0/keycloak-saml-nameid-mapper-1.0.0.jar
           cp keycloak-saml-nameid-mapper-1.0.0.jar /opt/keycloak/providers/
   ```

3. **Commit and let ArgoCD sync**:
   ```bash
   git add infrastructure/bootstrap/infrastructure/keycloak/controller/base/deployment.yaml
   git commit -m "Add custom mapper to Keycloak"
   git push
   ```

4. **Wait for ArgoCD** to detect and apply the change

5. **Verify in Keycloak UI** (same steps as Docker test)

## Troubleshooting

### Mapper doesn't appear in UI

1. **Check if JAR is in the pod**:
   ```bash
   kubectl exec -n rig-system deployment/keycloak -- ls -la /opt/keycloak/providers/
   ```

   You should see:
   ```
   keycloak-nl-design-system.jar
   keycloak-saml-nameid-mapper-1.0.0.jar  ← This one!
   ```

2. **Check Keycloak logs**:
   ```bash
   kubectl logs -n rig-system deployment/keycloak | grep -i "mapper\|provider"
   ```

3. **Enable debug logging**:
   ```bash
   kubectl set env deployment/keycloak \
     KC_LOG_LEVEL="INFO,nl.minbzk.rig.keycloak.mapper:DEBUG" \
     -n rig-system
   ```

4. **Check SPI registration**:
   The mapper should be registered via the SPI file at:
   `META-INF/services/org.keycloak.broker.provider.IdentityProviderMapper`

   Verify in the JAR:
   ```bash
   jar tf keycloak-migration/custom-mapper/target/keycloak-saml-nameid-mapper-1.0.0.jar | grep META-INF
   ```

### Docker test fails

1. **Check if port 8080 is already in use**:
   ```bash
   lsof -i :8080
   ```

2. **Check Docker logs**:
   ```bash
   docker logs keycloak-test
   ```

3. **Rebuild the JAR**:
   ```bash
   cd keycloak-migration/custom-mapper
   mvn clean package
   ```

## Expected Results

✅ **Success indicators**:
- Docker container starts without errors
- Keycloak UI accessible at http://localhost:8080
- Custom mapper appears in mapper dropdown list
- Mapper configuration saves successfully
- No errors in Keycloak logs

❌ **Failure indicators**:
- Keycloak fails to start
- Custom mapper not in dropdown
- Error saving mapper configuration
- ClassNotFound or NoClassDefFound errors in logs

## Next Steps After Successful Test

1. Document the mapper configuration for your use case
2. Publish to GitHub releases: `task publish-keycloak-custom-mapper`
3. Update production Keycloak deployment
4. Configure the actual XPath expression for your SAML NameID extraction
5. Add protocol mapper to override `sub` claim with the extracted attribute
