# Task: Lightweight Kind Cluster for Local Development

**Status**: Planned
**Priority**: High
**Created**: 2026-01-30

## Goal

Create a minimal "ready to go" Kind cluster setup that runs the Operations Manager without the overhead of the full gitops-haven-plus cluster. The setup should be fast to create, easy to throw away, and suitable for debugging and development.

---

## Current State Analysis

### What We Have Now

The current `setup-local-cluster` task installs:

| Component | Required? | Notes |
|-----------|-----------|-------|
| Kind cluster | Yes | Base Kubernetes |
| NGINX Ingress | Yes | Routing/ingress |
| CloudNativePG Operator | Yes | PostgreSQL management |
| PostgreSQL | Yes | Database for Operations Manager |
| Keycloak | Yes | Authentication/OIDC |
| MinIO | Yes | Object storage for projects |
| ArgoCD | Yes | GitOps deployment engine |
| Redis | Optional | Only if projects request it |
| Chisel | Optional | Only for remote cluster cloning |
| Prometheus | Optional | Metrics (gracefully degraded if missing) |
| Vault | Disabled | Not currently used |
| Forgejo | Disabled | s6-overlay permission issue |
| PgAdmin | Disabled | Admin UI |

### What's Actually Required

**Core dependencies for Operations Manager to start:**
1. PostgreSQL - database storage
2. NGINX Ingress - HTTP routing
3. ArgoCD - GitOps deployment
4. MinIO - S3-compatible storage

**Optional (can be added later):**
- Keycloak (use `AUTH_MODE=api-key-only` without it)
- Redis, Chisel, Prometheus, Forgejo

---

## Proposed Solution

### Two-Tier Cluster Setup

#### Tier 1: Minimal Cluster (`task setup-minimal-cluster`)

Fast, lightweight cluster with only essentials:

```
Kind Cluster
├── NGINX Ingress Controller
├── CloudNativePG Operator
├── PostgreSQL (rig-db)
├── MinIO (object storage)
└── ArgoCD Operator + ArgoCD
```

**Estimated resources:** ~8-10 pods, ~2-3GB memory

**Use case:** Quick debugging, UI development, API testing

#### Tier 2: Full Local Cluster (`task setup-local-cluster`)

Everything including optional services:

```
Tier 1 +
├── Keycloak (full SSO)
├── Redis
├── Chisel
├── Prometheus
└── Forgejo (when fixed)
```

**Estimated resources:** ~15-20 pods, ~4-6GB memory

**Use case:** Full integration testing, production-like environment

---

## Implementation

### Phase 1: Minimal Kustomization

**File**: `infrastructure/bootstrap/clusters/minimal/kustomization.yaml` (new)

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
  # Core infrastructure only - no Keycloak, Redis, Chisel, Prometheus, Forgejo
  - ../../infrastructure/cert-manager/controller/base
  - ../../infrastructure/postgresql/operator/base
  - ../../infrastructure/postgresql/database/base
  - ../../infrastructure/minio/base
  - ../../infrastructure/secrets/config/overlays/local

# Patches to reduce resource requests for minimal cluster
patches:
  - target:
      kind: Cluster
      name: rig-db
    patch: |
      - op: replace
        path: /spec/instances
        value: 1
      - op: replace
        path: /spec/resources/requests/memory
        value: "256Mi"
      - op: replace
        path: /spec/resources/limits/memory
        value: "512Mi"
```

### Phase 2: Minimal Bootstrap ArgoCD Application

**File**: `bootstrap/rig-system/kustomize/overlays/minimal/kustomization.yaml` (new)

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
  - ../../base
  - argocd-application-minimal-infrastructure.yaml
```

**File**: `bootstrap/rig-system/kustomize/overlays/minimal/argocd-application-minimal-infrastructure.yaml` (new)

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: minimal-infrastructure
  namespace: rig-system
