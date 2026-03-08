# E2E Browser Testing

## What It Is

End-to-end browser tests using Playwright that exercise the real Operations Manager UI in a headless Chromium browser. Tests verify page rendering, navigation, authentication redirects, and form interactions against a running FastAPI server.

## How It Works

The test suite starts a real FastAPI app instance on a free TCP port with:
- Startup tasks patched out (no Keycloak, database, or MinIO connections needed)
- A known `SECRET_KEY` used to pre-sign session cookies
- All readiness services marked as ready

Authentication is handled by pre-signing a Starlette session cookie containing a test user, which is injected into the Playwright browser context. No production code changes are required.

## Running Tests

```bash
cd operations-manager/python

# Install dependencies (first time only)
uv sync
uv run playwright install chromium --with-deps

# Run E2E tests
uv run pytest tests/e2e/ -m e2e -v

# Or via Taskfile
task test-e2e
```

E2E tests are **excluded by default** from regular `pytest` runs via the `addopts` configuration. They only run when explicitly selected with `-m e2e` or by targeting the `tests/e2e/` directory.

## Test Structure

```
tests/e2e/
  conftest.py              # App server, session signing, authenticated context fixtures
  test_public_pages.py     # Health endpoint, root redirect, architecture page
  test_navigation.py       # Auth redirects, authenticated page loads
  test_self_service.py     # Project creation form rendering and interactions
```

## Key Fixtures

| Fixture | Scope | Description |
|---|---|---|
| `app_server` | session | Starts FastAPI on a free port, yields base URL |
| `authenticated_context` | function | Playwright browser context with pre-signed auth cookie |
| `auth_page` | function | Page from the authenticated context |
| `page` | function | Default Playwright page (unauthenticated, from pytest-playwright) |
| `browser` | session | Default Playwright browser (from pytest-playwright) |

## Adding New Tests

1. Create a test file in `tests/e2e/` or add to an existing one
2. Mark tests with `@pytest.mark.e2e` (or use `pytestmark = pytest.mark.e2e` at module level)
3. Use `auth_page` for authenticated scenarios, `page` for unauthenticated
4. Use `app_server` to get the base URL

```python
import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

def test_my_page(app_server: str, auth_page: Page) -> None:
    auth_page.goto(f"{app_server}/my-page")
    assert auth_page.locator("h1").text_content() == "Expected Title"
```

## Requirements

- Python 3.13+
- `playwright` and `pytest-playwright` (included in test dependencies)
- Chromium browser (`uv run playwright install chromium --with-deps`)
