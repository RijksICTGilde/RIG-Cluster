# Backup System

This document describes the comprehensive backup system that enables offsite backups of persistent volumes (PVCs), PostgreSQL databases, and MinIO buckets to external S3-compatible storage using Kopia.

## Overview

The backup system provides:
- **Multiple resource types**: PVC, PostgreSQL database, and MinIO bucket backups
- **Incremental backups** using Kopia's deduplication
- **Per-project encryption** derived from SOPS age keys
- **Offsite storage** to external S3-compatible storage
- **Sequential execution** with distributed locking
- **Label-based selection** of PVCs to backup
- **Backup all mode** for Helm/external projects without labels
- **Resource type tagging** for filtering snapshots by type (pvc, database, bucket)
- **Scheduled backups** per deployment (daily/weekly/monthly) - see [scheduled-backups.md](scheduled-backups.md)

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  Operations Manager API                                             │
│                                                                     │
│  Trigger:                                                           │
│    POST   /api/v1/backup/project/{project}/deployment/{deployment}  │
│           (backs up PVCs, databases, and MinIO buckets in one run)  │
│                                                                     │
│  Inspect:                                                           │
│    GET    /api/v1/backup/status                                     │
│    GET    /api/v1/backup/runs/{project}/{deployment}                │
│                                                                     │
│  Delete:                                                            │
│    DELETE /api/v1/backup/snapshot/{project}/{deployment}/{id}       │
│                                                                     │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Backup Managers                                                    │
│                                                                     │
│  PVCBackupManager (for persistent volumes):                         │
│    1. Create VolumeSnapshot (instant, copy-on-write)                │
│    2. Create temp PVC clone from snapshot                           │
│    3. Derive encryption key from namespace's SOPS age key           │
│    4. Spawn Kopia backup pod                                        │
│    5. Upload to external S3 (encrypted, deduplicated)               │
│    6. Cleanup temp resources                                        │
│                                                                     │
│  DatabaseBackupManager (for PostgreSQL):                            │
│    1. Derive encryption key from namespace's SOPS age key           │
│    2. Spawn backup pod that runs pg_dump | kopia snapshot --stdin   │
│    3. Database dump streamed directly to Kopia (encrypted)          │
│    4. Cleanup backup pod                                            │
│                                                                     │
│  BucketBackupManager (for MinIO buckets):                           │
│    1. Derive encryption key from namespace's SOPS age key           │
│    2. Spawn backup pod with mc mirror + Kopia                       │
│    3. Mirror bucket to temp dir, then create Kopia snapshot         │
│    4. Cleanup backup pod                                            │
│                                                                     │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│  External S3 Bucket                                                 │
│                                                                     │
│  /rig-backups/                                                      │
│    ├── local/project-alpha/   ← Encrypted with project-alpha's key  │
│    ├── local/project-beta/    ← Encrypted with project-beta's key   │
│    └── local/rig-system/      ← Encrypted with rig-system's key     │
│                                                                     │
│  Each prefix = separate Kopia repository                            │
│  Each repository = separate encryption key                          │
│  Snapshots tagged with resource_type: pvc | database | bucket       │
└─────────────────────────────────────────────────────────────────────┘
```

## Quick Start

### 1. Configure Backup in project.yaml

Add a `backup` section to your project.yaml to automatically label PVCs for backup:

```yaml
name: my-project

backup:
  enabled: true          # Enable backup for this project
  schedule: daily        # Options: daily, weekly, manual (default: manual)

components:
  - name: my-app
    storage:
      - type: persistent
        size: 10Gi
        mount-path: /data
        backup: true     # Override: enable backup for this specific storage

deployments:
  - name: production
    cluster: local
    namespace: my-project
    # ...
```

### 2. Trigger a Backup

Backups are scoped to a (project, deployment) pair. One call backs up every
resource the deployment owns — PVCs, databases, MinIO buckets — in a single
backup run with a shared run ID.

```bash
curl -X POST "http://localhost:9595/api/v1/backup/project/my-project/deployment/production" \
  -H "X-API-Key: your-api-key"
