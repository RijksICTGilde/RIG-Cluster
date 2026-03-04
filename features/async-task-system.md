# Async Task System + Federation ("Hive") Architecture for ZAD

**Status**: Planned (not yet implemented)
**Date**: 2026-02-28
**Priority**: High

## Context

Two connected problems need solving:

1. **Sync API timeouts**: Most deployment API endpoints block for 5-30+ minutes, causing HTTP 504s. The web portal has a working async pattern (BackgroundTasks + in-memory TaskProgressManager), but it's single-instance, in-memory, and not used by the REST API.

2. **Multi-cluster coordination**: ZAD needs to run across multiple Kubernetes clusters (local, sandboxed-local, odcn-production) with multiple instances. There's no shared database between clusters -- only HTTP. Users want a single frontend (master OPI) that delegates work to per-cluster slave OPIs.

**Key insight**: The async task API (`POST` -> `202` + task_id, `GET /api/tasks/{id}` to poll) becomes the inter-cluster communication protocol. The master delegates to slaves by calling their standard task API via HTTP.

### Problem Summary

| Endpoint | Method | Typical Duration |
|----------|--------|-----------------|
| `/api/projects/{name}/:upsert-deployment` | POST | 5-30 min |
| `/api/projects/{name}/deployments/{dep}/image` | PUT | 3-15 min |
| `/api/projects/{name}/{dep}` | DELETE | 5-15 min |
| `/api/projects/{name}/deployments/{dep}/:clone-database-from-external` | POST | 10-60 min |
| `/api/projects/{name}/deployments/{dep}/:clone-bucket-from-external` | POST | 10-30 min |
| `/api/projects/{name}/deployments/{dep}/:refresh` | GET | 5-20 min |

### Existing Patterns We Build On

- `CLUSTER_MANAGER` setting -- each OPI already manages exactly one cluster (`config.py:142`)
- `MASTER_API_KEY` + `validate_master_api_key` decorator -- admin auth already exists (`endpoint_util.py:82`)
- `HttpConnector` -- HTTP client with Bearer/Basic/Keycloak auth (`connectors/http.py`)
- `DatabasePool` -- asyncpg connection pool for PostgreSQL (`core/database_pool.py`)
- Project YAML `cluster` field on deployments -- routing info already in data model (`projects/simple-example.yaml`)
- Deployment filtering: `d.get("cluster") == settings.CLUSTER_MANAGER` throughout `project_manager.py`

### Current Limitations

- **In-memory only** -- tasks lost on pod restart
- **Single-process only** -- can't scale to multiple ZAD instances
- **No retry mechanism** -- failed tasks are not retried
- **No persistence** -- no audit trail or recovery
- **API endpoints block** -- only the web portal uses the background task pattern

---

# Part 1: Async Task System (within a single cluster)

## 1.1 Database Schema

**File**: `opi/core/async_task_service.py` (new)

Table created at startup via `CREATE TABLE IF NOT EXISTS` (same pattern as existing `SUBDOMAIN_REGISTRY_TABLE_SQL`). Uses the local rig-db PostgreSQL.

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
    -- Federation proxy columns
    is_proxy BOOLEAN NOT NULL DEFAULT FALSE,
    remote_task_id UUID,
    remote_opi_url VARCHAR(512),
    cached_status JSONB,
    cached_at TIMESTAMPTZ
);

-- Index for the worker claim query (SELECT ... FOR UPDATE SKIP LOCKED)
CREATE INDEX IF NOT EXISTS idx_async_tasks_pending
    ON async_tasks(status, created_at)
    WHERE status = 'pending';

-- Index for stale task recovery (heartbeat check)
CREATE INDEX IF NOT EXISTS idx_async_tasks_heartbeat
    ON async_tasks(status, heartbeat_at)
    WHERE status IN ('claimed', 'running');

-- Index for querying tasks by project
CREATE INDEX IF NOT EXISTS idx_async_tasks_project
    ON async_tasks(project_name, created_at DESC);

