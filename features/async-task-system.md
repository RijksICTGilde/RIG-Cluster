# Async Task System

**Status**: Implemented
**Date**: 2026-03-01
**Related**: [Federation Routing](./federation-routing.md) (builds on top of this)

## Problem

Most deployment API endpoints block for 5-30+ minutes, causing HTTP 504 timeouts. The web portal has a working async pattern (`BackgroundTasks` + in-memory `TaskProgressManager`), but it is single-instance, in-memory, and not used by the REST API.

| Endpoint | Method | Typical Duration |
|----------|--------|-----------------|
| `/api/projects/{name}/:upsert-deployment` | POST | 5-30 min |
| `/api/projects/{name}/deployments/{dep}/image` | PUT | 3-15 min |
| `/api/projects/{name}/{dep}` | DELETE | 5-15 min |
| `.../:clone-database-from-external` | POST | 10-60 min |
| `.../:clone-bucket-from-external` | POST | 10-30 min |
| `.../:refresh` | GET | 5-20 min |

### Current Limitations

- **In-memory only** -- tasks lost on pod restart
- **Single-process only** -- cannot scale to multiple OPI replicas
- **No retry mechanism** -- failed tasks are not retried
- **No persistence** -- no audit trail or recovery
- **API endpoints block** -- only the web portal uses the background task pattern

### Existing Patterns We Build On

- `CLUSTER_MANAGER` setting -- each OPI manages exactly one cluster (`config.py`)
- `MASTER_API_KEY` + `validate_master_api_key` -- admin auth already exists (`endpoint_util.py`)
- `DatabasePool` -- asyncpg connection pool for PostgreSQL (`core/database_pool.py`)
- `TaskProgressManager` -- in-memory task tracking with subtasks (`core/task_manager.py`)
- `simple_background.py` -- existing background project processing
- Project YAML `cluster` field on deployments -- routing info already in data model

---

## Architecture Overview

### Target Architecture: Frontend + Workers

The system separates into two roles that communicate exclusively through the PostgreSQL `async_tasks` table:

```
                          Shared PostgreSQL (rig-db, same cluster)
                          +----------------------------------+
                          |         async_tasks table        |
                          +--^-----------+----------^--------+
                             |           |          |
                  INSERT task|    SELECT |   UPDATE |  progress/result
                             |    status |          |
                +------------+--+  +-----v---+  +--+----------+
                |  Frontend OPI |  | Client   |  | Worker 1   |
                |  (API server) |  | (polls)  |  | (executor) |
                |               |  +----------+  |            |
                | - Serves HTTP |                 | - Claims   |
                | - Creates     |                 |   tasks    |
                |   tasks       |  +----------+  | - Runs     |
                | - Reads       |  | Worker 2 |  |   handlers |
                |   status      |  | (executor)|  | - Writes   |
                | - No task     |  |           |  |   progress |
                |   execution   |  +-----------+  +------------+
                +---------------+
```

**Frontend OPI** (API server): Receives HTTP requests, inserts tasks into the database, reads task status back for clients. Never executes tasks itself.

**Worker(s)**: Separate process(es) that claim tasks via `SELECT ... FOR UPDATE SKIP LOCKED`, execute them (calling ProjectManager, connectors, etc.), and write progress + results back to the database. Scale by adding more worker pods.

**PostgreSQL**: The shared communication channel. The frontend writes tasks, workers read and update them, clients poll status through the frontend.

Since workers are separate processes that don't share memory with the frontend, all progress (current_step, subtasks, logs, etc.) must be written to the database. This is why the schema includes progress columns -- the frontend has no other way to know what a worker is doing.

### Starting Point: Combined Mode

For the initial implementation, the frontend and worker run in the **same process** (worker as an asyncio background task inside the FastAPI lifespan). This matches the current architecture and avoids requiring a separate deployment.

```
Combined OPI (current setup, single pod)
  |
  +-- FastAPI (serves HTTP, creates tasks, reads status)
  +-- TaskWorker (asyncio loop, claims + executes tasks)
  |
  Shared PostgreSQL
```

