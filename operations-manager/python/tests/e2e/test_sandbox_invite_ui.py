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
import uuid
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import Error as PlaywrightError
from tests.e2e.helpers import sandbox_api, service_config
from tests.e2e.helpers.lifecycle import RUNNABLE_IMAGE, project_name_from_progress, read_api_key_with_retry
from tests.e2e.helpers.wizard import WizardHelper, veldbesturing, veldbesturing_eindigend_op

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page
    from tests.e2e.helpers.forgejo import ForgejoClient

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox]

_API_VERIFY_SSL = os.environ.get("E2E_API_VERIFY_SSL", "false").lower() in ("1", "true", "yes")
_USER_EMAIL = os.environ.get("E2E_SANDBOX_USER", "admin@sandbox.rijksapp.dev")

# Unique per run: invite keys are a cross-project global namespace (UniqueInviteKeyEnforcer),
# so a leftover project from an interrupted run would otherwise block every later create.
_RUN = uuid.uuid4().hex[:8]
_WIZARD_KEY = f"probe-invite-wizard-{_RUN}"
_CONTACT = "invite-contact@sandbox.rijksapp.dev"

#: De stand die de wizard achterliet, vastgelegd door de fixture. Zie daar waarom.
_WIZARD_STAND: dict[str, list] = {"keys": []}


def _select_service(page: Page, name: str) -> None:
    # Wait for the services step to render its cards (the HTMX step swap can lag on a
    # freshly-started pod), so a slow load isn't mistaken for a missing service.
    page.wait_for_selector(f"input[name='services[]'][value='{name}']", timeout=15000)
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


def _invite_contacts(forgejo: ForgejoClient, project_name: str) -> list[str]:
    active = _invite_config(forgejo, project_name).get("active") or []
    return [item.get("contact-email") for item in active if isinstance(item, dict)]


def _keycloak_client_names(forgejo: ForgejoClient, project_name: str) -> list[str]:
    data = forgejo.get_project_yaml(project_name) or {}
    for entry in data.get("services", []):
        if isinstance(entry, dict) and entry.get("name") == "keycloak":
            clients = (entry.get("config") or {}).get("additional-clients") or []
            return [c.get("name") for c in clients if isinstance(c, dict)]
    return []


def _add_wizard_invite(page: Page) -> None:
    """On the invite config step: fill the invite's key + contact.

    Hier werd eerst op "Item toevoegen" geklikt. Die knop is er niet, en met opzet: de
    ``active``-reeks van deze dienst staat op ``min_items=1``, ``max_items=1`` en
    ``add_remove=False`` (opi/services/catalog/invite/editables.py), dus de ene rij staat
    er al zodra de stap opent. Gemeten op de sandbox liep de opzet van alle vijf de tests
    dood op het wachten op die knop.
    """
    # Op de BESTURING en niet op [name$=...]: zie veldbesturing_eindigend_op.
    veldbesturing_eindigend_op(page, "active[0]/key").first.wait_for(state="visible", timeout=15000)
    veldbesturing_eindigend_op(page, "active[0]/key").first.fill(_WIZARD_KEY)
    veldbesturing_eindigend_op(page, "active[0]/contact-email").first.fill(_CONTACT)


