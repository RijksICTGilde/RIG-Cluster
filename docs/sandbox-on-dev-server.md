# Installing the Sandbox Cluster on the Dev Server

Guide for setting up the Kind sandbox cluster on the shared Linux dev server where Caddy and dclaude sessions run.

## Prerequisites

### Required Tools

Install all of these on the host (not inside Docker):

```bash
# Docker — must be installed and running
docker info

# Kubernetes tools
kind            # Creates K8s cluster in Docker containers
kubectl         # Kubernetes CLI
kustomize       # >= 5.0, manifest templating

# Encryption
sops            # >= 3.8, secret encryption
age-keygen      # From 'age' package, key generation

# Utilities
yq              # YAML processing
jq              # JSON processing
pwgen           # Password generation
rsync           # File sync (for Forgejo repo sync)
htpasswd        # From apache2-utils, bcrypt hashing
go-task         # The 'task' CLI (https://taskfile.dev)
```

Verify everything is installed:

```bash
task requirements-check
```

### Developer AGE Key

You need the developer AGE key (`AGE-SECRET-KEY-...`) to decrypt the wildcard TLS certificate for `*.sandbox.rijksapp.dev`. The setup will prompt for it interactively.

## Setup

### 1. Set Kind ports to avoid conflict with Caddy

Caddy owns ports 80/443, so Kind must use alternative ports:

```bash
export KIND_HTTP_PORT=8880
export KIND_HTTPS_PORT=8443
```

Add these to your shell profile (`~/.bashrc` or `~/.zshrc`) so they persist:

```bash
echo 'export KIND_HTTP_PORT=8880' >> ~/.bashrc
echo 'export KIND_HTTPS_PORT=8443' >> ~/.bashrc
```

### 2. Run the sandbox setup

```bash
cd /path/to/RIG-Cluster
task sandbox:setup
```

This is interactive — it will prompt for:
- The developer AGE key (for TLS cert decryption)
- Whether to configure production Keycloak SSO (optional, say no for testing)

Takes ~5-10 minutes. It creates:
- Kind cluster `rig-sandbox` with K8s v1.32.0
- NGINX ingress controller (on ports 8880/8443)
- CloudNativePG operator + PostgreSQL database
- Forgejo git server (admin: `rig-admin` / `admin1234`)
- ArgoCD for GitOps
- Keycloak for auth
- Operations Manager (OPI)

### 3. Add Caddy reverse proxy

Add this snippet to the Caddyfile:

```
*.sandbox.rijksapp.dev {
    reverse_proxy localhost:8880
    tls internal
}
```

Then reload Caddy:

```bash
sudo systemctl reload caddy
# or
caddy reload --config /etc/caddy/Caddyfile
```

### 4. Verify

```bash
# Health check via Caddy
curl -k https://zad.sandbox.rijksapp.dev/health

# Check OPI pod is running
kubectl get pods -n rig-system -l app=operations-manager

# Check all services
kubectl get pods -n rig-system
```

You should see: operations-manager, forgejo, keycloak, argocd, postgresql pods all running.

## Hot-Reload Development

For active development with auto-deploy on code changes:

```bash
task sandbox:skaffold-dev
```

This uses Skaffold to watch for Python code changes, rebuild the OPI container, and redeploy to the sandbox cluster. Port-forward is available at `localhost:9595`.

## Running E2E Tests

Once the sandbox is running and Caddy is configured:

```bash
# Full sandbox E2E tests
E2E_BASE_URL=https://zad.sandbox.rijksapp.dev task test-e2e-sandbox

# Or manually
cd operations-manager/python
uv run playwright install chromium --with-deps
E2E_BASE_URL=https://zad.sandbox.rijksapp.dev \
  uv run pytest tests/e2e/ -m "e2e and sandbox" -v --timeout=300
```

## Teardown

```bash
task sandbox:destroy
```

This deletes the Kind cluster and all associated Docker resources.

## Troubleshooting

### Port already in use

If Kind fails because ports are busy:

```bash
# Check what's using the ports
ss -tlnp | grep -E '8880|8443'

# Destroy and recreate
task sandbox:destroy
task sandbox:setup
```

### OPI not starting

```bash
# Check pod status
kubectl get pods -n rig-system

# Check logs
kubectl logs -n rig-system deployment/operations-manager -f

# Check previous container (after crash)
kubectl logs -n rig-system deployment/operations-manager -f --previous
```

### Caddy not routing

```bash
# Test direct Kind access (bypassing Caddy)
curl -k https://localhost:8443/health --resolve zad.sandbox.rijksapp.dev:8443:127.0.0.1

# Check Caddy logs
journalctl -u caddy -f
```

### DNS resolution

`*.sandbox.rijksapp.dev` should resolve to `127.0.0.1` via public DNS. Verify:

```bash
dig zad.sandbox.rijksapp.dev +short
# Should return 127.0.0.1
```
