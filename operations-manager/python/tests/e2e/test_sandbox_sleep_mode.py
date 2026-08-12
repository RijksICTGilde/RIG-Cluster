"""Live sandbox E2E for sleep-mode: sleep a deployment, then wake it via the waker page.

This drives the whole user-visible feature against a running sandbox cluster:

  1. create a minimal project (publish-on-web + a runnable app) through the real wizard;
  2. inject the ``sleep-mode`` service config (``wake-mode: confirm`` + a tiny deadline)
     into the project file and refresh the project;
  3. let the sweeper put the deployment to sleep -> a waker Deployment appears and the app
     scales to zero;
  4. open the deployment's public URL and assert the "slaapstand" confirm page with the
     "Applicatie starten" button is served by the waker;
  5. click the button, and assert the app comes back and the waker page reloads to the
     real app (the waker steps out of the EndpointSlice).

Prerequisites on the sandbox OPI (so the sweep is observable within the test timeout):
  - ``SLEEP_MODE_WAKER_IMAGE`` must resolve to an image present on the cluster
    (e.g. a ``kind load``-ed ``zad-waker`` tag), else the waker pod ImagePullBackOffs;
  - ``SLEEP_MODE_SWEEP_MINUTES`` should be small (1) so the deadline is swept promptly.

The test skips cleanly when ``E2E_BASE_URL`` is unset (no sandbox running).

**Eigen tijdsbudget.** De suite draait met ``--timeout=300`` per test, en dat is voor deze
test te krap: alleen de eigen begrensde wachtmomenten tellen al op tot ruim daarboven
(ingress 180s, app serveert 300s, in slaap 360s, pods weg 360s, wakerpagina 180s, wakker
300s, terug naar awake 120s), en daarna volgt de opruimende DELETE die tot 180s op de
verwijdertaak wacht. Zonder eigen markering viel de test daardoor om in de opruiming
NADAT alle toetsen geslaagd waren - een te klein budget, geen vastloper. De lange suites
(``test_sandbox_reallife.py``, ``test_sandbox_all_services.py``) doen hetzelfde.
"""

from __future__ import annotations

import base64
import os
import subprocess
import time
from typing import TYPE_CHECKING

import httpx
import pytest
import yaml
from playwright.sync_api import Error as PlaywrightError
from tests.e2e.helpers import sandbox_api
from tests.e2e.helpers.lifecycle import create_project_with_services

if TYPE_CHECKING:
    from playwright.sync_api import Page
    from tests.e2e.helpers.forgejo import ForgejoClient

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox]

_API_VERIFY_SSL = os.environ.get("E2E_API_VERIFY_SSL", "false").lower() in ("1", "true", "yes")
_FORGEJO_URL = os.environ.get("FORGEJO_URL", "https://forgejo.sandbox.rijksapp.dev")
_FORGEJO_AUTH = (os.environ.get("FORGEJO_USER", "rig-admin"), os.environ.get("FORGEJO_PASSWORD", "admin1234"))
_FORGEJO_REPO = os.environ.get("FORGEJO_PROJECTS_REPO", "rig-admin/zad-projects")
_FORGEJO_VERIFY = os.environ.get("FORGEJO_VERIFY_SSL", "false").lower() in ("1", "true", "yes")
_USER_EMAIL = os.environ.get("E2E_SANDBOX_USER", "admin@sandbox.rijksapp.dev")

# How long to wait for the sweeper to sleep the deployment, and for the app to come back.
_SLEEP_WAIT_S = float(os.environ.get("E2E_SLEEP_WAIT_S", "360"))
_WAKE_WAIT_S = float(os.environ.get("E2E_WAKE_WAIT_S", "300"))


def _project_contents_url() -> str:
    return f"{_FORGEJO_URL}/api/v1/repos/{_FORGEJO_REPO}/contents/projects"


def _inject_sleep_mode_config(project_name: str, deployment_name: str) -> None:
    """Add a ``sleep-mode`` service (confirm mode, 1s deadline) to the project file."""
    path = f"{_project_contents_url()}/{project_name}.yaml"
    with httpx.Client(verify=_FORGEJO_VERIFY, timeout=30.0) as client:
        meta = client.get(f"{path}?ref=main", auth=_FORGEJO_AUTH)
        meta.raise_for_status()
        blob = meta.json()
        data = yaml.safe_load(base64.b64decode(blob["content"]).decode())
        services = [s for s in (data.get("services") or []) if _service_name(s) != "sleep-mode"]
        services.append(
            {
                "name": "sleep-mode",
                "config": {
                    "enabled": True,
                    "wake-mode": "confirm",
                    "sleep-after-deploy": "1s",
                    "sleep-after-wake": "1h",
                    "match": [deployment_name],
                    "title": "Sleep demo",
                    "description": "Sleep-mode E2E",
                },
            }
        )
        data["services"] = services
        put = client.put(
            path,
            json={
                "content": base64.b64encode(yaml.safe_dump(data, sort_keys=False).encode()).decode(),
                "sha": blob["sha"],
                "message": "test: enable sleep-mode confirm on the demo deployment",
                "branch": "main",
            },
            auth=_FORGEJO_AUTH,
        )
        put.raise_for_status()


