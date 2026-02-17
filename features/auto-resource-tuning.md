# Auto Resource Tuning

**Status**: Planned
**Priority**: Future Enhancement
**Created**: 2026-02-10

## Problem Statement

Many deployments in the cluster run with over-provisioned CPU and memory requests/limits. This wastes cluster resources and increases costs. Under-provisioned workloads risk OOM kills and application instability. Currently, resource values are hardcoded in the deployment template (`manifests/deployment.yaml.jinja`) with no mechanism to tune them based on actual usage.

Manual resource tuning is time-consuming, error-prone, and rarely revisited after initial deployment.

## Constraints

### Tenant Cluster

The production environment (ODCN) is a **tenant cluster** where we cannot install cluster-wide resources. This rules out:

- **Vertical Pod Autoscaler (VPA)** — requires CRD installation and a cluster-scoped controller (vpa-recommender, vpa-admission-controller)
- **Prometheus Operator** — requires CRDs (`ServiceMonitor`, `PodMonitor`, etc.) and a cluster-scoped controller
- Any solution that depends on cluster-admin permissions

### GitOps Compatibility

All resource changes must flow through git. Direct mutation of pod specs in the cluster (as VPA Auto mode does) conflicts with ArgoCD, which would flag perpetual OutOfSync. The solution must update resource values **in git**, letting ArgoCD deploy the changes.

## Approach: Metrics-Driven GitOps Feedback Loop

The Operations Manager already has the infrastructure to implement this. The approach uses existing metrics connectors to observe actual resource usage, compute recommendations, and write updated values back to project files in git.

```
Metrics Source (Prometheus / Grafana)
        |
        | PromQL queries (CPU, memory, OOM kills)
        v
Operations Manager (Resource Analyzer)
        |
        | Compare observed vs. declared, apply thresholds
        v
Project Files (git)
        |
        | Commit updated resource values
        v
ArgoCD (deploys changes)
```

### Why Not VPA?

VPA is the standard Kubernetes answer for this problem, but it requires cluster-admin permissions to install CRDs and controllers. In a tenant cluster this is not available. Even if it were, VPA's Auto mode directly mutates pod specs, which conflicts with GitOps principles. VPA's recommendation-only mode would work architecturally, but still requires the CRD installation we cannot do.

### Why Not Prometheus Operator CRDs?

The fake Prometheus CRDs installed in the cluster exist solely to satisfy the ArgoCD Operator's ClusterRole validation. They register the API group but have no controller watching them. The standalone Prometheus pod uses its own `prometheus.yml` config and is completely independent of these CRDs. The auto-tuning feature does not depend on or interact with the Prometheus CRDs in any way.

## Existing Infrastructure

### Metrics Connectors (already implemented)

Both connectors share the same interface and are selected via the `METRICS_BACKEND` setting:

| Connector | File | Used in |
|-----------|------|---------|
| `PrometheusConnector` | `opi/connectors/prometheus.py` | Local / sandbox (direct Prometheus access) |
| `GrafanaPrometheusConnector` | `opi/connectors/grafana_prometheus.py` | Production (Grafana API with service account token) |
| `get_metrics_connector()` | `opi/connectors/prometheus.py` | Abstraction — returns the right connector based on config |

### Already Available Queries

| Data | Method | Status |
|------|--------|--------|
| CPU usage (instant + time-series) | `get_component_metrics_timeseries()` | Available |
| Memory usage (instant + time-series) | `get_component_metrics_timeseries()` | Available |
| CPU/memory limits | `kube_pod_container_resource_limits` query | Available |
| Pod restarts | `get_pod_restarts()` | Available |
| Workload discovery | `discover_workloads_in_namespace()` | Available |
| HTTP request rate | `get_component_metrics()` | Available |
| PVC storage usage | `get_pvc_storage_by_namespace()` | Available |

### Missing Query: OOM Kill Detection

One PromQL query needs to be added to the connectors:

```promql
kube_pod_container_status_last_terminated_reason{reason="OOMKilled", namespace="<namespace>"}
```

This metric comes from **kube-state-metrics**, which is the same source as `kube_pod_info` and `kube_pod_container_resource_limits` — both already queried successfully by the existing connectors. No additional installation is required.

## OOM Kill Handling

OOM kills require special treatment because they produce **misleading usage data**. A container killed before reaching its actual memory need shows artificially low observed usage. Naively right-sizing based on that data would lower the limit further, making OOM kills more frequent.

### Priority Rules

| Condition | Action |
|-----------|--------|
| OOM kills detected in evaluation window | **Increase** memory limit (ignore observed usage for memory) |
| Observed usage well below limit, no OOM kills | Safe to **reduce** limit |
| Observed usage near limit, no OOM kills | **Leave alone** (correctly sized) |

