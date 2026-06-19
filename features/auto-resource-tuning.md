# Auto Resource Tuning

**Status**: Implemented (on-demand + scheduled, memory + CPU)
**Created**: 2026-02-10
**Updated**: 2026-06-19

## Overview

The auto resource tuning system computes recommended Kubernetes resource requests and limits and writes them into project YAML files. Changes flow through git, so ArgoCD deploys them like any other configuration change.

Recommendations come from one of two sources:

- **VPA recommender** (on clusters where `supports_vpa` is true, e.g. `odcn-production`): an Off-mode `VerticalPodAutoscaler` is generated per component. The platform's recommender publishes CPU **and** memory recommendations to its `.status`, which the tuner reads. This is the preferred source and is the only one that tunes CPU.
- **Prometheus** (fallback on non-VPA clusters, and for components whose VPA has no recommendation yet): the historical memory-usage window described below. Memory only.

Available both as an on-demand API endpoint (`POST /api/resources/{project_name}/tune`) and as a scheduled background process that tunes the whole estate (see Scheduled Auto-Tuning).

## How It Works

```
Prometheus (memory metrics + OOM kills)
        |
        | PromQL queries
        v
Resource Analyzer (compute_memory_recommendation)
        |
        | Compare observed vs declared, apply buffer + thresholds
        v
Resource Router (tune endpoint)
        |
        | Update deployment overrides + base component definition
        v
Project YAML (git commit) --> ArgoCD (deploy)
```

### Tuning Flow

1. For each component in the target deployment(s):
   - Query `max_over_time(container_memory_working_set_bytes{...})` for peak memory
   - Query `avg_over_time(container_memory_working_set_bytes{...})` for average memory
   - Query `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}` for OOM kills
2. Compute recommendations using the resource analyzer
3. Write updated values to deployment-level overrides in the project YAML
4. Optionally propagate the memory request to the base component definition (see below)
5. Commit to git and trigger ArgoCD reprocessing

### Recommendation Algorithm

Tuning drives the **request** (the reserved memory that actually counts against
cluster capacity); the limit is treated as a passive ceiling:

