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
|-- instructions/           # How to work in the code (read before changing a subsystem)
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

## Documentation

Read `instructions/` before changing a subsystem it covers - it holds the contracts and the
step-by-step, and saves you reverse-engineering them. Start with `instructions/README.md`,
which also states which folder is for what (`instructions/` vs `features/` vs `docs/`).

- `instructions/services.md` - the service system: what a service owns, how config, forms,
  provisioning, manifests and approvals hook in, and how to add one.

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

<!--- trying out https://github.com/multica-ai/andrej-karpathy-skills/blob/main/CLAUDE.md -->

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
