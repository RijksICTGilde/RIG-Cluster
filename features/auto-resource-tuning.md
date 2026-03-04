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

### Existing Resource Tuning Endpoint

The `POST /api/resources/{project_name}/tune` endpoint in `opi/api/resource_router.py` already implements a basic version of this pattern:

- Queries `max_over_time(container_memory_working_set_bytes{...}[{window}h])` for peak memory
- Queries `avg(container_memory_working_set_bytes{...})` for average memory
- Calculates recommendation: `max_observed + (max_observed * buffer_percent)`
- Updates project YAML via `file_handler.set_component_resources()`
- Commits changes to git and triggers reprocessing

The auto-tuning feature extends this to run automatically and adds CPU analysis + OOM kill detection.

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
| `AUTO_SCALE_MIN_DATA_HOURS` | `12` | Minimum hours of metrics data before making recommendations |
| `AUTO_SCALE_DRY_RUN` | `true` | Log recommendations without applying (safe rollout) |

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

---

## Implementation

### Phase 1: OOM Kill Query

**File**: `opi/connectors/prometheus.py` (modify)

Add to `PrometheusConnector`:

```python
def get_oom_kill_count(self, namespace: str, pod_prefix: str, hours: int = 24) -> int:
    """Count OOM kills for pods matching prefix in the given time window."""
    query = (
        f'sum(increase(kube_pod_container_status_last_terminated_reason'
        f'{{reason="OOMKilled",namespace="{namespace}",pod=~"{pod_prefix}.*"}}[{hours}h]))'
    )
    result = self.custom_query(query)
    if result and len(result) > 0:
        value = float(result[0].get("value", [0, 0])[1])
        return int(value)
    return 0
```

Add the same method to `GrafanaPrometheusConnector` with the Grafana API wrapper.

### Phase 2: Resource Analyzer

**File**: `opi/services/resource_analyzer.py` (new)

