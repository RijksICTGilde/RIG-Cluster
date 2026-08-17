"""Een keuze in een cascade die valt terwijl er nog een hertekenverzoek loopt (RC-127).

De cascade in de wizard is server-side: elke keuze in een afhankelijk veld
(``data-rerender``) dient de stap in en de server tekent de rij opnieuw, met de lijst
eronder gevuld. Overlappen twee van die keuzes -- de tweede valt binnen het verzoek van de
eerste -- dan verdween de tweede geruisloos. Gemeten in de browser:

    change  to/component   inflight=false  -> htmx:configRequest, htmx:beforeRequest
    change  from/project    inflight=true  -> (geen van beide, ook niet later)
    de keuzelijst 'from/deployment' bood daarna alleen [''] en bleef dat

htmx zet dat tweede verzoek in de wachtrij van het element dat het doet (het formulier) en
speelt het na het eerste antwoord opnieuw af -- maar dat antwoord vervangt
``#wizard-step-content``, waar het formulier zelf in zit, en htmx weigert een verzoek op een
element dat niet meer in het document staat. Gevolg: een geldige keuze in de rij, een lege
lijst eronder, geen fout, en geen herstel. De stap is dan niet meer in te vullen, want het
veld eronder is verplicht.

Deze test forceert dat venster in plaats van erop te hopen: de twee wijzigingen worden in
hetzelfde script afgevuurd, dus de tweede valt gegarandeerd binnen het verzoek van de
eerste. Zo is dit een poort en niet een kansspel. Draai je de bescherming in
``static/js/wizard.js`` terug, dan blijft de deploymentlijst leeg en valt hij om.

Run: uv run pytest tests/e2e/test_wizard_cascade_tijdens_verzoek.py -m "e2e and not sandbox" -q
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from opi.services.services_enums import ServiceType
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from tests.e2e.helpers.htmx import wait_for_htmx_quiet
from tests.e2e.helpers.tekst import veld
from tests.e2e.helpers.wizard import WizardHelper, unique_project_name

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = [pytest.mark.e2e]

SERVICE = ServiceType.CROSS_DOMAIN_ACCESS.value
STEP = f"{SERVICE}-config"
FIELD = f"_services-config/{SERVICE}/config/inbound[0]"

PEER_PROJECT = "test-project"
PEER_DEPLOYMENT = "default"
OWN_COMPONENT = "web"

_MAX_WIZARD_STEPS = 15
#: Vangnet, geen wachtmechanisme: de wachtregels hieronder keren terug zodra de voorwaarde
#: waar is. Een hertekening is een volledige heen-en-weer met de server.
_TIMEOUT_MS = 30_000

#: Tellen wat htmx doet, zodat een rood zegt WAAROM de lijst leeg bleef in plaats van alleen
#: dat hij leeg bleef. Een wijziging die binnen een lopend verzoek valt en geen
#: ``configRequest`` oplevert, is de fout zelf -- die staat hier dus als eigen regel.
_PROBE = """
window.__cascade = {verzoeken: 0, antwoorden: 0, genegeerd: []};
document.addEventListener('htmx:configRequest', function () { window.__cascade.verzoeken++; }, true);
document.addEventListener('htmx:afterRequest', function () { window.__cascade.antwoorden++; }, true);
document.addEventListener('change', function (e) {
    var f = document.getElementById('wizard-step-form');
    if (!f || !f.classList.contains('htmx-request')) return;
    if (!(e.target.closest && e.target.closest('[data-rerender]'))) return;
    window.__cascade.genegeerd.push(e.target.getAttribute('name'));
}, true);
"""


def _step(page: Page) -> str:
    return page.url.rsplit("/step/", 1)[-1]


def _walk_to_the_rule_step(wizard: WizardHelper, page: Page) -> None:
    """Klik door tot de cross-domain-stap en vul onderweg wat de doorgang blokkeert."""
    for _ in range(_MAX_WIZARD_STEPS):
        if _step(page) == STEP:
            return
        if _step(page) == "team":
            wizard.fill_team(email="test@example.com")
        elif _step(page) == "components":
            wizard.fill_component(name=OWN_COMPONENT, image="nginx:1.25")
        wizard.click_next()
        page.wait_for_load_state("networkidle")
    raise AssertionError(f"{STEP} niet bereikt binnen {_MAX_WIZARD_STEPS} stappen; blijft staan op {page.url}")


def _keuzelijst(page: Page, veldnaam: str) -> list[dict[str, str]] | None:
    """De opties van dit veld: waarde EN opschrift.

    Het opschrift hoort erbij. Een lege lijst komt met een uitleg als "Kies eerst een
    project" of "Dit project heeft geen deployments op dit cluster", en dat is het verschil
    tussen "de server heeft de keuze nooit gezien" en "de server heeft hem gezien en had
    niets te bieden". Zonder dat opschrift is een lege lijst niet te duiden.
    """
    return page.evaluate(
        "(name) => { const el = document.querySelector(`select[name='${name}']`);"
        " return el ? [...el.options].map(o => ({waarde: o.value, opschrift: o.label})) : null; }",
        f"{FIELD}/{veldnaam}",
    )


def _rij(page: Page) -> dict[str, str]:
    return page.evaluate(
        "() => Object.fromEntries([...document.querySelectorAll('select[name*=\"/config/inbound[\"]')]"
        ".map(e => [e.getAttribute('name').split('/inbound[0]/')[1], e.value]))"
    )


def _cascade_stand(page: Page) -> dict[str, Any]:
    return page.evaluate("() => window.__cascade")


def test_een_keuze_tijdens_een_lopende_hertekening_raakt_niet_weg(app_server: str, auth_page: Page) -> None:
    page = auth_page
    page.add_init_script(_PROBE)

    wizard = WizardHelper(page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(display_name=unique_project_name("cda"), description="cascade tijdens verzoek")
    wizard.click_next()
    wizard.fill_services([SERVICE])
    _walk_to_the_rule_step(wizard, page)

    page.locator("button:has-text('Item toevoegen')").first.click()
    veld(page, f"{FIELD}/name").wait_for(state="visible", timeout=_TIMEOUT_MS)
    veld(page, f"{FIELD}/name").fill("tijdens-een-verzoek")
    wait_for_htmx_quiet(page)

    # Twee keuzes in hetzelfde script: de eerste start een hertekenverzoek, de tweede valt
    # daar binnen. Geen wachten ertussen, want juist die overlap is wat getoetst wordt.
    page.evaluate(
        """([pad, eigenComponent, peerProject]) => {
            const kies = (veldnaam, waarde) => {
                const el = document.querySelector(`select[name='${pad}${veldnaam}']`);
                el.value = waarde;
                el.dispatchEvent(new Event('change', {bubbles: true}));
            };
            kies('to/component', eigenComponent);
            kies('from/project', peerProject);
        }""",
        [f"{FIELD}/", OWN_COMPONENT, PEER_PROJECT],
    )

    # De keuze mag onderweg één keer overgeslagen worden -- daar gaat dit over -- maar hij
    # moet daarna alsnog bij de server landen, en dat is te zien aan de lijst die hij vult.
    try:
        page.wait_for_function(
            "([name, waarde]) => { const el = document.querySelector(`select[name='${name}']`);"
            " return el && [...el.options].some(o => o.value === waarde); }",
            arg=[f"{FIELD}/from/deployment", PEER_DEPLOYMENT],
            timeout=_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError as exc:
        stand = _cascade_stand(page)
        raise AssertionError(
            f"de deploymentlijst bleef leeg na een keuze binnen een lopend verzoek: "
            f"lijst={_keuzelijst(page, 'from/deployment')}; rij={_rij(page)}; "
            f"htmx-verzoeken={stand['verzoeken']} antwoorden={stand['antwoorden']}; "
            f"overgeslagen wijzigingen={stand['genegeerd']}"
        ) from exc

    # En de keuze staat ook echt in de rij: een gevulde lijst met een leeg veld erboven zou
    # betekenen dat de hertekening de keuze onderweg alsnog kwijtraakte.
    assert _rij(page)["from/project"] == PEER_PROJECT, f"de gekozen peer staat niet in de rij: {_rij(page)}"

    stand = _cascade_stand(page)
    assert stand["genegeerd"], (
        "geen enkele wijziging viel binnen een lopend verzoek, dus deze test heeft het "
        f"venster niet geraakt en toetst niets: {stand}"
    )
