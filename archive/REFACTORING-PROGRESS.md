# Distributed Operations Manager Refactoring - Progress Report

**Date:** 2025-10-21
**Objective:** Implement Option B (Distributed Operations Manager) with consistent CLUSTER_MANAGER filtering, eliminate code duplication, and enforce separation of concerns.

---

## Architecture Decision: Option B - Distributed Operations Manager

Each operations-manager instance manages resources **ONLY** for its configured `CLUSTER_MANAGER` cluster.

**Key Principle:**
- All methods must filter by `CLUSTER_MANAGER` by default
- Each cluster has its own operations-manager instance
- No cross-cluster resource creation

---

## ✅ COMPLETED WORK

### Phase 1: Helper Methods Added to ProjectManager ✓

**File:** `python/opi/manager/project_manager.py` (lines 141-201)

Added centralized data access methods:

```python
async def get_deployments(
    self,
    cluster_filter: bool = True,  # Defaults to True for Option B
    deployment_name: str | None = None
) -> list[dict[str, Any]]

async def get_deployment_by_name(self, deployment_name: str) -> dict[str, Any] | None

async def get_repositories(self) -> list[dict[str, Any]]

async def get_components(self) -> list[dict[str, Any]]
```

**Impact:** Eliminates ~50+ instances of duplicate `project_data.get("deployments")` and filtering logic.

---

### Phase 5: ProjectManager Direct Access Elimination ✓

**Files Changed:** `python/opi/manager/project_manager.py`

#### Replaced ALL 14 instances of direct name access:
- ❌ OLD: `project_name = project_data.get("name")` or `project_data["name"]`
- ✅ NEW: `project_name = await self.get_name()`

**Lines changed:** 396, 677, 690, 911, 992, 1076-1077, 1654, 1674, 1712, 1790, 1917, 2537, 3336

#### Replaced 8+ instances of direct deployments access:
- ❌ OLD: `deployments = project_data.get("deployments", [])`
- ✅ NEW: `deployments = await self.get_deployments(cluster_filter=True)`

**Lines changed:** 679-680, 695, 919, 1080, 1483-1484, 1683, 2540, 3339

#### Major Method Refactorings:

**1. `has_deployments_for_current_cluster()` - Simplified from 50 lines to 3 lines:**
```python
# BEFORE: 50 lines of manual filtering
async def has_deployments_for_current_cluster(self) -> bool:
    project_data = await self.get_contents()
    project_name = project_data["name"]
    deployments = project_data.get("deployments", [])
    # ... 40+ lines of filtering logic ...

# AFTER: 3 lines using helper
async def has_deployments_for_current_cluster(self) -> bool:
    current_cluster_deployments = await self.get_deployments(cluster_filter=True)
    return bool(current_cluster_deployments)
```

**2. `_create_argocd_application()` - Now respects CLUSTER_MANAGER:**
```python
# BEFORE: No cluster filtering, manual deployment_name filtering
deployments = project_data.get("deployments", [])
if deployment_name:
    deployments = [d for d in deployments if d.get("name") == deployment_name]

# AFTER: Built-in filtering
deployments = await self.get_deployments(cluster_filter=True, deployment_name=deployment_name)
```

**3. `_get_project_keycloak_config_for_cluster()` - Made async, removed project_data parameter:**
```python
# BEFORE: Sync method taking project_data
def _get_project_keycloak_config_for_cluster(
    self, project_data: dict[str, Any], cluster: str
) -> dict[str, Any] | None:
    project_name = project_data.get("name")

# AFTER: Async, uses helper methods
async def _get_project_keycloak_config_for_cluster(self, cluster: str) -> dict[str, Any] | None:
    project_name = await self.get_name()
```

**Callers updated:** Lines 2880, 2994 (changed to `await`)

---

## 📋 REMAINING WORK

### Phase 2: Move ArgoCD Methods to ArgoManager

