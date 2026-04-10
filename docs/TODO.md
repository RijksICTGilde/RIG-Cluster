# TODO - RIG Cluster Platform

## High Priority

### Multi-Cluster Keycloak Credential Management
**Issue**: Both local and production operations managers update the same `projects/wies.yaml` file to store Keycloak credentials, which can cause Git conflicts and credential overwrites.

**Context**:
- Local ops manager processes deployments with `cluster: local`
- Production ops manager processes deployments with `cluster: odcn-production`
- Both try to write to `config.keycloak.{deployment-name}` in the same project file

**Potential Solutions**:
1. **Add Git conflict handling** in project file writing with retry logic
2. **Use deployment-specific branches** for storing credentials (e.g., `local/credentials`, `production/credentials`)
3. **Separate config files per cluster** (e.g., `projects/wies-local.yaml`, `projects/wies-production.yaml`)
4. **Implement proper Git locking/coordination** between operations managers
5. **Use external credential store** (Vault, K8s secrets) instead of project files

**Priority**: High - Can cause data loss and deployment failures
**Estimated Effort**: Medium
**Dependencies**: None

---

## Medium Priority

### Keycloak HTTP/HTTPS Protocol Detection
**Issue**: Keycloak generating HTTP URLs when accessed via HTTPS, causing Content-Security-Policy errors.

**Context**:
- Local cluster needs to support both HTTP (internal pod access) and HTTPS (browser access with localStorage)
- Current configuration: `KC_PROXY_HEADERS=xforwarded`, `KC_HOSTNAME=keycloak.kind`
- CSP error: "frame-src" blocked loading `http://keycloak.kind/...` when accessing via HTTPS

**Symptoms**:
- Mixed content errors (HTTP resources loaded on HTTPS pages)
- 3p-cookies frame loading over HTTP instead of HTTPS

**Attempted Solutions**:
1. Changed `KC_HOSTNAME` from `http://keycloak.kind` to `keycloak.kind` (no protocol)
2. Changed `KC_PROXY=edge` to `KC_PROXY_HEADERS=xforwarded`
3. Added `KC_HOSTNAME_STRICT=false` and `KC_HOSTNAME_PORT=-1`
4. Added `KC_PROXY_ADDRESS_FORWARDING=true`

**Next Steps**:
- Investigate why Keycloak is not reading X-Forwarded-Proto header correctly
- Verify nginx ingress controller is forwarding proxy headers properly
- Test with explicit KC_HOSTNAME_URL setting for HTTPS
- Document proper Keycloak configuration for dual HTTP/HTTPS support

**Priority**: Medium - Blocks local development with HTTPS
**Estimated Effort**: Small
**Dependencies**: Nginx ingress controller configuration

---

### ~~Project Realm Admin Login~~ (RESOLVED)
**Resolution Date**: 2025-11-21

**Issue**: Project realm admin users could not access admin console - getting blank page with 403 Forbidden on `/admin/serverinfo`.

**Root Cause**: Admin users were incorrectly created in master realm with client roles, requiring them to access master realm admin console which needs master realm permissions.

**Solution Implemented**:
- Refactored `_setup_project_keycloak_realm()` in `keycloak_manager.py:795-806`
- Admin users now created WITHIN their project realm (not master realm)
- Assigned built-in `realm-admin` composite role instead of client roles
- Project realm admins now access: `https://keycloak.kind/admin/{realm-name}/console/`

**Changes Made**:
```python
# OLD: Created in master realm
user_info = await keycloak.create_user(
    realm_name="master", username=admin_username, ...
)
await keycloak.assign_realm_management_role(...)

# NEW: Created in project realm
user_info = await keycloak.create_user(
    realm_name=realm_name, username=admin_username, ...
)
await keycloak.assign_realm_roles_to_user(
    realm_name=realm_name, user_id=user_info["id"], role_names=["realm-admin"]
)
```

**Testing**: Projects creating new realms will automatically get properly configured admin users

---

### PostgreSQL Database Image Upgrade Behavior
**Issue**: PostgreSQL database clusters (CloudNativePG) are not automatically recreated when the image version changes in the project configuration.

**Context**:
- When `namespace-postgresql-database` service config is updated with a new image (e.g., `postgres:16` → `postgres:17`)
- The infrastructure manifests are regenerated and committed to Git
- ArgoCD syncs the changes, but the PostgreSQL cluster may not automatically upgrade

**Questions to Investigate**:
1. How does CloudNativePG handle image updates in cluster manifests?
2. Does it perform in-place upgrades, or does the cluster need to be recreated?
3. What is the proper upgrade procedure for PostgreSQL major/minor versions?
4. Do we need to implement manual intervention for major version upgrades?
5. Should we add pre-upgrade backup logic before changing images?

**Current Behavior**: Unknown/Undocumented
- Need to verify what happens when image field changes in PostgreSQL Cluster resource
- May need to add validation or warnings for major version changes
- May need to implement backup/restore workflow for major upgrades

**Priority**: Medium - Important for production database management
**Estimated Effort**: Medium - Requires CloudNativePG documentation review and testing
**Dependencies**: CloudNativePG operator behavior

---

### [Add other TODOs here as they come up]

---

## Notes
- Created: 2025-08-18
- Last Updated: 2025-08-18
- Maintainer: Operations Team
