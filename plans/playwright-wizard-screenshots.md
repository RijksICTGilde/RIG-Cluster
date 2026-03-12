# Plan: Playwright Wizard Screenshot Tests

## Status: PARTIALLY COMPLETE — waiting on Docker image rebuild

## What's Done

All code is committed and pushed to `claude/editwizard` (commit 3abcce1):

| File | Status | Description |
|------|--------|-------------|
| `tests/e2e/testserver.py` | DONE | Test server module — standalone + fixture-compatible |
| `tests/e2e/fixtures/projects/test-project.yaml` | DONE | Realistic test project YAML |
| `tests/e2e/fixtures/projects/test-project-with-services.yaml` | DONE | Test project with Keycloak + PostgreSQL |
| `tests/e2e/conftest.py` | DONE | Refactored `app_server` to use testserver, added `screenshot_dir` fixture |
| `tests/e2e/helpers/wizard.py` | DONE | Added fill_services, fill_domain, screenshot, get_visible_step_titles |
| `tests/e2e/test_wizard_flows.py` | DONE | 8 wizard flow test scenarios |
| `features/e2e-ui-testing.md` | DONE | Feature documentation |

## What's Blocked

**Playwright can't launch Chromium** — the developer Docker image was missing system libraries (libglib2.0, libnss3, etc.). DistributedClaude added them to the Dockerfile (commit a3535e9 on master). The image needs to be rebuilt and this session restarted.

## What To Do After Image Rebuild

1. Install Playwright browser:
   ```bash
   cd /workspace/operations-manager/python
   export PATH="/home/claude/.local/share/mise/installs/python/3.13.12/bin:$HOME/.local/bin:$PATH"
   uv sync
   uv run playwright install chromium
   ```

2. Run the wizard screenshot test:
   ```bash
   E2E_SCREENSHOT_DIR=./screenshots uv run pytest tests/e2e/test_wizard_flows.py::TestWizardScreenshots -m "e2e and not sandbox" -v --timeout=60
   ```

3. Run ALL wizard flow tests:
   ```bash
   uv run pytest tests/e2e/test_wizard_flows.py -m "e2e and not sandbox" -v --timeout=60
   ```

4. Run all E2E tests to verify nothing broke:
   ```bash
   uv run pytest tests/e2e/ -m "e2e and not sandbox" -v --timeout=60
   ```

5. View screenshots:
   ```bash
   ls -la ./screenshots/
   ```

## Key Design Decisions

### Test Server (`testserver.py`)
- `create_test_app()` returns a context manager that patches all external deps
- Standalone mode: `uv run python -m tests.e2e.testserver` on port 8111, OIDC disabled
- Mocks: `run_startup_tasks`, `ensure_projects_fresh`, `SubdomainConnector.get_by_subdomain`, `process_project_yaml_background`

### Conftest Refactor
- `app_server` fixture now delegates to `testserver.create_test_app()` (DRY)
- Added `screenshot_dir` fixture (configurable via `E2E_SCREENSHOT_DIR` env var)

### Test Scenarios (8 tests)
1. `test_full_wizard_no_services` — full flow → submit → redirect
2. `test_wizard_with_keycloak_service` — select Keycloak → config step
3. `test_wizard_with_postgresql_service` — select PostgreSQL → config step
4. `test_back_navigation_preserves_data` — back button preserves form data
5. `test_validation_blocks_advance` — empty fields block advancement
6. `test_review_shows_summary` — review page shows entered data
7. `test_conditional_steps_hidden` — no services → no service config steps
8. `test_screenshots_each_step` — screenshot every wizard step

## Troubleshooting

If Chromium still fails after image rebuild, check:
```bash
# Verify system libs are present
ldd /home/claude/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell 2>&1 | grep "not found"
```

If libs are still missing, tell DistributedClaude which specific ones. The request file is at `/messages/dclaude-DistributedClaude/playwright-deps-request.md`.
