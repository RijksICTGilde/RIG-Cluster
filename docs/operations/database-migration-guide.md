# Database Migration Guide - External Source Cloning

This guide explains how to clone databases from external sources (like other clusters) into your RIG-Cluster deployments using the Operations Manager API.

For security requirements, see `features/externe-kloonbron-op-een-publiek-adres.md`.

## Overview

The external database cloning feature allows you to:
- Migrate databases from Digilab cluster to local Kind cluster
- Migrate databases from Digilab to ODCN production
- Clone any PostgreSQL database that is reachable on a public address, directly or through a Chisel tunnel

The source host that the operations-manager connects to must resolve to a public address. A
`kubectl port-forward` to `localhost` is therefore refused with a 422: use the `tunnel`
parameters below to reach a source inside another cluster.

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

Direct connection to a publicly reachable source:

```json
{
  "sourceHost": "db.voorbeeld.nl",
  "sourcePort": 5432,
  "sourceUsername": "postgres",
  "sourcePassword": "password",
  "sourceDatabase": "amt_staging",
  "sourceSchema": "amt_staging",
  "forceClone": true
}
```

Through a Chisel tunnel, for a source that is only reachable inside the source cluster:

```json
{
  "sourceUsername": "postgres",
  "sourcePassword": "password",
  "sourceDatabase": "amt_staging",
  "sourceSchema": "amt_staging",
  "forceClone": true,
  "tunnel": {
    "serverUrl": "https://chisel.source-cluster.example.com",
    "username": "admin",
    "password": "secret",
    "remoteHost": "amt-cluster-db-rw.tn-ai-validation-amt.svc.cluster.local",
    "remotePort": 5432
  }
}
```

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `sourceHost` | string | Yes, unless `tunnel` is used | External database host; must resolve to a public address |
| `sourcePort` | integer | Yes, unless `tunnel` is used | External database port (e.g., `5432`) |
| `sourceUsername` | string | Yes | Username for source database |
| `sourcePassword` | string | Yes | Password for source database |
| `sourceDatabase` | string | Yes | Source database name |
| `sourceSchema` | string | Yes | Source schema name to clone |
| `forceClone` | boolean | No | If true, drops existing target database (default: false) |
| `tunnel` | object | No | Chisel tunnel to a source inside another cluster |
| `tunnel.serverUrl` | string | Yes, inside `tunnel` | Chisel server URL (`https://` only); must resolve to a public address |
| `tunnel.username` | string | Yes, inside `tunnel` | Chisel authentication username |
| `tunnel.password` | string | Yes, inside `tunnel` | Chisel authentication password |
| `tunnel.remoteHost` | string | Yes, inside `tunnel` | Source hostname as seen by the Chisel server; not checked here |
| `tunnel.remotePort` | integer | Yes, inside `tunnel` | Source port as seen by the Chisel server |

### Response

**Success (200)**:
```json
{
  "status": "success",
  "message": "Database cloned successfully from db.voorbeeld.nl:5432 to amt/production",
  "project": "amt",
  "deployment": "production",
  "source": {
    "host": "db.voorbeeld.nl",
    "port": 5432,
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
  "message": "Database clone failed: Source validation failed: de bron is niet bereikbaar of weigerde de verbinding",
  "project": "amt",
  "deployment": "production",
  "source": {
    "host": "db.voorbeeld.nl",
    "port": 5432,
    "database": "amt_staging",
    "schema": "amt_staging"
  },
  "target": {},
  "operations": [
    {
      "type": "source_validation",
      "status": "failed",
      "error": "de bron is niet bereikbaar of weigerde de verbinding"
    }
  ],
  "errors": [
    "Source validation failed: de bron is niet bereikbaar of weigerde de verbinding"
  ]
}
```

An unreachable source, an unknown database and a wrong password all give this one message.
Which of the three it was, with host and port, is in the operations-manager log.

**Refused source (422)**: a source host or `tunnel.serverUrl` that does not resolve to a
public address is refused before any connection is made, with the message "de kloonbron moet
op een publiek adres staan".

## Usage Examples

### Example 1: Digilab to Local Kind (AMT Migration)

The source database lives inside the Digilab cluster, so it is reached through the Chisel
server of that cluster. The operations-manager sets the tunnel up itself; nothing needs to be
port-forwarded.

```bash
curl -X POST "http://localhost:9595/api/projects/amt/deployments/local/:clone-database-from-external" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-amt-api-key" \
  -d '{
    "sourceUsername": "postgres",
    "sourcePassword": "digilab-postgres-password",
    "sourceDatabase": "amt_production",
    "sourceSchema": "amt_production",
    "forceClone": true,
    "tunnel": {
      "serverUrl": "https://chisel.digilab.example.com",
      "username": "admin",
      "password": "chisel-password",
      "remoteHost": "amt-cluster-db-rw.tn-ai-validation-amt.svc.cluster.local",
      "remotePort": 5432
    }
  }'
```

### Example 2: Digilab to ODCN Production

Same request against the ODCN operations-manager. Port-forwarding the operations-manager
itself is fine: that is the API you call, not the clone source.

```bash
# Access the ODCN operations-manager
kubectl config use-context odcn-production
kubectl port-forward -n rig-prd-operations svc/operations-manager 9595:9595
```

```bash
curl -X POST "http://localhost:9595/api/projects/amt/deployments/production/:clone-database-from-external" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-amt-api-key" \
  -d '{
    "sourceUsername": "postgres",
    "sourcePassword": "digilab-postgres-password",
    "sourceDatabase": "amt_production",
    "sourceSchema": "amt_production",
    "forceClone": true,
    "tunnel": {
      "serverUrl": "https://chisel.digilab.example.com",
      "username": "admin",
      "password": "chisel-password",
      "remoteHost": "postgresql.amt-namespace.svc.cluster.local",
      "remotePort": 5432
    }
  }'
```

### Example 3: Clone Without Force (Safe Mode)

If you want to prevent accidental overwrites, omit `forceClone` or set it to `false`. This
example connects directly to a source that is reachable on a public address:

```bash
curl -X POST "http://localhost:9595/api/projects/amt/deployments/staging/:clone-database-from-external" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-amt-api-key" \
  -d '{
    "sourceHost": "db.voorbeeld.nl",
    "sourcePort": 5432,
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
0. **Public address** - Does the source host, or `tunnel.serverUrl`, resolve to a public address?
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

| Error | Cause | Solution |
|-------|-------|----------|
| `de kloonbron moet op een publiek adres staan` (422) | Source host or `tunnel.serverUrl` does not resolve to a public address | Use a public source address, or reach a private source through `tunnel` |
| `de bron is niet bereikbaar of weigerde de verbinding` | Unreachable source, unknown database, or wrong credentials | Check host, port and credentials; the operations-manager log says which of the three it was |
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

### Source Refused
```bash
# Does the source host, or the Chisel server, resolve to a public address?
dig +short db.voorbeeld.nl
dig +short chisel.digilab.example.com
```

A source on a private address is only accepted if the cluster allows that range; see
`features/externe-kloonbron-op-een-publiek-adres.md`.

### Source Unreachable
```bash
# Test connectivity manually from a machine that can reach the source
psql -h db.voorbeeld.nl -p 5432 -U postgres -d amt_staging
```

The response does not say whether the host, the database or the password was the problem.
The operations-manager log does:

```bash
kubectl logs -n rig-prd-operations deployment/operations-manager | grep -i "external"
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
