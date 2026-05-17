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
│  PVC Backups:                                                       │
│    POST /api/v1/backup/project/{project}/deployment/{deployment}    │
│    POST /api/v1/backup/namespace/{namespace}                        │
│    POST /api/v1/backup/namespace/{namespace}/all                    │
│    POST /api/v1/backup/pvc/{namespace}/{pvc_name}                   │
│                                                                     │
│  Database Backups:                                                  │
│    POST /api/v1/backup/database/{namespace}/{reference_name}        │
│                                                                     │
│  Bucket Backups:                                                    │
│    POST /api/v1/backup/bucket/{namespace}/{reference_name}          │
│                                                                     │
│  GET  /api/v1/backup/status                                         │
│  GET  /api/v1/backup/runs/{project}/{deployment}                    │
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

**Backup a project deployment (recommended):**
```bash
curl -X POST "http://localhost:9595/api/v1/backup/project/my-project/deployment/production" \
  -H "X-API-Key: your-api-key"
```

**Backup a namespace:**
```bash
curl -X POST "http://localhost:9595/api/v1/backup/namespace/my-project" \
  -H "X-API-Key: your-api-key"
```

**Backup all PVCs in a namespace (no labels required - for Helm projects):**
```bash
curl -X POST "http://localhost:9595/api/v1/backup/namespace/my-project/all" \
  -H "X-API-Key: your-api-key"
```

**Backup specific PVCs:**
```bash
curl -X POST "http://localhost:9595/api/v1/backup/namespace/my-project" \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"pvcs": ["app-data", "cache-data"]}'
```

**Backup a single PVC:**
```bash
curl -X POST "http://localhost:9595/api/v1/backup/pvc/my-project/app-data" \
  -H "X-API-Key: your-api-key"
```

### 3. Check Backup Status

```bash
curl -X GET "http://localhost:9595/api/v1/backup/status" \
  -H "X-API-Key: your-api-key"
```

### 4. List Available Backups

Before restoring, you need to know what backups exist. Use the snapshot listing endpoints:

```bash
# List all snapshots for a namespace
curl -X GET "http://localhost:9595/api/v1/restore/snapshots/local/my-project" \
  -H "X-API-Key: your-api-key"

# List snapshots for a specific PVC
curl -X GET "http://localhost:9595/api/v1/restore/snapshots/local/my-project/app-data" \
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
curl -X POST "http://localhost:9595/api/v1/restore/pvc/local/my-project/app-data" \
  -H "X-API-Key: your-api-key"

# Restore with custom settings
curl -X POST "http://localhost:9595/api/v1/restore/pvc/local/my-project/app-data" \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "target_pvc_name": "app-data-restored",
    "storage_size": "20Gi"
  }'

# Restore to existing PVC (requires explicit overwrite)
curl -X POST "http://localhost:9595/api/v1/restore/pvc/local/my-project/app-data" \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "target_pvc_name": "existing-pvc",
    "overwrite": true
  }'

# Restore a specific snapshot
curl -X POST "http://localhost:9595/api/v1/restore/pvc/local/my-project/app-data" \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "snapshot_id": "k1234567890abcdef"
  }'
```

## API Reference

### PVC Backup Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/backup/status` | Get current backup status |
| `POST` | `/api/v1/backup/project/{project_name}/deployment/{deployment_name}` | Backup all labeled PVCs in a deployment (app + infra namespaces) |
| `POST` | `/api/v1/backup/namespace/{namespace}` | Backup labeled PVCs in namespace |
| `POST` | `/api/v1/backup/namespace/{namespace}/all` | Backup ALL PVCs in namespace (no labels required) |
| `POST` | `/api/v1/backup/pvc/{namespace}/{pvc_name}` | Backup a specific PVC |

### Database Backup Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/backup/database/{namespace}/{reference_name}` | Backup a PostgreSQL database |
| `POST` | `/api/v1/restore/database/{cluster}/{namespace}/{reference_name}` | Restore a PostgreSQL database |

### Bucket Backup Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/backup/bucket/{namespace}/{reference_name}` | Backup a MinIO bucket (Kopia encrypted or mc mirror) |
| `POST` | `/api/v1/restore/bucket/{cluster}/{namespace}/{reference_name}` | Restore a MinIO bucket |

### PVC Restore Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/restore/snapshots/{cluster}/{namespace}` | List snapshots for namespace |
| `GET` | `/api/v1/restore/snapshots/{cluster}/{namespace}/{pvc_name}` | List snapshots for specific PVC |
| `POST` | `/api/v1/restore/project/{project_name}` | **Recommended:** Restore PVC for RIG-managed project (auto-updates project file) |
| `POST` | `/api/v1/restore/pvc/{cluster}/{namespace}/{pvc_name}` | Manual restore to new or existing PVC |

### Request/Response Examples

**Backup Response:**
```json
{
  "status": "success",
  "message": "Backed up 2 PVC(s) in namespace my-project",
  "results": [
    {
      "namespace": "my-project",
      "pvc_name": "app-data",
      "success": true,
      "snapshot_name": "app-data-backup-20250112-143022",
      "duration_seconds": 45.3
    },
    {
      "namespace": "my-project",
      "pvc_name": "cache-data",
      "success": true,
      "snapshot_name": "cache-data-backup-20250112-143108",
      "duration_seconds": 12.1
    }
  ]
}
```

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
| `BACKUP_S3_BUCKET` | S3 bucket name | `rig-backups` |
| `BACKUP_S3_ACCESS_KEY` | S3 access key | - |
| `BACKUP_S3_SECRET_KEY` | S3 secret key | - |
| `BACKUP_SNAPSHOT_CLASS` | VolumeSnapshotClass name | `ocs-storagecluster-rbdplugin-snapclass` |
| `BACKUP_TIMEOUT_SECONDS` | Max backup duration | `3600` |
| `BACKUP_RETENTION_KEEP_LATEST` | Keep N latest snapshots | `7` |
| `BACKUP_RETENTION_KEEP_DAILY` | Keep N daily snapshots | `7` |
| `BACKUP_RETENTION_KEEP_WEEKLY` | Keep N weekly snapshots | `4` |

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

