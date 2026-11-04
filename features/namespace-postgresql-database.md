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
| `image` | string | `ghcr.io/cloudnative-pg/postgresql:17` | CNPG-compatible PostgreSQL image. Letters, digits and `. _ : / @ -` only |
| `instances` | int | `1` | Number of PostgreSQL replicas |
| `storage` | string | `1Gi` | Storage size for each instance. Between `50Mi` and `20Gi`, and it can only ever go UP: a PVC cannot shrink |
| `privileges` | list | `[]` | PostgreSQL privileges for the app user |
| `postInitSQL` | list | `[]` | PostgreSQL extensions to create: each as `CREATE EXTENSION IF NOT EXISTS <name> [SCHEMA <schema>];` |
| `resources` | object | See below | CPU/memory requests and limits. Memory 25Mi-1024Mi (requests) / 25Mi-4096Mi (limits), CPU 25m-250m / 25m-4000m |

De grenzen op `storage` en `resources` zijn gedeclareerd met het mechanisme uit
`speelruimte-van-een-dienst.md` (`shared/postgres_settings.py`), dus het formulier, de
service-config-API en een met de hand bijgewerkt projectbestand geven dezelfde weigering.

Het formulier biedt `storage` als keuzelijst aan: de maten van het platform (50Mi tot 1Gi)
plus de opgeslagen waarde als die er niet in staat. Die laatste moet erbij, want de
speelruimte loopt tot 20Gi en een volume mag niet krimpen: zonder de eigen waarde bood de
select een project op 2Gi alleen kleinere maten aan.

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

Use `postInitSQL` to create PostgreSQL extensions during database initialization. Each statement must be in the form `CREATE EXTENSION IF NOT EXISTS <name> [SCHEMA <schema>];`. Other SQL statements are not allowed: this field runs as the PostgreSQL superuser, so it accepts extensions only.

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

## What OPI Waits For

Provisioning a dedicated cluster is a two-stage wait, in this order:

1. **ArgoCD reports the infrastructure application Synced and Healthy.** The health of a CNPG
   `Cluster` comes from a Lua check in the ArgoCD CR, which calls the cluster healthy as soon
   as `status.readyInstances` equals `spec.instances`, so as soon as the pod is Ready.
2. **The database accepts connections.** OPI connects to `<project>-db-rw` with the superuser
   and runs `SELECT 1`, retrying for at most 180 seconds. Stage 1 is reached before the primary
   listens: on 3 November 2026 provisioning of a new project died on a `ConnectionRefusedError`
   right after the healthy signal, and the task failed permanently. Databases and roles are
   created only after this stage.

If no connection is accepted within those 180 seconds, the task fails with a message naming the
host, plus the cluster's `status.phase` and `status.phaseReason` when CNPG published them.

### The warning "databasecluster niet Ready volgens CNPG"

After the wait, OPI reads the `Cluster` resource and checks its `Ready` condition. When that
condition is not `True`, the task logs a warning (with phase and phaseReason) and continues.

That warning means: **the database works, but the CNPG operator cannot reach the pods.** The
application connects over the `-rw` service, which is unaffected, so provisioning and the
application are fine. What is not guaranteed is everything the operator does on top of a
running instance: backups, failover and switchover.

The usual cause is the network policy. The generated `tenant-baseline` policy in the
infrastructure namespace allows ingress from the operator's namespace, and which namespace that
is comes from `database_operator_namespace` per cluster in `opi/core/cluster_config.py`. If that
value does not match the namespace the operator actually runs in, the operator cannot scrape
instance status on port 8000. The cluster then stays at `Ready=False` with
`phase: Instance Status Extraction Error: HTTP communication issue`.

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
