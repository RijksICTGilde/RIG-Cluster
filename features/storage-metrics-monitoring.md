# Storage Metrics Monitoring

This feature enables monitoring of storage usage for PostgreSQL databases and MinIO buckets via Prometheus, allowing you to track sizes, monitor growth, and identify systems needing migration.

## Overview

| Component | Metrics Endpoint | Port | Key Metric |
|-----------|-----------------|------|------------|
| PostgreSQL (CNPG) | `/metrics` | 9187 | `cnpg_pg_database_size_bytes` |
| MinIO | `/minio/v2/metrics/cluster` | 9000 | `minio_bucket_usage_total_bytes` |

Both components are scraped by Prometheus via:
1. Pod annotations (`prometheus.io/scrape: "true"`)
2. Dedicated scrape jobs in the Prometheus ConfigMap (fallback)

## PostgreSQL Database Metrics

### Available Metrics

| Metric | Description |
|--------|-------------|
| `cnpg_pg_database_size_bytes` | Size of each database in bytes |
| `cnpg_pg_stat_database_tup_inserted` | Rows inserted (growth indicator) |
| `cnpg_pg_stat_database_tup_updated` | Rows updated |
| `cnpg_pg_stat_database_tup_deleted` | Rows deleted |
| `cnpg_pg_database_xid_age` | Transaction ID age |
| `cnpg_pg_replication_lag_seconds` | Replication lag (if replicas exist) |

### Prometheus Queries

**List all database sizes (human-readable):**
```promql
cnpg_pg_database_size_bytes
```

**Database sizes sorted by size (top 10 largest):**
```promql
topk(10, cnpg_pg_database_size_bytes)
```

**Size of a specific database:**
```promql
cnpg_pg_database_size_bytes{datname="keycloak"}
```

**Database sizes in MB:**
```promql
cnpg_pg_database_size_bytes / 1024 / 1024
```

**Database sizes in GB:**
```promql
cnpg_pg_database_size_bytes / 1024 / 1024 / 1024
```

**Growth over the last 24 hours (bytes):**
```promql
delta(cnpg_pg_database_size_bytes[24h])
```

**Growth for a specific database over 24 hours:**
```promql
delta(cnpg_pg_database_size_bytes{datname="keycloak"}[24h])
```

**Growth per second (linear regression):**
```promql
deriv(cnpg_pg_database_size_bytes[24h])
```

**Databases larger than 100MB:**
```promql
cnpg_pg_database_size_bytes > 100 * 1024 * 1024
```

**Total storage used by all databases:**
```promql
sum(cnpg_pg_database_size_bytes)
```

### Accessing PostgreSQL Metrics Directly

```bash
# Port-forward to the CNPG pod (metrics are on port 9187)
kubectl port-forward -n rig-system pod/rig-db-1 9187:9187

# Query metrics
curl http://localhost:9187/metrics | grep cnpg_pg_database_size_bytes
```

## MinIO Bucket Metrics

### Available Metrics

| Metric | Description |
|--------|-------------|
| `minio_bucket_usage_total_bytes` | Total size of each bucket in bytes |
| `minio_bucket_usage_object_total` | Number of objects in each bucket |
| `minio_cluster_capacity_usable_total_bytes` | Total usable cluster capacity |
| `minio_cluster_capacity_usable_free_bytes` | Free usable capacity |
| `minio_node_disk_used_bytes` | Disk space used |
| `minio_node_disk_free_bytes` | Disk space free |

### Prometheus Queries

**List all bucket sizes:**
```promql
minio_bucket_usage_total_bytes
```

**Bucket sizes sorted by size (top 10 largest):**
```promql
topk(10, minio_bucket_usage_total_bytes)
```

**Size of a specific bucket:**
```promql
minio_bucket_usage_total_bytes{bucket="my-bucket-name"}
```

**Bucket sizes in MB:**
```promql
minio_bucket_usage_total_bytes / 1024 / 1024
```

**Bucket sizes in GB:**
```promql
minio_bucket_usage_total_bytes / 1024 / 1024 / 1024
```

**Object count per bucket:**
```promql
minio_bucket_usage_object_total
```