```python
import logging
from dataclasses import dataclass
from opi.connectors.prometheus import get_metrics_connector
from opi.handlers.project_file_handler import ProjectFileHandler

logger = logging.getLogger(__name__)


@dataclass
class ResourceRecommendation:
    component_name: str
    deployment_name: str
    current_memory_limit: str
    current_cpu_limit: str
    recommended_memory_limit: str | None  # None = no change
    recommended_cpu_limit: str | None     # None = no change
    reason: str
    oom_kills_detected: int
    p95_memory_bytes: float
    p95_cpu_millicores: float
    confidence: str  # "high" (>24h data), "medium" (12-24h), "skip" (<12h)


def parse_resource_value(value: str) -> float:
    """Convert Kubernetes resource string to base units (bytes for memory, millicores for CPU)."""
    value = value.strip()
    if value.endswith("Gi"):
        return float(value[:-2]) * 1024 * 1024 * 1024
    elif value.endswith("Mi"):
        return float(value[:-2]) * 1024 * 1024
    elif value.endswith("Ki"):
        return float(value[:-2]) * 1024
    elif value.endswith("m"):
        return float(value[:-1])  # millicores
    else:
        return float(value) * 1000  # cores -> millicores for CPU


def format_memory(bytes_value: float) -> str:
    """Convert bytes to human-readable Kubernetes memory string."""
    if bytes_value >= 1024 * 1024 * 1024:
        return f"{int(bytes_value / (1024 * 1024 * 1024))}Gi"
    return f"{int(bytes_value / (1024 * 1024))}Mi"


def format_cpu(millicores: float) -> str:
    """Convert millicores to Kubernetes CPU string."""
    if millicores >= 1000:
        return f"{int(millicores / 1000)}"
    return f"{int(millicores)}m"


class ResourceAnalyzer:
    def __init__(self, config: dict):
        self.connector = get_metrics_connector()
        self.buffer_pct = config.get("buffer_percentage", 20)
        self.min_gap_pct = config.get("min_gap_percentage", 10)
        self.oom_increase_pct = config.get("oom_memory_increase_percentage", 50)
        self.min_memory = parse_resource_value(config.get("min_memory", "64Mi"))
        self.max_memory = parse_resource_value(config.get("max_memory", "4Gi"))
        self.min_cpu = parse_resource_value(config.get("min_cpu", "50m"))
        self.max_cpu = parse_resource_value(config.get("max_cpu", "2000m"))
        self.min_data_hours = config.get("min_data_hours", 12)
        self.window_hours = config.get("evaluation_window_hours", 24)

    def analyze_component(
        self,
        namespace: str,
        pod_prefix: str,
        component_name: str,
        deployment_name: str,
        current_resources: dict[str, str],
    ) -> ResourceRecommendation:
        """Analyze a single component and return resource recommendation."""

        current_mem_limit = parse_resource_value(current_resources.get("limits_memory", "512Mi"))
        current_cpu_limit = parse_resource_value(current_resources.get("limits_cpu", "1000m"))

        # Check data availability
        timeseries = self.connector.get_component_metrics_timeseries(
            namespace=namespace,
            pod_prefix=pod_prefix,
            duration_minutes=self.window_hours * 60,
            step_minutes=5,
        )

        memory_series = timeseries.get("memory", {}).get("values", [])
        cpu_series = timeseries.get("cpu", {}).get("values", [])

        data_hours = len(memory_series) * 5 / 60  # 5-min intervals
        if data_hours < self.min_data_hours:
            return ResourceRecommendation(
                component_name=component_name,
                deployment_name=deployment_name,
                current_memory_limit=current_resources.get("limits_memory", "512Mi"),
                current_cpu_limit=current_resources.get("limits_cpu", "1000m"),
                recommended_memory_limit=None,
                recommended_cpu_limit=None,
                reason=f"Insufficient data: {data_hours:.0f}h < {self.min_data_hours}h minimum",
                oom_kills_detected=0,
                p95_memory_bytes=0,
                p95_cpu_millicores=0,
                confidence="skip",
            )

        # Calculate p95 values
        mem_values = sorted([float(v[1]) for v in memory_series if v[1]])
        cpu_values = sorted([float(v[1]) for v in cpu_series if v[1]])

        p95_idx = int(len(mem_values) * 0.95)
        p95_memory = mem_values[p95_idx] if mem_values else 0
        p95_cpu = (cpu_values[int(len(cpu_values) * 0.95)] if cpu_values else 0) * 1000  # cores -> millicores

        # Check OOM kills
        oom_kills = self.connector.get_oom_kill_count(
            namespace=namespace, pod_prefix=pod_prefix, hours=self.window_hours
        )

        # --- Memory recommendation ---
        recommended_mem = None
        mem_reason = ""

        if oom_kills > 0:
            # OOM kills detected: increase memory, ignore observed usage
            new_mem = max(
                current_mem_limit * (1 + self.oom_increase_pct / 100),
                p95_memory * (1 + self.buffer_pct / 100),
            )
            new_mem = max(self.min_memory, min(self.max_memory, new_mem))
            recommended_mem = format_memory(new_mem)
            mem_reason = f"OOM kills detected ({oom_kills}x), increasing memory"

        elif p95_memory > 0:
            new_mem = p95_memory * (1 + self.buffer_pct / 100)
            new_mem = max(self.min_memory, min(self.max_memory, new_mem))
            gap_pct = abs(new_mem - current_mem_limit) / current_mem_limit * 100

            if gap_pct >= self.min_gap_pct:
                recommended_mem = format_memory(new_mem)
                direction = "reducing" if new_mem < current_mem_limit else "increasing"
                mem_reason = f"p95={format_memory(p95_memory)}, {direction} (gap={gap_pct:.0f}%)"
            else:
                mem_reason = f"Correctly sized (gap={gap_pct:.0f}% < {self.min_gap_pct}% threshold)"

        # --- CPU recommendation ---
        recommended_cpu = None
        cpu_reason = ""

        if p95_cpu > 0:
            new_cpu = p95_cpu * (1 + self.buffer_pct / 100)
            new_cpu = max(self.min_cpu, min(self.max_cpu, new_cpu))
            gap_pct = abs(new_cpu - current_cpu_limit) / current_cpu_limit * 100

            if gap_pct >= self.min_gap_pct:
                recommended_cpu = format_cpu(new_cpu)
                direction = "reducing" if new_cpu < current_cpu_limit else "increasing"
                cpu_reason = f"p95={format_cpu(p95_cpu)}, {direction} (gap={gap_pct:.0f}%)"

        reason = "; ".join(filter(None, [mem_reason, cpu_reason])) or "No changes needed"

        return ResourceRecommendation(
            component_name=component_name,
            deployment_name=deployment_name,
            current_memory_limit=current_resources.get("limits_memory", "512Mi"),
            current_cpu_limit=current_resources.get("limits_cpu", "1000m"),
            recommended_memory_limit=recommended_mem,
            recommended_cpu_limit=recommended_cpu,
            reason=reason,
            oom_kills_detected=oom_kills,
            p95_memory_bytes=p95_memory,
            p95_cpu_millicores=p95_cpu,
            confidence="high" if data_hours >= 24 else "medium",
        )
```

