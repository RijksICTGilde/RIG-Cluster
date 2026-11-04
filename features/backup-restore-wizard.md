# Backup & Restore Wizard

Interactive modal wizards for creating backups and restoring from backups directly from the project details page.

## What It Does

Adds two buttons to the Backups section of the project details page:

- **Backup aanmaken** - Opens a wizard to create a backup of a deployment's resources (PVCs, databases, MinIO buckets)
- **Herstellen** - Opens a wizard to restore from an existing backup run (only visible when backups exist, and only for the roles that may restore)

Who may what: a backup is for every project member, a restore is not. See
`features/rollen-voor-operationele-handelingen.md` and "Who may restore" below.

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
   - **Existing**: Select an existing deployment as restore target. Only the data is put back, the deployment itself is left alone.
   - **New**: An info card explains the next step will configure the deployment
5. *(Only for new deployment mode)* User configures the new deployment using standard editables: name, environment, **config overnemen van** (the config source), components, subdomain, base-domain, custom base domain, and domain format. See "Restoring into a new deployment" below.
6. Review page shows confirmation with warning about data overwrite
7. On confirm, a background task runs:
   - For **new deployment**: creates the deployment in the project file, pre-creates PVCs with backup data, provisions infrastructure via `process_project_from_git` (ArgoCD adopts existing PVCs), then restores non-PVC resources (database, MinIO)
   - For **existing deployment**: restores each resource with versioning and progress tracking

## Restoring into a new deployment

This usually goes from production to a (possibly new) production, so the new deployment has
to run the same images and the same env vars as the source: otherwise the restored data sits
next to an application that does not match it. It is therefore a clone-from, with a backup
run as its data source instead of a running deployment.

The info step asks **Config overnemen van**: every deployment of the project, defaulting to
the deployment the backup was taken from, plus "Geen (lege deployment)".

| What | With a source | Without a source |
|---|---|---|
| Image per component | from the source, editable in the components step | empty, filled in by hand |
| `user-env-vars` per component | from the source, as ciphertext | none |
| The rest of the deployment config (services, repository, cluster, namespace) | from the source | the system fields from the deployment the backup was taken from |
| Hostnames (`subdomain`, `base-domain`, ...) | never from the source, the web-address step decides them | the web-address step |
| `backup` and a service's runtime state | never | never |

The wizard does not show the env vars. They travel as AGE ciphertext: the project, and so
the AGE key, is the same one, so nothing is decrypted. Changing them is the env-vars dialog's
job, with its own step-up.

What a new deployment inherits from an existing one is one derivation,
`derive_deployment_from_source` in `opi/manager/clone_validation.py`. The upsert with
`cloneFrom` uses it too, so cloning and restoring cannot drift apart; see
`features/kloon-erft-de-vorm-van-het-webadres.md`. On top of that,
`build_restore_target_deployment` adds the wizard's choices and the `clone-from` pointing at
the backup run.

That builder runs twice on the same input: in the confirmation request on a copy of the
project, to weigh the rights and the step-up against the state it would produce, and in the
restore task, to save it. The task saves and not the wizard, because `_pre_restore_pvcs` has
to fill the PVCs before ArgoCD syncs the project.

## Who may restore

| Check | Weighed on | When |
|---|---|---|
| `restore.run` | the source deployment and the target deployment | always |
| `deployment.clone-from` | the deployment the config is inherited from | when there is a config source |
| `environment.set` | the project | when the target lands on production |
| step-up | the resulting state (`save_requires_step_up`) | when that state asks for a fresh code |

`restore.run` is weighed on the source and the target, no longer on the highest marking in
the project. A developer can restore between two non-production deployments inside a project
that has production, and cannot pick a production source or a production target: that is ops,
with a step-up. The judgement is about the marking of the source and of the target, not about
which wizard someone opened.

The wizard itself opens as soon as restoring is allowed on at least one deployment of the
project (`require_on_any_deployment`); the real decision falls at the confirmation, and that
is the one the audit records.

## Architecture

### Files

| File | Purpose |
|------|---------|
| `opi/core/backup_tasks.py` | Background task wrappers (`run_backup_task`, `run_restore_task`) with `TaskProgressManager` |
| `opi/templates/wizard/partials/backup_select_deployment.html.j2` | Deployment + resource type selection |
| `opi/templates/wizard/partials/restore_select_backup.html.j2` | Backup run selection |
| `opi/templates/wizard/partials/restore_select_target.html.j2` | Target deployment selection (mode toggle) |
| `opi/forms/visualizers/wizard_sections.py` (`build_restore_new_deployment_sections`) | New deployment config editables (steps 3 to 5, conditional) |
| `opi/manager/clone_validation.py` | `derive_deployment_from_source` and `build_restore_target_deployment` |

### Key Design Decisions

- **No project file changes**: Backup/restore flows skip the YAML save step - they trigger operations directly (except when creating a new deployment, which modifies the project file)
- **Member-level auth**: Any project member can create backups. Restore is weighed on the source and the target deployment, see "Who may restore"
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

When restoring to a new deployment, the `clone-from` field is set to `type: "backup"`. This signals to infrastructure managers (`database_manager`, `minio_manager`, `seaweedfs_manager`, `pvc_manager`) that they should create empty resources rather than attempting to live-clone data from another deployment. The restore process then fills these resources with backup data.

```yaml
clone-from:
  type: backup
  reference: productie          # the deployment the backup was taken from
  run: '20261030084847'         # the backup_run_id to restore
  mode: once
```

`run` names the backup run, and `run` only: which snapshots that run holds is runtime state at Kopia, not project data. Each manager looks the run up itself through `clone_backup_items` (`opi/manager/backup/runs.py`) and picks the item for its own resource type. A run that no longer exists is an error naming the run and the source deployment, not an empty restore that reports success.

## Scope

- Create backup for any deployment on the current cluster
- Restore to the same deployment or a different existing deployment
- Create a new deployment from a backup: inherit an existing deployment's config (images and env vars included) or start empty, then restore the data
- Progress tracking with subtasks per resource type

## Future

- Cross-cluster restore
