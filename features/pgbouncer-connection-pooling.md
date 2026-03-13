# PgBouncer Connection Pooling

**Status**: Planned
**Priority**: Future Enhancement
**Created**: 2026-02-18

## Problem Statement

Project workloads hold excessive numbers of database connections (20-30 per deployment observed in production), quickly exhausting PostgreSQL's `max_connections` limit. When all slots are consumed, infrastructure services like Keycloak fail with "Database operation failed" errors because no connections remain for non-superuser roles.

Increasing `max_connections` (done as an immediate fix from 100 to 200) is a stopgap. Each PostgreSQL connection consumes memory (~5-10MB), and the real issue is that application connection pools are oversized relative to actual query concurrency. Connection pooling at the infrastructure level is the proper solution.

## Why PgBouncer

CloudNativePG has built-in support for PgBouncer via the `Pooler` custom resource. This means:

- No separate PgBouncer deployment to manage
- CNPG handles TLS, authentication, and lifecycle automatically
- The Pooler creates a Kubernetes Service that applications connect to instead of the direct `*-db-rw` service
- Health checks and monitoring are integrated

## Design

### Pool Mode Considerations

| Mode | Behavior | Compatibility |
|------|----------|---------------|
| `transaction` | Connection returned to pool after each transaction | Best efficiency, breaks prepared statements and advisory locks |
| `session` | Connection held for entire client session | Safe for all features, lower efficiency |

**Keycloak requires `session` mode** — it uses prepared statements and potentially advisory locks via Hibernate. Transaction pooling causes intermittent query failures.

**Project workloads can use `transaction` mode** — most web applications issue independent queries per request and don't rely on session-level features.

### Design Decision: Keep Infrastructure Direct, Pool Applications

Rather than two poolers, the simpler approach is:
- **Keycloak and Forgejo** continue connecting directly to `rig-db-rw` (they use few connections, need full compatibility)
- **Project workloads only** route through a transaction-mode pooler

This avoids complexity of managing two poolers with different modes.

### Key Distinction: Admin vs Application Connections

The Operations Manager itself performs DDL operations (CREATE DATABASE, CREATE USER, GRANT) which require direct PostgreSQL access. These admin connections must bypass PgBouncer. Only the application credentials stored in `DatabaseSecret` should point to the pooler.

This is already naturally separated — `database_manager.py` creates its own `PostgresConnector` with admin credentials, while `DatabaseSecret` is what gets injected into application pods.

### Tuning Parameters

Based on observed production patterns:

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| `default_pool_size` | `10` | 10 server connections per user/database pair; with ~20 databases, this gives 200 max server conns — matching PostgreSQL's `max_connections=200` |
| `max_client_conn` | `500` | Allow up to 500 client connections to PgBouncer; these are cheap (PgBouncer uses ~2KB per client) |
| `min_pool_size` | `2` | Keep 2 connections warm per pool to avoid latency on first query |
| `reserve_pool_size` | `5` | 5 extra connections when pool is exhausted |
| `reserve_pool_timeout` | `3` | Wait 3s before using reserve pool |
| `server_idle_timeout` | `600` | Close idle server connections after 10 minutes |
| `server_lifetime` | `3600` | Recycle server connections after 1 hour |

---

## Implementation

### Phase 1: Shared Database Pooler

#### 1.1 Pooler Resource

**File**: `infrastructure/bootstrap/infrastructure/postgresql/database/base/pooler.yaml` (new)

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: Pooler
metadata:
  name: rig-db-pooler
  namespace: rig-system
spec:
  cluster:
    name: rig-db
  instances: 1
  type: rw
  pgbouncer:
    poolMode: transaction
    parameters:
      default_pool_size: "10"
      max_client_conn: "500"
      min_pool_size: "2"
      reserve_pool_size: "5"
      reserve_pool_timeout: "3"
      server_idle_timeout: "600"
      server_lifetime: "3600"
    monitoring:
      enablePodMonitor: true
```

Add to the existing `infrastructure/bootstrap/infrastructure/postgresql/database/base/kustomization.yaml`:

```yaml
resources:
  - cluster.yaml
  - pooler.yaml   # <-- add this
```

#### 1.2 Update Cluster Config

**File**: `opi/core/cluster_config.py` (modify)

```python
CLUSTER_CONFIG = {
    "local": {
        # ... existing config ...
        "database_server": "rig-db-rw.rig-system.svc.cluster.local",
        "database_pooler": "rig-db-pooler-rw.rig-system.svc.cluster.local",  # <-- add
        # ...
    },
    "odcn-production": {
        # ... existing config ...
        "database_server": "rig-db-rw.rig-prd-operations.svc.cluster.local",
        "database_pooler": "rig-db-pooler-rw.rig-prd-operations.svc.cluster.local",  # <-- add
        # ...
    },
}