def _walk_create_wizard(page: Page, sandbox_url: str, forgejo: ForgejoClient) -> tuple[str, str]:
    """Drive the create wizard, adding one invite on its config step; return (name, api_key)."""
    wizard = WizardHelper(page, sandbox_url)
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
        email = veldbesturing(page, "users[0]/email")
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
    # De naam komt van de voortgangspagina: de taak weet hem en weet of het gelukt is.
    # Zie project_name_from_progress - de git-listing afvissen op een klok meldde een
    # geslaagde aanmaak als mislukking zodra de timer eerder afliep dan ArgoCD.
    name = project_name_from_progress(page, timeout=600)
    assert name in forgejo.list_project_names(), f"'{name}' staat niet in zad-projects"
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
    # De stand ZOALS DE WIZARD HEM ACHTERLIET, hier vastgelegd. De ``active``-reeks telt
    # precies een uitnodiging (max_items=1), dus elke schrijfactie van een van de andere
    # tests in deze module VERVANGT hem. Wie daarna nog naar de sleutel van de wizard
    # zoekt, meet de volgorde waarin pytest de tests draait (en die is willekeurig) in
    # plaats van de wizard. Vandaar deze momentopname.
    _WIZARD_STAND["keys"] = _invite_keys(forgejo, name)
    try:
        yield name
    finally:
        with contextlib.suppress(Exception):
            sandbox_api.delete_project_via_api(sandbox_url, name, api_key, verify_ssl=_API_VERIFY_SSL)


def test_wizard_wrote_invite_active(invite_project: str, forgejo: ForgejoClient) -> None:
    # The headline capability: creating an invite through the portal persists it.
    # Before the fix this was ``active: []`` even though the row was filled at submit.
    #
    # Op de momentopname uit de fixture en niet op het bestand van NU: zie daar.
    del forgejo
    keys = _WIZARD_STAND["keys"]
    assert _WIZARD_KEY in keys, f"invite key not persisted to services/invite/config/active: {keys}"


def test_configure_modal_writes_the_invite(
    invite_project: str, sandbox_url: str, sandbox_page: Page, forgejo: ForgejoClient, capture
) -> None:
    """(B) De 'Configureer'-knop opent de invite-modal, en wat je daar wijzigt komt in het bestand.

    Deze test voegde een TWEEDE uitnodiging toe. Dat kan niet meer, en met opzet: de
    ``active``-reeks staat op ``min_items=1``, ``max_items=1``, ``add_remove=False``
    (opi/services/catalog/invite/editables.py), dus er is geen toevoegknop en er is altijd
    precies een uitnodiging. Wat de vangrail moet bewaken is ongewijzigd - de modal schrijft
    naar het projectbestand - en dat wordt hier op de bestaande uitnodiging gemeten.
    """
    service_config.open_detail(sandbox_page, sandbox_url, invite_project)
    service_config.open_service_config_modal(sandbox_page, "Uitnodiging")
    assert "Uitnodiging" in service_config.modal_heading(sandbox_page)

    nieuw_contact = f"modal-{_RUN}@sandbox.rijksapp.dev"
    veld = veldbesturing_eindigend_op(sandbox_page, "active[0]/contact-email")
    veld.first.wait_for(state="visible", timeout=15000)
    veld.first.fill(nieuw_contact)
    capture(sandbox_page, "invite-configure-modal")
    service_config.modal_submit(sandbox_page)

    contacts = _poll(lambda: _invite_contacts(forgejo, invite_project), lambda cs: nieuw_contact in cs)
    assert nieuw_contact in contacts, f"wijziging via de Configureer-modal niet opgeslagen: {contacts}"


def test_configure_modal_adds_keycloak_client(
    invite_project: str, sandbox_url: str, sandbox_page: Page, forgejo: ForgejoClient, capture
) -> None:
    # (C) the SAME shared sequence path on keycloak (untouched by this branch): adding an
    # additional-clients entry through its Configureer modal must persist.
    client_name = f"probe-client-{_RUN}"
    service_config.open_detail(sandbox_page, sandbox_url, invite_project)
    # "Keycloak Authentication" is the exact card title -- plain "Keycloak" also matches the
    # Uitnodiging card (its description mentions the Keycloak-realm), an ambiguous selector.
    service_config.open_service_config_modal(sandbox_page, "Keycloak Authentication")
    # The keycloak config is a single step: the additional-clients sequence (with its
    # "Item toevoegen" button) is right here, no "Volgende" navigation. With no clients yet
    # there is exactly one add button, so modal_add_sequence_item targets it.
    service_config.modal_add_sequence_item(sandbox_page)
    sandbox_page.locator("#edit-section-inner [name*='additional-clients'][name$='/name']").last.fill(client_name)
    capture(sandbox_page, "keycloak-configure-modal")
    service_config.modal_submit(sandbox_page)

    names = _poll(lambda: _keycloak_client_names(forgejo, invite_project), lambda ns: client_name in ns)
    assert client_name in names, f"keycloak additional-clients entry not persisted: {names}"


