# Cleaning Up Old Versions After Clone/Restore

This document describes how to clean up old versioned resources (databases and buckets) after cloning or restoring operations. The generational versioning system preserves old resources, which may need manual cleanup.

## Overview

When using the generational versioning system for restore or clone operations:
- Old resources are **preserved** (never automatically deleted)
- New versioned resources are created alongside the old ones
- Manual cleanup is required to reclaim storage

This design ensures data safety - you can always roll back by changing the generation number in the project file.

## Understanding What Needs Cleanup

After a clone or restore operation, you may have multiple versions of resources:

```
Original (no version suffix):
  - Database: myproject_staging
  - Bucket: myproject-staging

After first clone/restore (generation: 1):
  - Database: myproject_staging_v1  <- active
  - Bucket: myproject-staging-v1    <- active
  - Old: myproject_staging, myproject-staging  <- orphaned

After second clone/restore (generation: 2):
  - Database: myproject_staging_v2  <- active
  - Bucket: myproject-staging-v2    <- active
  - Old: myproject_staging, myproject_staging_v1  <- orphaned
  - Old: myproject-staging, myproject-staging-v1  <- orphaned
```

## Pre-Cleanup Checklist

Before cleaning up old versions, verify:

1. **Application is stable** on the new version
2. **Check the project file** for the current generation:
   ```yaml
   deployments:
     - name: staging
       services:
         - reference: minio-storage
           config:
             generation: 2  # Current active version is v2
         - reference: database
           config:
             generation: 2  # Current active version is v2
   ```
3. **Confirm rollback is not needed** - once deleted, data cannot be recovered

## Cleaning Up PostgreSQL Databases

### List All Databases for a Project

Connect to PostgreSQL and list databases:

```bash
# Using psql directly
psql -h <postgres-host> -U postgres -c "\l" | grep myproject

# Or using kubectl exec
kubectl exec -it <postgres-pod> -n <namespace> -- psql -U postgres -c "\l" | grep myproject
```

Expected output:
```
myproject_staging      | myproject_staging | UTF8     | ...
myproject_staging_v1   | myproject_staging | UTF8     | ...
myproject_staging_v2   | myproject_staging | UTF8     | ...
```

### Identify Orphaned Databases

Compare the database list with the generation in your project file:
- If `generation: 2`, then `myproject_staging_v2` is active
- `myproject_staging` and `myproject_staging_v1` are orphaned

### Drop Orphaned Databases

**WARNING**: This permanently deletes data. Ensure you have backups if needed.

```bash
# Connect as superuser
psql -h <postgres-host> -U postgres

# Terminate connections to the old database
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = 'myproject_staging_v1';

# Drop the database
DROP DATABASE myproject_staging_v1;

# Repeat for other old versions
DROP DATABASE myproject_staging;
```

### Drop Associated Schema and User (if applicable)

If the old database had a dedicated schema and user:

```bash
# Connect to the database cluster
psql -h <postgres-host> -U postgres

# Drop the old user (after dropping database)
DROP USER IF EXISTS myproject_staging;
DROP USER IF EXISTS myproject_staging_v1;
```

## Cleaning Up MinIO Buckets

### List All Buckets for a Project

Using the MinIO client (`mc`):

```bash
# Configure mc alias (if not already done)
mc alias set myminio http://minio:9000 <access-key> <secret-key>

# List all buckets
mc ls myminio/ | grep myproject
```

Expected output:
```
[2024-01-15 10:30:22 UTC]     0B myproject-staging/
[2024-01-16 14:22:11 UTC]     0B myproject-staging-v1/
[2024-01-17 09:15:33 UTC]     0B myproject-staging-v2/
```

### Check Bucket Sizes Before Deletion

```bash
# Check size of each bucket
mc du myminio/myproject-staging
mc du myminio/myproject-staging-v1
mc du myminio/myproject-staging-v2
```

### Identify Orphaned Buckets

Compare the bucket list with the generation in your project file:
- If `generation: 2`, then `myproject-staging-v2` is active
- `myproject-staging` and `myproject-staging-v1` are orphaned

### Delete Orphaned Buckets

**WARNING**: This permanently deletes data. Ensure you have backups if needed.

```bash
# Delete bucket contents and the bucket itself
mc rb --force myminio/myproject-staging-v1

# Repeat for other old versions
mc rb --force myminio/myproject-staging
```

### Remove Associated MinIO Users/Policies (if applicable)

If dedicated users were created for old buckets:

```bash
# Using mc admin
mc admin user remove myminio myproject_staging_v1
mc admin policy detach myminio myproject_staging_v1-myproject-staging-v1-policy --user myproject_staging_v1
mc admin policy remove myminio myproject_staging_v1-myproject-staging-v1-policy
```

## Automating Cleanup (Future)

Currently, cleanup is manual. Future versions may include:

- API endpoint: `DELETE /api/v1/cleanup/database/{namespace}/{reference_name}?keep_versions=1`
- API endpoint: `DELETE /api/v1/cleanup/bucket/{namespace}/{reference_name}?keep_versions=1`
- Automatic cleanup after configurable retention period

## Best Practices

1. **Wait before cleanup**: Allow time to verify the new version works correctly (e.g., 24-48 hours)
2. **Create backups first**: Even if you're deleting old data, consider backing it up first
3. **Document what you delete**: Keep a log of cleanup operations for audit purposes
4. **Clean up regularly**: Don't let old versions accumulate indefinitely

## Rolling Back (Alternative to Cleanup)

If you need to roll back to a previous version instead of cleaning up:

1. **Update the project file** to use the old generation:
   ```yaml
   services:
     - reference: minio-storage
       config:
         generation: 1  # Changed from 2 back to 1
   ```

2. **Commit and push** the project file change

3. **Trigger a project refresh** - the application will switch to the older version

4. **Note**: Rolling back to generation 0 (no suffix) requires setting `generation: 0` explicitly

## Troubleshooting

### Cannot drop database - active connections

```bash
# Force terminate all connections
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = 'myproject_staging_v1'
  AND pid <> pg_backend_pid();
```

### Bucket deletion fails - access denied

Ensure you're using credentials with delete permissions:

```bash
# Check current user's permissions
mc admin user info myminio <username>
```

### Unsure which version is active

1. Check the project file in git for the current generation
2. Check the Kubernetes secret for the database/bucket name:
   ```bash
   kubectl get secret <deployment>-database-credentials -n <namespace> -o jsonpath='{.data.database}' | base64 -d
   kubectl get secret <deployment>-minio-credentials -n <namespace> -o jsonpath='{.data.bucket}' | base64 -d
   ```
