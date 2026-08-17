# Backup & Restore Wizard

Interactive modal wizards for creating backups and restoring from backups directly from the project details page.

## What It Does

Adds two buttons to the Backups section of the project details page:

- **Backup aanmaken** - Opens a wizard to create a backup of a deployment's resources (PVCs, databases, MinIO buckets)
- **Herstellen** - Opens a wizard to restore from an existing backup run (only visible when backups exist)

Both buttons use the existing modal wizard infrastructure (`FormFlow` + `FormSection` with `TemplatePartial` layouts) with custom `post_save_action` types (`trigger_backup` / `trigger_restore`).

## How It Works

### Backup Flow

1. User clicks "Backup aanmaken"
2. Wizard shows a dropdown of deployments on the current cluster that have backupable resources (PVC, Database, or MinIO). Deployments without any backupable resources are excluded.
3. User selects a deployment from the dropdown; the resource type checkboxes update dynamically via HTMX to show only the types available for that deployment (all checked by default)
4. Review page shows confirmation summary
5. On confirm, a background task runs the backup with progress tracking

### Restore Flow

1. User clicks "Herstellen"
2. Wizard lists available backup runs grouped by `backup_run_id`, showing timestamp, deployment, and resource types
3. User selects a backup run
4. User selects restore mode: **existing deployment** or **new deployment**
   - **Existing**: Select an existing deployment as restore target
   - **New**: An info card explains the next step will configure the deployment
5. *(Only for new deployment mode)* User configures the new deployment using standard editables: name, clone-from, subdomain, base-domain, custom base domain, and domain format. Components and services are copied from the source deployment.
6. Review page shows confirmation with warning about data overwrite
7. On confirm, a background task runs:
   - For **new deployment**: creates the deployment in the project file, pre-creates PVCs with backup data, provisions infrastructure via `process_project_from_git` (ArgoCD adopts existing PVCs), then restores non-PVC resources (database, MinIO)
   - For **existing deployment**: restores each resource with versioning and progress tracking

## Architecture

### Files

| File | Purpose |
|------|---------|
| `opi/core/backup_tasks.py` | Background task wrappers (`run_backup_task`, `run_restore_task`) with `TaskProgressManager` |
| `opi/templates/wizard/partials/backup_select_deployment.html.j2` | Deployment + resource type selection |
| `opi/templates/wizard/partials/restore_select_backup.html.j2` | Backup run selection |
| `opi/templates/wizard/partials/restore_select_target.html.j2` | Target deployment selection (mode toggle) |
| `opi/forms/visualizers/wizard_sections.py` (`RESTORE_NEW_DEPLOYMENT_SECTION`) | New deployment config editables (step 3, conditional) |

### Key Design Decisions

- **No project file changes**: Backup/restore flows skip the YAML save step - they trigger operations directly (except when creating a new deployment, which modifies the project file)
- **Member-level auth**: Any project member can create backups and restore, not just admins/owners
- **Template context via yaml_data**: `TemplatePartial` rendering was extended to pass `yaml_data` as template context, enabling dynamic data (deployments, backup runs) in wizard partials
- **Custom post_save_action**: New action types `trigger_backup` and `trigger_restore` are handled in `_modal_do_submit()`, which creates background tasks instead of saving project files
- **Async context building**: Backup run data is gathered asynchronously during wizard initialization
- **HTMX deployment selection**: Changing the deployment dropdown triggers an HTMX GET to re-render the wizard step with updated resource type checkboxes for the selected deployment
- **Backupable service registry**: Services declare backup support via `backup_label` on their `ServiceDefinition`. The wizard dynamically discovers backupable services from `ServiceAdapter.get_backupable_labels()` - no hardcoded service type checks in the wizard code. Adding backup support for a new service type only requires setting `backup_label` on its definition.
- **Empty service filtering**: Dict service entries with `None` or empty values (e.g. `{"persistent-storage": null}`) are skipped during backup detection - these are unconfigured placeholders left by the form system, not active services. The wizard submission also strips these entries to keep project files clean.
- **v1/v2 compatibility**: Service detection works with both v2 (`services` key) and v1 (`uses-services` key) project file formats.
- **Clone-from type enums**: Clone types (`deployment`, `remote-source`, `backup`) and restore modes (`existing`, `new`) use `CloneFromType` and `RestoreMode` enums from `opi.services.services_enums` for type-safe comparison instead of raw strings.

### Data Flow

```
Button click → openEditModal()
  → GET /modal-wizard/{flow_id}
    → _build_backup_restore_context_async() (populates deployment/backup data)
    → Render wizard step with TemplatePartial (data available via yaml_data)
  → POST /step/{section_id} (raw form data stored directly, no editable validation)
  → Review page (summary_fn returns (label, value) pairs; the builder escapes them)
  → POST /confirm
    → _handle_backup_restore_submit()
      → create_task() → BackgroundTask(run_backup/restore_task)
      → Return progress template with task_id for polling
```

### PVC Pre-Restore for New Deployments

When restoring to a new deployment, PVC backup data is pre-created **before** infrastructure provisioning. This avoids a window where the pod runs with empty storage:

1. Deployment is created in the project file
2. Namespace is created via `ProjectManager.check_and_create_namespaces()` (reusing existing method)
3. PVCs are created and filled with backup data via `backup_manager.restore_to_project_pvc()`
4. `process_project_from_git` runs - ArgoCD adopts the existing PVCs (the `Replace=false` sync-option on `pvc.yaml.jinja` prevents recreation)
5. Non-PVC resources (database, MinIO) are restored after infrastructure is ready

PVC naming for the new deployment uses generation 0 (no version suffix), while the source PVC name preserves the original generation for Kopia snapshot lookup.

### Clone-from Type "backup"

When restoring to a new deployment, the `clone-from` field is set to `type: "backup"`. This signals to infrastructure managers (`database_manager`, `minio_manager`, `pvc_manager`) that they should create empty resources rather than attempting to live-clone data from another deployment. The restore process then fills these resources with backup data.

```yaml
clone-from:
  type: backup
  reference: source-deployment-name
  mode: once
```

## Scope

- Create backup for any deployment on the current cluster
- Restore to the same deployment or a different existing deployment
- Create a new deployment from a backup (copies source structure, restores data)
- Progress tracking with subtasks per resource type

## Future

- Cross-cluster restore