The combined mode uses the exact same code paths. The only difference is that `TASK_WORKER_ENABLED=True` starts the worker loop inside the API server process. Setting it to `False` creates a frontend-only instance. Running the worker separately is a deployment change, not a code change.

### Scaling Path

```
Phase 1 (now):     Combined OPI pod          -- single process, worker inside API server
Phase 2 (scale):   1 Frontend + N Workers    -- separate pods, same DB, same cluster
Phase 3 (future):  Federation routing        -- multiple clusters, HTTP between them
```

**Binary data (images) are NOT stored in the payload.** Image endpoints that need async handling should first store the image (e.g., to MinIO or a temp path) and put a reference in the payload instead.

---

## 1. Database Schema

**File**: `opi/core/async_task_schema.py` (new)

Table created at startup via `CREATE TABLE IF NOT EXISTS` (same pattern as existing `SUBDOMAIN_REGISTRY_TABLE_SQL` in `connectors/subdomain.py`). Uses the local rig-db PostgreSQL.

```sql
CREATE TABLE IF NOT EXISTS async_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_type VARCHAR(64) NOT NULL,
    project_name VARCHAR(63) NOT NULL,
    deployment_name VARCHAR(63),
    cluster VARCHAR(63) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    payload JSONB NOT NULL DEFAULT '{}',
    result JSONB,
    error_message TEXT,
    current_step VARCHAR(255) DEFAULT 'Queued',
    progress_percent SMALLINT DEFAULT 0,
    subtasks JSONB DEFAULT '[]',
    logs TEXT[],
    events JSONB,
    web_addresses JSONB,
    claimed_by VARCHAR(255),
    claimed_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_by VARCHAR(255),
    attempt_count SMALLINT NOT NULL DEFAULT 0,
    max_attempts SMALLINT NOT NULL DEFAULT 3,
    affects_deployments VARCHAR(63)[]
);

CREATE INDEX IF NOT EXISTS idx_async_tasks_pending
    ON async_tasks(status, created_at)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_async_tasks_heartbeat
    ON async_tasks(status, heartbeat_at)
    WHERE status IN ('claimed', 'running');

CREATE INDEX IF NOT EXISTS idx_async_tasks_project
    ON async_tasks(project_name, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_async_tasks_deployment
    ON async_tasks(project_name, deployment_name, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_async_tasks_completed
    ON async_tasks(status, completed_at)
    WHERE status IN ('completed', 'failed', 'cancelled');

CREATE INDEX IF NOT EXISTS idx_async_tasks_affects
    ON async_tasks USING GIN (affects_deployments);
```

### Design Decisions

- **JSONB for payload**: Each task type has different request parameters. Storing as JSONB avoids schema proliferation.
- **subtasks as JSONB array**: Mirrors the existing `TaskProgressManager.tasks` dict but stored persistently. At most ~15 subtasks per operation.
- **heartbeat_at**: Essential for stale task recovery when a pod dies mid-task.
- **affects_deployments**: The deployments a task reprocesses, written once by `create_task()`
  from `scope_of()`. NULL means project-wide. Claiming compares these scopes for OVERLAP, so a
  project-wide task and a deployment-scoped task of the same project no longer run at the same
  time. Added in migration 005 - see `features/taakscope-en-de-uitrolwacht.md`.
- **cluster column**: Each task records which cluster it targets. Workers only claim tasks matching their own `CLUSTER_MANAGER`. This is also the foundation for federation routing later.
- **No separate subtasks table**: The subtask count is small enough that a JSONB array is sufficient.

---

## 2. AsyncTaskService

**File**: `opi/core/async_task_service.py` (new)

Service layer using the existing `DatabasePool` (from `opi/core/database_pool.py`). All database operations go through this service.

### Enums

```python
class TaskType(str, Enum):
    UPSERT_DEPLOYMENT = "upsert_deployment"
    UPDATE_IMAGE = "update_image"
    DELETE_DEPLOYMENT = "delete_deployment"
    CLONE_DATABASE = "clone_database"
    CLONE_BUCKET = "clone_bucket"
    REFRESH_DEPLOYMENT = "refresh_deployment"
    CREATE_PROJECT = "create_project"

class AsyncTaskStatus(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
```