**Object count for a specific bucket:**
```promql
minio_bucket_usage_object_total{bucket="my-bucket-name"}
```

**Growth over the last 24 hours (bytes):**
```promql
delta(minio_bucket_usage_total_bytes[24h])
```

**Buckets larger than 1GB:**
```promql
minio_bucket_usage_total_bytes > 1024 * 1024 * 1024
```

**Total storage used across all buckets:**
```promql
sum(minio_bucket_usage_total_bytes)
```

**Storage utilization percentage:**
```promql
(1 - (minio_cluster_capacity_usable_free_bytes / minio_cluster_capacity_usable_total_bytes)) * 100
```

### Accessing MinIO Metrics Directly

```bash
# Port-forward to MinIO (metrics are on the API port 9000)
kubectl port-forward -n rig-system svc/minio 9000:9000

# Query metrics
curl http://localhost:9000/minio/v2/metrics/cluster | grep minio_bucket_usage
```

## Accessing Prometheus UI

```bash
# Port-forward to Prometheus
kubectl port-forward -n rig-system svc/prometheus 9090:9090

# Open in browser: http://localhost:9090
# Or query via API:
curl 'http://localhost:9090/api/v1/query?query=cnpg_pg_database_size_bytes'
```

## Configuration

### PostgreSQL (CNPG)

Metrics are enabled via `inheritedMetadata` in the Cluster spec:

```yaml
# infrastructure/bootstrap/infrastructure/postgresql/database/base/cluster.yaml
spec:
  inheritedMetadata:
    annotations:
      prometheus.io/scrape: "true"
      prometheus.io/port: "9187"
      prometheus.io/path: "/metrics"
```

### MinIO

Metrics are enabled via pod annotations and environment variable:

```yaml
# infrastructure/bootstrap/infrastructure/minio/controller/base/deployment.yaml
spec:
  template:
    metadata:
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "9000"
        prometheus.io/path: "/minio/v2/metrics/cluster"
    spec:
      containers:
      - name: minio
        env:
        - name: MINIO_PROMETHEUS_AUTH_TYPE
          value: "public"  # Required for unauthenticated metrics access
```

Note: By default, MinIO requires authentication for the metrics endpoint. Setting `MINIO_PROMETHEUS_AUTH_TYPE=public` allows Prometheus to scrape without a bearer token.

### Prometheus Scrape Jobs

Dedicated scrape jobs are configured in the Prometheus ConfigMap:

```yaml
# infrastructure/bootstrap/infrastructure/prometheus/controller/base/configmap.yaml

# CloudNativePG job (pod discovery)
- job_name: 'cloudnative-pg'
  kubernetes_sd_configs:
    - role: pod
      namespaces:
        names:
          - rig-system
  relabel_configs:
    - source_labels: [__meta_kubernetes_pod_label_cnpg_io_cluster]
      action: keep
      regex: rig-db
    # ... port rewriting to 9187

# MinIO job (static config)
- job_name: 'minio'
  static_configs:
    - targets: ['minio.rig-system:9000']
  metrics_path: /minio/v2/metrics/cluster
```

## Limitations

- **Scrape interval**: Storage metrics are collected every 2 hours (sufficient for capacity planning)
- **Prometheus retention**: Default is 7 days, limiting historical growth analysis (~84 data points per metric)
- **Redis**: Not included (requires external redis_exporter)
- **Alerting**: No alert rules configured yet (can be added later)

## Troubleshooting

### Metrics not appearing in Prometheus

1. Check if the pod has the correct annotations:
   ```bash
   kubectl get pod rig-db-1 -n rig-system -o jsonpath='{.metadata.annotations}' | grep prometheus
   ```

2. Verify the metrics endpoint is accessible:
   ```bash
   kubectl run curl-test -n rig-system --rm -i --restart=Never --image=curlimages/curl:latest -- \
     curl -s http://<pod-ip>:9187/metrics | head
   ```

3. Check Prometheus targets:
   ```bash
   kubectl port-forward -n rig-system svc/prometheus 9090:9090
   # Open http://localhost:9090/targets
   ```

### MinIO metrics showing empty buckets

MinIO metrics may take a few minutes to populate after bucket creation. The metrics are updated periodically by MinIO's internal scanner.
