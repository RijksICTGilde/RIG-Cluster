"""Live sandbox E2E for the invite config UI -- purely user-based (clicks + fills).

This is the test that reversed RC-13's approve to rework: it drives the real portal
and verifies the resulting project file in Forgejo, so it catches the silent
sequence-drop that the unit tests (which exercise the model/provider in isolation)
could not.

Covers:
1. the create wizard: select keycloak + invite, reach the invite config step (it sits
   AFTER the keycloak step because the realm-role picker reads the keycloak config),
   add an invite item, fill its key + contact, submit -- then assert the committed file
   carries ``services/invite/config/active`` with that key (the drop wrote ``active: []``);
2. the service card's 'Configureer' button opening the invite config modal, adding a
   second invite, and the file gaining it;
3. keycloak's own 'Configureer' modal adding an ``additional-clients`` entry -- the SAME
   shared sequence-merge path, untouched by this branch, verified so the fix is proven
   platform-wide;
4. the detail-page invite block is shown to an admin and lists the invite link.

Every action is a real button press or field fill; no ``page.evaluate`` shortcuts and no
direct modal-fragment URLs. Skips when E2E_BASE_URL is unset.
"""

from __future__ import annotations

import contextlib
import os
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

_WIZARD_KEY = "probe-invite-wizard"
_MODAL_KEY = "probe-invite-modal"
_CONTACT = "invite-contact@sandbox.rijksapp.dev"


def _select_service(page: Page, name: str) -> None:
    checkbox = page.locator(f"input[name='services[]'][value='{name}']").first
    assert checkbox.count() > 0, f"service card '{name}' not on the services step"
    if not checkbox.is_checked():
        page.locator(f"[data-service='{name}']").first.click()


def _invite_config(forgejo: ForgejoClient, project_name: str) -> dict:
    data = forgejo.get_project_yaml(project_name) or {}
    for entry in data.get("services", []):
        if isinstance(entry, dict) and entry.get("name") == "invite":
            return entry.get("config") or {}
    return {}


def _invite_keys(forgejo: ForgejoClient, project_name: str) -> list[str]:
    active = _invite_config(forgejo, project_name).get("active") or []
    return [item.get("key") for item in active if isinstance(item, dict)]


def _keycloak_client_names(forgejo: ForgejoClient, project_name: str) -> list[str]:
    data = forgejo.get_project_yaml(project_name) or {}
    for entry in data.get("services", []):
        if isinstance(entry, dict) and entry.get("name") == "keycloak":
            clients = (entry.get("config") or {}).get("additional-clients") or []
            return [c.get("name") for c in clients if isinstance(c, dict)]
    return []


def _add_wizard_invite(page: Page) -> None:
    """On the invite config step: add one item and fill its key + contact, then continue."""
    # Add a row (create-wizard context: the button triggers an HTMX form re-render).
    page.locator("button:has-text('Item toevoegen'), a:has-text('Item toevoegen')").last.click()
    page.wait_for_load_state("networkidle")
    page.locator("[name$='active[0]/key']").first.fill(_WIZARD_KEY)
    page.locator("[name$='active[0]/contact-email']").first.fill(_CONTACT)


def _walk_create_wizard(page: Page, sandbox_url: str, forgejo: ForgejoClient) -> tuple[str, str]:
    """Drive the create wizard, adding one invite on its config step; return (name, api_key)."""
    wizard = WizardHelper(page, sandbox_url)
    before = forgejo.list_project_names()
    wizard.open_create_wizard()
    wizard.fill_identity(display_name="inviteui", description="invite UI e2e")
    wizard.click_next()
    # invite requires keycloak; select both explicitly so the step order is deterministic.
    _select_service(page, "keycloak")
    _select_service(page, "invite")
    wizard.click_next()

    saw_invite_step = False
    for _ in range(20):
        page.wait_for_load_state("networkidle")
        if page.locator("button:has-text('Project aanmaken'), button:has-text('Indienen')").count() > 0:
            break
        email = page.locator("[name='users[0]/email']")
        if email.count() > 0 and (email.first.input_value() or "") == "":
            wizard.fill_team(email=_USER_EMAIL)
        if page.locator("[name='components[0]/name']").count() > 0:
            wizard.fill_component(name="web", image=RUNNABLE_IMAGE)
        # The invite config step: the 'active' sequence lives here (default-language select present).
        if page.locator("select[name*='default-language']").count() > 0 and not saw_invite_step:
            saw_invite_step = True
            _add_wizard_invite(page)
        wizard.click_next()

    assert saw_invite_step, "the invite config step never appeared in the create wizard"
    wizard.submit_wizard()
    page.wait_for_load_state("networkidle")
    name = forgejo.wait_for_new_project(before, timeout=240)
    assert name, "no project appeared in Forgejo"
    return name, read_api_key_with_retry(page, sandbox_url, name)


