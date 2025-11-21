# Local Kind Cluster Setup Guide

This guide explains how to set up a throw-away local Kubernetes cluster using Kind for developing and testing the RIG-Cluster infrastructure.

## Overview

The RIG-Cluster can be deployed on a local Kind (Kubernetes in Docker) cluster for development and testing. This setup includes:

- **Kind cluster** with Kubernetes v1.32.0
- **NGINX Ingress Controller** for routing HTTP traffic
- **CloudNativePG (CNPG)** operator for PostgreSQL management
- **ArgoCD** for GitOps deployments
- **Forgejo** as a local Git server for user application repositories
- **Infrastructure services**: PostgreSQL, Keycloak, MinIO, Chisel, Operations Manager

## Prerequisites

### Required Tools

Install these tools before proceeding:

```bash
# macOS (using Homebrew)
brew install kind kubectl kustomize sops age pwgen go-task docker

# Verify installations
kind version
kubectl version --client
kustomize version
sops --version
age --version
task --version
docker --version
```

### Tool Versions

- **kind**: Latest
- **kubectl**: 1.29+
- **kustomize**: 5.0+
- **sops**: 3.8+
- **age**: 1.1+
- **Docker**: 20.10+ (running and accessible)
- **task**: 3.0+

## Quick Start

For the impatient, here's the one-command setup:

```bash
# Select local cluster configuration
task select-cluster
# Choose option 1 for local cluster

# Complete automated setup
task setup-local-cluster
```

This will:
1. Create the Kind cluster
2. Install required operators
3. Configure CoreDNS
4. Prepare ArgoCD operator
5. Build and load Docker images
6. Generate encryption keys and secrets

Then follow the "Next steps" output to complete the setup.

## Manual Step-by-Step Setup

If you prefer to understand each step or need to troubleshoot:

### 1. Select Cluster Configuration

```bash
task select-cluster
# Choose option 1 for local cluster
```

This creates `.env-taskfile-current` with local cluster settings.

### 2. Create Kind Cluster

```bash
task create-local-kind-cluster
```

This creates a cluster named `gitops-fluxcd` using `kind-config.yaml` with:
- Kubernetes v1.32.0
- Port mappings (80:80, 443:443)
- Ingress-ready node labels

**Verify:**
```bash
kind get clusters
kubectl cluster-info
kubectl get nodes
```

### 3. Install NGINX Ingress Controller

```bash
task install-ingress-nginx
```

Installs the NGINX Ingress Controller for Kind, which handles routing to services via `*.kind` domains.

**Verify:**
```bash
kubectl get pods -n ingress-nginx
kubectl wait --namespace ingress-nginx \
  --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller \
  --timeout=90s
```

### 4. Install CloudNativePG Operator

```bash
task install-cnpg-operator
```

Installs the CNPG operator v1.23.0 for managing PostgreSQL clusters.

**Verify:**
```bash
kubectl get pods -n cnpg-system
kubectl get crds | grep postgresql
```

### 5. Configure CoreDNS for *.kind Domains

```bash
task configure-coredns-kind-domains
```

Patches CoreDNS to rewrite `*.kind` domains to the ingress controller, enabling internal DNS resolution.

**Verify:**
```bash
kubectl run -it --rm --restart=Never --image=busybox:1.28 dnstest -- nslookup keycloak.kind
```

### 6. Prepare ArgoCD Operator

```bash
task prepare-argocd-operator
```

Downloads and customizes the ArgoCD operator for local development:
- Disables webhooks
- Applies fake Prometheus CRD
- Builds operator manifest

**Verify:**
```bash
ls -l bootstrap/crd/operator/argocd-operator-install.yaml
```

### 7. Build and Load Docker Images

#### CMP Kustomize SOPS Image (Required)

```bash
task update-cmp-kustomize-sops
```

Builds the ArgoCD Config Management Plugin for processing SOPS-encrypted kustomize files.

#### Operations Manager (Required)

```bash
task update-operations-manager
```

Builds and loads the Operations Manager image.

#### PostgreSQL with Dictionaries (Optional)

```bash
task build-postgresql-image
```

Builds a custom PostgreSQL image with Dutch/English full-text search support (only if needed).

**Verify all images:**
```bash
docker exec -it gitops-fluxcd-control-plane crictl images | grep -E "(rig-cmp|operations-manager|postgresql)"
```

### 8. Generate AGE Encryption Key

```bash
task generate-age-key
```

Creates an AGE key pair at `security/key.txt` for SOPS encryption.

**Important:** This file is gitignored and must be backed up securely.

**Verify:**
```bash
ls -l security/key.txt
head -1 security/key.txt  # Should show "# created:"
```

