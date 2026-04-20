# Project Outline

Briefing for Claude sessions working on this repository.

## What This Project Is

- **RIG-Cluster** is a Kubernetes platform for the Dutch government (ODC-Noord) built around **ZAD** — a self-service portal where developers declare infrastructure needs in YAML
- The central application is the **Operations Manager (OPI)** — a FastAPI app that reads project YAML files and provisions everything: databases, storage, auth, K8s manifests, ArgoCD deployments
- Each cluster runs its own OPI instance. Instances never manage resources on other clusters
- Three git repos are managed by OPI: `zad-projects` (definitions), `zad-argo-user-applications` (ArgoCD apps), `zad-deployments` (generated manifests + secrets)

## Project Files

Every project on the platform is defined by a single YAML file stored in the `zad-projects` git repository. OPI reads these files and uses them to provision databases, storage, authentication, Kubernetes manifests, and ArgoCD deployments. Users create project files through the self-service wizard or the API; OPI also writes back to them (e.g. when auto-tuning resources or adding deployments).

### Schema

The canonical schema is defined as Pydantic models in `opi/forms/models/project_file.py`:

| Model | Purpose |
|---|---|
| `ProjectFileModel` | Root model — basic info, clusters, services, users, repositories, components, deployments |
| `ComponentModel` | Application component — type, ports, resources, path routing, service bindings, env vars, aliases |
| `DeploymentModel` | Deployment of components to a cluster — image references, namespace, repository, configuration |
| `RepositoryModel` | Git repository — URL, credentials, branch, path |
| `ProjectUserModel` | Team member — email and role (at least one admin required) |
| `ResourcesModel` | CPU and memory limits |
| `PortsModel` | Inbound and outbound ports |
| `DeploymentComponentModel` | Component reference within a deployment — image, pull policy |

In addition to the schema-modeled fields, project files contain OPI-managed sections that are not user-editable through forms:
- `config` — project-specific AGE keypair, API key, Keycloak credentials (all AGE-encrypted)
- `registries` — container registry credentials
- `schema-version` — currently `2`

### Key Sections

| Section | What it defines |
|---|---|
| `name` / `display-name` / `description` | Project identity |
| `users` | Team members and roles (`admin` or `developer`) |
| `clusters` | Which clusters this project targets (e.g. `odcn-production`) |
| `services` | Platform services the project uses — can be plain strings (`publish-on-web`) or dicts with config (`keycloak: {config: {template: ...}}`) |
| `registries` | Container registries with encrypted credentials |
| `repositories` | Git repositories containing application source code |
| `components` | Application components — each defines ports, resource limits, path routing, service bindings, and environment variables |
| `deployments` | Concrete deployments of components to a cluster — ties components to container images and a namespace |
| `config` | OPI-managed cryptographic material and service credentials |

### Full Example (sanitized)

Based on a real production project with three components (backend, frontend, admin frontend) and Keycloak + PostgreSQL services. AGE-encrypted values are replaced with `<AGE-encrypted>`.

