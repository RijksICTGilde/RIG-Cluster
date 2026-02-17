# Capsule Proxy Prometheus Discovery

## What it is

In ODCN production, we run in a tenant cluster where direct namespace listing against the Kubernetes API is not possible. Capsule Proxy solves this by acting as a transparent API proxy that filters responses based on tenant permissions. This allows Prometheus to use native `kubernetes_sd_configs` for dynamic pod discovery — without hardcoded namespace lists.

Capsule Proxy is already used by several other components for the same reason:

- **ArgoCD CMP sidecar** — to discover and deploy resources across tenant namespaces
- **Operations Manager** — to manage namespaces and resources
- **External-DNS** — to watch Ingress resources across tenant namespaces

## How it works

Prometheus's `kubernetes_sd_configs` with `role: pod` opens a watch connection to the Kubernetes API. By setting the `KUBERNETES_SERVICE_HOST` environment variable to the Capsule Proxy endpoint, Prometheus talks to Capsule Proxy instead of the real API server. Capsule Proxy then:

1. Authenticates the request using the pod's service account token
2. Determines which tenant the service account belongs to
3. Filters the API response to only include namespaces/resources visible to that tenant
4. Streams watch events for real-time updates (no polling needed)

This means:

- **No hardcoded namespace lists** — Capsule Proxy handles scoping automatically
- **Automatic discovery** — New namespaces added to the tenant are picked up in real-time via the Kubernetes watch API
- **No restart needed** — Prometheus maintains a persistent watch connection and receives events as they happen

## Configuration

### Environment variable

The Prometheus deployment in the ODCN overlay sets:

```yaml
env:
- name: KUBERNETES_SERVICE_HOST
  value: cluster-api.apps.prd1.gn2.quattro.rijksapps.nl
```

This is the Capsule Proxy endpoint shared across all tenant-aware components.

### Scrape config

The `kubernetes_sd_configs` uses `role: pod` without any `namespaces.names` filter:

```yaml
kubernetes_sd_configs:
  - role: pod
```

Compare this to local/sandboxed-local, which has full cluster access and uses explicit namespace lists:

```yaml
kubernetes_sd_configs:
  - role: pod
    namespaces:
      names:
        - rig-system
        - rig-algor-abc
        # ... hardcoded list
```

### RBAC

The ODCN overlay includes a namespace-scoped Role and RoleBinding granting the `namespace-manager` service account read access to pods, services, and endpoints. This is the same service account used by the Prometheus deployment.

### What is NOT available through Capsule Proxy

Node-level metrics are not available because tenant clusters don't expose node resources:

- **cAdvisor** (container metrics via kubelet) — not available
- **kubelet metrics** (PVC volume stats) — not available
- **kube-state-metrics** — scaled to 0 replicas (no cluster-wide object access)

## Scrape targets

Through Capsule Proxy discovery, Prometheus automatically scrapes:

| Job | Discovery | What it scrapes |
|-----|-----------|----------------|
| `kubernetes-pods` | Pods with `prometheus.io/scrape: "true"` | Any annotated pod (Operations Manager, custom apps) |
| `cloudnative-pg` | Pods with `cnpg.io/cluster: rig-db` label | PostgreSQL metrics on port 9187 |
| `minio` | Pods with `app: minio` label | MinIO cluster metrics |
| `keycloak-rig-metrics` | Pods with `app: keycloak` label | Keycloak custom realm metrics |

## Adding a new project namespace

No Prometheus configuration changes needed. When a new project namespace is added to the Capsule tenant:

1. Capsule Proxy automatically includes it in API responses
2. Prometheus discovers pods in the new namespace via its existing watch connection
3. Pods with `prometheus.io/scrape: "true"` annotation are scraped automatically

## Dependencies

- Capsule Proxy accessible at `cluster-api.apps.prd1.gn2.quattro.rijksapps.nl`
- `namespace-manager` service account configured as part of the Capsule tenant
- Pods must have `prometheus.io/scrape: "true"` annotation to be discovered

## Troubleshooting

### Prometheus not discovering pods

1. Verify Capsule Proxy is reachable from the Prometheus pod:
   ```bash
   kubectl exec -n rig-prd-operations deployment/prometheus -- \
     wget -qO- --no-check-certificate \
     https://cluster-api.apps.prd1.gn2.quattro.rijksapps.nl/api/v1/pods
   ```

2. Check that the `KUBERNETES_SERVICE_HOST` env var is set:
   ```bash
   kubectl get deployment -n rig-prd-operations prometheus \
     -o jsonpath='{.spec.template.spec.containers[0].env}'
   ```

3. Verify the service account has tenant access:
   ```bash
   kubectl auth can-i list pods --as=system:serviceaccount:rig-prd-operations:namespace-manager
   ```

### New namespace not appearing

- Verify the namespace is part of the Capsule tenant
- Check Prometheus targets page (Status > Targets) for discovery errors
- Verify pods in the namespace have the `prometheus.io/scrape: "true"` annotation
