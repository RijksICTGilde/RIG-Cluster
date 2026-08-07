# E2E Browser Testing

## What It Is

End-to-end browser tests using Playwright that exercise the real Operations Manager UI in a headless Chromium browser. Tests verify page rendering, navigation, authentication redirects, and form interactions.

Two test layers:

1. **Local tests** - run against a local FastAPI instance with mocked startup (fast, no infrastructure needed)
2. **Sandbox tests** - run against a live sandbox cluster via the full ingress path (Caddy → Kind nginx-ingress → OPI pod)

## How It Works

### Local Tests (Layer 1)

The test suite starts a real FastAPI app instance on a free TCP port with:
- Startup tasks patched out (no Keycloak, database, or MinIO connections needed)
- A known `SECRET_KEY` used to pre-sign session cookies
- All readiness services marked as ready

### Sandbox Tests (Layer 2)

Playwright connects to the sandbox via `E2E_BASE_URL` (e.g., `https://zad.sandbox.rijksapp.dev`), routing through:
```
Caddy (ports 80/443) → Kind nginx-ingress (KIND_HTTP_PORT) → OPI pod
```

Authentication uses the sandbox's `SECRET_KEY` (configurable via `E2E_SECRET_KEY` env var) to pre-sign a session cookie for the sandbox admin user (`admin@sandbox.rijksapp.dev`).

## Running Tests

```bash
cd operations-manager/python

# Install dependencies (first time only)
uv sync
uv run playwright install chromium --with-deps

# Layer 1: Local tests (no sandbox needed, ~15s)
uv run pytest tests/e2e/ -m "e2e and not sandbox" -v
task test-e2e

# Layer 2: Sandbox tests (requires running sandbox cluster)
E2E_BASE_URL=https://zad.sandbox.rijksapp.dev \
  uv run pytest tests/e2e/ -m "e2e and sandbox" -v --timeout=300
task test-e2e-sandbox
```

E2E tests are **excluded by default** from regular `pytest` runs via the `addopts` configuration.

## Test Structure

```
tests/e2e/
  conftest.py                # App server, session signing, local + sandbox fixtures
  test_public_pages.py       # Health endpoint, root redirect, architecture page
  test_navigation.py         # Auth redirects, authenticated page loads
  test_self_service.py       # Project creation form rendering
  test_wizard_validation.py  # Wizard form validation (local, no sandbox)
  test_wizard_create.py      # Full wizard walkthrough (sandbox required)
  helpers/
    wizard.py                # WizardHelper page object
```

## Key Fixtures

| Fixture | Scope | Description |
|---|---|---|
| `app_server` | session | Starts FastAPI on a free port, yields base URL |
| `authenticated_context` | function | Browser context with pre-signed auth cookie (local) |
| `auth_page` | function | Page from the authenticated context (local) |
| `sandbox_url` | session | Base URL from `E2E_BASE_URL` env var |
| `sandbox_context` | session | Browser context with sandbox auth cookie |
| `sandbox_page` | function | Page from the sandbox context |
| `page` | function | Default Playwright page (unauthenticated, from pytest-playwright) |
| `browser` | session | Default Playwright browser (from pytest-playwright) |

## WizardHelper Page Object

The `WizardHelper` class in `tests/e2e/helpers/wizard.py` encapsulates wizard interactions:

```python
from tests.e2e.helpers.wizard import WizardHelper

wizard = WizardHelper(page, base_url)
wizard.open_create_wizard()
wizard.fill_identity(display_name="my-project", description="Test")
wizard.click_next()
# ... fill other steps ...
wizard.submit_wizard()
```

Key methods: `fill_identity()`, `fill_team()`, `fill_component()`, `click_next()`, `click_previous()`, `submit_wizard()`, `has_validation_errors()`.

## Sandbox Setup

### Configurable Kind Ports

The sandbox Kind cluster supports configurable host ports via environment variables:

```bash
# Default (ports 80/443 on host)
task sandbox:setup

# Custom ports (e.g., when Caddy owns 80/443)
export KIND_HTTP_PORT=8880
export KIND_HTTPS_PORT=8443
task sandbox:setup
```

### Caddy Configuration

When running on a shared dev server where Caddy handles TLS and routing, add this snippet to the Caddyfile:

```
*.sandbox.rijksapp.dev {
    reverse_proxy localhost:8880
    tls internal
}
```

This routes all sandbox domains through Caddy to the Kind cluster's ingress.

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `KIND_HTTP_PORT` | `80` | Host port mapped to Kind's port 80 |
| `KIND_HTTPS_PORT` | `443` | Host port mapped to Kind's port 443 |
| `E2E_BASE_URL` | (none) | Sandbox URL for E2E tests |
| `E2E_SECRET_KEY` | OPI default | Secret key for signing sandbox session cookies |

## Adding New Tests

### Local Tests

1. Create a test file in `tests/e2e/`
2. Mark with `@pytest.mark.e2e`
3. Use `auth_page` + `app_server` fixtures

### Sandbox Tests

1. Mark with both `@pytest.mark.e2e` and `@pytest.mark.sandbox`
2. Use `sandbox_page` + `sandbox_url` fixtures
3. Generate unique project names with `_unique_project_name()`
4. Register created projects with the `cleanup` fixture

```python
pytestmark = [pytest.mark.e2e, pytest.mark.sandbox]

def test_my_sandbox_test(sandbox_url: str, sandbox_page: Page) -> None:
    sandbox_page.goto(f"{sandbox_url}/my-page")
    assert sandbox_page.locator("h1").text_content() == "Expected"
```

## Requirements

- Python 3.13+
- `playwright` and `pytest-playwright` (included in test dependencies)
- Chromium browser (`uv run playwright install chromium --with-deps`)
- For sandbox tests: running Kind cluster with `task sandbox:setup`