### Key Methods

| Method | SQL Pattern | Description |
|--------|-------------|-------------|
| `create_task()` | INSERT + dedup check | Create a task. If same project+deployment+type is already pending/running, return existing task ID |
| `claim_next_task()` | `SELECT ... FOR UPDATE SKIP LOCKED` | Claim next pending task for this cluster |
| `start_task()` | UPDATE status=running | Mark task as started |
| `update_progress()` | UPDATE current_step, progress_percent, subtasks, heartbeat_at | Update task progress |
| `send_heartbeat()` | UPDATE heartbeat_at | Keep-alive signal from worker |
| `complete_task()` | UPDATE status=completed, result, completed_at | Mark task as done with result |
| `fail_task()` | Re-queue or status=failed | Re-queue if attempts < max, else mark failed |
| `get_task()` | SELECT by id | Get single task status |
| `list_tasks()` | SELECT with filters | List tasks filtered by project, deployment, status |
| `recover_stale_tasks()` | UPDATE WHERE heartbeat stale | Reset tasks from dead workers |
| `cleanup_old_tasks()` | DELETE WHERE completed > retention | Remove old completed/failed tasks |

### Task Claiming (Multi-Instance Safe)

```sql
BEGIN;
SELECT id, task_type, payload, ...
FROM async_tasks
WHERE status = 'pending'
  AND cluster = $cluster_manager
  -- geen actieve taak van dit project met een overlappende scope, en geen oudere
  -- wachtende taak van dit project met een overlappende scope (RC-166)
ORDER BY created_at ASC
LIMIT 1
FOR UPDATE SKIP LOCKED;

UPDATE async_tasks
SET status = 'claimed', claimed_by = $instance_id, claimed_at = NOW()
WHERE id = $task_id;
COMMIT;
```

`SKIP LOCKED` ensures no deadlocks or contention between multiple OPI replicas sharing the same database.

### Stale Task Recovery (every 60s)

```sql
UPDATE async_tasks
SET status = 'pending', claimed_by = NULL, claimed_at = NULL,
    heartbeat_at = NULL, attempt_count = attempt_count + 1
WHERE status IN ('claimed', 'running')
  AND heartbeat_at < NOW() - INTERVAL '120 seconds'
  AND attempt_count < max_attempts;

UPDATE async_tasks
SET status = 'failed',
    error_message = 'Worker died, max retries exceeded',
    completed_at = NOW()
WHERE status IN ('claimed', 'running')
  AND heartbeat_at < NOW() - INTERVAL '120 seconds'
  AND attempt_count >= max_attempts;
```

### Deduplication

When creating a task, check if one already exists for the same `(project_name, deployment_name, task_type)` with status in `('pending', 'claimed', 'running')`. If so, return the existing task ID instead of creating a duplicate.

---

## 3. Task Worker

**File**: `opi/core/task_worker.py` (new)

The worker is a standalone class that only needs a `DatabasePool` and the task handler functions. It has no dependency on FastAPI, HTTP, or the API layer. This is what makes it deployable both inside the API server process (combined mode) and as a separate worker process.

### Running Modes

**Combined mode** (Phase 1): Started as an `asyncio.Task` inside the FastAPI lifespan when `TASK_WORKER_ENABLED=True`. The worker shares the process with the API server.

**Standalone worker** (Phase 2): Run as a separate process/pod. Entry point:
```python
# opi/worker_main.py (new, Phase 2)
async def main():
    pool = DatabasePool(...)
    await pool.initialize()
    task_service = AsyncTaskService(pool)
    worker = TaskWorker(task_service)
    await worker.run()  # blocks forever, processing tasks
```

Both modes use the exact same `TaskWorker` class. The only difference is who starts it.

### Worker Loops

```
Main Loop (every TASK_WORKER_POLL_INTERVAL seconds):
    claim_next_task() via SKIP LOCKED
    -> start heartbeat coroutine (every 30s)
    -> route to handler based on task_type
    -> complete_task() or fail_task()

Stale Recovery Loop (every 60s):
    recover_stale_tasks(threshold=120s)

Cleanup Loop (every hour):
    cleanup_old_tasks(retention=72h)
```

