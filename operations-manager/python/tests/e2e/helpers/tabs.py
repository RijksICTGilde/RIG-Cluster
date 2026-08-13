"""Een tabblad van de projectpagina openen, in welke weergave dan ook.

De twee weergaven doen dit fundamenteel anders, en dat is geen detail maar een van de
weinige echte gedragsverschillen van de omzetting:

- De bestaande pagina zet ALLE tabbladen in een document en wisselt ze in de browser met
  ``switchTab('deployments')``.
- De nieuwe pagina geeft elk tabblad een eigen PAD (``/projects/<naam>/deployments``).
  Daardoor is een tab deelbaar, werkt de terugknop, en doet de pagina het zonder
  JavaScript. Sinds RC-76 is dat een pad en geen ``?tab=``-parameter, en er is bewust geen
  doorverwijzing van de oude vorm: die heeft nooit buiten deze applicatie geleefd.

Tests die ``switchTab`` aanroepen falen op de nieuwe weergave met "switchTab is not
defined". Dat is geen storing in de applicatie: de functie hoort daar niet te bestaan.
Deze helper vraagt de PAGINA wat ze kan, in plaats van aan te nemen wat er is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlparse

from opi.web.lotc_switch import project_tab_url

if TYPE_CHECKING:
    from playwright.sync_api import Page


def open_tab(page: Page, tab: str) -> None:
    """Open het tabblad ``tab`` op de projectdetailpagina die al open staat.

    Gebruikt ``switchTab()`` als die er is, en navigeert anders naar het PAD van dat
    tabblad. Zo hoeft een test niet te weten welke weergave hij meet.

    De projectnaam komt uit het pad van de huidige pagina: dat is het TWEEDE segment
    (``/projects/<naam>/<tabblad>``, sinds RC-93 staat de naam voorop). Niet het laatste -
    er kan een deployment achter staan (``/projects/<naam>/deployments/<deployment>``), en
    dan zou het laatste segment de deployment zijn.
    """
    heeft_switch = page.evaluate("() => typeof window.switchTab === 'function'")
    if heeft_switch:
        page.evaluate(f"switchTab({tab!r})")
        return

    stukken = urlparse(page.url)
    segmenten = stukken.path.strip("/").split("/")
    projectnaam = segmenten[1] if len(segmenten) > 1 else ""
    page.goto(f"{stukken.scheme}://{stukken.netloc}{project_tab_url(projectnaam, tab)}")
    page.wait_for_load_state("networkidle")
