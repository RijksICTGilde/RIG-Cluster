# PVC Backup System

This document describes the PVC backup system that enables offsite backups of persistent volumes to external S3-compatible storage using Kopia.

## Overview

The backup system provides:
- **Incremental backups** using Kopia's deduplication
- **Per-project encryption** derived from SOPS age keys
- **Offsite storage** to external S3-compatible storage
- **Sequential execution** with distributed locking
- **Label-based selection** of PVCs to backup
- **Backup all mode** for Helm/external projects without labels

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  Operations Manager API                                             │
│                                                                     │
│  POST /api/v1/backup/project/{project_name}  (recommended)          │
│  POST /api/v1/backup/namespace/{namespace}                          │
│  POST /api/v1/backup/namespace/{namespace}/all  (no labels needed)  │
│  POST /api/v1/backup/pvc/{namespace}/{pvc_name}                     │
│  GET  /api/v1/backup/status                                         │
│                                                                     │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│  BackupManager                                                      │
│                                                                     │
│  For each PVC:                                                      │
│    1. Create VolumeSnapshot (instant, copy-on-write)                │
│    2. Create temp PVC clone from snapshot                           │
│    3. Derive encryption key from namespace's SOPS age key           │
│    4. Spawn Kopia backup pod                                        │
│    5. Upload to external S3 (encrypted, deduplicated)               │
│    6. Cleanup temp resources                                        │
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

### Backup Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/backup/status` | Get current backup status |
| `POST` | `/api/v1/backup/project/{project_name}/deployment/{deployment_name}` | Backup all labeled PVCs in a deployment (app + infra namespaces) |
| `POST` | `/api/v1/backup/namespace/{namespace}` | Backup labeled PVCs in namespace |
| `POST` | `/api/v1/backup/namespace/{namespace}/all` | Backup ALL PVCs in namespace (no labels required) |
| `POST` | `/api/v1/backup/pvc/{namespace}/{pvc_name}` | Backup a specific PVC |

### Restore Endpoints

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

## PVC Generation System (Project-Based Restore)

For RIG-managed projects, PVCs use a generation-based naming system that enables zero-downtime restore operations with automatic ArgoCD integration.

### How It Works

```
┌─────────────────────────────────────────────────────────────────────┐
│  Initial State                                                       │
│  - PVC: my-app-data-pvc (generation 0, no suffix)                   │
│  - Project file: no generation field (defaults to 0)                │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Restore Triggered                                                   │
│  1. Create new PVC: my-app-data-pvc-v1 (generation 1)               │
│  2. Restore backup data to new PVC                                  │
│  3. Update project file: generation = 1                             │
│  4. Commit & push project file                                      │
│  5. Trigger project refresh                                         │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│  ArgoCD Syncs                                                        │
│  - New manifest points to my-app-data-pvc-v1                        │
│  - PVC already exists (created during restore) → ArgoCD adopts it   │
│  - Old PVC (my-app-data-pvc) no longer in manifest → ArgoCD prunes  │
│  - Deployment restarts with new PVC containing restored data        │
└─────────────────────────────────────────────────────────────────────┘
```

### PVC Naming Convention

| Generation | PVC Name |
|------------|----------|
| 0 (default) | `{deployment}-{component}-{storage}-pvc` |
| 1 | `{deployment}-{component}-{storage}-pvc-v1` |
| 2 | `{deployment}-{component}-{storage}-pvc-v2` |
| N | `{deployment}-{component}-{storage}-pvc-vN` |

### Project File Structure

The generation is stored in the project.yaml under the component's storage configuration:

```yaml
deployments:
  - name: production
    components:
      - name: my-app
        storage:
          - mount-path: /data
            size: 10Gi
            generation: 2  # Added after restore, incremented each time
```

### Benefits

- **Zero-downtime**: Application keeps running on old PVC until ArgoCD switches
- **Atomic switch**: Deployment restarts with fully restored data
- **Easy rollback**: Decrement generation in project file to switch back
- **GitOps compatible**: All changes tracked in git
- **ArgoCD adoption**: New PVC has `argocd.argoproj.io/sync-options: Replace=false` annotation

### Finding Storage Name

The `storage_name` parameter in the restore API is derived from the mount path:

| Mount Path | Storage Name |
|------------|--------------|
| `/data` | `data` |
| `/var/lib/mysql` | `var-lib-mysql` |
| `/app/uploads` | `app-uploads` |

For multiple storages in the same component, an index suffix is added:
- First storage: `data`
- Second storage: `data-1`
- Third storage: `data-2`

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
