# Prometheus Monitoring

## What it is

Prometheus is deployed as part of the RIG cluster infrastructure to collect and query metrics from applications and cluster components. It provides:

- Automatic pod discovery via Kubernetes service discovery (local/sandbox) or static targets (ODCN)
- CPU, memory, and request metrics per component
- Operations Manager internal state tracking (memory, caches, connections, background tasks)
- Optional deep Python memory profiling via tracemalloc
- Integration with the Operations Manager dashboard for visualizing project metrics

## Getting Prometheus Running

### Prerequisites

- A running RIG cluster (local, sandboxed-local, or ODCN production)
- `kubectl` access to the cluster
- For ODCN: access to the secure LAN (`147.181.0.0/16`)

### Deployment per Environment

#### Local (Kind)

Prometheus is included in the local cluster bootstrap. Deploy infrastructure:

```bash
task deploy-infrastructure CLUSTER=local
```

Access the UI at `https://prometheus.kind` (add `127.0.0.1 prometheus.kind` to `/etc/hosts`).

#### Sandboxed-local

Prometheus is included in the sandboxed-local cluster bootstrap. Deploy infrastructure:

```bash
task deploy-infrastructure CLUSTER=sandboxed-local
```

Access the UI at `https://prometheus.sandbox.rijksapp.dev`.

#### ODCN Production

Prometheus is enabled in the ODCN cluster kustomization (`infrastructure/bootstrap/clusters/odcn/kustomization.yaml`). It uses Kubernetes service discovery via **Capsule Proxy** for dynamic pod discovery across tenant namespaces - no hardcoded namespace lists needed. See [Capsule Proxy Prometheus Discovery](capsule-proxy-prometheus-discovery.md) for details.

To deploy, apply the infrastructure manifests. The Prometheus UI is available at:

```
https://prometheus.rig.prd1.gn2.quattro.rijksapps.nl
```

Access is restricted to the ODCN secure LAN (`147.181.0.0/16`) via HAProxy IP whitelisting.

### Verifying Prometheus is Running

```bash
# Check the Prometheus pod is running
kubectl get pods -n <namespace> -l app=prometheus

# Check targets are being scraped (from inside the cluster)
kubectl exec -n <namespace> deployment/prometheus -- \
  wget -qO- http://localhost:9090/api/v1/targets | python3 -m json.tool

# Or via the UI: navigate to Status > Targets
```

### Enabling Operations Manager Metrics

The Operations Manager deployment already has Prometheus scrape annotations in the base:

```yaml
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/port: "8000"
  prometheus.io/path: "/metrics"
```

After deploying the Operations Manager, verify metrics are accessible:

```bash
# Port-forward to the Operations Manager
kubectl port-forward -n <namespace> deployment/operations-manager 8000:8000

# Fetch metrics
curl http://localhost:8000/metrics
```

### Enabling tracemalloc (Deep Memory Profiling)

Set the `ENABLE_TRACEMALLOC` environment variable to `true` in the Operations Manager deployment. This adds ~10-30% memory overhead but provides per-file Python allocation tracking.

In the operations-manager env secrets or config:
```
ENABLE_TRACEMALLOC=true
```

## PromQL Queries for Memory Analysis

### Process Memory (start here)

These use the built-in `process_*` metrics from `prometheus_client` - available out of the box.

| Query | What it shows |
|-------|---------------|
| `process_resident_memory_bytes{job="operations-manager"}` | Physical memory (RSS) - **the primary metric to watch** |
| `process_virtual_memory_bytes{job="operations-manager"}` | Virtual memory (includes mapped but unused pages) |
| `rate(process_cpu_seconds_total{job="operations-manager"}[5m])` | CPU usage rate |
| `process_open_fds{job="operations-manager"}` | Open file descriptors (leak indicator) |

#### Memory trend over time

```promql
# RSS memory over the last hour
process_resident_memory_bytes{job="operations-manager"}[1h]

# Rate of memory growth per minute (positive = growing, potential leak)
deriv(process_resident_memory_bytes{job="operations-manager"}[30m])

# Memory in human-readable MB
process_resident_memory_bytes{job="operations-manager"} / 1024 / 1024
```

### OPI Internal State Metrics

These custom metrics help identify which internal component is consuming memory.

| Query | What it shows |
|-------|---------------|
| `opi_projects_cached` | Number of projects held in the in-memory cache |
| `opi_task_projects_tracked` | Projects tracked by the task manager |
| `opi_task_managers_active` | Active TaskProgressManager instances |
| `opi_websocket_connections_global` | Active WebSocket connections |
| `opi_websocket_connections_users` | Distinct users with WebSocket connections |
| `opi_rate_limiter_tracked_clients` | Clients tracked by the rate limiter |
| `opi_background_tasks_active` | Active asyncio background tasks |
| `opi_gc_objects{generation="0"}` | Python GC gen-0 objects (short-lived) |
| `opi_gc_objects{generation="1"}` | Python GC gen-1 objects (survived one collection) |
| `opi_gc_objects{generation="2"}` | Python GC gen-2 objects (long-lived, potential leaks) |
| `opi_container_memory_bytes` | Total container memory from cgroup (Python + subprocesses) |
| `opi_child_process_count` | Number of active child processes |
| `opi_child_process_rss_bytes` | RSS per active child process (labeled by pid and command) |

