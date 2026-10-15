# Prepare

Steps to get a working development environment for the Operations Manager. Run all commands from `operations-manager/python/`.

## Python Environment

```bash
cd operations-manager/python
uv sync --all-groups        # installs all dependencies (main + test + dev)
```

This creates `.venv/` with Python 3.14 and all packages. uv handles the venv automatically - no manual `python -m venv` needed.

## Playwright (for E2E tests)

Playwright browsers are NOT installed by `uv sync`. Install them separately:

```bash
uv run playwright install chromium
```

This downloads the Chromium binary that E2E tests use. Without it, any `@pytest.mark.e2e` test will fail at browser launch.

## `PROJECT_REPO_PASSWORD` (for the wizard E2E tests)

De wizardtests laten de app een projectbestand schrijven, en
`generate_self_service_project_yaml` (`opi/utils/project_utils.py`) weigert met een HTTP 500
zolang `PROJECT_REPO_PASSWORD` niet gezet is. Staat hij niet in de omgeving, dan vallen de
tests die de wizard doorlopen om op "geen projectbestand", zonder dat er iets met de code mis
is.

`task test-e2e` en `task test-e2e-random` zetten zelf een default (`plain:repo-secret`). Draai
je pytest met de hand, zet hem dan mee:

```bash
PROJECT_REPO_PASSWORD=plain:repo-secret uv run pytest tests/e2e/ -m "e2e and not sandbox"
```

Alleen de VORM telt hier (het `plain:`-voorvoegsel plus een waarde), dus dit is geen secret.
De echte waarde hoort in de uitrolconfiguratie van een cluster.

## Verify Setup

```bash
uv run pytest tests/test_project_service.py -x -q --tb=short   # quick smoke test (unit)
uv run ruff check . --fix                                        # lint
uv run pyright                                                   # type check
```

If these pass, the environment is ready.

## E2E Test Verification

```bash
uv run pytest tests/e2e/test_public_pages.py -x -q --tb=short
```

This starts a FastAPI test server with mocked dependencies and runs Playwright against it. No cluster or external services needed.

## Live-Sandbox E2E Tests (optional)

The E2E suite can also run against a live sandbox cluster (a local Kind cluster reachable at `https://zad.sandbox.rijksapp.dev` - see `workflow/sandbox.md`). These verify the real UI, API, and project-file writes end to end.

```bash
task sandbox:setup          # if no sandbox is running yet (~5-10 min)
task test-e2e-sandbox       # runs tests/e2e/ with -m "e2e and sandbox and not reallife and not punt14"
```

Without a running sandbox (`E2E_BASE_URL` unset), all `@pytest.mark.sandbox` tests skip automatically. The local E2E suite (`-m "e2e and not sandbox"`) needs no cluster.

## What You Do NOT Need for Unit/E2E Tests

- No running Kubernetes cluster
- No Docker / docker-compose
- No Keycloak, PostgreSQL, MinIO, or ArgoCD instances
- Tests mock all external connectors via fixtures in `tests/conftest.py`

## What You DO Need for Integration Tests

- A running sandbox cluster (`task sandbox:setup`)
- Or a docker-compose environment (`docker compose -f docker-compose.dev.yaml up`)
- The infra-dependent tests are marked `@pytest.mark.requires_infra` and excluded from default test runs; the Kind-based ones in `tests/integration/` self-skip when Docker/kind/kubectl are missing
