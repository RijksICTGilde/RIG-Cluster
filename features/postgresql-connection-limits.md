# PostgreSQL Connection Limits and Pool Resilience

**Status**: Implemented
**Priority**: Infrastructure
**Created**: 2026-02-18

## Problem Statement

Project workloads exhausted all PostgreSQL connection slots (default 100), leaving no connections available for infrastructure services like Keycloak. This caused Keycloak to fail with `"Database operation failed"` errors when creating realms, effectively breaking authentication for the entire cluster.

A single project (AMT) held 75 connections across 3 deployments (29, 26, 20) due to a bug in its connection pool setup that created a new SQLAlchemy engine (and connection pool) on every request instead of reusing a singleton.

Additionally, when PostgreSQL restarted to apply the `max_connections` parameter change, it hung for 30 minutes waiting for those clients to disconnect gracefully.

## Changes Made

### 1. Increased max_connections (cluster.yaml)

`max_connections` raised from 100 to 200 to provide immediate relief.

### 2. Reserved connections for critical services (cluster.yaml)

```yaml
postgresql:
  parameters:
    max_connections: "200"
    reserved_connections: "10"
```

PostgreSQL 17 supports `reserved_connections`: 10 slots are set aside for roles with the `pg_use_reserved_connections` privilege. Even when all 187 general slots are consumed, Keycloak and Forgejo can still connect.

### 3. Per-role connection limits (cluster.yaml)

```yaml
managed:
  roles:
    - name: keycloak
      connectionLimit: 20
      inRoles:
        - pg_use_reserved_connections
    - name: forgejo
      connectionLimit: 10
      inRoles:
        - pg_use_reserved_connections
```

Infrastructure roles get explicit caps and access to the reserved pool.

### 4. Per-user connection limit for project workloads (postgres.py)

Every database user created by the Operations Manager now includes `CONNECTION LIMIT 20`:

```sql
CREATE USER <username> WITH PASSWORD '<password>' CONNECTION LIMIT 20
```

This prevents any single project deployment from monopolizing connection slots.

### 5. Shutdown timeouts (cluster.yaml)

```yaml
smartShutdownTimeout: 30
stopDelay: 60
```

- `smartShutdownTimeout: 30` - PostgreSQL waits 30 seconds for clients to disconnect, then escalates to fast shutdown (force-terminates sessions)
- `stopDelay: 60` - Kubernetes force-kills the pod after 60 seconds total

Previously these were 180s and 1800s respectively, causing a 30-minute hang when PostgreSQL needed to restart.

### 6. Pool resilience (database_pool.py, database_pools.py)

```python
min_size=0
max_inactive_connection_lifetime=300.0
```

- `min_size=0` - no pre-allocated connections. Connections are created on demand. After a PostgreSQL restart, there are zero stale connections to recover.
- `max_inactive_connection_lifetime=300` - idle connections are automatically closed after 5 minutes, preventing stale connections from accumulating.

Previously `min_size=2` meant the pool always held 2 open connections. When PostgreSQL restarted, these became stale and the pool could not recover without an application restart.

## Connection Budget

| Pool | Slots | Notes |
|------|-------|-------|
| Superuser reserved | 3 | PostgreSQL default |
| Infrastructure reserved | 10 | Keycloak, Forgejo via `pg_use_reserved_connections` |
| General (project workloads) | 187 | Available to all roles |
| **Per-role cap** | **20** | No single role can exceed this |
| **Total** | **200** | |

## Limitations

- The `CONNECTION LIMIT 20` on project users only applies to newly created roles. Existing roles (e.g., `amt_odc_prd_productie`) retain their previous unlimited setting until manually altered or recreated.
- The per-role limit is hardcoded at 20. It is not yet configurable per project.
- These limits address connection count only, not connection pooling efficiency. See `features/pgbouncer-connection-pooling.md` for the longer-term solution.

## Related

- `features/pgbouncer-connection-pooling.md` - future PgBouncer integration for proper connection pooling
- `infrastructure/bootstrap/infrastructure/postgresql/database/base/cluster.yaml` - CNPG cluster configuration
- `operations-manager/python/opi/connectors/postgres.py` - user creation with CONNECTION LIMIT
- `operations-manager/python/opi/core/database_pool.py` - pool configuration
- `operations-manager/python/opi/core/database_pools.py` - pool initialization
