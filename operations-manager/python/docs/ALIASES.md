# Environment Variable Aliases

## Overview

The alias feature allows you to create custom environment variable names that reference system-provided variables. This is useful when your application expects specific environment variable names that differ from the standard variables provided by the operations-manager.

## How It Works

### Basic Concept

When you deploy a component with services (database, MinIO, Keycloak), the operations-manager automatically provides environment variables for each service:

**Database Service Provides:**
- `DATABASE_SERVER_HOST` - PostgreSQL server hostname
- `DATABASE_SERVER_PORT` - PostgreSQL server port
- `DATABASE_SERVER_USER` - Database username
- `DATABASE_PASSWORD` - Database password
- `DATABASE_DB` - Database name
- `DATABASE_SCHEMA` - Database schema name
- Plus aliases: `APP_DATABASE_*` versions of the above

**MinIO Service Provides:**
- `OBJECT_STORE_URL` - MinIO server URL
- `OBJECT_STORE_USER` - MinIO access key
- `OBJECT_STORE_PASSWORD` - MinIO secret key
- `OBJECT_STORE_BUCKET_NAME` - MinIO bucket name
- `OBJECT_STORE_REGION` - MinIO region

**Keycloak Service Provides:**
- `OIDC_CLIENT_ID` - OAuth2/OIDC client ID
- `OIDC_CLIENT_SECRET` - OAuth2/OIDC client secret
- `OIDC_DISCOVERY_URL` - OIDC discovery endpoint URL

### Creating Aliases

If your application needs these variables under different names, you can define aliases in your component configuration:

```yaml
deployments:
  - name: production
    services:
      - postgresql-database
      - minio-storage
    components:
      - name: api
        services:
          - publish-on-web
        aliases:
          # Create custom database connection string
          DATABASE_URL: "$DATABASE_SERVER_HOST:$DATABASE_SERVER_PORT/$DATABASE_DB"

          # Alternative PostgreSQL connection string
          POSTGRES_CONNECTION: "postgresql://$DATABASE_SERVER_USER:$DATABASE_PASSWORD@$DATABASE_SERVER_HOST/$DATABASE_DB"

          # Custom MinIO endpoint
          S3_ENDPOINT: "$OBJECT_STORE_URL/$OBJECT_STORE_BUCKET_NAME"

          # Simple variable rename
          DB_HOST: "$DATABASE_SERVER_HOST"
          S3_BUCKET: "$OBJECT_STORE_BUCKET_NAME"
```

## Variable Reference Syntax

Aliases support two variable reference formats:

1. **Simple format:** `$VARIABLE_NAME`
2. **Braced format:** `${VARIABLE_NAME}` (recommended for clarity)

Both formats work identically. The braced format is recommended when combining multiple variables or when variable names might be ambiguous.

### Examples

```yaml
aliases:
  # Both of these work:
  DB_HOST: "$DATABASE_SERVER_HOST"
  DB_HOST: "${DATABASE_SERVER_HOST}"

  # Combining multiple variables:
  CONNECTION: "${DATABASE_SERVER_USER}:${DATABASE_PASSWORD}@${DATABASE_SERVER_HOST}"

  # Literals with variables:
  API_URL: "https://${DATABASE_SERVER_HOST}/api"
```

### Escaping

If you need a literal dollar sign in your value, use `$$`:

```yaml
aliases:
  PRICE: "$$10.00"  # Results in: $10.00
```

## How Aliases Are Resolved

### Build-Time Resolution

Aliases are resolved **at build time** when creating deployment manifests. This means:

1. When you deploy a component, the operations-manager scans all components for aliases
2. Aliases are categorized by which service they reference (database, minio, keycloak)
3. When creating each service's secret, the relevant aliases are resolved and added
4. The resolved values are stored in the secret and encrypted with SOPS

**Example:**

