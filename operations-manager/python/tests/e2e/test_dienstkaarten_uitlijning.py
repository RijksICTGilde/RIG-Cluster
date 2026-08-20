"""De dienstkaarten in de wizard staan uitgelijnd.

Waarom dit in een browser gemeten wordt en niet uit de HTML af te lezen is: het gaat om
posities. Of de aanvinkvakjes van een rij op een lijn staan hangt af van een keten die door
drie elementen loopt (de kaart, de container die het thema eromheen zet, en onze eigen
stapel), en die keten is pas na het opmaken bekend.

Aanleiding: de kaarten stonden met vier of meer naast elkaar, waardoor icoon, naam en
vraagteken niet meer op een regel pasten, en het aanvinkvakje stond bij elke kaart op een
andere hoogte omdat de omschrijvingen verschillen. Gemeten voor de reparatie: kaarten van
245px met vakjes op y=103, y=130 en y=174 binnen dezelfde rij.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.e2e.helpers.wizard import WizardHelper

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = [pytest.mark.e2e]

#: Breed genoeg voor drie kolommen; de meting gaat over de uitlijning, niet over het
#: gedrag op een smal scherm.
BEELD_BREEDTE = 1440
BEELD_HOOGTE = 1000


def _kaarten_per_rij(page: Page) -> dict[int, list[dict[str, int]]]:
    return page.evaluate(
        """() => {
            const rijen = {};
            for (const kaart of document.querySelectorAll('.service-card')) {
                const r = kaart.getBoundingClientRect();
                const vakje = kaart.querySelector('nldd-checkbox');
                const v = vakje ? vakje.getBoundingClientRect() : null;
                const rij = Math.round(r.top);
                (rijen[rij] = rijen[rij] || []).push({
                    hoogte: Math.round(r.height),
                    vakjeTop: v ? Math.round(v.top) : null,
                    vakjeRechts: v ? Math.round(r.right - v.right) : null,
                });
            }
            return rijen;
        }"""
    )


@pytest.fixture
def dienstenstap(app_server: str, auth_page: Page) -> Page:
    wizard = WizardHelper(auth_page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(display_name="uitlijning", description="uitlijning van de dienstkaarten")
    wizard.click_next()
    auth_page.set_viewport_size({"width": BEELD_BREEDTE, "height": BEELD_HOOGTE})
    auth_page.locator(".service-card").first.wait_for(state="visible")
    return auth_page


def test_er_staan_drie_kaarten_naast_elkaar(dienstenstap: Page) -> None:
    """Drie, want bij vier breekt de regel met icoon, naam en vraagteken."""
    rijen = _kaarten_per_rij(dienstenstap)
    volle_rijen = [len(kaarten) for kaarten in rijen.values()]
    assert max(volle_rijen) == 3, f"er passen {max(volle_rijen)} kaarten op een rij, verwacht 3"


def test_de_kaarten_van_een_rij_zijn_even_hoog(dienstenstap: Page) -> None:
    for top, kaarten in _kaarten_per_rij(dienstenstap).items():
        hoogtes = {kaart["hoogte"] for kaart in kaarten}
        assert len(hoogtes) == 1, f"rij op y={top} heeft kaarten van verschillende hoogte: {sorted(hoogtes)}"


def test_de_aanvinkvakjes_van_een_rij_staan_op_een_lijn(dienstenstap: Page) -> None:
    """Het vakje hangt onderaan de kaart, dus bij gelijke kaarthoogte staan ze op een lijn.

    Dat kostte moeite. De keten van de rastercel naar het vakje liep stuk op <c-card>: dat
    zet altijd een <nldd-container> om zijn inhoud, en de schaduw-div daarbinnen groeit niet
    mee met zijn eigen host. Gemeten op een korte kaart in een hoge rij: kaart 426px,
    container 426px, div erin 214px. Alles eronder volgde die 214.

    Daar is niet bij te komen zonder in de binnenkant van dat component te schrijven, dus de
    kaart is nu een <nldd-card> met onze eigen <c-box> en <c-stack> erin. Die zijn gewone
    divs uit de layoutlaag zonder schaduwboom, dus flex-grow werkt er gewoon op.
    """
    for top, kaarten in _kaarten_per_rij(dienstenstap).items():
        hoogtes = {kaart["vakjeTop"] for kaart in kaarten}
        assert len(hoogtes) == 1, f"rij op y={top} heeft vakjes op verschillende hoogte: {sorted(hoogtes)}"


def test_de_inhoud_gebruikt_de_hele_kaartbreedte(dienstenstap: Page) -> None:
    """De tekst en het vakje horen de breedte van de kaart te volgen.

    Hier stond een toets dat de vakjes van een rij op EEN LIJN staan. Dat vroeg om de kaart
    op volle celhoogte te zetten en de <nldd-container> erin te laten meegroeien, en die
    container heeft een schaduwboom: zijn kind wordt daarin geslot, dus onze flexregels
    landden op een host waarvan de layout ergens anders bepaald wordt. Op de sandbox bleef
    de hele inhoud van een kaart daardoor op ongeveer de halve breedte staan - tekst en
    aanvinkvakje allebei.

    Die regels zijn weg. Wat hier nu getoetst wordt is wat er echt toe doet: de inhoud volgt
    de kaart, op elke breedte.
    """
    voor_iedere_kaart = dienstenstap.evaluate(
        """() => [...document.querySelectorAll('.service-card')].map(kaart => {
            const k = kaart.getBoundingClientRect();
            const tekst = kaart.querySelector('nldd-rich-text');
            const vakje = kaart.querySelector('nldd-checkbox');
            const t = tekst ? tekst.getBoundingClientRect() : null;
            const v = vakje ? vakje.getBoundingClientRect() : null;
            return {
                kaart: Math.round(k.width),
                tekst: t ? Math.round(t.width) : null,
                vakjeRechts: v ? Math.round(k.right - v.right) : null,
            };
        })"""
    )
    for meting in voor_iedere_kaart:
        assert meting["tekst"] > meting["kaart"] * 0.75, (
            f"de tekst is {meting['tekst']}px in een kaart van {meting['kaart']}px; "
            "de inhoud volgt de kaartbreedte niet"
        )
        assert meting["vakjeRechts"] is not None, "de kaart heeft geen aanvinkvakje"
        assert meting["vakjeRechts"] < 40, (
            f"het vakje staat {meting['vakjeRechts']}px van de rechterrand; het hoort rechts te staan"
        )


def test_de_aanvinkvakjes_staan_rechts(dienstenstap: Page) -> None:
    for top, kaarten in _kaarten_per_rij(dienstenstap).items():
        afstanden = {kaart["vakjeRechts"] for kaart in kaarten}
        assert len(afstanden) == 1, f"rij op y={top} lijnt niet gelijk uit rechts: {sorted(afstanden)}"
        assert max(afstanden) < 40, f"het vakje staat {max(afstanden)}px van de rechterrand, dat is niet rechts"


def test_de_vakjes_verspringen_niet_bij_het_aanvinken(dienstenstap: Page) -> None:
    """Aanvinken mag de kaarten niet van hoogte laten veranderen.

    Gemeld: "omdat de vereist-tekst bij de cards in komt springen alle checkboxes gek heen
    en weer". Dat kwam van twee regels die verschenen en verdwenen met de keuze: de
    configuratieregel en de regel "Vereist door ...". De eerste reserveert nu zijn ruimte
    (visibility in plaats van display), de tweede staat niet meer in de kaart maar in de
    dialoog die verschijnt als je een vereiste dienst probeert uit te zetten.

    Keycloak is de scherpste toets: die zet publieke publicatie automatisch aan en zet hem
    vast, dus er verandert van alles tegelijk.
    """
    voor = _kaarten_per_rij(dienstenstap)
    dienstenstap.locator('.service-card[data-service="keycloak"] nldd-checkbox').first.click()
    dienstenstap.locator('.service-card[data-service="keycloak"].service-card--selected').wait_for(timeout=5000)
    dienstenstap.locator(".service-card--locked-checked").first.wait_for(timeout=5000)
    na = _kaarten_per_rij(dienstenstap)

    # De structurele grond onder deze test: de regel "Vereist door ..." wordt niet meer IN
    # een kaart geschreven. Zolang dat element nergens ontstaat, kan het de hoogte ook niet
    # meer veranderen. De metingen eronder toetsen de uitkomst.
    assert dienstenstap.locator(".service-card__hint--depends").count() == 0, (
        "er staat weer een 'Vereist door'-regel in een kaart; die laat de vakjes verspringen"
    )

    assert sorted(voor) == sorted(na), "er zijn rijen bij gekomen of weggevallen"
    for top in voor:
        hoogtes_voor = [kaart["hoogte"] for kaart in voor[top]]
        hoogtes_na = [kaart["hoogte"] for kaart in na[top]]
        assert hoogtes_voor == hoogtes_na, f"de rij op y={top} is van hoogte veranderd: {hoogtes_voor} -> {hoogtes_na}"
        vakjes_voor = [kaart["vakjeTop"] for kaart in voor[top]]
        vakjes_na = [kaart["vakjeTop"] for kaart in na[top]]
        assert vakjes_voor == vakjes_na, (
            f"de vakjes van de rij op y={top} zijn verschoven: {vakjes_voor} -> {vakjes_na}"
        )


def test_een_vereiste_dienst_meldt_zich_in_een_dialoog(dienstenstap: Page) -> None:
    """Geen window.alert maar de dialoog van het thema.

    Een systeemvenster draagt de naam van de host, weet niets van de vormgeving en bevriest
    de pagina. Zo ook gemeld. Deze test faalt als de melding terugvalt op alert(): dan komt
    er een dialooggebeurtenis langs en blijft <nldd-modal-dialog> dicht.
    """
    systeemvensters: list[str] = []
    dienstenstap.on("dialog", lambda d: (systeemvensters.append(d.message), d.dismiss()))

    dienstenstap.locator('.service-card[data-service="keycloak"] nldd-checkbox').first.click()
    dienstenstap.locator(".service-card--locked-checked").first.wait_for(timeout=5000)
    dienstenstap.locator('.service-card[data-service="publish-on-web"] nldd-checkbox').first.click()

    melding = dienstenstap.locator("nldd-modal-dialog").first
    melding.wait_for(state="visible", timeout=5000)
    assert not systeemvensters, f"er kwam alsnog een systeemvenster: {systeemvensters}"
    uitleg = melding.get_attribute("supporting-text") or ""
    assert "Keycloak" in uitleg, f"de melding noemt niet wie de dienst vereist: {uitleg!r}"
