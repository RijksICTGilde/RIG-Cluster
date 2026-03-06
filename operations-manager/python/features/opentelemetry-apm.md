# OpenTelemetry APM with SigNoz

## What it is

Full-stack distributed tracing and APM (Application Performance Monitoring) for the OPI Operations Manager. Uses OpenTelemetry for automatic instrumentation and SigNoz as the visualization backend, providing service maps, trace flow graphs, and request-level performance insights.

## How it works

### Trace Flow

```
OPI FastAPI App
  ├── [auto] HTTP routes → spans
  ├── [auto] httpx/aiohttp outbound → spans
  ├── [auto] asyncpg DB queries → spans
  └── [auto] log correlation → trace_id in logs
         │
         ▼ OTLP gRPC (:4317)
  OTel Collector → ClickHouse
                        ▲
  SigNoz UI (:8080) ────┘
  → Service Maps, Traces, Metrics, Logs
```

### Instrumented Libraries

| Library | What it traces |
|---------|---------------|
| FastAPI | Inbound HTTP requests (method, status, duration) |
| httpx | Outbound HTTP calls (to Keycloak, ArgoCD, etc.) |
| aiohttp | Outbound async HTTP calls (connectors) |
| asyncpg | PostgreSQL database queries |
| logging | Injects trace_id/span_id into log records |

Health, readiness, and metrics endpoints are excluded from tracing to reduce noise.

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OTEL_ENABLED` | `false` | Enable/disable tracing (zero overhead when off) |
| `OTEL_SERVICE_NAME` | `opi-operations-manager` | Service name in SigNoz |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://signoz-otel-collector.rig-system:4317` | OTel Collector gRPC endpoint |
| `OTEL_TRACES_SAMPLER_ARG` | `1.0` | Sampling rate (1.0 = 100%, 0.1 = 10%) |
| `OTEL_LOG_CORRELATION` | `true` | Inject trace_id/span_id into log records |

### Enable in .env.local

```env
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://signoz-otel-collector.rig-system:4317
```

### Production Sampling

For production, reduce sampling to avoid storage overhead:

```env
OTEL_TRACES_SAMPLER_ARG=0.1  # Sample 10% of traces
```

## Infrastructure

SigNoz is deployed as Kubernetes infrastructure in `rig-system` namespace:

| Component | Image | Purpose |
|-----------|-------|---------|
| ZooKeeper | `bitnami/zookeeper:3.7.1` | ClickHouse coordination |
| ClickHouse | `clickhouse/clickhouse-server:24.1` | Trace/metrics/logs storage (20Gi PVC) |
| OTel Collector | `signoz/signoz-otel-collector:0.111.24` | Receives OTLP, writes to ClickHouse |
| SigNoz | `signoz/signoz:latest` | UI + query service |
| Schema Migrator | `signoz/signoz-schema-migrator:latest` | One-time DB schema setup |

### Local Access

SigNoz UI is available at `https://signoz.kind` (local Kind cluster).

### Kustomize Structure

```
infrastructure/bootstrap/infrastructure/signoz/
└── controller/
    ├── base/           # All manifests
    └── overlays/
        ├── local/      # Adds ingress (signoz.kind)
        ├── odcn/       # Production overlay
        └── sandboxed-local/
```

## Dependencies

- ClickHouse requires ZooKeeper to be running first
- Schema Migrator Job runs once after ClickHouse is ready (has init container wait)
- SigNoz needs ClickHouse with migrated schema
- OPI needs OTel Collector endpoint to export traces

## Troubleshooting

### Traces not appearing in SigNoz

1. Verify `OTEL_ENABLED=true` in app config
2. Check OTel Collector is running: `kubectl get pods -n rig-system -l app=signoz-otel-collector`
3. Check collector logs: `kubectl logs -n rig-system -l app=signoz-otel-collector`
4. Verify network connectivity from app pod to collector on port 4317

### Schema Migrator Job failing

1. Check if ClickHouse is ready: `kubectl logs -n rig-system -l app=signoz-schema-migrator`
2. The init container waits for ClickHouse — check its logs for connection issues
3. Delete and recreate the job if needed: `kubectl delete job signoz-schema-migrator -n rig-system`

### High storage usage

Reduce sampling rate (`OTEL_TRACES_SAMPLER_ARG`) or configure ClickHouse TTL for trace retention.
