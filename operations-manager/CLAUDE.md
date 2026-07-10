# CLAUDE.md - Operations Manager (OPI)

Guidance for working with the Operations Manager codebase - a FastAPI application that provides self-service Kubernetes environments through GitOps.

## Working Directory

Python source code is at `operations-manager/python/`. Run all commands from there.

```bash
cd operations-manager/python
uv sync                    # Install dependencies
uv run pytest tests/ -q    # Run tests
uv run ruff check . --fix  # Lint
uv run ruff format .       # Format
uv run pyright             # Type check
```

## Module Architecture

```
opi/
|-- server.py              # FastAPI entry point, lifespan management
|
|-- api/                   # REST API endpoints
|   |-- router.py          #   Main API router
|   |-- v2/router.py       #   V2 API router
|   |-- auth.py            #   Authentication endpoints
|   |-- backup.py          #   Backup/restore endpoints
|   |-- logs.py            #   Log streaming endpoints
|   |-- metrics.py         #   Metrics endpoints
|   |-- task.py            #   Async task endpoints
|   +-- image.py           #   Image management endpoints
|
|-- web/                   # Web UI routes (Jinja2 HTML)
|   |-- router.py          #   Main web router
|   |-- project_form.py    #   Project creation form
|   |-- wizard.py          #   Multi-step wizard
|   |-- detail_edit.py     #   Project detail editing
|   |-- self_service.py    #   Self-service portal
|   +-- metrics_explorer.py #  Prometheus metrics UI
|
|-- manager/               # Business logic orchestration
|   |-- project_manager.py #   Primary orchestrator (large file)
|   |-- argo_manager.py    #   ArgoCD application lifecycle
|   |-- database_manager.py #  PostgreSQL schema/user management
|   |-- keycloak_manager.py #  Realm/client/user management
|   |-- minio_manager.py   #  Bucket provisioning
|   |-- backup_manager.py  #  PVC backup operations
|   |-- clone_manager.py   #  Database/bucket cloning
|   +-- delete_project_manager.py # Project teardown
|
|-- connectors/            # External system integrations
|   |-- git.py             #   Git operations (clone, push, SSH)
|   |-- kubectl.py         #   Kubernetes operations
|   |-- keycloak.py        #   Keycloak API client
|   |-- argo.py            #   ArgoCD API client
|   |-- postgres.py        #   PostgreSQL connections
|   |-- minio_connector.py #   MinIO/S3 operations
|   |-- kopia.py           #   Backup tool integration
|   |-- prometheus.py      #   Metrics collection
|   |-- skopeo.py          #   Container image inspection
|   |-- chisel.py          #   Reverse tunnel service
|   +-- subdomain.py       #   DNS/subdomain management
|
|-- core/                  # Application bootstrap and config
|   |-- config.py          #   Settings (env vars, cluster config)
|   |-- startup.py         #   Initialization tasks
|   |-- database_pools.py  #   AsyncPG connection pool management
|   |-- git_monitor.py     #   Git file change watcher
|   |-- metrics.py         #   Prometheus metrics definitions
|   |-- tracing.py         #   OpenTelemetry setup
|   +-- auth_decorators.py #   Authentication/authorization decorators
|
|-- generation/            # Kubernetes manifest generation
|   +-- manifests.py       #   Jinja2 template rendering
|
|-- forms/                 # Dynamic form framework
|   |-- converters.py      #   Form data conversion
|   |-- fields.py          #   Field definitions
|   +-- validation.py      #   Form validation logic
|
|-- services/              # Business logic services
|   |-- project_service.py #   Project CRUD operations
|   |-- resource_analyzer.py #  Resource usage analysis
|   |-- schema_migration.py #  Database schema migrations
|   +-- oom_watcher.py     #   OOM kill detection for auto-tuning
|
|-- handlers/              # Request processing
|   |-- project_file_handler.py # Project file CRUD
|   |-- configuration_handler.py # Config management
|   +-- sops.py            #   SOPS-specific handling
|
|-- middleware/            # HTTP middleware
|   |-- authorization.py   #   RBAC and user isolation
|   |-- csrf.py            #   CSRF protection
|   +-- session.py         #   Session handling
|
|-- utils/                 # Utility functions
|   |-- age.py             #   AGE encryption/decryption
|   |-- sops.py            #   SOPS file operations
|   |-- naming.py          #   Naming conventions (large file)
|   |-- secrets.py         #   Secret generation/management
|   +-- env_vars.py        #   Environment variable handling
|
|-- templates/             # Jinja2 HTML templates for web UI
|-- jobs/                  # Background job scheduling
|-- migrations/            # Alembic database migrations
|-- locale/                # i18n translations
|-- configs/               # Configuration presets
+-- bootstrap/             # Keycloak bootstrap during cluster init
```

