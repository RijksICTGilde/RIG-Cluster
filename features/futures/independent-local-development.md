# Feature: Independent Local Development for Operations Manager

**Status:** Future / Planned
**Priority:** High
**Created:** 2026-01-30

## Summary

Enable any developer to run the Operations Manager and full local cluster independently, without needing access to master SOPS keys, shared secrets, or complex manual setup steps.

---

## Problem Statement

### Current Blockers for Independent Local Dev

| Issue | Impact | Root Cause |
|-------|--------|------------|
| Forgejo **disabled** in local bootstrap | No local Git server | s6-overlay permission issue (commented out) |
| ArgoCD still points to **GitHub** | Can't use local Git | Config never updated for Forgejo |
| SOPS secrets encrypted with **master key** | Others can't decrypt | Single shared key |
| SSO is **not optional** | Keycloak required for startup | Tight coupling in startup.py |
| Per-app CA trust patches | Fragile | Python/Java need explicit env vars |
| Hardcoded paths in `config.py` | Only works for original developer | User-specific paths |

---

## Proposed Solution

### Phase 1: Foundation Fixes

#### 1.1 Remove Hardcoded User Paths

**File:** `operations-manager/python/opi/core/config.py` (lines 159, 185)

```python
# FROM:
GIT_SERVER_KEY_PATH: str = "/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/keys/git-server-key"
GIT_ARGO_APPLICATIONS_KEY: str = "/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/keys/git-server-key"

# TO:
GIT_SERVER_KEY_PATH: str = ""  # Empty = not configured, use password auth
GIT_ARGO_APPLICATIONS_KEY: str = ""  # Empty = use password auth instead of SSH key
```

#### 1.2 Fix Forgejo s6-overlay Permission Issue

**File:** `infrastructure/bootstrap/clusters/local/kustomization.yaml`

- Investigate and fix the s6-overlay permission error
- Uncomment Forgejo in the kustomization

---

### Phase 2: Make SSO Optional

**Goal:** Allow Operations Manager to start without Keycloak/SSO for local development.

#### 2.1 Add Configuration Flag

**File:** `operations-manager/python/opi/core/config.py`

```python
OIDC_ENABLED: bool = True  # Set to False to skip OIDC/Keycloak entirely
AUTH_MODE: str = "oidc"  # Options: "oidc", "api-key-only", "none"
```

#### 2.2 Conditional Startup Flow

**File:** `operations-manager/python/opi/core/startup.py`

```python
if settings.AUTH_MODE == "oidc" and settings.OIDC_ENABLED:
    await setup_keycloak()
    await register_oauth_client_after_keycloak_setup(app)
elif settings.AUTH_MODE == "api-key-only":
    logger.info("Running in API-key-only mode - no OIDC")
else:
    logger.info("Running without authentication - development only!")
```

---

### Phase 3: Integrate Forgejo as Local Git Server

**Goal:** ArgoCD and Operations Manager use local Forgejo instead of GitHub.

#### 3.1 Update ArgoCD Application Configs

**File:** `bootstrap/rig-system/kustomize/overlays/local/argocd-application-user-applications.yaml`

```yaml
# FROM:
repoURL: https://github.com/RijksICTGilde/argo-applications.git

# TO:
repoURL: http://forgejo.rig-system.svc.cluster.local:3000/admin/argo-user-applications.git
```

#### 3.2 Update Operations Manager ConfigMap

**File:** `bootstrap/rig-system/kustomize/operations-manager/overlays/local/configmap.yaml`

```yaml
GIT_PROJECTS_SERVER_URL=http://forgejo.rig-system.svc.cluster.local:3000/admin/rig-cluster-projects.git
GIT_PROJECTS_SERVER_USERNAME=admin
GIT_PROJECTS_SERVER_PASSWORD=plain:admin

GIT_ARGO_APPLICATIONS_URL=http://forgejo.rig-system.svc.cluster.local:3000/admin/argo-user-applications.git
GIT_ARGO_APPLICATIONS_USERNAME=admin
GIT_ARGO_APPLICATIONS_PASSWORD=plain:admin
```

#### 3.3 Enhance Forgejo Bootstrap Job

**File:** `infrastructure/bootstrap/infrastructure/forgejo/config/base/bootstrap-job.yaml`

- Create third repository: `rig-cluster-projects`
- Make idempotent: Check if token exists before creating
- Use Kubernetes Secret for admin password instead of hardcoded

---

### Phase 4: Pre-Generated Wildcard Certificate

**Goal:** Eliminate certificate complexity by using a real, pre-generated wildcard cert.

#### 4.1 One-Time Setup (admin does this once)

1. **Configure public DNS:**
   ```
   *.local.example.com  A  127.0.0.1
   ```

2. **Request wildcard cert from Let's Encrypt** (via certbot with DNS-01):
   ```bash
   certbot certonly --manual --preferred-challenges dns \
     -d "*.local.example.com"
   ```

3. **Store cert in repo:**
   ```
   security/tls/local-wildcard/
   ├── fullchain.pem    # Certificate + chain
   └── privkey.pem      # Private key
   ```

4. **Renew every 90 days** (can automate on one machine)

#### 4.2 Update Domain References

Change all `.kind` references to `.local.example.com`:
- `operations-manager/python/opi/core/cluster_config.py`: `ingress_postfix: ".local.example.com"`
- All ingress configs in `bootstrap/rig-system/kustomize/overlays/local/`

#### 4.3 Update CoreDNS Rewrite

```
rewrite name regex (.+)\.local\.digilab\.network ingress-nginx-controller.ingress-nginx.svc.cluster.local
```

#### 4.4 Import Cert to Cluster