Processes 1 task at a time per worker instance. Scale by adding more worker pods/processes -- they coordinate via `SKIP LOCKED` on the shared database.

### Task Handlers

Each handler extracts the logic currently inline in `opi/api/router.py` and `opi/core/simple_background.py`:

| Handler Function | Current Source | Router Line |
|-----------------|----------------|-------------|
| `handle_create_project()` | `simple_background.process_project_background()` | entire file |
| `handle_upsert_deployment()` | `router.upsert_deployment()` | ~884 |
| `handle_update_image()` | `router.update_deployment_image()` | ~1265 |
| `handle_delete_deployment()` | `router.delete_project_deployment()` | ~1674 |
| `handle_clone_database()` | `router.clone_database_from_external()` | ~1745 |
| `handle_clone_bucket()` | `router.clone_bucket_from_external()` | ~1883 |
| `handle_refresh_deployment()` | `router.refresh_deployment()` | ~1494 |

Each handler receives:
- The deserialized `payload` dict (from the JSONB column)
- A `PersistentTaskProgressManager` instance (writes progress to DB)

Each handler calls the appropriate `ProjectManager` methods, exactly as the current inline code does, but through the persistent progress manager. Handlers have **no dependency on FastAPI or the Request object** -- they only use `ProjectManager`, connectors, and the progress manager.

#### How a handler reports failure

The task's `status` is the field a caller reads first, so it must say what happened. A
handler signals failure in one of three ways, and `reported_failure()` in
`opi/core/task_worker.py` reads all three:

1. `{"success": False, "error": ...}` -- the backup and restore handlers;
2. `{"status": "failed", "error": ..., "error_type": ...}` -- the component and service handlers;
3. calling `progress.fail_project(...)`, whatever it returns afterwards.

Any of the three ends the task on `status: failed` with the handler's own result kept
(so `error_type` and the parts that did succeed survive) and **without a retry** -- the
handler already decided this is permanent.

This used to read only the first form. A rejected service selection therefore reported
`status: completed` while its own `result` said `failed`, its `error_message` was set and
its subtask "Component toevoegen" had failed -- measured in
`docs/generale-repetitie-2026-08-12.md`, bevinding 5. Form 3 was a race on top of that:
`fail_project()` writes fire-and-forget, and the worker's `complete_task()` landed after it.

A task that succeeds is unchanged: `complete_task()`, `progress_percent: 100`,
`current_step: Done`.

### Instance Identification

```python
import os
import socket
instance_id = os.environ.get("HOSTNAME", socket.gethostname())
```

In Kubernetes this is the pod name (e.g., `zad-operations-manager-7b8c5d-x4j2k`). Used in `claimed_by` to track which worker is processing a task.

---

## 4. PersistentTaskProgressManager

**File**: `opi/core/persistent_task_progress.py` (new)

Drop-in replacement for the existing `TaskProgressManager` (`core/task_manager.py`) that writes to PostgreSQL instead of in-memory dicts.

### Why DB-backed progress is needed

In the target architecture, workers are **separate processes** from the frontend API server. They don't share memory. When a client asks "what's the progress of task X?", the frontend must read it from the database because the worker executing the task is a different process. This is the fundamental reason progress must be persisted.

The write frequency is modest: ~10-20 writes over a 5-30 minute task (at step boundaries like "creating namespace", "deploying Helm chart", etc.), not continuous streaming.

### Interface (matches existing TaskProgressManager)

```python
class PersistentTaskProgressManager:
    def __init__(self, task_id: str, project_name: str, task_service: AsyncTaskService):
        ...

    def add_task(self, name: str) -> str
    def add_subtask(self, parent_task_id: str, name: str) -> str
    def complete_task(self, task_id: str) -> None
    def fail_task(self, task_id: str, error: str) -> None
    def update_current_step(self, step: str) -> None
    def complete_project(self) -> None
    def fail_project(self, error: str) -> None
    def set_namespace(self, namespace: str) -> None
    def add_logs(self, logs: list[str]) -> None
    def add_events(self, events: list[dict[str, str]]) -> None
    def update_component_web_address(self, component_name: str, web_address: str) -> None
    def update_component_readiness(self, component_name: str, deployment_ready: str) -> None
    def start_monitoring(self) -> None
```

