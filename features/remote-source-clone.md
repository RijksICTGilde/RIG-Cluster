# Remote Source Clone

## What it is

Enables cloning database schemas and MinIO buckets from external sources across clusters using secure Chisel tunnels. This allows you to bootstrap a new deployment with data from an existing production or staging environment running on a different cluster.

## How to use it

### 1. Define remote sources in your project YAML

```yaml
remote-sources:
  - name: odcn-production          # Unique identifier for this remote source
    chisel:
      server-url: https://chisel-server.prd.apps.example.com
      username: admin
      password: |                  # AGE-encrypted with project's public key
        -----BEGIN AGE ENCRYPTED FILE-----
        ...
        -----END AGE ENCRYPTED FILE-----
    services:
      postgresql-database:
        host: my-db-service        # Service name in remote cluster
        port: 5432
        username: myuser
        password: |                # AGE-encrypted
          -----BEGIN AGE ENCRYPTED FILE-----
          ...
          -----END AGE ENCRYPTED FILE-----
        database: mydb
        schema: public
      minio-storage:
        host: my-minio-service     # Service name in remote cluster
        port: 9000
        access-key: |              # AGE-encrypted
          -----BEGIN AGE ENCRYPTED FILE-----
          ...
          -----END AGE ENCRYPTED FILE-----
        secret-key: |              # AGE-encrypted
          -----BEGIN AGE ENCRYPTED FILE-----
          ...
          -----END AGE ENCRYPTED FILE-----
        bucket: mybucket
        secure: false              # Use HTTP (false) or HTTPS (true)
```

### 2. Reference remote source in deployment clone-from

```yaml
deployments:
  - name: my-deployment
    cluster: local
    namespace: my-project
    clone-from:
      type: remote-source
      reference: odcn-production   # Links to remote-sources by name
      mode: once                   # Clone only if target doesn't exist
    components:
      - reference: my-component
        image: myorg/myapp:latest
```

## Configuration

### Remote Source Configuration

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique identifier for the remote source |
| `chisel.server-url` | Yes | URL of the Chisel tunnel server |
| `chisel.username` | Yes | Chisel authentication username |
| `chisel.password` | Yes | AGE-encrypted Chisel password |

### PostgreSQL Service Configuration

| Field | Required | Description |
|-------|----------|-------------|
| `host` | Yes | PostgreSQL service hostname in remote cluster |
| `port` | No | Port number (default: 5432) |
| `username` | Yes | Database username |
| `password` | Yes | AGE-encrypted database password |
| `database` | Yes | Source database name |
| `schema` | No | Schema to clone (default: public) |

### MinIO Service Configuration

| Field | Required | Description |
|-------|----------|-------------|
| `host` | Yes | MinIO service hostname in remote cluster |
| `port` | No | Port number (default: 9000) |
| `access-key` | Yes | AGE-encrypted MinIO access key |
| `secret-key` | Yes | AGE-encrypted MinIO secret key |
| `bucket` | Yes | Source bucket name |
| `secure` | No | Use HTTPS (default: false) |

### Clone-from Configuration

| Field | Required | Description |
|-------|----------|-------------|
| `type` | Yes | Must be `remote-source` |
| `reference` | Yes | Name of the remote source to clone from |
| `mode` | No | `once` (only if empty) or `force` (always overwrite) |

## Examples

### Complete project with remote source clone

```yaml
name: my-project

remote-sources:
  - name: production-cluster
    chisel:
      server-url: https://chisel.production.example.com
      username: tunnel-user
      password: |
        -----BEGIN AGE ENCRYPTED FILE-----
        YWdlLWVuY3J5cHRpb24ub3JnL3YxCi0+IFgy...
        -----END AGE ENCRYPTED FILE-----
    services:
      postgresql-database:
        host: postgres-primary.database.svc
        port: 5432
        username: app_user
        password: |
          -----BEGIN AGE ENCRYPTED FILE-----
          YWdlLWVuY3J5cHRpb24ub3JnL3YxCi0+IFgy...
          -----END AGE ENCRYPTED FILE-----
        database: app_production
        schema: public
      minio-storage:
        host: minio.storage.svc
        port: 9000
        access-key: |
          -----BEGIN AGE ENCRYPTED FILE-----
          YWdlLWVuY3J5cHRpb24ub3JnL3YxCi0+IFgy...
          -----END AGE ENCRYPTED FILE-----
        secret-key: |
          -----BEGIN AGE ENCRYPTED FILE-----
          YWdlLWVuY3J5cHRpb24ub3JnL3YxCi0+IFgy...
          -----END AGE ENCRYPTED FILE-----
        bucket: app-files
        secure: false

services:
  - postgresql-database
  - minio-storage

config:
  age-public-key: age1abc123...

deployments:
  - name: staging
    cluster: staging-cluster
    namespace: my-project-staging
    clone-from:
      type: remote-source
      reference: production-cluster
      mode: once
    components:
      - reference: web-app
        image: myorg/web-app:staging
```

