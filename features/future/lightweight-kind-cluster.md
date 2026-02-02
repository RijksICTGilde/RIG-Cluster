# Task: Lightweight Kind Cluster for Local Development

**Status:** Future / Planned
**Priority:** High
**Created:** 2026-01-30

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
2. Keycloak - authentication (OR optional with `AUTH_MODE=api-key-only`)
3. MinIO - S3-compatible storage
4. NGINX Ingress - HTTP routing
5. ArgoCD - GitOps deployment

**Optional (can be added later):**
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

## Implementation Plan

### Phase 1: Create Minimal Kustomization

**New file:** `infrastructure/bootstrap/clusters/minimal/kustomization.yaml`

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
  # Core infrastructure only
  - ../../infrastructure/cert-manager
  - ../../infrastructure/postgresql
  - ../../infrastructure/minio
  - ../../infrastructure/secrets/config/overlays/local

  # NO: keycloak, redis, chisel, prometheus, forgejo
```

### Phase 2: Create Minimal Bootstrap

**New file:** `bootstrap/rig-system/kustomize/overlays/minimal/kustomization.yaml`

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
  - ../../base
  - argocd-application-minimal-infrastructure.yaml

# Minimal ArgoCD setup without full infrastructure
```

### Phase 3: Add Taskfile Tasks

**New tasks in `Taskfile.yaml`:**

```yaml
setup-minimal-cluster:
  desc: "Create minimal Kind cluster for quick development"
  cmds:
    - task: _setup-step
      vars: { STEP: "1/6", NAME: "Check prerequisites", CMD: "_check-prerequisites" }
    - task: _setup-step
      vars: { STEP: "2/6", NAME: "Create Kind cluster", CMD: "_ensure-kind-cluster" }
    - task: _setup-step
      vars: { STEP: "3/6", NAME: "Install NGINX Ingress", CMD: "install-ingress-nginx" }
    - task: _setup-step
      vars: { STEP: "4/6", NAME: "Install CNPG Operator", CMD: "install-cnpg-operator" }
    - task: _setup-step
      vars: { STEP: "5/6", NAME: "Configure CoreDNS", CMD: "configure-coredns-kind-domains" }
    - task: _setup-step
      vars: { STEP: "6/6", NAME: "Deploy minimal infrastructure", CMD: "_deploy-minimal-infra" }
    - task: _minimal-setup-complete

_deploy-minimal-infra:
  internal: true
  cmds:
    - kubectl apply -k infrastructure/bootstrap/clusters/minimal/

_minimal-setup-complete:
  internal: true
  cmds:
    - echo ""
    - echo "╔═══════════════════════════════════════════════════════════╗"
    - echo "║         Minimal Cluster Ready!                            ║"
    - echo "╠═══════════════════════════════════════════════════════════╣"
    - echo "║                                                           ║"
    - echo "║  Running services:                                        ║"
    - echo "║    - PostgreSQL (rig-db)                                  ║"
    - echo "║    - MinIO                                                ║"
    - echo "║    - ArgoCD                                               ║"
    - echo "║                                                           ║"
    - echo "║  NOT running (add with task setup-local-cluster):         ║"
    - echo "║    - Keycloak (use AUTH_MODE=api-key-only)                ║"
    - echo "║    - Redis, Chisel, Prometheus                            ║"
    - echo "║                                                           ║"
    - echo "╚═══════════════════════════════════════════════════════════╝"

destroy-cluster:
  desc: "Delete the Kind cluster completely"
  cmds:
    - kind delete cluster --name gitops-fluxcd
    - echo "Cluster deleted. Run 'task setup-minimal-cluster' to recreate."
```

### Phase 4: Operations Manager Minimal Mode

Ensure Operations Manager can run with minimal dependencies:

**Config additions (`config.py`):**
```python
# Already exists, but document usage:
SKIP_STARTUP_CHECKS: bool = True  # Skip Keycloak/MinIO checks
AUTH_MODE: str = "api-key-only"   # Skip OIDC entirely
```

**`.env.minimal` file:**
```env
ENVIRONMENT=minimal
SKIP_STARTUP_CHECKS=true
AUTH_MODE=api-key-only
USE_UNSAFE_API_KEY=true
ENABLE_GIT_MONITOR=false
```

---

## Critical Files to Create/Modify

| File | Action |
|------|--------|
| `infrastructure/bootstrap/clusters/minimal/kustomization.yaml` | Create - minimal infrastructure |
| `bootstrap/rig-system/kustomize/overlays/minimal/kustomization.yaml` | Create - minimal bootstrap |
| `Taskfile.yaml` | Add `setup-minimal-cluster`, `destroy-cluster` tasks |
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
   ```

3. **Run Operations Manager:**
   ```bash
   cd operations-manager/python
   ENVIRONMENT=minimal uv run python -m opi
   # Should start without Keycloak
   ```

4. **Destroy and recreate:**
   ```bash
   task destroy-cluster
   task setup-minimal-cluster
   # Should be fast and repeatable
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

1. **Add-on tasks:** `task add-keycloak`, `task add-prometheus` to incrementally add services
2. **Profiles:** Docker Compose-style profiles for different setups
3. **Prebuilt images:** Cache operator images for faster cluster creation
4. **Snapshot/restore:** Save cluster state for quick restoration

---

## Dependencies on Other Features

- **Independent Local Development** (`features/future/independent-local-development.md`): The SSL/domain changes should apply to both minimal and full clusters
- **SSO Optional** (`AUTH_MODE` setting): Required for minimal cluster to work without Keycloak