### Key differences from in-memory version

- Writes progress to PostgreSQL via `AsyncTaskService.update_progress()` using fire-and-forget `asyncio.create_task()` calls (non-blocking)
- Also populates the legacy `_projects` dict for web portal backward compatibility when running in combined mode
- Batches DB writes (coalesces rapid updates within a short window to avoid excessive DB round-trips)

### Combined mode optimization

When running in combined mode (worker inside the API server process), the `PersistentTaskProgressManager` also writes to the in-memory `_projects` dict. This means the web portal's existing progress page works without any changes -- it reads from memory for live updates, while the DB serves as the durable store. In standalone worker mode, only the DB path is used.

---

## 5. Task Status API

**File**: `opi/api/task_router.py` (new)

### Endpoints

```
GET  /api/tasks/{task_id}          -- Full status, progress, subtasks, logs
GET  /api/tasks                    -- List (filters: project_name, deployment_name, status)
POST /api/tasks/{task_id}/:cancel  -- Cancel pending task
POST /api/tasks                    -- Create task directly (used by federation, protected by MASTER_API_KEY)
```

### HTTP Status Codes on `GET /api/tasks/{task_id}`

| Task Status | HTTP Code | Meaning |
|-------------|-----------|---------|
| `pending`, `claimed`, `running` | `202 Accepted` | Task still in progress, keep polling |
| `completed` | `200 OK` | Task finished, `result` field populated |
| `failed` | `200 OK` | Task failed, `error_message` populated |
| `cancelled` | `200 OK` | Task was cancelled |
| (not found) | `404 Not Found` | Unknown task ID |

Clients use the HTTP status code to decide whether to keep polling: `202` means retry, `200` means done (check `status` for success vs failure).

### Who may poll a task

`GET /api/tasks/{task_id}` and `POST /api/tasks/{task_id}/:cancel` accept two credentials:

1. The **project's `X-API-Key`**, compared against the project the task belongs to. This is the
   normal path.
2. An **`Authorization: Bearer <SSO token>`** whose email equals the task's `created_by`.

The second exists for exactly one case: `POST /api/v2/projects` returns the new project's API key
with its `202`, but that key is only accepted once the project file exists - which is what the task
is still doing. Without a second way in, a client that just created a project has nothing to poll
and no signal to wait for. The task records who started it, so that person's token is that signal.

A valid token says who the caller is, not that the task is theirs: a task without `created_by`
cannot be opened with any token, and another user's token is refused. See
`opi/api/task_router.py::_validate_task_access` and `tests/test_task_router.py::TestGetTaskWithBearerToken`.

### Response Format

**In-progress** (returns `202`):
```json
{
  "task_id": "abc-123",
  "task_type": "upsert_deployment",
  "status": "running",
  "progress_percent": 45,
  "current_step": "Deploying Helm chart",
  "subtasks": [...],
  "result": null,
  "created_at": "2026-03-01T10:00:00Z",
  "started_at": "2026-03-01T10:00:02Z"
}
```

**Completed** (returns `200`):
```json
{
  "task_id": "abc-123",
  "task_type": "upsert_deployment",
  "status": "completed",
  "progress_percent": 100,
  "current_step": "Done",
  "result": {
    "deployment_name": "my-app-acc",
    "web_addresses": ["https://my-app.acc.example.nl"],
    "warnings": []
  },
  "created_at": "2026-03-01T10:00:00Z",
  "started_at": "2026-03-01T10:00:02Z",
  "completed_at": "2026-03-01T10:15:30Z"
}
```

### Typed Result Models

