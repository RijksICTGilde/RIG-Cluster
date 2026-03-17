# Plan: Playwright Wizard Screenshot Tests

## Status: MOSTLY COMPLETE - 6/8 tests pass, 2 need fixing

## What's Done

All code is committed and pushed to `claude/editwizard` (commit 3abcce1):

| File | Status | Description |
|------|--------|-------------|
| `tests/e2e/testserver.py` | DONE | Test server module - standalone + fixture-compatible |
| `tests/e2e/fixtures/projects/test-project.yaml` | DONE | Realistic test project YAML |
| `tests/e2e/fixtures/projects/test-project-with-services.yaml` | DONE | Test project with Keycloak + PostgreSQL |
| `tests/e2e/conftest.py` | DONE | Refactored `app_server` to use testserver, added `screenshot_dir` fixture |
| `tests/e2e/helpers/wizard.py` | DONE | Added fill_services, fill_domain, screenshot, get_visible_step_titles |
| `tests/e2e/test_wizard_flows.py` | DONE | 8 wizard flow test scenarios |
| `features/e2e-ui-testing.md` | DONE | Feature documentation |

## What's Done Since Last Session

- Chromium system deps installed via `uv run playwright install-deps chromium`
- Added `--no-sandbox` fixture to conftest.py for container environments
- Screenshot test passes - 9 screenshots generated (all wizard steps)
- Screenshots committed and pushed to `claude/editwizard`

## Current Test Results (6/8 pass)

| Test | Status | Issue |
|------|--------|-------|
| test_screenshots_each_step | PASS | |
| test_wizard_with_keycloak_service | PASS | |
| test_wizard_with_postgresql_service | PASS | |
| test_back_navigation_preserves_data | PASS | |
| test_validation_blocks_advance | PASS | |
| test_conditional_steps_hidden | PASS | |
| test_full_wizard_no_services | FAIL | Review page doesn't show project name in body text |
| test_review_shows_summary | FAIL | Same - `name in body` assertion fails on review page |

## What Needs Fixing

The 2 failing tests assert `project_name in page.text_content("body")` on the review page.
The review page renders but the project name may be in an attribute or different element.
Need to investigate what the review page actually shows and fix the assertions.

## Key Design Decisions

### Test Server (`testserver.py`)
- `create_test_app()` returns a context manager that patches all external deps
- Standalone mode: `uv run python -m tests.e2e.testserver` on port 8111, OIDC disabled
- Mocks: `run_startup_tasks`, `ensure_projects_fresh`, `SubdomainConnector.get_by_subdomain`, `process_project_yaml_background`

### Conftest Refactor
- `app_server` fixture now delegates to `testserver.create_test_app()` (DRY)
- Added `screenshot_dir` fixture (configurable via `E2E_SCREENSHOT_DIR` env var)

### Test Scenarios (8 tests)
1. `test_full_wizard_no_services` - full flow → submit → redirect
2. `test_wizard_with_keycloak_service` - select Keycloak → config step
3. `test_wizard_with_postgresql_service` - select PostgreSQL → config step
4. `test_back_navigation_preserves_data` - back button preserves form data
5. `test_validation_blocks_advance` - empty fields block advancement
6. `test_review_shows_summary` - review page shows entered data
7. `test_conditional_steps_hidden` - no services → no service config steps
8. `test_screenshots_each_step` - screenshot every wizard step

## Troubleshooting

If Chromium still fails after image rebuild, check:
```bash
# Verify system libs are present
ldd /home/claude/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell 2>&1 | grep "not found"
```

If libs are still missing, tell DistributedClaude which specific ones. The request file is at `/messages/dclaude-DistributedClaude/playwright-deps-request.md`.
