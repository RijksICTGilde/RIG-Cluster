"""Tekent elk icoon ook echt iets, in een echte browser.

Waarom dit naast tests/test_lotc_icon_mapping.py bestaat, dat dezelfde vraag lijkt te
stellen: die poort leest een LIJST (opi/web/nldd_iconen.py, de namen uit de geleverde
bundel) en vergelijkt daarmee. Zo'n lijst is een aanname over de browser, en juist die
aanname is hier twee keer misgegaan - eerst met icons.json (de bedoelde woordenschat, 56
namen die nergens getekend worden) en daarna met de aliaslaag die een bestaande naam naar
een niet-bestaande herschrijft.

Deze test kijkt niet in een lijst maar in de browser: een echte <nldd-icon> per naam en
de vraag of er iets in het SVG staat. Dat is de enige meting die geen aanname bevat, en
daarmee ook de poort ONDER de andere poort - zodra de lijst en de werkelijkheid uit
elkaar lopen, valt deze om en de andere niet.

Twee vragen, twee tests:

1. Tekent elke naam die de applicatie kan produceren? Dat zijn de namen uit de
   sjablonen, uit de Python-bron, uit de dienstdefinities en uit de presets, allemaal
   door dezelfde vertaling als tijdens het renderen.
2. Staat er op een DRAAIENDE pagina nog een lege plek? Daar begon RC-94 mee: elke poort
   groen, en de gebruiker zag lege plekken. De oorzaak was dat de sjablonen de vertaling
   op twaalf plekken niet uitvoerden, en dat zie je alleen op de pagina zelf.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from opi.web.lotc_switch import project_tab_url
from opi.web.navigation_lotc import to_nldd_icon
from tests.test_lotc_icon_mapping import (
    _gerenderde_naam,
    _iconen_als_macroargument,
    _iconen_in_lotc_templates,
    _iconen_in_python,
    _icons_used_by_services,
    alle_presets,
)

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

PROJECT = "test-project-detail"

#: Zet een <nldd-icon> per naam neer en meld per naam hoeveel er in het SVG staat.
#: Het wachten is nodig: het element haalt zijn pad op nadat het is aangehaakt.
TEKEN_PROBE = """
(namen) => {
  const bak = document.createElement('div');
  bak.id = 'icoonproef';
  document.body.appendChild(bak);
  bak.innerHTML = namen.map(n => `<nldd-icon name="${n}"></nldd-icon>`).join('');
  return new Promise(klaar => setTimeout(() => {
    const uit = {};
    bak.querySelectorAll('nldd-icon').forEach(el => {
      const svg = el.shadowRoot && el.shadowRoot.querySelector('svg');
      uit[el.getAttribute('name')] = svg ? svg.innerHTML.trim().length : 0;
    });
    bak.remove();
    klaar(uit);
  }, 500));
}
"""

#: Elke <nldd-icon> op de pagina, ook die binnen een schaduwboom van een ander component.
#: Een knop en een cel dragen zelf een ``icon=``-attribuut maar tekenen niets: ze zetten
#: er een <nldd-icon> in. Meten op die knop zou dus elke knop als leeg melden.
PAGINA_PROBE = """
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


def _wacht_op_nldd(page: Page) -> None:
    page.wait_for_load_state("networkidle")
    page.wait_for_function(
        "() => document.querySelectorAll('*:not(:defined)').length === 0",
        timeout=15000,
    )


def _namen_die_de_applicatie_kan_tonen() -> list[str]:
    """Elke iconnaam die op een pagina terecht kan komen, na dezelfde vertaling.

    De vier bronnen naast elkaar, want ze lopen langs verschillende wegen: een
    letterlijke naam in een sjabloon gaat NIET door ``to_nldd_icon`` (die staat er al in
    NLDD-vorm), en alles wat uit gegevens of uit Python komt WEL. Daarna nog een keer
    door de aliaslaag van LOTC, want dat is de naam die de browser opzoekt.
    """
    namen = set(_iconen_in_lotc_templates()) | set(_iconen_als_macroargument())
    vertaald = {
        to_nldd_icon(naam)
        for naam in set(_iconen_in_python()) | _icons_used_by_services() | {p.icon for p in alle_presets()}
    }
    return sorted({_gerenderde_naam(naam) for naam in namen | vertaald if naam})


def test_elke_iconnaam_van_de_applicatie_tekent_in_de_browser(app_server: str, page: Page) -> None:
    """Geen enkele naam die wij kunnen tonen levert een leeg SVG op."""
    namen = _namen_die_de_applicatie_kan_tonen()
    assert len(namen) > 40, f"te weinig namen verzameld ({len(namen)}); deze test zou gratis groen zijn"

    page.goto(f"{app_server}/lotc/bg/dashboard")
    _wacht_op_nldd(page)

    getekend: dict[str, int] = page.evaluate(TEKEN_PROBE, namen)
    leeg = sorted(naam for naam, lengte in getekend.items() if not lengte)
    assert not leeg, (
        f"deze iconnamen tekenen niets in de browser (een lege plek zonder foutmelding): {leeg}. "
        "Kies een naam die NLDD levert of leg de afbeelding in ROOS_TO_NLDD_ICONS."
    )


#: De pagina's die de gebruiker noemde toen hij lege plekken zag. Het projecttabblad in
#: zijn drie vormen, want de diensten en de deploymentacties staan elk op een eigen
#: tabblad en juist daar renderden de iconen uit gegevens.
PAGINAS = [
    "/lotc/bg/dashboard",
    # De wizard herstart en toont zijn eerste stap; de kop van elke stap draagt een
    # iconnaam uit de sectiedefinitie, en dat is een van de wegen die niet vertaalde.
    "/forms/wizard/restart",
    "/lotc/bg/services",
    "/services",
    project_tab_url(PROJECT, "project"),
    project_tab_url(PROJECT, "componenten"),
    project_tab_url(PROJECT, "services"),
    project_tab_url(PROJECT, "deployments"),
]


@pytest.mark.parametrize("pad", PAGINAS)
def test_geen_lege_plek_op_een_draaiende_pagina(app_server: str, auth_page: Page, pad: str) -> None:
    """Elk icoon dat de pagina neerzet, tekent ook iets.

    Dit is de toets die de gebruiker deed en die alle andere poorten misten: niet "kent
    NLDD deze naam" maar "staat er iets op het scherm".
    """
    antwoord = auth_page.goto(f"{app_server}{pad}")
    assert antwoord is not None, f"{pad} gaf geen antwoord"
    assert antwoord.ok, f"{pad} gaf {antwoord.status}"
    _wacht_op_nldd(auth_page)

    iconen: list[list] = auth_page.evaluate(PAGINA_PROBE)
    assert iconen, f"{pad} bevat geen enkel icoon; deze test zou gratis groen zijn"

    leeg = sorted({naam for naam, lengte in iconen if not lengte})
    assert not leeg, f"{pad} toont lege plekken waar een icoon hoort: {leeg}"
