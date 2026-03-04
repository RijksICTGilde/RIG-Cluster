# Federation Routing (Cross-Cluster Task Delegation)

**Status**: Implemented
**Date**: 2026-03-01
**Depends on**: [Async Task System](./async-task-system.md)

## Problem

ZAD needs to run across multiple Kubernetes clusters (local, sandboxed-local, odcn-production). Users want a single frontend (master OPI) that can delegate work to per-cluster OPI instances. There is no shared database between clusters -- all communication must be HTTP.

### Infrastructure Constraints

- Each cluster has its own PostgreSQL (rig-db), not shared
- All inter-cluster communication is HTTP-only
- Chisel tunnels exist for temporary operations (DB cloning, MinIO access) but are not suitable as permanent infrastructure for a task queue due to reliability/latency concerns
- Each OPI instance already manages exactly one cluster via the `CLUSTER_MANAGER` setting

### Design Principle

The federation layer is a **thin HTTP routing proxy** on top of the async task system. The master OPI does not maintain its own copy of remote task state. It simply:

1. Forwards task creation requests to the correct slave OPI
2. Proxies task status requests to the slave that is executing the task

No proxy tasks in the database. No background polling loops. No cached status.

---

## Architecture

```
                    Users / CI
                       |
                       v
              +------------------+
              |  Master OPI      |  (odcn-production or central)
              |  - Full frontend |
              |  - Routing layer |
              |  - Local worker  |  (handles tasks for its own cluster)
              +--------+---------+
                       |
              HTTP (X-API-Key auth)
                       |
              +--------+--------+
              |                 |
     +--------v------+  +------v---------+
     | Slave OPI     |  | Slave OPI      |
     | cluster=local |  | cluster=sandbox|
     | - API + worker|  | - API + worker |
     | - Own rig-db  |  | - Own rig-db   |
     +---------------+  +----------------+
```

Each slave is a fully functional OPI with its own async task system. The master just routes requests to the right one.

### Standalone Mode

A standalone OPI (default, e.g., sandbox) has no federation config. Zero overhead, works exactly as before. It does not know federation exists.

---

## How Routing Works

### Task Creation (POST)

1. Client sends `POST /api/projects/foo/:upsert-deployment` to the master
2. Master reads the project YAML, finds the deployment's `cluster` field
3. If `cluster == settings.CLUSTER_MANAGER` (local): create task locally (standard async flow)
4. If `cluster` matches a configured peer: forward the request to that peer's OPI via HTTP
5. The slave creates the task in its own async_tasks table, returns `202 + task_id`
6. Master stores a lightweight routing entry: `{task_id, slave_url}` (in-memory dict or small DB table)
7. Master returns the task_id to the client

### Task Status (GET)

1. Client sends `GET /api/tasks/{task_id}` to the master
2. Master checks: is this a local task? Query local DB.
3. Not local? Look up the routing entry to find which slave has it, proxy `GET /api/tasks/{task_id}` to that slave
4. Return the slave's response directly to the client

This is a simple pass-through. No transformation, no caching, no state management on the master side.

### What the Master Forwards

The master forwards:
- The JSON payload (request body) -- sent once during task creation
- Its own `X-API-Key` to authenticate with the slave

The master does NOT forward:
- User authentication tokens (the master validates the user, then uses its own service key)
- Binary data (images are stored by reference before reaching the task system)

---

## Components

### 1. Federation Configuration

**File**: `opi/core/federation_config.py` (new)

```python
class PeerConfig(BaseModel):
    cluster: str        # Must match remote OPI's CLUSTER_MANAGER
    url: str            # e.g., "https://zad.rijksapp.nl"
    api_key: str        # Remote OPI's MASTER_API_KEY
    verify_tls: bool = True
    timeout: int = 30

class FederationRegistry:
    """Loaded from FEDERATION_PEERS JSON setting. Indexed by cluster name."""

    def get_peer(self, cluster: str) -> PeerConfig | None
    def is_local_cluster(self, cluster: str) -> bool
    def get_all_peers(self) -> list[PeerConfig]
```

**Settings** (add to `config.py`):

```python
FEDERATION_ROLE: str = "standalone"     # "standalone" | "master" | "slave"
FEDERATION_PEERS: str = ""              # JSON: [{"cluster":"local","url":"...","api_key":"..."}]
FEDERATION_REQUEST_TIMEOUT: int = 30
```

- **standalone** (default): no peers, everything local, no overhead
- **slave**: serves API, task creation authorized by MASTER_API_KEY
- **master**: full frontend, routes to slaves via HTTP

### 2. OPI Connector

**File**: `opi/connectors/opi.py` (new)

Uses the existing `HttpConnector` pattern. Sends `X-API-Key` header (matches the existing `validate_master_api_key` decorator on the slave).

```python
class OpiConnector:
    def __init__(self, peer: PeerConfig): ...

    async def health_check(self) -> bool
        """GET /readyz on the peer."""

    async def create_task(self, task_type: str, project_name: str,
                          deployment_name: str | None, payload: dict) -> dict
        """POST /api/tasks on the peer. Returns the 202 response."""

    async def get_task_status(self, task_id: str) -> dict
        """GET /api/tasks/{task_id} on the peer. Returns the response as-is."""
```

