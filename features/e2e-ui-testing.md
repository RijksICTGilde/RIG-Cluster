# E2E UI Testing

Lightweight test server and Playwright flow tests for the wizard UI.

## Overview

The E2E UI test infrastructure provides:
1. **Test server** - starts the real FastAPI app with mocked externals and fixture data
2. **Playwright flow tests** - automated wizard scenarios with screenshot capture
3. **Standalone mode** - run the server interactively for UI development

No sandbox cluster, database, Git, Keycloak, or Kubernetes required.

## Quick Start

```bash
cd operations-manager/python

# Install Playwright browsers (one-time)
uv run playwright install chromium

# Run wizard flow tests
uv run pytest tests/e2e/test_wizard_flows.py -v --timeout=60

# Run with screenshots saved to disk
E2E_SCREENSHOT_DIR=./screenshots uv run pytest tests/e2e/test_wizard_flows.py -v

# Run all E2E tests (excluding sandbox)
uv run pytest tests/e2e/ -m "e2e and not sandbox" -v
```

## Interactive Development

Start the test server standalone for manual UI testing:

```bash
cd operations-manager/python
uv run python -m tests.e2e.testserver
```

This starts the app at `http://127.0.0.1:8111` with:
- OIDC disabled (no login required)
- Fixture projects loaded from `tests/e2e/fixtures/projects/`
- All external services mocked (Git, DB, Keycloak, etc.)

## Architecture

### Test Server (`tests/e2e/testserver.py`)

The test server module provides `create_test_app()` which returns a context manager that:
- Patches `run_startup_tasks` (no DB/Git/Keycloak initialization)
- Patches `ensure_projects_fresh` (no Git refresh needed)
- Patches `SubdomainConnector.get_by_subdomain` (always returns available)
- Patches `process_project_yaml_background` (no deployment pipeline)
- Marks all readiness services as ready
- Seeds ProjectService with fixture YAML files
- Adds test user to the email allowlist

### Fixture Projects (`tests/e2e/fixtures/projects/`)

YAML files in this directory are loaded into ProjectService at startup:
- `test-project.yaml` - minimal project with one component
- `test-project-with-services.yaml` - project with Keycloak + PostgreSQL services

Het echte AGE-sleutelpaar van zo'n fixture staat niet in het bestand: de testserver munt per
run een platformsleutel, dus een blok dat met een eerdere sleutel is versleuteld gaat nooit
meer open. In de fixture staan `age-public-key` en `age-private-key` als placeholder, en
`_munt_projectsleutelpaar()` vervangt die bij het laden door een paar van deze run. Een
fixture die geen `age-private-key` draagt, krijgt niets gemunt.

Een testwaarde voor een veld dat OPI versleuteld verwacht (`config.api-key`) mag als
klaartekst in de fixture: `decrypt_password_smart` laat een waarde zonder AGE-markering
ongemoeid door. Een echt geheim hoort er niet in.

### Conftest (`tests/e2e/conftest.py`)

Provides pytest fixtures:
- `app_server` - session-scoped, starts the test server on a free port
- `authenticated_context` - browser context with pre-signed session cookie
- `auth_page` - page from the authenticated context
- `screenshot_dir` - directory for saving screenshots (configurable via `E2E_SCREENSHOT_DIR`)

### WizardHelper (`tests/e2e/helpers/wizard.py`)

Page object for wizard interaction:
- `fill_identity()`, `fill_team()`, `fill_component()` - step-specific form filling
- `fill_services()` - select service cards
- `fill_deployment()`, `fill_domain()` - additional step support
- `click_next()`, `click_previous()`, `click_review()`, `submit_wizard()` - navigation
- `screenshot()` - full-page screenshot capture
- `get_visible_step_titles()` - introspect the step indicator

## Test Scenarios

| Test | What it verifies |
|------|-----------------|
| `test_full_wizard_no_services` | Complete wizard flow without services → submit → redirect |
| `test_wizard_with_keycloak_service` | Keycloak selection → config step appears |
| `test_wizard_with_postgresql_service` | PostgreSQL selection → config step appears |
| `test_back_navigation_preserves_data` | Back button preserves form data |
| `test_validation_blocks_advance` | Empty required fields block advancement |
| `test_review_shows_summary` | Review page displays entered data |
| `test_conditional_steps_hidden` | No services → no service config steps |
| `test_screenshots_each_step` | Screenshots captured for every step |

## Adding New Tests

1. Add test functions to `tests/e2e/test_wizard_flows.py`
2. Mark with `@pytest.mark.e2e` (NOT `@pytest.mark.sandbox`)
3. Use `WizardHelper` for wizard interaction
4. Use `auth_page` fixture for authenticated access

## Wachten in een browsertest

Wacht op de voorwaarde die de test nodig heeft (het paneel staat er, het veld draagt de
waarde), niet op `networkidle`. Dat laatste is geen bovengrens: elk verzoek dat nog
binnenkomt schuift de stilte vooruit. Op een bezette host loopt de wacht daardoor tegen de
30 s-grens van Playwright aan terwijl het element er allang staat, en valt de test om op de
host en niet op de code.

Voor een tabwissel op de projectdetailpagina doet `open_tab()` (`tests/e2e/helpers/tabs.py`)
dat al: het wacht op het paneel `#tab-<tabblad>`, dus zet er geen eigen wacht achter.

## Adding Fixture Data

1. Add YAML files to `tests/e2e/fixtures/projects/`
2. Files are auto-loaded at server startup
3. Include `name`, `config.api-key`, and `users` fields
4. Geen vooraf versleuteld AGE-blok in de fixture, want de sleutels die het openen zijn
   per run nieuw:
   - `config.api-key` als klaartekst. De projectsleutel opent hem, en een AGE-blok daar
     laat `/projects/<naam>/details` op een `ValueError` lopen
   - `config.age-public-key` en `age-private-key` als placeholder, zoals de bestaande
     fixtures doen: `_munt_projectsleutelpaar()` vervangt ze per run (zie Fixture Projects
     hierboven). Zonder `age-private-key` valt er niets te munten en loopt dezelfde pagina
     op een `KeyError`