def test_detail_block_shows_invite_link(
    invite_project: str, sandbox_url: str, sandbox_page: Page, forgejo: ForgejoClient, capture
) -> None:
    # (D) the invite block on the detail page is shown to an admin and shows the /invite/<key>
    # link. The link is rendered as a <code class="config-code"> (a copyable string), not an
    # <a href>, so match on the text.
    #
    # De sleutel komt uit het projectbestand van DIT moment en is niet die van de wizard:
    # de reeks telt er een, dus een andere test in deze module kan hem vervangen hebben.
    # Wat hier getoetst wordt is dat het blok de HUIDIGE uitnodiging toont.
    keys = [key for key in _invite_keys(forgejo, invite_project) if key]
    assert keys, "er staat geen uitnodiging in het projectbestand om te tonen"
    # Op Services info en niet op Overzicht: b134a581 heeft de blokken die de diensten zelf
    # leveren naar een eigen tabblad verplaatst. Deze test keek nog op de landingspagina.
    service_config.open_services_info_tab(sandbox_page, sandbox_url, invite_project)
    link = sandbox_page.locator("code.config-code", has_text=f"/invite/{keys[0]}")
    capture(sandbox_page, "invite-detail-block")
    assert link.count() > 0, f"invite link not shown on the detail page for an admin (sleutel {keys[0]})"


def test_configure_via_api_writes_invite(
    invite_project: str, sandbox_url: str, sandbox_page: Page, forgejo: ForgejoClient
) -> None:
    # (E) the NEW capability: the unified per-service config endpoint. Because the invite
    # service owns a config model, it is API-configurable for free -- a generated typed route
    # whose body IS InviteConfig. Submit a real config block over HTTP and read the committed
    # file back, the same submit-and-read rule the wizard test uses (checklist section 13).
    # PUT replaces the whole invite config, so this runs last: it overwrites the keys the
    # earlier tests added on this shared throwaway project.
    #
    # We assert on the committed file, not on full task completion: the config write commits
    # early (the "Service-config schrijven" subtask), and the task then reconciles the whole
    # project through ArgoCD -- a slow deploy wait that is not what "did the config land"
    # depends on, and matches the invite's save_only nature (configuring an invite triggers
    # no manifest change). So we start the task and poll the project file for the key.
    api_key = read_api_key_with_retry(sandbox_page, sandbox_url, invite_project)
    api_invite_key = f"probe-invite-api-{_RUN}"
    # ``active`` gaat er als EEN entry in, niet als lijst van een: de invite-dienst zet
    # ``api_singular_lists = {"active"}`` (4323ebae), dus de API toont en accepteert het
    # enkelvoud. De OPSLAG blijft een lijst -- vandaar dat ``_invite_keys`` hieronder
    # gewoon over het projectbestand loopt. Wie hier een lijst stuurt krijgt 422.
    body = {"default-language": "nl", "active": {"key": api_invite_key, "contact-email": _CONTACT}}
    sandbox_api.start_task(
        sandbox_url,
        "PUT",
        f"/api/v2/projects/{invite_project}/services/invite/config/project",
        api_key,
        body,
        verify_ssl=_API_VERIFY_SSL,
    )
    keys = _poll(lambda: _invite_keys(forgejo, invite_project), lambda ks: api_invite_key in ks)
    assert api_invite_key in keys, f"invite configured via API not persisted to the project file: {keys}"


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
