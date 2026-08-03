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
import contextlib
import faulthandler
import json
import os
import socket
import threading
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import pytest
import uvicorn
from itsdangerous import TimestampSigner
from tests.e2e.helpers.forgejo import ForgejoClient
from tests.e2e.testserver import SECRET_KEY, create_test_app

if TYPE_CHECKING:
    from collections.abc import Generator

    from playwright.sync_api import BrowserContext, Page

# Sandbox config - override via environment variables
E2E_BASE_URL = os.environ.get("E2E_BASE_URL", "")
# Sandbox tests sign cookies for an already-running cluster, so the secret
# must match what that cluster uses. The default is only useful when no
# sandbox tests are actually run.
E2E_SECRET_KEY = os.environ.get("E2E_SECRET_KEY", "sandbox-e2e-test-secret-key-min32chars")

# Forgejo (zad-projects repo) - the primary "did it work?" verification source.
# Defaults match the sandbox cluster configmap.
FORGEJO_URL = os.environ.get("FORGEJO_URL", "https://forgejo.sandbox.rijksapp.dev")
FORGEJO_USER = os.environ.get("FORGEJO_USER", "rig-admin")
FORGEJO_PASSWORD = os.environ.get("FORGEJO_PASSWORD", "admin1234")
FORGEJO_PROJECTS_REPO = os.environ.get("FORGEJO_PROJECTS_REPO", "rig-admin/zad-projects")
FORGEJO_VERIFY_SSL = os.environ.get("FORGEJO_VERIFY_SSL", "true").lower() not in ("0", "false", "no")

# Playwright tracing / screenshots of events. Trace defaults on for sandbox runs.
E2E_TRACE = os.environ.get("E2E_TRACE", "1").lower() not in ("0", "false", "no")

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


@pytest.fixture(autouse=True)
def _quiet_faulthandler_for_e2e() -> None:
    """Disarm pytest's per-test faulthandler timer for E2E tests.

    The repo sets ``faulthandler_timeout = 60`` globally, which is a useful hang
    net for the fast unit tests. But sandbox E2E tests legitimately run for
    minutes (they wait on real ArgoCD sync and CNPG clusters coming up), so that
    timer fires mid-test and dumps every thread's stack -- pure noise that reads
    like a failure ("Timeout (0:01:00)!"). pytest arms the timer once for the whole
    test protocol; cancelling it here (during setup) leaves it disarmed through the
    long call phase, for E2E only, leaving the stricter unit-test setting untouched.
    Per-test timeouts still come from the helpers' own bounded waits."""
    faulthandler.cancel_dump_traceback_later()


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
def authenticated_context(app_server: str, browser: BrowserContext) -> Generator[BrowserContext]:
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


@pytest.fixture(scope="session")
def artifact_dir() -> Path:
    """Directory for saving traces and event screenshots (E2E_ARTIFACT_DIR)."""
    env_dir = os.environ.get("E2E_ARTIFACT_DIR")
    path = Path(env_dir) if env_dir else Path(__file__).parent / "artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Expose each phase's result on the item so fixtures can react to failures."""
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


# --- Sandbox fixtures ---


@pytest.fixture(scope="session")
def forgejo() -> ForgejoClient:
    """Read-only Forgejo client for verifying project files in zad-projects."""
    return ForgejoClient(
        base_url=FORGEJO_URL,
        username=FORGEJO_USER,
        password=FORGEJO_PASSWORD,
        repo=FORGEJO_PROJECTS_REPO,
        verify_ssl=FORGEJO_VERIFY_SSL,
    )


@pytest.fixture(scope="session")
def sandbox_url() -> str:
    """Base URL for sandbox cluster. Requires E2E_BASE_URL env var."""
    if not E2E_BASE_URL:
        pytest.skip("E2E_BASE_URL not set - sandbox tests require a running cluster")
    return E2E_BASE_URL


@pytest.fixture(scope="session")
def sandbox_context(browser: BrowserContext, sandbox_url: str) -> Generator[BrowserContext]:
    """Authenticated browser context for sandbox cluster tests.

    When E2E_TRACE is enabled, tracing is started once on the context; per-test
    trace chunks are produced by the sandbox_page fixture.
    """
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
    if E2E_TRACE:
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
    yield context
    if E2E_TRACE:
        context.tracing.stop()
    context.close()


@pytest.fixture
def sandbox_page(
    request: pytest.FixtureRequest,
    sandbox_context: BrowserContext,
    artifact_dir: Path,
) -> Generator[Page]:
    """New page from the sandbox-authenticated browser context.

    Records a per-test Playwright trace chunk (screenshots of every action) and,
    on test failure, saves a full-page screenshot - both into artifact_dir.
    """
    safe_name = request.node.name.replace("/", "_").replace("::", "_")
    if E2E_TRACE:
        sandbox_context.tracing.start_chunk(title=request.node.name)
    page = sandbox_context.new_page()
    try:
        yield page
    finally:
        if getattr(request.node, "rep_call", None) is not None and request.node.rep_call.failed:
            with contextlib.suppress(Exception):
                page.screenshot(path=str(artifact_dir / f"FAILED-{safe_name}.png"), full_page=True)
        if E2E_TRACE:
            sandbox_context.tracing.stop_chunk(path=str(artifact_dir / f"trace-{safe_name}.zip"))
        page.close()


@pytest.fixture
def capture(artifact_dir: Path, request: pytest.FixtureRequest):
    """Return a helper to save a named full-page screenshot at a specific event.

    Usage: capture(page, "before-submit")
    """
    safe_name = request.node.name.replace("/", "_").replace("::", "_")

    def _capture(page: Page, label: str) -> Path:
        path = artifact_dir / f"{safe_name}-{label}.png"
        page.screenshot(path=str(path), full_page=True)
        return path

    return _capture
