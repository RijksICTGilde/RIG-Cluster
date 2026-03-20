# PostgreSQL Connection Pooling

## Status

**Superseded** by [PgBouncer Connection Pooling](../pgbouncer-connection-pooling.md) - which provides a more specific implementation design using CloudNativePG's native PgBouncer pooler support.

## Overview

As the RIG platform grows and manages more projects with individual PostgreSQL databases, connection management becomes critical. A shared connection pooler in front of PostgreSQL databases becomes almost mandatory to maintain performance and prevent connection exhaustion.

## Problem Statement

### Current Architecture

Currently, each project can provision its own PostgreSQL database through the Operations Manager. Applications connect directly to PostgreSQL, with each application maintaining its own connection pool.

### Challenges Without Connection Pooling

1. **Connection Limits**: PostgreSQL has a finite limit on concurrent connections (typically 100-300)
2. **Connection Overhead**: Each connection consumes memory (~10MB) and creates process overhead
3. **Multi-Tenant Scaling**: With many projects/databases, total connections can quickly exhaust available capacity
4. **Idle Connections**: Applications may hold idle connections, wasting resources
5. **Connection Storms**: During deployments or scaling events, connection spikes can overwhelm the database

## Solution: Connection Pooling

A connection pooler sits between applications and PostgreSQL, multiplexing many client connections onto a smaller pool of database connections.

### Benefits

- **Reduced Connection Count**: Serve hundreds of client connections with dozens of database connections
- **Lower Memory Usage**: Fewer PostgreSQL backend processes
- **Better Performance**: Faster connection establishment (reuse existing connections)
- **Protection**: Prevents connection storms from overwhelming the database
- **Multi-Database Support**: Single pooler instance can manage connections to multiple databases

## Recommended Solutions

### 1. PgBouncer (Recommended)

**Pros:**
- Mature, battle-tested solution
- Lightweight (written in C)
- Simple configuration
- Widely used in production
- Good Kubernetes operator support

**Cons:**
- Limited protocol features
- No connection migration during reload (brief downtime)

**Configuration Example:**
```ini
[databases]
* = host=postgres port=5432

[pgbouncer]
listen_port = 5432
listen_addr = *
auth_type = md5
auth_file = /etc/pgbouncer/userlist.txt
pool_mode = transaction
max_client_conn = 1000
default_pool_size = 25
```

### 2. Odyssey (Alternative)

**Pros:**
- Multi-threaded (better for high-concurrency)
- Advanced routing capabilities
- Connection migration without downtime
- Modern codebase

**Cons:**
- Less mature than PgBouncer
- Smaller community
- More complex configuration

### 3. PgCat (Emerging)

**Pros:**
- Written in Rust (memory-safe)
- Built-in query load balancing
- Read/write splitting
- Modern architecture

**Cons:**
- Very new (less production usage)
- Limited ecosystem/tooling
- Fewer deployment examples

## Pooling Modes

### Transaction Mode (Recommended)

Connection is returned to pool after each transaction completes.

**Advantages:**
- Highest connection multiplexing
- Best resource utilization
- Ideal for microservices with short transactions

**Limitations:**
- Cannot use session-level features (prepared statements, temp tables, advisory locks)
- Most applications work fine with this mode

### Session Mode

Connection held for entire client session.

**Advantages:**
- Full PostgreSQL feature support
- No application changes needed

**Disadvantages:**
- Lower connection multiplexing
- Less effective pooling

### Statement Mode

Connection returned after each statement (rarely used).

## Architecture Integration

### Deployment Pattern

```
┌─────────────────┐
│   Application   │
│    (Project)    │
└────────┬────────┘
         │
         ├─ postgresql://pgbouncer.namespace:5432/dbname
         │
         ▼
┌─────────────────┐
│    PgBouncer    │
│  (per-project   │
│   or shared)    │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   PostgreSQL    │
│   (CloudNative  │
│      PG)        │
└─────────────────┘
```

### Deployment Options

#### Option 1: Sidecar Pattern (Per-Project)

Deploy PgBouncer as a sidecar container alongside each project's PostgreSQL instance.

**Pros:**
- Isolated per project
- Simple RBAC
- Project-specific tuning

**Cons:**
- More resource overhead
- Harder to manage at scale

#### Option 2: Shared Pooler (Multi-Tenant)

Deploy a shared PgBouncer instance that pools connections for multiple databases.

**Pros:**
- Lower resource overhead
- Centralized management
- Better resource utilization

**Cons:**
- Requires careful configuration
- Shared fate (one pooler failure affects multiple projects)

#### Option 3: Namespace-Level Pooler

Deploy one PgBouncer per namespace, pooling all databases in that namespace.

**Pros:**
- Balance between isolation and efficiency
- Natural security boundary
- Manageable at scale