### Clone only database (no MinIO)

If you only need to clone the database and not MinIO storage, simply omit the `minio-storage` from the remote source services:

```yaml
remote-sources:
  - name: db-only-source
    chisel:
      server-url: https://chisel.example.com
      username: admin
      password: |
        -----BEGIN AGE ENCRYPTED FILE-----
        ...
        -----END AGE ENCRYPTED FILE-----
    services:
      postgresql-database:
        host: db-readonly.example.svc
        port: 5432
        username: readonly_user
        password: |
          -----BEGIN AGE ENCRYPTED FILE-----
          ...
          -----END AGE ENCRYPTED FILE-----
        database: production_db
        schema: app_schema
```

### Multiple remote sources

You can define multiple remote sources for different environments:

```yaml
remote-sources:
  - name: eu-production
    chisel:
      server-url: https://chisel.eu.example.com
      username: admin
      password: |
        -----BEGIN AGE ENCRYPTED FILE-----
        ...
        -----END AGE ENCRYPTED FILE-----
    services:
      postgresql-database:
        host: postgres-eu.svc
        database: app_eu

  - name: us-production
    chisel:
      server-url: https://chisel.us.example.com
      username: admin
      password: |
        -----BEGIN AGE ENCRYPTED FILE-----
        ...
        -----END AGE ENCRYPTED FILE-----
    services:
      postgresql-database:
        host: postgres-us.svc
        database: app_us

deployments:
  - name: eu-staging
    clone-from:
      type: remote-source
      reference: eu-production
      mode: once
    # ...

  - name: us-staging
    clone-from:
      type: remote-source
      reference: us-production
      mode: once
    # ...
```

## Clone Modes

| Mode | Behavior |
|------|----------|
| `once` | Clone only if the target database/bucket is empty or doesn't exist. Safe for repeated deployments. |
| `force` | Always clone, overwriting any existing data. Use with caution! |

## How it works

1. When a deployment with `clone-from.type: remote-source` is processed:
   - The operations manager looks up the referenced remote source configuration
   - A Chisel tunnel is established to the remote cluster
   - Database schema is cloned via pg_dump/pg_restore through the tunnel
   - MinIO bucket contents are synced through the tunnel
   - The tunnel is automatically closed after the operation

2. All credentials (Chisel password, database password, MinIO keys) are:
   - Encrypted with the project's AGE public key
   - Decrypted at runtime using the project's private key
   - Never stored in plain text

## Dependencies

- **Chisel Server**: A Chisel tunnel server must be running and accessible from the operations manager
- **Network Access**: The Chisel server must have network access to the remote PostgreSQL and MinIO services
- **Services Required**: The deployment must have `postgresql-database` and/or `minio-storage` services enabled

## Troubleshooting

### Clone fails with connection timeout
- Verify Chisel server URL is correct and accessible
- Check Chisel credentials are properly AGE-encrypted
- Ensure the remote services are reachable from the Chisel server

### Database clone fails
- Verify PostgreSQL credentials have read access to the source database
- Check the source database and schema exist
- Review operations-manager logs for pg_dump/pg_restore errors

### MinIO clone fails
- Verify MinIO credentials have read access to the source bucket
- Check the source bucket exists
- Ensure `secure` setting matches the actual MinIO configuration

### Password decryption failure
- Ensure all passwords are AGE-encrypted with the project's public key
- Verify the project has a valid AGE keypair in `config.age-public-key` and `config.age-private-key`

## Security Considerations

1. All remote source credentials should be AGE-encrypted
2. Use read-only credentials for source databases when possible
3. Chisel provides encrypted tunnels, but ensure your Chisel server is properly secured
4. Consider using `mode: once` to prevent accidental data overwrites
5. Rotate remote source credentials regularly
