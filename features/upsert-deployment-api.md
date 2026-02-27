# Upsert Deployment API

The upsert deployment API (`POST /api/projects/{project_name}/:upsert-deployment`) creates or updates deployments within an existing project. It is the primary mechanism used by CI/CD pipelines to deploy preview environments and update running deployments.

## How It Works

The API has two modes depending on whether the deployment already exists:

### Create (deployment does not exist)

When the deployment name is new, a deployment entry is added to the project YAML. The caller provides:

- `deploymentName` - name for the new deployment
- `components` - list of `{reference, image}` pairs
- `cloneFrom` (optional) - source deployment to clone configuration from

**Only the provided components are included.** If the source deployment has 3 components but the API call only sends 1, the new deployment will only contain that 1 component. This is by design: CI/CD pipelines typically only build one component per PR.

### Update (deployment already exists)

When the deployment exists, component images are updated in place:

- **Existing component**: image is updated to the new value
- **New component**: if a component reference is sent that doesn't exist in the deployment yet, it is **added** to the deployment
- Components not mentioned in the request are left unchanged

This means you can incrementally add components to a deployment by sending updates for them.

## Clone-From Behavior

When `cloneFrom` is specified on create, the following properties are copied from the source deployment:

| Copied | Not Copied |
|---|---|
| `cluster` | `name` |
| `namespace` | `components` |
| `repository` | `subdomain` |
| `configuration` | `base-domain` |
| `services` | `domain-mode` |
| (other custom fields) | `issuer` |

### Why custom domain fields are excluded

The `base-domain`, `domain-mode`, and `issuer` fields configure a custom domain with DNS and TLS certificates. Cloned deployments (typically PR previews) should use the default cluster domain instead of inheriting the source's custom domain setup. This avoids:

- DNS conflicts between source and clone
- Unnecessary Let's Encrypt certificate issuance for temporary deployments
- Coupling preview deployments to production domain configuration

### Subdomain heuristic

While `subdomain` is excluded from the general clone, there is one exception: if the source deployment's `subdomain` matches its `name` (e.g., deployment `regelrecht` with `subdomain: regelrecht`), the clone automatically gets `subdomain` set to the new deployment name. This preserves the shared-hostname pattern where components use paths to differentiate (e.g., `pr129.cluster.domain/editor` and `pr129.cluster.domain/upload`) instead of each getting a separate hostname.

## API Request

```bash
curl -X POST "https://operations-manager/api/projects/my-project/:upsert-deployment" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <project-api-key>" \
  -d '{
    "deploymentName": "pr-123",
    "components": [
      {"reference": "frontend", "image": "ghcr.io/org/app:pr-123"}
    ],
    "cloneFrom": "production",
    "forceClone": false
  }'
```

### Parameters

| Field | Type | Required | Description |
|---|---|---|---|
| `deploymentName` | string | yes | Name of the deployment (lowercase, hyphens allowed) |
| `components` | list | yes | Components with their image references |
| `cloneFrom` | string | null | Source deployment to clone config from (only on create, or if `forceClone` is true) |
| `forceClone` | boolean | false | Re-clone even if the deployment already exists |

### Response

- `201` - deployment was created
- `200` - deployment was updated
- Response includes deployment URLs and processing status

## Examples

### PR preview with single component

A CI pipeline building only the frontend:

```json
{
  "deploymentName": "pr-42",
  "components": [
    {"reference": "frontend", "image": "ghcr.io/org/frontend:pr-42"}
  ],
  "cloneFrom": "production"
}
```

Result: deployment `pr-42` is created with only `frontend`. Other components from `production` (e.g., `backend`, `worker`) are **not** included.

### Adding a missing component later

To add the backend component to the existing `pr-42` deployment:

```json
{
  "deploymentName": "pr-42",
  "components": [
    {"reference": "backend", "image": "ghcr.io/org/backend:pr-42"}
  ]
}
```

Result: `backend` is added to the existing deployment. The `frontend` component remains unchanged.

### Updating an image

```json
{
  "deploymentName": "pr-42",
  "components": [
    {"reference": "frontend", "image": "ghcr.io/org/frontend:pr-42-v2"}
  ]
}
```

Result: only the `frontend` image is updated. All other components and configuration remain unchanged.
