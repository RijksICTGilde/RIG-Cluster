# HTMX Lazy-Loading for Deployment Metrics

## What it is

The project details page now lazy-loads deployment metrics via HTMX instead of fetching all Prometheus data synchronously during page render. This makes the page load significantly faster since expensive Prometheus queries no longer block the initial render.

## How it works

1. **Page load**: The metrics card renders immediately with a "Metrics laden..." placeholder
2. **HTMX trigger**: `hx-trigger="load"` fires an async GET request to fetch metrics for the first deployment on the current cluster
3. **Deployment switch**: sinds RC-92 toont het tabblad Metrics EEN deployment per pagina (`/projects/metrics/<project>/<naam>`). Een andere kiezen is navigeren; het fragment van die deployment laadt dan op de nieuwe pagina. `switchDeployment()` bestaat niet meer
4. **Chart initialization**: The fragment includes a `<script>` tag that calls `initMetricsCharts()` after each HTMX swap to initialize Chart.js canvases

## Architecture

### Files

| File | Purpose |
|------|---------|
| `opi/templates/partials/deployment_metrics.html.j2` | Standalone HTML fragment (no `{% extends %}`) containing component charts, helm workload charts, and PVC storage |
| `opi/templates/project-details.html.j2` | Main page with HTMX placeholder `div#metrics-content` |
| `opi/web/router.py` | New endpoint `GET /projects/details/{project_name}/metrics/{deployment_name}` |

### Endpoint

```
GET /projects/details/{project_name}/metrics/{deployment_name}
```

- Protected by `@requires_sso` and project-level access check
- Queries Prometheus for a single deployment (components or helm workloads + PVC storage)
- Returns an HTML fragment (no full page layout)

### Template context

The fragment receives:
- `deployment` - object with `.name` and `.components` attributes
- `metrics` - `dict[component_name, metrics_data]` scoped to this deployment
- `discovered_workloads` - `list[dict]` for helm-based deployments
- `pvc_storage` - `dict[pvc_name, pvc_data]`

## Single deployment behavior

When only one deployment exists, the global selector is hidden but metrics still lazy-load. The UX is identical, just without a dropdown.

## Dependencies

- HTMX (loaded by the base template/component library)
- Chart.js + chartjs-plugin-annotation (loaded in `project-details.html.j2` additional_scripts block)
- Prometheus connector (`opi.connectors.prometheus`)
