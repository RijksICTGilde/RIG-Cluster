# Future: Storage Configuration Restructure

**Status**: Proposed
**Priority**: Low (cleanup/consistency improvement)
**Created**: 2026-02-06

## Problem Statement

Storage configuration for components is currently split across two locations in project YAML files:

### 1. Component-level `storage` block (defines what storage exists)
```yaml
components:
- name: frontend
  type: deployment
  uses-services:
  - persistent-storage    # Declares the component uses this service
  storage:                 # Defines the actual storage volumes
  - name: data
    type: persistent
    size: 250Mi
    mount-path: /data
  - name: temp
    type: ephemeral
    size: 250Mi
    mount-path: /tmp
```

### 2. Deployment-level `services.persistent-storage` block (defines generation for PVC versioning)
```yaml
deployments:
- name: staging2
  components:
  - reference: frontend
    image: ghcr.io/example/app:latest
    services:
      persistent-storage:
      - reference: data
        config:
          generation: 1
```

### Problems

1. **Scattered configuration**: Storage-related settings are in two different places, making it confusing to understand and maintain.
2. **Inconsistent with other services**: Database and MinIO follow a cleaner `uses-services` with `services.{type}` pattern where generation is tracked alongside service configuration.
3. **Confusing `uses-services` vs `storage`**: The component declares `uses-services: [persistent-storage]` but the actual storage definition is in a separate `storage:` block.
4. **Generation tracking mismatch**: The `storage:` block defines size/mount-path, but generation is tracked separately in `services.persistent-storage`.

## Proposed Structure

Consolidate storage configuration under the `uses-services` pattern, similar to how database and minio work:

### Component definition (defines what services are used)
```yaml
components:
- name: frontend
  type: deployment
  uses-services:
  - publish-on-web
  - keycloak
  - persistent-storage:
    - name: data
      type: persistent
      size: 250Mi
      mount-path: /data
    - name: temp
      type: ephemeral
      size: 250Mi
      mount-path: /tmp
```

### Deployment definition (tracks per-deployment runtime config like generation)
```yaml
deployments:
- name: staging2
  components:
  - reference: frontend
    image: ghcr.io/example/app:latest
    services:
      persistent-storage:
      - reference: data
        config:
          generation: 1
          backup_enabled: true  # Override project-level backup setting
```

---

## Implementation

### Phase 1: Dual-Read Support (Backwards Compatible)

**File**: `opi/handlers/project_file_handler.py` (modify)

Add a helper that reads storage config from either the old or new location:

```python
def extract_storage_config(self, component: dict) -> list[dict]:
    """
    Read storage configuration from either new (uses-services) or old (storage block) format.
    New format takes priority if both exist.
    """
    # Try new format: uses-services list with persistent-storage dict entry
    uses_services = component.get("uses-services", [])
    for service in uses_services:
        if isinstance(service, dict) and "persistent-storage" in service:
            return service["persistent-storage"]

    # Fall back to old format: top-level storage block
    storage = component.get("storage", [])
    if storage:
        return storage

    return []


def has_legacy_storage_format(self, component: dict) -> bool:
    """Check if component uses the old separate storage block."""
    has_old = bool(component.get("storage", []))
    has_new = any(
        isinstance(s, dict) and "persistent-storage" in s
        for s in component.get("uses-services", [])
    )
    return has_old and not has_new
```

### Phase 2: Update Consumers to Use Dual-Read

**File**: `opi/manager/pvc_manager.py` (modify)

Replace direct `component.get("storage", [])` reads with the dual-read helper:

```python
# Before:
storage_volumes = component.get("storage", [])

# After:
storage_volumes = file_handler.extract_storage_config(component)
```

Affected methods in `pvc_manager.py`:
- `generate_pvc_manifests()` — generates PVC YAML
- `get_volume_mounts()` — generates volumeMounts for deployment template
- `get_volumes()` — generates volumes list for deployment template

**File**: `opi/manager/project_manager.py` (modify)

Same pattern — replace direct storage reads:

```python
# Anywhere that reads storage config:
storage = file_handler.extract_storage_config(component)
```

### Phase 3: Smart Value Functions for Storage

**File**: `opi/forms/editables/service_path.py` (modify)

Extend `smart_get_value` and `smart_set_value` to handle the new storage path:

```python
def smart_get_value(data: dict, yaml_path: str) -> Any:
    # Existing service path handling...

    # NEW: Handle storage paths
    if yaml_path.startswith("uses-services/persistent-storage"):
        component = _find_component(data, yaml_path)
        if component:
            return _extract_storage_from_uses_services(component)
        # Fall back to legacy
        return component.get("storage", [])

    # Existing standard path handling...
```

### Phase 4: Migration Helper

**File**: `opi/services/migration_service.py` (new)