```

To restrict to specific resource types:

```bash
curl -X POST "http://localhost:9595/api/v1/backup/project/my-project/deployment/production" \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"resource_types": ["pvc", "database"]}'
```

Scheduled backups are configured per deployment in `project.yaml` under
`deployments[].backup.schedule` (RRULE). OPI's in-process scheduler fires
them automatically — no curl needed.

### 3. Check Backup Status

```bash
curl -X GET "http://localhost:9595/api/v1/backup/status" \
  -H "X-API-Key: your-api-key"
```

### 4. List Available Backups

Before restoring, you need to know what backups exist. Use the snapshot listing endpoints:

```bash
# List all snapshots for a namespace
curl -X GET "http://localhost:9595/api/v1/restore/snapshots/local/rig-my-project?project_name=my-project" \
  -H "X-API-Key: your-api-key"

# List snapshots for a specific PVC
curl -X GET "http://localhost:9595/api/v1/restore/snapshots/local/rig-my-project/app-data?project_name=my-project" \
  -H "X-API-Key: your-api-key"
```

**Example Response:**
```json
{
  "cluster": "local",
  "namespace": "my-project",
  "snapshots": [
    {
      "snapshot_id": "k1234567890abcdef",
      "pvc_name": "app-data",
      "timestamp": "2025-01-12T14:30:22Z",
      "size_bytes": 1073741824
    },
    {
      "snapshot_id": "k0987654321fedcba",
      "pvc_name": "app-data",
      "timestamp": "2025-01-11T14:30:15Z",
      "size_bytes": 1073200128
    },
    {
      "snapshot_id": "kabcdef1234567890",
      "pvc_name": "cache-data",
      "timestamp": "2025-01-12T14:35:00Z",
      "size_bytes": 524288000
    }
  ]
}
```

**Understanding Snapshots:**
- `snapshot_id`: Unique Kopia snapshot identifier (use this for point-in-time restore)
- `pvc_name`: The original PVC name this backup is from
- `timestamp`: When the backup was created
- `size_bytes`: Size of the backup data

### 5. Restore from Backup

**Project-Based Restore (recommended for RIG-managed projects):**

This method automatically handles PVC versioning, project file updates, and ArgoCD integration:

```bash
curl -X POST "http://localhost:9595/api/v1/restore/project/my-project" \
  -H "X-Master-API-Key: your-master-key" \
  -H "Content-Type: application/json" \
  -d '{
    "deployment_name": "production",
    "component_name": "my-app",
    "storage_name": "data"
  }'
```

This will:
1. Create a new PVC with incremented generation (e.g., `my-app-data-pvc-v2`)
2. Restore backup data to the new PVC
3. Update the project file with the new generation
4. Commit and push the change to git
5. Trigger a project refresh for the specific deployment
6. ArgoCD syncs and switches to the new PVC, pruning the old one

**Manual Restore (for non-RIG managed projects):**

```bash
# Restore latest backup to new PVC
curl -X POST "http://localhost:9595/api/v1/restore/pvc/local/rig-my-project/app-data?project_name=my-project" \
  -H "X-API-Key: your-api-key"

# Restore with custom settings
curl -X POST "http://localhost:9595/api/v1/restore/pvc/local/rig-my-project/app-data?project_name=my-project" \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "target_pvc_name": "app-data-restored",
    "storage_size": "20Gi"
  }'

# Restore to existing PVC (requires explicit overwrite)
curl -X POST "http://localhost:9595/api/v1/restore/pvc/local/rig-my-project/app-data?project_name=my-project" \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "target_pvc_name": "existing-pvc",
    "overwrite": true
  }'

# Restore a specific snapshot
curl -X POST "http://localhost:9595/api/v1/restore/pvc/local/rig-my-project/app-data?project_name=my-project" \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "snapshot_id": "k1234567890abcdef"
  }'