### Phase 3: Auto-Scale Scheduler

**File**: `opi/services/auto_scale_scheduler.py` (new)

```python
import asyncio
import logging
from datetime import datetime
from opi.core.config import settings
from opi.services.resource_analyzer import ResourceAnalyzer, ResourceRecommendation
from opi.connectors.git import GitConnector
from opi.handlers.project_file_handler import ProjectFileHandler

logger = logging.getLogger(__name__)


class AutoScaleScheduler:
    def __init__(self, project_service, file_handler: ProjectFileHandler):
        self.project_service = project_service
        self.file_handler = file_handler
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self):
        if not settings.ENABLE_AUTO_SCALE:
            logger.info("Auto-scaling disabled (ENABLE_AUTO_SCALE=false)")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "Auto-scale scheduler started (interval=%ds, dry_run=%s)",
            settings.AUTO_SCALE_INTERVAL_SECONDS,
            settings.AUTO_SCALE_DRY_RUN,
        )

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self):
        while self._running:
            try:
                await self._analyze_all_projects()
            except Exception:
                logger.exception("Auto-scale analysis failed")
            await asyncio.sleep(settings.AUTO_SCALE_INTERVAL_SECONDS)

    async def _analyze_all_projects(self):
        """Iterate all projects with auto-scale-resources: true."""
        projects = await self.project_service.list_projects()
        analyzer = ResourceAnalyzer({
            "buffer_percentage": settings.AUTO_SCALE_BUFFER_PERCENTAGE,
            "min_gap_percentage": settings.AUTO_SCALE_MIN_GAP_PERCENTAGE,
            "oom_memory_increase_percentage": settings.AUTO_SCALE_OOM_MEMORY_INCREASE_PERCENTAGE,
            "min_memory": settings.AUTO_SCALE_MIN_MEMORY,
            "max_memory": settings.AUTO_SCALE_MAX_MEMORY,
            "min_cpu": settings.AUTO_SCALE_MIN_CPU,
            "max_cpu": settings.AUTO_SCALE_MAX_CPU,
            "min_data_hours": settings.AUTO_SCALE_MIN_DATA_HOURS,
            "evaluation_window_hours": settings.AUTO_SCALE_EVALUATION_WINDOW_HOURS,
        })

        for project in projects:
            project_data = await self.project_service.get_project(project["name"])
            if not project_data:
                continue

            if not project_data.get("auto-scale-resources", False):
                continue

            await self._analyze_project(analyzer, project_data)

    async def _analyze_project(self, analyzer: ResourceAnalyzer, project_data: dict):
        """Analyze all deployments in a project and apply recommendations."""
        project_name = project_data["name"]
        recommendations: list[ResourceRecommendation] = []
        changes_made = False

        deployments = project_data.get("deployments", [])
        for deployment in deployments:
            dep_name = deployment["name"]

            # Check deployment-level override
            if deployment.get("auto-scale-resources") is False:
                logger.debug("Skipping %s/%s (disabled at deployment level)", project_name, dep_name)
                continue

            namespace = f"rig-{project_name}-{dep_name}"
            components = deployment.get("components", [])

            for comp in components:
                comp_ref = comp.get("reference", comp.get("name", ""))
                pod_prefix = f"{dep_name}-{comp_ref}"

                # Get current resources
                current = self.file_handler.extract_component_resources(project_data, comp_ref)
                dep_override = self.file_handler.extract_deployment_component_resources(
                    project_data, dep_name, comp_ref
                )
                if dep_override:
                    current.update(dep_override)

                rec = analyzer.analyze_component(
                    namespace=namespace,
                    pod_prefix=pod_prefix,
                    component_name=comp_ref,
                    deployment_name=dep_name,
                    current_resources=current,
                )
                recommendations.append(rec)

                if rec.confidence == "skip":
                    continue

                if rec.recommended_memory_limit or rec.recommended_cpu_limit:
                    logger.info(
                        "Recommendation for %s/%s/%s: memory=%s->%s, cpu=%s->%s (%s)",
                        project_name, dep_name, comp_ref,
                        rec.current_memory_limit, rec.recommended_memory_limit or "(no change)",
                        rec.current_cpu_limit, rec.recommended_cpu_limit or "(no change)",
                        rec.reason,
                    )

                    if not settings.AUTO_SCALE_DRY_RUN:
                        new_resources = {}
                        if rec.recommended_memory_limit:
                            new_resources["limits_memory"] = rec.recommended_memory_limit
                        if rec.recommended_cpu_limit:
                            new_resources["limits_cpu"] = rec.recommended_cpu_limit
                        self.file_handler.set_component_resources(project_data, comp_ref, new_resources)
                        changes_made = True

        # Commit changes to git if any were made
        if changes_made:
            await self._commit_changes(project_name, project_data, recommendations)

    async def _commit_changes(
        self, project_name: str, project_data: dict, recommendations: list[ResourceRecommendation]
    ):
        """Write updated project data to git with descriptive commit message."""
        changed = [r for r in recommendations if r.recommended_memory_limit or r.recommended_cpu_limit]
        summary_lines = []
        for r in changed:
            parts = []
            if r.recommended_memory_limit:
                parts.append(f"mem: {r.current_memory_limit}->{r.recommended_memory_limit}")
            if r.recommended_cpu_limit:
                parts.append(f"cpu: {r.current_cpu_limit}->{r.recommended_cpu_limit}")
            summary_lines.append(f"  {r.deployment_name}/{r.component_name}: {', '.join(parts)}")

        commit_msg = (
            f"auto-scale: tune resources for {project_name}\n\n"
            + "\n".join(summary_lines)
            + f"\n\nGenerated by auto-scale at {datetime.utcnow().isoformat()}"
        )

        try:
            git_connector = await self.project_service.get_git_connector(project_name)
            async with git_connector:
                await git_connector.ensure_repo_cloned()
                # Write updated YAML
                file_path = f"projects/{project_name}.yaml"
                import yaml
                yaml_content = yaml.dump(project_data, default_flow_style=False, allow_unicode=True)
                await git_connector.add_file(file_path, yaml_content)
                await git_connector.commit_and_push(commit_msg)
            logger.info("Committed resource changes for %s", project_name)
        except Exception:
            logger.exception(
                "Failed to commit resource changes for %s. "
                "Changes will be retried on next cycle.",
                project_name,
            )
            # No retry mechanism here -- the next scheduler cycle will
            # re-analyze and re-apply if the gap still exceeds thresholds.
```

