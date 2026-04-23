# Deployment Read Endpoints

Read-only V2 API endpoints that return the current state of deployments in a project, including components, images, and computed public URLs.

## Endpoints

### List deployments

```
GET /api/v2/projects/{project_name}/deployments
```

Returns all deployments in the project that target the current cluster.

### Get single deployment

```
GET /api/v2/projects/{project_name}/deployments/{deployment_name}
```

Returns a single deployment by name. Returns 404 if the deployment does not exist on the current cluster.

## Authentication

Both endpoints require the project API key via `X-API-Key` header (same as all V2 endpoints).

## Response shape

Each deployment includes:

| Field | Description |
|---|---|
| `name` | Deployment name |
| `project` | Project name |
| `cluster` | Target cluster |
| `namespace` | Kubernetes namespace |
| `subdomain` | DNS subdomain override (if set) |
| `components` | List of component references with `reference`, `image`, and `image_pull_policy` |
| `urls` | Computed public URLs keyed by component name (only for components with `publish-on-web`) |

### Example response (single deployment)

```json
{
  "name": "production",
  "project": "my-project",
  "cluster": "odcn-production",
  "namespace": "my-project",
  "subdomain": "production",
  "components": [
    {
      "reference": "frontend",
      "image": "ghcr.io/org/frontend:1.2.3",
      "image_pull_policy": "Always"
    },
    {
      "reference": "api",
      "image": "ghcr.io/org/api:2.0.0",
      "image_pull_policy": "Always"
    }
  ],
  "urls": {
    "frontend": "https://production-my-project.rijksapps.nl",
    "api": "https://api-production-my-project.rijksapps.nl"
  }
}
```

### Example response (list)

```json
{
  "project": "my-project",
  "cluster": "odcn-production",
  "deployments": [
    { "name": "production", "..." : "..." },
    { "name": "staging", "..." : "..." }
  ]
}
```

## How URLs are computed

URLs are computed from the project file using the same naming utilities as the web UI and the upsert deployment handler. Only components with the `publish-on-web` service get URLs. The computation uses the cluster's ingress postfix, TLS settings, and any deployment-level subdomain/base-domain/domain-format overrides.

## Why this exists

Previously, clients (e.g. zad-cli) had to reconstruct deployment details by combining `/api/logs/{project}` (for components and namespace) with `/api/tasks?project_name=...` (for URLs and images from the most recent completed task). This was slow, fragile, and broke when task history was pruned.

These endpoints return authoritative state directly from the project file, independent of task history.
