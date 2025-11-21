# Database Migration Guide - External Source Cloning

This guide explains how to clone databases from external sources (like other clusters) into your RIG-Cluster deployments using the Operations Manager API.

## Overview

The external database cloning feature allows you to:
- Migrate databases from Digilab cluster to local Kind cluster
- Migrate databases from Digilab to ODCN production
- Clone any PostgreSQL database accessible via network/port-forward

## Architecture

The implementation follows single-responsibility principles:

1. **`_validate_external_source()`** - Validates external source connectivity and schema existence
2. **`clone_database_from_external_source()`** - Orchestrator that reuses existing methods:
   - Validates source and target
   - Resolves credentials (reuses `_resolve_database_credentials()`)
   - Handles force_clone (reuses `delete_database()` + `create_database()`)
   - Executes clone (reuses `clone_schema_from_external()`)
   - Stores credentials

## API Endpoint

```
POST /api/projects/{project_name}/deployments/{deployment_name}/:clone-database-from-external
```

### Request Headers
- `X-API-Key` - Required project API key

### Request Body

```json
{
  "sourceHost": "localhost",
  "sourcePort": 15432,
  "sourceUsername": "postgres",
  "sourcePassword": "password",
  "sourceDatabase": "amt_staging",
  "sourceSchema": "amt_staging",
  "forceClone": true
}
```

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `sourceHost` | string | Yes | External database host (e.g., `localhost` for port-forward) |
| `sourcePort` | integer | Yes | External database port (e.g., `15432`) |
| `sourceUsername` | string | Yes | Username for source database |
| `sourcePassword` | string | Yes | Password for source database |
| `sourceDatabase` | string | Yes | Source database name |
| `sourceSchema` | string | Yes | Source schema name to clone |
| `forceClone` | boolean | No | If true, drops existing target database (default: false) |

### Response

**Success (200)**:
```json
{
  "status": "success",
  "message": "Database cloned successfully from localhost:15432 to amt/production",
  "project": "amt",
  "deployment": "production",
  "source": {
    "host": "localhost",
    "port": 15432,
    "database": "amt_staging",
    "schema": "amt_staging"
  },
  "target": {
    "database": "amt_production",
    "schema": "amt_production",
    "username": "amt_production"
  },
  "operations": [
    {
      "type": "source_validation",
      "status": "success",
      "table_count": 42
    },
    {
      "type": "target_validation",
      "status": "success"
    },
    {
      "type": "credentials_resolved",
      "status": "success"
    },
    {
      "type": "database_dropped",
      "status": "success"
    },
    {
      "type": "database_recreated",
      "status": "success"
    },
    {
      "type": "database_cloned",
      "status": "success"
    },
    {
      "type": "credentials_stored",
      "status": "success"
    }
  ],
  "errors": []
}
```

**Failure (500)**:
```json
{
  "status": "failed",
  "message": "Database clone failed: Source validation failed: Source database 'amt_staging' does not exist at localhost:15432",
  "project": "amt",
  "deployment": "production",
  "source": {
    "host": "localhost",
    "port": 15432,
    "database": "amt_staging",
    "schema": "amt_staging"
  },
  "target": {},
  "operations": [
    {
      "type": "source_validation",
      "status": "failed",
      "error": "Source database 'amt_staging' does not exist at localhost:15432"
    }
  ],
  "errors": [
    "Source validation failed: Source database 'amt_staging' does not exist at localhost:15432"
  ]
}
```

## Usage Examples

### Example 1: Digilab to Local Kind (AMT Migration)

**Step 1: Port-forward source database from Digilab**
```bash
# Connect to Digilab cluster
kubectl config use-context digilab

# Port-forward PostgreSQL from Digilab
kubectl port-forward -n tn-ai-validation-amt svc/amt-cluster-db-rw 15432:5432
```

**Step 2: Clone database to local kind**
```bash
curl -X POST "http://localhost:9595/api/projects/amt/deployments/local/:clone-database-from-external" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-amt-api-key" \
  -d '{
    "sourceHost": "localhost",
    "sourcePort": 15432,
    "sourceUsername": "postgres",
    "sourcePassword": "digilab-postgres-password",
    "sourceDatabase": "amt_production",
    "sourceSchema": "amt_production",
    "forceClone": true
  }'
```

### Example 2: Digilab to ODCN Production

