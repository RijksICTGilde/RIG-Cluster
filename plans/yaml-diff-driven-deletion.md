# YAML Diff-Driven Deletion & Resource Cleanup

## Status: Implemented

This plan has been implemented. The notes below reflect the **actual implementation**, which diverged from the original plan in key areas.

## Context

When a user edits their project YAML file (removes a deployment, removes a component from a deployment, or removes a service), the system should detect these removals and clean up the corresponding infrastructure resources. The change detection pipeline (`ProjectFileHandler.analyze_project_changes()` using DeepDiff) existed but deletions were silently ignored.

### Bug Fixed

`_parse_deepdiff_path()` at `project_file_handler.py` had a regex that only converted `['key']` to `.key` but left bare numeric indices `[0]` untouched. This caused `_analyze_deployment_changes()` to miss all list-based deletions. **Fixed** by adding `re.sub(r"\[(\d+)]", r".\1", clean_path)` to convert numeric indices to dot notation.

### Implementation Decision: Fix DeepDiff Path Parsing (not bypass)

The original plan proposed a semantic change interpreter to bypass DeepDiff. The actual implementation took the simpler approach: **fix the regex bug** in `_parse_deepdiff_path()` and keep the existing DeepDiff-based `_analyze_deployment_changes()` pipeline. This avoids introducing a new module while solving the immediate problem.

### Data Safety Decision

When a deployment is removed from YAML, persistent data resources (databases, MinIO buckets, backups) are **marked for deferred deletion** with a configurable grace period (default 7 days). Ephemeral resources (ArgoCD apps, Keycloak clients, manifests) are deleted immediately. This allows accident recovery via git revert.

---

## What Was Built

### 1. Two-Phase Deletion in `DeleteProjectManager`

**New method**: `delete_deployment_from_yaml_change()` in `opi/manager/delete_project_manager.py`

- **Immediate cleanup**: ArgoCD application files, AppProject files, repository secrets, Keycloak clients, deployment manifests, subdomain registrations
- **Deferred cleanup (marked)**: PostgreSQL databases/users, MinIO buckets/users/policies, backup data (Kopia snapshots), namespaces with persistent resources

### 2. Marked-for-Deletion Service

**New file**: `opi/services/marked_for_deletion_service.py`

CRUD service layer for the `marked_for_deletion` database table. Supports mark, unmark, query by project/namespace, and deletion.

### 3. Database Schema & Migration

**New files**: `opi/core/marked_for_deletion_schema.py`, `opi/migrations/versions/002_add_marked_for_deletion.py`

Table with unique index on `(resource_type, resource_name, cluster)` to prevent duplicate marks. Upsert preserves original `marked_at` timestamp.

### 4. Reconciliation Job

**New file**: `opi/jobs/reconciliation.py`

- Unmarks resources that reappear in project YAMLs (git revert recovery)
- Purges resources past the grace period in correct dependency order
- Provides `cleanup_project()` for project-scoped cleanup
- Provides `reconcile()` for full reconciliation across all projects

### 5. Admin API

**New file**: `opi/api/admin_router.py`

Authenticated via `ADMIN_API_KEY`, all trigger endpoints default to `dry_run=true`:
- `GET /api/v2/admin/marked-for-deletion` — list marks
- `POST /api/v2/admin/cleanup/trigger` — purge expired marks for a project
- `POST /api/v2/admin/reconciliation/trigger` — full reconciliation
- `DELETE /api/v2/admin/marked-for-deletion/{mark_id}` — cancel a scheduled deletion

### 6. Wiring in `process_project_from_git()`

`project_manager.py` Step 1.6 processes deleted deployments before creations, calling `delete_deployment_from_yaml_change()` with a `MarkedForDeletionService` instance.

---

## Files Changed

| File | Action | What |
|------|--------|------|
| `opi/handlers/project_file_handler.py` | **MODIFIED** | Fixed `_parse_deepdiff_path()` regex for numeric indices |
| `opi/core/marked_for_deletion_schema.py` | **NEW** | SQL schema for `marked_for_deletion` table |
| `opi/services/marked_for_deletion_service.py` | **NEW** | CRUD service layer |
| `opi/migrations/versions/002_add_marked_for_deletion.py` | **NEW** | Alembic migration |
| `opi/manager/delete_project_manager.py` | **MODIFIED** | Added `delete_deployment_from_yaml_change()` |
| `opi/manager/project_manager.py` | **MODIFIED** | Wired deletion into `process_project_from_git()` |
| `opi/jobs/reconciliation.py` | **NEW** | Reconciliation job with `cleanup_project()` and `reconcile()` |
| `opi/api/admin_router.py` | **NEW** | Admin API endpoints |
| `opi/api/endpoint_util.py` | **MODIFIED** | Added `validate_admin_api_key` decorator |
| `opi/core/config.py` | **MODIFIED** | Added `DELETION_GRACE_PERIOD_DAYS` and `ADMIN_API_KEY` settings |

## Scope

**Implemented**: Deployment-level deletion detection and two-phase cleanup
**Deferred**: Component-level removal (Phase 3), Service-level removal (Phase 4)
