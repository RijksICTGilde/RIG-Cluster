# Auto Resource Tuning

**Status**: Implemented (on-demand + nightly, VPA-driven memory + CPU)
**Created**: 2026-02-10
**Updated**: 2026-06-23

## Overview

The auto resource tuning system computes recommended Kubernetes resource requests and limits and writes them into project YAML files. Changes flow through git, so ArgoCD deploys them like any other configuration change.

Recommendations come from one of two sources:

- **VPA recommender** (on clusters where `supports_vpa` is true, e.g. `odcn-production`): an Off-mode `VerticalPodAutoscaler` is generated per component. The platform's recommender publishes CPU **and** memory recommendations to its `.status`, which the tuner reads. This is the preferred source and is the only one that tunes CPU.
- **Prometheus** (fallback on non-VPA clusters, and for components whose VPA has no recommendation yet): the historical memory-usage window described below. Memory only.

Available both as an on-demand API endpoint (`POST /api/resources/{project_name}/tune`) and as a nightly background sweep that tunes the whole estate (see Nightly Auto-Tuning).

## How It Works

```
  VPA recommender .status              Prometheus (fallback + OOM detection)
  (memory + CPU: target/bounds)        (max_over_time memory, OOMKilled reason)
              \                          /
               v                        v
          Resource Analyzer  (memory + CPU recommendation)
                         |
          request+buffer -> deadband gate -> OOM floor -> min/max clamps
                         |
                         v
   Project YAML  --(git commit)-->  reprocess  -->  ArgoCD  -->  pod rollout
   (deployment override + base component definition)
```

### Tuning Flow

For each component in the target deployment(s):

1. Skip if opted out (`auto-tune-resources: false`) or the deployment is not Available.
2. Determine the recommendation source:
   - If the cluster has VPA and the component's `VerticalPodAutoscaler` has a populated `.status` → use its `target` (memory **and** CPU).
   - Otherwise fall back to Prometheus `max_over_time(container_memory_working_set_bytes{...})` over `RESOURCE_TUNING_WINDOW_HOURS` (memory only).
   - OOM kills are always read from Prometheus (`kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}`).
3. Compute the recommendation (analyzer), apply the deadband gate, OOM floor, and clamps.
4. Write changed values to the deployment-level override and propagate the request to the base component definition (see below).
5. Commit once per project, then reprocess so ArgoCD redeploys.

### Recommendation Algorithm (Memory)

Tuning drives the **request** (the reserved memory that counts against cluster
capacity); the limit is treated as a ceiling:

- **Request** = `recommendation * (1 + buffer%)`, where `recommendation` is the
  VPA `target` (VPA path) or the Prometheus peak `max_over_time` (fallback).
  Apps using >= 100Mi get an extra flat 25Mi headroom on top of the percentage buffer.
- **Limit**:
  - If the current limit **equals** the current request (the untouched default),
    the limit follows the request so the two stay equal.
  - If the current limit **already differs** from the request (set deliberately,
    or raised by a prior OOM), the limit is **left untouched** - only the
    request is tuned ("frozen limit").

Subject to the cluster memory minimum (default 25Mi) and maximum; the request is
capped to never exceed the limit; and the **deviation deadband** (below) decides
whether the change is worth committing.

### Recommendation Algorithm (CPU)

CPU is tuned only on the VPA path (the Prometheus fallback leaves CPU untouched).
CPU is compressible - it throttles rather than OOM-kills - so there is no floor
or emergency path:

- **Request** = VPA cpu `target * (1 + buffer%)`, clamped to `[min_cpu_m, max_cpu_request_m]` (25m..250m on odcn).
- **Limit** follows the same frozen-vs-equal rule as memory, clamped to `max_cpu_limit_m` (4000m on odcn).

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
| `RESOURCE_TUNING_MIN_DELTA_MI` | `16` | Ignore memory changes smaller than this (absolute deadband) |
| `RESOURCE_TUNING_MIN_DELTA_M` | `10` | Ignore CPU changes smaller than this in millicores (absolute deadband) |
| `RESOURCE_TUNING_SCHEDULER_ENABLED` | `true` | Run the nightly fleet-wide tuner |
| `RESOURCE_TUNING_HOUR` | `1` | Hour (Europe/Amsterdam) of the nightly sweep (off-peak, before backups) |
| `RESOURCE_TUNING_PACE_SECONDS` | `15` | Delay after each changed project, to spread pod rollouts |

Cluster-specific bounds live in `cluster_config.py`: memory via `get_min_memory_limit_mi()` / `get_max_memory_limit_mi()` / `get_max_memory_request_mi()`, CPU via `get_min_cpu_m()` (25m) / `get_max_cpu_request_m()` (250m) / `get_max_cpu_limit_m()` (4000m), and the `supports_vpa` capability flag.

### Deviation Deadband

A change must clear **both** an absolute floor and a percentage threshold, so the tuner never churns on insignificant drift:

