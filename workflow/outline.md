# Project Outline

Briefing for Claude sessions working on this repository.

## What This Project Is

- **RIG-Cluster** is a Kubernetes platform for the Dutch government (ODC-Noord) built around **ZAD** — a self-service portal where developers declare infrastructure needs in YAML
- The central application is the **Operations Manager (OPI)** — a FastAPI app that reads project YAML files and provisions everything: databases, storage, auth, K8s manifests, ArgoCD deployments
- Each cluster runs its own OPI instance. Instances never manage resources on other clusters
- Three git repos are managed by OPI: `zad-projects` (definitions), `zad-argo-user-applications` (ArgoCD apps), `zad-deployments` (generated manifests + secrets)

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

FastAPI, Python 3.13, SQLAlchemy 2.0 (async) + AsyncPG, Alembic, Keycloak (python-keycloak + authlib), Jinja2 + jinja-roos-components, OpenTelemetry + Prometheus, uv (package manager), Ruff (linter/formatter), Pyright (type checker), SOPS + AGE (secrets), Playwright (E2E)
