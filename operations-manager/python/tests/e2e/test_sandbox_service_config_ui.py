"""Live sandbox E2E for the redis and minio-storage config UI -- purely user-based.

Both services carried a real project-level setting (``acl-key-prefix``,
``enable-versioning``) that had a model and an API route but no field anywhere, so the
only way to change it was to hand-edit the project file (RC-25). These tests exercise the
three flows a user reaches a project-level config section through:

1. the create wizard, where the section appears once the service is selected;
2. the service card's 'Configureer' button, opening the service's own config modal;
3. the 'Services & Integraties' > 'Bewerken' modal, which chains to the config step.

Every action is a real button press or field fill (helpers in ``service_config``); no
``page.evaluate`` shortcuts and no direct modal-fragment URLs. The assertion of record is
the project YAML in Forgejo, not the page. Skips when E2E_BASE_URL is unset.
"""

from __future__ import annotations

import contextlib
import os
import time
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import Error as PlaywrightError
from tests.e2e.helpers import sandbox_api, service_config
from tests.e2e.helpers.lifecycle import RUNNABLE_IMAGE, read_api_key_with_retry
from tests.e2e.helpers.wizard import WizardHelper

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page
    from tests.e2e.helpers.forgejo import ForgejoClient

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox]

_API_VERIFY_SSL = os.environ.get("E2E_API_VERIFY_SSL", "false").lower() in ("1", "true", "yes")
_USER_EMAIL = os.environ.get("E2E_SANDBOX_USER", "admin@sandbox.rijksapp.dev")


def _select_service(page: Page, name: str) -> None:
    checkbox = page.locator(f"input[name='services[]'][value='{name}']").first
    assert checkbox.count() > 0, f"service card '{name}' not on the services step"
    if not checkbox.is_checked():
        page.locator(f"[data-service='{name}']").first.click()


def _walk_create_wizard(page: Page, sandbox_url: str, forgejo: ForgejoClient) -> tuple[str, str]:
    """Create a project with redis + minio-storage; return (project_name, api_key).

    Asserts both config steps show up in the wizard once their service is ticked -- that
    is the gap this closes, so it is checked here and not only in the modals.
    """
    wizard = WizardHelper(page, sandbox_url)
    before = forgejo.list_project_names()
    wizard.open_create_wizard()
    wizard.fill_identity(display_name="svccfgui", description="service config UI e2e")
    wizard.click_next()
    _select_service(page, "publish-on-web")
    _select_service(page, "redis")
    _select_service(page, "minio-storage")
    wizard.click_next()

    saw_redis_step = False
    saw_minio_step = False
    for _ in range(18):
        page.wait_for_load_state("networkidle")
        if page.locator("button:has-text('Project aanmaken'), button:has-text('Indienen')").count() > 0:
            break
        email = page.locator("[name='users[0]/email']")
        if email.count() > 0 and (email.first.input_value() or "") == "":
            wizard.fill_team(email=_USER_EMAIL)
        if page.locator("[name='components[0]/name']").count() > 0:
            wizard.fill_component(name="web", image=RUNNABLE_IMAGE)
        acl = page.locator("[name*='acl-key-prefix']")
        if acl.count() > 0:
            saw_redis_step = True
            # Default is on; untick it, so the file has to carry an explicit false.
            if acl.first.is_checked():
                acl.first.uncheck()
        versioning = page.locator("[name*='enable-versioning']")
        if versioning.count() > 0:
            saw_minio_step = True
            if not versioning.first.is_checked():
                versioning.first.check()
        wizard.click_next()

    assert saw_redis_step, "the redis config step never appeared in the create wizard"
    assert saw_minio_step, "the minio-storage config step never appeared in the create wizard"
    wizard.submit_wizard()
    page.wait_for_load_state("networkidle")
    name = forgejo.wait_for_new_project(before, timeout=240)
    assert name, "no project appeared in Forgejo"
    return name, read_api_key_with_retry(page, sandbox_url, name)