spec:
  project: default
  source:
    repoURL: git://localhost/
    targetRevision: HEAD
    path: infrastructure/bootstrap/clusters/minimal
  destination:
    server: https://kubernetes.default.svc
    namespace: rig-system
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
```

### Phase 3: Taskfile Tasks

**File**: `Taskfile.yaml` (modify - add these tasks)

```yaml
  setup-minimal-cluster:
    desc: "Create minimal Kind cluster for quick development (no Keycloak, no Redis)"
    cmds:
      - task: requirements-check
      - task: generate-age-key
      - task: generate-local-ca
      - task: create-local-kind-cluster
      - task: install-ingress-nginx
      - task: install-cnpg-operator
      - task: configure-coredns-kind-domains
      - task: prepare-argocd-operator
      - task: update-cmp-kustomize-sops
      - task: _deploy-minimal-infrastructure
      - task: import-ca-to-cluster
      - task: _wait-for-minimal-infrastructure
      - |
        echo ""
        echo "========================================="
        echo "  Minimal Cluster Ready!"
        echo "========================================="
        echo ""
        echo "Running services:"
        echo "  - PostgreSQL (rig-db)"
        echo "  - MinIO"
        echo "  - ArgoCD"
        echo "  - NGINX Ingress"
        echo ""
        echo "NOT running (add with 'task setup-local-cluster'):"
        echo "  - Keycloak (use AUTH_MODE=api-key-only)"
        echo "  - Redis, Chisel, Prometheus"
        echo ""
        echo "Start Operations Manager:"
        echo "  cd operations-manager/python"
        echo "  cp .env.minimal .env"
        echo "  uv run python -m opi"
        echo ""
    silent: false

  _deploy-minimal-infrastructure:
    internal: true
    cmds:
      - |
        echo "========================================="
        echo "Deploying minimal infrastructure"
        echo "========================================="
        echo ""
        echo "Applying minimal kustomization..."
        kustomize build infrastructure/bootstrap/clusters/minimal | kubectl apply -f -
        echo ""
        echo "Applying minimal bootstrap..."
        kustomize build bootstrap/rig-system/kustomize/overlays/minimal | kubectl apply -f -
    silent: true

  _wait-for-minimal-infrastructure:
    internal: true
    cmds:
      - |
        echo "Waiting for PostgreSQL cluster to be ready..."
        until kubectl get cluster rig-db -n rig-system -o jsonpath='{.status.phase}' 2>/dev/null | grep -q "Cluster in healthy state"; do
          echo "  PostgreSQL not ready yet, waiting..."
          sleep 10
        done
        echo "PostgreSQL ready."

        echo "Waiting for MinIO to be ready..."
        kubectl wait --namespace rig-system \
          --for=condition=ready pod \
          --selector=app=minio \
          --timeout=300s 2>/dev/null || echo "  MinIO pods not found yet, continuing..."
        echo "MinIO ready."

        echo ""
        echo "All minimal infrastructure services are running."
    silent: true

  destroy-cluster:
    desc: "Delete the Kind cluster completely"
    cmds:
      - |
        echo "========================================="
        echo "Destroying Kind cluster: {{.KIND_CLUSTER_NAME}}"
        echo "========================================="

        if ! kind get clusters | grep -q "^{{.KIND_CLUSTER_NAME}}$"; then
          echo "Cluster '{{.KIND_CLUSTER_NAME}}' does not exist."
          exit 0
        fi

        kind delete cluster --name {{.KIND_CLUSTER_NAME}}
        echo ""
        echo "Cluster deleted."
        echo "Run 'task setup-minimal-cluster' to recreate."
    silent: false
```

### Phase 4: Minimal Environment Configuration

**File**: `operations-manager/python/.env.minimal` (new)

```env
# Minimal cluster configuration - no Keycloak, no SSO
ENVIRONMENT=minimal
CLUSTER_MANAGER=local