### 9. Generate Bootstrap Secrets

```bash
task generate-bootstrap-secrets-for-cluster
```

Generates SOPS-encrypted secrets for ArgoCD:
- Admin credentials
- Repository credentials
- Git daemon access

Creates `secrets-overview-bootstrap-local.yaml` with plaintext passwords - **copy and delete this file**.

**Verify:**
```bash
ls -l bootstrap/rig-system/kustomize/overlays/local/*.sops.yaml
```

### 10. Generate Infrastructure Secrets

```bash
task generate-infrastructure-secrets-for-cluster
```

Generates SOPS-encrypted secrets for infrastructure services:
- PostgreSQL admin password
- Keycloak admin credentials
- Keycloak database credentials
- Forgejo database credentials
- MinIO admin credentials

Creates `secrets-overview-infrastructure-local.yaml` - **copy and delete this file**.

**Verify:**
```bash
ls -l infrastructure/bootstrap/infrastructure/secrets/config/overlays/local/*.sops.yaml
```

### 11. Create SOPS Age Key Secret in Kubernetes

```bash
kubectl create namespace rig-system
kubectl create secret generic sops-age-key \
  --from-file=key=security/key.txt \
  -n rig-system
```

This allows ArgoCD to decrypt SOPS-encrypted files.

**Verify:**
```bash
kubectl get secret sops-age-key -n rig-system
```

### 12. Start Git Daemon

In a **separate terminal**, run from the repository root:

```bash
git daemon \
  --base-path=. \
  --export-all \
  --reuseaddr \
  --informative-errors \
  --verbose \
  --enable=upload-pack \
  --port=9090 \
  --listen=0.0.0.0 \
  --log-destination=stderr
```

This serves the infrastructure repository to ArgoCD at `git://host.docker.internal:9090/`.

**Verify:**
```bash
# In another terminal
git ls-remote git://localhost:9090/
```

### 13. Bootstrap ArgoCD System

```bash
task bootstrap-argo-system
```

Deploys ArgoCD and the Operations Manager to the `rig-system` namespace.

**Verify:**
```bash
kubectl get pods -n rig-system
kubectl get applications -n rig-system
```

### 14. Wait for Infrastructure Deployment

ArgoCD will automatically deploy all infrastructure components:
- PostgreSQL cluster
- Forgejo (Git server)
- Keycloak (SSO)
- MinIO (S3 storage)
- Chisel (tunneling)
- Operations Manager

**Monitor progress:**
```bash
# Watch pods
kubectl get pods -n rig-system -w

# Check ArgoCD applications
kubectl get applications -n rig-system

# View ArgoCD logs
kubectl logs -n rig-system deployment/argocd-server -f
```

### 15. Access Services

Add to `/etc/hosts`:
```
127.0.0.1 argo.kind keycloak.kind minio.kind operations-manager.kind forgejo.kind
```

**Service URLs:**
- **ArgoCD**: http://argo.kind (admin / admin)
- **Forgejo**: http://forgejo.kind (admin / admin)
- **Operations Manager**: http://operations-manager.kind
- **Keycloak**: http://keycloak.kind (admin / <from secrets>)
- **MinIO**: http://minio.kind (admin / <from secrets>)

**Port forwarding alternative:**
```bash
# ArgoCD
kubectl port-forward svc/argocd-server -n rig-system 8080:80

# Forgejo
kubectl port-forward svc/forgejo -n rig-system 3000:3000
```

## Infrastructure Components

### PostgreSQL (CloudNativePG)

**Namespace:** `rig-system`
**Cluster name:** `rig-db`
**Managed roles:**
- `keycloak` - Keycloak database
- `forgejo` - Forgejo database

**Connection:**
```bash
# Read-write service
rig-db-rw.rig-system.svc.cluster.local:5432

# Read-only service
rig-db-ro.rig-system.svc.cluster.local:5432
```

**Access:**
```bash
kubectl exec -it rig-db-1 -n rig-system -- psql -U postgres
```

### Forgejo (Local Git Server)

**Purpose:** Stores user application repositories and ArgoCD application manifests.

**Repositories created by bootstrap:**
1. `admin/user-applications` - User application code
2. `admin/argo-user-applications` - ArgoCD application manifests

**Web UI:** http://forgejo.kind
**Credentials:** admin / admin

**Operations Manager Integration:**
The Operations Manager automatically uses Forgejo for local development:
- `GIT_PROJECTS_SERVER_URL`: Points to Forgejo
- `GIT_ARGO_APPLICATIONS_URL`: Points to Forgejo
- No code changes needed - configuration-driven

