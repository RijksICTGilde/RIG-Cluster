"""De wizard schrijft het webadres onder de dienst -- gemeten aan het BESTAND.

De bestaande webadres-e2e's kijken naar de STAP: staat het veld er, wordt de selectielijst
gevuld, verschijnt het vinkje. Dat is precies wat groen bleef terwijl een create stuk was:
de stap ziet er goed uit en wat er wordt weggeschreven is iets anders. Deze poort kijkt
daarom naar de uitkomst -- het projectbestand dat de wizard aan de aanmaaktaak geeft.

Wat hij vastlegt (RC-60):

* de gekozen instellingen staan onder ``deployments[0]/services[publish-on-web]/config``;
* ze staan NIET meer los in de wortel van de deployment;
* het resultaat komt door ``validate_project_schema``, dat de wortelvorm sinds v2.7 afwijst.

Dat laatste is de dp-bn7-les: een bestand dat wordt geschreven maar niet valideert, valt
stil om bij de eerstvolgende verwerking en niemand ziet een foutmelding.

Draaien: uv run pytest tests/e2e/test_wizard_webadres_landt_onder_de_dienst.py -m "e2e and not sandbox" -q
"""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from opi.core.project_schema import validate_project_schema
from opi.forms.editables.editable import SERVICE_VIRTUALIZE, apply_virtualize
from opi.services.catalog.publish_on_web.domain_config import (
    DOMAIN_SETTING_KEYS,
    DomainSetting,
    domain_setting_path,
    get_domain_setting,
)
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from tests.e2e.helpers.wizard import WizardHelper, _unique_project_name

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = [pytest.mark.e2e]

OWN_COMPONENT = "web"
SUBDOMAIN = "webadrestest"
DOMAIN_FORMAT = "subdomain"
_MAX_WIZARD_STEPS = 15


def _field(setting: DomainSetting) -> str:
    """De naam waaronder het formulier deze instelling post.

    Afgeleid uit wat de dienst declareert, niet uitgetypt: het formulier post onder de
    VIRTUELE dienstensleutel, en een met de hand geschreven selector verloopt geruisloos --
    de locator matcht dan nooit en de stap loopt af op een time-out.
    """
    return apply_virtualize(domain_setting_path(setting, 0), SERVICE_VIRTUALIZE)


