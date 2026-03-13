# Sidecar and Non-App Container Resource Tuning

**Status**: Planned
**Priority**: Medium
**Created**: 2026-02-15

## Problem Statement

The current resource tuning endpoint (`POST /api/resources/{project_name}/tune` in `opi/api/resource_router.py`) only queries Prometheus for the `app` container. Sidecar containers (e.g. `authorization-wall` / oauth2-proxy) and any future sidecars are invisible to the tuning logic, meaning their resource usage is never analyzed or adjusted.

### Specific Issues

- Prometheus queries filter on `container="app"`, missing sidecar memory/CPU usage entirely
- Sidecar resource limits are hardcoded in the sidecar Jinja templates (e.g. 64Mi for oauth2-proxy in `sidecar-authorization-wall.yaml.jinja`)
- Pod-level memory pressure may be caused by sidecars, but the tuning endpoint won't detect it
- OOM kill detection also only looks at the `app` container
- The sanitize endpoint has the same blind spot (only checks `app` container health)

## Solution Design

### Approach

Extend the existing resource tuning flow to:
1. Discover all containers in a pod (not just `app`)
2. Query metrics per-container
3. Generate per-container recommendations
4. Allow sidecar resource overrides in project YAML
5. Pass overrides from project config to sidecar Jinja templates

### Container Discovery

Use the existing `discover_workloads_in_namespace()` method, then query container-level metrics:

```promql
# Discover all containers in a deployment's pods
kube_pod_container_info{namespace="<namespace>", pod=~"<pod_prefix>.*"}

# Memory per container (not just container="app")
container_memory_working_set_bytes{namespace="<namespace>", pod=~"<pod_prefix>.*", container!="", container!="POD"}

# CPU per container
rate(container_cpu_usage_seconds_total{namespace="<namespace>", pod=~"<pod_prefix>.*", container!="", container!="POD"}[5m])

# OOM kills per container
kube_pod_container_status_last_terminated_reason{reason="OOMKilled", namespace="<namespace>", pod=~"<pod_prefix>.*"}
```

The key change: remove `container="app"` filter and group by `container` label instead.

---

## Implementation

### Phase 1: Per-Container Metrics Methods

**File**: `opi/connectors/prometheus.py` (modify)

Add to `PrometheusConnector`:

```python
def get_per_container_metrics(
    self, namespace: str, pod_prefix: str, duration_minutes: int = 60, step_minutes: int = 5
) -> dict[str, dict[str, Any]]:
    """
    Query CPU and memory metrics for ALL containers in pods matching prefix.
    Returns: {
        "app": {"memory": {"values": [...]}, "cpu": {"values": [...]}},
        "authorization-wall": {"memory": {"values": [...]}, "cpu": {"values": [...]}},
    }
    """
    end = datetime.utcnow()
    start = end - timedelta(minutes=duration_minutes)
    step = f"{step_minutes}m"

    # Memory per container
    mem_query = (
        f'container_memory_working_set_bytes{{namespace="{namespace}",'
        f'pod=~"{pod_prefix}.*",container!="",container!="POD"}}'
    )
    mem_result = self.query_range(mem_query, start.isoformat(), end.isoformat(), step)

    # CPU per container
    cpu_query = (
        f'rate(container_cpu_usage_seconds_total{{namespace="{namespace}",'
        f'pod=~"{pod_prefix}.*",container!="",container!="POD"}}[5m])'
    )
    cpu_result = self.query_range(cpu_query, start.isoformat(), end.isoformat(), step)

    # Group by container name
    containers: dict[str, dict[str, Any]] = {}
    for series in mem_result:
        container = series["metric"].get("container", "unknown")
        containers.setdefault(container, {})["memory"] = {"values": series.get("values", [])}

    for series in cpu_result:
        container = series["metric"].get("container", "unknown")
        containers.setdefault(container, {})["cpu"] = {"values": series.get("values", [])}

    return containers


def get_per_container_oom_kills(
    self, namespace: str, pod_prefix: str, hours: int = 24
) -> dict[str, int]:
    """
    Count OOM kills per container.
    Returns: {"app": 0, "authorization-wall": 2}
    """
    query = (
        f'sum by (container) (increase('
        f'kube_pod_container_status_last_terminated_reason'
        f'{{reason="OOMKilled",namespace="{namespace}",pod=~"{pod_prefix}.*"}}[{hours}h]))'
    )
    result = self.custom_query(query)
    kills = {}
    for series in result:
        container = series["metric"].get("container", "unknown")
        kills[container] = int(float(series.get("value", [0, 0])[1]))
    return kills
```

Add the same methods to `GrafanaPrometheusConnector`.

### Phase 2: Sidecar Resource Overrides in Project YAML

Allow projects to override default sidecar resource limits:

```yaml
components:
  - name: frontend
    type: deployment
    uses-services:
      - publish-on-web
      - keycloak
      - authorization-wall:
          config:
            banner: "Welkom"
    # NEW: sidecar resource overrides
    sidecar-resources:
      authorization-wall:
        requests:
          memory: "64Mi"
          cpu: "25m"
        limits:
          memory: "128Mi"
          cpu: "100m"
```

### Phase 3: Template Variable Passing

**File**: `opi/manager/manifest_manager.py` (modify)

When rendering sidecar templates, pass resource overrides from the project YAML:

```python
def get_sidecar_resources(
    self, component: dict, sidecar_name: str
) -> dict[str, dict[str, str]]:
    """Get resource config for a sidecar, with project-level overrides."""
    # Default sidecar resources
    defaults = {
        "authorization-wall": {
            "requests": {"memory": "32Mi", "cpu": "10m"},
            "limits": {"memory": "64Mi", "cpu": "50m"},
        },
    }

    resources = defaults.get(sidecar_name, {
        "requests": {"memory": "32Mi", "cpu": "10m"},
        "limits": {"memory": "64Mi", "cpu": "50m"},
    })

    # Apply project-level overrides
    overrides = component.get("sidecar-resources", {}).get(sidecar_name, {})
    if overrides:
        for level in ("requests", "limits"):
            if level in overrides:
                resources[level].update(overrides[level])

    return resources
```

**File**: `manifests/sidecar-authorization-wall.yaml.jinja` (modify)

Replace hardcoded resource values with template variables:

```yaml
# In the container section:
resources:
  requests:
    memory: "{{ sidecar_resources.requests.memory | default('32Mi') }}"
    cpu: "{{ sidecar_resources.requests.cpu | default('10m') }}"
  limits:
    memory: "{{ sidecar_resources.limits.memory | default('64Mi') }}"
    cpu: "{{ sidecar_resources.limits.cpu | default('50m') }}"
```

### Phase 4: Extended Tune Endpoint

**File**: `opi/api/resource_router.py` (modify)

Update the `tune_resources()` endpoint to include sidecar analysis:

```python
@resource_router.post("/{project_name}/tune")
@validate_api_token
async def tune_resources(
    request: Request,
    project_name: str,
    deployment: str | None = Query(None),
    include_sidecars: bool = Query(False, description="Include sidecar containers in analysis"),
) -> JSONResponse:
    # ... existing logic for app container ...

    if include_sidecars:
        connector = get_metrics_connector()
        per_container = connector.get_per_container_metrics(
            namespace=namespace, pod_prefix=pod_prefix,
            duration_minutes=settings.RESOURCE_TUNING_WINDOW_HOURS * 60,
        )
        oom_kills = connector.get_per_container_oom_kills(
            namespace=namespace, pod_prefix=pod_prefix,
            hours=settings.RESOURCE_TUNING_WINDOW_HOURS,
        )

        for container_name, metrics in per_container.items():
            if container_name == "app":
                continue  # Already handled by existing logic

            # Analyze sidecar container
            recommendation = analyze_container(
                container_name=container_name,
                metrics=metrics,
                oom_kills=oom_kills.get(container_name, 0),
                current_resources=get_sidecar_defaults(container_name),
            )
            results["sidecars"][container_name] = recommendation

    return JSONResponse(content=results)
```

### Phase 5: Auto-Scale Integration

If auto-resource-tuning is enabled, the `ResourceAnalyzer` should also analyze sidecar containers:

```python
# In ResourceAnalyzer.analyze_component():
# After analyzing the 'app' container, also analyze sidecars
per_container = self.connector.get_per_container_metrics(...)
for container_name, metrics in per_container.items():
    if container_name == "app":
        continue
    sidecar_rec = self._analyze_sidecar(container_name, metrics, oom_kills)
    recommendations.append(sidecar_rec)
```

Sidecar recommendations are written to the project YAML under `sidecar-resources` and committed to git.

---

## Known Sidecar Containers

| Container Name | Template | Default Memory | Default CPU | Notes |
|---------------|----------|---------------|-------------|-------|
| `authorization-wall` | `sidecar-authorization-wall.yaml.jinja` | 64Mi | 50m | oauth2-proxy; predictable low usage for most sites |
| `app` | `deployment.yaml.jinja` | 512Mi | 1000m | Main application container |

Future sidecars should follow the same pattern: define defaults in the template, allow overrides via `sidecar-resources`.

---

## Files Summary

### Modified Files

| File | Change |
|------|--------|
| `opi/connectors/prometheus.py` | Add `get_per_container_metrics()`, `get_per_container_oom_kills()` |
| `opi/connectors/grafana_prometheus.py` | Add same methods |
| `opi/api/resource_router.py` | Add `include_sidecars` parameter to tune endpoint |
| `opi/manager/manifest_manager.py` | Add `get_sidecar_resources()` for template variable population |
| `manifests/sidecar-authorization-wall.yaml.jinja` | Replace hardcoded resources with template variables |
| `opi/handlers/project_file_handler.py` | Add `extract_sidecar_resources()`, `set_sidecar_resources()` methods |

### No New Files

All changes are modifications to existing files.

---

## Dependencies

- Existing resource tuning endpoint (implemented)
- `kube-state-metrics` deployed in cluster (for `kube_pod_container_info` and OOM kill metrics)
- Auto-resource-tuning feature (optional — sidecar tuning works independently via manual `/tune` endpoint)

## Verification

1. **Container discovery**: `get_per_container_metrics()` returns entries for both `app` and `authorization-wall`
2. **OOM detection per container**: Kill oauth2-proxy with memory pressure, verify OOM is detected for `authorization-wall` specifically
3. **Tune endpoint with sidecars**: `POST /api/resources/myproject/tune?include_sidecars=true` returns recommendations for all containers
4. **YAML override**: Set `sidecar-resources.authorization-wall.limits.memory: 128Mi` in project YAML, verify sidecar template receives the value
5. **Default fallback**: Without `sidecar-resources` in YAML, sidecar uses hardcoded template defaults

## Related

- `opi/api/resource_router.py` — existing tune endpoint
- `opi/connectors/prometheus.py` — metrics connector
- `manifests/sidecar-authorization-wall.yaml.jinja` — oauth2-proxy sidecar template
- `features/auto-resource-tuning.md` — auto-scaling feature (extends to sidecars in Phase 5)