def get_database_server(cluster_name: str) -> str:
    """Returns direct database endpoint (for admin operations)."""
    return CLUSTER_CONFIG.get(cluster_name, {}).get(
        "database_server", "rig-db-rw.rig-system.svc.cluster.local"
    )


def get_database_pooler(cluster_name: str) -> str:
    """Returns PgBouncer pooler endpoint (for application connections)."""
    config = CLUSTER_CONFIG.get(cluster_name, {})
    # Fall back to direct server if pooler not configured
    return config.get("database_pooler", config.get(
        "database_server", "rig-db-rw.rig-system.svc.cluster.local"
    ))
```

#### 1.3 Update DatabaseSecret Generation

**File**: `opi/manager/database_manager.py` (modify)

When generating the `DatabaseSecret` that gets injected into application pods, use the pooler endpoint:

```python
# In the method that creates the database secret for application pods:
from opi.core.cluster_config import get_database_pooler

# Change from:
host = get_database_server(cluster_name)
# To:
host = get_database_pooler(cluster_name)

# Admin operations (CREATE DATABASE, etc.) continue using get_database_server()
```

#### 1.4 Verify Admin Operations Are Unaffected

The `database_manager.py` admin operations already use their own `PostgresConnector` with the admin (superuser) credentials and direct endpoint. Verify these methods continue using `get_database_server()`, not `get_database_pooler()`:

- `create_database()`
- `delete_database()`
- `create_user()`
- `grant_permissions()`

### Phase 2: Namespace-Specific Database Poolers

#### 2.1 Pooler Template

**File**: `manifests/postgresql-pooler.yaml.jinja` (new)

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: Pooler
metadata:
  name: {{ project_name }}-db-pooler
  namespace: {{ infrastructure_namespace }}
  annotations:
    argocd.argoproj.io/sync-wave: "2"
  labels:
    project: "{{ project_name }}"
    component: database-pooler
spec:
  cluster:
    name: {{ project_name }}-db
  instances: 1
  type: rw
  pgbouncer:
    poolMode: transaction
    parameters:
      default_pool_size: "{{ database_config.pooler.default_pool_size | default('10') }}"
      max_client_conn: "{{ database_config.pooler.max_client_conn | default('200') }}"
      min_pool_size: "{{ database_config.pooler.min_pool_size | default('2') }}"
      reserve_pool_size: "{{ database_config.pooler.reserve_pool_size | default('3') }}"
      reserve_pool_timeout: "3"
      server_idle_timeout: "600"
      server_lifetime: "3600"
    monitoring:
      enablePodMonitor: true
```

#### 2.2 Generate Pooler Alongside Cluster

**File**: `opi/manager/database_manager.py` (modify)

In the method that generates the `postgresql-cluster.yaml.jinja` manifest, add generation of the pooler manifest:

```python
# After generating the CNPG Cluster manifest:
pooler_manifest = render_template(
    "postgresql-pooler.yaml.jinja",
    project_name=project_name,
    infrastructure_namespace=infrastructure_namespace,
    database_config=database_config,
)
# Write pooler manifest alongside cluster manifest
```

#### 2.3 Update Namespace-Specific Endpoint

**File**: `opi/core/cluster_config.py` (modify)

```python
def get_database_cluster_service_endpoint(cluster_name: str, project_name: str) -> str:
    """Direct endpoint for admin operations on namespace-specific DB."""
    infra_ns = get_infrastructure_namespace(cluster_name, project_name)
    return f"{project_name}-db-rw.{infra_ns}.svc.cluster.local"


def get_database_cluster_pooler_endpoint(cluster_name: str, project_name: str) -> str:
    """Pooler endpoint for application connections to namespace-specific DB."""
    infra_ns = get_infrastructure_namespace(cluster_name, project_name)
    return f"{project_name}-db-pooler-rw.{infra_ns}.svc.cluster.local"
```

### Phase 3: Monitoring and Tuning

#### 3.1 PgBouncer Metrics

CNPG exposes PgBouncer metrics automatically when `enablePodMonitor: true` is set. Key metrics to monitor:

| Metric | Alert Threshold | Meaning |
|--------|----------------|---------|
| `pgbouncer_pools_server_active` | > `default_pool_size * 0.8` | Pool near exhaustion |
| `pgbouncer_pools_client_waiting` | > 0 for > 30s | Clients queuing for connections |
| `pgbouncer_pools_server_idle` | 0 for > 60s | Pool fully utilized |
| `pgbouncer_stats_total_query_time` | p99 > 5s | Slow queries affecting pool |

#### 3.2 PromQL Queries for Dashboards

```promql
# Active server connections per pool
pgbouncer_pools_server_active{namespace="rig-system"}

# Waiting clients (should be 0)
pgbouncer_pools_client_waiting{namespace="rig-system"}

# Connection utilization percentage
pgbouncer_pools_server_active / pgbouncer_pools_server_active + pgbouncer_pools_server_idle * 100

# Total client connections
pgbouncer_stats_total_client_connections{namespace="rig-system"}
```

