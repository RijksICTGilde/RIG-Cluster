# Auto Resource Tuning

**Status**: Implemented (on-demand), Planned (scheduled)
**Created**: 2026-02-10
**Updated**: 2026-03-26

## Overview

The auto resource tuning system analyzes actual memory usage via Prometheus and adjusts Kubernetes resource limits and requests in project YAML files. Changes flow through git, so ArgoCD deploys them like any other configuration change.

Currently available as an on-demand API endpoint (`POST /api/resources/{project_name}/tune`). A scheduled background analyzer is planned as a future enhancement.

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

The analyzer computes memory limits and requests separately:

- **Limit** = `max_observed * (1 + buffer%)` - protects against peak usage
- **Request** = `avg_observed * (1 + buffer%)` - reflects typical usage for scheduling
- Apps using >= 100Mi get an additional flat 25Mi headroom on top of the percentage buffer

Both values are subject to:
- A configurable cluster minimum (default 25Mi)
- Request is capped to never exceed limit
- Changes below the threshold percentage (default 20%) are skipped (no noise commits)

### Limit/Request Collapse

When the recommended limit and request are within **10% of each other**, they are collapsed to the same value. A tiny gap between limit and request adds no scheduling benefit - it just creates noise in the YAML.

Example: if the algorithm computes limit=100Mi and request=95Mi (5% gap), both are set to 100Mi.

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
      "previous_requests_memory": "128Mi",
      "new_requests_memory": "100Mi",
      "max_observed_memory_mb": "100",
      "avg_observed_memory_mb": "80",
      "has_oom_kills": "False",
      "reason": "Limit: max 100Mi + 25% + 25Mi headroom = 150Mi. Request: avg 80Mi + 25% = 100Mi"
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
| `RESOURCE_TUNING_MEMORY_BUFFER_PERCENT` | `25` | Headroom above observed usage |
| `RESOURCE_TUNING_THRESHOLD_PERCENT` | `20` | Minimum change % to trigger update |

Cluster-specific minimum memory is configured via `get_min_memory_limit_mi()` (default 25Mi).

## Key Files

| File | Purpose |
|------|---------|
| `opi/services/resource_analyzer.py` | Pure computation: observed usage → recommendation |
| `opi/api/resource_router.py` | API endpoint: orchestrates metrics queries, applies changes, commits |
| `opi/handlers/project_file_handler.py` | YAML manipulation: read/write component resources |
| `opi/connectors/prometheus.py` | Direct Prometheus connector |
| `opi/connectors/grafana_prometheus.py` | Grafana-proxied Prometheus connector |

## Constraints

### Tenant Cluster

The production environment (ODCN) is a tenant cluster - no cluster-admin permissions. This rules out VPA (requires CRDs + cluster-scoped controllers). The metrics-driven GitOps approach works entirely within namespace-scoped permissions.

### GitOps Compatibility

All changes flow through git commits. Direct mutation of pod specs (as VPA Auto mode does) would conflict with ArgoCD sync. This system updates project YAML files, which ArgoCD then deploys.

## Safety Mechanisms

| Mechanism | Purpose |
|-----------|---------|
| **Cluster minimum** | Never set memory below 25Mi |
| **Change threshold (20%)** | Only commit when the difference is meaningful |
| **OOM kill priority** | Always increase memory when OOM kills are present |
| **2x propagation cap** | Base component not inflated by outlier deployments |
| **10% collapse** | Eliminates noise from near-identical limit/request values |
| **Git-based changes** | All changes are auditable, reviewable, and reversible |
| **Deployment-level scoping** | Tuning writes to deployment overrides, not shared definitions (except request propagation) |
| **Fresh git reads** | Tuning reads the latest YAML from git before modifying, preventing stale cache data from overwriting concurrent changes |
| **Legacy key migration** | Flat `cpu`/`memory` resource keys are migrated into nested `requests`/`limits` before removal, preventing silent data loss |

## Future: Scheduled Auto-Tuning

The current on-demand endpoint can be extended with a background scheduler that runs periodically across all projects. See the planned configuration for per-project opt-in (`auto-scale-resources: true`) and deployment-level overrides.

## Related

- `features/futures/sidecar-resource-tuning.md` - extends tuning to sidecar containers
- `features/futures/configurable-deployment-resources.md` - prerequisite for resource values in YAML