### Phase 4: Startup Integration

**File**: `opi/server.py` (modify)

```python
# In lifespan startup, after project_service is available:
if settings.ENABLE_AUTO_SCALE:
    from opi.services.auto_scale_scheduler import AutoScaleScheduler
    scheduler = AutoScaleScheduler(project_service, file_handler)
    await scheduler.start()
    app.state.auto_scale_scheduler = scheduler

# In lifespan shutdown:
if hasattr(app.state, "auto_scale_scheduler"):
    await app.state.auto_scale_scheduler.stop()
```

### Phase 5: API Endpoint for Manual Trigger + Status

**File**: `opi/api/resource_router.py` (modify)

Add alongside existing `/tune` endpoint:

```python
@resource_router.get("/auto-scale/status")
@validate_api_token
async def auto_scale_status(request: Request) -> JSONResponse:
    """Show last auto-scale run results and next scheduled run."""
    scheduler = request.app.state.auto_scale_scheduler
    return JSONResponse(content={
        "enabled": settings.ENABLE_AUTO_SCALE,
        "dry_run": settings.AUTO_SCALE_DRY_RUN,
        "interval_seconds": settings.AUTO_SCALE_INTERVAL_SECONDS,
        "last_run": scheduler.last_run_at.isoformat() if scheduler.last_run_at else None,
        "last_recommendations": [asdict(r) for r in scheduler.last_recommendations],
    })

@resource_router.post("/auto-scale/run")
@validate_master_api_key
async def trigger_auto_scale(request: Request) -> JSONResponse:
    """Manually trigger an auto-scale analysis cycle."""
    scheduler = request.app.state.auto_scale_scheduler
    await scheduler._analyze_all_projects()
    return JSONResponse(content={"status": "completed"})
```

