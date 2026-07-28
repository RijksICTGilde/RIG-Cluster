"""Live sandbox E2E for the sleep-mode config UI -- purely user-based (clicks + fills).

Covers the three flows a user reaches the sleep-mode config through:
1. the create wizard: the config step sits AFTER the components step, so the
   waker-component select is populated from the components just entered;
2. the service card's 'Configureer' button, opening the service's own config modal;
3. the 'Services & Integraties' > 'Bewerken' modal, which chains to the config step.

Every action is a real button press or field fill (helpers in ``service_config``); no
``page.evaluate`` shortcuts and no direct modal-fragment URLs. Skips when E2E_BASE_URL is
unset. This is the pattern other config-owning services should copy.
"""

from __future__ import annotations

import contextlib
import os
from typing import TYPE_CHECKING

import pytest
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


@pytest.fixture(scope="module")
def sleep_project(sandbox_context: BrowserContext, sandbox_url: str, forgejo: ForgejoClient):
    """Create one sleep-mode project through the real wizard and yield its name.

    Doubles as the create-wizard coverage: it walks the wizard by clicking and filling,
    and asserts that the sleep-mode step appears AFTER the components step with its
    waker-component select populated from the 'web' component just entered.
    """
    page = sandbox_context.new_page()
    name: str | None = None
    api_key: str | None = None
    try:
        wizard = WizardHelper(page, sandbox_url)
        before = forgejo.list_project_names()
        wizard.open_create_wizard()
        wizard.fill_identity(display_name="sleepui", description="sleep-mode UI e2e")
        wizard.click_next()
        _select_service(page, "publish-on-web")
        _select_service(page, "sleep-mode")
        wizard.click_next()

        saw_sleep_step = False
        for _ in range(16):
            page.wait_for_load_state("networkidle")
            if page.locator("button:has-text('Project aanmaken'), button:has-text('Indienen')").count() > 0:
                break
            email = page.locator("[name='users[0]/email']")
            if email.count() > 0 and (email.first.input_value() or "") == "":
                wizard.fill_team(email=_USER_EMAIL)
            if page.locator("[name='components[0]/name']").count() > 0:
                wizard.fill_component(name="web", image=RUNNABLE_IMAGE)
            waker = page.locator("select[name*='waker-component']")
            if waker.count() > 0:
                saw_sleep_step = True
                values = waker.first.locator("option").evaluate_all("opts => opts.map(o => o.value)")
                assert "web" in values, f"waker-component select not populated with the component: {values}"
                page.fill("input[name*='match'], textarea[name*='match']", "pr-*")
            wizard.click_next()

        assert saw_sleep_step, "the sleep-mode config step never appeared in the create wizard"
        wizard.submit_wizard()
        page.wait_for_load_state("networkidle")
        name = forgejo.wait_for_new_project(before, timeout=240)
        assert name, "no project appeared in Forgejo"
        api_key = read_api_key_with_retry(page, sandbox_url, name)
        yield name
    finally:
        page.close()
        if name and api_key:
            with contextlib.suppress(Exception):
                sandbox_api.delete_project_via_api(sandbox_url, name, api_key, verify_ssl=_API_VERIFY_SSL)


def _sleep_config(forgejo: ForgejoClient, project_name: str) -> dict:
    data = forgejo.get_project_yaml(project_name) or {}
    for entry in data.get("services", []):
        if isinstance(entry, dict) and entry.get("name") == "sleep-mode":
            return entry.get("config") or {}
    return {}


def test_wizard_wrote_sleep_config(sleep_project: str, forgejo: ForgejoClient) -> None:
    # The wizard walk (fixture) reached the after-components step and filled it; the file
    # must now carry an enabled sleep-mode config with the comma-match we typed.
    config = _sleep_config(forgejo, sleep_project)
    assert config.get("enabled") is True
    assert config.get("match") == ["pr-*"]
    assert config.get("wake-mode") in ("auto", "confirm", "manual")


def test_configure_button_opens_config_modal(sleep_project: str, sandbox_url: str, sandbox_page: Page, capture) -> None:
    # (B) the per-service 'Configureer' button opens the sleep-mode config modal.
    service_config.open_detail(sandbox_page, sandbox_url, sleep_project)
    service_config.open_service_config_modal(sandbox_page, "Slaapstand")
    capture(sandbox_page, "sleep-configure-modal")
    assert "Slaapstand" in service_config.modal_heading(sandbox_page)
    waker = service_config.modal_field(sandbox_page, "waker-component")
    assert waker.count() > 0, "waker-component select not in the config modal"
    values = waker.first.locator("option").evaluate_all("opts => opts.map(o => o.value)")
    assert "web" in values, f"waker-component not populated in the modal: {values}"


def test_services_modal_reaches_sleep_config(sleep_project: str, sandbox_url: str, sandbox_page: Page, capture) -> None:
    # (A) adding/having sleep-mode in the 'Services beheren' modal chains to its config step.
    service_config.open_detail(sandbox_page, sandbox_url, sleep_project)
    service_config.open_services_modal(sandbox_page)
    assert "Services beheren" in service_config.modal_heading(sandbox_page)
    reached = service_config.modal_advance_to_field(sandbox_page, "waker-component")
    capture(sandbox_page, "services-modal-sleep-step")
    assert reached, "the services modal did not chain to the sleep-mode config step"
    assert "Slaapstand" in service_config.modal_heading(sandbox_page)