```

## API Reference

Backups are scoped to (project, deployment). A single trigger covers every
resource the deployment owns — PVCs, databases, MinIO buckets — in one run.

### Backup endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/backup/project/{project_name}/deployment/{deployment_name}` | Trigger backup for a deployment (PVCs + databases + buckets, per `resource_types`) |
| `GET` | `/api/v1/backup/status` | Distributed-lock status (who's currently running a backup) |
| `GET` | `/api/v1/backup/runs/{project_name}/{deployment_name}` | List backup runs, grouped by `backup_run_id` |
| `DELETE` | `/api/v1/backup/snapshot/{project_name}/{deployment_name}/{snapshot_id}` | Delete a single snapshot (typically used to prune manual backups) |

### Restore endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/restore/snapshots/{cluster}/{namespace}` | List snapshots for namespace |
| `GET` | `/api/v1/restore/snapshots/{cluster}/{namespace}/{pvc_name}` | List snapshots for specific PVC |
| `POST` | `/api/v1/restore/project/{project_name}` | Restore for a RIG-managed project (auto-updates project file via generation versioning) |
| `POST` | `/api/v1/restore/pvc/{cluster}/{namespace}/{pvc_name}` | Manual PVC restore to new or existing PVC |
| `POST` | `/api/v1/restore/database/{cluster}/{namespace}/{reference_name}` | Restore a PostgreSQL database |
| `POST` | `/api/v1/restore/bucket/{cluster}/{namespace}/{reference_name}` | Restore a MinIO bucket |

All `{cluster}/{namespace}` endpoints require a `project_name` query parameter matching the
`X-API-Key`, and `{namespace}` must be that project's own prefixed namespace
(`rig-{project}` in sandbox, `rig-prd-{project}` in production). Any other namespace is
rejected with `403` — a project can only list or restore its own backups.

### Request body

`POST /api/v1/backup/project/{project}/deployment/{deployment}` accepts an
optional body to restrict to specific resource types:

```json
{ "resource_types": ["pvc", "database", "minio"] }
```

Omitting the body backs up all three types.

### Backup response

```json
{
  "status": "success",
  "message": "Backed up 3 resources for my-project/production",
  "backup_run_id": "20260520020000",
  "total_results": 3,
  "results": [
    {"namespace": "rig-my-project", "pvc_name": "production-frontend-data-pvc", "success": true, "duration_seconds": 45.3},
    {"namespace": "rig-my-project", "reference_name": "frontend-database", "success": true, "duration_seconds": 12.1},
    {"namespace": "rig-my-project", "reference_name": "frontend-uploads", "success": true, "duration_seconds": 8.7}
  ]
}
```

All snapshots in one run share `backup_run_id` and are tagged with `project`,
`deployment`, `component`, `resource_type`, `generation`, and `trigger`
(`"scheduled"` or `"manual"`). The trigger value also drives a per-resource
Kopia source identity so that scheduled retention never touches manual
snapshots — see "Trigger metadata and retention isolation" below.

**Manual Restore Response:**
```json
{
  "status": "success",
  "message": "Restored app-data to app-data-restored-20250112-150000",
  "result": {
    "namespace": "my-project",
    "pvc_name": "app-data",
    "success": true,
    "target_pvc_name": "app-data-restored-20250112-150000",
    "snapshot_id": "k1234567890abcdef",
    "duration_seconds": 60.2
  }
}
```

**Project Restore Response:**
```json
{
  "status": "success",
  "message": "Restored production-my-app-data-pvc to production-my-app-data-pvc-v2",
  "result": {
    "namespace": "rig-my-project",
    "pvc_name": "production-my-app-data-pvc",
    "success": true,
    "target_pvc_name": "production-my-app-data-pvc-v2",
    "duration_seconds": 75.4
  },
  "new_generation": 2,
  "project_updated": true,
  "refresh_triggered": true
}
```

## Configuration

### project.yaml Backup Configuration

```yaml
backup:
  enabled: true          # Enable backup labels on generated PVCs
  schedule: daily        # Schedule hint: daily, weekly, manual (for cron jobs)
```

Per-storage override in components:
```yaml
components:
  - name: my-app
    storage:
      - type: persistent
        size: 10Gi
        mount-path: /data
        backup: true     # Enable backup for this storage
      - type: persistent
        size: 5Gi
        mount-path: /cache
        backup: false    # Disable backup for this storage (e.g., cache)
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `BACKUP_S3_ENDPOINT` | S3 endpoint URL | `minio.rig-backup-destination.svc:9000` |
| `BACKUP_S3_BUCKET` | S3 bucket name (used when project context unavailable) | `rig-backups` |
| `BACKUP_S3_ACCESS_KEY` | S3 access key | - |
| `BACKUP_S3_SECRET_KEY` | S3 secret key | - |
| `BACKUP_SNAPSHOT_CLASS` | VolumeSnapshotClass name | `ocs-storagecluster-rbdplugin-snapclass` |
| `BACKUP_TIMEOUT_SECONDS` | Max backup duration | `3600` |
| `BACKUP_RETENTION_KEEP_LATEST` | Keep N latest snapshots per source | `30` |
| `BACKUP_RETENTION_KEEP_DAILY` | Keep N daily snapshots per source | `30` |
| `BACKUP_RETENTION_KEEP_WEEKLY` | Keep N weekly snapshots per source | `4` |
| `BACKUP_RETENTION_KEEP_MONTHLY` | Keep N monthly snapshots per source | `12` |
| `BACKUP_SCHEDULER_ENABLED` | Enable the in-process scheduler | `true` |
| `BACKUP_SCHEDULER_INTERVAL` | Scheduler tick interval (cron-anchored to wall-clock boundaries) | `600` |
| `BACKUP_MAX_CONCURRENT` | Max simultaneous backup/restore tasks | `2` |

Retention applies per (project, deployment, resource) — each PVC, database,
or bucket has its own Kopia source identity, so the counters are independent
across resources. Manual snapshots live under a separate `-manual` source
identity and are exempt from automatic retention entirely.

### Local Development Setup

For local testing, the backup destination MinIO is included in the bootstrap:

```bash
task bootstrap-argo-system
```

This creates:
- `rig-backup-destination` namespace
- MinIO deployment with S3-compatible API
- Default credentials: `backup-admin` / `backup-secret-key-local`

## Generational Versioning System

For RIG-managed projects, all stateful resources (PVCs, databases, buckets) use a consistent generation-based naming system. This enables zero-downtime restore and clone operations with automatic ArgoCD integration.

### Important: Version Suffix Behavior

The versioning system follows a consistent pattern across all resource types:

| Generation Value | Name Suffix | Description |
|-----------------|-------------|-------------|
| Not set / `null` | No suffix | Original resource (e.g., `my-bucket`) |
| `0` | No suffix | Explicitly unversioned (e.g., `my-bucket`) |
| `1` | `-v1` or `_v1` | First versioned resource (e.g., `my-bucket-v1`) |
| `2` | `-v2` or `_v2` | Second version (e.g., `my-bucket-v2`) |
| `N` | `-vN` or `_vN` | Nth version |

**Key behavior**: When you first set a generation value (e.g., `generation: 1`), the system creates a NEW versioned resource. The original unversioned resource is preserved but no longer referenced. This means:

- Setting `generation: 1` creates `my-bucket-v1`, leaving original `my-bucket` intact
- Data must be migrated or restored to the new versioned resource
- To use the original resource, set `generation: 0` or remove the generation field

### Naming Conventions by Resource Type

| Resource Type | No Generation / 0 | Generation 1+ |
|---------------|-------------------|---------------|
| **PVC** | `{deployment}-{component}-{storage}-pvc` | `{deployment}-{component}-{storage}-pvc-v{N}` |
| **Database** | `{project}_{deployment}` | `{project}_{deployment}_v{N}` |
| **Bucket** | `{project}-{deployment}` | `{project}-{deployment}-v{N}` |

**Examples:**

```
# PVC naming
generation: null  -> frontend-webapp-data-pvc
generation: 0     -> frontend-webapp-data-pvc
generation: 1     -> frontend-webapp-data-pvc-v1
generation: 2     -> frontend-webapp-data-pvc-v2

# Database naming (underscore separator)
generation: null  -> myproject_staging
generation: 0     -> myproject_staging
generation: 1     -> myproject_staging_v1
generation: 2     -> myproject_staging_v2

# Bucket naming (hyphen separator)
generation: null  -> myproject-staging
generation: 0     -> myproject-staging
generation: 1     -> myproject-staging-v1
generation: 2     -> myproject-staging-v2
```

### How Restore/Clone Works

```
┌─────────────────────────────────────────────────────────────────────┐
│  Initial State                                                       │
│  - Resource: my-bucket (no generation set)                          │
│  - Project file: no generation field                                │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Restore/Clone with Versioning                                       │
│  1. Read current generation (null/0 = no suffix)                    │
│  2. Increment generation: null -> 1                                 │
│  3. Create new resource: my-bucket-v1                               │
│  4. Restore/copy data to new resource                               │
│  5. Update project file: generation = 1                             │
│  6. Commit & push project file                                      │
│  7. Trigger project refresh                                         │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│  ArgoCD Syncs                                                        │
│  - New manifest points to my-bucket-v1                              │
│  - Resource already exists (created during restore)                 │
│  - Old resource (my-bucket) needs manual cleanup                    │
│  - Application uses new versioned resource                          │
└─────────────────────────────────────────────────────────────────────┘
```

### Project File Structure

Generation is stored at different levels depending on resource type:

**PVC Generation** (component-level):
```yaml
deployments:
  - name: production
    components:
      - reference: my-app
        storage:
          - mount-path: /data
            generation: 2  # PVC generation
```

**Database/Bucket Generation** (deployment-level):
```yaml
deployments:
  - name: production
    services:
      - reference: minio-storage
        config:
          generation: 1  # Bucket generation
      - reference: database
        config:
          generation: 1  # Database generation
```

### Benefits

- **Zero-downtime**: Application keeps running on old resource until switch
- **Atomic switch**: Application restarts with fully restored data
- **Rollback capability**: Change generation in project file to switch versions
- **GitOps compatible**: All changes tracked in git
- **Data preservation**: Old versions preserved until explicitly cleaned up
- **Consistent pattern**: Same versioning logic for PVC, database, and bucket

### Finding Storage/Reference Names

**PVC storage_name** (derived from mount path):

| Mount Path | Storage Name |
|------------|--------------|
| `/data` | `data` |
| `/var/lib/mysql` | `varlibmysql` |
| `/app/uploads` | `appuploads` |

**Database/Bucket reference_name**: Use the service reference name from your deployment configuration (e.g., `minio-storage`, `database`).

## Trigger metadata and retention isolation

Every backup carries a `trigger` value — `"scheduled"` (from the in-process
scheduler) or `"manual"` (from the UI button or the API endpoint). The
trigger drives two pieces of behavior:

1. **Scheduler isolation.** When checking "have we already run today?", the
   scheduler only considers tasks with `trigger=scheduled`. A user-triggered
   manual backup never suppresses the next automatic run.
2. **Retention isolation.** Each backup uses a per-resource Kopia source
   identity:
   - Scheduled: `opi-backup@<project>-<deployment>-<kind>-<resource>`
   - Manual: `opi-backup@<project>-<deployment>-<kind>-<resource>-manual`

   `kind` is `pvc`, `db`, or `bucket`. `resource` is the storage name, the
   database reference name, or the bucket reference name. Retention runs
   per-source — the scheduled run sets a policy on its own source and
   expires only its own snapshots. Manual snapshots are never touched by
   automatic expiry and persist until an operator deletes them via the
   delete-snapshot endpoint.

The UI marks each backup run with an "auto" or "handmatig" badge so the
distinction is visible at a glance.

## Database Backups (PostgreSQL)

Database backups run as part of the deployment backup trigger above — no
separate endpoint. When you call `POST /api/v1/backup/project/{p}/deployment/{d}`
with `resource_types` including `database` (the default), OPI:

1. Looks up the deployment's database credentials from its Kubernetes secret.
2. Spawns a backup pod in the project namespace.
3. Pipes `pg_dump --format=custom` directly into `kopia snapshot create --stdin-file`.
4. Tags the snapshot with `resource_type:database`, `database:<reference>`,
   `project`, `deployment`, `generation`, `backup_run`, `trigger`, etc.
5. Cleans up the pod.

For RIG-managed projects, OPI discovers the database from the deployment's
services and uses the auto-generated secret — no extra configuration needed
on the caller's side.

### Restore a Database

```bash
# Restore latest snapshot
curl -X POST "http://localhost:9595/api/v1/restore/database/local/rig-my-project/mydb?project_name=my-project" \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "target_database_host": "postgresql.my-namespace.svc.cluster.local",
    "target_database_port": 5432,
    "target_database_name": "myapp_restored",
    "target_database_user": "myapp",
    "target_database_password": "secret"
  }'

