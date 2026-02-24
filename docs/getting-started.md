# Getting Started with RIG-Cluster

This guide walks you through setting up a local RIG-Cluster development environment using the **sandboxed-local** setup. This is the recommended approach for new developers -- it creates a fully self-contained Kubernetes cluster with all services running locally, no external dependencies needed.

> **Note:** This guide is currently macOS-focused. Linux instructions will be added in the future.

## What You'll Get

A single Kind cluster running:

| Service | URL | Credentials |
|---------|-----|-------------|
| ArgoCD | https://argo.sandbox.rijksapp.dev | admin / admin1234 |
| Forgejo (Git) | https://forgejo.sandbox.rijksapp.dev | rig-admin / admin1234 |
| Keycloak (SSO) | https://keycloak.sandbox.rijksapp.dev | admin / admin1234 |
| MinIO (S3) | https://minio.sandbox.rijksapp.dev | admin / admin1234 |
| Operations Manager | https://zad.sandbox.rijksapp.dev | - |

All services use real TLS certificates (`*.sandbox.rijksapp.dev`) and run entirely on your machine.

## Prerequisites

### 1. Install Required Tools

```bash
# macOS (using Homebrew)
brew install go-task kind kubectl kustomize sops age ksops pwgen jq yq rsync skaffold

# Verify installations
task requirements-check
```

**What each tool does:**

| Tool | Purpose |
|------|---------|
| go-task | Task runner (like Make, but better) -- runs all `task` commands |
| kind | Creates local Kubernetes clusters using Docker |
| kubectl | Kubernetes CLI |
| kustomize | Kubernetes manifest templating |
| sops | Encrypts/decrypts secrets in YAML files |
| age | Encryption backend used by SOPS |
| ksops | Kustomize plugin for decrypting SOPS-encrypted resources |
| pwgen | Generates random passwords during secret generation |
| jq | JSON processing (used in setup scripts) |
| yq | YAML processing (used in secret generation) |
| rsync | File synchronization (used to sync to in-cluster Forgejo) |
| skaffold | Hot-reload development for the Operations Manager |

### 2. Obtain the Developer Key

The TLS wildcard certificates for `*.sandbox.rijksapp.dev` are stored AGE-encrypted in the repository. You need the developer AGE private key to decrypt them during setup.

Ask the ZAD developers for the key. It starts with `AGE-SECRET-KEY-...`. During setup, you'll be prompted to paste it and it will be saved locally at `security/developer-key.txt` for future use.

### 3. DNS

The domain `*.sandbox.rijksapp.dev` resolves to `127.0.0.1` via public DNS. Verify this works on your machine:

```bash
dig +short test.sandbox.rijksapp.dev
# Should return: 127.0.0.1
```

If your network blocks DNS responses pointing to loopback addresses (DNS rebinding protection), you'll need a local workaround. See [Troubleshooting](#dns-not-resolving-in-browser).

### 4. Ensure Docker Is Running

```bash
docker ps
```

## Setup

One command does everything:

```bash
task sandbox:setup
```

This takes approximately 5-10 minutes and will:

1. Verify all tools are installed
2. Generate encryption keys and SOPS-encrypted secrets
3. Optionally configure SSO via production Keycloak (for real user authentication)
4. Create a Kind cluster (`rig-sandbox`)
5. Install NGINX Ingress Controller and CNPG operator
6. Configure in-cluster DNS for `*.sandbox.rijksapp.dev`
7. Import the wildcard TLS certificate
8. Deploy PostgreSQL and Forgejo
9. Initialize Forgejo with Git repositories
10. Sync infrastructure configuration to Forgejo
11. Install and bootstrap ArgoCD

When complete, you'll see a summary with all service URLs.

### SSO Configuration (Optional)

During setup, you'll be asked whether to configure SSO. This connects the sandbox to the production Keycloak (`keycloak.rijksapp.nl`) for real SSO-Rijk authentication. Without it, the sandbox works with a local admin user only.

To configure SSO you need the client secret for the `development-clusters` client from the production Keycloak's `rig-platform` realm. See [Sandbox SSO Setup](../features/sandbox-sso-setup.md) for details.

## Verify

After setup completes, check that everything is running:

```bash
# All pods should be Running or Completed
kubectl get pods -n rig-system

# ArgoCD should show 2 applications
kubectl get applications -n rig-system
```

Open https://argo.sandbox.rijksapp.dev in your browser (admin / admin1234) to see ArgoCD syncing infrastructure.

## Daily Development Workflow

