# MinIO Migration to Versioned Storage

This folder contains resources for migrating from MinIO filesystem mode to erasure coding mode, which enables S3 object versioning support.

## Background

MinIO in filesystem mode (`minio server /data`) does **not** support object versioning. To enable versioning (required for docs app version history), MinIO must run in erasure coding mode (`minio server /data/disk{1...4}`).

This migration creates a new MinIO instance with erasure coding, migrates data, then switches the service to point to the new instance.

## Migration Overview

| Phase | Description | Managed By | Downtime |
|-------|-------------|------------|----------|
| 1 | Deploy new versioned MinIO alongside existing | Argo | None |
| 2 | Run migration job to copy data | Manual | None |
| 3 | Switch service selector to new MinIO | Argo | Brief (~seconds) |
| 4 | Cleanup old resources | Manual | None |

## Prerequisites

- Access to the Kubernetes cluster
- Argo CD synced and healthy
- Backup of MinIO data (recommended)

## Phase 1: Deploy Versioned MinIO

**Already configured in base kustomization.** When Argo syncs, it deploys:
- `minio-versioned` Deployment (erasure coding mode)
- `minio-versioned` Service
- `minio-storage-versioned` PVC

The existing MinIO continues running - no disruption.

**Verify deployment:**
```bash
# Local
kubectl get pods -n rig-system | grep minio

# ODCN
kubectl get pods -n rig-prd-operations | grep minio

# Should see both minio-xxx and minio-versioned-xxx running
```

## Phase 2: Run Migration Job

Apply the migration job to copy all buckets and objects:

**For local (Kind) cluster:**
```bash
kubectl apply -f job-migrate-to-versioned.yaml
kubectl logs -f job/minio-migration-to-versioned -n rig-system
```

**For ODCN (production):**
```bash
kubectl apply -f job-migrate-to-versioned-odcn.yaml
kubectl logs -f job/minio-migration-to-versioned -n rig-prd-operations
```

The job will:
1. Set up mc aliases for both MinIO instances
2. Create buckets on new MinIO
3. Enable versioning on each bucket
4. Mirror all objects with metadata preserved

**Note:** The migration is idempotent - safe to run multiple times. Delete the job first if re-running:
```bash
kubectl delete job minio-migration-to-versioned -n $NS
```

**Verify migration:**
```bash
# Set namespace (rig-system for local, rig-prd-operations for ODCN)
NS=rig-system  # or rig-prd-operations

# Check buckets exist on new MinIO
kubectl exec -n $NS deploy/minio-versioned -- sh -c \
  'export MC_CONFIG_DIR=/tmp/.mc && \
   mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" && \
   mc ls local/'

# Check versioning is enabled
kubectl exec -n $NS deploy/minio-versioned -- sh -c \
  'export MC_CONFIG_DIR=/tmp/.mc && \
   mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" && \
   mc version info local/YOUR_BUCKET_NAME'
```

## Phase 3: Switch Service to New MinIO

**This is configured in the overlay kustomization.** The patch changes the `minio` service selector from `app: minio` to `app: minio-versioned`.

For local cluster: `overlays/local/kustomization.yaml`
For ODCN: `overlays/odcn/kustomization.yaml`

**To apply immediately (without waiting for Argo):**
```bash
# Set namespace (rig-system for local, rig-prd-operations for ODCN)
NS=rig-system  # or rig-prd-operations

kubectl patch service minio -n $NS -p '{"spec":{"selector":{"app":"minio-versioned"}}}'
```

**Verify switch:**
```bash
kubectl get endpoints minio -n $NS
# Should show IP of minio-versioned pod
```

**Restart apps to ensure fresh connections (optional but recommended):**
```bash
# For docs app
kubectl rollout restart deployment -n rig-mijn-bureau-docs mijn-bureau-docs-backend
kubectl rollout restart deployment -n rig-mijn-bureau-docs mijn-bureau-docs-celery-worker
```

## Phase 4: Cleanup (After Verification)

Once verified working, remove old resources:

1. **Remove old MinIO deployment and PVC from kustomization** (separate PR)
2. **Remove minio-versioned service** (no longer needed - main service now routes to versioned)
3. **Delete migration job:**
   ```bash
   kubectl delete job minio-migration-to-versioned -n rig-system
   ```

## Rollback

If issues occur before Phase 3:
- Old MinIO is still running with original data
- Simply continue using old MinIO service

If issues occur after Phase 3:
```bash
# Revert service selector
kubectl patch service minio -n rig-system -p '{"spec":{"selector":{"app":"minio"}}}'
```

## Troubleshooting

**Migration job fails with permission denied:**
- Ensure `MC_CONFIG_DIR` and `HOME` env vars are set to `/tmp`

**Migration job fails with jq not found:**
- Use `awk` parsing instead (already fixed in current job manifests)

**Versioning not working after migration:**
- Existing objects have only 1 version (the migrated copy)
- New versions appear after editing documents
- Restart app backends to ensure fresh MinIO connections

## Storage Classes

| Environment | Storage Class |
|-------------|---------------|
| Local (Kind) | `csi-hostpath-sc` |
| ODCN | `ocs-storagecluster-ceph-rbd` |

The storage class is configured in the overlay kustomization patches.