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
| `status` | Single enum covering all states (see values below). Always present. |
| `sync_revision` | Git revision (full SHA) the cluster last reconciled — `null` if never reconciled |
| `last_synced_at` | ISO timestamp of the last reconciliation **attempt**, regardless of outcome. Combine with `status` to know whether that attempt succeeded — for a `Degraded` deployment this can be the time of a failed sync. `null` if never synced |
| `errors` | List of cluster-side error entries — empty when `status` is `Healthy`, `Pending`, `Unavailable`, or `Unknown` |
| `approvals` | Goedkeuringen die deze deployment nog niet heeft — leeg wanneer alles is goedgekeurd. Zie hieronder |

`status` values:

| Value | Meaning |
|---|---|
| `Healthy` | Synced and Healthy — running the desired state, all probes passing |
| `Disabled` | Every component of this deployment is switched off in the project file (`disabled: true` → `replicas: 0`). Not an ArgoCD verdict; see below |
| `Degraded` | One or more resources unhealthy (worst-of-both wins over sync state) |
| `Progressing` | Mid-rollout, not yet stabilized |
| `OutOfSync` | Cluster is running, but drifted from the desired state in git |
| `Suspended` | Reconciliation paused |
| `Missing` | Resources expected but not found |
| `Pending` | The cluster has no `Application` for this deployment yet — normal in the gap between commit and reconciliation |
| `Unavailable` | The status fetch failed for this specific deployment (only emitted by the list endpoint) |
| `Unknown` | The backend reported `Unknown` or returned a value we don't recognize |

Each `errors` entry has:

| Field | Description |
|---|---|
| `resource` | `Kind/name` (e.g. `Pod/frontend-abc`) for cluster resources, `Event/<obj>` for namespace events, or a condition type name |
| `message` | The raw cluster message — for automation, regex matching, correlation |
| `category` | Programmatic category for filtering / grouping / colorizing. One of: `ImagePull`, `CrashLoop`, `OutOfMemory`, `HealthCheck`, `SyncFailed`, `ComparisonError`, `Unknown` |
| `explanation` | Human-friendly description of the category and what to do next; `null` for `Unknown` |
| `timestamp` | ISO timestamp if known |

Each `approvals` entry has:

| Field | Description |
|---|---|
| `service` | De dienst die de goedkeuring bezit, zoals in de servicecatalogus (vandaag altijd `publish-on-web`) |
| `type` | Wat er goedgekeurd moet worden binnen die dienst: `domain` of `subdomain` |
| `label` | Hoe de portal dit soort goedkeuring noemt (`Domein`, `Subdomein`) |
| `subject` | Wat er is aangevraagd, bijvoorbeeld `mijn-app.nl` |
| `status` | `requested` (wacht op een beheerder), `denied` (afgewezen) of `none` (nog niets aangevraagd). `approved` komt hier niet voor — wat is goedgekeurd staat niet in deze lijst |
| `text` | Wat dit betekent voor deze deployment, inclusief het gevolg, in gewone taal |
| `by` / `date` / `message` | Wie het laatste oordeel gaf, wanneer, en met welke toelichting |

Dit veld staat er om dezelfde reden als `pending_rollout`: het antwoord beschrijft anders
een deployment alsof die op het gevraagde adres draait. Een niet-goedgekeurd domein
blokkeert de uitrol niet — de deployment publiceert dan op het standaard clusteradres, en
dat adres staat dus ook gewoon in `urls`. Zonder `approvals` is er niets dat zegt waarom
daar een ander adres staat dan gevraagd. Zie `features/domain-configuration.md`.

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
  "status": "Healthy",
  "sync_revision": "abc123def456789",
  "last_synced_at": "2026-04-22T12:00:00Z",
  "errors": []
}
```

### Example response (deployment not running correctly)

```json
{
  "name": "production",
  "...": "...",
  "status": "Degraded",
  "sync_revision": "deadbeefcafe",
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
```

### Example response (not yet known to the cluster)

```json
{
  "name": "production",
  "...": "...",
  "status": "Pending",
  "sync_revision": null,
  "last_synced_at": null,
  "errors": []
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

`status` reports what the cluster has actually reconciled, queried per request. Today this is sourced from the ArgoCD `Application` for each deployment, but the schema is intentionally backend-neutral so the source can change without breaking callers.

ArgoCD exposes two orthogonal dimensions — `sync.status` (Synced/OutOfSync) and `health.status` (Healthy/Degraded/Progressing/Suspended/Missing) — which we collapse into a single value:

```
Degraded / Suspended / Missing  →  use that (worst-of-both wins)
OutOfSync                        →  "OutOfSync"  (cluster is running, but drifted from git)
Progressing                      →  "Progressing"
Healthy                          →  "Healthy"  (or "Disabled", see below)
otherwise                        →  "Unknown"
```

`Disabled` is the one value that does not come from ArgoCD. A deployment whose components are all switched off renders `replicas: 0`, and ArgoCD calls zero replicas Healthy because nothing is failing — so the intent recorded in the project file replaces that verdict, and only that one. `Degraded`, `OutOfSync`, `Progressing`, `Missing`, `Suspended` and `Unknown` are things the cluster really observed and are never masked: switching a component off must not be a way to make a failure disappear.

**Behaviour change (RC-31).** A client filtering on `status == "Healthy"` no longer gets switched-off deployments back. That is the intent — they were never healthy, only unfailing — but such a client needs to add `Disabled` where it means "not broken".

`last_synced_at` is the timestamp of the last reconciliation **attempt** — succeeded or failed. Combined with `sync_revision`, it tells callers "we are running commit `<sync_revision>` as of `<last_synced_at>`" only when `status` is `Healthy`. For a `Degraded` deployment, `last_synced_at` may be the time of a failed sync attempt, not a healthy one. (See follow-up issue for splitting into `last_attempt_at` + `last_success_at`.)

When `status` indicates a problem (`Degraded`, `OutOfSync`, `Suspended`, `Missing`), the response also includes `errors[]`: aggregated from the ArgoCD `Application` (resources, conditions, sync result), the resource tree (Pod / ReplicaSet messages — where `ImagePullBackOff` / `CrashLoopBackOff` etc. surface), and recent namespace events. Healthy / Pending / Unavailable / Unknown deployments skip this fetch to keep responses small and fast.

The diagnostics gathering logic is shared with the web UI via `opi/services/deployment_diagnostics.py`, so both surfaces report the same view of "what's broken."

### Failure modes

The two endpoints have different strictness, deliberately:

- **Single-deployment endpoint** (`GET /deployments/{name}`): strict. Any failure to fetch status — backend unreachable, login failed, or this deployment's fetch raised — returns **`503 Service Unavailable`**. There's only one resource being asked about; partial truth is misleading.

- **List endpoint** (`GET /deployments`): lenient on per-deployment failures. The whole-backend-down case (login failed, can't reach Argo at all) still returns **`503`**, but if the backend is reachable and one deployment's fetch raises, that deployment is returned with `status: "Unavailable"`. The other deployments are returned normally. This keeps a CLI's `list` working through partial outages.

If the backend is reachable but does not yet know about a deployment (Argo has no `Application` for it yet), `status` is `"Pending"`. This is normal during the gap between project file commit and reconciliation.

## Why this exists

Previously, clients (e.g. zad-cli) had to reconstruct deployment details by combining `/api/logs/{project}` (for components and namespace) with `/api/tasks?project_name=...` (for URLs and images from the most recent completed task). This was slow, fragile, and broke when task history was pruned.

These endpoints return authoritative state directly from the project file (for static fields) and the cluster (for live status), independent of task history.