---

## Error Handling Strategy

### Git Write-Back Failures

| Failure | Handling |
|---------|----------|
| Git clone fails (network/auth) | Log error, skip project, retry on next cycle |
| Commit conflict (concurrent edit) | Log error, skip project, retry on next cycle (fresh clone will pick up latest) |
| Push fails (remote rejected) | Log error, skip project, retry on next cycle |
| YAML serialization error | Log error, skip project (should not happen with validated data) |

The key insight is that **retries are implicit**: since the scheduler runs every hour, a failed commit will be retried naturally because the metrics analysis will produce the same (or updated) recommendation on the next cycle. No explicit retry queue is needed.

### Metrics Failures

| Failure | Handling |
|---------|----------|
| Prometheus/Grafana unreachable | `confidence="skip"` — no changes applied |
| Insufficient data (<12h) | `confidence="skip"` — no changes applied |
| Partial metrics (CPU but no memory) | Only recommend for the metric that has data |
| Metrics return NaN/empty | Treat as zero, skip recommendation |

---

## Safety Mechanisms

| Mechanism | Purpose |
|-----------|---------|
| **Min/max bounds** | Prevent runaway scaling in either direction |
| **Minimum data requirement (12h)** | No recommendations without sufficient observation |
| **Gap threshold (10%)** | Only update when the difference is meaningful |
| **OOM kill priority** | Never reduce memory when OOM kills are present |
| **Git-based changes** | All changes are auditable, reviewable, and reversible |
| **Per-project opt-in** | Feature is disabled by default, enabled per project |
| **Deployment-level override** | Critical deployments can opt out |
| **Dry-run mode (default)** | First deployment only logs, does not apply changes |
| **Commit message audit trail** | Every auto-scale change has a detailed commit message |

---

## Files Summary

### New Files

| File | Purpose |
|------|---------|
| `opi/services/resource_analyzer.py` | Recommendation algorithm with p95, OOM, bounds |
| `opi/services/auto_scale_scheduler.py` | Background scheduler loop + git integration |

### Modified Files

| File | Change |
|------|--------|
| `opi/connectors/prometheus.py` | Add `get_oom_kill_count()` method |
| `opi/connectors/grafana_prometheus.py` | Add `get_oom_kill_count()` method |
| `opi/core/config.py` | Add `ENABLE_AUTO_SCALE`, `AUTO_SCALE_*` settings |
| `opi/server.py` | Start/stop scheduler in lifespan |
| `opi/api/resource_router.py` | Add status + manual trigger endpoints |

---

## Dependencies

- **Phase 1 prerequisite**: Dynamic resource configuration must be implemented first (resources readable/writable in project YAML via `file_handler.set_component_resources()` — already exists)
- Metrics collection must be functional (Prometheus or Grafana reachable)
- `kube-state-metrics` must be deployed in the cluster
- Git connector must have write access to project files
- ArgoCD must be configured for sync (automatic or manual trigger)

## Verification

1. **Dry-run test**: Enable `AUTO_SCALE_DRY_RUN=true`, verify recommendations are logged but not committed
2. **OOM detection**: Kill a pod with `kubectl exec ... -- stress --vm 1 --vm-bytes 600M`, verify OOM is detected and memory increase is recommended
3. **Over-provisioned**: Deploy with 4Gi limit, observe 100Mi usage, verify reduction is recommended
4. **Under-provisioned**: Deploy with 128Mi limit near 120Mi usage, verify no change (within gap threshold)
5. **Git commit**: Disable dry-run, verify commit appears in git with correct message and YAML changes
6. **Multi-project**: Enable on 3 projects, verify all are analyzed in one cycle
7. **Metrics unavailable**: Stop Prometheus, verify no changes are applied (graceful degradation)

## Related

- `opi/connectors/prometheus.py` — direct Prometheus connector
- `opi/connectors/grafana_prometheus.py` — Grafana-proxied Prometheus connector
- `opi/api/resource_router.py` — existing manual tune endpoint
- `features/future/sidecar-resource-tuning.md` — extends this to sidecar containers
- `features/future/configurable-deployment-resources.md` — prerequisite for resource values in YAML
