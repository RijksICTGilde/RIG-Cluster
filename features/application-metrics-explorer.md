# Application Metrics Explorer

## What It Is

A self-service metrics exploration feature in the Operations Manager UI that lets users discover and visualize application-specific metrics exposed by their deployments. Instead of needing to know PromQL or navigate Grafana, users select metrics from a dropdown populated by what their application actually exposes, choose a time range, and get an instant chart.

This feature also addresses two existing UX issues:
- **PVC display per deployment**: PVCs are currently listed as a separate global section for the entire namespace, instead of being shown as part of each deployment's own metrics
- **Full page refreshes**: Metric and PVC interactions currently require full page reloads instead of partial updates

## Metrics Source Architecture

### The Problem

Applications expose custom metrics via `/metrics` endpoints (request latency histograms, queue depths, cache hit rates, business counters, etc.). These are scraped by Prometheus and available for querying — but users don't know what metrics exist or how to query them.

Additionally, the metrics sources differ per environment:

| Environment | Infrastructure metrics | Application metrics |
|---|---|---|
| **Sandboxed-local** | Local Prometheus | Same local Prometheus |
| **ODCN Production** | Grafana (central Mimir/Prometheus) | Own RIG Prometheus |

The current `METRICS_BACKEND` setting is a single toggle (`"prometheus"` or `"grafana"`), treating all metrics as coming from one source. This works for infrastructure metrics but breaks down when application metrics live in a different backend than infrastructure metrics.

### Proposed Source Routing

Introduce a **dual-connector model** where the system can query different backends depending on the metric category:

```
METRICS_BACKEND=prometheus          # Infrastructure metrics source (existing)
APP_METRICS_BACKEND=prometheus      # Application metrics source (new)
APP_METRICS_PROMETHEUS_URL=http://prometheus.rig-system:9090  # (new, defaults to PROMETHEUS_URL)
```

**Routing logic:**

