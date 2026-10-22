"""Hoeveel schendingen staat een afgedwongen nonce-beleid nog in de weg?

Het portaal stuurt naast zijn beleid een ``Content-Security-Policy-Report-Only`` mee met
``script-src 'self' 'nonce-...' 'strict-dynamic'``: het beleid dat we WILLEN. Elke
schending daarvan is iets dat eerst weg moet. De grootste bron is bekend en zit niet in
deze codebase: de componentbibliotheek zet ``@click`` om in een ``onclick``-attribuut, en
een attribuut kan geen nonce dragen (``features/security-headers.md``).

Deze meting zet daar een GETAL op, per pagina, zodat het werk naar een afgedwongen beleid
een beginstand heeft. Hij faalt niet op het aantal zelf, wel als het stijgt: een nieuw
inline scriptje zonder nonce of een nieuwe inline handler komt er dan niet stil in.

Een report-endpoint hoort hier niet bij. Dat zou een nieuwe publieke schrijfroute zijn;
de browser levert dezelfde gegevens via ``securitypolicyviolation``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

import pytest
from opi.web.lotc_switch import PROJECT_TABS, project_tab_url

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

PROJECT = "test-project-detail"

#: De hoofdpagina's, de API-documentatie, plus elk tabblad van een projectpagina.
PAGINAS = [
    "/dashboard",
    "/docs",
    "/redoc",
    "/projects",
    "/services",
    "/admin/users",
    "/admin/approvals",
    "/admin/usage",
    "/forms/wizard/start",
    *(project_tab_url(PROJECT, tab) for tab in PROJECT_TABS),
]

#: De gemeten beginstand (21 oktober 2026): per pagina ``(schendingen, inline handlers)``.
#: Een pagina die hierboven komt is een REGRESSIE; lager mag altijd. Bijwerken hoort bij
#: werk dat schendingen wegneemt, en dan naar beneden. Een uitzondering hoort hier met
#: reden bij te staan: ``/admin/users`` ging van 12 naar 14 handlers doordat elke
#: gebruikersrij er een knop "Sessies beëindigen" bij kreeg (RC-287). ``/redoc`` staat op 1:
#: zie ENFORCE_UITZONDERINGEN hierna.
BASELINE: dict[str, tuple[int, int]] = {
    "/dashboard": (3, 10),
    "/docs": (0, 0),
    "/redoc": (1, 0),
    "/projects": (3, 10),
    "/services": (3, 36),
    "/admin/users": (3, 14),
    "/admin/approvals": (3, 12),
    "/admin/usage": (3, 10),
    "/forms/wizard/start": (3, 10),
    "/projects/test-project-detail/details": (3, 28),
    "/projects/test-project-detail/team": (3, 26),
    "/projects/test-project-detail/componenten": (3, 32),
    "/projects/test-project-detail/services": (3, 28),
    "/projects/test-project-detail/services-info": (3, 26),
    "/projects/test-project-detail/deployments": (3, 37),
    "/projects/test-project-detail/metrics": (3, 26),
    "/projects/test-project-detail/backups": (3, 28),
    "/projects/test-project-detail/taken": (3, 25),
}

#: Inline event-handlers in de DOM. Die staan NIET in de schendingen hierboven: een
#: ``onclick`` wordt pas gecompileerd als er geklikt wordt, dus een paginabezoek meldt hem
#: niet. Zonder dit getal lijkt het beleid dichter bij afdwingbaar dan het is, want precies
#: deze handlers zijn de reden dat 'unsafe-inline' nog in style-src en script-src staat.
HANDLERS_JS = """
() => Array.from(document.querySelectorAll('*')).filter(
    (el) => Array.from(el.attributes).some((a) => a.name.startsWith('on'))
).length
"""

#: Verzamelt de schendingen van het Report-Only-beleid. Op ``document`` en niet op
#: ``window``: alleen daar vuurt het event. ``disposition`` scheidt report-only van het
#: afgedwongen beleid, zodat een echte blokkade niet als meting wegvalt.
VERZAMEL_JS = """
window.__cspSchendingen = [];
document.addEventListener('securitypolicyviolation', (e) => {
    window.__cspSchendingen.push({
        disposition: e.disposition,
        directive: e.effectiveDirective || e.violatedDirective,
        blocked: e.blockedURI,
        bron: e.sourceFile ? e.sourceFile + ':' + e.lineNumber : '',
    });
});
"""


class Meting(NamedTuple):
    """Wat een paginabezoek oplevert. ``report`` is het beleid dat we willen, ``enforce``
    het beleid dat nu geldt: een schending daar breekt de pagina echt."""

    report: list[dict[str, str]]
    enforce: list[dict[str, str]]
    handlers: int
    externe_urls: list[str]


def _meet(page: Page, app_server: str, pad: str) -> Meting:
    # ``data:`` en ``blob:`` zijn geen externe host: die gaan niet over de lijn.
    externe_urls: list[str] = []
    page.on(
        "request",
        lambda verzoek: (
            None if verzoek.url.startswith((app_server, "data:", "blob:")) else externe_urls.append(verzoek.url)
        ),
    )
    page.add_init_script(VERZAMEL_JS)
    page.goto(f"{app_server}{pad}")
    page.wait_for_load_state("networkidle")
    schendingen: list[dict[str, str]] = page.evaluate("() => window.__cspSchendingen || []")
    handlers: int = page.evaluate(HANDLERS_JS)
    return Meting(
        report=[s for s in schendingen if s["disposition"] == "report"],
        enforce=[s for s in schendingen if s["disposition"] == "enforce"],
        handlers=handlers,
        externe_urls=externe_urls,
    )


@pytest.mark.parametrize("pad", PAGINAS)
def test_de_meting_stijgt_niet(auth_page: Page, app_server: str, pad: str) -> None:
    meting = _meet(auth_page, app_server, pad)
    schendingen, handlers = meting.report, meting.handlers

    per_directive: dict[str, int] = {}
    for schending in schendingen:
        per_directive[schending["directive"]] = per_directive.get(schending["directive"], 0) + 1

    print(f"\n{pad:44} {len(schendingen):3} schendingen {per_directive!s:36} {handlers:3} inline handlers")
    for schending in schendingen:
        print(f"    {schending['directive']:16} {schending['blocked']}  {schending['bron']}")

    grens_schendingen, grens_handlers = BASELINE[pad]
    assert len(schendingen) <= grens_schendingen, (
        f"{pad}: {len(schendingen)} schendingen, beginstand is {grens_schendingen}. Per directive: {per_directive}"
    )
    assert handlers <= grens_handlers, f"{pad}: {handlers} inline handlers, beginstand is {grens_handlers}"


#: Schendingen van het AFGEDWONGEN beleid die blijven staan, per pagina de directive en de
#: geblokkeerde bron. Alleen ``/redoc``, en beide komen uit de redoc-bundel zelf: hij maakt
#: een web worker uit een ``blob:``-URL, en hij laadt zijn eigen logo van ``cdn.redoc.ly``.
#: Dat is zo op main, redoc rendert ervan onverstoord, en het beleid breder maken om een
#: blob-worker en een extern logo toe te staan is precies wat we niet willen.
ENFORCE_UITZONDERINGEN: dict[str, set[tuple[str, str]]] = {
    "/redoc": {
        ("worker-src", "blob"),
        ("img-src", "https://cdn.redoc.ly/redoc/logo-mini.svg"),
    },
}

#: Externe origins die een pagina mag aanspreken. Alleen ``/redoc``: de bundel laadt zijn
#: eigen logo van ``cdn.redoc.ly``. Het afgedwongen beleid blokkeert die request al
#: (``img-src 'self' data:``), dus er gaat niets naar buiten; de request staat wel in de
#: lijst omdat de browser hem aankondigt voordat het beleid hem tegenhoudt.
EXTERNE_UITZONDERINGEN: dict[str, set[str]] = {
    "/redoc": {"https://cdn.redoc.ly"},
}


@pytest.mark.parametrize("pad", PAGINAS)
def test_het_afgedwongen_beleid_breekt_niets(auth_page: Page, app_server: str, pad: str) -> None:
    """Breekt het beleid dat NU geldt iets op de pagina?

    ``test_de_meting_stijgt_niet`` telt alleen de report-only-schendingen: die meten wat een
    strikter beleid in de weg staat, en kosten vandaag niets. Deze toets gaat over het
    afgedwongen beleid, waar een schending wel een stuk pagina kost: een script dat niet
    draait, een stijl die niet laadt, een bibliotheek die een eigen host nodig heeft.
    """
    meting = _meet(auth_page, app_server, pad)

    toegestaan = ENFORCE_UITZONDERINGEN.get(pad, set())
    onverwacht = [s for s in meting.enforce if (s["directive"], s["blocked"]) not in toegestaan]

    print(f"\n{pad:44} {len(meting.enforce):3} enforce-schendingen, {len(meting.externe_urls):2} externe requests")
    for schending in meting.enforce:
        print(f"    {schending['directive']:16} {schending['blocked']}  {schending['bron']}")
    for url in meting.externe_urls:
        print(f"    request        {url}")

    assert not onverwacht, f"{pad}: het afgedwongen beleid blokkeert iets op de pagina: {onverwacht}"

    # startswith op een leeg tuple is altijd False, dus zonder uitzondering valt elke
    # externe request door.
    externe_origins = tuple(EXTERNE_UITZONDERINGEN.get(pad, set()))
    buiten = [url for url in meting.externe_urls if not url.startswith(externe_origins)]
    assert not buiten, f"{pad}: de pagina doet een request naar een andere origin: {buiten}"


def test_een_ingeswapt_fragmentscript_komt_er_niet_bij(auth_page: Page, app_server: str) -> None:
    """Het tabblad Metingen swapt een fragment in met een inline script erin.

    Dat script draagt de nonce van ZIJN verzoek, niet die van het document: een nonce geldt
    een antwoord, en de browser toetst tegen het beleid van de pagina. Htmx plaatst het
    script met JavaScript en niet via de parser, en daar is ``'strict-dynamic'`` voor, dus de
    stale nonce maakt niets uit. Dit meet dat, want de aanname is het enige alternatief.
    """
    auth_page.add_init_script(VERZAMEL_JS)
    auth_page.goto(f"{app_server}{project_tab_url(PROJECT, 'metrics')}")
    # Het ingeswapte script haalt Chart.js zelf op met createElement('script'); de plugin is
    # de laatste van de drie, dus zijn registratie zegt dat het script helemaal gedraaid is.
    auth_page.wait_for_function("() => !!window.Chart?.registry.plugins.items.annotation")

    schendingen: list[dict[str, str]] = auth_page.evaluate("() => window.__cspSchendingen || []")
    inline = [s for s in schendingen if s["disposition"] == "report" and s["blocked"] == "inline"]
    assert not inline, f"een inline script uit een fragment wordt geblokkeerd: {inline}"
