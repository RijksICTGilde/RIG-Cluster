"""Live sandbox E2E for the env-vars and aliases fields after RC-25 made them services.

RC-25 moved these two fields out of hand-authored form code and into the catalog: the
component form's "Variabelen" fieldset was replaced by two service-owned fieldsets, and
the deployment-component env-vars fieldset now comes from a registry hook
(``config_deployment_component_layout``) that did not exist before. Unit tests prove the
objects are wired together; only a browser proves the fields still render, save, and end
up encrypted in the project file.

``test_sandbox_reallife.py`` already covers component-level env-vars, so what is new here
is the coverage that was missing: **aliases** (never covered E2E) and the
**deployment-component** env-vars override (the layer whose hook is new).

Every action is a real click or fill; the assertion of record is the project YAML in
Forgejo, not the page. Skips when E2E_BASE_URL is unset.
"""

from __future__ import annotations

import contextlib
import os
import time
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import Error as PlaywrightError
from tests.e2e.helpers import sandbox_api
from tests.e2e.helpers.edit_modal import EditModalHelper
from tests.e2e.helpers.lifecycle import RUNNABLE_IMAGE, read_api_key_with_retry
from tests.e2e.helpers.wizard import WizardHelper

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page
    from tests.e2e.helpers.forgejo import ForgejoClient

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox]

_API_VERIFY_SSL = os.environ.get("E2E_API_VERIFY_SSL", "false").lower() in ("1", "true", "yes")
_USER_EMAIL = os.environ.get("E2E_SANDBOX_USER", "admin@sandbox.rijksapp.dev")
_AGE_ARMOR = "-----BEGIN AGE ENCRYPTED FILE-----"


def _walk_create_wizard(page: Page, sandbox_url: str, forgejo: ForgejoClient) -> tuple[str, str]:
    """Create a minimal one-component project; return (project_name, api_key).

    Also asserts the two system-service fieldsets are present on the component step --
    they are contributed by the registry now, and a system service that failed to
    contribute would simply render nothing, which is the silent failure worth catching.
    """
    wizard = WizardHelper(page, sandbox_url)
    before = forgejo.list_project_names()
    wizard.open_create_wizard()
    wizard.fill_identity(display_name="envali", description="env-vars/aliases UI e2e")
    wizard.click_next()
    page.locator("[data-service='publish-on-web']").first.click()
    wizard.click_next()

    saw_fields = False
    for _ in range(16):
        page.wait_for_load_state("networkidle")
        if page.locator("button:has-text('Project aanmaken'), button:has-text('Indienen')").count() > 0:
            break
        email = page.locator("[name='users[0]/email']")
        if email.count() > 0 and (email.first.input_value() or "") == "":
            wizard.fill_team(email=_USER_EMAIL)
        if page.locator("[name='components[0]/name']").count() > 0:
            wizard.fill_component(name="web", image=RUNNABLE_IMAGE)
            # Both system services must have contributed their field to this step.
            assert page.locator("[name='components[0]/aliases']").count() > 0, (
                "the aliases field is not on the component step -- the aliases system "
                "service did not contribute its fieldset"
            )
            assert page.locator("[name='components[0]/user-env-vars']").count() > 0, (
                "the user-env-vars field is not on the component step"
            )
            saw_fields = True
        wizard.click_next()

    assert saw_fields, "never reached the component step"
    wizard.submit_wizard()
    name = forgejo.wait_for_new_project(before, timeout=240)
    assert name, "no project appeared in Forgejo"
    return name, read_api_key_with_retry(page, sandbox_url, name)


@pytest.fixture(scope="module")
def envali_project(sandbox_context: BrowserContext, sandbox_url: str, forgejo: ForgejoClient):
    """One project created through the real wizard, retried like the sibling suites."""
    name: str | None = None
    api_key: str | None = None
    last_error: Exception | None = None
    for _ in range(3):
        page = sandbox_context.new_page()
        try:
            name, api_key = _walk_create_wizard(page, sandbox_url, forgejo)
            break
        except PlaywrightError as error:
            last_error = error
        finally:
            page.close()
    if not name or not api_key:
        pytest.fail(f"create wizard did not complete after retries: {last_error}")
    try:
        yield name
    finally:
        with contextlib.suppress(Exception):
            sandbox_api.delete_project_via_api(sandbox_url, name, api_key, verify_ssl=_API_VERIFY_SSL)


def _open_components_modal(page: Page, sandbox_url: str, project_name: str) -> EditModalHelper:
    """Open the 'Components beheren' modal, the way reallife does."""
    modal = EditModalHelper(page, sandbox_url, project_name)
    modal.open_detail_page()
    modal.open_edit_modal("modal-edit-components", "Components beheren")
    return modal