# Restore a specific snapshot
curl -X POST "http://localhost:9595/api/v1/restore/database/local/rig-my-project/mydb?project_name=my-project" \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "snapshot_id": "k1234567890abcdef",
    "target_database_host": "postgresql.my-namespace.svc.cluster.local",
    "target_database_name": "myapp",
    "target_database_user": "myapp",
    "target_database_password": "secret"
  }'
```

**Restore Parameters:**
- `cluster`: Cluster name where the backup was made
- `namespace`: Kubernetes namespace for the restore pod
- `reference_name`: Logical name of the database backup to restore
- `snapshot_id`: Optional specific snapshot ID (default: latest)
- `target_database_*`: Connection parameters for the target database

### Database Backup Response

```json
{
  "status": "success",
  "message": "Database backup of mydb completed successfully",
  "result": {
    "namespace": "my-namespace",
    "reference_name": "mydb",
    "database_name": "myapp",
    "success": true,
    "snapshot_name": "database-mydb.dump",
    "duration_seconds": 45.3
  }
}
```

## Bucket Backups (MinIO)

Bucket backups run as part of the deployment backup trigger — no separate
endpoint. When you call `POST /api/v1/backup/project/{p}/deployment/{d}`
with `resource_types` including `minio` (the default), OPI:

1. Looks up the deployment's MinIO credentials from its Kubernetes secret.
2. Spawns a backup pod in the project namespace.
3. Mirrors the source bucket to a temp directory with `mc mirror`.
4. Creates an encrypted Kopia snapshot of the temp directory.
5. Tags the snapshot with `resource_type:bucket`, `bucket:<reference>`,
   `source_bucket`, `project`, `deployment`, `trigger`, etc.
6. Cleans up.

Snapshots are encrypted with the project's SOPS-derived Kopia password —
S3 credentials alone can't decrypt them.

### Restore a Bucket

```bash
# Restore latest snapshot
curl -X POST "http://localhost:9595/api/v1/restore/bucket/local/rig-my-project/mybucket?project_name=my-project" \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "target_minio_endpoint": "http://minio.my-namespace.svc.cluster.local:9000",
    "target_bucket_name": "my-bucket-restored",
    "target_access_key": "minioaccess",
    "target_secret_key": "miniosecret"
  }'

