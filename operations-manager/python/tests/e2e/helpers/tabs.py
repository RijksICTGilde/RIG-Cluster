"""Een tabblad van de projectpagina openen, in welke weergave dan ook.

De twee weergaven doen dit fundamenteel anders, en dat is geen detail maar een van de
weinige echte gedragsverschillen van de omzetting:

- De bestaande pagina zet ALLE tabbladen in een document en wisselt ze in de browser met
  ``switchTab('deployments')``.
- De nieuwe pagina geeft elk tabblad een eigen URL (``?tab=deployments``). Daardoor is een
  tab deelbaar, werkt de terugknop, en doet de pagina het zonder JavaScript.

Tests die ``switchTab`` aanroepen falen op de nieuwe weergave met "switchTab is not
defined". Dat is geen storing in de applicatie: de functie hoort daar niet te bestaan.
Deze helper vraagt de PAGINA wat ze kan, in plaats van aan te nemen wat er is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page


def open_tab(page: Page, tab: str) -> None:
    """Open het tabblad ``tab`` op de projectdetailpagina die al open staat.

    Gebruikt ``switchTab()`` als die er is, en navigeert anders naar ``?tab=<naam>`` op
    de pagina waar de browser al staat. Zo hoeft een test niet te weten welke weergave
    hij meet.
    """
    heeft_switch = page.evaluate("() => typeof window.switchTab === 'function'")
    if heeft_switch:
        page.evaluate(f"switchTab({tab!r})")
        return

    from urllib.parse import urlencode, urlparse, urlunparse

    stukken = urlparse(page.url)
    query = dict(pair.split("=", 1) for pair in stukken.query.split("&") if "=" in pair)
    query["tab"] = tab
    page.goto(urlunparse(stukken._replace(query=urlencode(query))))
    page.wait_for_load_state("networkidle")
