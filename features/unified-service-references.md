# Unified Service References

**Status**: Implemented
**Created**: 2026-03-04

## Summary

Unifies how services are referenced across all levels of the project YAML, introduces schema versioning, and auto-migrates project files at load time.

## What Changed

### Before (schema v1)
```yaml
# Component level used "uses-services" (plain strings) + separate "storage" block
components:
- name: frontend
  uses-services:
  - publish-on-web
  - keycloak
  - persistent-storage
  storage:
  - name: data
    type: persistent
    size: 250Mi
    mount-path: /data
```

### After (schema v2)
```yaml
schema-version: 2

# Component level now uses "services" (same mixed string/dict pattern as root)
components:
- name: frontend
  services:
  - publish-on-web
  - keycloak
  - persistent-storage:
      config:
      - name: data
        size: 250Mi
        mount-path: /data
```

### Key changes

| Before | After |
|--------|-------|
| `uses-services` on components | `services` (same format as root-level) |
| Separate `storage:` block | Storage config under service name dict entry |
| `type: persistent` / `type: ephemeral` fields | Implied by service name (`persistent-storage` / `temp-storage`) |
| No schema version | `schema-version: 2` at root |

### What stays the same

- Root-level `services:` - unchanged
- Deployment-level `services:` with `reference:` pattern - unchanged
- `uses-components` - left unchanged (not used yet)
- Helm chart and helmfile `uses-services` renamed to `services` (same as components)

## Auto-Migration

When OPI loads a project file, it detects the schema version:

1. If `schema-version` field exists, use its value
2. Otherwise, detect v1 by presence of `uses-services` on components/helm-charts/helmfiles
3. If v1 detected, auto-migrate to v2 and commit back to git

Migration is transparent - no manual intervention needed.

## Schema Versioning

The `schema-version` field at the project root enables future migrations:

```yaml
schema-version: 2
name: my-project
```

Future migrations will increment this number and run in sequence (v2 → v3 → v4 etc).

## Implementation Files

### New
- `opi/services/schema_migration.py` - Migration framework with `detect_schema_version()`, `migrate_to_latest()`, and v1→v2 migration logic

### Modified
- `opi/manager/project_manager.py` - Auto-migration hook in `process_project_from_git()`
- `opi/handlers/project_file_handler.py` - Updated all read sites to use `component["services"]` format
- `opi/utils/project_utils.py` - Updated write paths to produce v2 format
- `opi/forms/editables/fields/components.py` - Updated editable paths using `{K}` filter syntax for storage
- `opi/forms/editables/path.py` - Extended with `{K}` dict-key and `{F=V}` field-match filter syntax
- Various managers and routers - Updated to read from new format

## Path Filter Syntax

The path resolution system (`path.py`) was extended with two new filter expressions to navigate mixed string/dict lists natively:

### `{K}` - Dict-key filter
Finds a dict whose top-level key matches `K` in a mixed list and descends into `dict[K]`.

```python
# Data: services: ["publish-on-web", {"keycloak": {"config": {"template": "sso-only"}}}]
get_value(data, "services{keycloak}/config/template")  # → "sso-only"
```

On write, promotes a matching string to `{K: {}}` or appends a new entry if not found.

### `{F=V}` - Field-match filter
Finds the first dict in a list where `dict[F] == V` and descends into that dict.

```python
# Data: config: [{"name": "data", "size": "250Mi"}, {"name": "uploads", "size": "1Gi"}]
get_value(data, "config{name=data}/size")  # → "250Mi"
```

On write, appends `{F: V}` if no match exists.

### Storage editables

Storage editables use the `{K}` filter to navigate directly into the service entries:

```python
# Persistent storage
PERSISTENT_STORAGE_NAME_EDITABLE = Editable(
    yaml_path="components[*]/services{persistent-storage}/config[*]/name",
)

# Temp storage
TEMP_STORAGE_NAME_EDITABLE = Editable(
    yaml_path="components[*]/services{temp-storage}/config[*]/name",
)
```

This eliminates the need for any prepare/enforce workaround - the form system reads and writes storage data directly in the v2 structure.

## Testing

- `tests/test_schema_migration.py` - 21 tests covering detection, migration, edge cases
- `tests/test_path_filters.py` - 33 tests covering `{K}` and `{F=V}` filter syntax
- All existing form tests pass without regression
- Verified against all 27 real project YAML files from the projects repository