- **Request** = `max_observed * (1 + buffer%)` - sized to the peak ("the highest
  we measured") so the reservation always covers real usage
- Apps using >= 100Mi get an additional flat 25Mi headroom on top of the percentage buffer
- **Limit**:
  - If the current limit **equals** the current request (the untouched default),
    the limit follows the request down/up so the two stay equal.
  - If the current limit **already differs** from the request (someone set it
    deliberately, or a prior OOM raised it), the limit is **left untouched** -
    only the request is tuned.

Both values are subject to:
- A configurable cluster minimum (default 25Mi)
- Request is capped to never exceed limit
- Changes below the threshold percentage (default 20%, measured on the request)
  are skipped (no noise commits)

The "Geheugen kan worden verminderd" portal card and its saving figure are
expressed as the **request** reduction, since requests are what free scheduling
capacity.

### OOM Kill Handling

OOM-killed containers produce misleading usage data (the pod was killed before reaching its true peak). When OOM kills are detected:

- The limit is set to at least **1.5x the current limit**, regardless of observed usage
- If the pod was OOM-killed on startup with zero Prometheus metrics, the current YAML values are used as a baseline for the 1.5x calculation
- OOM kills bypass the change threshold - any OOM kill triggers an update

### Base Component Propagation

Resource tuning writes to **deployment-level overrides** (e.g., production gets its own limits). However, when a new deployment is created, it inherits the **base component definition's** defaults - which may be too low.

After updating a deployment's resources, the tuning system also updates the base component's memory request, with two guards:

1. **Only increase, never decrease** - if the base is already higher (set manually for a reason), it stays
2. **Only when the ratio is <= 2x** - if the new request is more than double the current base, it's likely a deployment-specific need (e.g., production vs test) and shouldn't inflate the shared default

Example:
- Base component has `requests.memory: 64Mi`
- Production tuning recommends `requests.memory: 100Mi` (ratio 1.56x) → base updated to 100Mi
- Production tuning recommends `requests.memory: 175Mi` (ratio 2.73x) → base left at 64Mi

Only `requests.memory` is propagated - limits are deployment-specific by nature (production and staging may have very different limits).

## API

### Tune Resources

```
POST /api/resources/{project_name}/tune?deployment={deployment_name}
```

- `deployment` is optional - omit to tune all deployments in the project
- Requires API token authentication

Response:
```json
{
  "project": "my-project",
  "changes": [
    {
      "component": "api",
      "deployment": "production",
      "previous_limits_memory": "512Mi",
      "new_limits_memory": "150Mi",
      "previous_requests_memory": "512Mi",
      "new_requests_memory": "150Mi",
      "max_observed_memory_mb": "100",
      "avg_observed_memory_mb": "80",
      "has_oom_kills": "False",
      "reason": "Request: max 100Mi + 25% + 25Mi headroom = 150Mi. Limit kept equal at 150Mi"
    }
  ],
  "unchanged": [],
  "deployment_refresh_triggered": true
}
```

### Sanitize Deployment

```
POST /api/resources/{project_name}/sanitize?deployment={deployment_name}
```

Detects broken deployments (crash loops, missing images, OOM kills) and disables them by setting `disabled: true` in the project YAML.

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `RESOURCE_TUNING_WINDOW_HOURS` | `24` | Prometheus lookback window |
| `RESOURCE_TUNING_MEMORY_BUFFER_PERCENT` | `25` | Headroom above observed/VPA target |
| `RESOURCE_TUNING_INCREASE_THRESHOLD` | `10` | Apply an increase when the request grows by ≥ this % |
| `RESOURCE_TUNING_DECREASE_THRESHOLD` | `30` | Apply a decrease only when the request shrinks by ≥ this % |
| `RESOURCE_TUNING_SCHEDULER_ENABLED` | `true` | Run the scheduled fleet-wide tuner |
| `RESOURCE_TUNING_SCHEDULER_INTERVAL` | `21600` | Seconds between scheduler ticks (6h) |
| `RESOURCE_TUNING_COOLDOWN_DAYS` | `7` | Don't re-tune a project within this many days |
| `RESOURCE_TUNING_MAX_PROJECTS_PER_TICK` | `5` | Cap projects tuned per tick (anti-storm) |

Cluster-specific bounds live in `cluster_config.py`: memory via `get_min_memory_limit_mi()` / `get_max_memory_limit_mi()` / `get_max_memory_request_mi()`, CPU via `get_min_cpu_m()` (25m) / `get_max_cpu_request_m()` (250m) / `get_max_cpu_limit_m()` (4000m), and the `supports_vpa` capability flag.

### Asymmetric Deviation Gate

To avoid a storm of tiny commits, changes are gated by direction: an **increase** is applied when the request grows by at least `RESOURCE_TUNING_INCREASE_THRESHOLD` (react promptly - reliability), while a **decrease** must clear the larger `RESOURCE_TUNING_DECREASE_THRESHOLD` (conservative - reclaiming a little memory/CPU isn't worth the churn). OOM-driven increases bypass the gate entirely.

### Opt-Out

Auto-tuning is **on by default**. A component opts out with `auto-tune-resources: false`, settable at the component-definition level or overridden per deployment-component (deployment override wins).

## Key Files

| File | Purpose |
|------|---------|
| `opi/services/resource_analyzer.py` | Pure computation: usage/VPA target → memory & CPU recommendation, asymmetric gate |
| `opi/services/resource_tuning_service.py` | Orchestrates analysis (VPA or Prometheus), applies changes, commits |
| `opi/core/resource_tuning_scheduler.py` | Scheduled fleet-wide tuner (cooldown + per-tick cap) |
| `opi/connectors/vpa.py` | Parse VPA `.status.recommendation` (CPU→m, memory→Mi) |
| `opi/api/resource_router.py` | On-demand API endpoint |
| `opi/handlers/project_file_handler.py` | YAML manipulation: read/write resources, opt-out flag |
| `manifests/vpa.yaml.jinja` | Off-mode VPA generated per component on VPA-capable clusters |
| `opi/connectors/prometheus.py` / `grafana_prometheus.py` | Prometheus connectors (fallback source) |

## Constraints

### Tenant Cluster

The production environment (ODCN) is a tenant cluster - no cluster-admin permissions. The platform already runs the OpenShift VPA Operator, so creating namespace-scoped `VerticalPodAutoscaler` objects (in `updateMode: "Off"`) is fully within tenant permissions. Off-mode VPAs are advice-only: they never evict or mutate pods, so they create no ArgoCD drift. (Only VPA *Auto/Recreate* mode - which mutates pod specs - is ruled out, because it would conflict with GitOps.)

### GitOps Compatibility

All changes flow through git commits. The tuner reads recommendations (from the VPA `.status` or Prometheus) and writes the resulting requests/limits into project YAML files, which ArgoCD then deploys. Pod specs are never mutated directly.

## Safety Mechanisms

| Mechanism | Purpose |
|-----------|---------|
| **Cluster minimum** | Never set memory below 25Mi |
| **Change threshold (20%)** | Only commit when the difference is meaningful |
| **OOM kill priority** | Always increase memory when OOM kills are present |
| **2x propagation cap** | Base component not inflated by outlier deployments |
| **Frozen limit** | A limit already set to differ from the request is left untouched - only the request is tuned |
| **Git-based changes** | All changes are auditable, reviewable, and reversible |
| **Deployment-level scoping** | Tuning writes to deployment overrides, not shared definitions (except request propagation) |
| **Fresh git reads** | Tuning reads the latest YAML from git before modifying, preventing stale cache data from overwriting concurrent changes |
| **Legacy key migration** | Flat `cpu`/`memory` resource keys are migrated into nested `requests`/`limits` before removal, preventing silent data loss |

## Scheduled Auto-Tuning

`ResourceTuningScheduler` (`opi/core/resource_tuning_scheduler.py`, started from the server lifespan, modelled on `BackupScheduler`) runs every `RESOURCE_TUNING_SCHEDULER_INTERVAL` and tunes the whole estate:

1. Enumerate all projects with a deployment on this OPI's cluster.
2. Select those past cooldown - the most recent `auto-tune`/`oom-watcher` resource-history timestamp is older than `RESOURCE_TUNING_COOLDOWN_DAYS` (or never tuned). No extra state store: the cooldown is read from the existing resource history.
3. Tune the oldest-first batch, capped at `RESOURCE_TUNING_MAX_PROJECTS_PER_TICK`, by calling `tune_deployment_resources(project)`.

The per-project cooldown plus the per-tick cap bound how many commits/ArgoCD reprocessings happen per tick. Because reductions are conservative (decrease threshold + cooldown + OOM floor) and increases react to real pressure, enabling writes fleet-wide is safe even while freshly-created VPAs are still warming up their ~8-day histogram. Urgent memory increases in the meantime are handled by the reactive OOM watcher, which is independent of this cooldown.

## Related

- `features/futures/sidecar-resource-tuning.md` - extends tuning to sidecar containers
- `features/futures/configurable-deployment-resources.md` - prerequisite for resource values in YAML