```yaml
# You define:
aliases:
  DATABASE_URL: "$DATABASE_SERVER_HOST:$DATABASE_SERVER_PORT/$DATABASE_DB"

# At build time, this becomes (in the database secret):
DATABASE_URL: "postgres.svc.cluster.local:5432/myapp"

# The secret is then encrypted and deployed to Kubernetes
```

### Where Aliases Are Stored

Aliases are added to the **deployment-level secrets** for the service they reference:

- **Database aliases** → Added to `{deployment}-database` secret
- **MinIO aliases** → Added to `{deployment}-minio` secret
- **Keycloak aliases** → Added to `{deployment}-keycloak` secret

This means:
- All components in a deployment share the same resolved aliases
- Aliases are mounted into pods via `envFrom` (just like the original variables)
- No runtime script execution or performance overhead

## Rules and Restrictions

### 1. Aliases Must Reference Known Variables

You can only reference variables provided by the services. Referencing unknown variables will result in an error:

```yaml
# ERROR - UNKNOWN_VAR is not provided by any service:
aliases:
  MY_VAR: "$UNKNOWN_VAR"
```

**Error message will show all available variables:**
```
Alias 'MY_VAR' references unknown variables: UNKNOWN_VAR.
Available variables: DATABASE_DB, DATABASE_PASSWORD, DATABASE_SCHEMA, ...
```

### 2. Aliases Must Reference Only One Service

Each alias must reference variables from a single service (database, minio, OR keycloak), not multiple:

```yaml
# ERROR - references both database and MinIO:
aliases:
  MIXED: "$DATABASE_SERVER_HOST and $OBJECT_STORE_URL"
```

**Error message:**
```
Alias 'MIXED' references variables from multiple services: database, minio.
Each alias must reference variables from only one service.
```

**Why this restriction?**
- Keeps the architecture simple
- Makes it clear which secret contains which aliases
- Prevents complex dependency issues

**Workaround:** Create separate aliases:
```yaml
aliases:
  DB_ENDPOINT: "$DATABASE_SERVER_HOST"
  S3_ENDPOINT: "$OBJECT_STORE_URL"
```

### 3. Aliases Must Have At Least One Variable Reference

Aliases without variable references are not allowed:

```yaml
# ERROR - no variable reference:
aliases:
  STATIC_VALUE: "some-literal-value"
```

**Why?** Use `user-env-vars` for static values instead. Aliases are specifically for referencing system-provided variables.

### 4. No Circular References

Aliases cannot reference other aliases:

```yaml
# ERROR - circular reference:
aliases:
  VAR_A: "$VAR_B"
  VAR_B: "$VAR_A"
```

Aliases can only reference the base variables provided by services, not other aliases.

## Complete Example

```yaml
deployments:
  - name: production
    cluster: local
    namespace: myapp-prod
    services:
      - postgresql-database
      - minio-storage

    components:
      - name: backend
        image: my-backend:latest
        port: 8080
        services:
          - publish-on-web

        # Define aliases for database connection
        aliases:
          # Django-style database URL
          DATABASE_URL: "postgresql://${DATABASE_SERVER_USER}:${DATABASE_PASSWORD}@${DATABASE_SERVER_HOST}:${DATABASE_SERVER_PORT}/${DATABASE_DB}"

          # Separate host/port for configuration
          DB_HOSTNAME: "${DATABASE_SERVER_HOST}"
          DB_PORT: "${DATABASE_SERVER_PORT}"
          DB_NAME: "${DATABASE_DB}"

          # MinIO configuration with custom names
          AWS_S3_ENDPOINT_URL: "${OBJECT_STORE_URL}"
          AWS_ACCESS_KEY_ID: "${OBJECT_STORE_USER}"
          AWS_SECRET_ACCESS_KEY: "${OBJECT_STORE_PASSWORD}"
          AWS_STORAGE_BUCKET_NAME: "${OBJECT_STORE_BUCKET_NAME}"

      - name: frontend
        image: my-frontend:latest
        port: 3000
        services:
          - publish-on-web

        # Frontend only needs the API URL (no database access)
        # Uses user-env-vars for static configuration
        user-env-vars:
          REACT_APP_API_URL: "https://api.example.com"
```

