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


# The services a user can select in the CREATE wizard (hidden types -
# namespace-postgresql-database, namespace-redis, platform - are added via the
# edit flow instead). authorization-wall auto-pulls publish-on-web + keycloak.
ALL_CREATE_WIZARD_SERVICES = [
    "publish-on-web",
    "keycloak",
    "authorization-wall",
    "metrics-scraper",
    "persistent-storage",
    "temp-storage",
    "postgresql-database",
    "minio-storage",
    "redis",
    "attachments",
]


def _check_service_cards(page: Page, services: list[str]) -> None:
    """Tick each service card on the SERVICES step, idempotently.

    Dependency cards (publish-on-web/keycloak pulled in by authorization-wall)
    render locked+checked; those are left as-is. A card is only clicked when its
    checkbox is not already checked, so a locked-checked card is never toggled off.
    """
    for service in services:
        checkbox = page.locator(f"input[name='services[]'][value='{service}']").first
        assert checkbox.count() > 0, f"Service card '{service}' not found on the services step"
        try:
            already = checkbox.is_checked()
        except Exception:
            already = False
        if not already:
            page.locator(f"[data-service='{service}']").first.click()


def _fill_service_config_step(page: Page, *, banner: str) -> None:
    """Fill required/desired fields on whichever service-config step is showing.

    Detects the step by the presence of its fields, so it is order-independent:
    - keycloak config: enable restrict-access (auth-wall submit requires it) and
      leave the template at its default (sso-support).
    - authorization-wall config: set the banner text.
    """
    restrict = page.locator("[name='services/keycloak/config/restrict-access/enabled']")
    if restrict.count() > 0:
        try:
            if not restrict.first.is_checked():
                # ROOS checkbox: click the card/label rather than the hidden input.
                page.locator(
                    "label:has([name='services/keycloak/config/restrict-access/enabled']), "
                    "[name='services/keycloak/config/restrict-access/enabled']"
                ).first.click()
        except Exception:
            restrict.first.click()

    banner_field = page.locator("[name='services/authorization-wall/config/banner']")
    if banner_field.count() > 0:
        banner_field.first.fill(banner)


def _fill_component_identity(page: Page, component_name: str, image: str) -> None:
    """Fill component 0's required name + image, surviving the step's late re-render.

    The components step finishes initializing a moment AFTER htmx:afterSettle (it is
    a large services-heavy form). Filling too early gets clobbered by that init
    render, which also leaves the "Volgende" button disabled. So we wait for the
    network to settle, then fill and re-fill until both values stick, targeting the
    exact fields (the fuzzy `[name*='name']` selector also matches storage sub-fields).
    """
    name_field = page.locator("[name='components[0]/name']").first
    image_field = page.locator("[name='components[0]/image']").first
    next_button = page.locator(".wizard-step__actions button[type='submit']").first
    name_field.wait_for(state="visible", timeout=10000)
    page.wait_for_load_state("networkidle")
    # Loop until BOTH the values stick AND the Next button is enabled. The button is
    # disabled until client-side validation accepts the required fields; clicking it
    # early leaves the wizard on the components step.
    for _ in range(10):
        page.wait_for_timeout(500)
        name_field.fill(component_name)
        image_field.fill(image)
        page.wait_for_timeout(300)  # let client validation react and toggle the button
        values_ok = (name_field.input_value() or "") == component_name and (image_field.input_value() or "") == image
        if values_ok and not next_button.is_disabled():
            return
    raise AssertionError("component name/image did not settle (values stuck / Next enabled) on the components step")


def walk_create_wizard_with_services(
    page: Page,
    base_url: str,
    project_name: str,
    *,
    user_email: str,
    services: list[str],
    component_name: str = "web",
    image: str = RUNNABLE_IMAGE,
    banner: str = "E2E all-services access banner",
) -> None:
    """Drive the create wizard with a set of services selected, through to submit.

    Extends the plain walk with service selection plus the conditional service
    config steps (keycloak, auth-wall) and the attachments step. Fills each step's
    known fields as it appears and advances until the review page, so it tolerates
    the variable step count that the selected services produce. Component-level
    service config (storage name/mount-path) keeps its pre-filled defaults.
    """
    wizard = WizardHelper(page, base_url)
    wizard.open_create_wizard()

    wizard.fill_identity(display_name=project_name, description=f"E2E all-services {project_name}")
    wizard.click_next()  # identity -> services

    _check_service_cards(page, services)
    wizard.click_next()  # services -> first config/team step

    for _ in range(12):
        if on_review(page):
            break
        _fill_service_config_step(page, banner=banner)
        # Fill the team email if this is the team step and it is still empty.
        email_field = page.locator("[name='users[0]/email']")
        if email_field.count() > 0 and (email_field.first.input_value() or "") == "":
            wizard.fill_team(email=user_email)
        # The components step is a large HTMX-swapped form; its required name/image
        # fields must be filled (and verified) before advancing, else the step
        # re-renders with a "field required" error and never progresses.
        if page.locator("[name='components[0]/name']").count() > 0:
            _fill_component_identity(page, component_name, image)
        wizard.click_next()

    assert on_review(page), f"Did not reach the review page (stuck at {page.url})"
    body_text = page.text_content("body") or ""
    assert project_name in body_text, f"Project '{project_name}' not visible on review page"
    wizard.submit_wizard()
    page.wait_for_load_state("networkidle")


def create_project_with_services(
    page: Page,
    base_url: str,
    forgejo: ForgejoClient,
    display_name: str,
    *,
    user_email: str,
    services: list[str],
    component_name: str = "web",
    image: str = RUNNABLE_IMAGE,
    create_timeout: float = 240.0,
) -> CreatedProject:
    """Create a project through the wizard with `services` selected; resolve its identity."""
    before = forgejo.list_project_names()
    walk_create_wizard_with_services(
        page,
        base_url,
        display_name,
        user_email=user_email,
        services=services,
        component_name=component_name,
        image=image,
    )
    name = forgejo.wait_for_new_project(before, timeout=create_timeout)
    assert name, f"No new project file appeared in Forgejo for display-name '{display_name}'"
    deployment_name = forgejo.get_first_deployment_name(name)
    api_key = read_api_key_with_retry(page, base_url, name)
    return CreatedProject(name=name, display_name=display_name, api_key=api_key, deployment_name=deployment_name)


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