```yaml
schema-version: 2
name: algor-odc
display-name: Algoritmeregister (eigen database)
description: Project created via self-service portal

users:
  - email: user@rijksoverheid.nl
    role: admin

clusters:
  - odcn-production

services:
  - publish-on-web
  - keycloak:
      config:
        template: algoritmeregister
        additional_redirect_uris:
          - http://localhost:8080/*
          - http://127.0.0.1:8080/*
  - namespace-postgresql-database:
      config:
        image: ghcr.io/rijksictgilde/algoritmeregister/postgresql-with-dictionaries:2024.11.19
        registry: github-registry
        instances: 1
        storage: 1Gi
        privileges:
          - SUPERUSER

registries:
  - name: github-registry
    url: ghcr.io
    username: someuser
    password: <AGE-encrypted>

repositories:
  - name: main-repo
    url: https://github.com/RijksICTGilde/rig-cluster-application-test.git
    username: git
    password: <AGE-encrypted>
    branch: main
    path: .
    project_name: algor-odc

components:
  - name: component-1
    type: single
    ports:
      inbound: [8000]
      outbound: [80, 443]
    path:
      - match: /aanleverapi
      - match: /api
    uses-components: []
    resources:
      cpu: '1'
      requests:
        memory: 649Mi
      limits:
        memory: 649Mi
      history:
        - timestamp: '2026-03-24T12:52:18.964665+00:00'
          limits:
            memory: 649Mi
          source: auto-tune
          deployment: deployment-1
          reason: 'Limit: max 499Mi + 25% + 25Mi headroom = 649Mi'
    aliases:
      POSTGRES_SERVER: $DATABASE_SERVER_HOST
      POSTGRES_PORT: $DATABASE_SERVER_PORT
      POSTGRES_USER: $DATABASE_SERVER_USER
      POSTGRES_PASSWORD: $DATABASE_PASSWORD
      POSTGRES_DB: $DATABASE_DB
      KEYCLOAK_URI: $OIDC_URL
      KEYCLOAK_REALM: $OIDC_REALM
      PREVIEW_URL: $PUBLIC_HOST
    user-env-vars: <AGE-encrypted>
    services:
      - publish-on-web
      - keycloak
      - namespace-postgresql-database

  - name: component-2
    type: frontend
    ports:
      inbound: [3000]
      outbound: [80, 443]
    path: /
    uses-components: []
    resources:
      requests:
        memory: 158Mi
      limits:
        memory: 158Mi
    user-env-vars: <AGE-encrypted>
    services:
      - publish-on-web
      - keycloak

  - name: component-3
    type: frontend
    ports:
      inbound: [8080]
      outbound: [80, 443]
    path: /webformulier
    uses-components: []
    resources:
      requests:
        memory: 88Mi
      limits:
        memory: 88Mi
    services:
      - publish-on-web
      - keycloak

deployments:
  - name: deployment-1
    cluster: odcn-production
    namespace: algor-odc
    repository: main-repo
    subdomain: deployment-1
    configuration: <AGE-encrypted>
    components:
      - reference: component-1
        image: ghcr.io/rijksictgilde/algoritmeregister/backend:2024.11.24-fixed
        registry: github-registry
        resources:
          requests:
            memory: 649Mi
          limits:
            memory: 649Mi
      - reference: component-2
        image: ghcr.io/rijksictgilde/algoritmeregister/frontend:2024.11.21
        registry: github-registry
        resources:
          requests:
            memory: 158Mi
          limits:
            memory: 158Mi
      - reference: component-3
        image: ghcr.io/rijksictgilde/algoritmeregister/frontend-beheer:2024.12.08
        imagePullPolicy: Always
        resources:
          requests:
            memory: 88Mi
          limits:
            memory: 88Mi

config:
  age-public-key: age1d489e9c48pmwam6603vecp7y29zz9fx5cgpe9uk6cu9l7asfzg9sx5s0tq
  age-private-key: <AGE-encrypted>
  api-key: <AGE-encrypted>
  keycloak:
    - host: https://keycloak.rijksapp.nl
      realm: algor-odc-odcn-production
      username: algor_odc_odcn_production_admin
      password: <AGE-encrypted>
```

## Two Distinct Concerns

### Infrastructure
Declarative Kustomize manifests that bootstrap the cluster itself — PostgreSQL, Keycloak, MinIO, Forgejo, Prometheus, Redis, ArgoCD, cert-manager, ingress-nginx, and more. Each component follows `base/` + `overlays/{local,sandboxed-local,odcn-production}/`. Secrets are SOPS+AGE encrypted. All operations via Taskfile (100+ tasks, no shell scripts).

### Application (OPI)
A Python 3.13 FastAPI app with a web UI (Jinja2 + ROOS design system) and a REST API. Handles the full lifecycle: project creation, database provisioning, Keycloak realm setup, MinIO buckets, manifest generation, ArgoCD management, backup/restore, resource tuning, user admin.

## Where To Find Things

