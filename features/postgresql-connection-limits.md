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

### 4. Per-role connection limit for project workloads (instelbaar, RC-201)

Every database role the Operations Manager creates for a deployment gets a `CONNECTION LIMIT`.
The value is a setting of the `postgresql-database` service
(`opi/services/catalog/postgresql_database/connection_limit.py`): 1 to 500, default 20.

```yaml
services:
  - name: postgresql-database
    config:
      connection-limit: 30        # every deployment of this project
deployments:
  - name: pr-250
    services:
      - reference: postgresql-database
        config:
          connection-limit: 80    # only this deployment
```

- Deployment over project over the default of 20. Nothing set keeps 20.
- The `_ro` role of a deployment gets the same value as the read-write role.
- The limit is per role, so several databases of one deployment (generations) share it.
- On every processing run OPI reads `rolconnlimit` and sends `ALTER ROLE ... CONNECTION LIMIT`
  only when it differs, so an existing role follows a changed value. The task shows the
  outcome per role ("van 20 naar 80", or "ongewijzigd"). Credentials are not touched by this.
- In the portal: the project value is in the database configuration (next to the extra
  schemas), the deployment value behind the "Connectielimiet" button on the deployment card.
  Both are a list of steps (10 to 500); a value outside the steps, set via the API, is shown
  as "37 (eigen waarde)". An empty deployment field follows the project.
- A value outside 1 to 500, or `true`, is refused on save with the message of the setting.

**Let op:** nothing but the bound limits this. There is no quota per project and no
permission check. `max_connections` is 250 in base and 500 on production
(`overlays/odcn`, applied only with a restart): one deployment asking 500 can claim 1000
connections (two roles), twice the production server.

**Uitrol:** the first processing run after the rollout brings every existing role to the
computed value, usually 20. That includes roles that are unlimited today (`-1`, e.g.
`amt_odc_prd_productie`) and roles with a value set by hand (e.g. the 60 on
`mpfm_w3h_pr_250`). Roles of `namespace-postgresql-database` go the same way and always get
20, because only the `postgresql-database` block is read; superusers are not subject to the
limit. Before rolling out:

1. List the roles that will change:
   ```sql
   SELECT rolname, rolconnlimit FROM pg_roles
   WHERE rolcanlogin AND NOT rolsuper AND rolconnlimit <> 20;
   ```
2. Put every value that must stay into the project file of that project (`connection-limit`
   on the project or the deployment).

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
| General (project workloads) | 237 | Available to all roles (487 on production) |
| **Per-role cap** | **20** | Default; configurable per project and deployment (1-500) |
| **Total** | **250** | `max_connections` in base; 500 in `overlays/odcn` |

## Limitations

- The per-role limit has no quota across projects; see the warning in section 4.
- These limits address connection count only, not connection pooling efficiency. See `features/pgbouncer-connection-pooling.md` for the longer-term solution.

## Related

- `features/pgbouncer-connection-pooling.md` - future PgBouncer integration for proper connection pooling
- `infrastructure/bootstrap/infrastructure/postgresql/database/base/cluster.yaml` - CNPG cluster configuration
- `operations-manager/python/opi/services/catalog/postgresql_database/connection_limit.py` - the setting and the merge
- `operations-manager/python/opi/connectors/postgres.py` - `create_user` and `set_connection_limit`
- `operations-manager/python/opi/core/database_pool.py` - pool configuration
- `operations-manager/python/opi/core/database_pools.py` - pool initialization