-- Index for querying tasks by project + deployment
CREATE INDEX IF NOT EXISTS idx_async_tasks_deployment
    ON async_tasks(project_name, deployment_name, created_at DESC);

-- Index for proxy task refresh loop
CREATE INDEX IF NOT EXISTS idx_async_tasks_proxy
    ON async_tasks(is_proxy, status)
    WHERE is_proxy = TRUE;

-- Index for cleanup of old completed tasks
CREATE INDEX IF NOT EXISTS idx_async_tasks_completed
    ON async_tasks(status, completed_at)
    WHERE status IN ('completed', 'failed', 'cancelled');
```

Key design decisions:
- **JSONB for payload**: Each task type has different request parameters. Storing as JSONB avoids schema proliferation.
- **subtasks as JSONB array**: Mirrors the existing `TaskProgressManager.tasks` dict but stored persistently.
- **heartbeat_at**: Essential for stale task recovery when a pod dies mid-task.
- **No separate subtasks table**: At most ~15 subtasks per operation; JSONB array is sufficient.
- **Federation proxy columns**: `is_proxy`, `remote_task_id`, `remote_opi_url`, `cached_status`, `cached_at` support the master-slave delegation pattern without a separate table.

## 1.2 AsyncTaskService

**File**: `opi/core/async_task_service.py` (new)

Key methods using the existing `DatabasePool` (from `opi/core/database_pool.py`):

| Method | SQL Pattern |
|--------|-------------|
| `create_task()` | INSERT + dedup check (same project+deployment+type already pending/running -> return existing) |
| `claim_next_task()` | `SELECT ... FOR UPDATE SKIP LOCKED` where `status='pending' AND is_proxy=FALSE` |
| `start_task()` | UPDATE status=running, started_at |
| `update_progress()` | UPDATE current_step, progress_percent, subtasks, heartbeat_at |
| `send_heartbeat()` | UPDATE heartbeat_at only |
| `complete_task()` | UPDATE status=completed, result, completed_at |
| `fail_task()` | Re-queue if attempt_count < max_attempts, else status=failed |
| `get_task()` / `list_tasks()` | SELECT with filters |
| `recover_stale_tasks()` | Reset tasks with heartbeat_at > 120s old |
| `cleanup_old_tasks()` | DELETE completed tasks older than 72h |
| `create_proxy_task()` | INSERT with is_proxy=TRUE, remote_task_id, remote_opi_url |
| `update_proxy_status()` | UPDATE cached_status, cached_at for proxy tasks |

Enums: `TaskType` (upsert_deployment, update_image, delete_deployment, clone_database, clone_bucket, refresh_deployment, create_project), `AsyncTaskStatus` (pending, claimed, running, completed, failed, cancelled).

### Multi-Instance Coordination

**Task claiming** uses `SELECT ... FOR UPDATE SKIP LOCKED`:
```sql
BEGIN;
SELECT id, task_type, payload, ...
FROM async_tasks
WHERE status = 'pending' AND is_proxy = FALSE
ORDER BY created_at ASC
LIMIT 1
FOR UPDATE SKIP LOCKED;

UPDATE async_tasks
SET status = 'claimed', claimed_by = $instance_id, claimed_at = NOW()
WHERE id = $task_id;
COMMIT;
```

`SKIP LOCKED` ensures no deadlocks or contention between instances.

**Stale task recovery** (every 60s):
```sql
-- Re-queue tasks from dead workers
UPDATE async_tasks
SET status = 'pending', claimed_by = NULL, claimed_at = NULL,
    heartbeat_at = NULL, attempt_count = attempt_count + 1
WHERE status IN ('claimed', 'running')
AND heartbeat_at < NOW() - INTERVAL '120 seconds'
AND attempt_count < max_attempts;

