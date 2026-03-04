# Plan: YAML Diff-Driven Deletion & Resource Cleanup

## Context

The `ProjectFileHandler` already compares the current project YAML against the previous git version using **DeepDiff** and produces a structured changes dictionary with `added`, `changed`, and `deleted` keys. The `ProjectManager.process_project_from_git()` method calls this analysis and logs the results, but currently **ignores deletions** - it processes the full project regardless of what changed (see TODO at `project_manager.py:2173`).

Meanwhile, `DeleteProjectManager` already knows how to delete entire deployments and entire projects, but there is **no mechanism for partial/incremental deletions** triggered by YAML changes (e.g., removing a single service from a component, removing a component from a deployment, or removing a deployment from the project file).

## Current Architecture

### Change Detection Pipeline (already working)

```
project_file_handler.py
  ├── get_previous_yaml_content()     # Gets previous YAML from git history
  ├── generate_yaml_diff()            # DeepDiff(previous, current, ignore_order=True)
  ├── extract_changes_from_diff()     # Structures into {added, changed, deleted}
  └── _parse_deepdiff_path()          # Converts DeepDiff paths to readable format
                                      #   e.g. "root['deployments'][0]['name']" -> "deployments.0.name"

project_manager.py
  ├── process_project_from_git()      # Calls analyze_project_changes, logs diff, then ignores it
  └── _analyze_deployment_changes()   # Filters changes to deployment-specific paths
```

### Deletion Pipeline (already working, but only for explicit API calls)

```
delete_project_manager.py
  ├── delete_project()                # Deletes entire project (all deployments + project file)
  └── delete_deployment()             # Deletes single deployment:
      ├── ArgoCD application file from GitOps repo
      ├── ArgoCD AppProject (if namespace not shared)
      ├── Repository secrets (if repo not shared)
      ├── ArgoCD application resource (waits for deletion)
      ├── Kubernetes namespace
      ├── Database resources (user, schema, database)
      ├── MinIO resources (user, bucket, policy)
      └── Deployment folder from infrastructure git repo
```

### Project YAML Hierarchy

```yaml
name: example-project           # PROJECT level
repositories: [...]             # PROJECT level
components:                     # PROJECT level (definitions)
  - name: frontend
    services: [database, minio]
    ports: [8080]
deployments:                    # DEPLOYMENT level
  - name: staging
    cluster: local
    repository: main-repo
    components:                 # COMPONENT level (instances within deployment)
      - reference: frontend
        image: "nginx:latest"
    services:                   # SERVICE level (instances within deployment)
      - reference: database
        config: { generation: 1 }
```

## What the Diff Tells Us

### DeepDiff Change Types Mapped to Actions

| DeepDiff Path Pattern | What Changed | Required Action |
|---|---|---|
| `deployments.[N]` (deleted) | Entire deployment removed | `delete_deployment()` |
| `deployments.[N].components.[M]` (deleted) | Component removed from deployment | Delete component manifests, ingress, related config |
| `deployments.[N].components.[M].services` or top-level service ref removed | Service removed from component | Delete service resources (DB/MinIO/Keycloak client) |
| `deployments.[N].name` (changed) | Deployment renamed | Delete old deployment, create new one (rename = delete + add) |
| `deployments.[N].components.[M].reference` (changed) | Component swapped | Delete old component resources, create new component |
| `components.[N]` (deleted) | Component definition removed | Delete from all deployments that reference it |
| `repositories.[N]` (deleted) | Repository removed | Clean up repo secrets if no deployments use it |

### Key Challenges

1. **List index instability**: DeepDiff with `ignore_order=True` matches list items by value, not index. Removing item `[0]` from a 3-item list shows as `iterable_item_removed` with the removed item's value, not just an index shift. This is actually helpful - we get the removed item's data directly.

2. **Rename detection**: DeepDiff sees a rename as a remove + add (two different items). We could detect renames by matching on other properties, but this adds complexity. Safer to treat as delete old + create new.

3. **Cascade logic**: Removing a component definition (`components.[N]`) should cascade to all deployments referencing it. Removing a deployment should cascade to its services.

4. **Service lifecycle**: Services like databases should NOT be auto-deleted when removed from YAML - data loss risk. Need a confirmation/safety mechanism.

## Proposed Approach

### Phase 1: Semantic Change Interpreter

Create a new module `opi/handlers/change_interpreter.py` that takes the raw DeepDiff changes and interprets them into **actionable operations**:

```python
class ChangeInterpreter:
    """Interprets structured YAML changes into actionable operations."""

    def interpret_changes(
        self,
        changes: dict[str, Any],        # {added, changed, deleted}
        current_yaml: dict[str, Any],
        previous_yaml: dict[str, Any],
    ) -> ChangeActions:
        """
        Returns:
            ChangeActions with:
              - deployments_to_delete: list[dict]   # full deployment data from previous YAML
              - deployments_to_add: list[dict]       # full deployment data from current YAML
              - components_to_remove: list[ComponentRemoval]  # (deployment_name, component_ref)
              - components_to_add: list[ComponentAddition]
              - services_to_remove: list[ServiceRemoval]  # (deployment_name, service_ref)
              - services_to_add: list[ServiceAddition]
              - renames: list[RenameOperation]        # detected rename pairs
              - safe_changes: list[str]               # value changes that just need reprocessing
        """
```

