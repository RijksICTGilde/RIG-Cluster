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

### [Add other TODOs here as they come up]

---

## Notes
- Created: 2025-08-18
- Last Updated: 2025-08-18
- Maintainer: Operations Team