-- Fail tasks that exceeded max attempts
UPDATE async_tasks
SET status = 'failed', error_message = 'Worker died, max retries exceeded', completed_at = NOW()
WHERE status IN ('claimed', 'running')
AND heartbeat_at < NOW() - INTERVAL '120 seconds'
AND attempt_count >= max_attempts;
```

## 1.3 Task Worker

**File**: `opi/core/task_worker.py` (new)

Runs as `asyncio.Task` in each OPI instance (started in FastAPI lifespan). 1 task at a time per instance -- scale by adding replicas.

```
Main Loop (every 2s):
  claim_next_task() via SKIP LOCKED (WHERE is_proxy=FALSE)
  -> start heartbeat coroutine (every 30s)
  -> route to handler based on task_type
  -> complete_task() or fail_task()

Stale Recovery Loop (every 60s):
  recover_stale_tasks(threshold=120s)

Cleanup Loop (every hour):
  cleanup_old_tasks(retention=72h)
```

### Task Handlers

Each handler extracts the logic currently inline in `opi/api/router.py`:

| Handler | Source | Router Line |
|---------|--------|-------------|
| `handle_upsert_deployment()` | `upsert_deployment()` | ~876-1008 |
| `handle_update_image()` | `update_deployment_image()` | ~1263-1400 |
| `handle_delete_deployment()` | `delete_project_deployment()` | ~1672-1741 |
| `handle_clone_database()` | `clone_database_from_external()` | ~1743-1870 |
| `handle_clone_bucket()` | `clone_bucket_from_external()` | ~1881-2017 |
| `handle_create_project()` | `simple_background.process_project_background()` | entire file |

Each handler receives the deserialized `payload` and a `PersistentTaskProgressManager`, then calls the appropriate `ProjectManager` methods.

### Instance Identification

Use `os.environ.get("HOSTNAME", socket.gethostname())` -- in Kubernetes this is the pod name (e.g., `zad-operations-manager-7b8c5d-x4j2k`).

## 1.4 PersistentTaskProgressManager

**File**: `opi/core/persistent_task_progress.py` (new)

Drop-in replacement for `TaskProgressManager` that:
- Same interface (add_task, complete_task, fail_task, add_subtask, update_current_step, etc.)
- Writes to PostgreSQL via AsyncTaskService
- Also populates legacy `_projects` dict for web portal backward compat
- Uses `asyncio.create_task()` for fire-and-forget DB updates

## 1.5 Task Status API

**File**: `opi/api/task_router.py` (new)

```
GET  /api/tasks/{task_id}          -- Full status, progress, subtasks, logs
GET  /api/tasks                    -- List (filters: project_name, deployment_name, status)
POST /api/tasks/{task_id}/:cancel  -- Cancel pending task
POST /api/tasks                    -- Create task (used by federation master -> slave)
```

### Response Design: Typed Result Envelope

Each task type produces a different result shape (e.g. upsert returns deployment info, clone returns row counts). Rather than creating separate status endpoints per task type or prefixing routes with `/tasks`, we use a single **generic envelope with a typed `result` field**, discriminated by `task_type`.

**Why a single `/api/tasks/{id}` endpoint**:
- Clients already know what result type to expect because they initiated the request
- The `task_type` field in the response makes it explicit for generic consumers
- Avoids duplicating every route (e.g. `POST /api/tasks/projects/{name}/:upsert-deployment`)
- Avoids mixed response schemas on resource endpoints (e.g. `GET /deployments/{dep}` should always return a deployment, not sometimes a task status)
- Cleanly handles multiple concurrent tasks for the same deployment

**HTTP status codes on `GET /api/tasks/{task_id}`**:

| Task Status | HTTP Code | Meaning |
|-------------|-----------|---------|
| `pending`, `claimed`, `running` | `202 Accepted` | Task still in progress, keep polling |
| `completed` | `200 OK` | Task finished, `result` field populated |
| `failed` | `200 OK` | Task failed, `error_message` populated (status field distinguishes from success) |
| `cancelled` | `200 OK` | Task was cancelled |
| (not found) | `404 Not Found` | Unknown task ID |

This lets clients use the HTTP status code alone to decide whether to keep polling: `202` means retry, `200` means done (check `status` field for success vs failure).

**Task status response format** (in-progress, returns `202`):

```json
{
  "task_id": "abc-123",
  "task_type": "upsert_deployment",
  "status": "running",
  "progress_percent": 45,
  "current_step": "Deploying Helm chart",
  "result": null
}
```

**Task status response format** (completed, returns `200`):

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
  }
}
```

