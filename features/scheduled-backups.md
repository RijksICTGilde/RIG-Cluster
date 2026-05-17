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

`BYHOUR` and `BYMINUTE` are interpreted in **Europe/Amsterdam** wall-clock time. Backups fire at or shortly after the configured moment — within ~10 minutes, depending on the scheduler tick interval. `BYDAY` (single day code like `MO`, or a comma-separated list like `MO,WE,FR` per RFC 5545) and `BYMONTHDAY` are optional parameters for weekly and monthly schedules respectively. For MONTHLY with a `BYMONTHDAY` larger than the current month allows (e.g. 31 in February), the last day of the month is used — standard cron semantics.

### Resource Types

- `pvc` — persistent volume claims (via kopia)
- `database` — PostgreSQL databases (via pg_dump)
- `minio` — MinIO buckets (via bucket mirror)

Defaults to all three if `resource_types` is omitted.

When no `backup` section is present on a deployment, no automatic backups are scheduled. Manual backups via the UI remain available.

## How It Works

1. The **BackupScheduler** runs as a background service inside the Operations Manager.
2. Ticks are **anchored to wall-clock boundaries** of `BACKUP_SCHEDULER_INTERVAL` seconds (default 600). With a 600s interval, ticks fire at HH:00, HH:10, HH:20, … regardless of when OPI started — restarting the pod does not shift the firing phase.
3. Each tick walks all projects on this cluster. For each deployment with a `backup.schedule`, a backup is due when:
   - **Today is a target day** for the schedule (`FREQ`/`BYDAY`/`BYMONTHDAY`), AND
   - **Current Amsterdam wall-clock time is at or past today's `BYHOUR:BYMINUTE`** but still **within the catch-up window** (`BACKUP_SCHEDULE_CATCH_UP_SECONDS`, default 4 hours), AND
   - **No scheduled backup has completed at or after today's target time** (manual backups are ignored for this check, so a user-triggered run does not suppress the next automatic one).
4. When due, a backup task is created via the async task queue with `payload.trigger = "scheduled"`.
5. The backup task covers the configured `resource_types`.

This catches up automatically after brief OPI downtime: if the pod was offline at 02:00 (the configured target) and starts at 03:30, the next tick at 03:40 sees "past today's target AND no scheduled backup yet today AND within catch-up window" and fires immediately.

The catch-up window prevents late-day surprise runs. If OPI has been down all day or the schedule is freshly added late in the day, the scheduler will **not** fire a 02:00 backup at, say, 22:00 — it skips today and waits for tomorrow's 02:00. Bump `BACKUP_SCHEDULE_CATCH_UP_SECONDS` if your downtime windows can exceed 4 hours.

### Prerequisites

- Project-level `backup.enabled: true` must be set (controls PVC backup labels)
- The deployment must be on the cluster managed by this Operations Manager instance
- The async task worker must be running (`TASK_WORKER_ENABLED=true`)

### Task Execution

Backup tasks are processed by `TaskWorker` via the `async_tasks` PostgreSQL table:

1. Worker claims pending task (`SELECT FOR UPDATE SKIP LOCKED`)
2. `handle_backup()` in `task_handlers_backup.py` executes each resource type as a subtask
3. All subtasks share a `backup_run_id` (timestamp-based) for grouping in restore UI
4. Completed tasks are retained for 7 days, then cleaned up

## UI Configuration

The backup schedule can be configured per deployment from the project detail page:

1. Navigate to the Backups section on the project detail page
2. Click the "Backup schema" button next to a deployment
3. Select the desired schedule (Geen/Dagelijks/Wekelijks/Maandelijks)
4. Configure time, day, and resource types as needed
5. Save

The current schedule is displayed as a tag next to each deployment name in the backups section (e.g. "Dagelijks rond 02:00").

### UI Fields

| Field | Type | Visibility | Description |
|-------|------|-----------|-------------|
| Herhaling (frequency) | Select | Always | DAILY / WEEKLY / MONTHLY / Geen |
| Tijd (time) | Select | When frequency is set | Preferred backup time (indicative) |
| Dag van de week | Select | When WEEKLY | Day of the week |
| Dag van de maand | Select | When MONTHLY | Day of the month |
| Resource types | Checkbox group | When frequency is set | Which resource types to back up |

The frequency select triggers an HTMX re-render (`data-rerender="true"`) to show/hide dependent fields.

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `BACKUP_SCHEDULER_ENABLED` | `true` | Enable/disable the backup scheduler |
| `BACKUP_SCHEDULER_INTERVAL` | `600` | Seconds between scheduler ticks. Ticks are anchored to wall-clock boundaries (e.g. 600 → HH:00, HH:10, HH:20…). Maximum firing latency after the configured time equals this value. |
| `BACKUP_SCHEDULE_CATCH_UP_SECONDS` | `14400` | Catch-up window after the configured `BYHOUR:BYMINUTE`. Backups missed inside this window fire late; missed outside it wait for the next target day. Default 4 hours. |
| `BACKUP_MAX_CONCURRENT` | `2` | Max backup/restore tasks running simultaneously |

## Concurrency Control

Backup and restore tasks are limited to `BACKUP_MAX_CONCURRENT` (default 2) running simultaneously. When the limit is reached, additional pending backup/restore tasks stay in the queue and are picked up as slots free up. Other task types (deployments, image updates, etc.) are unaffected by this limit.

This is enforced at the task worker level via `claim_next_task` — the worker simply skips backup/restore tasks when the limit is reached, so other work continues unblocked.

## Interaction with Manual Backups

Manual and scheduled backups use the same async task queue and the same backup logic, but are tracked independently via `payload.trigger` (`"manual"` or `"scheduled"`):

- **The scheduler ignores manual runs** when checking "have we already run today?" — a user-triggered backup at 14:00 does not suppress the next automatic 02:00 run.
- **Manual snapshots are exempt from automatic retention.** Each backup gets a stable Kopia source identity (`<project>-<deployment>-<kind>-<resource>` for scheduled, `…-manual` suffix for manual). Retention (`kopia policy set` + `kopia snapshot expire`) is applied per-source, so the scheduled run's expiry never touches a manual snapshot. Manual snapshots persist until an operator deletes them explicitly.
- Backup runs in the UI are tagged "auto" or "handmatig" so the distinction is visible at a glance.

Legacy tasks created before the `trigger` field existed are treated as `scheduled` (the historical default).

## Key Files

| File | Purpose |
|------|---------|
| `opi/core/backup_scheduler.py` | Background scheduler |
| `opi/core/task_handlers_backup.py` | Backup execution handler |
| `opi/core/async_task_service.py` | Task queue (PostgreSQL) |
| `opi/forms/editables/fields/deployments.py` | Editable definitions (schedule + resource_types) |
| `opi/forms/visualizers/fields/deployments.py` | Visualizer definitions (labels, widgets) |
| `opi/forms/visualizers/wizard_sections.py` | Section builder with processing order |
| `opi/forms/visualizers/providers.py` | `BackupResourceTypesOptionsProvider`, `BackupDeploymentOptionsProvider` |
| `opi/forms/editables/converters.py` | `RRuleFrequencyConverter`, `RRuleTimeConverter`, etc. |
| `opi/utils/rrule_utils.py` | Shared RRULE parsing and formatting |
| `static/js/wizard.js` | `data-rerender` JS listener |
| `tests/test_backup_pipeline.py` | Pipeline tests |
| `tests/forms/test_backup_schedule_flow.py` | Form/converter unit tests |
| `tests/e2e/test_backup.py` | E2E browser tests |