#### Database connection pools

```promql
# Active connections per pool
opi_database_pool_active_connections

# Pool size per pool
opi_database_pool_size

# Connection utilization ratio
opi_database_pool_active_connections / opi_database_pool_size
```

### tracemalloc Queries (when ENABLE_TRACEMALLOC=true)

These show exactly which Python files are allocating the most memory.

```promql
# Top memory allocators by file (sorted by size in the UI table view)
sort_desc(opi_tracemalloc_alloc_bytes)

# Total tracked Python allocations
sum(opi_tracemalloc_alloc_bytes)

# Allocations from OPI code only (filter out stdlib/third-party)
opi_tracemalloc_alloc_bytes{file=~"opi/.*"}

# Growth of a specific file's allocations over time
opi_tracemalloc_alloc_bytes{file="opi/services/project_service.py"}[1h]
```

### Container Memory and Subprocess Tracking

The Operations Manager spawns subprocesses (git, kubectl, sops, age, psql, kopia, mc) that
consume memory outside the Python process. The built-in `process_resident_memory_bytes` only
tracks the Python process RSS - but Kubernetes counts **all** memory in the container's cgroup
when deciding to OOM kill.

These metrics bridge that gap:

| Query | What it shows |
|-------|---------------|
| `opi_container_memory_bytes{job="operations-manager"}` | Total container memory from cgroup (Python + all subprocesses) - **this is what Kubernetes measures for OOM** |
| `opi_child_process_rss_bytes{job="operations-manager"}` | RSS of each active child process (labeled by pid and command) |
| `opi_child_process_count{job="operations-manager"}` | Number of active child processes at scrape time |

#### Key queries for OOM investigation

```promql
# Total container memory in MB (what Kubernetes sees)
opi_container_memory_bytes{job="operations-manager"} / 1024 / 1024

# How close to OOM kill threshold (2Gi limit for ODCN production)
opi_container_memory_bytes{job="operations-manager"} / 2147483648

# Subprocess memory overhead (container total minus Python RSS)
(opi_container_memory_bytes{job="operations-manager"} - process_resident_memory_bytes{job="operations-manager"}) / 1024 / 1024

# Active child processes and their memory (check during heavy operations)
opi_child_process_rss_bytes{job="operations-manager"}

# Child process count over time (spikes = concurrent subprocess activity)
opi_child_process_count{job="operations-manager"}
```

**Note:** The child process scan runs at each Prometheus scrape (every 30s). Short-lived
subprocesses (< 30s) may not be captured individually, but their memory impact is always
reflected in `opi_container_memory_bytes`. The cgroup metric handles cgroups v2 and v1.

#### Known memory-heavy subprocesses

| Connector | Command | Risk |
|-----------|---------|------|
| `postgres.py` | pg_dump/psql (schema clone) | High - database dumps can be large |
| `git.py` | git clone/push | High - depends on repo size |
| `kopia.py` | kopia backup/restore | High - backup data in memory |
| `kubectl.py` | kubectl apply (large manifests) | Moderate |
| `sops.py` / `age.py` | sops/age encrypt/decrypt | Low |
| `minio_mc.py` | mc commands | Low-moderate |

### Diagnosing an OOM Kill

When the Operations Manager gets OOMKilled, use these queries to understand what happened leading up to the kill:

```promql
# 1. Was container memory approaching the limit? (start here)
opi_container_memory_bytes{job="operations-manager"} / 1024 / 1024

# 2. Was it the Python process or subprocesses?
process_resident_memory_bytes{job="operations-manager"} / 1024 / 1024

# 3. Subprocess overhead (if this is large, subprocesses caused the OOM)
(opi_container_memory_bytes{job="operations-manager"} - process_resident_memory_bytes{job="operations-manager"}) / 1024 / 1024

# 4. Were there many concurrent child processes?
opi_child_process_count{job="operations-manager"}

# 5. What internal structures were growing?
opi_projects_cached
opi_task_managers_active
opi_background_tasks_active
opi_websocket_connections_global

# 6. Were GC gen-2 objects accumulating? (objects that survive collection = potential leak)
opi_gc_objects{generation="2"}

# 7. Were database connections piling up?
opi_database_pool_active_connections

# 8. If tracemalloc was enabled, which files were the biggest allocators?
topk(10, opi_tracemalloc_alloc_bytes)
```

### Container-Level Metrics (local/sandbox only)

These come from cAdvisor and are available when Kubernetes service discovery is active (not on ODCN static config).

