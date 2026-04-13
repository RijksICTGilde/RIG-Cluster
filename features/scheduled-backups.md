# Scheduled Backups

Automatic backup scheduling per deployment, running on a configurable daily, weekly, or monthly interval.

## Overview

Scheduled backups extend the existing manual backup system by allowing automatic backups per deployment. The backup scheduler runs inside the Operations Manager and creates backup tasks via the async task queue.

## Per-Deployment Configuration

Backup schedules are configured per deployment in the project YAML file using RRULE strings (RFC 5545 subset):

```yaml
# Project-level backup must be enabled (controls PVC labeling):
backup:
  enabled: true

deployments:
  - name: production
    backup:
      schedule: "FREQ=DAILY;BYHOUR=2;BYMINUTE=0"
      resource_types:
        - pvc
        - database
        - minio
  - name: staging
    # no backup section = no scheduled backups (manual only)
```

### Schedule Options

| Schedule | RRULE | Description |
|----------|-------|-------------|
| Daily | `FREQ=DAILY;BYHOUR=2;BYMINUTE=0` | Runs once every 24 hours at the preferred time |
| Weekly | `FREQ=WEEKLY;BYDAY=MO;BYHOUR=2;BYMINUTE=0` | Runs once every 7 days on the specified day |
| Monthly | `FREQ=MONTHLY;BYMONTHDAY=1;BYHOUR=2;BYMINUTE=0` | Runs once every 30 days on the specified day |

`BYHOUR` and `BYMINUTE` specify the preferred backup window (±60 minutes). `BYDAY` and `BYMONTHDAY` are optional parameters for weekly and monthly schedules respectively.

When no `backup` section is present on a deployment, no automatic backups are scheduled. Manual backups via the UI remain available.

## How It Works

1. The **BackupScheduler** runs as a background service inside the Operations Manager
2. Every 60 seconds (configurable), it checks all projects for deployments with a `backup.schedule`
3. For each scheduled deployment on the current cluster, it checks the last completed BACKUP task in the database
4. If sufficient time has elapsed (based on the schedule), a new backup task is created via the async task queue
5. The backup task covers all resource types: PVC, database, and MinIO

### Prerequisites

- Project-level `backup.enabled: true` must be set (controls PVC backup labels)
- The deployment must be on the cluster managed by this Operations Manager instance
- The async task worker must be running (`TASK_WORKER_ENABLED=true`)

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `BACKUP_SCHEDULER_ENABLED` | `true` | Enable/disable the backup scheduler |
| `BACKUP_SCHEDULER_INTERVAL` | `3600` | Seconds between schedule checks (hourly) |
| `BACKUP_MAX_CONCURRENT` | `2` | Max backup/restore tasks running simultaneously |

## Concurrency Control

Backup and restore tasks are limited to `BACKUP_MAX_CONCURRENT` (default 2) running simultaneously. When the limit is reached, additional pending backup/restore tasks stay in the queue and are picked up as slots free up. Other task types (deployments, image updates, etc.) are unaffected by this limit.

This is enforced at the task worker level via `claim_next_task` — the worker simply skips backup/restore tasks when the limit is reached, so other work continues unblocked.

## UI Configuration

The backup schedule can be configured per deployment from the project detail page:

1. Navigate to the Backups section on the project detail page
2. Click the "Backup schema" button next to a deployment
3. Select the desired schedule (Geen/Dagelijks/Wekelijks/Maandelijks)
4. Save

The current schedule is displayed as a tag next to each deployment name in the backups section.

## Interaction with Manual Backups

Manual backups and scheduled backups use the same async task queue and the same backup logic. A manual backup resets the schedule timer (the scheduler checks `completed_at` of the last backup task, regardless of whether it was manual or scheduled).

## Task Queue Integration

Backup and restore operations now use the PostgreSQL-backed async task queue (`AsyncTaskService`) instead of the legacy in-memory `TaskProgressManager`. This provides:

- Progress tracking that survives pod restarts
- Database-backed task deduplication (no duplicate backups for the same deployment)
- Consistent progress polling from the UI via both in-memory cache and database fallback
