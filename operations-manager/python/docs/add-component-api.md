# Add Component to Existing Project API

## Context

ZAD already supports multi-component projects in its YAML model, but there's no API endpoint to add a new component to an **existing** project. Currently, the only way is to manually edit the project YAML in Git. The immediate use case is adding a long-running background worker (deployment type) to a project that currently has a single web component.

## Design Decisions

- **API-only** — no UI changes (UI is undergoing separate changes)
- **Component type:** deployment (long-running worker), no ports/ingress needed
- **Services per component:** each component declares its own `uses-services`. Project-level `services:` defines what infrastructure is *available*, but each component's `uses-services:` determines what it actually gets (secrets/env vars). A background worker typically needs `postgresql-database` but NOT `publish-on-web` or `keycloak`.
- **Deployment targeting:** caller must explicitly specify which deployments to add the component to (no auto-add)

## How services work

- **Project-level `services:`** — infrastructure provisioning (PostgreSQL cluster, MinIO bucket, Keycloak client). Already exists, unchanged.
- **Component-level `uses-services:`** — per-component list controlling which secrets/env vars are injected. Only services listed here get their secrets mounted.
- `publish-on-web` is a `uses-services` entry — controls ingress generation. A background worker omits this.
- The API caller specifies which services the new component needs.

## API

### `POST /api/projects/{project_name}/components`

Add a new component definition to an existing project and reference it in specified deployments.

**Headers:**
- `X-API-Key`: The API key for the project (required)

**Request body:**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | yes | — | Component name (K8s-compliant) |
| `image` | string | yes | — | Container image URL |
| `port` | int | no | `null` | Inbound port (omit for background workers) |
| `path` | string | no | `"/"` | Ingress path (only relevant with publish-on-web) |
| `services` | list[string] | no | `null` | Component's uses-services (e.g. `["postgresql-database"]`) |
| `cpu_limit` | string | no | `null` | CPU limit, e.g. `"500m"` |
| `memory_limit` | string | no | `null` | Memory limit, e.g. `"512Mi"` |
| `env_vars` | string | no | `null` | User env vars in KEY=value format (will be AGE-encrypted) |
| `deployment_names` | list[string] | yes | — | Deployments to add this component to (must exist) |

**Example — add a background worker:**

```bash
curl -X POST "http://localhost:9595/api/projects/my-project/components" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{
    "name": "worker",
    "image": "ghcr.io/myorg/worker:latest",
    "services": ["postgresql-database"],
    "deployment_names": ["main"]
  }'
```

**Success response (201):**

```json
{
  "status": "success",
  "message": "Component 'worker' added successfully",
  "component": {
    "name": "worker",
    "type": "deployment",
    "ports": {"inbound": [], "outbound": [80, 443]},
    "path": "/",
    "uses-services": ["postgresql-database"],
    "uses-components": []
  },
  "deployments_updated": ["main"],
  "urls": {},
  "processing": {"status": "completed"}
}
```

**Error responses:**

| Status | Error type | Description |
|--------|-----------|-------------|
| 400 | `invalid_deployments` | One or more deployment names not found |
| 401 | — | Missing or invalid API key |
| 409 | `duplicate_component` | Component name already exists in project |
| 422 | — | Missing required fields (Pydantic validation) |
| 500 | `internal_error` | Unexpected server error |

## Files Changed

- `opi/api/router.py` — `AddComponentRequest` model + `POST /api/projects/{project_name}/components` endpoint
- `opi/manager/project_manager.py` — `add_component()` method
- `tests/integration/test_project_api.py` — `TestAddComponentEndpoint` test class