# Restore with clear target (remove existing files first)
curl -X POST "http://localhost:9595/api/v1/restore/bucket/local/rig-my-project/mybucket?project_name=my-project" \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "snapshot_id": "k1234567890abcdef",
    "target_minio_endpoint": "http://minio.my-namespace.svc.cluster.local:9000",
    "target_bucket_name": "my-bucket",
    "target_access_key": "minioaccess",
    "target_secret_key": "miniosecret",
    "clear_target": true
  }'
```

**Restore Parameters:**
- `cluster`: Cluster name where the backup was made
- `namespace`: Kubernetes namespace for the restore pod
- `reference_name`: Logical name of the bucket backup to restore
- `snapshot_id`: Optional specific snapshot ID (default: latest)
- `target_minio_endpoint`: Target MinIO endpoint URL
- `target_bucket_name`: Target bucket name (can be different from source)
- `target_access_key`: Target MinIO access key
- `target_secret_key`: Target MinIO secret key
- `clear_target`: If `true`, clear target bucket before restoring (default: false)

### Bucket Backup Response

```json
{
  "status": "success",
  "message": "Bucket backup of mybucket completed successfully",
  "result": {
    "namespace": "my-namespace",
    "reference_name": "mybucket",
    "bucket_name": "my-bucket",
    "success": true,
    "use_kopia": true,
    "duration_seconds": 120.5
  }
}
```

### Choosing Between Kopia and mc mirror

| Feature | Kopia (use_kopia=true) | mc mirror (use_kopia=false) |
|---------|------------------------|------------------------------|
| Encryption | Yes (SOPS-derived key) | No |
| Deduplication | Yes | No |
| Speed | Slower (download + encrypt) | Faster (direct sync) |
| Storage | Efficient (dedup) | 1:1 copy |
| Restore | From Kopia snapshot | Not supported via API |
| Use case | Production backups | Quick syncs, staging |

## Resource Type Filtering

All backups are tagged with a `resource_type` tag for easy filtering:

- `resource_type:pvc` - Persistent Volume Claim backups
- `resource_type:database` - PostgreSQL database backups
- `resource_type:bucket` - MinIO bucket backups

The Kopia connector supports filtering by resource type when listing snapshots:

```python
# In Python code
snapshots = await kopia_connector.list_snapshots(config, resource_type="database")
```

## Security Model

### Per-Project Encryption

Each namespace's backups are encrypted with a unique key derived from its SOPS age key:

```
Project SOPS Age Key → SHA256 derivation → Kopia Repository Password
```

**Security properties:**
- S3 credentials leaked? Data is encrypted, unusable without project keys
- Project A cannot read Project B's backups (different encryption keys)
- Backup key is derived, not stored separately

### Disaster Recovery

For disaster recovery when the cluster is destroyed:

1. **Retrieve the age key** from project.yaml in git (stored as `config.age-private-key`)
2. **Derive the Kopia password:**
   ```python
   import hashlib
   import base64

   def derive_backup_password(namespace: str, age_key: str) -> str:
       material = f"kopia-backup-{namespace}-{age_key}".encode()
       derived = hashlib.sha256(material).digest()
       return base64.b64encode(derived).decode()[:32]
   ```
3. **Connect to Kopia:**
   ```bash
   kopia repository connect s3 \
     --bucket=rig-backups \
     --prefix=local/my-project/ \
     --endpoint=s3.example.com \
     --access-key=$S3_ACCESS_KEY \
     --secret-access-key=$S3_SECRET_KEY \
     --password="$DERIVED_PASSWORD" \
     --disable-tls-verification
   ```
4. **List and restore:**
   ```bash
   kopia snapshot list
   kopia restore <snapshot-id> /restore/path
   ```

## Backup Flow Details

### Step 1: Acquire Lock

A distributed lock (ConfigMap in `rig-system`) ensures only one backup runs at a time:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: backup-lock
  namespace: rig-system
data:
  locked_at: "2025-01-12T14:30:22+00:00"
  locked_by: "opi-deployment-abc123"
  current_namespace: "my-project"
  current_pvc: "app-data"
```