```python
class UpsertDeploymentResult(BaseModel):
    deployment_name: str
    web_addresses: list[str]
    warnings: list[str] = []

class UpdateImageResult(BaseModel):
    deployment_name: str
    image: str
    previous_image: str

class DeleteDeploymentResult(BaseModel):
    deployment_name: str
    resources_removed: list[str]

class CloneDatabaseResult(BaseModel):
    source: str
    target: str
    rows_copied: int | None = None

class CloneBucketResult(BaseModel):
    source: str
    target: str
    objects_copied: int | None = None

class RefreshDeploymentResult(BaseModel):
    deployment_name: str
    changes_detected: list[str]

class TaskResponse(BaseModel):
    task_id: UUID
    task_type: TaskType
    status: AsyncTaskStatus
    progress_percent: int
    current_step: str
    subtasks: list[dict] | None = None
    result: dict | None = None  # Typed per task_type
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
```

---

## 6. API Endpoint Conversion

Each blocking endpoint changes to return `202 Accepted` immediately. The `?sync=true` query param preserves old behavior during migration.

### Before (blocks 5-30 min)

```python
processing_result = await project_manager.process_project_from_git(...)
return JSONResponse(content=result, status_code=200)
```

### After (returns instantly)

```python
task = await task_service.create_task(
    task_type=TaskType.UPSERT_DEPLOYMENT,
    project_name=project_name,
    deployment_name=deployment_data.deploymentName,
    cluster=settings.CLUSTER_MANAGER,
    payload=deployment_data.model_dump(),
    created_by=get_request_user(request),
)
return JSONResponse(
    content={
        "status": "accepted",
        "task_id": str(task.id),
        "task_type": "upsert_deployment",
        "poll_url": f"/api/tasks/{task.id}",
    },
    status_code=202,
    headers={"Location": f"/api/tasks/{task.id}"},
)
```

The `Location` header (per RFC 7231) enables HTTP-aware clients and API gateways to follow it automatically.

### Endpoint to Task Type Mapping

| Endpoint | Task Type |
|----------|-----------|
| `POST /api/projects/{name}/:upsert-deployment` | `upsert_deployment` |
| `PUT /api/projects/{name}/deployments/{dep}/image` | `update_image` |
| `DELETE /api/projects/{name}/{dep}` | `delete_deployment` |
| `POST .../:clone-database-from-external` | `clone_database` |
| `POST .../:clone-bucket-from-external` | `clone_bucket` |
| `GET .../:refresh` | `refresh_deployment` |
| Web portal project creation | `create_project` |

---

## 7. Configuration

**File**: `opi/core/config.py` (add to existing Settings class)

```python
# Async task system settings
TASK_WORKER_ENABLED: bool = True          # Run worker loop inside this process (combined mode)
TASK_WORKER_POLL_INTERVAL: float = 2.0    # Seconds between claim attempts
TASK_WORKER_HEARTBEAT_INTERVAL: float = 30.0  # Seconds between heartbeat writes
TASK_WORKER_STALE_THRESHOLD: int = 120    # Seconds before a task is considered abandoned
TASK_WORKER_MAX_ATTEMPTS: int = 3         # Max retry attempts for failed tasks
TASK_WORKER_CLEANUP_RETENTION_HOURS: int = 72  # Hours to keep completed/failed tasks
```

### Deployment Configurations

| Mode | `TASK_WORKER_ENABLED` | Use case |
|------|----------------------|----------|
| **Combined** (default) | `True` | Single pod handles both API and task execution. Current setup. |
| **Frontend only** | `False` | API server only, no task execution. Pair with standalone workers. |
| **Standalone worker** | N/A (separate entry point) | Worker process only, no HTTP. Uses `opi/worker_main.py`. |

In combined mode, the frontend accepts requests AND processes tasks. In the scaled setup, set `TASK_WORKER_ENABLED=False` on the frontend and deploy separate worker pods that run `worker_main.py`.

---

## 8. Server Integration

### Combined Mode: `opi/server.py` (modify existing)

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # ... existing startup ...
    await run_startup_tasks(app)  # This now also creates the async_tasks table

    # Start task worker if enabled (combined mode)
    if settings.TASK_WORKER_ENABLED:
        task_worker = TaskWorker(task_service)
        worker_task = asyncio.create_task(task_worker.run())

    yield

    # Shutdown worker
    if settings.TASK_WORKER_ENABLED:
        task_worker.stop()
        await worker_task

    # ... existing shutdown ...
