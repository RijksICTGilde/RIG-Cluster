# Namespace PostgreSQL Database Service

The `namespace-postgresql-database` service provisions a dedicated CloudNativePG (CNPG) PostgreSQL database cluster for a project, isolated in its own infrastructure namespace.

## What It Is

Unlike the shared `postgresql-database` service that uses the central RIG database, the `namespace-postgresql-database` service creates:

- A dedicated CNPG PostgreSQL cluster per project
- An isolated infrastructure namespace (`rig-<project>-infrastructure`)
- Project-specific database credentials and secrets
- Full control over database configuration (instances, storage, extensions)

## When to Use

Use `namespace-postgresql-database` when:

- Your application requires specific PostgreSQL extensions not available in the shared database
- You need SUPERUSER privileges for migrations or extension management
- You want database isolation from other projects
- Your application has specific PostgreSQL version or configuration requirements

## How to Use

### Basic Configuration

Add the service to your project's `services` section:

```yaml
services:
  - namespace-postgresql-database
```

This uses all default settings:
- Image: `ghcr.io/cloudnative-pg/postgresql:17`
- Instances: 1
- Storage: 10Gi
- No special privileges
- Default resource limits

### Custom Configuration

For more control, use the config block:

```yaml
services:
  - namespace-postgresql-database:
      config:
        instances: 2
        storage: 20Gi
        privileges:
          - SUPERUSER
        postInitSQL:
          - CREATE EXTENSION IF NOT EXISTS pg_trgm;
          - CREATE EXTENSION IF NOT EXISTS unaccent;
```

## Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `image` | string | `ghcr.io/cloudnative-pg/postgresql:17` | CNPG-compatible PostgreSQL image |
| `instances` | int | `1` | Number of PostgreSQL replicas |
| `storage` | string | `10Gi` | Storage size for each instance |
| `privileges` | list | `[]` | PostgreSQL privileges for the app user |
| `postInitSQL` | list | `[]` | SQL statements to run after database init |
| `resources` | object | See below | CPU/memory requests and limits |

### Resource Defaults

```yaml
resources:
  requests:
    memory: 256Mi
    cpu: 100m
  limits:
    memory: 512Mi
    cpu: 500m
```

### Valid Privileges

- `SUPERUSER` / `NOSUPERUSER`
- `CREATEDB` / `NOCREATEDB`
- `CREATEROLE` / `NOCREATEROLE`
- `LOGIN` / `NOLOGIN`
- `REPLICATION` / `NOREPLICATION`
- `BYPASSRLS` / `NOBYPASSRLS`

### postInitSQL

Use `postInitSQL` to create extensions or run setup SQL during database initialization:

```yaml
postInitSQL:
  - CREATE EXTENSION IF NOT EXISTS pg_trgm;
  - CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;
  - CREATE EXTENSION IF NOT EXISTS unaccent;
```

**Note:** The `vector` extension is always created by default.

## Environment Variables

The service provides these environment variables to your application:

| Variable | Description |
|----------|-------------|
| `DATABASE_SERVER_HOST` | Database hostname (service endpoint) |
| `DATABASE_SERVER_PORT` | Database port (5432) |
| `DATABASE_DB` | Database name |
| `DATABASE_SERVER_USER` | Database username |
| `DATABASE_PASSWORD` | Database password |

## Example: MijnBureau Docs

Here's a complete example for an application requiring multiple PostgreSQL extensions:

```yaml
name: mijn-bureau-docs
services:
  - namespace-postgresql-database:
      config:
        instances: 1
        storage: 1Gi
        privileges:
          - SUPERUSER
        postInitSQL:
          - CREATE EXTENSION IF NOT EXISTS pg_trgm;
          - CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;
          - CREATE EXTENSION IF NOT EXISTS unaccent;

deployments:
  - name: local-deployment
    cluster: local
    namespace: mijn-bureau-docs
    # ... rest of deployment config
```

## Infrastructure Created

When using `namespace-postgresql-database`, the Operations Manager creates:

1. **Namespace**: `rig-<project>-infrastructure`
2. **CNPG Cluster**: `<project>-db` in the infrastructure namespace
3. **Secrets**:
   - `<project>-postgres-superuser` (admin credentials)
   - Database user credentials in the deployment namespace
4. **Services**:
   - `<project>-db-rw` (read-write endpoint)
   - `<project>-db-r` (read endpoint)
   - `<project>-db-ro` (read-only endpoint)

## Troubleshooting

### Database Not Ready

Check CNPG cluster status:
```bash
kubectl get clusters.postgresql.cnpg.io -n rig-<project>-infrastructure
kubectl describe clusters.postgresql.cnpg.io <project>-db -n rig-<project>-infrastructure
```

### Extension Creation Failed

Ensure the user has `SUPERUSER` privilege if creating C language extensions:
```yaml
privileges:
  - SUPERUSER
```

### Pod Init Errors

Check CNPG operator logs:
```bash
kubectl logs -n cnpg-system -l app.kubernetes.io/name=cloudnative-pg
```

### Wrong PostgreSQL Image

CNPG requires images with postgres user UID 26. Use CNPG-compatible images:
- `ghcr.io/cloudnative-pg/postgresql:17` (default)
- Custom images built for CNPG

Do NOT use:
- `postgres:17` (Docker Hub official - wrong UID)
- `bitnami/postgresql:17` (wrong UID)
