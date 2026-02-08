# Future: Storage Configuration Restructure

## Current Situation

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

## Problems with Current Approach

1. **Scattered configuration**: Storage-related settings are in two different places, making it confusing to understand and maintain.

2. **Inconsistent with other services**: Database and MinIO follow a cleaner `uses-services` with `services.{type}` pattern where generation is tracked alongside service configuration.

3. **Confusing `uses-services` vs `storage`**: The component declares `uses-services: [persistent-storage]` but the actual storage definition is in a separate `storage:` block.

4. **Generation tracking mismatch**: The `storage:` block defines size/mount-path, but generation is tracked separately in `services.persistent-storage`.

## Proposed Future Structure

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

## Benefits of Proposed Structure

1. **Single source of truth**: All storage configuration in one conceptual location
2. **Consistent pattern**: Follows the same `uses-services` → `services.{type}` pattern as database/minio
3. **Clear separation**: Component defines "what storage exists", deployment defines "runtime config per deployment"
4. **Easier maintenance**: No more hunting for storage settings across different sections

## Migration Path

1. Add support for the new `uses-services: [persistent-storage: [...]]` format
2. Keep backward compatibility with current `storage:` block format
3. Deprecate the old format with warnings
4. Migrate existing projects over time
5. Remove support for old format in a future major version

## Related Files

- `opi/handlers/project_file_handler.py` - Project file parsing and generation lookups
- `opi/manager/pvc_manager.py` - PVC manifest generation
- `opi/manager/project_manager.py` - Main orchestration
- Project YAML files in `projects/` directory

## Tracking

- **Status**: Proposed
- **Created**: 2026-02-06
- **Priority**: Low (current system works, this is a cleanup/consistency improvement)
