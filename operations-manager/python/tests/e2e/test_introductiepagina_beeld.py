"""De introductiepagina in een echte browser: rendert hij, en staat er niets leeg?

Waarom dit naast tests/test_introductiepagina.py bestaat, dat dezelfde pagina al toetst:
die leest de HTML. Op deze bouwlijn is dat aantoonbaar niet genoeg - de dienstkaarten zijn
webcomponenten die pas iets voorstellen nadat nldd.js ze heeft opgebouwd, en een blok dat
in de HTML compleet is kan in het beeld nul hoog zijn. Dat is hier al twee keer gebeurd
(een keuzelijst zonder opties, een foutmelding met display:none), en geen enkele
HTML-assertie ving het.

Deze pagina is bovendien het EERSTE dat iemand van ZAD ziet, en de enige bezoeker die hem
echt nodig heeft is niet ingelogd. Daarom draait alles hier op de ANONIEME ``page``-fixture
en niet op ``auth_page``: een pagina die alleen werkt als je al binnen bent, mist zijn hele
publiek, en dat merk je nooit als je zelf ingelogd test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from opi.web.lotc_switch import build_lotc_introductie

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

SCREENSHOT_DIR = "tests/e2e/screenshots/lotc"

#: Elke <nldd-icon> op de pagina, ook binnen de schaduwboom van een ander component, met
#: de hoeveelheid tekening erin. Zelfde probe als tests/e2e/test_lotc_iconen_tekenen.py:
#: een component met een ``icon=``-attribuut tekent zelf niets, het zet er een
#: <nldd-icon> in, dus meten op de buitenkant meldt alles als leeg.
ICONEN_PROBE = """
() => {
  const uit = [];
  const loop = (wortel) => {
    wortel.querySelectorAll('nldd-icon').forEach(el => {
      const svg = el.shadowRoot && el.shadowRoot.querySelector('svg');
      uit.push([el.getAttribute('name') || '(zonder naam)', svg ? svg.innerHTML.trim().length : 0]);
    });
    wortel.querySelectorAll('*').forEach(el => { if (el.shadowRoot) loop(el.shadowRoot); });
  };
  loop(document);
  return uit;
}
"""

#: Per dienstkaart: de titel, en of hij ECHT ruimte inneemt. Een kaart met tekst in de HTML
#: en hoogte 0 is precies de fout die een HTML-assertie niet ziet.
KAARTEN_PROBE = """
() => Array.from(document.querySelectorAll('[data-lotc-component="catalog-card"]')).map(el => {
  const doos = el.getBoundingClientRect();
  const titel = el.querySelector('.lotc-catalog-title');
  return {
    titel: titel ? titel.textContent.trim() : '',
    tekst: el.textContent.trim().length,
    hoogte: doos.height,
    breedte: doos.width,
  };
})
"""


def _wacht_op_nldd(page: Page) -> None:
    """Wacht tot de browser elk custom element heeft opgebouwd.

    Zonder dit leg je ongestileerde tekst vast en meet je hoogtes die nergens op slaan.
    """
    page.wait_for_load_state("networkidle")
    page.wait_for_function("() => document.querySelectorAll('*:not(:defined)').length === 0", timeout=15000)


def _open(app_server: str, page: Page) -> None:
    page.set_viewport_size({"width": 1440, "height": 900})
    response = page.goto(f"{app_server}/introductie")
    assert response is not None
    assert response.ok, f"de introductiepagina komt niet door zonder sessie: {response.status}"
    _wacht_op_nldd(page)


def test_de_pagina_opent_zonder_sessie_en_toont_zijn_kop(app_server: str, page: Page) -> None:
    """Anoniem openen, en dan staat de kop er ook echt zichtbaar."""
    _open(app_server, page)

    kop = page.locator("h1").first
    assert kop.is_visible()
    assert kop.bounding_box()["height"] > 0

    page.screenshot(path=f"{SCREENSHOT_DIR}/introductie.png", full_page=True)


def test_elke_dienst_uit_de_catalogus_staat_er_als_gevulde_kaart(app_server: str, page: Page) -> None:
    """Evenveel kaarten als diensten, elk met een titel, tekst en echte afmetingen."""
    context = build_lotc_introductie(None)
    verwacht = [d["label"] for d in context["diensten_zelf"]] + [d["label"] for d in context["diensten_achtergrond"]]
    assert verwacht, "de catalogus levert niets; deze test zou gratis groen zijn"

    _open(app_server, page)
    kaarten = page.evaluate(KAARTEN_PROBE)

    assert [k["titel"] for k in kaarten] == verwacht, "de kaarten op het scherm zijn niet die van de catalogus"

    leeg = [k["titel"] or "(zonder titel)" for k in kaarten if k["hoogte"] <= 0 or k["breedte"] <= 0 or not k["tekst"]]
    assert not leeg, f"deze dienstkaarten nemen geen ruimte in of zijn leeg: {leeg}"


def test_er_staat_geen_lege_plek_waar_een_icoon_hoort(app_server: str, page: Page) -> None:
    """Een iconnaam die de bundel niet kent rendert als niets, zonder foutmelding."""
    _open(app_server, page)

    getekend = page.evaluate(ICONEN_PROBE)
    assert getekend, "geen enkel icoon gevonden; de probe meet dan niets"

    leeg = sorted({naam for naam, lengte in getekend if not lengte})
    assert not leeg, f"deze iconen tekenen niets op de introductiepagina: {leeg}"


def test_geen_enkel_paneel_blijft_leeg(app_server: str, page: Page) -> None:
    """Elk paneel heeft een kop EN inhoud, en neemt ruimte in.

    Een macro die niets teruggeeft levert een kaart met alleen een titel op. Dat ziet er in
    de HTML uit als een compleet blok en op het scherm als een streep.
    """
    _open(app_server, page)

    panelen = page.evaluate("""
        () => Array.from(document.querySelectorAll('nldd-card, [data-lotc-component="card"]')).map(el => ({
            tekst: el.textContent.trim().length,
            hoogte: el.getBoundingClientRect().height,
        }))
    """)
    assert panelen, "geen enkele kaart gevonden; de probe meet dan niets"

    kaal = [p for p in panelen if p["hoogte"] < 20 or p["tekst"] < 20]
    assert not kaal, f"{len(kaal)} kaart(en) zijn leeg of plat: {kaal}"


def test_de_twee_tellingen_nemen_ook_echt_breedte_in(app_server: str, page: Page) -> None:
    """Een c-metric in een cluster wordt 0 breed; hier hoort hij in een auto-grid.

    Zo stond hij er eerst: 155 pixels hoog, breedte 0, in de HTML compleet, en op het
    scherm een lege strook onder de tekst. Dezelfde val als <c-stack> in een tabelcel, en
    alleen zichtbaar als je de BREEDTE opmeet.
    """
    _open(app_server, page)

    metingen = page.evaluate("""
        () => Array.from(document.querySelectorAll('[data-lotc-component="metric"]')).map(el => {
            const doos = el.getBoundingClientRect();
            return {tekst: el.textContent.trim(), hoogte: doos.height, breedte: doos.width};
        })
    """)
    assert len(metingen) == 2, f"twee tellingen verwacht, gevonden: {metingen}"
    for meting in metingen:
        assert meting["breedte"] > 100, f"telling is plat: {meting}"
        assert meting["hoogte"] > 20, f"telling is plat: {meting}"


def test_een_anonieme_bezoeker_krijgt_inloggen_en_geen_uitloggen(app_server: str, page: Page) -> None:
    """Rechtsboven hoort de weg NAAR binnen te staan, niet die naar buiten.

    De schil tekende het accountmenu onvoorwaardelijk, dus een nieuwkomer kreeg "Account"
    met "Profiel" en "Uitloggen" - drie bestemmingen die hem alle drie op het inlogscherm
    zetten, terwijl "Inloggen" er niet stond.
    """
    _open(app_server, page)

    balk = page.evaluate(
        "() => Array.from(document.querySelectorAll('nldd-menu-bar-item')).map(el => el.getAttribute('text'))"
    )
    assert "Inloggen" in balk, f"geen weg naar binnen in de hulpbalk: {balk}"
    assert "Uitloggen" not in page.content()
    assert 'href="/account"' not in page.content()


def test_wie_wel_is_ingelogd_houdt_zijn_accountmenu(app_server: str, auth_page: Page) -> None:
    """De tegenproef bij de test hierboven: de schil mag niet voor iedereen kaal worden."""
    auth_page.goto(f"{app_server}/introductie")
    _wacht_op_nldd(auth_page)

    assert 'href="/auth/logout"' in auth_page.content()
    assert 'href="/account"' in auth_page.content()


def test_de_verwijzingen_naar_de_cli_en_de_actions_staan_erop(app_server: str, page: Page) -> None:
    """zadctl en de Actions horen genoemd te worden, met een link die zonder rechten werkt.

    Naar de repositories en niet naar /cli en /actions: die twee dragen ``@requires_sso``,
    dus vanaf hier zou je op het inlogscherm belanden.
    """
    _open(app_server, page)

    assert page.locator("text=zadctl").first.is_visible()
    for repo in ("RijksICTGilde/zad-cli", "RijksICTGilde/zad-actions"):
        link = page.locator(f'a[href="https://github.com/{repo}"]')
        assert link.count() == 1, f"verwijzing naar {repo} ontbreekt"
        assert link.first.is_visible()