**Current State:** These methods are in `project_manager.py` but should be in `argo_manager.py`:

1. **`_create_argocd_application()`** (project_manager.py:909-983)
   - Move to `argo_manager.py` as `create_applications()`
   - Already uses `get_deployments()` with cluster filtering ✓
   - Needs to be made public method

2. **`_create_argocd_kustomization_file()`** (project_manager.py:2525-2555)
   - Move to `argo_manager.py` as `create_kustomization_files()`
   - Currently uses `cluster_filter=False` - needs review
   - Should only process CLUSTER_MANAGER cluster

**Why:** Separation of concerns - all ArgoCD logic should be in ArgoManager.

---

### Phase 3: Fix ArgoManager Methods for Option B

**File:** `python/opi/manager/argo_manager.py`

#### 3.1 Fix `create_repository_secrets()` (lines 33-133)

**Current Bugs:**
```python
# Lines 60-67: BUG - Collects all clusters but only uses first!
clusters_used = set()
for deployment in deployments:
    clusters_used.add(deployment["cluster"])  # ALL clusters
cluster_name = next(iter(clusters_used))  # Uses FIRST only! 🐛

# Lines 77-78: UNUSED variables
username = repository.get("username", "")  # NEVER USED
password = repository.get("password", "")  # NEVER USED
```

**Required Changes:**
```python
async def create_repository_secrets(
    self,
    project_data: dict[str, Any],
    deployment_name: str | None = None  # Add parameter
) -> None:
    # Get deployments for CLUSTER_MANAGER only
    deployments = await self.project_manager.get_deployments(
        cluster_filter=True,
        deployment_name=deployment_name
    )

    if not deployments:
        logger.warning(f"No deployments for cluster {settings.CLUSTER_MANAGER}")
        return

    # Use CLUSTER_MANAGER directly
    cluster_name = settings.CLUSTER_MANAGER
    project_name = await self.project_manager.get_name()

    # Create secrets in THIS cluster's directory only
    project_dir = os.path.join(working_dir, cluster_name, project_name)

    # Remove lines 77-78 (unused variables)
```

#### 3.2 Fix `create_app_projects()` (lines 234-292)

**Current Issues:**
- No CLUSTER_MANAGER filtering
- No deployment_name parameter support
- Creates AppProjects for all clusters

**Required Changes:**
```python
async def create_app_projects(
    self,
    project_data: dict[str, Any],
    deployment_name: str | None = None  # Add parameter
) -> None:
    project_name = await self.project_manager.get_name()

    # Get deployments for THIS cluster only
    deployments = await self.project_manager.get_deployments(
        cluster_filter=True,
        deployment_name=deployment_name
    )

    if not deployments:
        logger.warning(f"No deployments for cluster {settings.CLUSTER_MANAGER}")
        return

    # Simplified - only one cluster now
    cluster_name = settings.CLUSTER_MANAGER
    namespaces = {d.get("namespace") for d in deployments}

    # Create one AppProject for this cluster with these namespaces
    # ... rest of method
```

---

### Phase 4: Update `create_argocd_resources()` Orchestration

**File:** `python/opi/manager/project_manager.py` (lines 1646-1663)

**Current State:**
```python
async def create_argocd_resources(self, deployment_name: str | None = None) -> None:
    project_data = await self.get_contents()
    project_name = await self.get_name()  # ✓ Already fixed

    # These don't respect deployment_name parameter! ❌
    await self._argo_manager.create_repository_secrets(project_data)
    await self._argo_manager.create_app_projects(project_data)
    await self._create_argocd_application(deployment_name)  # Only this one does!
    await self._create_argocd_kustomization_file()
```