### Decision Logic

```
for each container in namespace:
    if oom_kills_detected(container, window=24h):
        # OOM kills always take priority — container was killed before
        # reaching its actual need, so observed usage is unreliable
        new_memory_limit = max(current_memory_limit * (1 + oom_increase%), observed_p95_memory * (1 + buffer%))
        # CPU is unaffected by OOM kills, tune normally
        new_cpu = compute_cpu_recommendation(observed_p95_cpu, buffer%)

    elif observed_p95 < current_request * (1 - gap%):
        # Over-provisioned — safe to reduce
        new_limit = observed_p95 * (1 + buffer%)

    else:
        # Correctly sized — no change
        skip
```

## Configuration

### Global Settings (config.py)

| Setting | Default | Description |
|---------|---------|-------------|
| `ENABLE_AUTO_SCALE` | `false` | Feature toggle |
| `AUTO_SCALE_INTERVAL_SECONDS` | `3600` | How often the analyzer runs (1 hour) |
| `AUTO_SCALE_EVALUATION_WINDOW_HOURS` | `24` | Metrics lookback window |
| `AUTO_SCALE_BUFFER_PERCENTAGE` | `20` | Headroom above observed p95 usage |
| `AUTO_SCALE_MIN_GAP_PERCENTAGE` | `10` | Minimum gap to trigger an update |
| `AUTO_SCALE_OOM_MEMORY_INCREASE_PERCENTAGE` | `50` | Memory increase when OOM detected |
| `AUTO_SCALE_MIN_MEMORY` | `64Mi` | Floor — never go below this |
| `AUTO_SCALE_MAX_MEMORY` | `4Gi` | Ceiling — never exceed this |
| `AUTO_SCALE_MIN_CPU` | `50m` | Floor for CPU |
| `AUTO_SCALE_MAX_CPU` | `2000m` | Ceiling for CPU |

### Project-Level Settings

```yaml
name: my-project
auto-scale-resources: true

# Optional overrides
auto-scale-config:
  buffer-percentage: 20
  min-gap-percentage: 10
  evaluation-window: 24h
```

### Deployment-Level Override

```yaml
deployments:
  - name: production
    auto-scale-resources: false  # Disable for this specific deployment
```

## Implementation Phases

### Phase 1: Dynamic Resource Configuration

Make resource values configurable in project files (prerequisite — without this, auto-tuning has nowhere to write).

- Modify `manifests/deployment.yaml.jinja` to use template variables instead of hardcoded values
- Update project manager to pass resource config from project files to templates
- Add project file handler methods for reading/writing resource values

### Phase 2: OOM Kill Query + Resource Analyzer

Add the missing OOM kill query to the metrics connectors and build the analysis logic.

- Add `get_oom_kill_count(namespace, pod_prefix, hours)` to both `PrometheusConnector` and `GrafanaPrometheusConnector`
- Create `opi/services/resource_analyzer.py` with the recommendation logic
- Implement p95 calculation, OOM kill priority, buffer application, safety bounds

### Phase 3: Scheduler + GitOps Integration

Wire the analyzer into a periodic background task that updates project files.

- Create `opi/services/auto_scale_scheduler.py` — background task running at configured interval
- For each project with `auto-scale-resources: true`, run the analyzer
- Apply recommendations by updating project files in git via the git connector
- Trigger ArgoCD sync for affected applications

## Safety Mechanisms

| Mechanism | Purpose |
|-----------|---------|
| **Min/max bounds** | Prevent runaway scaling in either direction |
| **Minimum data requirement** | Require at least 12 hours of metrics before making recommendations |
| **Gap threshold** | Only update when the difference exceeds a meaningful percentage |
| **OOM kill priority** | Never reduce memory when OOM kills are present |
| **Git-based changes** | All changes are auditable, reviewable, and reversible |
| **Per-project opt-in** | Feature is disabled by default, enabled per project |
| **Deployment-level override** | Critical deployments can opt out |

## Dependencies

- Metrics collection must be functional (Prometheus or Grafana reachable)
- `kube-state-metrics` must be deployed in the cluster (provides `kube_pod_container_resource_limits` and `kube_pod_container_status_last_terminated_reason` — typically cluster-admin managed)
- Git connector must have write access to project files
- ArgoCD must be configured for sync (automatic or manual trigger)

## Related

- `operations-manager/python/features/auto-scale-resources.md` — detailed implementation plan with code examples
- `opi/connectors/prometheus.py` — direct Prometheus connector
- `opi/connectors/grafana_prometheus.py` — Grafana-proxied Prometheus connector