### Step 2: Create VolumeSnapshot

```yaml
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: app-data-backup-20250112-143022
  namespace: my-project
spec:
  volumeSnapshotClassName: ocs-storagecluster-rbdplugin-snapclass
  source:
    persistentVolumeClaimName: app-data
```

### Step 3: Create Clone PVC

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: app-data-backup-clone-20250112-143022
  namespace: my-project
spec:
  dataSource:
    kind: VolumeSnapshot
    apiGroup: snapshot.storage.k8s.io
    name: app-data-backup-20250112-143022
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
```

### Step 4: Run Backup Pod

A pod is spawned in the project namespace that:
1. Mounts the clone PVC
2. Connects to Kopia repository (creates if needed)
3. Runs incremental backup with PVC tag
4. Applies retention policy

### Step 5: Cleanup

After backup completes (success or failure):
- Delete backup pod
- Delete clone PVC
- Delete VolumeSnapshot
- Release lock

## Storage Efficiency

### Incremental Backups

Kopia uses content-defined chunking and deduplication:

| Backup | Data Change | Uploaded | Total Storage |
|--------|-------------|----------|---------------|
| Day 1 (full) | - | 10 GB | 10 GB |
| Day 2 | 500 MB | 500 MB | 10.5 GB |
| Day 3 | 200 MB | 200 MB | 10.7 GB |
| ... | ... | ... | ... |
| Day 30 | 100 MB | ~6 GB total | ~16 GB |

### VolumeSnapshots (In-Cluster)

Ceph RBD snapshots are copy-on-write:
- Snapshot creation is instant (~0 bytes)
- Only changed blocks consume additional storage
- Deleted after backup completes

## Troubleshooting

### Backup Pod Failed

Check pod logs:
```bash
kubectl logs -n my-project backup-app-data-20250112-143022
```

Common issues:
- S3 connectivity (check network policies)
- S3 credentials (check environment variables)
- PVC not bound (check storage class)

### Lock Stuck

If a backup crashed without releasing the lock:
```bash
# Check lock status
kubectl get cm backup-lock -n rig-system -o yaml

