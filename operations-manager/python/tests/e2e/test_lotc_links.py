"""Elke interne link op een hertekende pagina komt ergens aan.

Het vierde van de vijf dingen die bij de vorige ronde bovenkwamen: een link naar een 404.
De voettekst wees naar ``/api`` in plaats van naar ``/docs``. Dat staat in
``base_lotc.html.j2``, dus het was op ELKE pagina fout, en toch was er niets rood.

**Waarom de suite zweeg.** Er wordt op van alles getoetst - welke dialoog een knop opent,
welk htmx-adres een blok ophaalt, welke velden een formulier heeft - maar nergens of een
``href`` ergens AANKOMT. Een link is de enige soort navigatie die niemand aanroept: hij
staat er, hij ziet er goed uit, en pas als een gebruiker klikt blijkt het niets te zijn.
Het gedragsoppervlak in ``lotc_compare_behaviour.py`` verzamelt bestemmingen wel, maar legt
ze alleen naast de oude pagina; een link die in BEIDE vormgevingen fout is - of die alleen
in de nieuwe bestaat, zoals deze - komt daar niet uit.

Daarom wordt hier elke interne link van elke omgezette pagina echt BEZOCHT, met de browser
en dus met de sessie erbij. Eerst worden alle links van alle pagina's verzameld en
ontdubbeld: de voettekst en de zijkolom staan op elke pagina, dus zonder ontdubbelen bezoek
je dezelfde twintig adressen negen keer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

#: De pagina's waarvan de links nagelopen worden. Dezelfde lijst als de visuele sweep
#: (scripts/lotc_visuele_sweep.py), zodat er niet twee lijsten uit de pas gaan lopen.
PAGINAS = [
    "/dashboard",
    "/projects",
    "/services",
    "/admin/users",
    "/admin/approvals",
    "/admin/usage",
    "/about",
    "/account",
    "/forms/wizard/start",
]

#: Links die met opzet niet bezocht worden.
#:
#: Uitloggen beeindigt de sessie en maakt daarmee elke volgende meting stuk; de
#: weergaveschakelaar en de wizard-herstart zijn ACTIES achter een GET en horen niet
#: blind afgevuurd te worden. Een lijst die te ruim is maakt de meting waardeloos, dus
#: staat de reden er per regel bij.
NEGEER = (
    "/logout",
    "/auth/logout",
    "/weergave",
    "/forms/wizard/restart",
)

#: De links die de pagina zelf oplevert, verzameld over alle pagina's heen.
LINK_JS = """
() => Array.from(document.querySelectorAll('a[href]'))
        .map(a => a.getAttribute('href'))
        .filter(h => h && h.startsWith('/') && !h.startsWith('//'))
        .map(h => h.split('#')[0])
        .filter(Boolean)
"""


def _verzamel_links(app_server: str, page: Page) -> dict[str, str]:
    """Elke interne link, met de pagina waar hij vandaan komt.

    De herkomst wordt onthouden zodat een bevinding zegt WAAR de kapotte link staat. Een
    melding "/api geeft 404" zonder pagina laat je zoeken in vijftien sjablonen.
    """
    gevonden: dict[str, str] = {}
    for pad in PAGINAS:
        antwoord = page.goto(f"{app_server}{pad}?layout=nldd", wait_until="networkidle")
        assert antwoord is not None, f"{pad} leverde geen antwoord"
        assert antwoord.status == 200, f"{pad} zelf gaf HTTP {antwoord.status}"
        for link in page.evaluate(LINK_JS):
            if not link.startswith(NEGEER):
                gevonden.setdefault(link, pad)
    return gevonden


def test_elke_interne_link_komt_ergens_aan(app_server: str, auth_page: Page) -> None:
    """Geen enkele href op een omgezette pagina levert een 404."""
    links = _verzamel_links(app_server, auth_page)
    assert links, "geen enkele link gevonden: de pagina's laden kennelijk niet"

    kapot: dict[str, str] = {}
    for link, herkomst in sorted(links.items()):
        antwoord = auth_page.goto(f"{app_server}{link}", wait_until="domcontentloaded")
        if antwoord is not None and antwoord.status >= 400:
            kapot[link] = f"HTTP {antwoord.status}, staat op {herkomst}"

    assert kapot == {}, (
        f"deze links komen nergens aan: {kapot}. "
        f"Een kapotte link maakt niets rood en is op een schermafbeelding niet te zien - "
        f"hij ziet er precies zo uit als een werkende."
    )
