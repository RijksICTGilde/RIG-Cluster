# Service Revision Tracking

## What it is

Service revision tracking maintains an audit trail of versioned resources (databases and MinIO buckets) in the project file. When the generational versioning system creates new resource versions during clone or restore operations, revision entries are automatically recorded with timestamps, actions, and source information.

This feature enables:
- **Lifecycle management**: Track what resources exist and their status (active/superseded)
- **Audit trail**: Know when resources were created and why
- **Future cleanup**: Identify orphaned resources for deletion
- **Reconciliation support**: Compare expected resources with actual infrastructure state

## Architecture

The feature is implemented through a dedicated `RevisionManager` class that centralizes all revision-related operations:

```
opi/manager/revision_manager.py  <- Manages revision tracking
opi/manager/database_manager.py  <- Calls RevisionManager on clone
opi/manager/minio_manager.py     <- Calls RevisionManager on clone
opi/manager/project_manager.py   <- Exposes RevisionManager via _revision_manager
```

## How it works

### Automatic Revision Recording

When a clone or restore operation creates a new versioned resource, the system automatically:

1. Marks any existing active revision entry as `superseded` with a `superseded_at` timestamp
2. Creates a new `active` revision entry for the new generation
3. Records the action (clone, restore, initial) and source information
4. Updates the service generation in the project file

### Revision Structure

Revisions are stored in the deployment's services configuration:

```yaml
deployments:
  - name: staging
    services:
      - reference: postgresql-database
        config:
          generation: 2
          revisions:
            - generation: 2
              resource: myproject_staging_v2
              status: active
              created_at: 2026-02-03T13:11:32+00:00
              actions:
                - timestamp: 2026-02-03T13:11:32+00:00
                  type: clone
                  source: "external:db.example.com:5432/source_db"
            - generation: 1
              resource: myproject_staging_v1
              status: superseded
              created_at: 2026-02-01T14:30:00+00:00
              superseded_at: 2026-02-03T13:11:32+00:00
              actions:
                - timestamp: 2026-02-01T14:30:00+00:00
                  type: clone
                  source: "deployment:production"
      - reference: minio-storage
        config:
          generation: 2
          revisions:
            - generation: 2
              resource: myproject-staging-v2
              status: active
              created_at: 2026-02-03T13:11:35+00:00
              actions:
                - timestamp: 2026-02-03T13:11:35+00:00
                  type: clone
                  source: "external:minio-host:9000/source-bucket"
```

### Revision Entry Fields

| Field | Type | Description |
|-------|------|-------------|
| `generation` | int | Generation number of this resource |
| `resource` | string | Actual resource name (database name or bucket name) |
| `status` | string | `active` for current resource, `superseded` for old versions |
| `created_at` | ISO8601 | Timestamp when this resource was created |
| `superseded_at` | ISO8601 | Timestamp when marked as superseded (only for superseded entries) |
| `actions` | list | List of actions performed on this resource |

### Action Entry Fields

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | ISO8601 | When this action occurred |
| `type` | string | Type of action: `clone`, `restore`, `initial` |
| `source` | string | Source reference describing where data came from |

### Source Reference Formats

| Format | Example | Description |
|--------|---------|-------------|
| `deployment:{name}` | `deployment:production` | Cloned from another deployment |
| `external:{host}:{port}/{resource}` | `external:db.example.com:5432/mydb` | Cloned from external source |
| `backup:{date}` | `backup:2026-02-01` | Restored from a backup |
| `remote-source:{name}` | `remote-source:odcn-production` | Cloned via remote-source configuration |

## RevisionManager API

The `RevisionManager` class provides the following methods:

### Recording Operations

```python
# Record a clone operation
revision_manager.record_clone(
    project_data=project_data,
    deployment_name="staging",
    service_type="postgresql-database",
    generation=2,
    resource_name="myproject_staging_v2",
    source="deployment:production",
)

# Record a restore operation
revision_manager.record_restore(
    project_data=project_data,
    deployment_name="staging",
    service_type="postgresql-database",
    generation=3,
    resource_name="myproject_staging_v3",
    backup_reference="backup:2026-02-01",
)

# Record initial resource creation
revision_manager.record_initial(
    project_data=project_data,
    deployment_name="staging",
    service_type="postgresql-database",
    generation=1,
    resource_name="myproject_staging_v1",
)
```