def _refresh_project(sandbox_url: str, project_name: str, api_key: str) -> None:
    with httpx.Client(verify=_API_VERIFY_SSL, timeout=60.0) as client:
        client.post(
            f"{sandbox_url}/api/v2/projects/{project_name}/:refresh",
            headers={"X-API-Key": api_key},
            json={},
        )


def _sleep_state(forgejo: ForgejoClient, project_name: str, deployment_name: str) -> str:
    data = forgejo.get_project_yaml(project_name) or {}
    for deployment in data.get("deployments", []) or []:
        if deployment.get("name") == deployment_name:
            return (deployment.get("sleep") or {}).get("state", "-")
    return "-"


def _service_name(entry: object) -> str | None:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return entry.get("name") or next(iter(entry), None)
    return None


@pytest.mark.timeout(2400)
def test_sleep_then_wake_via_waker_page(
    sandbox_url: str,
    sandbox_page: Page,
    forgejo: ForgejoClient,
    capture,
) -> None:
    project = create_project_with_services(
        sandbox_page,
        sandbox_url,
        forgejo,
        "sleepe2e",
        user_email=_USER_EMAIL,
        services=["publish-on-web"],
    )
    deployment = project.deployment_name
    try:
        # The app must be serving before we sleep it, so the wake path has something to
        # bring back. The public host is whatever the generated ingress serves.
        host = _wait_for_ingress_host(project.name)
        preview = f"https://{host}"
        assert _wait_for_http_ok(preview), f"app never served on {preview}"

        _inject_sleep_mode_config(project.name, deployment)
        _refresh_project(sandbox_url, project.name, project.api_key)

        # The sweeper stamps a deadline, then (deadline already passed) sleeps it, which
        # generates the waker Deployment. Poll the project file for state == sleeping.
        assert _wait_for_state(forgejo, project.name, deployment, "sleeping", _SLEEP_WAIT_S), (
            "deployment never reached sleeping"
        )

        # state == sleeping is written to the file first; the app scaling to zero and the
        # waker becoming Ready follow via ArgoCD, which can lag on a busy cluster. Wait for
        # the app to actually be gone before expecting the waker page, so we do not read the
        # still-serving real app and mistake it for "never slept".
        assert _wait_for_app_gone(project.name, _SLEEP_WAIT_S), "app never scaled to zero"

        # The waker pod then needs a moment to become Ready before the ingress routes to it;
        # until then the URL has no endpoint (empty/502). Retry until the confirm page shows.
        assert _wait_for_confirm_page(sandbox_page, preview, 180), "waker confirm page never served"
        capture(sandbox_page, "sleep-confirm-page")
        wake_button = sandbox_page.locator("#wake-btn")
        assert wake_button.count() > 0, "'Applicatie starten' button missing"
        assert wake_button.is_visible(), "'Applicatie starten' button not visible"

        # Click it: the waker POSTs /__zad/wake, OPI moves the deployment to waking and
        # scales the app back up; the page polls status and reloads when the app is ready.
        wake_button.click()
        sandbox_page.wait_for_timeout(1500)
        capture(sandbox_page, "sleep-loading-page")
        assert sandbox_page.locator("#loading").is_visible(), "loading panel not shown after clicking wake"

        assert _wait_for_app_back(sandbox_page, _WAKE_WAIT_S), "app did not come back / page did not reload"
        capture(sandbox_page, "app-back")
        assert _wait_for_state(forgejo, project.name, deployment, "awake", 120), "did not settle back to awake"
    finally:
        sandbox_api.delete_project_via_api(sandbox_url, project.name, project.api_key, verify_ssl=_API_VERIFY_SSL)


def _wait_for_ingress_host(project_name: str) -> str:
    """Poll the project namespace for the publish-on-web ingress host OPI generated."""
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        out = subprocess.run(
            ["kubectl", "-n", f"rig-{project_name}", "get", "ingress", "-o", "jsonpath={.items[0].spec.rules[0].host}"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if out:
            return out
        time.sleep(5)
    raise AssertionError(f"no ingress host for {project_name}")


def _wait_for_http_ok(url: str, timeout_s: float = 300) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        code = subprocess.run(
            ["curl", "-sk", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "8", url],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if code == "200":
            return True
        time.sleep(5)
    return False


def _wait_for_state(forgejo: ForgejoClient, project: str, deployment: str, want: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _sleep_state(forgejo, project, deployment) == want:
            return True
        time.sleep(10)
    return False


def _wait_for_app_gone(project_name: str, timeout_s: float) -> bool:
    """Wait until the app Deployment (the one without the waker label) has zero ready pods."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        ready = subprocess.run(
            [
                "kubectl",
                "-n",
                f"rig-{project_name}",
                "get",
                "deploy",
                "-l",
                "!zad-role",
                "-o",
                "jsonpath={.items[*].status.readyReplicas}",
            ],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if ready in ("", "0"):
            return True
        time.sleep(5)
    return False


def _wait_for_confirm_page(page: Page, preview: str, timeout_s: float) -> bool:
    """Reload the preview URL until the waker's 'slaapstand' confirm page is served."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            page.goto(preview, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(1000)
            if "slaapstand" in (page.text_content("body") or "").lower():
                return True
        except PlaywrightError:
            pass
        page.wait_for_timeout(4000)
    return False


def _wait_for_app_back(page: Page, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        page.wait_for_timeout(6000)
        text = (page.text_content("body") or "").lower()
        if "slaapstand" not in text and text.strip():
            return True
    return False