## Backup Strategies

### RIG-Managed Projects

For projects managed by RIG with generated manifests:

1. Add `backup.enabled: true` to project.yaml
2. PVCs will automatically get the `backup.rig.nl/enabled: "true"` label
3. Use `/api/v1/backup/project/{project_name}/deployment/{deployment_name}` to backup a specific deployment

### Helm/External Projects

For Helm charts or externally managed deployments where you can't add labels:

1. Use the `/api/v1/backup/namespace/{namespace}/all` endpoint
2. This backs up ALL PVCs in the namespace, regardless of labels
3. Useful for third-party applications

### Manual PVC Labeling

For existing PVCs, add the backup label manually:

```bash
kubectl label pvc my-pvc -n my-namespace backup.rig.nl/enabled=true
```

Or in YAML:
```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: my-pvc
  labels:
    backup.rig.nl/enabled: "true"
```

## Database Backups (PostgreSQL)

The backup system supports PostgreSQL database backups using `pg_dump` with streaming encryption through Kopia.

### How Database Backup Works

1. A backup pod is spawned in the target namespace
2. The pod runs `pg_dump --format=custom` piped directly to `kopia snapshot create --stdin-name`
3. The database dump is encrypted and deduplicated by Kopia
4. Snapshots are tagged with `resource_type:database` for filtering

### Backup a Database

```bash
curl -X POST "http://localhost:9595/api/v1/backup/database/my-namespace/mydb" \
  -H "X-API-Key: your-master-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "database_host": "postgresql.my-namespace.svc.cluster.local",
    "database_port": 5432,
    "database_name": "myapp",
    "database_user": "myapp",
    "database_password": "secret",
    "source_type": "namespace"
  }'
```

**Parameters:**
- `namespace`: Kubernetes namespace where the backup pod runs
- `reference_name`: Logical name for this database (used in tags and snapshot identification)
- `database_host`: PostgreSQL host address
- `database_port`: PostgreSQL port (default: 5432)
- `database_name`: Database name to backup
- `database_user`: Database username
- `database_password`: Database password
- `source_type`: `"namespace"` for namespace-local databases, `"shared"` for shared databases

### Restore a Database

```bash
# Restore latest snapshot
curl -X POST "http://localhost:9595/api/v1/restore/database/local/my-namespace/mydb" \
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
curl -X POST "http://localhost:9595/api/v1/restore/database/local/my-namespace/mydb" \
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

The backup system supports MinIO bucket backups with two modes:
1. **Kopia mode** (default): Encrypted, deduplicated backups via `mc mirror` + Kopia
2. **mc mirror mode**: Direct bucket-to-bucket sync (faster, but unencrypted)

### How Bucket Backup Works (Kopia Mode)

1. A backup pod is spawned in the target namespace
2. The pod runs `mc mirror` to download the bucket to a temp directory
3. Kopia creates an encrypted snapshot of the temp directory
4. Snapshots are tagged with `resource_type:bucket` for filtering

### How Bucket Backup Works (mc mirror Mode)

1. A backup pod is spawned in the target namespace
2. The pod runs `mc mirror` directly from source bucket to backup bucket
3. Files are synced without encryption (faster for large buckets)
4. Metadata is stored alongside the backup

### Backup a Bucket

```bash
# Kopia backup (encrypted, recommended)
curl -X POST "http://localhost:9595/api/v1/backup/bucket/my-namespace/mybucket" \
  -H "X-API-Key: your-master-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "source_minio_endpoint": "http://minio.my-namespace.svc.cluster.local:9000",
    "source_bucket_name": "my-bucket",
    "source_access_key": "minioaccess",
    "source_secret_key": "miniosecret",
    "source_type": "namespace",
    "use_kopia": true
  }'

# mc mirror backup (unencrypted, faster)
curl -X POST "http://localhost:9595/api/v1/backup/bucket/my-namespace/mybucket" \
  -H "X-API-Key: your-master-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "source_minio_endpoint": "http://minio.my-namespace.svc.cluster.local:9000",
    "source_bucket_name": "my-bucket",
    "source_access_key": "minioaccess",
    "source_secret_key": "miniosecret",
    "use_kopia": false
  }'
```

**Parameters:**
- `namespace`: Kubernetes namespace where the backup pod runs
- `reference_name`: Logical name for this bucket (used in tags and snapshot identification)
- `source_minio_endpoint`: MinIO endpoint URL
- `source_bucket_name`: Bucket name to backup
- `source_access_key`: MinIO access key
- `source_secret_key`: MinIO secret key
- `source_type`: `"namespace"` for namespace-local MinIO, `"shared"` for shared MinIO
- `use_kopia`: `true` for encrypted Kopia backup (default), `false` for mc mirror

### Restore a Bucket

```bash
# Restore latest snapshot
curl -X POST "http://localhost:9595/api/v1/restore/bucket/local/my-namespace/mybucket" \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "target_minio_endpoint": "http://minio.my-namespace.svc.cluster.local:9000",
    "target_bucket_name": "my-bucket-restored",
    "target_access_key": "minioaccess",
    "target_secret_key": "miniosecret"
  }'

# Restore with clear target (remove existing files first)
curl -X POST "http://localhost:9595/api/v1/restore/bucket/local/my-namespace/mybucket" \
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
