# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Interaction Guidelines

Think and act as a Principal Engineer. Be a critical thinker - do not agree with requests unless the approach is sound. Objectively outline alternatives when they exist. Always present a plan and wait for confirmation before implementing.

**Core principles**: KISS, YAGNI, SOLID, DRY. Prioritize clarity over cleverness, simplicity over flexibility. No "just in case" features, no premature abstractions.

**Planning**: Always present numbered task lists and wait for explicit confirmation before implementation.

## Architecture Overview

**RIG-Cluster** is a Kubernetes platform for RIG projects in ODC-Noord. At its core is **ZAD** (Zelfservice Applicatie Deployment) - a self-service portal where developers define their infrastructure needs in a declarative YAML project file. ZAD provisions databases, storage, authentication, generates Kubernetes manifests, and deploys via ArgoCD.

### How It Works

```
Project File (YAML)
       |
Operations Manager (OPI) - FastAPI app deployed per cluster
       |
       +-- Connectors: Git, Keycloak, PostgreSQL, MinIO, ArgoCD, kubectl
       |
       +-- Generates: K8s Manifests, Secrets, ConfigMaps, RBAC, NetworkPolicies
       |
       v
Three Git Repositories (managed by OPI):
  1. zad-projects          - project definitions
  2. zad-argo-user-applications - ArgoCD Application manifests
  3. zad-deployments       - generated K8s manifests + secrets
       |
       v
ArgoCD (GitOps) --> Kubernetes Cluster
```

### Distributed Model

Each cluster runs its own Operations Manager instance. Each instance only manages resources for its configured `CLUSTER_MANAGER` cluster - no cross-cluster resource creation.

## Repository Structure

```
RIG-Cluster/
|-- operations-manager/     # The OPI application (FastAPI + Python)
|   |-- python/             #   Python source code, tests, config
|   |   |-- opi/            #     Application modules (see operations-manager/CLAUDE.md)
|   |   |-- tests/          #     Unit tests
|   |   |-- manifests/      #     Jinja2 templates for K8s manifest generation
|   |   +-- functional_tests/ #   Integration tests
|   |-- Dockerfile          #   Multi-stage build (Python 3.13 + kubectl, SOPS, etc.)
|   +-- skaffold-*.yaml     #   Hot-reload development configs
|
|-- infrastructure/         # Infrastructure-as-Code (Kustomize)
|   +-- bootstrap/          #   Component configs (ArgoCD, PostgreSQL, Keycloak, MinIO, etc.)
|       +-- infrastructure/ #     Each component: base/ + overlays/ (local, sandboxed-local, odcn)
|
|-- bootstrap/              # Bootstrap Kustomize for rig-system namespace
|   +-- rig-system/         #   OPI deployment, secrets, overlays per cluster type
|
|-- projects/               # Example project definition files (YAML)
|-- features/               # Feature documentation (one .md per feature)
|-- docs/                   # Setup guides, known issues, post-mortems
|-- architecture/           # Architectural diagrams and overviews
|-- images/                 # Custom Docker images (e.g., postgresql-with-dictionaries)
|-- security/               # AGE keys, SOPS config (not committed to repo)
|-- scripts/                # Utility scripts
|-- weekly/                 # Release notes / weekly updates
|-- archive/                # Outdated documentation kept for reference
|-- Taskfile.yaml           # All operations (100+ tasks)
|-- kind-config.yaml        # Local Kind cluster config
+-- docker-compose.dev.yaml # Local dev (PostgreSQL + OPI)
```

## Environments and Namespaces

| Environment | Cluster Type | OPI Namespace | User Namespaces | URLs |
|---|---|---|---|---|
| Sandbox (local dev) | `sandboxed-local` (Kind) | `rig-system` | `rig-{project}` | `*.sandbox.rijksapp.dev` |
| Production (ODCN) | `odcn-production` | `rig-prd-operations` | `rig-prd-{project}` | `*.rijksapps.nl` |

**Key difference**: When checking logs, resources, or debugging:
- Sandbox: `kubectl -n rig-system ...`
- Production: `kubectl -n rig-prd-operations ...`
- User project namespaces follow the pattern above

## Logs and Debugging

The Operations Manager runs as a Kubernetes pod - there are no local log files. Always use `kubectl logs`:

```bash
# Operations Manager logs (sandbox)
kubectl logs -n rig-system deployment/operations-manager -f

# Operations Manager logs (production)
kubectl logs -n rig-prd-operations deployment/operations-manager -f

# Follow logs with previous container (after crash)
kubectl logs -n rig-system deployment/operations-manager -f --previous

# Other service logs
kubectl logs -n rig-system forgejo-0 -f
kubectl logs -n rig-system deployment/argocd-server -f
```

For local development with `docker-compose.dev.yaml`, logs are available via `docker compose logs -f`.

For hot-reload development via Skaffold, the port-forward is at `localhost:9595`.

## Kustomize Commands

```bash
# WITHOUT SOPS-encrypted secrets
kustomize build <path>

# WITH SOPS-encrypted secrets (most bootstrap/ paths)
SOPS_AGE_KEY="$(sed -n '3p' security/key.txt)" kustomize build \
  --enable-alpha-plugins --enable-exec \
  --load-restrictor LoadRestrictionsNone \
  <path>

# For sandboxed-local, use the sandbox AGE key:
SOPS_AGE_KEY="$(sed -n '3p' security/sandbox-key.txt)" kustomize build \
  --enable-alpha-plugins --enable-exec \
  --load-restrictor LoadRestrictionsNone \
  <path>

# infrastructure/ paths without SOPS generators: plain kustomize build works
kustomize build infrastructure/bootstrap/infrastructure/prometheus/controller/overlays/odcn
```

## Key Task Commands

```bash
task sandbox:setup                     # Full sandbox setup (~5-10 min)
task sandbox:sync                      # Sync infrastructure changes to Forgejo
task sandbox:skaffold-dev              # Hot-reload development (port 9595)
task sandbox:update-operations-manager # Rebuild and deploy OPI
task sandbox:destroy                   # Tear down sandbox
task requirements-check                # Verify all tools installed
```

## Project Preferences

- Use Taskfile for all operations, avoid shell scripts
- Organize kustomize resources in a base/overlays pattern
- Use GitOps principles with ArgoCD for deployment
- Use SOPS + AGE exclusively for secret management
- Keep local development workflow simple and repeatable

## Feature Documentation

When introducing a new feature, create a markdown document in `features/` with: what it is, how to use it, configuration, examples, dependencies. Use kebab-case filenames.

## Python Code Style

- **Imports**: Always at the top of the file, never inline/local. Use `ruff check --select I --fix` to sort and organize.
- **Modern type hints**: `dict`, `list`, `tuple` (lowercase), `str | None` (not `Optional`)
- **Type annotations**: Always for function parameters and return types
- **Error handling**: Specific exception types, avoid generic `except Exception`
- **Frontend**: Jinja2 + jinja-roos-components. Check `references/jinja_roos_copied.md` for component usage

## Post-Development Validation

```bash
cd operations-manager/python
uv run ruff check . --fix
uv run ruff format .
uv run pyright
```

## SOPS Secret Management

```bash
sops --encrypt --in-place path/to/secret.yaml
sops --decrypt path/to/encrypted-secret.yaml
sops path/to/encrypted-secret.yaml  # view
```