Manifest templates (Jinja2 for K8s YAML generation) are at: `operations-manager/python/manifests/*.yaml.jinja`

## Key Design Patterns

1. **Connector Pattern**: ALL external system calls go through connector classes. Never call `subprocess`, `kubectl`, or external APIs directly outside connectors.
2. **Project Manager as Orchestrator**: `project_manager.py` coordinates multi-step deployments by calling managers and connectors in sequence.
3. **Distributed Model**: Each OPI instance manages only its `CLUSTER_MANAGER` cluster. Use `get_deployments(cluster_filter=True)` (default) for filtering.
4. **Dual Cryptography**: AGE (`utils/age.py`) for runtime encryption, SOPS (`utils/sops.py`) for file-based secret management.
5. **Template-Driven Generation**: Jinja2 templates in `manifests/` produce K8s resources.
6. **GitOps First**: Primary deployment via ArgoCD, with direct `kubectl` fallback.

## Logs and Debugging

OPI runs as a Kubernetes pod. There are **no local log files** to check.

```bash
# Sandbox (rig-system namespace)
kubectl logs -n rig-system deployment/operations-manager -f
kubectl logs -n rig-system deployment/operations-manager -f --previous  # after crash

# Production ODCN (rig-prd-operations namespace)
kubectl logs -n rig-prd-operations deployment/operations-manager -f

# Local dev with docker-compose
docker compose -f docker-compose.dev.yaml logs -f

# Hot-reload dev with Skaffold - API at localhost:9595
task sandbox:skaffold-dev
```

### Namespace Context

| Environment | OPI Namespace | User Namespaces |
|---|---|---|
| Sandbox | `rig-system` | `rig-{project}` |
| Production | `rig-prd-operations` | `rig-prd-{project}` |

Always specify the correct namespace when debugging. Do not assume `rig-system` - check the environment.

## Code Style

- **Modern type hints**: `dict`, `list`, `str | None` (not `Optional`, `Dict`, `List`)
- **Type annotations**: Always on function parameters and return types
- **Error handling**: Specific exceptions, no generic `except Exception`. In new methods, let exceptions bubble up.
- **No emojis** in code, comments, or log messages
- **Frontend**: Jinja2 + jinja-roos-components. Check `references/jinja_roos_copied.md`

## Utility Scripts

Standalone operational tools (Keycloak, Grafana/Loki, diagnostics, project-file maintenance) live
in `operations-manager/python/scripts/` — see `scripts/README.md` there for the full index. Run
them from `operations-manager/python` with `uv run python scripts/<tool>.py`. Prefer an existing
tool over ad-hoc `kubectl exec`; e.g. `keycloak_flow_tool.py` for auth-flow inspect/repair,
`grafana_loki_logs.py` for production logs older than ~3h. (The repo-root `/scripts` folder holds
a few shell utilities only.)

## Dependencies

- **Framework**: FastAPI, Uvicorn, Starlette
- **Database**: SQLAlchemy 2.0, AsyncPG, Alembic
- **Auth**: python-keycloak, authlib
- **UI**: Jinja2, jinja-roos-components (private package)
- **Async**: aiohttp, httpx, async-lru
- **Monitoring**: OpenTelemetry, Prometheus
- **Testing**: pytest, pytest-asyncio, playwright (E2E), vcrpy, freezegun
- **Package manager**: `uv`

## Testing

```bash
uv run pytest tests/ -q                    # All unit tests
uv run pytest tests/forms/ -q              # Form tests only
uv run pytest tests/ -k "test_name" -q     # Specific test
```

Note: Some test collection errors are pre-existing due to jinja-roos-components import chain issues in non-form tests.
