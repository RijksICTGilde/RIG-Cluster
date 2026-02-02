# Auto-Scale Resources Feature

**Status**: Planned
**Priority**: Future Enhancement
**Created**: 2026-01-29

## Overview

Implement automatic resource scaling for Kubernetes deployments based on Prometheus metrics. The system will monitor CPU and memory usage over a 24-hour window, compare against current limits, and automatically update project files when the gap exceeds a configurable threshold (default 10%).

## Problem Statement

Currently, resource limits are **hardcoded** in `manifests/deployment.yaml.jinja` (lines 110-116):
```yaml
resources:
  requests:
    memory: "128Mi"
    cpu: "100m"
  limits:
    memory: "512Mi"
    cpu: "500m"
```

**Issues:**
- No mechanism to customize resources per component or deployment
- Over-provisioning wastes cluster resources
- Under-provisioning causes OOM kills and application instability
- Manual resource tuning is time-consuming and error-prone

## Design Decisions

1. **OOM Kill Data**: Graceful degradation - feature works without OOM metrics, uses them when available
2. **Scale-down behavior**: Same window as scale-up (24h) - consistent behavior for both directions
3. **Notifications**: Log scaling events for now; design for future extensibility (webhook support can be added later)

---

## Configuration Schema

### Project-Level Setting
```yaml
name: my-project
auto-scale-resources: true  # Default for all deployments

# Optional advanced settings
auto-scale-config:
  buffer-percentage: 20       # Headroom above observed usage (default: 20%)
  min-gap-percentage: 10      # Minimum gap to trigger update (default: 10%)
  evaluation-window: 24h      # Metrics window (default: 24h)
```

### Deployment-Level Override
```yaml
deployments:
  - name: production
    auto-scale-resources: false  # Override: disable for this deployment
    components:
      - reference: api-server
```

### Component-Level Resources (output written by auto-scaler)
```yaml
components:
  - name: api-server
    resources:
      requests:
        memory: "256Mi"
        cpu: "150m"
      limits:
        memory: "640Mi"
        cpu: "375m"
```

---

## Implementation Plan

### Phase 1: Enable Dynamic Resource Configuration

**Goal**: Make resources configurable in project files (prerequisite for auto-scaling)

#### 1.1 Modify Deployment Template
**File**: `manifests/deployment.yaml.jinja` (lines 110-116)

Change from hardcoded to template variables:
```yaml
resources:
  requests:
    memory: "{{ resource_requests_memory | default('128Mi') }}"
    cpu: "{{ resource_requests_cpu | default('100m') }}"
  limits:
    memory: "{{ resource_limits_memory | default('512Mi') }}"
    cpu: "{{ resource_limits_cpu | default('500m') }}"
```

#### 1.2 Update Project Manager
**File**: `opi/manager/project_manager.py` (around lines 4047-4074)

Add resource extraction and template variable passing:
```python
# Extract resources from component (with deployment override)
resource_config = self._extract_component_resources(component_ref, deployment)

variables = {
    # ... existing variables ...
    "resource_requests_memory": resource_config["requests"]["memory"],
    "resource_requests_cpu": resource_config["requests"]["cpu"],
    "resource_limits_memory": resource_config["limits"]["memory"],
    "resource_limits_cpu": resource_config["limits"]["cpu"],
}
```

#### 1.3 Add Project File Handler Methods
**File**: `opi/handlers/project_file_handler.py`

New methods:
- `extract_auto_scale_enabled(project_data, deployment_name)` - respects override hierarchy
- `extract_component_resources(project_data, component_name)` - get current resources
- `set_component_resources(project_data, component_name, resources)` - update resources

---

### Phase 2: Resource Analyzer Service

**Goal**: Analyze Prometheus metrics and generate recommendations

#### 2.1 Create Resource Analyzer
**New File**: `opi/services/resource_analyzer.py`

```python
@dataclass
class ResourceRecommendation:
    component_name: str
    deployment_name: str
    namespace: str
    current_limits: dict[str, str]
    recommended_limits: dict[str, str]
    metrics_summary: dict[str, float]  # avg, max, p95 for cpu/memory
    oom_kills_detected: int
    reason: str

class ResourceAnalyzer:
    async def analyze_component(
        self,
        namespace: str,
        pod_prefix: str,
        current_resources: dict,
        evaluation_window_hours: int = 24
    ) -> ResourceRecommendation | None:
        """Return recommendation if adjustment needed (gap > threshold)."""
```

**Logic**:
1. Query 24h CPU/memory time-series from Prometheus
2. Calculate avg, max, p95 percentiles
3. Query OOM kills count (graceful degradation if unavailable)
4. Apply buffer percentage (default 20%)
5. Compare with current limits
6. Return recommendation if gap > min-gap-percentage

#### 2.2 Extend Prometheus Connector
**File**: `opi/connectors/prometheus.py`

