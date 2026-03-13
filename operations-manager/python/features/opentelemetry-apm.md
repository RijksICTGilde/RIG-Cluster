# OpenTelemetry APM with Jaeger

## What it is

Distributed tracing for the OPI Operations Manager. Uses OpenTelemetry for automatic instrumentation and Jaeger all-in-one as the trace visualization backend.

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
  Jaeger all-in-one (:16686 UI)
  → Traces, Search, Compare, DAG view
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
| `OTEL_SERVICE_NAME` | `opi-operations-manager` | Service name in Jaeger |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://jaeger.rig-system:4317` | Jaeger OTLP gRPC endpoint |
| `OTEL_TRACES_SAMPLER_ARG` | `1.0` | Sampling rate (1.0 = 100%, 0.1 = 10%) |
| `OTEL_LOG_CORRELATION` | `true` | Inject trace_id/span_id into log records |

### Enable in .env.local

```env
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger.rig-system:4317
```

## Infrastructure

Jaeger all-in-one is deployed as a single pod in `rig-system` namespace:

| Component | Image | Ports | Storage |
|-----------|-------|-------|---------|
| Jaeger | `jaegertracing/all-in-one:1.64` | 16686 (UI), 4317 (OTLP gRPC), 4318 (OTLP HTTP) | In-memory |

### Local Access

Jaeger UI is available at `https://jaeger.kind` (local Kind cluster).

### Kustomize Structure

```
infrastructure/bootstrap/infrastructure/jaeger/
└── controller/
    ├── base/           # deployment.yaml, service.yaml
    └── overlays/
        ├── local/      # Adds ingress (jaeger.kind)
        ├── odcn/       # Production overlay
        └── sandboxed-local/
```

## Dependencies

- OPI needs the Jaeger service endpoint to export traces
- No external storage dependencies (in-memory)

## Troubleshooting

### Traces not appearing in Jaeger

1. Verify `OTEL_ENABLED=true` in app config
2. Check Jaeger is running: `kubectl get pods -n rig-system -l app=jaeger`
3. Check Jaeger logs: `kubectl logs -n rig-system -l app=jaeger`
4. Verify network connectivity from app pod to Jaeger on port 4317

### Traces lost on restart

Jaeger all-in-one uses in-memory storage. Traces are lost when the pod restarts. For persistent storage, consider adding Elasticsearch or Cassandra as a backend, or upgrading to a full SigNoz stack.
