# Backup & Restore Wizard

Interactive modal wizards for creating backups and restoring from backups directly from the project details page.

## What It Does

Adds two buttons to the Backups section of the project details page:

- **Backup aanmaken** — Opens a wizard to create a backup of a deployment's resources (PVCs, databases, MinIO buckets)
- **Herstellen** — Opens a wizard to restore from an existing backup run (only visible when backups exist)

Both buttons use the existing modal wizard infrastructure (`FormFlow` + `FormSection` with `TemplatePartial` layouts) with custom `post_save_action` types (`trigger_backup` / `trigger_restore`).

## How It Works

### Backup Flow

1. User clicks "Backup aanmaken"
2. Wizard shows deployments on the current cluster with available resource types
3. User selects a deployment and resource types (PVC, Database, MinIO — all checked by default)
4. Review page shows confirmation summary
5. On confirm, a background task runs the backup with progress tracking

### Restore Flow

1. User clicks "Herstellen"
2. Wizard lists available backup runs grouped by `backup_run_id`, showing timestamp, deployment, and resource types
3. User selects a backup run
4. User selects the target deployment (Phase 1: same deployment only)
5. Review page shows confirmation with warning about data overwrite
6. On confirm, a background task restores each resource with versioning and progress tracking

## Architecture

### Files

| File | Purpose |
|------|---------|
| `opi/core/backup_tasks.py` | Background task wrappers (`run_backup_task`, `run_restore_task`) with `TaskProgressManager` |
| `opi/templates/wizard/partials/backup_select_deployment.html.j2` | Deployment + resource type selection |
| `opi/templates/wizard/partials/restore_select_backup.html.j2` | Backup run selection |
| `opi/templates/wizard/partials/restore_select_target.html.j2` | Target deployment selection |

### Key Design Decisions

- **No project file changes**: Backup/restore flows skip the YAML save step — they trigger operations directly
- **Member-level auth**: Any project member can create backups and restore, not just admins/owners
- **Template context via yaml_data**: `TemplatePartial` rendering was extended to pass `yaml_data` as template context, enabling dynamic data (deployments, backup runs) in wizard partials
- **Custom post_save_action**: New action types `trigger_backup` and `trigger_restore` are handled in `_modal_do_submit()`, which creates background tasks instead of saving project files
- **Async context building**: Backup run data is gathered asynchronously during wizard initialization

### Data Flow

```
Button click → openEditModal()
  → GET /modal-wizard/{flow_id}
    → _build_backup_restore_context_async() (populates deployment/backup data)
    → Render wizard step with TemplatePartial (data available via yaml_data)
  → POST /step/{section_id} (raw form data stored directly, no editable validation)
  → Review page (summary_fn generates confirmation text)
  → POST /confirm
    → _handle_backup_restore_submit()
      → create_task() → BackgroundTask(run_backup/restore_task)
      → Return progress template with task_id for polling
```

## Phase 1 Scope

- Create backup for any deployment on the current cluster
- Restore backup to the **same** deployment it came from
- Progress tracking with subtasks per resource type

## Phase 2 (Future)

- Restore to a different existing deployment
- Clone deployment + restore (create new deployment from backup)
- Cross-cluster restore