**Repository URLs:**
```
http://forgejo.rig-system.svc.cluster.local:3000/admin/user-applications.git
http://forgejo.rig-system.svc.cluster.local:3000/admin/argo-user-applications.git
```

### ArgoCD

**Purpose:** GitOps continuous delivery for Kubernetes.

**Applications:**
- `production-infrastructure` - Deploys infrastructure components
- `user-applications` - Deploys user projects (created by Operations Manager)

**SOPS Support:**
ArgoCD includes a CMP (Config Management Plugin) sidecar that decrypts SOPS-encrypted files during sync.

### Operations Manager

**Purpose:** Self-service portal for creating and managing RIG projects.

**Features:**
- Create projects with database, Keycloak realm, MinIO buckets
- Generate ArgoCD applications
- Manage project lifecycle
- SSO integration

## Architecture

```
User → Ingress (*.kind) → Services
                          ├─ ArgoCD (GitOps controller)
                          ├─ Forgejo (Git server)
                          │   ├─ user-applications repo
                          │   └─ argo-user-applications repo
                          ├─ PostgreSQL CNPG Cluster
                          │   ├─ rig-db (main database)
                          │   ├─ keycloak DB (managed role)
                          │   └─ forgejo DB (managed role)
                          ├─ Keycloak (SSO/Identity)
                          ├─ MinIO (S3 storage)
                          └─ Operations Manager (API/UI)
                              └─ Uses Forgejo for repos
```

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
- No host machine `/etc/hosts` management for internal resolution

Alternative approaches (host file modifications, external DNS) were rejected due to maintenance overhead and inconsistent behavior across pods.

## Troubleshooting

### Cluster Issues

**Problem:** Cluster creation fails
```bash
# Check Docker is running
docker ps

# Check for port conflicts
lsof -i :80
lsof -i :443

# Remove existing cluster
task uninstall-local-kind-cluster
task create-local-kind-cluster
```

**Problem:** Can't access cluster
```bash
# Check cluster exists
kind get clusters

# Check kubeconfig
kubectl config get-contexts
kubectl config use-context kind-gitops-fluxcd

# Verify nodes
kubectl get nodes
```

### Operator Issues

**Problem:** CNPG operator not ready
```bash
# Check operator pods
kubectl get pods -n cnpg-system

# Check operator logs
kubectl logs -n cnpg-system deployment/cnpg-controller-manager

# Reinstall
kubectl delete -f https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.23/releases/cnpg-1.23.0.yaml
task install-cnpg-operator
```

**Problem:** Ingress controller not ready
```bash
# Check ingress pods
kubectl get pods -n ingress-nginx

# Check logs
kubectl logs -n ingress-nginx deployment/ingress-nginx-controller

# Reinstall
kubectl delete namespace ingress-nginx
task install-ingress-nginx
```

### Image Issues

**Problem:** ImagePullBackOff errors
```bash
# Check images in kind cluster
docker exec -it gitops-fluxcd-control-plane crictl images

# Rebuild and reload image
task update-cmp-kustomize-sops
task update-operations-manager

# Verify image present
docker exec -it gitops-fluxcd-control-plane crictl images | grep <image-name>
```

### SOPS/Encryption Issues

**Problem:** ArgoCD can't decrypt SOPS files
```bash
# Check sops-age-key secret exists
kubectl get secret sops-age-key -n rig-system

# Verify key content
kubectl get secret sops-age-key -n rig-system -o jsonpath='{.data.key}' | base64 -d | head -5

# Recreate secret
kubectl delete secret sops-age-key -n rig-system
kubectl create secret generic sops-age-key \
  --from-file=key=security/key.txt \
  -n rig-system
```

**Problem:** Can't decrypt locally
```bash
# Check SOPS_AGE_KEY_FILE
export SOPS_AGE_KEY_FILE=security/key.txt

# Verify age key
age --version
cat security/key.txt

# Test decryption
sops -d bootstrap/rig-system/kustomize/overlays/local/argocd-admin-secret.yaml.sops.yaml
```

### DNS/Networking Issues

**Problem:** Can't resolve *.kind domains
```bash
# Test DNS from within cluster
kubectl run -it --rm --restart=Never --image=busybox:1.28 dnstest -- nslookup keycloak.kind

# Check CoreDNS configuration
kubectl get configmap coredns -n kube-system -o yaml

# Reapply CoreDNS patch
task configure-coredns-kind-domains

# Restart CoreDNS
kubectl rollout restart deployment coredns -n kube-system
```