- **Infrastructure metrics** (CPU, memory, network, disk, pod restarts) — use `METRICS_BACKEND` as today (Grafana in ODCN, Prometheus in sandbox)
- **Application metrics** (custom metrics from app's `/metrics` endpoint) — always use `APP_METRICS_BACKEND`, which points to the RIG-owned Prometheus in both environments

In the sandboxed-local case, both point to the same Prometheus instance, so there is zero behavioral difference. In ODCN production, infra queries go through Grafana while application metric queries go directly to the local Prometheus.

**Why not just use Grafana for everything in production?**
Application metrics are scraped by the RIG-owned Prometheus, not the central ODCN Mimir. They are simply not available through Grafana. The central system handles cluster-wide infrastructure metrics; per-application custom metrics stay local.

## Feature: Metrics Discovery and Selection

### How It Works

1. User navigates to a deployment's detail view
2. The system queries Prometheus for all metric names associated with that deployment's pods
3. Metrics are presented in a searchable dropdown, grouped by type when possible
4. User selects a metric — a chart renders immediately via HTMX (no full page refresh)

### Discovery Mechanism

Query all metric names for pods matching the deployment:

```promql
# Get all metric names for pods matching a prefix
group({namespace="project-ns", pod=~"my-deployment-.*"}) by (__name__)
```

This returns every metric name that has data for the deployment's pods. The result is filtered to exclude:
- Internal Prometheus metrics (`scrape_*`, `up`)
- Infrastructure metrics already shown in the standard charts (CPU, memory, network, disk)

### Metric Type Detection

Where possible, infer the metric type from naming conventions to choose the right visualization:

| Suffix pattern | Likely type | Default visualization |
|---|---|---|
| `_total` | Counter | Rate line chart (`rate(metric[interval])`) |
| `_bucket` | Histogram | Heatmap or percentile lines |
| `_sum` / `_count` | Summary | Rate line chart |
| `_info` | Info | Table/label display |
| *(none of the above)* | Gauge | Direct value line chart |

This is a heuristic, not a guarantee — but it covers the vast majority of well-instrumented applications following Prometheus naming conventions.

### UI: Deployment Metrics Panel

```
+--------------------------------------------------+
| Deployment: my-app                               |
|                                                  |
| [Standard Metrics]                               |
|  [CPU] [Memory] [Network] [Disk] [Storage]       |
|  (existing charts, now including PVC storage)    |
|                                                  |
| [Application Metrics]                            |
|  Metric: [ http_requests_total          v ]      |
|  Time:   [ Last 1 hour                  v ]      |
|                                                  |
|  +----------------------------------------------+|
|  |  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~                ||
|  |  ~~~  Chart.js line chart  ~~~               ||
|  |  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~                ||
|  +----------------------------------------------+|
+--------------------------------------------------+
```

- **Metric dropdown**: Searchable `<select>` populated from discovery, grouped by prefix (e.g. `http_*`, `db_*`, `jvm_*`)
- **Time range dropdown**: Predefined ranges matching Prometheus conventions:
  - Last 5 minutes
  - Last 15 minutes
  - Last 1 hour (default)
  - Last 6 hours
  - Last 24 hours
  - Last 7 days

Both dropdowns trigger HTMX requests on change — no submit button needed.

## Feature: PVC Display Per Deployment

### The Problem

PVCs are currently shown as a separate, flat list for the entire namespace. This means when a user is looking at a specific deployment (e.g., their PostgreSQL StatefulSet), they see PVCs from every deployment in the namespace — including unrelated ones from MinIO, Redis, etc. PVC storage is a per-deployment concern and should be displayed alongside that deployment's other metrics (CPU, memory, network).

### The Fix

Move PVC storage charts **into each deployment's metrics panel** as another metric alongside CPU, memory, network, and disk. Each deployment only shows its own PVCs.

**Before (current):**
```
Deployment: postgres     [CPU] [Memory] [Network] [Disk]
Deployment: minio        [CPU] [Memory] [Network] [Disk]
Deployment: redis        [CPU] [Memory] [Network] [Disk]

PVC Storage (all):       [postgres-data] [minio-data] [redis-data]  <-- flat list, no context
```

**After (proposed):**
```
Deployment: postgres     [CPU] [Memory] [Network] [Disk] [Storage: postgres-data]
Deployment: minio        [CPU] [Memory] [Network] [Disk] [Storage: minio-data]
Deployment: redis        [CPU] [Memory] [Network] [Disk] [Storage: redis-data]
```

**Matching PVCs to deployments:** Prometheus exposes the `persistentvolumeclaim` label on `kubelet_volume_stats_*` metrics. Cross-reference with pod volume mounts to map each PVC to its owning workload:

```promql
# Get PVC usage for pods matching a specific deployment
kubelet_volume_stats_used_bytes{namespace="project-ns", pod=~"postgres-.*"}
kubelet_volume_stats_capacity_bytes{namespace="project-ns", pod=~"postgres-.*"}
```

The `pod` label on `kubelet_volume_stats_*` directly identifies which pod (and thus which deployment) owns the PVC. No complex join needed — just filter by the same pod prefix used for other metrics.

**Deployments with no PVCs** simply don't show a storage chart — no empty state needed.

## Feature: HTMX-Driven Interactions

### Why HTMX

The current project details page renders all metrics server-side on full page load. Any interaction (changing time range, selecting a different component) requires a full page refresh. HTMX enables partial page updates with minimal JavaScript.

The codebase already has HTMX referenced in the template engine (`htmx=True` in `templates.py`) but it is not actively used.

### HTMX Patterns

#### Metric Selection

```html
<!-- Metric dropdown triggers partial update -->
<select name="metric"
        hx-get="/web/projects/details/{project}/metrics-chart"
        hx-target="#app-metrics-chart"
        hx-include="[name='time_range']"
        hx-swap="innerHTML">
  <option value="http_requests_total">http_requests_total</option>
  ...
</select>

<!-- Time range dropdown also triggers update -->
<select name="time_range"
        hx-get="/web/projects/details/{project}/metrics-chart"
        hx-target="#app-metrics-chart"
        hx-include="[name='metric']"
        hx-swap="innerHTML">
  <option value="5m">Last 5 minutes</option>
  <option value="1h" selected>Last 1 hour</option>
  ...
</select>

<!-- Chart container swapped by HTMX -->
<div id="app-metrics-chart">
  <!-- Server renders Chart.js canvas + data here -->
</div>
```

### New API Endpoints (Web Router)

These return **HTML fragments**, not JSON — designed for HTMX consumption:

| Endpoint | Purpose |
|---|---|
| `GET /web/projects/details/{project}/app-metrics?deployment=X` | Discover available metrics for a deployment, return dropdown + initial chart |
| `GET /web/projects/details/{project}/metrics-chart?deployment=X&metric=Y&range=Z` | Render a single metric chart for given metric name + time range |

### Existing JSON API Additions

For the metrics discovery and querying, add to the existing `metrics_router`:

| Endpoint | Purpose |
|---|---|
| `GET /api/metrics/discover?namespace=X&pod_prefix=Y` | Return list of available metric names for a deployment |
| `GET /api/metrics/timeseries?metric=X&namespace=Y&pod_prefix=Z&range=1h` | Return time-series data for a specific metric |

## Implementation Approach

### Phase 1: PVC into Deployment Metrics
- Move PVC storage charts from the global namespace list into each deployment's metrics panel
- Filter PVC queries by pod prefix (same as CPU/memory) so each deployment only shows its own PVCs
- Remove the separate PVC section from the page
- Lowest risk, immediate UX improvement

### Phase 2: HTMX Infrastructure
- Verify HTMX is loaded in the base template (CDN or bundled)
- Create template partials for metrics chart rendering
- Add web router endpoints that return HTML fragments
- Refactor existing time-range for infrastructure metrics to use HTMX

### Phase 3: Application Metrics Discovery
- Add `discover_metrics_for_deployment()` to Prometheus connector
- Add metric name grouping and type detection logic
- Create searchable dropdown UI component
- Wire up HTMX for metric selection and chart rendering

### Phase 4: Multi-Source Configuration (ODCN)
- Add `APP_METRICS_BACKEND` and `APP_METRICS_PROMETHEUS_URL` config
- Create `get_app_metrics_connector()` factory alongside existing `get_metrics_connector()`
- Route application metric queries through the app connector
- Test with split-source topology

## Dependencies

- **Prometheus** must be scraping the application's pods (already the case — per-application scrapes are configured)
- **Chart.js** is already included in the project details template
- **HTMX** library needs to be verified as loaded (reference exists but may not be active)
- **RVO Roos design system** — dropdowns and selects should follow existing component patterns

## Configuration

```python
# Existing (unchanged)
METRICS_BACKEND: str = "prometheus"
PROMETHEUS_URL: str = "http://prometheus.rig-system:9090"
GRAFANA_URL: str = "http://grafana-service.rig-system.svc.cluster.local:3000"
GRAFANA_TOKEN: str | None = None
GRAFANA_DATASOURCE_UID: str | None = None

# New
APP_METRICS_BACKEND: str = "prometheus"  # Application metrics always from Prometheus
APP_METRICS_PROMETHEUS_URL: str | None = None  # Defaults to PROMETHEUS_URL if not set
```

For sandboxed-local, no additional config is needed — both default to the same Prometheus.

For ODCN production:
```
METRICS_BACKEND=grafana
GRAFANA_URL=https://grafana.odcn.example
GRAFANA_TOKEN=<token>
APP_METRICS_BACKEND=prometheus
APP_METRICS_PROMETHEUS_URL=http://prometheus.rig-system:9090
```

## Troubleshooting

### No metrics appear in the dropdown
- Verify the application pod exposes a `/metrics` endpoint
- Check that Prometheus is scraping the pod: query `up{namespace="project-ns", pod=~"deployment-.*"}` should return `1`
- Check Prometheus targets page for scrape errors

### Metrics appear but chart is empty
- The metric may have no data in the selected time range — try "Last 24 hours"
- Counter metrics need `rate()` to be useful — type detection should handle this, but verify the suffix heuristic matched

### PVC storage not showing for a deployment
- Verify the deployment's pods actually have PVCs mounted (StatefulSets typically do, Deployments may not)
- Check that `kubelet_volume_stats_used_bytes{pod=~"deployment-prefix-.*"}` returns data in Prometheus
- Deployments without PVCs correctly show no storage chart

### ODCN: Application metrics not loading
- Verify `APP_METRICS_PROMETHEUS_URL` points to the RIG-owned Prometheus, not the central Grafana
- Check network connectivity from Operations Manager pod to the Prometheus service