@pytest.fixture(scope="module")
def invite_project(sandbox_context: BrowserContext, sandbox_url: str, forgejo: ForgejoClient):
    """Create one invite project through the real wizard and yield its name."""
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


def test_wizard_wrote_invite_active(invite_project: str, forgejo: ForgejoClient) -> None:
    # The headline capability: creating an invite through the portal persists it.
    # Before the fix this was ``active: []`` even though the row was filled at submit.
    keys = _invite_keys(forgejo, invite_project)
    assert _WIZARD_KEY in keys, f"invite key not persisted to services/invite/config/active: {keys}"


def test_configure_modal_adds_invite(
    invite_project: str, sandbox_url: str, sandbox_page: Page, forgejo: ForgejoClient, capture
) -> None:
    # (B) the per-service 'Configureer' button opens the invite config modal; adding a second
    # invite through it must land in the file too.
    service_config.open_detail(sandbox_page, sandbox_url, invite_project)
    service_config.open_service_config_modal(sandbox_page, "Uitnodiging")
    assert "Uitnodiging" in service_config.modal_heading(sandbox_page)

    service_config.modal_add_sequence_item(sandbox_page)
    sandbox_page.locator(
        "#edit-section-inner [name$='active[1]/key'], #edit-section-inner [name$='active[0]/key']"
    ).last.fill(_MODAL_KEY)
    sandbox_page.locator(
        "#edit-section-inner [name$='active[1]/contact-email'], #edit-section-inner [name$='active[0]/contact-email']"
    ).last.fill(_CONTACT)
    capture(sandbox_page, "invite-configure-modal")
    service_config.modal_submit(sandbox_page)

    keys = _poll(lambda: _invite_keys(forgejo, invite_project), lambda ks: _MODAL_KEY in ks)
    assert _MODAL_KEY in keys, f"invite added via Configureer modal not persisted: {keys}"


def test_configure_modal_adds_keycloak_client(
    invite_project: str, sandbox_url: str, sandbox_page: Page, forgejo: ForgejoClient, capture
) -> None:
    # (C) the SAME shared sequence path on keycloak (untouched by this branch): adding an
    # additional-clients entry through its Configureer modal must persist.
    client_name = "probe-client"
    service_config.open_detail(sandbox_page, sandbox_url, invite_project)
    service_config.open_service_config_modal(sandbox_page, "Keycloak")
    service_config.modal_advance_to_field(sandbox_page, "additional-clients")
    service_config.modal_add_sequence_item(sandbox_page)
    sandbox_page.locator("#edit-section-inner [name*='additional-clients'][name$='/name']").last.fill(client_name)
    capture(sandbox_page, "keycloak-configure-modal")
    service_config.modal_submit(sandbox_page)

    names = _poll(lambda: _keycloak_client_names(forgejo, invite_project), lambda ns: client_name in ns)
    assert client_name in names, f"keycloak additional-clients entry not persisted: {names}"


def test_detail_block_shows_invite_link(invite_project: str, sandbox_url: str, sandbox_page: Page, capture) -> None:
    # (D) the invite block on the detail page is shown to an admin and shows the /invite/<key>
    # link. The link is rendered as a <code class="config-code"> (a copyable string), not an
    # <a href>, so match on the text.
    service_config.open_detail(sandbox_page, sandbox_url, invite_project)
    link = sandbox_page.locator("code.config-code", has_text=f"/invite/{_WIZARD_KEY}")
    capture(sandbox_page, "invite-detail-block")
    assert link.count() > 0, "invite link not shown on the detail page for an admin"


def _poll(read, done, *, tries: int = 30, delay_s: float = 4):
    """Poll ``read()`` until ``done(value)`` or attempts run out; return the last value.

    A modal save commits to git and reprocesses (several seconds), so the project file is the
    source of truth to wait on, not the page.
    """
    import time

    value = read()
    while not done(value) and tries > 0:
        time.sleep(delay_s)
        value = read()
        tries -= 1
    return value
