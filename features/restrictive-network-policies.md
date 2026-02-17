# Restrictive Network Policies

**Status**: Planned
**Priority**: Future Enhancement
**Created**: 2026-02-17

## Problem Statement

The current network policies are too permissive. The `allow-all-network-policy.yaml.jinja` template opens all ingress and egress for every pod in a namespace (`podSelector: {}` with empty ingress/egress rules). The `network-policy.yaml.jinja` template restricts by port but places no restriction on the source of traffic.

This is problematic because multiple deployments can share the same namespace. Under the current policies, any deployment in the namespace can freely communicate with any other deployment in that namespace, and there is no isolation between unrelated workloads.

## Current State

### `allow-all-network-policy.yaml.jinja`
- Applies to all pods in the namespace (`podSelector: {}`)
- Allows all ingress from any source
- Allows all egress to any destination
- Effectively disables network policy enforcement for the namespace

### `network-policy.yaml.jinja`
- Optionally scoped to pods via `pod_selector`
- Restricts ingress to specific ports
- Does **not** restrict the source of ingress traffic (no `from` clause)
- Does **not** define egress rules

## Goal

Implement label-based network policies that restrict traffic so that only related objects within a deployment can communicate with each other, while still allowing necessary access to shared RIG infrastructure services.

## Design

### Label-Based Isolation

Each deployment already labels its pods with `app: <deployment-name>`. Network policies should use these labels to scope traffic:

- **Ingress**: Only allow traffic from pods with the same `app` label (same deployment), plus the ingress controller
- **Egress**: Only allow traffic to pods with the same `app` label, plus required infrastructure

### Required Infrastructure Access

All deployments need egress access to shared RIG infrastructure services. These should be explicitly allowed:

| Service | Namespace | Purpose |
|---------|-----------|---------|
| Ingress controller | `ingress-nginx` | External traffic routing to pods |
| PostgreSQL (CloudNativePG) | project namespace / `cnpg-system` | Database access |
| Keycloak | `rig-system` or infrastructure namespace | SSO/authentication |
| MinIO | infrastructure namespace | Object storage |
| ArgoCD | `rig-system` | GitOps sync |
| DNS (CoreDNS / kube-dns) | `kube-system` | Service discovery |
| Prometheus | infrastructure namespace | Metrics scraping (ingress) |

### Policy Structure

Replace the current allow-all approach with a per-deployment policy:

**Ingress rules:**
1. Allow from pods with matching `app: <deployment-name>` label (intra-deployment)
2. Allow from ingress controller namespace (external traffic)
3. Allow from Prometheus namespace on metrics port (scraping)

**Egress rules:**
1. Allow to pods with matching `app: <deployment-name>` label (intra-deployment)
2. Allow to DNS on port 53 (UDP + TCP)
3. Allow to PostgreSQL cluster pods on port 5432
4. Allow to Keycloak on port 8080/8443
5. Allow to MinIO on port 9000
6. Allow to external HTTPS (port 443) for outbound API calls

### Default-Deny Base

Each namespace should have a default-deny policy that blocks all traffic not explicitly allowed:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-all
spec:
  podSelector: {}
  policyTypes:
    - Ingress
    - Egress
```

Per-deployment policies then layer on top to allow only what is needed.

## Implementation Considerations

- The deployment template already sets `app: <deployment-name>` labels on pods, so the selector infrastructure is in place
- The `network-policy.yaml.jinja` template needs a `from` clause added to its ingress rules and egress rules need to be defined
- The `allow-all-network-policy.yaml.jinja` should be replaced or deprecated in favor of the restrictive per-deployment policy
- Projects that genuinely need open policies (e.g., during migration) could opt-in to a legacy allow-all mode
- Infrastructure namespace labels (e.g., `kubernetes.io/metadata.name: ingress-nginx`) should be used for namespace-scoped selectors, which requires Kubernetes 1.21+ (already satisfied)

## Dependencies

- Deployment labels must be consistent across all manifest templates (deployment, service, ingress)
- Infrastructure namespace names must be stable and known at generation time
- The operations manager needs to know which infrastructure services a deployment depends on (could be derived from project configuration: database enabled, storage enabled, etc.)

## Related

- `features/shared-resource-warning-acme-network-policy.md` - Related issue with shared network policies across deployments
- `manifests/network-policy.yaml.jinja` - Current network policy template
- `manifests/allow-all-network-policy.yaml.jinja` - Current allow-all template
- `manifests/deployment.yaml.jinja` - Deployment template (source of pod labels)
