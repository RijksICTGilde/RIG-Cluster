"""Een cascade op een AANVINKVAKJE, tijdens een lopende hertekening (RC-128).

`test_wizard_cascade_tijdens_verzoek.py` pint de RC-127-fix op de cross-domain-stap, waar de
afhankelijke velden ``<select>`` zijn. De fix hangt aan een document-brede
``change``-luisteraar met filter ``[data-rerender]``, dus volgens zijn eigen opzet geldt hij
voor ELK afhankelijk veld. Niet elk afhankelijk veld is een select, en dat is wat hier
getoetst wordt.

Geteld op de bron met een AST-telling over ``opi/`` (elke ``EditableVisualizer`` met
``data-rerender`` in zijn ``attributes``, met zijn ``widget=``) - 23 velden in totaal:

    19x SELECT          cross-domain (8), publish-on-web (6), deployments (4), bijlagen (1)
     2x TEXT            Subdomein, Eigen domein
     1x CHECKBOX        keycloak "Toegang beperken"
     1x CHECKBOX_GROUP  de componentstap, "Gebruikte services"

Tel niet met een grep: de acht cross-domain-velden delen een ``_CASCADE``-constante als
``attributes``, dus een grep op ``data-rerender`` telt ze als een en komt uit op 15/11.

Twee dingen maken de laatste twee anders dan een select, en beide zijn hier gemeten:

1. Het herstelpad in ``wizard.js`` zet ``vers.value = waarde``. Bij een select en een
   tekstveld IS ``value`` de keuze; bij een vakje zit de keuze in ``checked`` en is ``value``
   een vaste string, dus daar draagt het niets over.
2. Het zoekt het verse veld op via ``_besturingMetNaam(e.target.getAttribute('name'))``.
   Gemeten op de componentstap: het ``[data-rerender]``-element is ``<nldd-checkbox-field>``,
   zijn ``name`` is de LEGE STRING, en de echte inputs zitten in geneste schaduwbomen (nul in
   de lichte boom). ``naam ? ... : null`` op een lege string is falsy, dus dat herstelpad kan
   dit veld niet terugvinden.

**En toch klopt het gedrag**, en dat is de uitkomst die hier vastligt: de swap op deze stap
haalt de ``<nldd-checkbox-field>`` niet uit het document, dus ``_hertekenNaDeSwap`` komt in
zijn EERSTE tak (``document.contains(bron)``) en tekent gewoon opnieuw. De waarde hoeft dan
niet gedragen te worden, want hij stond er nog. Het waardepad met ``.value`` is voor een vakje
dus dode letter, maar niet nodig.

Daarom staan hier twee tests: een die de VORM van het veld vastlegt (lege naam, vakjes in de
schaduwboom), en een die het GEDRAG in het venster meet. Verandert die vorm -- gaat de host
bijvoorbeeld wel een naam dragen, of wordt hij bij de swap vervangen -- dan valt de eerste
test en is dat het signaal om opnieuw naar het waardepad te kijken, in plaats van dat het
stil meebeweegt.

Run: uv run pytest tests/e2e/test_wizard_cascade_aanvinkvakje.py -m "e2e and not sandbox" -q
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from tests.e2e.helpers.htmx import wait_for_htmx_quiet
from tests.e2e.helpers.wizard import WizardHelper, unique_project_name

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = [pytest.mark.e2e]

_MAX_WIZARD_STEPS = 15

#: Tellen wat htmx doet, en de console-waarschuwingen opvangen: het herstelpad MELDT het als
#: het een veld niet kan terugzetten, en die regel is hier de directe aanwijzing.
_PROBE = """
window.__cascade = {verzoeken: 0, antwoorden: 0, genegeerd: [], waarschuwingen: []};
document.addEventListener('htmx:configRequest', function () { window.__cascade.verzoeken++; }, true);
document.addEventListener('htmx:afterRequest', function () { window.__cascade.antwoorden++; }, true);
document.addEventListener('change', function (e) {
    var f = document.getElementById('wizard-step-form');
    if (!f || !f.classList.contains('htmx-request')) return;
    if (!(e.target.closest && e.target.closest('[data-rerender]'))) return;
    window.__cascade.genegeerd.push(String(e.target.getAttribute('name')));
}, true);
(function () {
    var oud = console.warn;
    console.warn = function () {
        window.__cascade.waarschuwingen.push(Array.prototype.join.call(arguments, ' '));
        return oud.apply(console, arguments);
    };
})();
"""


def _step(page: Page) -> str:
    return page.url.rsplit("/step/", 1)[-1]


def _loop_naar_componenten(wizard: WizardHelper, page: Page) -> None:
    for _ in range(_MAX_WIZARD_STEPS):
        if _step(page) == "components":
            return
        if _step(page) == "team":
            wizard.fill_team(email="test@example.com")
        wizard.click_next()
        page.wait_for_load_state("networkidle")
    raise AssertionError(f"componentstap niet bereikt; blijft staan op {page.url}")


def _dienstvakjes(page: Page) -> list[dict[str, Any]]:
    """De dienstvakjes, uit de SCHADUWBOOM van het ``[data-rerender]``-veld.

    Ze staan niet in de lichte boom: het veld is een ``<nldd-checkbox-field>`` en de inputs
    zitten erin. Een selector op ``[data-rerender] input`` vindt er nul en levert een
    nietszeggende skip op -- dat was de eerste poging.
    """
    return page.evaluate(
        """() => {
             const host = document.querySelector('[data-rerender]');
             if (!host) return [];
             const uit = [];
             const loop = (wortel) => {
                 for (const c of wortel.querySelectorAll('input[type=checkbox]')) {
                     uit.push({waarde: c.value, aan: c.checked, naam: c.getAttribute('name')});
                 }
                 for (const e of wortel.querySelectorAll('*')) {
                     if (e.shadowRoot) loop(e.shadowRoot);
                 }
             };
             loop(host);
             if (host.shadowRoot) loop(host.shadowRoot);
             return uit;
           }"""
    )


def _veldinfo(page: Page) -> dict[str, Any]:
    return page.evaluate(
        """() => {
             const h = document.querySelector('[data-rerender]');
             if (!h) return {gevonden: false};
             return {gevonden: true, tag: h.tagName, naam: h.getAttribute('name'),
                     heeftSchaduw: !!h.shadowRoot,
                     vakjesInDeLichteBoom: h.querySelectorAll('input[type=checkbox]').length};
           }"""
    )


def test_het_afhankelijke_veld_op_de_componentstap_is_geen_select(app_server: str, auth_page: Page) -> None:
    """Legt de VORM vast waar de volgende test op rekent.

    Aparte test, want als deze vorm verandert (bijvoorbeeld een echt ``name`` op de host, of
    de inputs in de lichte boom) dan verandert daarmee ook of het herstelpad van RC-127 hier
    kan werken. Dan hoort deze test te vallen en niet stil mee te bewegen.
    """
    page = auth_page
    wizard = WizardHelper(page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(display_name=unique_project_name("vrm"), description="vorm van het veld")
    wizard.click_next()
    _loop_naar_componenten(wizard, page)
    wizard.fill_component(name="web", image="nginx:1.25")
    wait_for_htmx_quiet(page)

    info = _veldinfo(page)
    assert info["gevonden"], "geen enkel [data-rerender]-veld op de componentstap"
    assert info["tag"] == "NLDD-CHECKBOX-FIELD", f"onverwachte vorm van het afhankelijke veld: {info}"
    # LEGE STRING, niet afwezig. Dat is het hele punt: `_hertekenNaDeSwap` doet
    # `naam ? _besturingMetNaam(naam) : null`, en een lege string is falsy -- dus het
    # herstelpad kan dit veld niet terugvinden en haakt af met een console-waarschuwing.
    assert info["naam"] == "", (
        "de host draagt nu een echte naam. Dat is goed nieuws voor het herstelpad van RC-127 "
        f"(dat zoekt op naam), dus werk die verwachting hier bij: {info}"
    )
    assert info["vakjesInDeLichteBoom"] == 0, f"de vakjes staan nu in de lichte boom: {info}"


def test_een_vakje_dat_tijdens_een_hertekening_omgaat_houdt_de_laatste_stand(app_server: str, auth_page: Page) -> None:
    """Aanzetten en binnen datzelfde verzoek weer uitzetten: UIT hoort te blijven staan."""
    page = auth_page
    page.add_init_script(_PROBE)

    wizard = WizardHelper(page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(display_name=unique_project_name("vak"), description="cascade op een vakje")
    wizard.click_next()
    _loop_naar_componenten(wizard, page)
    wizard.fill_component(name="web", image="nginx:1.25")
    wait_for_htmx_quiet(page)

    vakjes = _dienstvakjes(page)
    assert vakjes, f"geen dienstvakjes gevonden; veldinfo={_veldinfo(page)}"
    doel = vakjes[0]["waarde"]
    begin = vakjes[0]["aan"]

    # Twee keer omzetten in hetzelfde script: de tweede wijziging valt gegarandeerd binnen het
    # hertekenverzoek dat de eerste aftrapt. De LAATSTE stand is wat de gebruiker koos.
    page.evaluate(
        """([doel, begin]) => {
            const vakje = () => {
                const h = document.querySelector('[data-rerender]');
                if (!h) return null;
                let gevonden = null;
                const loop = (wortel) => {
                    if (gevonden) return;
                    for (const c of wortel.querySelectorAll('input[type=checkbox]')) {
                        if (c.value === doel) { gevonden = c; return; }
                    }
                    for (const e of wortel.querySelectorAll('*')) {
                        if (e.shadowRoot) loop(e.shadowRoot);
                        if (gevonden) return;
                    }
                };
                loop(h);
                if (!gevonden && h.shadowRoot) loop(h.shadowRoot);
                return gevonden;
            };
            const zet = (aan) => {
                const c = vakje();
                if (!c) return;
                c.checked = aan;
                c.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
            };
            zet(!begin);
            zet(begin);
        }""",
        [doel, begin],
    )

    wait_for_htmx_quiet(page)
    page.wait_for_timeout(2000)

    stand = page.evaluate("() => window.__cascade")
    na = _dienstvakjes(page)
    nu = next((v for v in na if v["waarde"] == doel), None)

    assert stand["genegeerd"], (
        "geen enkele wijziging viel binnen een lopend verzoek, dus deze test heeft het venster "
        f"niet geraakt en toetst niets: {stand}"
    )
    assert nu is not None, f"het vakje {doel!r} is na de hertekening verdwenen: {na}"
    assert nu["aan"] == begin, (
        f"het vakje {doel!r} staat op {nu['aan']} maar de laatste keuze was {begin}: de stand die "
        f"tijdens de lopende hertekening werd gekozen is niet gevolgd. "
        f"overgeslagen wijzigingen={stand['genegeerd']}; waarschuwingen={stand['waarschuwingen']}"
    )