```promql
# Container memory usage
container_memory_usage_bytes{namespace="rig-system", container="operations-manager"}

# Container memory vs limit (how close to OOM)
container_memory_usage_bytes{container="operations-manager"}
  / container_spec_memory_limit_bytes{container="operations-manager"}

# Container CPU usage
rate(container_cpu_usage_seconds_total{container="operations-manager"}[5m])
```

## Architecture

```
infrastructure/bootstrap/infrastructure/prometheus/
├── controller/
│   ├── base/
│   │   ├── kustomization.yaml
│   │   ├── deployment.yaml            # Prometheus server (prom/prometheus:v2.54.1)
│   │   ├── service.yaml               # ClusterIP on port 9090
│   │   ├── configmap.yaml             # Full scrape config (k8s discovery + static)
│   │   ├── pvc.yaml                   # 10Gi storage, 7-day retention
│   │   ├── kube-state-metrics-deployment.yaml
│   │   └── kube-state-metrics-service.yaml
│   └── overlays/
│       ├── local/
│       │   ├── kustomization.yaml
│       │   └── ingress.yaml           # prometheus.kind with cert-manager
│       ├── sandboxed-local/
│       │   ├── kustomization.yaml
│       │   └── ingress.yaml           # prometheus.sandbox.rijksapp.dev
│       └── odcn/
│           ├── kustomization.yaml     # namespace: rig-prd-operations
│           ├── ingress.yaml           # HAProxy IP-whitelisted
│           ├── configmap-patch.yaml   # Capsule Proxy kubernetes_sd_configs
│           ├── role.yaml              # RBAC for kubernetes_sd_configs
│           └── rolebinding.yaml
```

### ODCN vs Local/Sandbox Differences

| Aspect | Local / Sandbox | ODCN Production |
|--------|----------------|-----------------|
| Scrape method | Kubernetes service discovery | Kubernetes service discovery via Capsule Proxy |
| K8s API access | Yes (namespace-manager SA) | Yes, via Capsule Proxy (namespace-manager SA) |
| Namespace | `rig-system` | `rig-prd-operations` |
| kube-state-metrics | Active | Scaled to 0 replicas |
| cAdvisor metrics | Yes | No |
| Ingress | nginx ingress | HAProxy with IP whitelist |
| Scrape targets | All annotated pods in multiple namespaces | All annotated pods in tenant namespaces |

## Operations Manager Metrics Endpoint

The `/metrics` endpoint is implemented in `opi/api/prometheus_router.py` and exposes metrics in Prometheus text format. The custom collectors in `opi/core/metrics.py` gather internal state at each scrape request (no background polling).

### Adding New Metrics

To add a new metric to the Operations Manager:

1. Edit `opi/core/metrics.py`
2. Add a new `GaugeMetricFamily` in `OPICollector.collect()`
3. Import and read the relevant internal state
4. The metric will be available at `/metrics` immediately on next scrape

Example:

```python
# In OPICollector.collect():
my_gauge = GaugeMetricFamily(
    "opi_my_new_metric",
    "Description of what this measures",
)
try:
    from opi.some_module import some_state
    my_gauge.add_metric([], len(some_state))
except Exception:
    logger.debug("Failed to collect my_new_metric", exc_info=True)
yield my_gauge
```

## Dependencies

- `prometheus_client` Python library (in Operations Manager)
- `namespace-manager` ServiceAccount with pod/service/endpoint read access (local/sandbox)
- Persistent volume for Prometheus data storage (7-day retention, 10Gi)
- nginx ingress controller (local/sandbox) or HAProxy (ODCN) for external access

## Troubleshooting

### Prometheus not scraping Operations Manager

1. Check pod annotations are present:
   ```bash
   kubectl get pod -n <namespace> -l app=operations-manager -o jsonpath='{.items[0].metadata.annotations}'
   ```

2. Verify the `/metrics` endpoint responds:
   ```bash
   kubectl exec -n <namespace> deployment/prometheus -- \
     wget -qO- http://operations-manager:8000/metrics | head -20
   ```

3. Check Prometheus targets page for errors:
   Navigate to the Prometheus UI > Status > Targets

### Metrics return empty values

- Custom metrics only populate when the relevant modules are imported and initialized
- If the Operations Manager just started, some metrics may be zero until the first project loads
- Check Operations Manager logs for `Registered OPI Prometheus metrics collector`

### tracemalloc metrics not appearing

- Verify `ENABLE_TRACEMALLOC=true` is set in the environment
- Check logs for `Tracemalloc started and collector registered`
- Note: tracemalloc metrics only appear if tracing is active; they won't show up with `ENABLE_TRACEMALLOC=false`

### ODCN Prometheus UI not accessible

- Verify you're on the secure LAN (`147.181.0.0/16`)
- Check the ingress is created: `kubectl get ingress -n rig-prd-operations prometheus`
- Check the route is active in OpenShift/HAProxy
