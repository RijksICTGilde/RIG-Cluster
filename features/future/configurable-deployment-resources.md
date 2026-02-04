# Configurable Deployment Resources

**Status**: Planned
**Priority**: High
**Created**: 2026-02-02

## Overview

This feature allows projects to configure CPU and memory resources for their deployments through the project file, with validation to ensure values stay within allowed limits.

## What It Is

By default, all deployments use the following resource values:
- **Requests**: 256Mi memory, 50m CPU
- **Limits**: 512Mi memory, 500m CPU

This feature enables projects to customize these values per component or deployment while enforcing cluster-wide limits to prevent resource abuse.

Projects should either omit the `resources` block entirely (using defaults) or specify only the minimal required requests to allow the scheduler flexibility.

## When to Use

Use custom resource configuration when:
- Your application requires more memory (e.g., ML models, data processing)
- Your application is lightweight and you want to reduce resource allocation
- You need different resource profiles for different components
- You want to optimize cluster resource utilization

## Configuration

### Component-Level Configuration

Resources are configured at the component level in your project file:

```yaml
components:
  - name: api-server
    image: my-registry/api:latest
    resources:
      requests:
        memory: "256Mi"
        cpu: "100m"
      limits:
        memory: "512Mi"
        cpu: "500m"
```

### Deployment-Level Override

You can override component resources for specific deployments:

```yaml
deployments:
  - name: production
    cluster: odcn-production
    components:
      - reference: api-server
        resources:
          requests:
            memory: "512Mi"
            cpu: "200m"
          limits:
            memory: "1024Mi"
            cpu: "1000m"
```

### Configuration Fields

| Field | Required | Description |
|-------|----------|-------------|
| `resources` | No | Resource configuration block |
| `resources.requests.memory` | No | Memory request (default: 256Mi) |
| `resources.requests.cpu` | No | CPU request (default: 50m) |
| `resources.limits.memory` | No | Memory limit (default: 512Mi) |
| `resources.limits.cpu` | No | CPU limit (default: 500m) |

## Validation Rules

All resource values are validated against allowed ranges to ensure fair cluster usage.

### Allowed Ranges

| Resource | Minimum | Maximum | Default Request | Default Limit |
|----------|---------|---------|-----------------|---------------|
| Memory | 32Mi | 2048Mi | 256Mi | 512Mi |
| CPU | 50m | 1000m | 50m | 500m |

### Validation Checks

1. **Format validation**: Values must be valid Kubernetes resource formats
   - Memory: `32Mi`, `256Mi`, `1Gi`, `2048Mi`
   - CPU: `50m`, `100m`, `500m`, `1000m`, `1` (= 1000m)

2. **Range validation**: Values must be within allowed min/max limits
   - Memory below 32Mi or above 2048Mi will be rejected
   - CPU below 50m or above 1000m will be rejected

3. **Request/Limit consistency**: Requests must not exceed limits
   - `requests.memory` <= `limits.memory`
   - `requests.cpu` <= `limits.cpu`

4. **Partial configuration**: You can specify only the values you want to change
   - Unspecified values use defaults

### Error Examples

```yaml
# ERROR: Memory limit exceeds maximum (2048Mi)
resources:
  limits:
    memory: "4096Mi"  # Rejected: exceeds 2048Mi max

# ERROR: CPU request exceeds limit
resources:
  requests:
    cpu: "800m"
  limits:
    cpu: "500m"  # Rejected: request (800m) > limit (500m)

# ERROR: Invalid format
resources:
  requests:
    memory: "256"  # Rejected: missing unit (should be "256Mi")
```

## Format Reference

### Memory Units

| Format | Value |
|--------|-------|
| `32Mi` | 32 Mebibytes |
| `256Mi` | 256 Mebibytes |
| `1Gi` | 1 Gibibyte (1024Mi) |
| `2048Mi` | 2048 Mebibytes (2Gi) |

### CPU Units

| Format | Value |
|--------|-------|
| `50m` | 50 millicores (0.05 CPU) |
| `100m` | 100 millicores (0.1 CPU) |
| `500m` | 500 millicores (0.5 CPU) |
| `1000m` or `1` | 1000 millicores (1 CPU) |

## Examples

### Lightweight API Service

```yaml
components:
  - name: health-checker
    image: my-registry/health:latest
    resources:
      requests:
        memory: "64Mi"
        cpu: "50m"
      limits:
        memory: "128Mi"
        cpu: "100m"
```

### Memory-Intensive Application

```yaml
components:
  - name: ml-model
    image: my-registry/ml-service:latest
    resources:
      requests:
        memory: "1024Mi"
        cpu: "500m"
      limits:
        memory: "2048Mi"
        cpu: "1000m"
```

### Different Resources per Environment

```yaml
components:
  - name: api-server
    image: my-registry/api:latest
    # No resources block - uses defaults (recommended)

deployments:
  - name: development
    cluster: local
    components:
      - reference: api-server
        # Uses system defaults (256Mi/50m requests, 512Mi/500m limits)

  - name: production
    cluster: odcn-production
    components:
      - reference: api-server
        resources:
          requests:
            memory: "512Mi"
            cpu: "200m"
          limits:
            memory: "1024Mi"
            cpu: "500m"
```

### Partial Override

Only override specific values; others use defaults:

```yaml
components:
  - name: worker
    image: my-registry/worker:latest
    resources:
      limits:
        memory: "1024Mi"  # Only increase memory limit
        # cpu uses default: 500m
      # requests use defaults: 256Mi memory, 50m cpu
```

## Default Behavior

When no resources are specified, the system applies sensible defaults:

```yaml
# These two configurations are equivalent:

# Explicit defaults
resources:
  requests:
    memory: "256Mi"
    cpu: "50m"
  limits:
    memory: "512Mi"
    cpu: "500m"

# No resources block (recommended - uses same defaults)
# (resources block omitted)
```

**Recommendation**: For most projects, omit the `resources` block entirely and use the defaults. Only specify custom resources when you have a specific need (e.g., memory-intensive applications).

### Minimal Configuration

If you only need to adjust limits, you can specify just those:

```yaml
components:
  - name: my-app
    image: my-registry/app:latest
    resources:
      limits:
        memory: "1024Mi"  # Increase memory limit only
    # requests use defaults: 256Mi memory, 50m cpu
    # cpu limit uses default: 500m
```

## Troubleshooting

### Deployment Rejected with Resource Error

Check the operations-manager logs for validation errors:

```bash
kubectl logs -n rig-prd-operations deployment/operations-manager | grep -i "resource"
```

Common issues:
- Value exceeds allowed maximum
- Request exceeds limit
- Invalid format (missing unit)

### Pod Stuck in Pending State

If the pod is pending after deployment, the cluster may not have enough resources available:

```bash
kubectl describe pod -n <namespace> <pod-name>
```

Look for events like:
- `Insufficient memory`
- `Insufficient cpu`

Consider reducing your resource requests.

### OOMKilled Errors

If your pod is being killed with OOMKilled:

```bash
kubectl describe pod -n <namespace> <pod-name> | grep -A5 "Last State"
```

Increase your memory limit (up to 2048Mi maximum).

## Related Features

- [auto-scale-resources.md](../../operations-manager/python/features/auto-scale-resources.md) - Automatic resource scaling based on metrics (planned)
