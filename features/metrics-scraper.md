# Metrics Scraper Service

## Overview

The **Metrics Scraper** service enables Prometheus to automatically discover and scrape metrics endpoints from your application components. When enabled, it adds Prometheus scraping annotations to your pod, allowing the Prometheus instance in your cluster to monitor your application's metrics.

This is an **opt-in service** — metrics scraping is only enabled for components that explicitly enable this service. Components without this service will not be scraped by Prometheus.

## How to Use

### Enable for a Component

Add the `metrics-scraper` service to your component's services list in `project.yaml`:

```yaml
components:
  - name: my-app
    reference: my-component
    services:
      - publish-on-web
      - metrics-scraper
```

### Configure Port and Path

By default, Prometheus will scrape metrics at:
- **Port**: Your component's application port (as specified in the component definition)
- **Path**: `/metrics`

To use a custom port or path, add configuration to the service:

```yaml
components:
  - name: my-app
    reference: my-component
    services:
      - publish-on-web
      - metrics-scraper:
          port: 9090
          path: /prometheus
```

## Configuration Options

| Option | Default | Description |
|--------|---------|-------------|
| `port` | Application port | The port where metrics are exposed |
| `path` | `/metrics` | The HTTP path where metrics endpoint is available |

## What Gets Created

When you enable the metrics-scraper service, the following Prometheus annotations are added to your pod template:

```yaml
prometheus.io/scrape: "true"
prometheus.io/port: "8080"        # or your custom port
prometheus.io/path: "/metrics"    # or your custom path
```

These annotations tell Prometheus to:
1. Include this pod in scrape targets
2. Scrape metrics on the specified port and path
3. Use the pod's IP address as the scrape endpoint

## Examples

### Simple Web Application

```yaml
components:
  - name: api-server
    reference: django-app
    image: my-registry.com/api-server:1.0
    services:
      - publish-on-web
      - metrics-scraper
```

This will scrape metrics from `http://<pod-ip>:8080/metrics` (assuming your Django app runs on port 8080).

### Application with Custom Metrics Endpoint

```yaml
components:
  - name: worker
    reference: celery-worker
    image: my-registry.com/worker:1.0
    services:
      - metrics-scraper:
          port: 9100
          path: /prom
```

This will scrape metrics from `http://<pod-ip>:9100/prom`.

## Application Requirements

For metrics scraping to work, your application must:

1. **Expose metrics endpoint**: Expose Prometheus-format metrics on the configured port and path
2. **Use standard format**: Metrics should be in Prometheus text format (application/openmetrics-text)
3. **Be reliable**: The metrics endpoint should respond consistently to HTTP GET requests

### Example Application Endpoints

**Python (Prometheus client)**:
```python
from prometheus_client import make_wsgi_app
from werkzeug.serving import run_simple

app = make_wsgi_app()
run_simple("0.0.0.0", 8000, app, threaded=True)
```

**Go (Prometheus client)**:
```go
import "github.com/prometheus/client_golang/prometheus/promhttp"

http.Handle("/metrics", promhttp.Handler())
http.ListenAndServe(":8000", nil)
```

**Node.js (Prometheus client)**:
```javascript
const client = require('prom-client');
const express = require('express');

const app = express();
app.get('/metrics', (req, res) => {
  res.set('Content-Type', client.register.contentType);
  res.end(client.register.metrics());
});
```

## Troubleshooting

### Metrics Not Scraping

1. **Check pod annotations**:
   ```bash
   kubectl get pods -n <namespace> -o yaml | grep prometheus.io
   ```
   Should show the `prometheus.io/scrape`, `prometheus.io/port`, and `prometheus.io/path` annotations.

2. **Verify endpoint is accessible**:
   ```bash
   kubectl port-forward -n <namespace> pod/<pod-name> 8080:<port>
   curl http://localhost:8080<path>
   ```
   Should return metrics in Prometheus text format.

3. **Check Prometheus configuration**:
   - Verify Prometheus is configured to scrape pods with annotations
   - Check Prometheus targets page in the UI to see if your pod is discovered

### 404 Responses

If your metrics endpoint returns 404:
- Verify the `path` configuration matches your application's actual metrics endpoint
- Ensure your application is running and listening on the configured `port`
- Check application logs for startup errors

### Port Conflicts

If you configure a non-standard port:
- Ensure your application actually exposes metrics on that port
- Make sure the port number matches between your service configuration and your application

## Disabling Metrics Scraping

To stop scraping metrics for a component, simply remove the `metrics-scraper` service:

```yaml
components:
  - name: my-app
    reference: my-component
    services:
      - publish-on-web
      # metrics-scraper removed — no longer scraped
```

## Dependencies

The metrics-scraper service has no dependencies on other services — it works independently.

## See Also

- Prometheus documentation: https://prometheus.io/docs/prometheus/latest/configuration/configuration/#scrape_config
- Prometheus Kubernetes SD: https://prometheus.io/docs/prometheus/latest/configuration/configuration/#kubernetes_sd_config