### 3. Federation Service (Routing Layer)

**File**: `opi/core/federation_service.py` (new)

```python
class FederationService:
    def __init__(self, registry: FederationRegistry, task_service: AsyncTaskService):
        self._registry = registry
        self._task_service = task_service
        self._route_table: dict[str, str] = {}  # task_id -> slave_url

    async def create_task(self, task_type, project_name, deployment_name,
                          target_cluster, payload, created_by) -> dict:
        """
        If target_cluster is local: create task in local DB (standard flow).
        If remote: forward to slave via OpiConnector, store routing entry.
        Returns {task_id, poll_url}.
        """

    async def get_task_status(self, task_id: str) -> dict | None:
        """
        Check local DB first. If not found, check route table and proxy to slave.
        Returns the task status response.
        """

    def _resolve_cluster(self, project_name: str, deployment_name: str) -> str:
        """Read project YAML -> find deployment -> return its cluster field."""
```

### 4. Route Table

The master needs to know which slave owns a given task_id. Two options:

**Option A: In-memory dict** (simplest)
- `{task_id: slave_url}` dict in FederationService
- Lost on master restart (client gets 404, can re-submit)
- Fine for a small number of concurrent tasks

**Option B: Small DB table** (durable)
```sql
CREATE TABLE IF NOT EXISTS task_routes (
    task_id UUID PRIMARY KEY,
    slave_url VARCHAR(512) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```
- Survives master restarts
- Auto-cleaned when tasks expire

Start with Option A. Move to Option B if master restarts become a problem.

### 5. Federation Health (Optional)

**File**: `opi/api/federation_router.py` (new)

Simple health endpoint for visibility:

```
GET /api/federation/health   -- Peer health status (master only)
GET /api/federation/peers    -- Peer list (cluster names, URLs, health -- no secrets)
```

Health checks can be periodic (every 30s ping `/readyz` on each peer) or on-demand (check when routing).

---

## Security

- **Master -> Slave auth**: `X-API-Key` header with the slave's `MASTER_API_KEY` (reuses existing decorator)
- **Communication is unidirectional**: master calls slaves, slaves never call master
- **TLS mandatory** for inter-cluster (`verify_tls=True` default)
- **Per-peer API keys**: compromising one slave's key does not affect others
- **User credentials are NOT forwarded**: master authenticates the user, then uses its own service key

---

## Failure Modes

| Scenario | Behavior |
|----------|----------|
| Slave unreachable during task creation | Return error to client (503), client can retry |
| Slave unreachable during status poll | Return error to client (502), client keeps polling |
| Slave pod dies mid-task | Slave's own stale recovery re-queues the task. Master status poll continues working once slave recovers |
| Master restarts (Option A: in-memory) | Route table lost. Client gets 404. Client re-submits or queries slave directly |
| Master restarts (Option B: DB table) | Route table survives. No disruption |

---

## Scaling Considerations

This design works well for a handful of clusters (2-10). At larger scale:
- The master becomes a bottleneck for all status polling
- Each status poll adds a network hop

If this becomes a problem, potential evolutions:
- Return the slave's URL directly to the client (skip master for status polling)
- Introduce a shared message bus (Redis, NATS) for status events
- Use webhooks: slave notifies master when task completes

These are future optimizations. The current HTTP proxy approach is simple, correct, and sufficient for the foreseeable scale.

---

## Implementation Order

Federation builds on top of the async task system. All of Part 1 (async-task-system.md) should be complete before starting federation.

| Step | File(s) | Parallel? |
|------|---------|-----------|
| F1 | `opi/core/federation_config.py` + config.py settings | Yes (with F2) |
| F2 | `opi/connectors/opi.py` | Yes (with F1) |
| F3 | `opi/core/federation_service.py` | After F1+F2 |
| F4 | `opi/api/federation_router.py` | After F3 |
| F5 | `opi/server.py` + `opi/api/router.py` | After F3 (integrate routing into endpoint conversion) |
| F6 | `tests/test_federation.py` | After F3 |

### Parallelism

```
F1 and F2 in parallel  ->  F3  ->  F4, F5, F6 in parallel
```

Maximum useful parallelism: 3 agents.

---

## Files Summary

### New Files

| File | Purpose |
|------|---------|
| `opi/core/federation_config.py` | PeerConfig model, FederationRegistry |
| `opi/connectors/opi.py` | OPI-to-OPI HTTP connector |
| `opi/core/federation_service.py` | Routing layer + route table |
| `opi/api/federation_router.py` | Health/peers API endpoints |
| `tests/test_federation.py` | Federation unit tests |

### Modified Files

| File | Change |
|------|--------|
| `opi/core/config.py` | Add FEDERATION_* settings |
| `opi/server.py` | Initialize federation service in lifespan |
| `opi/api/router.py` | Route through FederationService instead of directly creating local tasks |