**Required Changes:**
```python
async def create_argocd_resources(self, deployment_name: str | None = None) -> None:
    project_name = await self.get_name()
    logger.info(f"Creating ArgoCD resources for {project_name} on cluster {settings.CLUSTER_MANAGER}")

    project_data = await self.get_contents()

    # All methods now respect deployment_name and CLUSTER_MANAGER
    await self._argo_manager.create_repository_secrets(project_data, deployment_name)
    await self._argo_manager.create_app_projects(project_data, deployment_name)
    await self._argo_manager.create_applications(project_data, deployment_name)  # Moved method
    await self._argo_manager.create_kustomization_files(project_data, deployment_name)  # Moved method

    await (await self.get_git_connector_for_argocd()).commit_and_push(
        f"Added ArgoCD resources for project {project_name} on cluster {settings.CLUSTER_MANAGER}"
    )
```

---

### Phase 5: Replace Direct Access in Sub-Managers

**Files to Update:**

#### 5.1 argo_manager.py (2 instances of `project_data.get("name")`)
```bash
# Lines to check
opi/manager/argo_manager.py:47
opi/manager/argo_manager.py:244
```

#### 5.2 database_manager.py (2 instances)
```bash
# Find with: grep -n 'project_data.get("name")' opi/manager/database_manager.py
```

#### 5.3 minio_manager.py (3 instances)
```bash
# Find with: grep -n 'project_data.get("name")' opi/manager/minio_manager.py
```

#### 5.4 keycloak_manager.py (2 instances)
```bash
# Find with: grep -n 'project_data.get("name")' opi/manager/keycloak_manager.py
```

#### 5.5 clone_manager.py (1 instance)
#### 5.6 delete_project_manager.py (1 instance)

**Pattern for all sub-managers:**
```python
# BEFORE
project_name = project_data.get("name")

# AFTER
project_name = await self.project_manager.get_name()
```

---

### Phase 6: Replace `project_data.get("deployments")` in Sub-Managers

**Estimated:** ~20 more instances across:
- database_manager.py
- minio_manager.py
- keycloak_manager.py
- delete_project_manager.py

**Pattern:**
```python
# BEFORE
deployments = project_data.get("deployments", [])
for deployment in (d for d in deployments if d.get("cluster") == settings.CLUSTER_MANAGER):

# AFTER
deployments = await self.project_manager.get_deployments(cluster_filter=True)
for deployment in deployments:
```

---

### Phase 7: Update Documentation

**Files to Update:**

1. **Remove outdated TODOs:**
   - `project_manager.py:861` - "TODO: rethink logic for checking the cluster_manager all the time"
   - `project_manager.py:1667` - "TODO: rethink if all deployments should be handled or only the current cluster"

   Replace with:
   ```python
   # Architecture: Distributed Operations Manager (Option B)
   # Each instance manages only CLUSTER_MANAGER cluster resources
   ```

2. **Update method docstrings:**
   - `create_repository_secrets()` - Clarify single-cluster behavior
   - `create_app_projects()` - Clarify single-cluster behavior
   - All ArgoCD methods - Add CLUSTER_MANAGER filtering note

3. **Update CLAUDE.md files:**
   - Document Option B architecture
   - Document helper methods usage
   - Document cluster filtering defaults

---

### Phase 8: Testing (Post-Refactoring)

#### Unit Tests to Add/Update:
```python
# test_project_manager.py
async def test_get_deployments_filters_by_cluster():
    """Verify cluster_filter=True returns only CLUSTER_MANAGER deployments"""

async def test_get_deployments_respects_deployment_name():
    """Verify deployment_name parameter filters correctly"""

async def test_argocd_resources_respect_cluster_manager():
    """Verify ArgoCD resources only created for CLUSTER_MANAGER"""

# test_argo_manager.py
async def test_repository_secrets_single_cluster():
    """Verify repository secrets created only in CLUSTER_MANAGER directory"""

async def test_app_projects_single_cluster():
    """Verify AppProjects created only for CLUSTER_MANAGER"""
```

#### Manual Testing Checklist:
- [ ] Single cluster project works correctly
- [ ] Multi-cluster project only manages CLUSTER_MANAGER resources
- [ ] deployment_name parameter filters correctly end-to-end
- [ ] Repository secrets created ONLY in correct cluster directory
- [ ] No cross-cluster resource leakage

