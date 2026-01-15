# MinIO Migration to Versioned Storage

This folder contains resources for migrating from the old MinIO (filesystem mode) to the new MinIO with erasure coding (versioned storage support).

## Why This Migration?

The docs application requires S3 object versioning to support document version history. MinIO in filesystem mode (`minio server /data`) does not support versioning. Erasure coding mode (`minio server /data/disk{1...4}`) enables versioning support.

## Migration Phases

### Phase 1: Deploy New MinIO Stack (Argo-managed)

After pushing the infrastructure changes, Argo will deploy:
- `minio-versioned` deployment (erasure coding mode)
- `minio-versioned` service
- `minio-storage-versioned` PVC

The old MinIO continues running and serving traffic.

### Phase 2: Run Migration Job (Manual)

Apply the migration job to copy data from old to new MinIO:

```bash
# For local (Kind) cluster:
kubectl apply -f job-migrate-to-versioned.yaml

# Watch the job progress:
kubectl logs -f job/minio-migration-to-versioned -n rig-system
```

The job will:
1. Set up mc aliases for both MinIO instances
2. List and create buckets on new MinIO
3. Enable versioning on each bucket
4. Mirror all objects with metadata preserved

### Phase 3: Switch Applications to New MinIO

Once migration is verified:

1. **For local cluster**: Update the local overlay to patch existing MinIO deployment to use erasure coding and versioned PVC

2. **For production (ODCN)**:
   - Optionally change service port temporarily to block access during switch
   - Update applications to point to `minio-versioned:9000`
   - Or swap service selectors

3. **Cleanup**: Remove old MinIO deployment and PVC after verification

## Production Considerations

For ODCN, you may want to:
- Scale down apps using MinIO before migration
- Or temporarily change the `minio` service to a different port to prevent access during migration

## Verification

After migration, verify:
```bash
# Check buckets exist on new MinIO
kubectl exec -n rig-system deploy/minio-versioned -- mc ls local/

# Check versioning is enabled
kubectl exec -n rig-system deploy/minio-versioned -- mc version info local/BUCKET_NAME

# Test creating a version (upload same file twice)
```

## Rollback

If issues occur:
- Old MinIO is still running with original data
- Simply continue using old MinIO service
- Delete the versioned resources if needed
