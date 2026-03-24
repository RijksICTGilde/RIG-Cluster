# Prepare

Steps to get a working development environment for the Operations Manager. Run all commands from `operations-manager/python/`.

## Python Environment

```bash
cd operations-manager/python
uv sync --all-groups        # installs all dependencies (main + test + dev)
```

This creates `.venv/` with Python 3.13 and all packages. uv handles the venv automatically — no manual `python -m venv` needed.

## Playwright (for E2E tests)

Playwright browsers are NOT installed by `uv sync`. Install them separately:

```bash
uv run playwright install chromium
```

This downloads the Chromium binary that E2E tests use. Without it, any `@pytest.mark.e2e` test will fail at browser launch.

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

## What You Do NOT Need for Unit/E2E Tests

- No running Kubernetes cluster
- No Docker / docker-compose
- No Keycloak, PostgreSQL, MinIO, or ArgoCD instances
- Tests mock all external connectors via fixtures in `tests/conftest.py`

## What You DO Need for Integration/Functional Tests

- A running sandbox cluster (`task sandbox:setup`)
- Or a docker-compose environment (`docker compose -f docker-compose.dev.yaml up`)
- These are marked `@pytest.mark.requires_infra` and excluded from default test runs