# Skip services that aren't deployed in minimal cluster
SKIP_STARTUP_CHECKS=true
AUTH_MODE=api-key-only
USE_UNSAFE_API_KEY=true

# Disable features that require missing services
ENABLE_GIT_MONITOR=false
ENABLE_AUTO_SCALE=false
METRICS_BACKEND=none

# Database (PostgreSQL is available in minimal cluster)
DATABASE_HOST=postgresql.kind
DATABASE_PORT=5432
DATABASE_NAME=operations_manager

# MinIO (available in minimal cluster)
MINIO_ENDPOINT=minio.kind
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=minioadmin

# ArgoCD (available in minimal cluster)
ARGOCD_SERVER=argocd.kind

# Logging
LOG_LEVEL=DEBUG
```

### Phase 5: Taskfile Environment Configuration

**File**: `.env-taskfile-minimal` (new)

```env
# Taskfile variables for minimal cluster
KIND_CLUSTER_NAME=gitops-fluxcd
CLUSTER_TYPE=local
RIG_NAMESPACE=rig-system
BOOTSTRAP_CLUSTER_FOLDER=minimal
```

---

## Critical Files to Create/Modify

| File | Action |
|------|--------|
| `infrastructure/bootstrap/clusters/minimal/kustomization.yaml` | Create - minimal infrastructure kustomization |
| `bootstrap/rig-system/kustomize/overlays/minimal/kustomization.yaml` | Create - minimal bootstrap |
| `bootstrap/rig-system/kustomize/overlays/minimal/argocd-application-minimal-infrastructure.yaml` | Create - ArgoCD app for minimal infra |
| `Taskfile.yaml` | Modify - add `setup-minimal-cluster`, `_deploy-minimal-infrastructure`, `_wait-for-minimal-infrastructure`, `destroy-cluster` tasks |
| `operations-manager/python/.env.minimal` | Create - minimal environment config |
| `.env-taskfile-minimal` | Create - taskfile config for minimal cluster |

---

## Verification Steps

1. **Create minimal cluster:**
   ```bash
   task setup-minimal-cluster
   # Should complete in ~3-5 minutes
   ```

2. **Verify services:**
   ```bash
   kubectl get pods -n rig-system
   # Should show: postgresql, minio, argocd pods only
   # Should NOT show: keycloak, redis, chisel, prometheus
   ```

3. **Run Operations Manager:**
   ```bash
   cd operations-manager/python
   cp .env.minimal .env
   uv run python -m opi
   # Should start without Keycloak errors
   # Should respond to API calls with API key auth
   ```

4. **Destroy and recreate:**
   ```bash
   task destroy-cluster
   task setup-minimal-cluster
   # Should be fast and repeatable
   ```

5. **Upgrade to full:**
   ```bash
   task setup-local-cluster
   # Should add missing services on top of minimal
   ```

---

## Resource Comparison

| Cluster Type | Pods | Memory | Setup Time |
|--------------|------|--------|------------|
| Minimal | ~8-10 | ~2-3GB | ~3-5 min |
| Full Local | ~15-20 | ~4-6GB | ~8-12 min |
| Haven+ (old) | ~25-30 | ~6-8GB | ~15-20 min |

---

## Future Enhancements

1. **Add-on tasks:** `task add-keycloak`, `task add-prometheus` to incrementally add services to a minimal cluster
2. **Profiles:** Use Taskfile `vars` to select profiles: `task setup-local-cluster PROFILE=minimal`
3. **Prebuilt images:** Cache operator images for faster cluster creation
4. **Snapshot/restore:** Save cluster state for quick restoration

---

## Dependencies on Other Features

- **Independent Local Development** (`features/future/independent-local-development.md`): The SSL/domain changes should apply to both minimal and full clusters
- **SSO Optional** (`AUTH_MODE` setting): Required for minimal cluster to work without Keycloak - verify `AUTH_MODE=api-key-only` is fully implemented