### Querying Revisions

```python
# Get all revisions for a service
revisions = revision_manager.get_revisions(
    project_data, "staging", "postgresql-database"
)

# Get the currently active revision
active = revision_manager.get_active_revision(
    project_data, "staging", "postgresql-database"
)

# Get superseded resources (for cleanup)
superseded = revision_manager.get_superseded_resources(
    project_data, "staging", "postgresql-database"
)
```

### Maintenance Operations

```python
# Add an additional action to an existing revision
revision_manager.add_action(
    project_data=project_data,
    deployment_name="staging",
    service_type="postgresql-database",
    generation=2,
    action="backup",
    source="backup:2026-02-03",
)

# Prune old superseded entries
revision_manager.prune(
    project_data=project_data,
    deployment_name="staging",
    service_type="postgresql-database",
    max_superseded_entries=5,
)
```

## Configuration

### Pruning Old Entries

Revision entries for superseded resources can be pruned to prevent the list from growing indefinitely. The `prune` method keeps a configurable number of superseded entries.

Default behavior:
- All `active` entries are kept (should only be one)
- Up to 5 most recent `superseded` entries are kept
- Older superseded entries are removed

## Usage Examples

### Viewing Current Revisions

The revisions are stored in the project file (YAML). To view them:

```bash
# View the project file
cat projects/myproject/project.yaml | yq '.deployments[].services'
```

### Identifying Orphaned Resources

Resources marked as `superseded` in the revisions represent orphaned infrastructure that may need cleanup:

```yaml
# Look for entries with status: superseded
revisions:
  - generation: 1
    resource: myproject_staging_v1
    status: superseded      # <-- This resource is orphaned
    superseded_at: 2026-02-03T13:11:32+00:00
```

### Multiple Actions on Same Resource

When multiple operations occur on the same resource (e.g., a backup on an existing version), additional actions are appended:

```yaml
revisions:
  - generation: 2
    resource: myproject_staging_v2
    status: active
    created_at: 2026-02-03T13:11:32+00:00
    actions:
      - timestamp: 2026-02-03T13:11:32+00:00
        type: clone
        source: "deployment:production"
      - timestamp: 2026-02-05T09:00:00+00:00
        type: backup                         # Additional action
        source: "backup:2026-02-05"
```

## Implementation Details

### Files

| File | Description |
|------|-------------|
| `opi/manager/revision_manager.py` | RevisionManager class with all revision operations |
| `opi/manager/database_manager.py` | Calls `record_clone()` on database clone operations |
| `opi/manager/minio_manager.py` | Calls `record_clone()` on MinIO clone operations |
| `opi/manager/project_manager.py` | Instantiates and exposes `_revision_manager` |
| `tests/test_revision_manager.py` | Unit tests for RevisionManager |

### Integration Points

The RevisionManager is called automatically when:
- Database clone with `force-clone: true` creates a new generation
- Database clone creates initial database
- MinIO clone with `force-clone: true` creates a new generation
- MinIO clone creates initial bucket

## Future Enhancements

### Phase 2: Cleanup Operations

- CLI commands to delete orphaned resources
- API endpoints for cleanup operations
- Automatic cleanup based on retention policies

### Phase 3: Reconciliation

- Scan actual infrastructure (databases, buckets) to find real state
- Compare with expected state from revisions
- Generate delta reports identifying:
  - Resources in infrastructure but not in revisions (unexpected)
  - Resources in revisions but missing from infrastructure (missing)
  - Status mismatches (marked superseded but still exists)

## Dependencies

- Requires generational versioning to be enabled (clone-from or restore operations)
- Revisions are only tracked for new operations after this feature is deployed
- Existing resources without revision entries can be manually added if needed

## Troubleshooting

### Revisions not being recorded

Ensure the operation is creating a new generation. Revisions are recorded when:
- `force-clone: true` is set and the target already exists
- Initial clone to a new deployment
- Restore operation from backup

### Old entries not being pruned

Pruning must be explicitly called. Consider adding periodic pruning to:
- Post-clone/restore hooks
- Scheduled maintenance tasks

### Rollback considerations

If you rollback by changing the generation in the project file:
1. The revisions remain unchanged
2. The `active` entry still reflects the newer generation
3. Consider manually updating revisions if needed for accuracy