**Step 1: Port-forward source (Digilab) and target (ODCN) databases**
```bash
# Terminal 1: Port-forward from Digilab (source)
kubectl config use-context digilab
kubectl port-forward -n amt-namespace svc/postgresql 15432:5432

# Terminal 2: Access ODCN operations-manager
kubectl config use-context odcn-production
kubectl port-forward -n rig-system svc/operations-manager 9595:9595
```

**Step 2: Clone from Digilab to ODCN**
```bash
curl -X POST "http://localhost:9595/api/projects/amt/deployments/production/:clone-database-from-external" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-amt-api-key" \
  -d '{
    "sourceHost": "localhost",
    "sourcePort": 15432,
    "sourceUsername": "postgres",
    "sourcePassword": "digilab-postgres-password",
    "sourceDatabase": "amt_production",
    "sourceSchema": "amt_production",
    "forceClone": true
  }'
```

### Example 3: Clone Without Force (Safe Mode)

If you want to prevent accidental overwrites, omit `forceClone` or set it to `false`:

```bash
curl -X POST "http://localhost:9595/api/projects/amt/deployments/staging/:clone-database-from-external" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-amt-api-key" \
  -d '{
    "sourceHost": "localhost",
    "sourcePort": 15432,
    "sourceUsername": "postgres",
    "sourcePassword": "source-password",
    "sourceDatabase": "amt_production",
    "sourceSchema": "amt_production",
    "forceClone": false
  }'
```

This will fail if the target database already exists, preventing accidental data loss.

## Validation Process

The API performs comprehensive validation before cloning:

### Source Validation
1. **Connectivity** - Can we connect to source host:port?
2. **Authentication** - Are credentials valid?
3. **Database Exists** - Does source database exist?
4. **Schema Exists** - Does source schema exist?
5. **Table Count** - How many tables are in the schema? (logged for verification)

### Target Validation
1. **Project Exists** - Does the target project exist?
2. **Deployment Exists** - Does the target deployment exist?
3. **PostgreSQL Enabled** - Does the deployment use PostgreSQL service?
4. **Credentials** - Can we create/resolve target database credentials?

### Clone Execution
1. **Drop (if force_clone)** - Safely drops existing target database
2. **Create** - Creates target database with proper owner
3. **pg_dump Clone** - Streams data using pg_dump | psql pipeline
4. **Ownership** - Sets proper schema ownership
5. **Credentials Storage** - Stores credentials in Kubernetes secrets

## Error Handling

The API provides detailed error messages for common issues:

| Error | Cause | Solution |
|-------|-------|----------|
| `Source database does not exist` | Wrong database name or inaccessible | Verify port-forward and database name |
| `Authentication failed` | Wrong username/password | Check source credentials |
| `Source schema does not exist` | Schema name mismatch | Verify schema name in source |
| `Deployment does not use PostgreSQL` | Target deployment has no database | Add PostgreSQL to deployment services |
| `Target database already exists` | Database exists and forceClone=false | Use forceClone=true or delete manually |

## Security Considerations

1. **Credentials in Transit** - Source credentials are sent in request body
   - Use HTTPS in production
   - Consider using Kubernetes secrets for source credentials

2. **Target Credentials** - Target credentials are managed by operations-manager
   - Stored in Kubernetes secrets
   - Never exposed in API responses

3. **API Key** - Required for authentication
   - Project-specific API keys
   - Validated via `@validate_api_token` decorator

## Troubleshooting

### Port-forward Issues
```bash
# Verify port-forward is active
netstat -an | grep 15432

# Test connectivity manually
psql -h localhost -p 15432 -U postgres -d amt_staging
```

### Clone Taking Too Long
- Large databases may take considerable time
- Monitor operations-manager logs for progress
- Consider cloning during off-peak hours

### Permission Errors
- Ensure source user has SELECT permissions on schema
- Ensure operations-manager has admin credentials for target
- Check target deployment has PostgreSQL service enabled

## Implementation Details

The feature is implemented in two layers:

### Database Manager Layer
- `database_manager.py:_validate_external_source()` - Single-responsibility validation
- `database_manager.py:clone_database_from_external_source()` - Orchestrator

### API Layer
- `router.py:CloneDatabaseFromExternalRequest` - Pydantic request model
- `router.py:clone_database_from_external()` - FastAPI endpoint

### PostgreSQL Connector Layer
- `postgres.py:clone_schema_from_external()` - pg_dump streaming implementation (already existed)

## Future Enhancements

Potential improvements:
- CLI tool wrapper around API
- Support for MinIO cloning alongside database
- Scheduled/automated migrations
- Pre-migration data validation
- Progress streaming via WebSocket
