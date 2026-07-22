"""
Shared project-lifecycle helpers for sandbox E2E tests.

Creating a project through the real wizard (and discovering its technical name,
API key and first deployment) is needed by both the lifecycle suite
(test_sandbox_flows.py) and the real-life suite (test_sandbox_reallife.py), so
the logic lives here instead of in one test module.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tests.e2e.helpers import sandbox_api
from tests.e2e.helpers.wizard import WizardHelper

if TYPE_CHECKING:
    from playwright.sync_api import Page
    from tests.e2e.helpers.forgejo import ForgejoClient

# The wizard component gets inbound port 8080 by default. Use an image that actually
# listens on 8080 and runs as non-root so the deployment becomes healthy (nginx:latest
# serves on 80 as root and CrashLoopBackOffs under non-root enforcement, which makes the
# non-force UI delete drag on ArgoCD teardown).
RUNNABLE_IMAGE = "nginxinc/nginx-unprivileged:stable-alpine"

REVIEW_SUBMIT_SELECTOR = "button:has-text('Project aanmaken'), button:has-text('Indienen')"


@dataclass
class CreatedProject:
    name: str  # technical name, e.g. "e2e97-llv" (derived from display name, random postfix)
    display_name: str  # what was typed in the wizard, e.g. "e2e-97838-jdwm"
    api_key: str
    deployment_name: str


def on_review(page: Page) -> bool:
    """The review page is reached when the final submit button is present."""
    return page.locator(REVIEW_SUBMIT_SELECTOR).count() > 0


def walk_create_wizard(
    page: Page,
    base_url: str,
    project_name: str,
    *,
    user_email: str,
    component_name: str = "web",
    image: str = RUNNABLE_IMAGE,
) -> None:
    """Drive the create-project wizard through to the review page and submit it.

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
    wizard.fill_team(email=user_email)
    wizard.click_next()  # team -> components
    wizard.fill_component(name=component_name, image=image)

    # Advance through the remaining default steps (deployment, domains, ...) until review.
    for _ in range(8):
        if on_review(page):
            break
        wizard.click_next()
    assert on_review(page), f"Did not reach the review page (stuck at {page.url})"

    body_text = page.text_content("body") or ""
    assert project_name in body_text, f"Project '{project_name}' not visible on review page"
    wizard.submit_wizard()
    page.wait_for_load_state("networkidle")


def read_api_key_with_retry(page: Page, base_url: str, project_name: str, *, attempts: int = 20) -> str:
    """Read the API key from the details page, retrying while the project loads into memory."""
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            return sandbox_api.read_api_key(page, base_url, project_name)
        except Exception as exc:
            last_error = exc
            time.sleep(3.0)
    raise AssertionError(f"Could not read API key for '{project_name}': {last_error}")


def create_project_via_wizard(
    page: Page,
    base_url: str,
    forgejo: ForgejoClient,
    display_name: str,
    *,
    user_email: str,
    component_name: str = "web",
    image: str = RUNNABLE_IMAGE,
    create_timeout: float = 180.0,
) -> CreatedProject:
    """Create one project through the real wizard and resolve its identity.

    The technical name has a random postfix, so it is discovered by diffing the
    Forgejo repo listing before/after. Returns the project with its API key and
    first deployment name.
    """
    before = forgejo.list_project_names()
    walk_create_wizard(page, base_url, display_name, user_email=user_email, component_name=component_name, image=image)

    name = forgejo.wait_for_new_project(before, timeout=create_timeout)
    assert name, f"No new project file appeared in Forgejo for display-name '{display_name}'"

    deployment_name = forgejo.get_first_deployment_name(name)
    api_key = read_api_key_with_retry(page, base_url, name)
    return CreatedProject(name=name, display_name=display_name, api_key=api_key, deployment_name=deployment_name)
