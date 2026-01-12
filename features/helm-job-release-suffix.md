# Helm Job Release Suffix (Cache Buster)

## Overview

When deploying Helm charts that include Kubernetes Jobs (such as database migrations, initial setup scripts, or superuser creation), you may encounter issues where Jobs don't re-run on subsequent deployments. This is because Kubernetes Job names must be unique - once a Job with a specific name has completed, it cannot be recreated with the same name.

## The Problem

Kubernetes Jobs are immutable after creation. When ArgoCD or Helm tries to sync a deployment:

1. If the Job already exists and completed successfully, Kubernetes won't recreate it
2. If the Job spec changes but the name stays the same, you'll get an error like:
   ```
   Job.batch 'app-backend-migrate-' is invalid: spec.template: Invalid value: ... field is immutable
   ```
3. Migration or setup jobs won't run even when you need them to (e.g., after schema changes)

## The Solution: Release Suffix

Add a `releaseSuffix` (or similar cache buster) to Job configurations in your Helm values:

```yaml
backend:
  jobs:
    releaseSuffix: "v1"
```

This suffix is appended to Job names, creating unique names like:
- `app-backend-migrate-v1`
- `app-backend-createsuperuser-v1`

## When to Update the Release Suffix

Increment the release suffix when you need Jobs to re-run:

1. **Database migrations**: After adding new migration files
2. **Initial data seeding**: When seed data needs to be refreshed
3. **Configuration changes**: When Job behavior depends on new ConfigMap/Secret values
4. **Superuser creation**: When credentials need to be reset

Example progression:
```yaml
# Initial deployment
releaseSuffix: "v1"

# After adding new migrations
releaseSuffix: "v2"

# After fixing a migration issue
releaseSuffix: "v3"
```

## Implementation in Helm Charts

Helm chart templates should include the release suffix in Job names:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ include "app.fullname" . }}-migrate-{{ .Values.backend.jobs.releaseSuffix }}
```

## Best Practices

1. **Use semantic versioning or sequential numbers**: `v1`, `v2`, `v3` or `20240108a`, `20240108b`
2. **Document changes**: Note why you incremented the suffix in commit messages
3. **Don't decrement**: Always move forward to avoid conflicts with existing Jobs
4. **Consider cleanup**: Old completed Jobs may accumulate; configure `ttlSecondsAfterFinished` in the Job spec

## Example: MijnBureau Docs Deployment

```yaml
backend:
  jobs:
    releaseSuffix: "v1"  # Increment when migrations need to re-run
```

This ensures that migration and setup Jobs run fresh on each deployment where the suffix changes.

## Troubleshooting

### Job stuck with trailing hyphen in name

If you see errors like:
```
Job.batch 'app-backend-migrate-' is invalid
```

This indicates the `releaseSuffix` is empty or not being passed correctly. Check:
1. The Helm values include `releaseSuffix`
2. The value is not empty or null
3. YAML parsing is working correctly (watch for tab characters)

### Jobs not running on sync

If Jobs exist but don't run:
1. Increment the `releaseSuffix`
2. Sync the deployment
3. ArgoCD will create new Jobs with the updated suffix