| If you need to... | Look in... |
|---|---|
| Change OPI application logic | `operations-manager/python/opi/` |
| Add/modify an API endpoint | `opi/api/` (REST) or `opi/web/` (UI routes) |
| Change how OPI talks to external systems | `opi/connectors/` — **all** external calls go through connectors, never call subprocess directly |
| Change business orchestration (multi-step flows) | `opi/manager/` — `project_manager.py` is the primary orchestrator |
| Change business logic (data, analysis, CRUD) | `opi/services/` |
| Change form behavior (wizard, editing) | `opi/forms/` (editables, visualizers, wizard state) |
| Modify generated K8s manifests | `operations-manager/python/manifests/*.yaml.jinja` (28 Jinja2 templates) |
| Change infrastructure components | `infrastructure/bootstrap/infrastructure/{component}/` |
| Change OPI's own K8s deployment | `bootstrap/rig-system/kustomize/` |
| Write or read tests | `operations-manager/python/tests/` |
| Understand a feature | `features/` (68 docs) or `features/futures/` (22 planned) |
| Run any operation | `Taskfile.yaml` at repo root |

## Architecture Patterns That Matter

- **Connector Pattern**: Every external system (kubectl, git, Keycloak, ArgoCD, PostgreSQL, MinIO, Prometheus, Kopia, Skopeo, Chisel) has a dedicated connector class in `opi/connectors/`. Never bypass this — no raw subprocess or HTTP calls elsewhere
- **Manager Orchestration**: Managers in `opi/manager/` coordinate connectors and services for multi-step operations. `project_manager.py` is the main one
- **Dual Crypto**: AGE (`utils/age.py`) for runtime encryption, SOPS (`utils/sops.py`) for file-based secrets
- **GitOps First**: ArgoCD is the primary deployment mechanism. Direct kubectl is the fallback
- **Async Task System (V2)**: Generic `TaskResponse[TResult]` with 11 task types, polled via API key auth. V1 endpoints still run inline

## Testing

### Rules
- **Only run tests for changed files** — never run the full suite blindly
- **90% coverage minimum** is enforced
- Default pytest config excludes `requires_infra` and `e2e` markers automatically
- Post-dev validation is mandatory: `ruff check . --fix`, `ruff format .`, `pyright`

### Commands (all from `operations-manager/python/`)
```bash
uv run pytest tests/test_specific_file.py -x -q --tb=short   # targeted test
uv run pytest tests/forms/ -q                                  # form tests
uv run pytest tests/e2e/ -q                                    # Playwright E2E
uv run python functional_tests/run_all.py                      # integration (needs infra)
```

### Test Layout
- `tests/test_*.py` — ~149 unit tests (root level)
- `tests/forms/test_*.py` — ~11 form-specific tests
- `tests/integration/` — 7 integration tests (API, auth, kubectl)
- `tests/e2e/` — 12 Playwright browser tests
- `tests/conftest.py` — 20+ fixtures mocking connectors, services, settings
- `tests/e2e/conftest.py` — Playwright fixtures, test server setup, session signing

### Playwright E2E Specifics
- Uses **Python Playwright** (not Node.js) for browser-based UI testing
- Starts a real FastAPI server on a free TCP port with mocked startup dependencies
- Auth is handled by pre-signing session cookies with a known `SECRET_KEY` — no need to touch production auth code
- Can run against a live sandbox via `E2E_BASE_URL` env var
- Reusable helpers in `tests/e2e/helpers/` for wizard interaction, edit modals, and cleanup
- Covers: wizard create/edit/validation, self-service portal, detail pages, navigation, ROOS rendering, user admin

### Pytest Markers
`@pytest.mark.slow`, `@pytest.mark.enable_auth`, `@pytest.mark.requires_infra`, `@pytest.mark.e2e`, `@pytest.mark.sandbox`

## Code Style (Non-Obvious Rules)

- Modern type hints only: `dict`, `list`, `str | None` — never `Optional`, `Dict`, `List`
- Type annotations required on all function parameters and return types
- Specific exceptions only — no `except Exception`. Let exceptions bubble up in new code
- No emojis anywhere (code, comments, logs)
- ROOS components: always check `references/jinja_roos_copied.md` before using — many attributes are non-standard (camelCase, word-based values, Dutch icon names)
- Principles: KISS, YAGNI, SOLID, DRY — no premature abstractions, no "just in case" features

## Tech Stack Summary

FastAPI, Python 3.14, SQLAlchemy 2.0 (async) + AsyncPG, Alembic, Keycloak (python-keycloak + authlib), Jinja2 + jinja-roos-components, OpenTelemetry + Prometheus, uv (package manager), Ruff (linter/formatter), Pyright (type checker), SOPS + AGE (secrets), Playwright (E2E)
