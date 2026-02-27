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

### Pooler Resource

CloudNativePG creates a PgBouncer deployment and service when a `Pooler` resource is applied:

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
      default_pool_size: "25"
      max_client_conn: "200"
```

This creates a service `rig-db-pooler-rw` that applications connect to instead of `rig-db-rw`.

### Pool Mode Considerations

| Mode | Behavior | Compatibility |
|------|----------|---------------|
| `transaction` | Connection returned to pool after each transaction | Best efficiency, breaks prepared statements and advisory locks |
| `session` | Connection held for entire client session | Safe for all features, lower efficiency |

**Keycloak requires `session` mode** — it uses prepared statements and potentially advisory locks via Hibernate. Transaction pooling causes intermittent query failures.

**Project workloads can use `transaction` mode** — most web applications issue independent queries per request and don't rely on session-level features.

This means two poolers are needed:

1. **Infrastructure pooler** (`session` mode) — for Keycloak, Forgejo, and other infrastructure services that need full PostgreSQL feature compatibility
2. **Application pooler** (`transaction` mode) — for project deployment workloads where efficiency matters most

Alternatively, Keycloak and Forgejo could continue connecting directly to `rig-db-rw` (they are few connections) while only project workloads route through a transaction-mode pooler.

### Namespace-Specific Database Clusters

Projects using `namespace-postgresql-database` have their own CNPG Cluster. These would each need their own Pooler resource. The `postgresql-cluster.yaml.jinja` template would need a companion `pooler.yaml.jinja` template.

## Components That Need Changes

### Shared Database (rig-db)

| Component | Current Endpoint | New Endpoint | Notes |
|-----------|-----------------|--------------|-------|
| Keycloak | `rig-db-rw:5432` | Keep direct or use session-mode pooler | `infrastructure/bootstrap/infrastructure/keycloak/controller/base/deployment.yaml` |
| Forgejo | `rig-db-rw.rig-system.svc.cluster.local:5432` | Keep direct or use session-mode pooler | `infrastructure/bootstrap/infrastructure/forgejo/controller/base/statefulset.yaml` + sandbox overlay + bootstrap job |
| Project deployments | `rig-db-rw.<namespace>.svc.cluster.local` | `rig-db-pooler-rw.<namespace>.svc.cluster.local` | Via `get_database_server()` in `opi/core/cluster_config.py` |

### Namespace-Specific Databases

| Component | Current Endpoint | New Endpoint | Notes |
|-----------|-----------------|--------------|-------|
| Project deployments | `<project>-db-rw.<infra-ns>.svc.cluster.local` | `<project>-db-pooler-rw.<infra-ns>.svc.cluster.local` | Via `get_database_cluster_service_endpoint()` in `opi/core/cluster_config.py` |

### Operations Manager Code

| File | Change |
|------|--------|
| `opi/core/cluster_config.py` | Update `get_database_server()` and `get_database_cluster_service_endpoint()` to return pooler service names |
| `opi/manager/database_manager.py` | Admin operations (create/delete database, manage users) must still use the direct `*-db-rw` endpoint since PgBouncer doesn't support DDL well in transaction mode |
| `manifests/postgresql-cluster.yaml.jinja` | Add companion Pooler resource generation |
| `DatabaseSecret.host` | Must point to pooler endpoint for application use |

### Key Distinction: Admin vs Application Connections

The Operations Manager itself performs DDL operations (CREATE DATABASE, CREATE USER, GRANT) which require direct PostgreSQL access. These admin connections must bypass PgBouncer. Only the application credentials stored in `DatabaseSecret` should point to the pooler.

This is already naturally separated — `database_manager.py` creates its own `PostgresConnector` with admin credentials, while `DatabaseSecret` is what gets injected into application pods.

## Implementation Phases

### Phase 1: Shared Database Pooler

1. Create a `Pooler` resource in `infrastructure/bootstrap/infrastructure/postgresql/database/base/`
2. Use `transaction` mode for project workloads
3. Update `get_database_server()` in `cluster_config.py` to return the pooler service name
4. Keep Keycloak and Forgejo on direct `rig-db-rw` (few connections, need full compatibility)
5. Verify admin operations in `database_manager.py` still use direct endpoint

### Phase 2: Namespace-Specific Database Poolers

1. Create `pooler.yaml.jinja` template
2. Generate Pooler resource alongside each `postgresql-cluster.yaml.jinja`
3. Update `get_database_cluster_service_endpoint()` to return pooler service name
4. Update ArgoCD application to include the Pooler resource

### Phase 3: Monitoring and Tuning

1. Add PgBouncer metrics scraping (CNPG exposes these automatically)
2. Monitor connection pool utilization
3. Tune `default_pool_size` and `max_client_conn` based on observed usage
4. Consider reducing `max_connections` on PostgreSQL back to 100 once pooling is effective

## Risks

- **Application compatibility**: Some ORMs or frameworks may use prepared statements or `SET` commands that break with transaction pooling. Needs per-project testing.
- **Admin operations routing**: DDL through PgBouncer in transaction mode can cause issues. Must ensure admin paths bypass the pooler.
- **Migration**: Changing the service endpoint for existing deployments requires ArgoCD to sync new connection strings into secrets, which triggers pod restarts.

## Dependencies

- CloudNativePG must support the `Pooler` resource (available since CNPG 1.15+)
- The current CNPG operator version in the cluster must be verified

## Related

- `infrastructure/bootstrap/infrastructure/postgresql/database/base/cluster.yaml` — shared database cluster
- `operations-manager/python/manifests/postgresql-cluster.yaml.jinja` — namespace-specific database template
- `opi/core/cluster_config.py` — database endpoint resolution
- `opi/manager/database_manager.py` — admin database operations
- `features/auto-resource-tuning.md` — related capacity management feature