@pytest.fixture(scope="module")
def config_project(sandbox_context: BrowserContext, sandbox_url: str, forgejo: ForgejoClient):
    """One project with redis + minio-storage, created through the real wizard.

    Retried like the sleep-mode walk: the shared WizardHelper's per-step HTMX wait
    occasionally times out on a loaded sandbox while the server-side step succeeds. The
    walk only creates a project on its final submit, so a pre-submit timeout leaves
    nothing behind.
    """
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


def _service_config(forgejo: ForgejoClient, project_name: str, service: str) -> dict:
    data = forgejo.get_project_yaml(project_name) or {}
    for entry in data.get("services", []):
        if isinstance(entry, dict) and entry.get("name") == service:
            return entry.get("config") or {}
    return {}


def _wait_for_config(forgejo: ForgejoClient, project_name: str, service: str, key: str, wanted, timeout_s: float = 90):
    """Poll the project file until ``key`` holds ``wanted`` (a save commits + reprocesses)."""
    deadline = time.monotonic() + timeout_s
    value = _service_config(forgejo, project_name, service).get(key)
    while value != wanted and time.monotonic() < deadline:
        time.sleep(4)
        value = _service_config(forgejo, project_name, service).get(key)
    return value


def test_wizard_wrote_both_configs(config_project: str, forgejo: ForgejoClient) -> None:
    # Both settings were unreachable before RC-25; the wizard walk set them, so the file
    # must now carry exactly what was ticked (and unticked).
    assert _service_config(forgejo, config_project, "redis").get("acl-key-prefix") is False
    assert _service_config(forgejo, config_project, "minio-storage").get("enable-versioning") is True


def test_configure_button_opens_redis_config_modal(
    config_project: str, sandbox_url: str, sandbox_page: Page, capture
) -> None:
    service_config.open_detail(sandbox_page, sandbox_url, config_project)
    service_config.open_service_config_modal(sandbox_page, "Redis")
    capture(sandbox_page, "redis-configure-modal")
    assert "Redis" in service_config.modal_heading(sandbox_page)
    assert service_config.modal_field(sandbox_page, "acl-key-prefix").count() > 0, (
        "acl-key-prefix not in the redis config modal"
    )


def test_configure_button_opens_minio_config_modal(
    config_project: str, sandbox_url: str, sandbox_page: Page, capture
) -> None:
    service_config.open_detail(sandbox_page, sandbox_url, config_project)
    service_config.open_service_config_modal(sandbox_page, "MinIO")
    capture(sandbox_page, "minio-configure-modal")
    assert service_config.modal_field(sandbox_page, "enable-versioning").count() > 0, (
        "enable-versioning not in the minio-storage config modal"
    )


def test_redis_config_modal_saves_to_the_project_file(
    config_project: str, sandbox_url: str, sandbox_page: Page, forgejo: ForgejoClient, capture
) -> None:
    # A modal that opens but does not persist is the failure mode worth catching, so the
    # assertion is the YAML in Forgejo and not the page.
    service_config.open_detail(sandbox_page, sandbox_url, config_project)
    service_config.open_service_config_modal(sandbox_page, "Redis")
    checkbox = service_config.modal_field(sandbox_page, "acl-key-prefix").first
    checkbox.check()
    capture(sandbox_page, "redis-config-modal-filled")
    service_config.modal_submit(sandbox_page)
    assert _wait_for_config(forgejo, config_project, "redis", "acl-key-prefix", True) is True, (
        "saving the redis config modal did not write acl-key-prefix back to the project file"
    )


def test_services_modal_reaches_both_config_steps(
    config_project: str, sandbox_url: str, sandbox_page: Page, capture
) -> None:
    service_config.open_detail(sandbox_page, sandbox_url, config_project)
    service_config.open_services_modal(sandbox_page)
    assert "Services beheren" in service_config.modal_heading(sandbox_page)
    assert service_config.modal_advance_to_field(sandbox_page, "acl-key-prefix"), (
        "the services modal did not chain to the redis config step"
    )
    capture(sandbox_page, "services-modal-redis-step")
    assert service_config.modal_advance_to_field(sandbox_page, "enable-versioning"), (
        "the services modal did not chain to the minio-storage config step"
    )
    capture(sandbox_page, "services-modal-minio-step")