The interpreter would:
1. Walk `changes["deleted"]` paths and classify each by hierarchy level
2. Walk `changes["added"]` paths and classify similarly
3. Attempt to match delete+add pairs as potential renames (same structure, different name)
4. For each deleted deployment, extract the full deployment data from `previous_yaml`
5. For each deleted component reference, identify which deployment it belongs to

### Phase 2: Integrate with `process_project_from_git()`

In `project_manager.py`, after the existing change analysis:

```python
# Existing code (line 2158):
deployment_changes = self._analyze_deployment_changes(changes, current_yaml)

# New code:
change_actions = self._change_interpreter.interpret_changes(
    changes, current_yaml, previous_yaml
)

# Process deletions BEFORE processing additions
for deployment_data in change_actions.deployments_to_delete:
    await self._delete_project_manager.delete_deployment(
        project_name, deployment_data["name"]
    )

for removal in change_actions.components_to_remove:
    await self._handle_component_removal(removal)

for removal in change_actions.services_to_remove:
    await self._handle_service_removal(removal)

# Then process additions/changes as before
process_success = await self.process_project(deployment_name, force_clone)
```

### Phase 3: Safety Mechanisms

1. **Destructive change detection**: Before executing deletions, log a clear summary of what will be destroyed
2. **Service data protection**: For services with persistent data (database, minio), consider:
   - Requiring explicit confirmation via a flag in the YAML (e.g., `allow-delete: true`)
   - Or creating a backup before deletion
   - Or just deleting the Kubernetes resources but keeping the database/bucket data
3. **Dry-run mode**: Add ability to analyze changes without executing them (for UI preview)

### Phase 4: Component-Level Deletion Handler

New method needed since `DeleteProjectManager` only handles full deployment deletion:

```python
async def remove_component_from_deployment(
    self,
    project_name: str,
    deployment_name: str,
    component_reference: str,
) -> dict[str, Any]:
    """
    Remove a single component from a deployment.
    Cleans up:
    - Component manifests from infrastructure git repo
    - Ingress resources for the component
    - Keycloak client for the component (if SSO enabled)
    """
```

### Phase 5: Service-Level Deletion Handler

```python
async def remove_service_from_deployment(
    self,
    project_name: str,
    deployment_name: str,
    service_reference: str,
) -> dict[str, Any]:
    """
    Remove a single service from a deployment.
    Cleans up:
    - Database user/schema (if database service)
    - MinIO user/bucket/policy (if minio service)
    - Service secrets from Kubernetes
    """
```

## Testing Strategy

### Unit Tests for Change Interpreter

Test with concrete YAML diffs to verify correct classification:

```python
def test_deployment_removed():
    previous = {"deployments": [{"name": "staging", ...}, {"name": "prod", ...}]}
    current = {"deployments": [{"name": "prod", ...}]}
    # Should detect staging as deployments_to_delete

def test_component_removed_from_deployment():
    previous = {"deployments": [{"name": "staging", "components": [
        {"reference": "frontend"}, {"reference": "backend"}
    ]}]}
    current = {"deployments": [{"name": "staging", "components": [
        {"reference": "frontend"}
    ]}]}
    # Should detect backend as components_to_remove for staging

def test_service_removed():
    previous = {"deployments": [{"name": "staging", "services": [
        {"reference": "database"}, {"reference": "minio"}
    ]}]}
    current = {"deployments": [{"name": "staging", "services": [
        {"reference": "database"}
    ]}]}
    # Should detect minio as services_to_remove for staging

def test_deployment_renamed():
    previous = {"deployments": [{"name": "staging", "cluster": "local", "components": [...]}]}
    current = {"deployments": [{"name": "production", "cluster": "local", "components": [...]}]}
    # Should detect as rename (delete staging + add production)

def test_no_changes():
    same = {"deployments": [{"name": "staging"}]}
    # Should return empty actions
```

### Integration Tests

Mock the connectors and verify the correct deletion methods are called in the right order.

## Implementation Order

1. **`ChangeInterpreter`** - Pure logic, fully testable with unit tests
2. **Unit tests for interpreter** - Validate all change type classifications
3. **Component removal handler** - New capability in `DeleteProjectManager`
4. **Service removal handler** - New capability in `DeleteProjectManager`
5. **Wire into `process_project_from_git()`** - Connect the pipeline
6. **Safety mechanisms** - Dry-run, confirmation, data protection
7. **Integration tests** - End-to-end with mocked connectors

## Open Questions

1. **Should service data (databases, buckets) be auto-deleted when removed from YAML?** This is destructive. Options:
   - Never auto-delete data services (safest, require explicit API call)
   - Auto-delete with a configurable grace period
   - Add a `retain-data: false` flag to opt-in to auto-deletion

2. **How to handle renames?** Options:
   - Treat as delete + create (simple, but loses data)
   - Detect renames and perform in-place updates (complex, preserves data)
   - Require explicit rename operations via API (safest)

3. **Should the interpreter run in the current `process_project_from_git()` flow, or should it be a separate step triggered by the UI?** Running inline means every git push triggers potential deletions. A separate step gives the user a chance to review.

4. **What about `type_changes` from DeepDiff?** E.g., changing a service from a dict to a list format. Currently not handled in `extract_changes_from_diff()`.