#### 3.3 Post-Deployment Validation

After pooling is active and stable, consider:
1. Reduce PostgreSQL `max_connections` back to 100 (less memory per connection)
2. Tune `default_pool_size` based on observed `pgbouncer_pools_server_active` peaks
3. Reduce `max_client_conn` if actual client count is well below 500

---

## Migration Strategy

### Zero-Downtime Rollout

1. **Deploy pooler** — creates new `rig-db-pooler-rw` service alongside existing `rig-db-rw`
2. **Test with one project** — manually change one project's `DatabaseSecret` host to pooler endpoint, verify application works
3. **Roll out to all projects** — update `get_database_pooler()` to return pooler endpoint, redeploy affected projects via ArgoCD
4. **Monitor for 1 week** — watch PgBouncer metrics for connection issues
5. **Reduce max_connections** — once confident, lower PostgreSQL `max_connections`

### Rollback

If issues arise:
1. Change `get_database_pooler()` to return the direct `rig-db-rw` endpoint (same as `get_database_server()`)
2. ArgoCD will resync secrets with direct endpoint
3. Pods restart with direct connection — no PgBouncer in the path
4. Pooler resource can remain deployed (unused) or be removed

### Per-Project Testing Checklist

Before enabling pooler for a project, verify:

- [ ] Application starts and connects successfully
- [ ] Queries execute without errors (especially prepared statements)
- [ ] No `SET` commands or advisory locks in application code
- [ ] Connection pool configuration in application is reasonable (max 5-10 connections)
- [ ] ORM compatibility (Django, SQLAlchemy, etc. work with transaction pooling)

---

## Risks

| Risk | Mitigation |
|------|-----------|
| **Application uses prepared statements** | Test per-project; fall back to direct connection for incompatible apps |
| **Admin DDL through pooler** | Admin paths (`database_manager.py`) always use direct endpoint |
| **Secret rotation during migration** | ArgoCD syncs new secrets, triggers pod restart — brief downtime per deployment |
| **PgBouncer pod crashes** | CNPG restarts it automatically; clients reconnect via service |
| **Pool exhaustion** | Monitor `client_waiting` metric; increase `default_pool_size` or add pooler replicas |

---

## Configuration (config.py)

```python
# PgBouncer settings
PGBOUNCER_ENABLED: bool = True
PGBOUNCER_DEFAULT_POOL_SIZE: int = 10
PGBOUNCER_MAX_CLIENT_CONN: int = 500
```

---

## Files Summary

### New Files

| File | Purpose |
|------|---------|
| `infrastructure/bootstrap/infrastructure/postgresql/database/base/pooler.yaml` | Shared database pooler resource |
| `manifests/postgresql-pooler.yaml.jinja` | Namespace-specific database pooler template |

### Modified Files

| File | Change |
|------|--------|
| `infrastructure/bootstrap/infrastructure/postgresql/database/base/kustomization.yaml` | Add `pooler.yaml` to resources |
| `opi/core/cluster_config.py` | Add `database_pooler` entries + `get_database_pooler()` + `get_database_cluster_pooler_endpoint()` |
| `opi/manager/database_manager.py` | Use pooler endpoint for `DatabaseSecret`; generate pooler manifest for namespace-specific DBs |
| `opi/core/config.py` | Add `PGBOUNCER_*` settings |

---

## Dependencies

- CloudNativePG must support the `Pooler` resource (available since CNPG 1.15+)
- The current CNPG operator version in the cluster must be verified: `kubectl get deployment -n cnpg-system -o jsonpath='{.items[0].spec.template.spec.containers[0].image}'`

## Verification

1. **Pooler deploys**: `kubectl get pooler -n rig-system` shows `rig-db-pooler` with `READY=True`
2. **Service exists**: `kubectl get svc rig-db-pooler-rw -n rig-system` returns the pooler service
3. **Application connects**: Deploy a test app pointing to pooler, verify queries succeed
4. **Prepared statements break**: Confirm Keycloak fails with transaction mode (validates why it stays on direct)
5. **Connection reduction**: Before/after comparison of `pg_stat_activity` connection count
6. **Metrics visible**: `curl prometheus:9090/api/v1/query?query=pgbouncer_pools_server_active` returns data
7. **Failover**: Delete pooler pod, verify CNPG recreates it and clients reconnect

## Related

- `infrastructure/bootstrap/infrastructure/postgresql/database/base/cluster.yaml` — shared database cluster
- `manifests/postgresql-cluster.yaml.jinja` — namespace-specific database template
- `opi/core/cluster_config.py` — database endpoint resolution
- `opi/manager/database_manager.py` — admin database operations
- `features/postgresql-connection-limits.md` — related connection limit changes
