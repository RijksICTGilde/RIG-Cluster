"""Live sandbox E2E for the env-vars and aliases fields after RC-25 made them services.

RC-25 moved these two fields out of hand-authored form code and into the catalog: the
component form's "Variabelen" fieldset was replaced by two service-owned fieldsets, and
the deployment-component env-vars fieldset now comes from a registry hook
(``config_deployment_component_layout``) that did not exist before. Unit tests prove the
objects are wired together; only a browser proves the fields still render, save, and end
up encrypted in the project file.

RC-388 veranderde de weg voor env-vars: het veld staat niet meer in de bewerkwizard of de
componentmodals (het kan een geheim bevatten, dus hoort het niet in een algemeen
formulier), maar in een eigen venster achter een verse step-up
(``opi/web/router_env_vars.py``). Alleen de aanmaakwizard vraagt de initiële set: er is
dan nog geen opgeslagen waarde om te lezen. Deze suite meet die weg: de browsercontext
draagt een sessie MET een verse step-up-stempel (dezelfde vorm als ``geef_projecttoken``),
want de sandbox-admin registreert geen OTP; de OTP-dialoog zelf dekt
``tests/e2e/test_env_vars_venster.py`` lokaal.

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
from tests.e2e.helpers.lifecycle import RUNNABLE_IMAGE, naam_en_sleutel_na_de_wizard
from tests.e2e.helpers.step_up import context_with_fresh_step_up
from tests.e2e.helpers.wizard import WizardHelper, veldbesturing

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Page
    from tests.e2e.helpers.forgejo import ForgejoClient

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox]

_API_VERIFY_SSL = os.environ.get("E2E_API_VERIFY_SSL", "false").lower() in ("1", "true", "yes")
_USER_EMAIL = os.environ.get("E2E_SANDBOX_USER", "admin@sandbox.rijksapp.dev")
_AGE_ARMOR = "-----BEGIN AGE ENCRYPTED FILE-----"


def _walk_create_wizard(page: Page, sandbox_url: str, forgejo: ForgejoClient) -> tuple[str, str]:
    """Create a minimal one-component project; return (project_name, api_key).

    Asserts the aliases fieldset is present on the component step and the env-vars field
    is NOT: the aliases system service still contributes its field to the generic form,
    and user-env-vars deliberately does not anymore (RC-388); both are silent-failure
    shapes worth catching.
    """
    wizard = WizardHelper(page, sandbox_url)
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
        email = veldbesturing(page, "users[0]/email")
        if email.count() > 0 and (email.first.input_value() or "") == "":
            wizard.fill_team(email=_USER_EMAIL)
        if page.locator("[name='components[0]/name']").count() > 0:
            wizard.fill_component(name="web", image=RUNNABLE_IMAGE)
            assert page.locator("[name='components[0]/aliases']").count() > 0, (
                "the aliases field is not on the component step -- the aliases system "
                "service did not contribute its fieldset"
            )
            # Bij het AANMAKEN staat het veld er wel: er is nog geen opgeslagen waarde om
            # te lezen, dus geen step-up. Pas daarna schrijft alleen het venster (RC-388).
            assert page.locator("[name='components[0]/user-env-vars']").count() > 0, (
                "het initiële user-env-vars-veld mist op de aanmaakstap; alleen aanmaken mag zonder step-up"
            )
            saw_fields = True
        wizard.click_next()

    assert saw_fields, "never reached the component step"
    wizard.submit_wizard()
    # De naam komt van de voortgangspagina: de taak weet hem en weet of het gelukt is.
    return naam_en_sleutel_na_de_wizard(page, sandbox_url, forgejo, timeout=600)


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


def _open_window_and_fill(page: Page, sandbox_url: str, project_name: str, tekst: str) -> None:
    """Open het step-up-venster van component `web` en sla de tekst op.

    De browsercontext draagt een verse step-up-stempel (zie context_with_fresh_step_up),
    dus de dialoog komt niet ertussen; die is lokale e2e-werk in test_env_vars_venster.py.
    """
    from tests.e2e.helpers.tabs import open_tab

    page.goto(f"{sandbox_url}/projects/{project_name}/componenten")
    page.wait_for_load_state("networkidle")
    open_tab(page, "componenten")
    knop = (
        page.locator(".component-card")
        .filter(has=page.get_by_text("web", exact=True))
        .first.locator("nldd-button:has-text('Omgevingsvariabelen bewerken')")
    )
    knop.wait_for(state="visible")
    knop.click()
    editor = page.locator("#edit-section-inner .kv-editor .cm-content")
    editor.wait_for(state="visible")
    editor.click()
    page.keyboard.press("ControlOrMeta+a")
    if tekst:
        page.keyboard.type(tekst)
    page.locator("#edit-section-inner button:has-text('Opslaan')").first.click()


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
    # Sinds RC-106 EEN AGE-blok, net als user-env-vars: de naam staat erin en niet
    # ernaast, dus het projectbestand kan alleen bevestigen DAT er versleutelde aliassen
    # staan. Wat erin staat leest het scherm hieronder terug.
    assert isinstance(stored, str), f"aliases moeten een blok zijn, geen mapping: {type(stored).__name__}"
    assert _AGE_ARMOR in stored, "de alias staat onversleuteld in het projectbestand"

    # En dan de weg die daarna kapot was: modal opnieuw openen, IETS ANDERS wijzigen,
    # opslaan. Toen een alias nog per waarde onder zijn eigen naam werd versleuteld, gaf
    # de redactie van de wizardsessie de plaatshouder terug in het veld en weigerde de
    # validator elke volgende opslag met "Alias(sen) zonder verwijzing" - de modal was
    # daarna niet meer op te slaan, ook niet voor een gewone gebruiker.
    modal = _open_components_modal(sandbox_page, sandbox_url, envali_project)
    modal.fill_codemirror_kv("components[0]/aliases", "POSTGRES_HOST=$DATABASE_SERVER_HOST\nTWEEDE=$PUBLIC_HOST")
    modal.submit_step_expect_progress()
    capture(sandbox_page, "aliases-second-save")
    kept = _wait_for(lambda: (_component(forgejo, envali_project, "web") or {}).get("aliases"))
    assert kept, "de alias overleefde de tweede opslag niet"
    assert _AGE_ARMOR in str(kept), f"de alias staat niet meer versleuteld opgeslagen: {kept}"

    # De poort op wat er in het blok zit, en meteen op de melding die RC-106 veroorzaakte:
    # het bewerkveld moet de eigen alias tonen en niet de plaatshouder of een AGE-blok.
    modal = _open_components_modal(sandbox_page, sandbox_url, envali_project)
    veld = sandbox_page.locator(".kv-editor:has(textarea[name='components[0]/aliases']) .cm-content")
    veld.wait_for(state="visible")
    getoond = veld.inner_text()
    capture(sandbox_page, "aliases-teruggelezen")
    assert "POSTGRES_HOST=$DATABASE_SERVER_HOST" in getoond, f"het aliasveld toont iets anders: {getoond!r}"
    assert "__opi-redacted-secret__" not in getoond, "de redactieplaatshouder staat in het aliasveld"
    assert _AGE_ARMOR not in getoond, "er staat een AGE-blok in het aliasveld"


def test_component_env_vars_through_the_step_up_window(
    envali_project: str, sandbox_url: str, browser: Browser, forgejo: ForgejoClient, capture
) -> None:
    """Env-vars op het component: alleen het step-up-venster schrijft ze nog (RC-388)."""
    context = context_with_fresh_step_up(browser, sandbox_url)
    page = context.new_page()
    try:
        _open_window_and_fill(page, sandbox_url, envali_project, "E2E_TOKEN=rc388-secret-value")
        capture(page, "component-env-vars-venster")
    finally:
        page.close()
        context.close()

    stored = _wait_for(lambda: (_component(forgejo, envali_project, "web") or {}).get("user-env-vars"))
    assert stored, "the env var never reached the project file"
    assert _AGE_ARMOR in str(stored), f"user-env-vars is not AGE-encrypted: {str(stored)[:60]}"
    assert "rc388-secret-value" not in str(stored), "the plaintext value is in the project file"


def test_deployment_component_env_vars_override_through_the_step_up_window(
    envali_project: str, sandbox_url: str, browser: Browser, sandbox_page: Page, forgejo: ForgejoClient, capture
) -> None:
    """De deployment-overschrijving loopt via dezelfde beveiligde weg (RC-388).

    De twee lagen moeten APART in het bestand blijven: op de cluster wint de deployment-
    componentwaarde per sleutel, en dat betekent alleen iets zolang beide op hun eigen
    plek staan.
    """
    # De component-laag eerst, anders is "apart bewaren" niet te meten.
    if not (_component(forgejo, envali_project, "web") or {}).get("user-env-vars"):
        context = context_with_fresh_step_up(browser, sandbox_url)
        page = context.new_page()
        try:
            _open_window_and_fill(page, sandbox_url, envali_project, "E2E_TOKEN=rc388-secret-value")
        finally:
            page.close()
            context.close()
        assert _wait_for(lambda: (_component(forgejo, envali_project, "web") or {}).get("user-env-vars")), (
            "kon de component-laag niet klaarzetten voor deze test"
        )

    from tests.e2e.helpers.tabs import open_tab

    context = context_with_fresh_step_up(browser, sandbox_url)
    page = context.new_page()
    try:
        page.goto(f"{sandbox_url}/projects/{envali_project}/deployments")
        page.wait_for_load_state("networkidle")
        open_tab(page, "deployments")
        button = page.locator("nldd-button:has-text('Omgevingsvariabelen bewerken')").first
        button.wait_for(state="visible")
        button.click()
        editor = page.locator("#edit-section-inner .kv-editor .cm-content")
        editor.wait_for(state="visible")
        editor.click()
        page.keyboard.press("ControlOrMeta+a")
        page.keyboard.type("DEPLOY_ONLY=rc388-deployment-value")
        capture(page, "deployment-component-env-vars-venster")
        page.locator("#edit-section-inner button:has-text('Opslaan')").first.click()
    finally:
        page.close()
        context.close()

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

    component_level = (_component(forgejo, envali_project, "web") or {}).get("user-env-vars")
    assert component_level, (
        "writing the deployment-component override wiped the component-level user-env-vars; "
        "the two layers must be stored separately for the merge to have anything to merge"
    )
    assert str(component_level) != str(stored), (
        "component and deployment-component user-env-vars hold identical ciphertext, "
        "which suggests one write landed on both layers"
    )


def test_the_modals_leave_the_env_vars_byte_identical(
    envali_project: str, sandbox_url: str, sandbox_page: Page, forgejo: ForgejoClient
) -> None:
    """De gewone schrijfweg (RC-388): een no-op opslag via de componenten-modal mag het
    versleutelde blok niet aanraken - geen placeholder, geen terugzetstap, geen churn."""
    # Deze test heeft een bestaand blok nodig; zet het klaar als de eerdere tests in deze
    # module het nog niet deden (pytest-volgorde is niet te voorspellen).
    modal = _open_components_modal(sandbox_page, sandbox_url, envali_project)
    modal.submit_step_expect_progress()

    voor = (_component(forgejo, envali_project, "web") or {}).get("user-env-vars")
    if not voor:
        pytest.skip("geen user-env-vars opgeslagen; component-test draaide niet eerder")

    modal = _open_components_modal(sandbox_page, sandbox_url, envali_project)
    modal.submit_step_expect_progress()

    def _zelfde_blok():
        return (_component(forgejo, envali_project, "web") or {}).get("user-env-vars") == voor

    assert _wait_for(_zelfde_blok), "een no-op opslag via de componenten-modal schreef het user-env-vars-blok opnieuw"
