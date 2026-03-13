# Migrate Task Progress to Database

## What

Migrate the wizard/background task progress tracking from the in-memory `TaskProgressManager` (in `opi/core/task_manager.py`) to the database-backed `AsyncTaskService` (in `opi/core/async_task_service.py`).

## Why

Currently there are two task systems running in parallel:

1. **In-memory** (`task_manager.py`): Used by the wizard flow, `simple_background.py`, and the web progress page. Tasks are stored in `_projects` / `_project_managers` dicts.
2. **Database-backed** (`async_task_service.py`): Used by the V2 API, `task_router.py`, and `TaskWorker`. Tasks are stored in PostgreSQL.

The wizard creates tasks via `task_manager.create_task()` (in-memory) and tracks progress via `TaskProgressManager` (also in-memory). The progress page polls `/api/tasks/{task_id}/status` which currently reads from in-memory as well. This works but has drawbacks:

- Tasks are lost on pod restart
- No cross-instance visibility (each OPI instance has its own in-memory state)
- Two parallel systems to maintain

## Scope

1. **`TaskProgressManager`**: Refactor to write all state changes (`add_task`, `complete_task`, `fail_task`, `update_current_step`, etc.) to the database via `AsyncTaskService` instead of in-memory dicts.

2. **`simple_background.py`**: The `process_project_yaml_background` and `process_project_background` functions use `TaskProgressManager` — these should work without changes once the manager writes to the DB.

3. **Wizard task creation**: Replace `from opi.core.task_manager import create_task` calls in `router_wizard.py`, `router.py`, and `router_detail_edit.py` with `AsyncTaskService.create_task()`.

4. **Polling endpoint** (`GET /api/tasks/{task_id}/status`): Switch back to reading from `AsyncTaskService` (the database). Currently reverted to in-memory as a workaround.

5. **Progress page** (`GET /projects/progress/{task_id}`): Switch from `task_manager.get_task()` to `AsyncTaskService.get_task()`.

6. **Modal wizard progress** (`GET /{project_name}/modal-wizard/progress/{task_id}` in `router_detail_edit.py`): Same migration.

7. **Monitoring** (`_monitor_project_progress`, `_start_monitoring_if_not_active`): These write logs/events to in-memory `_projects`. Need to write to the database instead.

## Dependencies

- `AsyncTaskService` needs to support the subtask hierarchy that `TaskProgressManager` provides (parent/child tasks with `add_task`/`add_subtask`).
- The `logs`, `events`, and `web_addresses` fields need to be writable via `AsyncTaskService`.
- The database schema (`tasks` table) must support all fields currently tracked in-memory.

## Files Involved

- `opi/core/task_manager.py` — `TaskProgressManager`, `_projects`, `_project_managers`
- `opi/core/async_task_service.py` — Database-backed service
- `opi/core/simple_background.py` — Background task processing
- `opi/web/router.py` — Progress page + polling endpoint
- `opi/web/router_wizard.py` — Wizard submission
- `opi/web/router_detail_edit.py` — Detail edit + modal wizard progress
