# Local Kind Cluster Setup

## Overview

The local development environment uses Kind (Kubernetes in Docker) to replicate a Haven-compliant Kubernetes cluster for testing and development.

**Cluster Name**: `gitops-fluxcd`
**Kubernetes Version**: 1.32.0
**Cluster Type**: `local`

## Haven Compliance

This cluster follows the [Haven standard](https://haven.commonground.nl/) - a platform-agnostic Kubernetes configuration that ensures applications work consistently across different infrastructure providers. Haven is part of the Common Ground initiative for Dutch municipalities.

The local Kind cluster replicates production Haven environments, allowing development and testing of applications that will deploy to Haven-compliant infrastructure in ODC-Noord.

## DNS Resolution Architecture

### Challenge

Keycloak OIDC flows require stable internal DNS resolution. Services must communicate using domain names rather than cluster IPs, but standard CoreDNS cannot resolve the `.kind` TLD used for local development.

### Solution: CoreDNS Rewrite

A DNS rewrite rule redirects all `*.kind` domains to the NGINX ingress controller.

**Task**: `configure-coredns-kind-domains`

**Implementation**:
1. Patches CoreDNS ConfigMap (`kube-system` namespace)
2. Inserts rewrite rule after `ready` plugin:
   ```
   rewrite stop {
       name regex (.+)\.kind ingress-nginx-controller.ingress-nginx.svc.cluster.local
       answer auto
   }
   ```
3. Restarts CoreDNS deployment

**Outcome**: DNS queries for `keycloak.kind`, `argo.kind`, etc. resolve to the ingress controller service within the cluster.

**Verification**:
```bash
kubectl run -it --rm --restart=Never --image=busybox:1.28 dnstest -- nslookup keycloak.kind
```

### Design Rationale

DNS rewriting enables:
- Service discovery using friendly domain names
- Proper Keycloak OIDC redirect URIs and issuer URLs
- Consistent pod behavior without per-pod configuration
- No host machine `/etc/hosts` management

Alternative approaches (host file modifications, external DNS) were rejected due to maintenance overhead and inconsistent behavior across pods.

## Idempotency

The `configure-coredns-kind-domains` task checks for existing configuration before applying changes. Safe to re-run.

## Related Tasks

- `requirements-check` - Validates tooling (kind, kubectl, kustomize, sops, age)
- `bootstrap-argo-system` - Deploys ArgoCD using cluster overlay
- `uninstall-local-kind-cluster` - Removes Kind cluster