```

Register the task status API router:

```python
app.include_router(task_router, include_in_schema=True)
```

### Standalone Worker: `opi/worker_main.py` (new, Phase 2)

Separate entry point for running workers independently. Not needed for Phase 1 (combined mode) but designed from the start so the split is a deployment change, not a code change.

```python
"""Standalone task worker process.

Run with: python -m opi.worker_main

This process claims and executes tasks from the async_tasks table.
It does not serve HTTP. Deploy alongside the frontend OPI for scaling.
"""

async def main():
    # Initialize database pool (same config as API server)
    pool = DatabasePool(
        host=settings.DATABASE_HOST,
        user=settings.DATABASE_ADMIN_NAME,
        password=settings.DATABASE_ADMIN_PASSWORD,
        database=settings.DATABASE_NAME,
    )
    await pool.initialize()

    # Create service and worker
    task_service = AsyncTaskService(pool)
    worker = TaskWorker(task_service)

    try:
        await worker.run()  # Blocks forever, processing tasks
    finally:
        worker.stop()
        await pool.close()
```

The worker uses the same `DatabasePool`, `AsyncTaskService`, `TaskWorker`, and handler code as the combined mode. No code duplication.

---

## Implementation Order (Parallelizable Work Units)

The implementation is structured so that multiple agents can work in parallel. Dependencies are explicit.

### Wave 1: Foundation (no dependencies, fully parallel)

| Unit | File(s) | Description | Agent can start immediately |
|------|---------|-------------|-|
| **1A** | `opi/core/async_task_schema.py` | SQL schema string constant + table creation function | Yes |
| **1B** | `opi/core/async_task_service.py` | AsyncTaskService class with all DB methods, TaskType and AsyncTaskStatus enums | Yes |
| **1C** | `opi/core/config.py` | Add TASK_WORKER_* settings to existing Settings class | Yes |
| **1D** | `opi/api/task_router.py` + result models | Task status API endpoints + Pydantic response models | Yes |

**Note on 1B**: The service can be built against the schema defined in 1A, but since 1A is just a SQL string constant, an agent working on 1B can define the schema inline initially and extract it later.

### Wave 2: Worker and Progress (depends on Wave 1)

| Unit | File(s) | Depends on | Description |
|------|---------|------------|-------------|
| **2A** | `opi/core/persistent_task_progress.py` | 1B | PersistentTaskProgressManager (drop-in replacement for TaskProgressManager) |
| **2B** | `opi/core/task_worker.py` | 1B, 1C | Worker loop: claim, heartbeat, route to handler, stale recovery, cleanup |

**2A and 2B can be built in parallel** -- they both depend on 1B (AsyncTaskService) but not on each other. The worker calls handlers that use PersistentTaskProgressManager, but that interface is defined by the existing TaskProgressManager which is already known.

### Wave 3: Handler Extraction (depends on Wave 2A)

| Unit | File(s) | Depends on | Description |
|------|---------|------------|-------------|
| **3A** | `opi/core/task_handlers_project.py` (create_project, upsert_deployment) | 2A | Extract handler logic from simple_background.py and router.py |
| **3B** | `opi/core/task_handlers_deployment.py` (update_image, delete_deployment) | 2A | Extract handler logic from router.py |
| **3C** | `opi/core/task_handlers_operations.py` (clone_database, clone_bucket, refresh) | 2A | Extract handler logic from router.py |

**3A, 3B, 3C can all be built in parallel.** Each extracts different endpoint handlers into separate files. Each handler function receives `(payload: dict, progress: PersistentTaskProgressManager)` and calls the existing `ProjectManager` methods.

### Wave 4: Integration (depends on Waves 2-3)

| Unit | File(s) | Depends on | Description |
|------|---------|------------|-------------|
| **4A** | `opi/server.py` | 2B, 1D | Start worker in lifespan, register task_router |
| **4B** | `opi/core/startup.py` | 1A | Add async_tasks table creation to startup |
| **4C** | `opi/api/router.py` | 1B, 1D | Convert 6 endpoints to return 202 + create task. Add `?sync=true` fallback |

**4A and 4B can be parallel.** 4C can also be parallel but is the most sensitive change (modifies existing endpoints).

### Wave 5: Tests

| Unit | File(s) | Depends on | Description |
|------|---------|------------|-------------|
| **5A** | `tests/test_async_task_service.py` | 1B | Unit tests for service layer (mock DB) |
| **5B** | `tests/test_task_worker.py` | 2B | Unit tests for worker (mock service) |
| **5C** | `tests/test_task_router.py` | 1D | Unit tests for API endpoints |
| **5D** | `tests/integration/test_async_tasks.py` | All | Integration test: create task via API, worker picks up, status updates |

**5A, 5B, 5C can be parallel** and can even start alongside their respective waves.

### Parallelism Summary

```
Time -->

