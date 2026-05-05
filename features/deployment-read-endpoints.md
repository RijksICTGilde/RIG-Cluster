# Deployment Read Endpoints

Read-only V2 API endpoints that return the current state of deployments in a project: components, images, computed public URLs, and live reconciliation status.

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
| `components` | List of component references, each with `reference` and `image` |
| `urls` | Computed public URLs keyed by component name (only for components with `publish-on-web`) |
| `status` | Live reconciliation status (see below) — `null` if not available; check `status_reason` |
| `status_reason` | Why `status` is null. `Pending` (cluster has no Application yet), `Unavailable` (status fetch failed in the list endpoint). `null` when `status` is set. |

`status` sub-object:

| Field | Description |
|---|---|
| `sync_status` | Sync status (`Synced`, `OutOfSync`, ...) |
| `health_status` | Health status (`Healthy`, `Degraded`, `Progressing`, ...) |
| `revision` | Full git SHA last reconciled — `null` if never reconciled |
| `last_synced_at` | ISO timestamp of the last reconciliation against git — `null` if never synced |
| `errors` | List of cluster-side error entries — empty when `health_status` is `Healthy` |

Each `errors` entry has:

| Field | Description |
|---|---|
| `resource` | `Kind/name` (e.g. `Pod/frontend-abc`) for cluster resources, `Event/<obj>` for namespace events, or a condition type name |
| `message` | The raw cluster message — for automation, regex matching, correlation |
| `category` | Programmatic category for filtering / grouping / colorizing. One of: `ImagePull`, `CrashLoop`, `OutOfMemory`, `HealthCheck`, `SyncFailed`, `ComparisonError`, `Unknown` |
| `explanation` | Human-friendly description of the category and what to do next; `null` for `Unknown` |
| `timestamp` | ISO timestamp if known |

For component logs, use the existing `GET /api/logs/{project_name}` (HTTP) or `WS /api/logs/stream/{project_name}` (WebSocket) endpoints. Logs are intentionally not embedded in this response: they don't belong to "status" semantically, and the existing log endpoints already serve that need.

The list view returns the same per-deployment shape as the single view (it is a filtered slice, not a reduced projection).

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
      "image": "ghcr.io/org/frontend:1.2.3"
    },
    {
      "reference": "api",
      "image": "ghcr.io/org/api:2.0.0"
    }
  ],
  "urls": {
    "frontend": "https://production-my-project.rijksapps.nl",
    "api": "https://api-production-my-project.rijksapps.nl"
  },
  "status": {
    "sync_status": "Synced",
    "health_status": "Healthy",
    "revision": "abc123def456789",
    "last_synced_at": "2026-04-22T12:00:00Z",
    "errors": []
  }
}
```

### Example response (deployment not running correctly)

```json
{
  "name": "production",
  "...": "...",
  "status": {
    "sync_status": "OutOfSync",
    "health_status": "Degraded",
    "revision": "deadbeefcafe",
    "last_synced_at": "2026-04-22T11:00:00Z",
    "errors": [
      {
        "resource": "Pod/frontend-abc-7c9d8f-xxxxx",
        "message": "Back-off pulling image ghcr.io/org/frontend:v2 — manifest unknown",
        "category": "ImagePull",
        "explanation": "The container image could not be pulled. Check the image name, tag, and registry credentials.",
        "timestamp": "2026-04-22T10:55:00Z"
      },
      {
        "resource": "Event/frontend-abc-7c9d8f-xxxxx",
        "message": "[Failed] Failed to pull image ...",
        "category": "ImagePull",
        "explanation": "The container image could not be pulled. Check the image name, tag, and registry credentials."
      }
    ]
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

## How status is sourced

The `status` sub-object reports what the cluster has actually reconciled, queried per request. Today this is sourced from the ArgoCD `Application` for each deployment; the schema is intentionally backend-neutral so the source can change without breaking callers.

`last_synced_at` is the timestamp of the last reconciliation against git — combined with `revision`, it tells callers "we are running commit `<revision>` as of `<last_synced_at>`," which is what most "where is my deploy?" debugging needs.

When `health_status` is anything other than `Healthy`, the response also includes `errors[]`: aggregated from the ArgoCD `Application` (resources, conditions, sync result), the resource tree (Pod / ReplicaSet messages — where `ImagePullBackOff` / `CrashLoopBackOff` etc. surface), and recent namespace events. Healthy deployments skip this fetch to keep responses small and fast.

The diagnostics gathering logic is shared with the web UI via `opi/services/deployment_diagnostics.py`, so both surfaces report the same view of "what's broken."

### Failure modes

The two endpoints have different strictness, deliberately:

- **Single-deployment endpoint** (`GET /deployments/{name}`): strict. Any failure to fetch status — backend unreachable, login failed, or this deployment's fetch raised — returns **`503 Service Unavailable`**. There's only one resource being asked about; partial truth is misleading.

- **List endpoint** (`GET /deployments`): lenient on per-deployment failures. The whole-backend-down case (login failed, can't reach Argo at all) still returns **`503`**, but if the backend is reachable and one deployment's fetch raises, that deployment is returned with `status: null` and `status_reason: "Unavailable"`. The other deployments are returned normally. This keeps a CLI's `list` working through partial outages.

If the backend is reachable but does not yet know about a deployment (Argo has no `Application` for it yet), `status` is `null` with `status_reason: "Pending"`. This is normal during the gap between project file commit and reconciliation.

## Why this exists

Previously, clients (e.g. zad-cli) had to reconstruct deployment details by combining `/api/logs/{project}` (for components and namespace) with `/api/tasks?project_name=...` (for URLs and images from the most recent completed task). This was slow, fragile, and broke when task history was pruned.

These endpoints return authoritative state directly from the project file (for static fields) and the cluster (for live status), independent of task history.