**Problem:** Can't access services from browser
```bash
# Check /etc/hosts
cat /etc/hosts | grep kind

# Add entries
sudo sh -c 'echo "127.0.0.1 argo.kind keycloak.kind minio.kind operations-manager.kind forgejo.kind" >> /etc/hosts'

# Test with curl
curl -v http://argo.kind
```

### Forgejo Issues

**Problem:** Forgejo bootstrap job fails
```bash
# Check bootstrap job
kubectl get jobs -n rig-system | grep forgejo-bootstrap

# View job logs
kubectl logs -n rig-system job/forgejo-bootstrap

# Check Forgejo pod
kubectl get pods -n rig-system -l app=forgejo
kubectl logs -n rig-system -l app=forgejo

# Rerun bootstrap
kubectl delete job forgejo-bootstrap -n rig-system
kubectl apply -k infrastructure/bootstrap/infrastructure/forgejo/config/base
```

**Problem:** Can't access Forgejo
```bash
# Check Forgejo pod
kubectl get pods -n rig-system -l app=forgejo

# Check service
kubectl get svc forgejo -n rig-system

# Check ingress
kubectl get ingress forgejo -n rig-system

# Port forward
kubectl port-forward svc/forgejo -n rig-system 3000:3000
# Access at http://localhost:3000
```

**Problem:** Repositories not created
```bash
# Check if admin user exists
kubectl exec -n rig-system forgejo-0 -- forgejo admin user list

# Check database connection
kubectl exec -n rig-system forgejo-0 -- forgejo doctor check --run database

# Manually create repos via UI
# Login to http://forgejo.kind with admin/admin
# Create repositories manually
```

### ArgoCD Issues

**Problem:** ArgoCD applications stuck in sync
```bash
# Check application status
kubectl get applications -n rig-system

# Describe application
kubectl describe application production-infrastructure -n rig-system

# Check ArgoCD logs
kubectl logs -n rig-system deployment/argocd-server
kubectl logs -n rig-system deployment/argocd-repo-server

# Manually sync
kubectl patch application production-infrastructure -n rig-system --type merge -p '{"metadata":{"annotations":{"argocd.argoproj.io/refresh":"normal"}}}'
```

**Problem:** Git daemon connection fails
```bash
# Check git daemon is running
ps aux | grep "git daemon"

# Test git daemon
git ls-remote git://localhost:9090/

# Restart git daemon
# Kill existing process
pkill -f "git daemon"

# Start again
git daemon --base-path=. --export-all --reuseaddr --informative-errors --verbose --enable=upload-pack --port=9090 --listen=0.0.0.0 --log-destination=stderr
```

## Cleanup

### Remove Everything

```bash
task uninstall-local-kind-cluster
```

This deletes the entire Kind cluster and all resources.

### Remove Specific Components

```bash
# Delete ArgoCD
kubectl delete -k bootstrap/rig-system/kustomize/overlays/local

# Delete infrastructure
kubectl delete -k infrastructure/bootstrap/clusters/local

# Delete namespace
kubectl delete namespace rig-system
```

## Tips and Best Practices

### Development Workflow

1. **Make infrastructure changes** in the repository
2. **Git daemon serves changes** automatically
3. **ArgoCD detects changes** and syncs (or manually refresh)
4. **Verify in cluster** using kubectl

### Secret Management

- Never commit `security/key.txt` to git
- Back up AGE key securely
- Delete `secrets-overview-*.yaml` files after copying passwords
- Rotate secrets regularly

### Resource Management

- **Storage:** Kind uses Docker volumes, clean up old clusters regularly
- **Images:** Use `docker system prune` to clean up unused images
- **Logs:** View logs with `kubectl logs` instead of storing them

### Iterating Quickly

```bash
# Quick rebuild and update operations manager
task update-operations-manager

# Quick restart of ArgoCD
kubectl rollout restart deployment -n rig-system

# Force ArgoCD sync
kubectl patch application production-infrastructure -n rig-system --type merge -p '{"metadata":{"annotations":{"argocd.argoproj.io/refresh":"hard"}}}'
```

## Next Steps

Once the cluster is running:

1. **Explore ArgoCD UI** at http://argo.kind
2. **Create a test project** via Operations Manager
3. **Browse Forgejo repos** at http://forgejo.kind
4. **Configure Keycloak realms** for SSO testing
5. **Test MinIO buckets** for S3 storage

## References

- [Kind Documentation](https://kind.sigs.k8s.io/)
- [CloudNativePG Documentation](https://cloudnative-pg.io/)
- [ArgoCD Documentation](https://argo-cd.readthedocs.io/)
- [Forgejo Documentation](https://forgejo.org/docs/)
- [SOPS Documentation](https://github.com/mozilla/sops)
- [AGE Encryption](https://age-encryption.org/)