```python
def migrate_storage_format(project_data: dict) -> tuple[dict, list[str]]:
    """
    Migrate project YAML from old storage format to new format.
    Returns (updated_data, list_of_changes_made).
    """
    changes = []
    components = project_data.get("components", [])

    for component in components:
        storage = component.get("storage", [])
        if not storage:
            continue

        uses_services = component.get("uses-services", [])

        # Check if already migrated
        already_migrated = any(
            isinstance(s, dict) and "persistent-storage" in s
            for s in uses_services
        )
        if already_migrated:
            continue

        # Remove "persistent-storage" string entry if present
        uses_services = [s for s in uses_services if s != "persistent-storage"]

        # Add structured persistent-storage entry
        uses_services.append({"persistent-storage": storage})
        component["uses-services"] = uses_services

        # Remove old storage block
        del component["storage"]

        changes.append(f"Migrated storage config for component '{component.get('name', '?')}'")

    return project_data, changes
```

**File**: `opi/api/router.py` (modify)

Add migration endpoint:

```python
@router.post("/api/projects/{project_name}/:migrate-storage-format")
@validate_master_api_key
async def migrate_storage_format(project_name: str) -> JSONResponse:
    """Migrate a project's storage config from old to new format."""
    project_data = await project_service.get_project(project_name)
    updated, changes = migrate_storage_format(project_data)

    if not changes:
        return JSONResponse(content={"status": "no_changes", "message": "Already in new format"})

    # Write back to git
    await project_service.update_project_file(project_name, updated,
        commit_message=f"chore: migrate storage config to uses-services format")

    return JSONResponse(content={"status": "migrated", "changes": changes})
```

### Phase 5: Wizard Form Update

**File**: `opi/forms/visualizers/wizard_sections.py` (modify)

Update the storage section in the component wizard to write to the new location:

```python
# In COMPONENTS_SECTION layout, update the storage fields:
Sequence(
    field_name="uses-services/persistent-storage",  # Changed from "storage"
    child_layout=[
        Fieldset(legend="Volume", children=[
            "name",
            "type",       # persistent | ephemeral
            "size",       # e.g. "250Mi"
            "mount-path", # e.g. "/data"
        ]),
    ],
)
```

### Phase 6: Deprecation Warnings

**File**: `opi/handlers/project_file_handler.py` (modify)

Add deprecation logging when old format is detected:

```python
def extract_storage_config(self, component: dict) -> list[dict]:
    # ... existing dual-read logic ...

    # Fall back to old format with deprecation warning
    storage = component.get("storage", [])
    if storage:
        logger.warning(
            "Component '%s' uses deprecated 'storage' block format. "
            "Migrate to 'uses-services.persistent-storage' format. "
            "See: POST /api/projects/{name}/:migrate-storage-format",
            component.get("name", "unknown"),
        )
        return storage

    return []
```

---

## Deprecation Timeline

| Phase | Action | When |
|-------|--------|------|
| 1 | Dual-read support — old format continues to work | Immediate |
| 2 | Wizard writes new format for new projects | Immediate |
| 3 | Deprecation warnings in logs for old format | After Phase 1 |
| 4 | Migration endpoint available | After Phase 1 |
| 5 | Batch migrate existing projects | 1 month after Phase 4 |
| 6 | Remove old format support | 3 months after Phase 5 |

---

## Files Summary

### New Files

| File | Purpose |
|------|---------|
| `opi/services/migration_service.py` | `migrate_storage_format()` function |

### Modified Files

| File | Change |
|------|--------|
| `opi/handlers/project_file_handler.py` | `extract_storage_config()` dual-read + deprecation warnings |
| `opi/manager/pvc_manager.py` | Use `extract_storage_config()` instead of direct reads |
| `opi/manager/project_manager.py` | Use `extract_storage_config()` instead of direct reads |
| `opi/forms/editables/service_path.py` | Handle `uses-services/persistent-storage` path |
| `opi/forms/visualizers/wizard_sections.py` | Update storage field paths in component layout |
| `opi/api/router.py` | Add `/:migrate-storage-format` endpoint |

---

## Testing

1. **Dual-read (old format)**: Project with `storage:` block at component level still works
2. **Dual-read (new format)**: Project with `uses-services.persistent-storage` works
3. **New project**: Creating a project via wizard writes to the new location
4. **Migration**: POST `/:migrate-storage-format` converts old to new format
5. **PVC generation**: PVCs are generated correctly from both old and new formats
6. **Volume mounts**: Deployment template gets correct volumeMounts from both formats
7. **Deprecation logging**: Old format logs a warning (check OPI logs)
8. **Round-trip**: Edit a migrated project in the wizard, verify storage config stays in new location

## Related Files

- `opi/handlers/project_file_handler.py` - Project file parsing
- `opi/manager/pvc_manager.py` - PVC manifest generation
- `opi/manager/project_manager.py` - Main orchestration
- Project YAML files in `projects/` directory