**New Taskfile task:** `import-local-wildcard-cert`
```yaml
import-local-wildcard-cert:
  desc: "Import pre-generated wildcard cert to cluster"
  cmds:
    - |
      kubectl create secret tls local-wildcard-cert \
        --cert=security/tls/local-wildcard/fullchain.pem \
        --key=security/tls/local-wildcard/privkey.pem \
        -n rig-system --dry-run=client -o yaml | kubectl apply -f -
```

#### Benefits
- **No `/etc/hosts` modification** - public DNS resolves to 127.0.0.1
- **No CA trust setup** - Let's Encrypt is universally trusted
- **No per-pod CA mounting** - standard cert chain works everywhere
- **No cert-manager complexity** for local dev

---

### Phase 5: Secret Generation with Well-Known Defaults

**Goal:** Generate all secrets with predictable local defaults.

**New file:** `infrastructure/bootstrap/infrastructure/secrets/config/local-defaults.yaml`

```yaml
# Well-known defaults for local development
# DO NOT use these in production!
postgresql:
  admin_password: changeMe123!
keycloak:
  admin_password: changeMe123!
minio:
  admin_password: changeMe123!
redis:
  password: changeMe123!
forgejo:
  admin_password: admin
argocd:
  admin_password: admin
```

---

### Phase 6: One-Command Idempotent Setup

**Goal:** `task setup-local-dev` that works for anyone, can run multiple times.

```yaml
setup-local-dev:
  desc: "Complete local development setup (idempotent, feedback-driven)"
  cmds:
    - task: _setup-step
      vars: { STEP: "1/9", NAME: "Check prerequisites", CMD: "_check-prerequisites" }
    - task: _setup-step
      vars: { STEP: "2/9", NAME: "Generate AGE encryption key", CMD: "generate-age-key" }
    - task: _setup-step
      vars: { STEP: "3/9", NAME: "Create Kind cluster", CMD: "_ensure-kind-cluster" }
    - task: _setup-step
      vars: { STEP: "4/9", NAME: "Install operators", CMD: "_install-operators" }
    - task: _setup-step
      vars: { STEP: "5/9", NAME: "Configure CoreDNS", CMD: "configure-coredns-local-domains" }
    - task: _setup-step
      vars: { STEP: "6/9", NAME: "Import wildcard certificate", CMD: "import-local-wildcard-cert" }
    - task: _setup-step
      vars: { STEP: "7/9", NAME: "Generate secrets", CMD: "generate-local-secrets" }
    - task: _setup-step
      vars: { STEP: "8/9", NAME: "Bootstrap ArgoCD", CMD: "bootstrap-argo-system" }
    - task: _setup-step
      vars: { STEP: "9/9", NAME: "Initialize Forgejo repos", CMD: "_init-forgejo-repos" }
    - task: _setup-complete
```

---

## Critical Files Summary

| File | Change |
|------|--------|
| `operations-manager/python/opi/core/config.py` | Remove hardcoded paths, add OIDC_ENABLED/AUTH_MODE |
| `operations-manager/python/opi/core/startup.py` | Make SSO conditional based on AUTH_MODE |
| `operations-manager/python/opi/core/cluster_config.py` | Change `ingress_postfix` from `.kind` to `.local.example.com` |
| `infrastructure/bootstrap/clusters/local/kustomization.yaml` | Enable Forgejo (fix permission issue) |
| `infrastructure/bootstrap/infrastructure/forgejo/config/base/bootstrap-job.yaml` | Add 3rd repo, make idempotent |
| `bootstrap/rig-system/kustomize/overlays/local/argocd-application-user-applications.yaml` | Point to Forgejo |
| `bootstrap/rig-system/kustomize/operations-manager/overlays/local/configmap.yaml` | Use Forgejo URLs, update domain |
| `Taskfile.yaml` | Add `setup-local-dev`, `import-local-wildcard-cert`, update CoreDNS task |
| `security/tls/local-wildcard/` | Store pre-generated wildcard cert (fullchain.pem, privkey.pem) |

---

## Resolved Decisions

- **Domain:** Use `*.local.example.com` instead of `.kind`
- **SSL:** Pre-generated Let's Encrypt wildcard cert stored in repo
- **Initial repo content:** Empty repos
- **SSO:** Make optional via `AUTH_MODE` setting
- **Secrets:** Use well-known defaults for local dev

---

## Remaining Investigation

1. **Forgejo s6-overlay issue:** Need to enable Forgejo and check logs to identify the actual permission error

---

## Certificate Renewal Process

The wildcard cert needs renewal every 90 days:
```bash
# Run on machine with DNS API access
certbot certonly --manual --preferred-challenges dns \
  -d "*.local.example.com"

# Copy renewed certs to repo
cp /etc/letsencrypt/live/local.digilab.network/fullchain.pem security/tls/local-wildcard/
cp /etc/letsencrypt/live/local.digilab.network/privkey.pem security/tls/local-wildcard/

# Commit and push
git add security/tls/local-wildcard/
git commit -m "Renew local wildcard certificate"
git push
```

This can be automated with a cron job + DNS provider API (certbot has plugins for Cloudflare, Route53, TransIP, etc.)

---

## Verification Steps

1. **Fresh machine test:**
   ```bash
   git clone <repo>
   cd RIG-Cluster
   task setup-local-dev
   # Should complete with clear feedback at each step
   ```

2. **Idempotency test:**
   ```bash
   task setup-local-dev  # Run again
   # Should succeed, skip already-done steps
   ```

3. **Forgejo integration:**
   - Access https://forgejo.local.example.com
   - Verify 3 repos exist (argo-user-applications, user-applications, rig-cluster-projects)
   - Verify ArgoCD shows apps syncing from Forgejo

4. **SSO-disabled mode:**
   - Set `OIDC_ENABLED=false` in .env.local
   - Start Operations Manager
   - Should work with API key auth only