def _component(forgejo: ForgejoClient, project_name: str, component_name: str) -> dict:
    data = forgejo.get_project_yaml(project_name) or {}
    for component in data.get("components") or []:
        if component.get("name") == component_name:
            return component
    return {}


def _wait_for(check, timeout_s: float = 120):
    """Poll until ``check()`` is truthy (a save commits and reprocesses)."""
    deadline = time.monotonic() + timeout_s
    value = check()
    while not value and time.monotonic() < deadline:
        time.sleep(4)
        value = check()
    return value


def test_component_aliases_save_through_the_ui(
    envali_project: str, sandbox_url: str, sandbox_page: Page, forgejo: ForgejoClient, capture
) -> None:
    """Aliases: the field RC-25 moved into its own service, never covered E2E before."""
    modal = _open_components_modal(sandbox_page, sandbox_url, envali_project)
    modal.fill_codemirror_kv("components[0]/aliases", "POSTGRES_HOST=$DATABASE_SERVER_HOST")
    capture(sandbox_page, "aliases-filled")
    modal.submit_step_expect_progress()

    stored = _wait_for(lambda: (_component(forgejo, envali_project, "web") or {}).get("aliases"))
    assert stored, "the alias never reached the project file"
    assert "POSTGRES_HOST" in stored, f"unexpected alias map: {stored}"


def test_component_env_vars_save_encrypted_through_the_ui(
    envali_project: str, sandbox_url: str, sandbox_page: Page, forgejo: ForgejoClient, capture
) -> None:
    """Env-vars on the component: the field must still save AND be AGE-encrypted."""
    modal = _open_components_modal(sandbox_page, sandbox_url, envali_project)
    modal.fill_codemirror_kv("components[0]/user-env-vars", "E2E_TOKEN=rc25-secret-value")
    capture(sandbox_page, "component-env-vars-filled")
    modal.submit_step_expect_progress()

    stored = _wait_for(lambda: (_component(forgejo, envali_project, "web") or {}).get("user-env-vars"))
    assert stored, "the env var never reached the project file"
    assert _AGE_ARMOR in str(stored), f"user-env-vars is not AGE-encrypted: {str(stored)[:60]}"
    assert "rc25-secret-value" not in str(stored), "the plaintext value is in the project file"


def test_deployment_component_env_vars_override_saves(
    envali_project: str, sandbox_url: str, sandbox_page: Page, forgejo: ForgejoClient, capture
) -> None:
    """The deployment-component override: the layer whose form hook is new in RC-25.

    Before RC-25 this fieldset was hand-authored in ``forms/editables/fields/deployments.py``;
    it now comes from ``config_deployment_component_layout()`` on the user-env-vars service,
    collected by ``_service_deployment_component_layouts()``. Nothing but a browser proves
    that swap kept the field on screen and saving.
    """
    modal = EditModalHelper(sandbox_page, sandbox_url, envali_project)
    modal.open_detail_page()
    modal.open_edit_modal("modal-edit-deployment-0", "Deployment bewerken")

    field = sandbox_page.locator("textarea[name*='components'][name*='user-env-vars']").first
    assert field.count() > 0, (
        "no deployment-component user-env-vars field in the deployment modal -- the "
        "service did not contribute its layout to this layer"
    )
    field_name = field.get_attribute("name") or ""
    modal.fill_codemirror_kv(field_name, "DEPLOY_ONLY=rc25-deployment-value")
    capture(sandbox_page, "deployment-component-env-vars-filled")
    modal.submit_step_expect_progress()

    def _stored():
        data = forgejo.get_project_yaml(envali_project) or {}
        for deployment in data.get("deployments") or []:
            for component in deployment.get("components") or []:
                if component.get("user-env-vars"):
                    return component["user-env-vars"]
        return None

    stored = _wait_for(_stored)
    assert stored, "the deployment-level env var never reached the project file"
    assert _AGE_ARMOR in str(stored), f"deployment user-env-vars is not AGE-encrypted: {str(stored)[:60]}"

    # The two layers must stay SEPARATE in the file. They are merged at deploy time
    # (deployment-component wins per key, ProjectManager), and that merge only means
    # anything while both values are still stored in their own place. One service owning
    # both layers is exactly the arrangement that could accidentally write them to one
    # spot, so assert the component-level value written by the previous test survived.
    component_level = (_component(forgejo, envali_project, "web") or {}).get("user-env-vars")
    assert component_level, (
        "writing the deployment-component override wiped the component-level user-env-vars; "
        "the two layers must be stored separately for the merge to have anything to merge"
    )
    assert str(component_level) != str(stored), (
        "component and deployment-component user-env-vars hold identical ciphertext, "
        "which suggests one write landed on both layers"
    )
