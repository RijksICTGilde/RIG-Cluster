# Infrastructure

Kustomize-based Kubernetes infrastructure configuration for all RIG-Cluster environments.

## Structure

```
infrastructure/
+-- bootstrap/
    |-- clusters/                    # Per-environment overlays
    |   |-- local/                   #   Kind cluster with external Git
    |   |-- sandboxed-local/         #   Kind cluster with in-cluster Forgejo
    |   +-- odcn/                    #   Production (ODC-Noord)
    |
    +-- infrastructure/              # Component configurations
        |-- argocd/                  #   GitOps controller
        |-- postgresql/              #   CNPG database cluster
        |-- keycloak/                #   Identity and SSO
        |-- minio/                   #   S3-compatible storage
        |-- vault/                   #   Secret management
        |-- redis/                   #   Caching
        |-- prometheus/              #   Monitoring
        |-- cert-manager/            #   TLS certificates
        |-- external-dns/            #   DNS management (TransIP)
        |-- forgejo/                 #   In-cluster Git (sandbox only)
        |-- backup-destination/      #   PVC backup storage (Kopia)
        |-- chisel/                  #   Reverse tunnel
        |-- pgadmin/                 #   PostgreSQL admin UI
        |-- registry/               #   Container image registry
        |-- common/                  #   Shared resources (namespace, RBAC)
        +-- secrets/                 #   Secret templates and SOPS generation
```

## Component Pattern

Each component follows base/overlays:

```
component/
|-- config/
|   |-- base/
|   +-- overlays/ (local, sandboxed-local, odcn)
+-- controller/
    |-- base/
    +-- overlays/ (local, sandboxed-local, odcn)
```

## Building

```bash
# Without SOPS
kustomize build infrastructure/bootstrap/clusters/sandboxed-local

# With SOPS
SOPS_AGE_KEY="$(sed -n '3p' security/sandbox-key.txt)" kustomize build \
  --enable-alpha-plugins --enable-exec \
  --load-restrictor LoadRestrictionsNone \
  infrastructure/bootstrap/clusters/sandboxed-local
```