# Manual release (if stale > 1 hour)
kubectl delete cm backup-lock -n rig-system
```

### VolumeSnapshot Not Ready

```bash
kubectl get volumesnapshot -n my-project
kubectl describe volumesnapshot app-data-backup-20250112-143022 -n my-project
```

Check:
- VolumeSnapshotClass exists
- CSI driver is running
- PVC is bound

### Restore Fails

```bash
kubectl logs -n my-project restore-app-data-20250112-150000
```

Common issues:
- No snapshots found for PVC
- Target PVC exists without `overwrite: true`
- S3 connectivity issues

## Dependencies

- **Kubernetes**: VolumeSnapshot API (CSI snapshots)
- **Storage**: OCS/Ceph RBD with snapshot support
- **S3**: Any S3-compatible storage (MinIO, AWS S3, etc.)
- **Kopia**: Backup tool with deduplication and encryption

## RBAC Requirements

The operations-manager service account (`namespace-manager`) requires specific permissions to perform backup operations. These are configured in:

**File**: `bootstrap/rig-system/kustomize/operations-manager/overlays/local/cluster-role.yaml`

### Required Permissions

| Resource | API Group | Verbs | Purpose |
|----------|-----------|-------|---------|
| `configmaps` | `""` | create, get, delete, patch, update | Distributed backup lock in `rig-system` namespace |
| `persistentvolumeclaims` | `""` | create, get, list, delete | Get PVC info, create clone PVCs for backup, create restore PVCs |
| `pods` | `""` | create, get, list, delete, watch | Create and manage backup/restore pods |
| `pods/log` | `""` | get | Read backup pod logs for status and debugging |
| `volumesnapshots` | `snapshot.storage.k8s.io` | create, get, list, delete | Create CSI snapshots for point-in-time backups |
| `secrets` | `""` | get | Read SOPS age keys for backup encryption |

### ClusterRole Configuration

```yaml
# PVC Backup System Permissions
# Backup lock management (ConfigMap in rig-system namespace)
- apiGroups: [""]
  resources: [configmaps]
  verbs: [create, get, delete, patch, update]