---

### Phase 9: Code Quality Final Check

```bash
# After all changes
cd python

# Fix auto-fixable issues
ruff check opi/manager/ --fix
ruff format opi/manager/

# Type checking
pyright opi/manager/

# Verify no direct access remains
grep -r 'project_data.get("name")' opi/manager/
grep -r 'project_data\["name"\]' opi/manager/
grep -r 'project_data.get("deployments")' opi/manager/
```

---

## Key Files Modified So Far

1. **python/opi/manager/project_manager.py**
   - Added 4 helper methods (lines 141-201)
   - Replaced 14 instances of direct name access
   - Replaced 8+ instances of direct deployments access
   - Refactored 3 major methods

## Files Pending Changes

1. **python/opi/manager/argo_manager.py** - Phase 3 (fix CLUSTER_MANAGER filtering)
2. **python/opi/manager/database_manager.py** - Phase 5 (replace direct access)
3. **python/opi/manager/minio_manager.py** - Phase 5 (replace direct access)
4. **python/opi/manager/keycloak_manager.py** - Phase 5 (replace direct access)
5. **python/opi/manager/clone_manager.py** - Phase 5 (replace direct access)
6. **python/opi/manager/delete_project_manager.py** - Phase 5 (replace direct access)

---

## Critical Implementation Notes

### Cluster Filtering Guidelines

**When to use `cluster_filter=True` (DEFAULT):**
- Processing/creating resources (99% of cases)
- Applying manifests
- Creating namespaces
- Managing databases/MinIO/Keycloak
- Any operation that changes state

**When to use `cluster_filter=False` (RARE):**
- Validation/reporting across all clusters
- Collecting cluster statistics
- Building kustomization files that reference all clusters
- Checking if project has any deployments at all

### Method Signature Pattern

All ArgoCD methods should follow this pattern:
```python
async def method_name(
    self,
    project_data: dict[str, Any],  # Will eventually be removed
    deployment_name: str | None = None  # For partial updates
) -> None:
    project_name = await self.project_manager.get_name()
    deployments = await self.project_manager.get_deployments(
        cluster_filter=True,
        deployment_name=deployment_name
    )

    if not deployments:
        logger.warning(f"No deployments for cluster {settings.CLUSTER_MANAGER}")
        return
```

---

## Code Quality Status

**Ruff Check:** ✓ No new critical issues (pre-existing warnings only)
**Pyright Check:** ✓ No new type errors (pre-existing import issues only)
**Syntax:** ✓ All changes compile successfully

---

## Estimated Completion

- **Completed:** Phase 1 (100%), Phase 5 ProjectManager (100%)
- **Remaining:** Phases 2-4 (ArgoManager), Phase 5 (Sub-managers), Phase 6-9
- **Total Progress:** ~40% complete
- **Estimated Remaining Work:** 3-4 hours for experienced developer

---

## Next Steps for Continuation

1. **Start with Phase 2:** Move ArgoCD methods from ProjectManager to ArgoManager
   - Move `_create_argocd_application` → `create_applications`
   - Move `_create_argocd_kustomization_file` → `create_kustomization_files`

2. **Then Phase 3:** Fix `create_repository_secrets` and `create_app_projects`
   - Add CLUSTER_MANAGER filtering
   - Remove multi-cluster logic
   - Fix bugs identified in analysis

3. **Then Phase 4:** Update `create_argocd_resources` orchestration

4. **Then Phase 5-6:** Systematically replace direct access in all sub-managers

5. **Finally Phase 7-9:** Documentation, testing, and quality checks

---

## Questions for Next Context

- Should we continue with Phase 2 (moving methods to ArgoManager)?
- Do we need to update any tests immediately?
- Should we create a separate branch for this refactoring?

---

**End of Progress Report**
