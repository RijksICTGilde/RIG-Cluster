# Metrics Explorer

**Status**: Implemented

## What It Is

A self-service metrics exploration tool at `/metrics-explorer` that lets authenticated users browse Prometheus metrics for predefined infrastructure services. Users select a service and metric from dropdowns, and the tool renders the Prometheus graph UI in an embedded iframe.

## How It Works

### Architecture

The metrics explorer uses a simple two-step flow:

1. User selects a service from a dropdown
2. Backend returns available metric names for that service via the Prometheus series API
3. User selects a metric - Prometheus graph UI loads in an iframe

All metric discovery happens server-side via the `PrometheusConnector.discover_metric_names()` method, which queries the `/api/v1/series` endpoint (not PromQL instant queries). This is essential for services with long scrape intervals (2h) where PromQL instant queries would return empty due to the 5-minute staleness window.

### Monitored Services

Six infrastructure services are hardcoded in the router:

| Service | Prometheus Job | Scrape Interval | Default Range |
|---------|---------------|-----------------|---------------|
| PostgreSQL (rig-db) | `cloudnative-pg` | 2h | 7d |
| MinIO | `minio` | 2h | 7d |
| Keycloak | `keycloak-rig-metrics` | 2h | 7d |
| Operations Manager | `operations-manager` | default | 1h |
| Kubernetes Pods | `kubernetes-pods` | default | 1h |
| Prometheus | `prometheus` | default | 1h |

Each service definition includes an `id`, `name`, `description`, `match` (Prometheus label selector like `{job="minio"}`), and `range`.

### Routes

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/metrics-explorer` | Main page with service/metric dropdowns and iframe |
| GET | `/api/metrics-explorer/metrics/{service_id}` | Returns available metric names for a service (JSON) |

Both endpoints require SSO authentication (`@requires_sso`).

### UI Flow

1. User selects a service from the dropdown
2. JavaScript fetches metric names from `/api/metrics-explorer/metrics/{service_id}`
3. Metric dropdown is populated (with filter input when >10 metrics)
4. User selects a metric
5. Prometheus graph URL is constructed and loaded in a sandboxed iframe
6. A "Open in new tab" link is shown for standalone Prometheus access

### Discovery Mechanism

Uses the Prometheus `/api/v1/series` endpoint:

```
GET /api/v1/series?match[]={job="minio"}
```

Extracts unique `__name__` values from all returned series and returns a sorted list of metric names.

## Configuration

Uses the existing `PROMETHEUS_URL` setting - no additional configuration needed.

## Key Files

| File | Purpose |
|------|---------|
| `opi/web/metrics_explorer_router.py` | Routes + `MONITORED_SERVICES` definitions |
| `opi/templates/metrics-explorer.html.j2` | Template with dropdowns, filter, iframe |
| `opi/connectors/prometheus.py` | `discover_metric_names()` via series API |

## Troubleshooting

### No metrics appear for a service

- Verify the Prometheus job label matches (e.g., `cloudnative-pg`, not `postgresql`)
- For 2h-scrape services, ensure data exists within the last 7 days
- Check Prometheus `/api/v1/series?match[]={job="service-name"}` directly

### Iframe shows "No data points found"

- The metric may have no data in the selected time range
- Try a metric that is always present (e.g., `up` for the service's job)

### Metrics count shows 0

- The service may not be scraping correctly - check Prometheus targets page
- For long-interval scrapes (2h), it may take up to 2 hours for initial data to appear
