"""
E2E test fixtures: app server, session signing, authenticated browser context.

Supports two modes:
- Local: starts FastAPI on a free port with mocked startup (default)
- Sandbox: connects to a running sandbox cluster via E2E_BASE_URL env var

The app starts with a known SECRET_KEY. We pre-sign a session cookie containing
a test user, injecting it into the Playwright browser context. No production
code changes needed.
"""

import base64
import json
import os
import socket
import threading
from collections.abc import Generator
from pathlib import Path
from urllib.parse import urlparse

import pytest
import uvicorn
from itsdangerous import TimestampSigner
from playwright.sync_api import BrowserContext, Page
from tests.e2e.testserver import SECRET_KEY, create_test_app

# Sandbox config — override via environment variables
E2E_BASE_URL = os.environ.get("E2E_BASE_URL", "")
E2E_SECRET_KEY = os.environ.get("E2E_SECRET_KEY", "default-secret-key-for-development-change-in-production")

TEST_USER = {
    "sub": "e2e-user",
    "email": "test@example.com",
    "name": "E2E Test User",
}

SANDBOX_TEST_USER = {
    "sub": "sandbox-e2e-user",
    "email": "admin@sandbox.rijksapp.dev",
    "name": "Sandbox E2E User",
}


def _sign_session(data: dict, secret: str = SECRET_KEY) -> str:
    """Replicate Starlette SessionMiddleware cookie signing."""
    payload = base64.b64encode(json.dumps(data).encode()).decode()
    signer = TimestampSigner(secret)
    return signer.sign(payload).decode()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args: dict) -> dict:
    """Add --no-sandbox for running in containers (e.g. Docker as root)."""
    return {**browser_type_launch_args, "args": ["--no-sandbox", "--disable-setuid-sandbox"]}


@pytest.fixture(scope="session")
def app_server() -> Generator[str]:
    """Start the real FastAPI app on a free TCP port, yield the base URL."""
    port = _free_port()

    ctx = create_test_app()
    with ctx() as app:
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)

        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        # Wait for server to start
        import time

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError(f"Server did not start on port {port}")

        yield f"http://127.0.0.1:{port}"

        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture
def authenticated_context(app_server: str, browser: "BrowserContext") -> Generator[BrowserContext]:
    """Browser context with a pre-signed session cookie containing a test user."""
    parsed = urlparse(app_server)
    context = browser.new_context()
    signed = _sign_session({"user": TEST_USER})
    context.add_cookies(
        [
            {
                "name": "session",
                "value": signed,
                "domain": parsed.hostname or "127.0.0.1",
                "path": "/",
            }
        ]
    )
    yield context
    context.close()


@pytest.fixture
def auth_page(authenticated_context: BrowserContext) -> Generator[Page]:
    """New page from the authenticated browser context."""
    page = authenticated_context.new_page()
    yield page
    page.close()


@pytest.fixture(scope="session")
def screenshot_dir() -> Path:
    """Directory for saving E2E screenshots.

    Uses E2E_SCREENSHOT_DIR env var, or defaults to a temp directory.
    """
    env_dir = os.environ.get("E2E_SCREENSHOT_DIR")
    path = Path(env_dir) if env_dir else Path(__file__).parent / "screenshots"
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- Sandbox fixtures ---


@pytest.fixture(scope="session")
def sandbox_url() -> str:
    """Base URL for sandbox cluster. Requires E2E_BASE_URL env var."""
    if not E2E_BASE_URL:
        pytest.skip("E2E_BASE_URL not set — sandbox tests require a running cluster")
    return E2E_BASE_URL


@pytest.fixture(scope="session")
def sandbox_context(browser: "BrowserContext", sandbox_url: str) -> Generator[BrowserContext]:
    """Authenticated browser context for sandbox cluster tests."""
    context = browser.new_context(ignore_https_errors=True)
    signed = _sign_session({"user": SANDBOX_TEST_USER}, secret=E2E_SECRET_KEY)
    parsed = urlparse(sandbox_url)
    context.add_cookies(
        [
            {
                "name": "session",
                "value": signed,
                "domain": parsed.hostname or "localhost",
                "path": "/",
            }
        ]
    )
    yield context
    context.close()


@pytest.fixture
def sandbox_page(sandbox_context: BrowserContext) -> Generator[Page]:
    """New page from the sandbox-authenticated browser context."""
    page = sandbox_context.new_page()
    yield page
    page.close()