Wave 1:  [1A] [1B] [1C] [1D]        <-- 4 agents in parallel
Wave 2:       [2A] [2B]              <-- 2 agents in parallel
Wave 3:       [3A] [3B] [3C]        <-- 3 agents in parallel
Wave 4:       [4A] [4B] [4C]        <-- 3 agents in parallel
Wave 5:  [5A] [5B] [5C] [5D]        <-- 4 agents in parallel (5A-C can start with Wave 2+)

Maximum useful parallelism: 4 agents
Minimum sequential waves: 4 (Wave 1 -> 2 -> 3+4 -> 5D)
```

---

## Files Summary

### New Files

| File | Purpose | Wave |
|------|---------|------|
| `opi/core/async_task_schema.py` | SQL schema constant + table creation | 1A |
| `opi/core/async_task_service.py` | Task queue service layer + enums | 1B |
| `opi/core/persistent_task_progress.py` | PostgreSQL-backed progress manager | 2A |
| `opi/core/task_worker.py` | Worker loop + routing (no FastAPI dependency) | 2B |
| `opi/core/task_handlers_project.py` | Handlers for create_project, upsert_deployment | 3A |
| `opi/core/task_handlers_deployment.py` | Handlers for update_image, delete_deployment | 3B |
| `opi/core/task_handlers_operations.py` | Handlers for clone_database, clone_bucket, refresh | 3C |
| `opi/api/task_router.py` | Task status API + Pydantic models | 1D |
| `opi/worker_main.py` | Standalone worker entry point (Phase 2) | 4A |
| `tests/test_async_task_service.py` | Service unit tests | 5A |
| `tests/test_task_worker.py` | Worker unit tests | 5B |
| `tests/test_task_router.py` | API endpoint tests | 5C |
| `tests/integration/test_async_tasks.py` | End-to-end integration test | 5D |

### Modified Files

| File | Change | Wave |
|------|--------|------|
| `opi/core/config.py` | Add TASK_WORKER_* settings | 1C |
| `opi/core/startup.py` | Create async_tasks table at startup | 4B |
| `opi/server.py` | Start worker in lifespan (combined mode), register task_router | 4A |
| `opi/api/router.py` | Convert 6 endpoints to async task creation | 4C |

---

## Verification Checklist

### Phase 1: Combined Mode
1. **Single-instance async**: POST upsert-deployment returns 202; GET /api/tasks/{id} shows progress; task completes
2. **Multi-replica**: Run 2 combined replicas, submit 4 tasks, each processes 2 (SKIP LOCKED)
3. **Stale recovery**: Kill pod mid-task, other replica picks it up
4. **Web portal**: `/projects/progress/{task_id}` still works via in-memory bridge in combined mode
5. **Sync fallback**: `?sync=true` on API endpoints preserves blocking behavior
6. **Deduplication**: Submitting same deployment twice returns existing task ID
7. **Image payloads**: Endpoints with binary data store references, not inline data

### Phase 2: Frontend + Workers
8. **Frontend-only mode**: Set `TASK_WORKER_ENABLED=False`, verify API creates tasks but does not execute them
9. **Standalone worker**: Run `worker_main.py` separately, verify it claims and executes tasks from the DB
10. **Worker scaling**: Run 3 workers + 1 frontend, submit 6 tasks, workers distribute evenly
11. **Progress via DB**: Frontend returns progress for tasks running on separate worker processes
12. **Worker restart**: Kill a worker mid-task, another worker picks it up via stale recovery