Add methods:
```python
async def get_resource_metrics_for_autoscale(
    self, namespace: str, pod_prefix: str, hours: int
) -> dict[str, float]:
    """Return avg, max, p95 for CPU (millicores) and memory (MB)."""

async def get_oom_kill_count(
    self, namespace: str, pod_prefix: str, hours: int
) -> int:
    """Query kube_pod_container_status_last_terminated_reason{reason='OOMKilled'}.
    Returns 0 if metric is unavailable (graceful degradation).
    """
```

---

### Phase 3: Auto-Scale Scheduler

**Goal**: Periodic analysis and automatic updates

#### 3.1 Create Scheduler Service
**New File**: `opi/services/auto_scale_scheduler.py`

```python
class AutoScaleScheduler:
    def __init__(self, interval_seconds: int = 3600):
        self._task: asyncio.Task | None = None

    async def start(self, app: FastAPI) -> None:
        """Start background scheduler."""

    async def stop(self) -> None:
        """Stop scheduler gracefully."""

    async def analyze_all_projects(self) -> list[AutoScaleResult]:
        """Analyze all projects with auto-scale enabled."""
```

**Workflow**:
1. List all project files
2. For each project with `auto-scale-resources: true`:
   - Get deployments targeting current cluster
   - For each component, call ResourceAnalyzer
   - Collect recommendations
3. Apply recommendations to project files
4. Commit changes via GitConnector
5. Trigger ArgoCD sync

#### 3.2 Create Auto-Scale Handler
**New File**: `opi/handlers/auto_scale_handler.py`

```python
class AutoScaleHandler:
    async def apply_recommendations(
        self, project_path: str, recommendations: list[ResourceRecommendation]
    ) -> bool:
        """Update project file and trigger deployment."""
```

#### 3.3 Integrate with Application Lifecycle
**File**: `opi/server.py` or `opi/core/startup.py`

```python
if settings.ENABLE_AUTO_SCALE:
    scheduler = AutoScaleScheduler()
    await scheduler.start(app)
```

---

### Phase 4: Configuration Settings

**File**: `opi/core/config.py`

```python
# Auto-scaling configuration
ENABLE_AUTO_SCALE: bool = False
AUTO_SCALE_INTERVAL_SECONDS: int = 3600  # 1 hour
AUTO_SCALE_BUFFER_PERCENTAGE: int = 20
AUTO_SCALE_MIN_GAP_PERCENTAGE: int = 10
AUTO_SCALE_MIN_MEMORY: str = "64Mi"
AUTO_SCALE_MAX_MEMORY: str = "4Gi"
AUTO_SCALE_MIN_CPU: str = "50m"
AUTO_SCALE_MAX_CPU: str = "2000m"
AUTO_SCALE_OOM_MEMORY_INCREASE_PERCENTAGE: int = 50
AUTO_SCALE_EVALUATION_WINDOW_HOURS: int = 24
```

---

## Files Summary

### Critical Files to Modify

| File | Changes |
|------|---------|
| `manifests/deployment.yaml.jinja` | Template variables for resources |
| `opi/manager/project_manager.py` | Extract and pass resource config |
| `opi/handlers/project_file_handler.py` | Resource extraction/setting methods |
| `opi/connectors/prometheus.py` | OOM kill query, auto-scale metrics |
| `opi/core/config.py` | Auto-scale settings |
| `opi/server.py` or `opi/core/startup.py` | Scheduler lifecycle |

### New Files to Create

| File | Purpose |
|------|---------|
| `opi/services/resource_analyzer.py` | Metrics analysis and recommendations |
| `opi/services/auto_scale_scheduler.py` | Background scheduler |
| `opi/handlers/auto_scale_handler.py` | Apply recommendations to project files |

---

## Edge Cases & Safety

1. **Metrics unavailable**: Skip analysis, log warning
2. **Insufficient data points**: Require minimum 12 hours of data
3. **OOM kills detected**: Increase memory by 50% (configurable)
4. **Safety bounds**: Enforce min/max limits to prevent runaway scaling
5. **Concurrent updates**: Use git pull before update, lock during write
6. **Scale-down protection**: Apply same 24h window as scale-up

---

## Verification Plan

1. **Unit tests**: ResourceAnalyzer parsing, calculation, threshold logic
2. **Integration tests**: Mock Prometheus, verify project file updates
3. **Manual testing**:
   - Deploy test app with known resource usage
   - Enable auto-scale, wait 24h (or use shorter window for testing)
   - Verify recommendations generated correctly
   - Verify project file updated and ArgoCD syncs

---

## Dependencies

- Prometheus metrics collection must be functional
- Git connector must have write access to project files
- ArgoCD must be configured for automatic sync or manual trigger

---

## Future Enhancements

- Webhook notifications when scaling occurs
- Dashboard showing scaling history and recommendations
- Predictive scaling based on historical patterns
- Per-component custom thresholds
- Integration with Kubernetes HPA for horizontal scaling
