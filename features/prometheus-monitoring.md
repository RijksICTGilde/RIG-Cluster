# Prometheus Monitoring

## What it is

Prometheus is deployed as part of the RIG cluster infrastructure to collect and query metrics from applications and cluster components. It provides:

- Automatic pod discovery via Kubernetes service discovery
- CPU, memory, and request metrics per component
- Integration with the Operations Manager dashboard for visualizing project metrics

## How it works

### Service Discovery

Prometheus automatically discovers pods that have the following annotations:

```yaml
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/port: "8000"      # Port where metrics are exposed
  prometheus.io/path: "/metrics"  # Path to metrics endpoint (default: /metrics)
```

The Operations Manager automatically adds these annotations to all deployment manifests.

### RBAC

Prometheus uses the `namespace-manager` service account which has cluster-wide read access to pods, services, and endpoints. This allows it to discover and scrape pods in all project namespaces without needing additional RBAC configuration per namespace.

## Configuration

### Project-level metrics configuration

Components can optionally specify custom metrics port and path in `project.yaml`:

```yaml
components:
  - name: my-app
    metrics:
      port: 9090      # Custom metrics port (default: application port)
      path: /custom   # Custom metrics path (default: /metrics)
```

If not specified, defaults are used:
- `port`: Same as the component's application port
- `path`: `/metrics`

## Architecture

```
infrastructure/bootstrap/infrastructure/prometheus/
├── controller/
│   ├── base/
│   │   ├── deployment.yaml      # Prometheus server
│   │   ├── service.yaml         # ClusterIP service
│   │   ├── ingress.yaml         # External access at prometheus.kind
│   │   ├── configmap.yaml       # Scrape configuration
│   │   ├── pvc.yaml             # 7-day data retention storage
│   │   ├── kube-state-metrics-deployment.yaml
│   │   └── kube-state-metrics-service.yaml
│   └── overlays/
│       ├── local/
│       │   └── kustomization.yaml
│       └── odcn-production/
│           └── kustomization.yaml
```

### Components

| Component | Purpose |
|-----------|---------|
| Prometheus | Metrics collection and querying |
| kube-state-metrics | Kubernetes object state metrics (pods, deployments, etc.) |

## Operations Manager Integration

The Operations Manager includes:

1. **Prometheus Connector** (`opi/connectors/prometheus.py`): Python client for querying Prometheus
2. **Metrics API** (`/api/metrics/*`): REST endpoints for retrieving metrics
3. **Dashboard Gauges**: Visual display of CPU, memory, and request metrics per component

### API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/metrics/health` | Check Prometheus connection status |
| `GET /api/metrics/overview` | Cluster-wide metrics overview |
| `GET /api/metrics/cpu?namespace=X` | CPU usage by pod |
| `GET /api/metrics/memory?namespace=X` | Memory usage by pod |
| `GET /api/metrics/query?query=X` | Execute custom PromQL query |

## Access

- **Internal**: `http://prometheus.rig-system:9090`
- **External** (local): `https://prometheus.kind`

Add to `/etc/hosts` for local access:
```
127.0.0.1 prometheus.kind
```

## Metrics Available

### From cAdvisor (container metrics)

| Metric | Description |
|--------|-------------|
| `container_cpu_usage_seconds_total` | CPU usage in seconds |
| `container_memory_usage_bytes` | Memory usage in bytes |
| `container_network_receive_bytes_total` | Network received bytes |
| `container_network_transmit_bytes_total` | Network transmitted bytes |

### From kube-state-metrics

| Metric | Description |
|--------|-------------|
| `kube_pod_info` | Pod metadata |
| `kube_pod_status_phase` | Pod phase (Pending, Running, etc.) |
| `kube_pod_container_status_restarts_total` | Container restart count |
| `kube_deployment_status_replicas` | Deployment replica counts |

### From application pods

Applications exposing a `/metrics` endpoint (e.g., using Prometheus client libraries) will have their custom metrics scraped automatically.

## Dependencies

- `namespace-manager` ClusterRole with pod/service/endpoint read access
- Persistent volume for data storage (7-day retention)
- nginx ingress controller for external access

## Troubleshooting

### Prometheus not discovering pods

1. Check pod annotations:
   ```bash
   kubectl get pod -n <namespace> -o yaml | grep -A 5 "annotations:"
   ```

2. Check Prometheus targets:
   ```bash
   kubectl exec -n rig-system deployment/prometheus -- wget -qO- http://localhost:9090/api/v1/targets
   ```

3. Verify namespace-manager ClusterRole has pod read access:
   ```bash
   kubectl get clusterrole namespace-manager -o yaml
   ```

### Metrics not showing in Operations Manager

1. Check Prometheus connection:
   ```bash
   curl http://localhost:9595/api/metrics/health
   ```

2. Check Operations Manager logs for Prometheus connection errors