**Cons:**
- Moderate complexity

## Implementation Considerations

### When to Implement

Connection pooling should be implemented when:

1. **Connection count approaches limits**: `SELECT count(*) FROM pg_stat_activity` consistently shows >50% of max_connections
2. **Multiple projects deployed**: 5+ projects with individual databases
3. **Microservices architecture**: Many small services connecting to databases
4. **Connection errors**: "FATAL: remaining connection slots are reserved" errors occur
5. **Performance issues**: Slow connection establishment times

### Configuration Guidelines

For transaction-mode pooling with CloudNativePG:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: pgbouncer-config
data:
  pgbouncer.ini: |
    [databases]
    * = host=postgres-cluster-rw port=5432

    [pgbouncer]
    listen_port = 5432
    listen_addr = *
    auth_type = scram-sha-256
    auth_query = SELECT usename, passwd FROM pg_shadow WHERE usename=$1
    pool_mode = transaction

    # Connection limits
    max_client_conn = 1000
    default_pool_size = 25
    min_pool_size = 5
    reserve_pool_size = 5

    # Timeouts
    server_idle_timeout = 600
    query_timeout = 0
    idle_transaction_timeout = 0

    # Logging
    log_connections = 1
    log_disconnections = 1
```

### Monitoring

Key metrics to monitor:

- **Client connections**: Total active client connections
- **Server connections**: Actual PostgreSQL connections
- **Pool saturation**: Clients waiting for connections
- **Query duration**: Average query execution time
- **Connection churn**: Rate of connection create/destroy

Prometheus metrics can be exposed using `pgbouncer_exporter`.

### Application Changes

For transaction-mode pooling, applications must avoid:

- **Prepared statements with name**: Use unnamed prepared statements or disable them
- **Temporary tables**: These are session-scoped
- **Advisory locks**: Session-level locking
- **SET SESSION**: Session variables (use SET LOCAL or transaction-level)
- **LISTEN/NOTIFY**: Requires persistent connection

Most modern ORMs (Django, SQLAlchemy, ActiveRecord) work fine with transaction pooling by default.

## Migration Path

1. **Measure baseline**: Collect connection count and performance metrics
2. **Test in development**: Deploy PgBouncer to development environment
3. **Validate applications**: Ensure no session-level features are used
4. **Pilot with one project**: Deploy to production for a single, non-critical project
5. **Monitor and tune**: Adjust pool sizes based on actual usage
6. **Gradual rollout**: Expand to more projects incrementally
7. **Make it default**: Include PgBouncer in project provisioning templates

## Kubernetes Operator Options

### PgBouncer Operator

- **Repo**: https://github.com/carlosedp/pgbouncer-operator
- **Status**: Community-maintained
- **Features**: CRD-based configuration, automatic secret management

### CloudNativePG with Pooler

- **Repo**: https://cloudnative-pg.io/
- **Status**: Official support for PgBouncer
- **Features**: Integrated pooler configuration in Cluster resource

Example CloudNativePG with pooler:

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: postgres-cluster
spec:
  instances: 3

  # Enable connection pooling
  pooler:
    enabled: true
    instances: 2
    pgbouncer:
      poolMode: transaction
      parameters:
        max_client_conn: "1000"
        default_pool_size: "25"
```

## Cost-Benefit Analysis

### Without Pooler

- **PostgreSQL max_connections**: 100
- **Per-connection memory**: 10MB
- **Total memory overhead**: 1GB
- **Realistic usable connections**: ~70 (accounting for reserved connections)
- **Projects supported**: ~7 (assuming 10 connections per project)

### With Transaction Pooler

- **Client connections supported**: 1000+
- **PostgreSQL connections needed**: 25-50
- **Memory overhead**: 250-500MB
- **Projects supported**: 50+ (with proper pooling)
- **Additional resource cost**: ~100MB RAM, 0.1 CPU for pooler

**ROI**: Minimal resource cost enables 5-10x more projects per PostgreSQL cluster.

## Related Features

- [Auto Database Provisioning](./auto-database-provisioning.md) - Provisioning could include PgBouncer setup
- PostgreSQL Cluster Management - Connection pooling integrates with CloudNativePG clusters

## References

- [PgBouncer Documentation](https://www.pgbouncer.org/)
- [Odyssey](https://github.com/yandex/odyssey)
- [PgCat](https://github.com/postgresml/pgcat)
- [CloudNativePG Pooler](https://cloudnative-pg.io/documentation/current/connection_pooling/)
- [PostgreSQL Connection Pooling Best Practices](https://wiki.postgresql.org/wiki/Number_Of_Database_Connections)

---

**Last Updated**: 2026-01-14
**Status**: Planning / Not Implemented
**Priority**: Medium (implement before hitting 10+ active projects)
