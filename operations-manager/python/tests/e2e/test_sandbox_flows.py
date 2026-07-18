"""
Sandbox lifecycle E2E: create a project via the UI, add a component via the API,
delete the project via the UI - verifying each step against the project file that
lands in the Forgejo `zad-projects` repo.

Requires a running sandbox and E2E_BASE_URL (and E2E_SECRET_KEY matching the
cluster). Run with:

    task test-e2e-sandbox

or:

    E2E_BASE_URL=https://zad.sandbox.rijksapp.dev \
    E2E_SECRET_KEY=sandbox-dev-secret-key-fixed-for-stable-sessions-32min \
    uv run pytest tests/e2e/test_sandbox_flows.py -m "e2e and sandbox" -v --timeout=300
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import pytest
from tests.e2e.conftest import FORGEJO_VERIFY_SSL, SANDBOX_TEST_USER
from tests.e2e.helpers import sandbox_api
from tests.e2e.helpers.project_actions import delete_project_via_ui
from tests.e2e.helpers.wizard import WizardHelper, _unique_project_name

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from playwright.sync_api import BrowserContext, Page
    from tests.e2e.helpers.forgejo import ForgejoClient

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox]

# Sandbox uses a real certificate; API calls verify SSL like the Forgejo client.
_API_VERIFY_SSL = FORGEJO_VERIFY_SSL

# The wizard component gets inbound port 8080 by default. Use an image that actually
# listens on 8080 and runs as non-root so the deployment becomes healthy (nginx:latest
# serves on 80 as root and CrashLoopBackOffs under non-root enforcement, which makes the
# non-force UI delete drag on ArgoCD teardown).
_RUNNABLE_IMAGE = "nginxinc/nginx-unprivileged:stable-alpine"


def test_version_endpoint(sandbox_url: str) -> None:
    """The public /version endpoint reports which build is running (commit/branch/dirty)."""
    with httpx.Client(verify=_API_VERIFY_SSL, timeout=30.0) as client:
        response = client.get(f"{sandbox_url}/version")
    assert response.status_code == 200, response.text
    info = response.json()
    for key in ("name", "version", "commit", "branch", "build_date", "dirty"):
        assert key in info, f"missing '{key}' in /version response: {info}"
    logger.info("Sandbox is running build: %s", info)


@dataclass
class CreatedProject:
    name: str  # technical name, e.g. "e2e97-llv" (derived from display name, random postfix)
    display_name: str  # what was typed in the wizard, e.g. "e2e-97838-jdwm"
    api_key: str
    deployment_name: str


_REVIEW_SUBMIT_SELECTOR = "button:has-text('Project aanmaken'), button:has-text('Indienen')"


def _on_review(page: Page) -> bool:
    """The review page is reached when the final submit button is present."""
    return page.locator(_REVIEW_SUBMIT_SELECTOR).count() > 0


def _walk_create_wizard(page: Page, base_url: str, project_name: str) -> None:
    """Drive the create-project wizard through to the review page.

    Fills the required fields (identity, team, component) as their steps appear,
    then advances with defaults through the remaining steps (services, deployment,
    domains) until the review page is reached. The step count varies with the
    selected services, so we loop until the final submit button appears rather
    than hard-coding the number of steps.
    """
    wizard = WizardHelper(page, base_url)
    wizard.open_create_wizard()

    wizard.fill_identity(display_name=project_name, description=f"E2E lifecycle {project_name}")
    wizard.click_next()  # identity -> services
    wizard.click_next()  # services (none selected) -> team
    wizard.fill_team(email=SANDBOX_TEST_USER["email"])
    wizard.click_next()  # team -> components
    wizard.fill_component(name="web", image=_RUNNABLE_IMAGE)

    # Advance through the remaining default steps (deployment, domains, ...) until review.
    for _ in range(8):
        if _on_review(page):
            break
        wizard.click_next()
    assert _on_review(page), f"Did not reach the review page (stuck at {page.url})"

    body_text = page.text_content("body") or ""
    assert project_name in body_text, f"Project '{project_name}' not visible on review page"
    wizard.submit_wizard()
    page.wait_for_load_state("networkidle")


def _read_api_key_with_retry(page: Page, base_url: str, project_name: str, *, attempts: int = 20) -> str:
    """Read the API key from the details page, retrying while the project loads into memory."""
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            return sandbox_api.read_api_key(page, base_url, project_name)
        except Exception as exc:
            last_error = exc
            time.sleep(3.0)
    raise AssertionError(f"Could not read API key for '{project_name}': {last_error}")


@pytest.fixture(scope="module")
def lifecycle_project(
    sandbox_context: BrowserContext,
    sandbox_url: str,
    forgejo: ForgejoClient,
    artifact_dir: Path,
) -> Generator[CreatedProject]:
    """Create one project via the wizard, shared across the lifecycle tests.

    Yields the project name, its API key, and its first deployment name. Tears
    down with a best-effort API delete in case the UI-delete test did not run.
    """
    display_name = _unique_project_name()
    page = sandbox_context.new_page()
    created: CreatedProject | None = None
    try:
        before = forgejo.list_project_names()
        _walk_create_wizard(page, sandbox_url, display_name)
        page.screenshot(path=str(artifact_dir / f"lifecycle-{display_name}-after-create.png"), full_page=True)

        # The technical name has a random postfix, so discover it by diffing the repo.
        name = forgejo.wait_for_new_project(before, timeout=180)
        assert name, f"No new project file appeared in Forgejo for display-name '{display_name}'"

        deployment_name = forgejo.get_first_deployment_name(name)
        api_key = _read_api_key_with_retry(page, sandbox_url, name)
        created = CreatedProject(name=name, display_name=display_name, api_key=api_key, deployment_name=deployment_name)
        yield created
    finally:
        page.close()
        if created is not None:
            sandbox_api.delete_project_via_api(sandbox_url, created.name, created.api_key, verify_ssl=_API_VERIFY_SSL)


def test_create_project_via_ui(
    lifecycle_project: CreatedProject,
    sandbox_url: str,
    sandbox_page: Page,
    forgejo: ForgejoClient,
    capture,
) -> None:
    """The wizard-created project has a file in Forgejo and shows up in the list."""
    assert forgejo.project_file_exists(lifecycle_project.name)

    sandbox_page.goto(f"{sandbox_url}/projects")
    sandbox_page.wait_for_load_state("networkidle")
    capture(sandbox_page, "projects-list")
    body_text = sandbox_page.text_content("body") or ""
    assert lifecycle_project.name in body_text or lifecycle_project.display_name in body_text, (
        f"Project '{lifecycle_project.name}' (display '{lifecycle_project.display_name}') not found in projects list"
    )


def test_add_component_via_api(
    lifecycle_project: CreatedProject,
    sandbox_url: str,
    forgejo: ForgejoClient,
) -> None:
    """Adding a component via the v2 API lands it in the project file."""
    component_name = "apiworker"  # component names allow only lowercase letters and digits
    sandbox_api.add_component(
        sandbox_url,
        lifecycle_project.name,
        lifecycle_project.api_key,
        component_name=component_name,
        image=_RUNNABLE_IMAGE,
        deployment_names=[lifecycle_project.deployment_name],
        verify_ssl=_API_VERIFY_SSL,
    )
    assert forgejo.wait_for_component(lifecycle_project.name, component_name, timeout=120), (
        f"Component '{component_name}' did not appear in the Forgejo project file"
    )


def test_delete_project_via_ui(
    lifecycle_project: CreatedProject,
    sandbox_url: str,
    sandbox_page: Page,
    forgejo: ForgejoClient,
    capture,
) -> None:
    """Deleting the project through the danger-zone modal removes its Forgejo file."""
    delete_project_via_ui(sandbox_page, sandbox_url, lifecycle_project.name)
    capture(sandbox_page, "delete-started")

    # The authoritative signal is the project file disappearing from Forgejo. The
    # server-side delete of a still-deploying project can take minutes.
    assert forgejo.wait_for_project_gone(lifecycle_project.name, timeout=300), (
        f"Project file for '{lifecycle_project.name}' still present in Forgejo after delete"
    )

    sandbox_page.goto(f"{sandbox_url}/projects")
    sandbox_page.wait_for_load_state("networkidle")
    capture(sandbox_page, "after-delete")
    body_text = sandbox_page.text_content("body") or ""
    assert lifecycle_project.name not in body_text, f"Project '{lifecycle_project.name}' still listed after delete"