Clients use `progress_percent`, `current_step`, and `subtasks` for progress tracking while polling.

**Typed result models** (for OpenAPI documentation and client validation):

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

# Discriminated union for OpenAPI docs
TaskResult = (
    UpsertDeploymentResult
    | UpdateImageResult
    | DeleteDeploymentResult
    | CloneDatabaseResult
    | CloneBucketResult
    | RefreshDeploymentResult
)

class TaskResponse(BaseModel):
    task_id: UUID
    task_type: TaskType
    status: AsyncTaskStatus
    progress_percent: int
    current_step: str
    subtasks: list[dict] | None = None
    result: TaskResult | None = None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
```

## 1.6 API Endpoint Conversion

Each blocking endpoint changes to return `202 Accepted` immediately. `?sync=true` query param preserves old behavior during migration. The 202 response includes a `Location` header (per RFC 7231) alongside the JSON body, enabling HTTP-aware clients and API gateways to follow it automatically.

```python
# BEFORE (blocks 5-30 min):
processing_result = await project_manager.process_project_from_git(...)
return JSONResponse(content=result, status_code=200)

# AFTER (returns instantly):
task = await task_service.create_task(
    task_type=TaskType.UPSERT_DEPLOYMENT,
    project_name=project_name,
    deployment_name=deployment_data.deploymentName,
    cluster=settings.CLUSTER_MANAGER,
    payload=deployment_data.model_dump(),
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

**Deduplication**: If a task for the same project/deployment/type is already pending or running, the existing task ID is returned instead of creating a duplicate.

| Endpoint | Task Type |
|----------|-----------|
| `POST /api/projects/{name}/:upsert-deployment` | upsert_deployment |
| `PUT /api/projects/{name}/deployments/{dep}/image` | update_image |
| `DELETE /api/projects/{name}/{dep}` | delete_deployment |
| `POST .../:clone-database-from-external` | clone_database |
| `POST .../:clone-bucket-from-external` | clone_bucket |
| `GET .../:refresh` | refresh_deployment |

## 1.7 Configuration

Add to `opi/core/config.py` Settings:

```python
TASK_WORKER_ENABLED: bool = True
TASK_WORKER_POLL_INTERVAL: float = 2.0
TASK_WORKER_HEARTBEAT_INTERVAL: float = 30.0
TASK_WORKER_STALE_THRESHOLD: int = 120
TASK_WORKER_MAX_ATTEMPTS: int = 3
TASK_WORKER_CLEANUP_RETENTION_HOURS: int = 72
```

---

# Part 2: Federation / Hive (across clusters)

## 2.1 Architecture

```
                    Users / CI
                       |
                       v
              +------------------+
              |  Master OPI      |  (odcn-production or central)
              |  - Full frontend |
              |  - Federation    |
              |  - Local worker  |
              +--------+---------+
                       |  HTTP (X-API-Key auth)
              +--------+--------+
              |                 |
     +--------v------+  +------v---------+
     | Slave OPI     |  | Slave OPI      |
     | cluster=local |  | cluster=sandbox|  (standalone, no peers)
     | - API only    |  | - Full standalone
     | - Local worker|  | - Local worker |
     +---------------+  +----------------+
```

Each OPI has its own PostgreSQL. No shared DB. Communication is HTTP-only via the task API.

## 2.2 Cluster Registry (Configuration)

**File**: `opi/core/federation_config.py` (new)

```python
class PeerConfig(BaseModel):
    cluster: str        # Must match remote OPI's CLUSTER_MANAGER
    url: str            # e.g. "https://zad.rijksapp.nl"
    api_key: str        # Remote OPI's MASTER_API_KEY
    verify_tls: bool = True
    timeout: int = 30

class FederationRegistry:
    """Loaded from FEDERATION_PEERS JSON setting. Indexed by cluster name."""
    def get_peer(self, cluster: str) -> PeerConfig | None
    def is_local_cluster(self, cluster: str) -> bool
    def get_all_peers(self) -> list[PeerConfig]
```

New settings in `config.py`:

```python
FEDERATION_ROLE: str = "standalone"     # "standalone" | "master" | "slave"
FEDERATION_PEERS: str = ""              # JSON: [{"cluster":"local","url":"...","api_key":"..."}]
FEDERATION_HEALTH_INTERVAL: int = 30
FEDERATION_STATUS_CACHE_TTL: int = 5
FEDERATION_REQUEST_TIMEOUT: int = 30
```

**Standalone** (default): zero peers, everything local, no overhead. Sandbox uses this.
**Slave**: serves API, task creation authorized by MASTER_API_KEY, no frontend.
**Master**: full frontend, delegates to slaves via HTTP, proxy tasks for remote work.

## 2.3 OPI Connector

**File**: `opi/connectors/opi.py` (new)

Uses existing `HttpConnector` pattern. Sends `X-API-Key` header (matches existing `validate_master_api_key` decorator on slave).

```python
class OpiConnector:
    def __init__(self, peer: PeerConfig): ...
    async def health_check(self) -> dict           # GET /readyz
    async def create_task(self, ...) -> str         # POST /api/tasks -> remote task_id
    async def get_task_status(self, id) -> dict     # GET /api/tasks/{id}
    async def list_project_tasks(self, name) -> list  # GET /api/tasks?project_name=...
```

## 2.4 Federation Service (Task Routing)

**File**: `opi/core/federation_service.py` (new)

```python
class FederationService:
    async def delegate_task(self, task_type, project_name, deployment_name,
                            target_cluster, payload, created_by) -> str:
        """
        If target_cluster is local: create task locally (existing flow).
        If remote: call slave OPI's POST /api/tasks, create proxy task locally.
        Returns local task_id.
        """

    async def get_task_status(self, local_task_id) -> dict:
        """
        Local task: read from DB.
        Proxy task: fetch from slave (with TTL cache), return merged status.
        Slave unreachable: return cached_status with remote_unreachable flag.
        """

    async def route_deployment_task(self, project_name, deployment_name,
                                     task_type, payload) -> str:
        """Read project YAML -> get deployment's cluster field -> delegate_task()"""
```

**How routing works**: The master reads the project YAML (via existing `ProjectManager.get_deployments(cluster_filter=False)`), finds the deployment, reads its `cluster` field, and delegates to that cluster's OPI.

**Proxy task lifecycle**:
1. Master creates proxy task locally: `is_proxy=TRUE`, `status='running'`, `remote_task_id`, `remote_opi_url`
2. Background refresher polls slave for status, updates `cached_status`
3. When remote task reaches terminal state, proxy status updated to match
4. Local worker never touches proxy tasks (`WHERE is_proxy=FALSE` in claim query)

## 2.5 Health Monitor + Proxy Refresher

**File**: `opi/core/federation_health.py` (new)

Two background loops (master only):

**Health monitor** (every 30s): polls `GET /readyz` on each peer, tracks `PeerHealth` (healthy, last_check, response_time).

**Proxy status refresher** (every 5s): finds all non-terminal proxy tasks, groups by cluster, fetches status from slaves, updates `cached_status`. Terminal proxy tasks are no longer polled.

## 2.6 Federation API

**File**: `opi/api/federation_router.py` (new)

```
GET /api/federation/health   -- Peer health status (master only)
GET /api/federation/peers    -- Peer list (cluster names, URLs, health -- no secrets)
```

## 2.7 Security

- **Master -> Slave auth**: `X-API-Key` header with the slave's `MASTER_API_KEY` (reuses existing decorator)
- **Communication is unidirectional**: master polls slaves, slaves never call master
- **TLS mandatory** for inter-cluster (enforced by `verify_tls=True` default)
- **Per-peer API keys**: compromising one slave's key doesn't affect others
- **Future enhancement**: Keycloak client credentials flow (HttpConnector already supports it)

## 2.8 Failure Modes

| Scenario | Behavior |
|----------|----------|
| Slave unreachable during task creation | Create proxy task with status=failed, error explains unreachable |
| Slave unreachable during status poll | Return cached_status with `remote_unreachable: true` flag |
| Slave pod dies mid-task | Slave's own stale recovery re-queues the task |
| Entire slave cluster down | Proxy stays "running" with stale cache; configurable timeout marks as timed_out |
| Master restarts | Proxy tasks persist in PostgreSQL; refresher resumes polling |

---

# Implementation Order

| Step | Files | Depends On |
|------|-------|------------|
| 1 | `opi/core/async_task_service.py` -- Schema + service layer | -- |
| 2 | `opi/core/persistent_task_progress.py` -- Progress manager bridge | Step 1 |
| 3 | `opi/core/task_worker.py` -- Worker loop + task handlers | Steps 1, 2 |
| 4 | `opi/api/task_router.py` -- Task status API endpoints | Step 1 |
| 5 | `opi/core/config.py` -- Add TASK_WORKER_* + FEDERATION_* settings | -- |
| 6 | `opi/server.py` -- Start worker in lifespan, register router | Steps 1-5 |
| 7 | `opi/api/router.py` -- Convert 6 endpoints to async | Steps 1, 4 |
| 8 | `opi/core/federation_config.py` -- PeerConfig, Registry | Step 5 |
| 9 | `opi/connectors/opi.py` -- OPI-to-OPI connector | Step 8 |
| 10 | `opi/core/federation_service.py` -- Routing + proxy | Steps 1, 8, 9 |
| 11 | `opi/core/federation_health.py` -- Health + proxy refresher | Steps 9, 10 |
| 12 | `opi/api/federation_router.py` -- Federation health API | Step 11 |
| 13 | `opi/server.py` -- Start federation in lifespan | Steps 10-12 |
| 14 | Web templates -- Multi-cluster display | Step 12 |

Steps 1-7 can be delivered and tested independently (single-cluster async). Steps 8-14 add federation on top.

---

# Files Summary

## New Files

| File | Purpose |
|------|---------|
| `opi/core/async_task_service.py` | Task queue: schema, service, enums |
| `opi/core/task_worker.py` | Worker loop + task handlers |
| `opi/core/persistent_task_progress.py` | PostgreSQL-backed progress manager |
| `opi/api/task_router.py` | Task status API |
| `opi/core/federation_config.py` | Peer config model + registry |
| `opi/connectors/opi.py` | OPI-to-OPI HTTP connector |
| `opi/core/federation_service.py` | Task routing + proxy management |
| `opi/core/federation_health.py` | Peer health + proxy status refresher |
| `opi/api/federation_router.py` | Federation health/peers API |
| `tests/test_async_task_service.py` | Task service tests |
| `tests/test_federation.py` | Federation tests |

## Modified Files

| File | Change |
|------|--------|
| `opi/core/config.py` | TASK_WORKER_* + FEDERATION_* settings |
| `opi/server.py` | Lifespan: start worker + federation, register routers |
| `opi/api/router.py` | Convert 6 endpoints to async task creation |
| `opi/core/startup.py` | Ensure async_tasks table at startup |
| `bootstrap/.../deployment.yaml` | Allow multiple replicas |

---

# Verification

1. **Single-cluster async**: POST upsert-deployment returns 202; GET /api/tasks/{id} shows progress; task completes
2. **Multi-instance**: Run 2 replicas, submit 4 tasks, each processes 2 (SKIP LOCKED)
3. **Stale recovery**: Kill worker mid-task, another instance picks it up
4. **Web portal**: `/projects/progress/{task_id}` still works (in-memory bridge)
5. **Federation**: Master creates proxy task, polls slave, status propagates
6. **Slave unreachable**: Proxy shows cached status with warning flag
7. **Standalone mode**: No federation config = zero overhead, works as before
8. **Sync fallback**: `?sync=true` on API endpoints preserves blocking behavior
