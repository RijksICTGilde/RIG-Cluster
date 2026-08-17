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
  "refresh_triggered": true,
  "refresh_succeeded": true
}
```

### A restore that lands but is not applied

A versioned restore does two things: it puts the data in a new generation of the
resource, and it then triggers a project refresh that regenerates the manifests and
secrets so the deployment starts using that new generation. The second half can fail on
its own, and then the data is restored while the deployment keeps running on the old
manifests.

That is reported, not hidden in the OPI log:

| Outcome | `status` | HTTP | `refresh_triggered` | `refresh_succeeded` |
|---|---|---|---|---|
| Restored and applied | `success` | `200` | `true` | `true` |
| Restored, applying it failed | `partial` | `207` | `true` | `false` |
| Restored, no refresh asked for (`update_deployment: false`) | `success` | `200` | `false` | `null` |
| The restore itself failed | `failed` | `500` | `false` | `false` |

On `partial` the message says so as well. The restored data is in place; retry the
restore or trigger a project refresh once the cause is cleared.

### Restoring a database keeps the deployment's credentials

A database restore creates a **new generation** of the database (`myproject_prod` ->
`myproject_prod_v1`) owned by the same database user, and bumps the generation in the
project file. It deliberately does **not** touch that user's password.

That is not a detail, it is the whole reason the restore works. The password lives in
the `{deployment}-database` secret, whose manifest is in `zad-deployments` and which
ArgoCD applies with `syncPolicy.automated.selfHeal: true`. A direct `kubectl patch` of
that secret is reverted within milliseconds, so the **only** route into it is the
project refresh -- and the refresh reads the secret first, tests it, and refuses to
touch a secret whose credentials no longer work (`Manual intervention required to fix
database user or update secret`). Rotating the password therefore locked the project:
the restore reported `success`, the refresh aborted before writing any manifest, and
every later change hit the same wall.

Nothing was gained by rotating: the user already exists and simply becomes the owner of
the new generation as well. A password is only generated when there is no secret to read
one from -- the restore pod needs something to connect with.

The switch-over runs the ordinary GitOps route: the refresh writes the new
`DATABASE_DB`/`DATABASE_SCHEMA` into `zad-deployments`, ArgoCD syncs, and the pods pick
up the new generation. Pinned in `tests/test_restore_database_secret.py` (unit) and
`tests/e2e/test_sandbox_restore_op_slot.py` (against a live sandbox, including a change
made after the restore).

### Which schema a database restore renames

A backup dumps the **whole** database (`pg_dump --format=custom`, no `-n`), so the dump
carries the default schema *and* every extra schema (RC-17,
`features/postgresql-scope-and-schemas.md`). A restore into a new generation has to
rename exactly one of them: the default schema, because OPI puts the generation in the
database name **and** in the default schema name (`db_schema = db_database`). The extra
schemas (`{project}_{deployment}_{postfix}`) carry no generation, so their name is
already right in the target database and they are left alone.

Which schema that is, is **named by the platform** (`generate_database_name`) and passed
to the restore pod as `SOURCE_SCHEMA`; the pod never reads it from the dump. It used to:

```sh
pg_restore --list "$DUMP_PATH" | grep " SCHEMA - " | head -1 | awk '{print $6}'
```

and `pg_restore --list` sorts schemas **alphabetically**, not by creation order. A project
with a `rapportage` schema, restored to its second generation (`amt_prod_v2` ->
`amt_prod_v3`), therefore renamed `amt_prod_rapportage` (r < v) to `amt_prod_v3`: the
application read the reporting tables, its own data sat unused in `amt_prod_v2`, and
`DATABASE_SCHEMA_RAPPORTAGE` pointed at a schema that no longer existed. The restore
reported success. The first generation restore went fine (`amt_prod` is a prefix of
`amt_prod_rapportage` and sorts first), which is why it stayed unnoticed.

Three more things the pod now does on that path:

* it drops the target schema only when it is **empty** and with `RESTRICT`, never
  `CASCADE`. A non-empty target schema holds data the restore just wrote, so the restore
  **stops** instead of destroying it;
* it refuses a schema name that is not `[a-z][a-z0-9_]*`, and quotes both identifiers;
* it logs which schema was renamed and which schemas were left untouched, plus the final
  list of schemas — so a failure shows up in the pod log, not in the application.

If the source name cannot be established (a snapshot without project/deployment
metadata), the restore only continues when the dump itself already contains a schema with
the target's name — then there is provably nothing to rename. Otherwise it stops: a
restore that guesses puts the wrong data under the name the application reads.

Pinned in `tests/test_restore_schema_rename.py`, including three `requires_infra` tests
that run the shipped shell block against a real PostgreSQL.

### Failed restore: whose fault was it?

`POST /api/v1/restore/database/...` and `POST /api/v1/restore/bucket/...` answer a failure
with an `error_category` next to `message`, so a client does not have to read the pod log
text to decide whether retrying makes sense:

| Situation | Status | `error_category` |
|---|---|---|
| The destination the caller supplied is unusable: host does not resolve, port refuses, database/bucket unknown, or the credentials are rejected | `400` | `InvalidTarget` |
| Anything on our side: the Kopia repository, a missing snapshot, a pod that will not start, a timeout, the cluster | `500` | `Unknown` |
| Success | `200` | field absent |

**How that is decided — never on the log text.** Both restore pods probe their destination
before touching any data (`psql -c "SELECT 1"`, `mc alias set`). That gate exits with the
dedicated `RESTORE_TARGET_UNUSABLE_EXIT_CODE` (`opi/core/backup_constants.py`), and the
manager reads the exit code from the pod status into `RestoreResult.target_unusable`.
Matching on `could not translate host name` would break the moment PostgreSQL or mc rewords
its error; an exit code we choose ourselves does not.

**A restore without target fields never gets `InvalidTarget`.** Omit the four target fields
and the platform picks the project's own service (see "Restore a Database" below),
so a destination failure cannot be the caller's input — it stays `500`/`Unknown`.

Known limit: a destination that lets you in but refuses the write (enough rights to connect,
too few to restore) passes the gate and fails afterwards, which is a `500`.

The category never carries a value the caller supplied — the pod's error line names the
fields, not their contents. Pinned in `tests/test_restore_target_fault.py`.

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

Generation is stored at the level the resource's NAME is scoped to. That is not a
convention you have to remember separately — read the naming table above: a PVC name
carries the component, a database and a bucket name carry only the project and the
deployment. So a PVC has one generation per component, a database and a bucket have one
per deployment, shared by every component in it.

**PVC Generation** (component-level):
```yaml
deployments:
  - name: production
    components:
      - reference: my-app
        services:
          persistent-storage:
            - reference: data
              config:
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
      - reference: postgresql-database   # or namespace-postgresql-database,
        config:                          # whichever the project declares
          generation: 1  # Database generation