- **Absolute floor** (`RESOURCE_TUNING_MIN_DELTA_MI` / `_M`): ignore changes of only a few Mi / millicores, so tiny pods near the cluster minimum don't get resized every sweep over a handful of megabytes.
- **Percentage** (asymmetric): an **increase** applies at ≥ `RESOURCE_TUNING_INCREASE_THRESHOLD` (react promptly - reliability); a **decrease** must clear the larger `RESOURCE_TUNING_DECREASE_THRESHOLD` (conservative - a small reclaim isn't worth a pod rollout).

OOM-driven increases bypass the deadband entirely. The deadband is also what makes an explicit cooldown unnecessary: a right-sized project's recommendation stays within the deadband of its current size, so the nightly sweep simply produces no change for it.

### Opt-Out

Auto-tuning is **on by default**. A component opts out with `auto-tune-resources: false`, settable at the component-definition level or overridden per deployment-component (deployment override wins).

## Key Files

| File | Purpose |
|------|---------|
| `opi/services/resource_analyzer.py` | Pure computation: usage/VPA target → memory & CPU recommendation, asymmetric gate |
| `opi/services/resource_tuning_service.py` | Orchestrates analysis (VPA or Prometheus), applies changes, commits |
| `opi/core/resource_tuning_scheduler.py` | Nightly fleet-wide tuner (off-peak sweep + rollout pacing) |
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
| **Deviation deadband** | Only commit when the change clears both a % threshold (10% up / 30% down) and an absolute floor (16Mi / 10m) |
| **OOM kill priority** | Always increase memory when OOM kills are present |
| **2x propagation cap** | Base component not inflated by outlier deployments |
| **Frozen limit** | A limit already set to differ from the request is left untouched - only the request is tuned |
| **Git-based changes** | All changes are auditable, reviewable, and reversible |
| **Deployment-level scoping** | Tuning writes to deployment overrides, not shared definitions (except request propagation) |
| **Fresh git reads** | Tuning reads the latest YAML from git before modifying, preventing stale cache data from overwriting concurrent changes |
| **Legacy key migration** | Flat `cpu`/`memory` resource keys are migrated into nested `requests`/`limits` before removal, preventing silent data loss |

## Nightly Auto-Tuning

`ResourceTuningScheduler` (`opi/core/resource_tuning_scheduler.py`, started from the server lifespan) runs **once a night** at `RESOURCE_TUNING_HOUR` (Europe/Amsterdam, default 01:00 — off-peak, and an hour before backups (~02:00) so resize-triggered pod rollouts settle before the backup snapshot). Each night it:

1. Enumerates every project with a deployment on this OPI's cluster.
2. Calls `tune_deployment_resources(project)` for each. The deadband decides what actually changes; converged projects produce no change and cost only the (cheap, read-only) analysis.
3. Paces `RESOURCE_TUNING_PACE_SECONDS` after each project that changed, so a convergence night spreads its pod rollouts instead of bouncing the whole fleet at once.

Why a nightly full sweep rather than a paced/capped/cooldown schedule: **checking is cheap** (VPA `.status` reads + a few Prometheus queries, no writes); the real cost — commit, reprocess, ArgoCD sync, rollout — is only paid when a project drifts past the deadband. So cost scales with how many projects *drift*, not how many are checked, and the deadband (not a cooldown) is what prevents night-to-night churn. Urgent under-provisioning between sweeps is handled out-of-band by the reactive OOM watcher.

## Known Limitations

These are inherent to reactive, history-based autoscaling and are worth knowing before trusting the tuner blindly:

- **Idle-then-spike (peak) risk.** The recommendation reflects only what was observed in the window. A workload that is idle during the window and then does something new is sized too low, and the first spike can OOM-kill it. This is worsened by the **limit-collapse** rule: when limit == request (the default), a memory *decrease* shrinks both, leaving zero burst headroom. *(Mitigation under consideration: drive the memory limit from the VPA `upperBound`, or keep a headroom multiple, so requests can shrink for packing while the limit keeps burst room.)*

- **Window sensitivity.** Memory sized to `max_over_time` over a fixed window depends on what that window captured: one that caught a rare spike sizes generously, one that missed it sizes too lean. The VPA path (a decaying ~8-day histogram) is more robust and becomes the default as VPAs populate.

- **OOM recovery timing gap.** When a freshly-resized pod OOMs, the reactive recovery re-derives `has_oom_kills` from the Prometheus OOM metric, which lags the kill by a scrape interval. If recovery runs within seconds of the kill it reads "no OOM yet", skips the bump, and logs "OOM detected but auto-tune could not determine new limits" - even though the no-metrics trial-and-error bump (1.5-3x) would have worked. *(Mitigation under consideration: pass the already-known OOM signal from the sync / OOM-watcher into the tune so the bump fires regardless of the metric lag.)*

- **Net effect depends on provisioning.** On an *under*-provisioned fleet (many components at low defaults running real workloads), the tuner raises more than it lowers - net reserved memory goes *up*. That is reliability, not savings; the memory-reclaim story only pays out on an *over*-provisioned fleet.

## Related

- `features/futures/sidecar-resource-tuning.md` - extends tuning to sidecar containers
- `features/futures/configurable-deployment-resources.md` - prerequisite for resource values in YAML