@pytest.fixture
def captured_yaml(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Het projectbestand dat de wizard aan de aanmaaktaak geeft -- het artefact, niet de pagina."""
    captured: list[str] = []

    import opi.core.task_helpers as task_helpers

    async def _fake_create_async_task(**kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs["payload"]["yaml_content"])
        return {"task_id": "00000000-0000-0000-0000-000000000000"}

    monkeypatch.setattr(task_helpers, "create_async_task", _fake_create_async_task)
    return captured


def _step(page: Page) -> str:
    return page.url.rsplit("/step/", 1)[-1]


def _walk(wizard: WizardHelper, page: Page, until: str | None) -> None:
    """Doorklikken tot *until* (of de samenvatting), en invullen wat de weg blokkeert.

    Nooit een vast aantal kliks: elke gekozen dienst voegt zijn eigen stap toe, dus tellen
    verrot stil zodra de stappenlijst verandert.
    """
    for _ in range(_MAX_WIZARD_STEPS):
        if until is None and page.locator("#wizard-review-submit").count() > 0:
            return
        if until is not None and _step(page) == until:
            return
        if _step(page) == "team":
            wizard.fill_team(email="test@example.com")
        elif _step(page) == "components":
            wizard.fill_component(name=OWN_COMPONENT, image="nginx:1.25")
        wizard.click_next()
        page.wait_for_load_state("networkidle")
    errors = page.locator(
        ".rvo-form-feedback, .utrecht-form-field-error-message, [class*='error-text']"
    ).all_inner_texts()
    raise AssertionError(
        f"{until or 'samenvatting'} niet bereikt in {_MAX_WIZARD_STEPS} stappen; vast op {page.url}; meldingen: {errors}"
    )


def _fill_web_address(page: Page) -> None:
    """Kies het URL-formaat, vul het subdomein en vraag het aan als de stap dat eist.

    Het formaatveld draagt ``data-rerender``: de stap wordt server-side opnieuw opgebouwd en
    het subdomeinveld bestaat pas daarna. Meteen typen vult een veld dat op het punt staat
    vervangen te worden, en dan staat er bij het opslaan "Dit veld is verplicht" op een veld
    dat je hebt ingevuld. Daarom wordt na elke stap op de her-render gewacht.

    De domeinen van het testcluster zijn subdomeinbeperkt, dus een nog niet goedgekeurd
    subdomein maakt het aanvraagvinkje verplicht. Dat vinkje verschijnt pas NA de her-render
    die op het verlaten van het subdomeinveld volgt.
    """
    _htmx_settle(page)
    page.locator(f"select[name='{_field(DomainSetting.DOMAIN_FORMAT)}']").select_option(value=DOMAIN_FORMAT)
    _wait_htmx_settled(page)

    selector = f"input[name='{_field(DomainSetting.SUBDOMAIN)}']"
    subdomain = page.locator(selector)
    subdomain.wait_for(state="visible", timeout=10000)
    _htmx_settle(page)
    subdomain.fill(SUBDOMAIN)
    subdomain.press("Tab")
    _wait_htmx_settled(page)

    assert page.locator(selector).input_value() == SUBDOMAIN, (
        f"het subdomein bleef leeg na invullen (vast op {page.url})"
    )

    checkbox = page.locator("input[name='deployments[0]/_request-subdomain']")
    if checkbox.count():
        _htmx_settle(page)
        checkbox.first.check()
        _wait_htmx_settled(page)


def _htmx_settle(page: Page) -> None:
    """Arm the htmx:afterSettle flag before an action that re-renders the step."""
    page.evaluate("""() => {
        window.__htmxSettled = false;
        document.addEventListener('htmx:afterSettle', function handler() {
            window.__htmxSettled = true;
            document.removeEventListener('htmx:afterSettle', handler);
        });
    }""")


def _wait_htmx_settled(page: Page, timeout: int = 10000) -> None:
    """Wait for the re-render armed by ``_htmx_settle`` to land."""
    # A step that does not re-render never sets the flag, and that is not an error here:
    # the wait is a speed-up, the assertions are what decide.
    with contextlib.suppress(PlaywrightTimeoutError):
        page.wait_for_function("() => window.__htmxSettled === true", timeout=timeout)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(300)


@pytest.fixture
def created_project(app_server: str, auth_page: Page, captured_yaml: list[str]) -> dict[str, Any]:
    """Loop de create-wizard, vul de stap Webadres in, en geef het geschreven project terug."""
    wizard = WizardHelper(auth_page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(display_name=_unique_project_name("webadres"), description="webadres onder de dienst")
    wizard.click_next()

    _walk(wizard, auth_page, until="domains")

    _fill_web_address(auth_page)

    wizard.click_next()
    auth_page.wait_for_load_state("networkidle")
    _walk(wizard, auth_page, until=None)
    wizard.submit_wizard()

    # Wachten op het bestand, niet op een vast aantal seconden: hoe lang de submit duurt is
    # een eigenschap van de machine, niet van het gedrag dat hier wordt gemeten.
    deadline = time.monotonic() + 30
    while not captured_yaml and time.monotonic() < deadline:
        auth_page.wait_for_timeout(100)

    assert captured_yaml, f"de wizard maakte geen project aan (vast op {auth_page.url})"
    project = yaml.safe_load(captured_yaml[-1])
    assert isinstance(project, dict)
    return project


class TestWebadresLandtOnderDeDienst:
    def test_de_instellingen_staan_onder_de_dienst(self, created_project: dict[str, Any]) -> None:
        deployment = created_project["deployments"][0]
        config = next(
            entry["config"]
            for entry in deployment.get("services", [])
            if isinstance(entry, dict) and entry.get("reference") == "publish-on-web"
        )
        assert config["domain-format"] == DOMAIN_FORMAT
        assert config["subdomain"] == SUBDOMAIN

    def test_er_staat_niets_meer_los_in_de_wortel(self, created_project: dict[str, Any]) -> None:
        # De helft van de fout die dit plan opheft was dat beide plekken tegelijk gevuld
        # raakten; dan wint bij het lezen de een en bij het schrijven de ander.
        deployment = created_project["deployments"][0]
        stragglers = [key for key in DOMAIN_SETTING_KEYS if key in deployment]
        assert not stragglers, f"nog in de wortel van de deployment: {stragglers}"

    def test_de_accessor_leest_wat_de_wizard_schreef(self, created_project: dict[str, Any]) -> None:
        deployment = created_project["deployments"][0]
        assert get_domain_setting(deployment, DomainSetting.SUBDOMAIN) == SUBDOMAIN
        assert get_domain_setting(deployment, DomainSetting.DOMAIN_FORMAT) == DOMAIN_FORMAT

    def test_het_geschreven_bestand_valideert(self, created_project: dict[str, Any]) -> None:
        # Zonder deze regel is een create die de oude vorm schrijft nog steeds groen op de
        # drie asserties hierboven, en valt hij pas om bij de eerstvolgende verwerking.
        validate_project_schema(created_project)