```

This is the block the provisioning (`database_manager`, `minio_manager`) and the
reconciliation read to decide which database or bucket the running deployment points at,
so it is also the block the restore writes.

**Files written under the old placement are repaired on load.** A restore used to write
the database/bucket generation deployment-level while reading it back component-level, so
the number never travelled and every restore round recomputed `0 -> 1` (RC-123).
`schema_migration.relocate_resource_generations_to_deployment` moves any component-level
database/bucket generation up to the deployment on every project load; it is idempotent
and leaves storage generations alone. When both placements hold a value and they disagree
it keeps the **higher** one and logs a warning naming both, because a generation lower
than reality resolves to a resource that already exists.

The old component-level writer always used the fixed key `postgresql-database`, also for a
project that declares `namespace-postgresql-database`. The repair therefore writes the
value under the name the project actually declares and merges the two PostgreSQL names
into one value, rather than leaving it under the key it was found. Under the other key it
would not be read back at all, and next to a real entry it would shadow it: the
reconciliation resolves `postgresql-database` first, so it would expect the older name and
mark the running `_vN` database an orphan.

### A restore never writes into a target that already holds data

`pg_restore` adds rows, it does not replace them, and a bucket restore merges. So a
restore whose target already exists AND is not empty is refused with a 500 naming the
resource, instead of landing the backup on top of what is there:

```
Target database myproject_prod_v1 already exists and is not empty.
Restoring into it would add the backup on top of the rows already there.
Remove that database or raise the generation before retrying.
```

An existing but **empty** target is allowed through — that is what a retry after a restore
that failed halfway looks like. The checks are
`PostgresConnector.database_has_user_data` (any table, view, sequence or foreign table
outside the system schemas) and `MinioConnector.bucket_has_objects`.

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

**Database/Bucket reference_name**: the service reference the backup was registered
under — a component service reference (`{deployment}-postgresql`, `{deployment}-minio`)
or the deployment-wide fallback (`{deployment}-database`, `{deployment}-minio`).

You do not have to derive it: both read endpoints publish exactly the name the restore
endpoints accept.

| Read endpoint | Field |
|---|---|
| `GET /api/v1/backup/runs/{project}/{deployment}` | `reference_name` |
| `GET /api/v1/restore/snapshots/{cluster}/{namespace}` | `pvc_name` (carries every kind, PVC/database/bucket) |

A database or bucket snapshot carries no `pvc` tag, so this listing used to fall back to
the last segment of the snapshot's source path — `backup` for a database dump and
`bucket-backup` for a mirrored bucket. Both endpoints published that directory name while
the restore route wanted the reference, so no readable name was accepted (RC-95). The
listing now reads the `database`/`bucket` tag for those kinds; a PVC keeps its `pvc` tag,
which is what the PVC restore route takes.

A reference no deployment carries answers 404, naming the references that do exist.

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

The four `target_database_*` fields are optional. Omit them all and the restore goes
into the database of the project the API key belongs to: OPI reads those credentials
from the deployment secret in the project's own namespace, because they are injected
into the project's pods and are published by no API. Supply all four to restore into
an external database. Supplying only some of them is answered with 422 naming the
fields that are missing — OPI does not guess the rest.

```bash
# Restore the latest snapshot into the project's OWN database (no credentials needed)
curl -X POST "http://localhost:9595/api/v1/restore/database/local/rig-my-project/mydb?project_name=my-project" \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{}'

# Restore latest snapshot into an external database
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
- `target_database_*`: Connection parameters for the target database. Optional as a
  group: omit `target_database_host`, `target_database_name`, `target_database_user`
  and `target_database_password` to restore into the project's own database. A partial
  set is a 422. `reference_name` decides which deployment's database that is: it is
  either a component service reference (`{deployment}-postgresql`) or the
  deployment-wide fallback (`{deployment}-database`). An unknown reference, or a
  database that is not provisioned yet, answers 404.

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

The four target fields are optional, exactly as for databases: omit them all and the
restore goes into the bucket of the project the API key belongs to.

```bash
# Restore the latest snapshot into the project's OWN bucket (no credentials needed)
curl -X POST "http://localhost:9595/api/v1/restore/bucket/local/rig-my-project/mybucket?project_name=my-project" \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{}'

# Restore latest snapshot into an external bucket
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
- `target_minio_endpoint`: Target MinIO endpoint URL. This field and the three below
  are optional as a group: omit them all to restore into the project's own bucket; a
  partial set is a 422 naming what is missing
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