## Difference Between Aliases and User-Env-Vars

| Feature | Aliases | User-Env-Vars |
|---------|---------|---------------|
| Purpose | Reference system-provided variables | Define custom static values |
| Syntax | Must use `$VAR` references | Plain key-value pairs |
| Resolution | Build-time (during manifest creation) | No resolution needed |
| Storage | Added to service secrets (shared across components) | Stored in component-specific secret |
| Use Case | Rename/combine provided variables | Custom configuration values |

**Example:**

```yaml
components:
  - name: api
    # Use aliases to reference system variables:
    aliases:
      DATABASE_URL: "$DATABASE_SERVER_HOST:$DATABASE_SERVER_PORT"

    # Use user-env-vars for your own values:
    user-env-vars:
      LOG_LEVEL: "info"
      APP_NAME: "my-api"
      FEATURE_FLAG_X: "enabled"
```

## Debugging Aliases

### Check Alias Resolution in Logs

When processing a deployment, the operations-manager logs:

```
INFO - Collected 3 aliases for deployment 'production' (database: 2, minio: 1)
DEBUG - Alias 'DATABASE_URL' references database vars, categorized as 'database'
DEBUG - Resolving 2 database aliases for deployment production
INFO - Added 2 resolved database aliases to deployment secret
```

### Inspect Generated Secrets

After deployment, you can view the resolved aliases in the secret:

```bash
# Decrypt and view the database secret
sops -d production-database-secret.sops.yaml

# You'll see both original variables and your aliases:
data:
  DATABASE_SERVER_HOST: "postgres.svc"
  DATABASE_SERVER_PORT: "5432"
  DATABASE_URL: "postgres.svc:5432/mydb"  # Your alias!
```

### Common Errors

**Error: Unknown variable**
```
ValueError: Alias 'MY_VAR' references unknown variables: SOME_VAR
```
**Solution:** Check the available variables list in the error message and use only those.

**Error: Multiple services**
```
ValueError: Alias 'MIXED' references variables from multiple services: database, minio
```
**Solution:** Split into separate aliases, one per service.

**Error: No variable references**
```
ValueError: Alias 'STATIC' has no variable references
```
**Solution:** Use `user-env-vars` for static values instead of aliases.

## Security Considerations

1. **Build-time resolution:** Variables are resolved when creating manifests, not at runtime
2. **SOPS encryption:** All resolved aliases are encrypted with SOPS before being stored in git
3. **No sensitive data in logs:** Only alias names are logged, never the resolved values
4. **No code execution:** Pure string substitution, no shell scripts or eval

## Best Practices

1. **Use descriptive alias names:** Make it clear what the alias represents
   ```yaml
   # Good:
   DATABASE_CONNECTION_STRING: "$DATABASE_SERVER_HOST:$DATABASE_SERVER_PORT"

   # Less clear:
   DB_CONN: "$DATABASE_SERVER_HOST:$DATABASE_SERVER_PORT"
   ```

2. **Document complex aliases:** Add comments explaining non-obvious transformations
   ```yaml
   aliases:
     # Full PostgreSQL connection string with schema parameter
     DATABASE_URL: "postgresql://$DATABASE_SERVER_USER:$DATABASE_PASSWORD@$DATABASE_SERVER_HOST/$DATABASE_DB?options=--search_path%3D$DATABASE_SCHEMA"
   ```

3. **Keep aliases simple:** If you need complex logic, consider handling it in your application instead

4. **Use braced syntax for clarity:** `${VAR}` is more explicit than `$VAR`

5. **Test with a simple alias first:** Verify the feature works before creating complex aliases