# PVC operations for backup clones and restores
- apiGroups: [""]
  resources: [persistentvolumeclaims]
  verbs: [create, get, list, delete]

# Backup/restore pod management
- apiGroups: [""]
  resources: [pods]
  verbs: [create, get, list, delete, watch]

# Read backup pod logs for status and debugging
- apiGroups: [""]
  resources: [pods/log]
  verbs: [get]

# VolumeSnapshot operations (CSI snapshots for point-in-time backups)
- apiGroups: [snapshot.storage.k8s.io]
  resources: [volumesnapshots]
  verbs: [create, get, list, delete]
```

### Applying Permission Changes

After modifying the ClusterRole, apply with:

```bash
kubectl apply -f bootstrap/rig-system/kustomize/operations-manager/overlays/local/cluster-role.yaml
```

Or rebuild with kustomize:

```bash
kustomize build bootstrap/rig-system/kustomize/operations-manager/overlays/local | kubectl apply -f -
```

### Verifying Permissions

Check if the service account has the required permissions:

```bash
# Check configmap access in rig-system
kubectl auth can-i create configmaps -n rig-system --as=system:serviceaccount:rig-system:namespace-manager

# Check PVC access
kubectl auth can-i create persistentvolumeclaims -n my-project --as=system:serviceaccount:rig-system:namespace-manager

# Check volumesnapshot access
kubectl auth can-i create volumesnapshots.snapshot.storage.k8s.io -n my-project --as=system:serviceaccount:rig-system:namespace-manager
```