### Making Infrastructure Changes

1. Edit files under `infrastructure/`
2. Sync to Forgejo:
   ```bash
   task sandbox:sync
   ```
3. ArgoCD detects the change and deploys automatically

### Developing the Operations Manager

For hot-reload development (automatically syncs code changes to the running pod):

```bash
# Create .env.local if it doesn't exist (required by the Docker build)
touch operations-manager/python/.env.local

task sandbox:skaffold-dev
```

This starts Skaffold, which watches for file changes in `operations-manager/python/` and syncs them into the running container. The Operations Manager is port-forwarded to `localhost:9595`.

To do a full rebuild instead (rebuild Docker image and redeploy):

```bash
task sandbox:update-operations-manager
```

### Checking Service Status

```bash
# ArgoCD applications
kubectl get applications -n rig-system

# All pods
kubectl get pods -n rig-system

# Logs for a specific service
kubectl logs -n rig-system deployment/operations-manager -f
kubectl logs -n rig-system forgejo-0 -f
```

## Architecture

```
Kind Cluster (rig-sandbox)
|
+-- NGINX Ingress (ports 80/443 -> host)
|   Routes *.sandbox.rijksapp.dev -> services
|
+-- Forgejo (in-cluster Git server)
|   +-- zad-projects              (project definitions)
|   +-- zad-argo-user-applications (ArgoCD app manifests)
|   +-- zad-argo-infrastructure   (synced from local infrastructure/)
|   +-- zad-deployments           (generated by Operations Manager)
|
+-- ArgoCD (GitOps controller)
|   +-- sandbox-infrastructure    -> reads from zad-argo-infrastructure
|   +-- user-applications         -> reads from zad-argo-user-applications
|
+-- PostgreSQL (CNPG cluster: rig-db)
|   +-- keycloak database
|   +-- forgejo database
|
+-- Keycloak (identity provider)
+-- MinIO (S3-compatible storage)
+-- Operations Manager (self-service portal)
```

## Cleanup

### Destroy the Cluster

```bash
task sandbox:destroy
```

This deletes the Kind cluster and cleans up generated secrets and keys.

### Start Fresh

```bash
task sandbox:destroy
task sandbox:setup
```

The setup is fully repeatable and idempotent.

## Troubleshooting

### Port Conflicts

If ports 80 or 443 are already in use:

```bash
lsof -i :80
lsof -i :443
```

Stop the conflicting process before running setup.

### Forgejo Not Starting

Usually means PostgreSQL isn't ready yet:

```bash
kubectl get cluster rig-db -n rig-system
kubectl logs -n rig-system forgejo-0
```

### ArgoCD Not Syncing

Check the repo credentials and connectivity:

```bash
kubectl get secrets -n rig-system -l argocd.argoproj.io/secret-type=repository
kubectl exec -n rig-system deployment/argocd-server -- \
  curl -s http://forgejo.rig-system.svc.cluster.local:3000/api/healthz
```

### DNS Not Resolving in Browser

The domain `*.sandbox.rijksapp.dev` should resolve to `127.0.0.1` via public DNS. Verify with:

```bash
dig +short test.sandbox.rijksapp.dev
```

If this doesn't return `127.0.0.1`, your network may have DNS rebinding protection enabled. In that case, install dnsmasq as a local DNS resolver:

```bash
brew install dnsmasq
brew services start dnsmasq

# Add wildcard entry
echo "address=/.sandbox.rijksapp.dev/127.0.0.1" >> /opt/homebrew/etc/dnsmasq.conf
sudo brew services restart dnsmasq

# Create macOS resolver
sudo mkdir -p /etc/resolver
echo "nameserver 127.0.0.1" | sudo tee /etc/resolver/sandbox.rijksapp.dev
```

### Certificate Issues

Verify the wildcard cert is present:

```bash
kubectl get secret sandbox-wildcard-tls -n rig-system
ls -la security/tls/sandbox-wildcard/
```

## Further Reading

- [Sandboxed Local Development (feature doc)](../features/sandboxed-local-development.md) -- detailed architecture and configuration
- [Sandbox SSO Setup](../features/sandbox-sso-setup.md) -- connecting to production Keycloak for real authentication
- [Local Cluster Federation](../features/local-cluster-federation.md) -- connecting to production Keycloak
- [PVC Backup System](../features/backup-system.md) -- backup and restore
- [Kind Documentation](https://kind.sigs.k8s.io/)
- [ArgoCD Documentation](https://argo-cd.readthedocs.io/)
