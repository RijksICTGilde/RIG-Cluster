# Backup Schedule

## Overview

Per-deployment automatic backup scheduling using RRULE (RFC 5545 subset) strings stored in the project YAML file. Users configure backup frequency, time, day, and resource types through the UI; the system stores an RRULE string and resource type list, and a background scheduler creates backup tasks when they're due.

## How It Works

### YAML Storage

Backup configuration is stored under `deployments[*]/backup/`:

```yaml
deployments:
  - name: production
    cluster: local
    backup:
      schedule: "FREQ=DAILY;BYHOUR=2;BYMINUTE=0"
      resource_types:
        - pvc
        - database
        - minio
```

**`schedule`** — RRULE string (RFC 5545 subset) controlling when backups run:
- `FREQ=DAILY` — every 24 hours
- `FREQ=WEEKLY` — every 7 days (with optional `BYDAY=MO`)
- `FREQ=MONTHLY` — every 30 days (with optional `BYMONTHDAY=15`)
- `BYHOUR` and `BYMINUTE` — preferred backup window (±60 minutes)

**`resource_types`** — list of backup targets to include. Defaults to all three if omitted:
- `pvc` — persistent volume claims (via kopia)
- `database` — PostgreSQL databases (via pg_dump)
- `minio` — MinIO buckets (via bucket mirror)

### UI Flow

#### Backup Schedule Modal

The backup schedule modal (`modal-edit-backup-schedule-{N}`) is a single-step save-only wizard on the project detail page (Deployments tab). One button per deployment.

**Fields:**
| Field | Type | Visibility | Description |
|-------|------|-----------|-------------|
| Herhaling (frequency) | Select | Always | DAILY / WEEKLY / MONTHLY / Geen |
| Tijd (time) | Select | When frequency is set | Preferred backup time (indicative) |
| Dag van de week | Select | When WEEKLY | Day of the week |
| Dag van de maand | Select | When MONTHLY | Day of the month |
| Resource types | Checkbox group | When frequency is set | Which resource types to back up |

**Dynamic field visibility:** The frequency select has `data-rerender="true"`, which triggers an HTMX re-render when changed. Dependent fields (time, day, monthday, resource_types) use `depends_on` and `show_when` to hide/show based on the selected frequency. When "Geen" is selected, all sub-fields are hidden.

**Processing order:** Transient fields (time, day, monthday) are processed before the frequency field so that `RRuleFrequencyConverter.write()` can read their values when composing the final RRULE string. Resource types are stored directly as a list.

#### Manual Backup Modal

The "Backup aanmaken" modal (`modal-backup`) uses the same editable framework:
- **Deployment** select — choose which deployment to back up
- **Resource types** checkbox group — same `BackupResourceTypesOptionsProvider` as the schedule modal

Both modals use `CHECKBOX_GROUP` widgets with `BackupResourceTypesOptionsProvider`, which derives available types from `ServiceAdapter.get_backupable_labels()`.

### Scheduler

`BackupScheduler` (`opi/core/backup_scheduler.py`) runs as a background task:

1. Checks all projects every `BACKUP_SCHEDULER_INTERVAL` seconds (default: 3600)
2. For each deployment on this cluster with a `backup.schedule` RRULE:
   - Parses the RRULE string
   - Checks the last completed backup task via `AsyncTaskService`
   - If elapsed time exceeds the frequency interval AND current time is within ±60 min of preferred time → creates a backup task
3. Reads `backup.resource_types` from YAML (defaults to `["pvc", "database", "minio"]` if not set)
4. Task payload: `{project_name, deployment_name, resource_types: [...], scheduled: true}`

### Task Execution

Backup tasks are processed by `TaskWorker` via the `async_tasks` PostgreSQL table:

1. Worker claims pending task (`SELECT FOR UPDATE SKIP LOCKED`)
2. `handle_backup()` in `task_handlers_backup.py` executes:
   - **PVC backup** — backs up persistent volume claims via kopia (if `pvc` in resource_types)
   - **Database backup** — pg_dump if deployment uses PostgreSQL service (if `database` in resource_types)
   - **MinIO backup** — bucket mirror if deployment uses MinIO service (if `minio` in resource_types)
3. Each resource type is a subtask within the single backup task
4. All share a `backup_run_id` (timestamp-based) for grouping in restore UI
5. Completed tasks are retained for 7 days, then cleaned up

### Manual Backups

The "Backup aanmaken" modal creates the same task type with the same payload structure. The only differences: no `scheduled: true` flag, and `created_by` is the user identity instead of `"backup-scheduler"`.

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `BACKUP_SCHEDULER_ENABLED` | `true` | Enable/disable the scheduler |
| `BACKUP_SCHEDULER_INTERVAL` | `3600` | Seconds between scheduler checks |
| `BACKUP_MAX_CONCURRENT` | `2` | Max concurrent backup tasks |

## Display

The Deployments tab shows the current schedule status per deployment:
- **With schedule:** "Backups ingeschakeld — Dagelijks rond 02:00"
- **Without schedule:** "Geen backup schema ingesteld"

Rendered by the `rrule_schedule` Jinja filter.

## Key Files

| File | Purpose |
|------|---------|
| `opi/forms/editables/fields/deployments.py` | Editable definitions (schedule + resource_types + manual backup) |
| `opi/forms/visualizers/fields/deployments.py` | Visualizer definitions (labels, widgets, data-rerender) |
| `opi/forms/visualizers/wizard_sections.py` | Section builder with processing order |
| `opi/forms/visualizers/providers.py` | `BackupResourceTypesOptionsProvider`, `BackupDeploymentOptionsProvider` |
| `opi/forms/editables/converters.py` | `RRuleFrequencyConverter`, `RRuleTimeConverter`, etc. |
| `opi/core/backup_scheduler.py` | Background scheduler (reads resource_types from YAML) |
| `opi/core/task_handlers_backup.py` | Backup execution handler |
| `opi/core/async_task_service.py` | Task queue (PostgreSQL) |
| `static/js/wizard.js` | `data-rerender` JS listener + `_rerender` cleanup |
| `tests/test_backup_pipeline.py` | Pipeline tests (YAML → scheduler → handler) |
| `tests/forms/test_backup_schedule_flow.py` | Form/converter unit tests |
| `tests/e2e/test_backup.py` | E2E browser tests |
