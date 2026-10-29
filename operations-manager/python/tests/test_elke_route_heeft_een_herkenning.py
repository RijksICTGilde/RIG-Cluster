"""Elke geregistreerde route noemt hoe de aanroeper wordt herkend.

De middleware geeft elk pad onder /api/ meteen door naar de handler en leest voor de rest
_requires_sso met False als default. Deze test dwingt af dat elke route precies een
herkenning draagt, in PUBLIEKE_ROUTES staat met reden, of in HANDGESCHREVEN_CONTROLES
staat met de reden dat geen markering past en de sporen van de controle in de handler.
Dit is herkenning, niet rol: dat je herkend wordt, niet dat je mag wat je vraagt (dat is
fase 1).
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from fastapi import FastAPI
from opi.api.route_herkenning import WEBSOCKET, RouteHerkenning, route_herkenningen
from opi.core.auth_decorators import requires_sso
from opi.server import app

#: Routes die bewust zonder herkenning bereikbaar zijn, met de reden. Deze lijst is het punt
#: waar een reviewer naar kijkt: wat hier bij komt, komt op straat te liggen.
PUBLIEKE_ROUTES: dict[tuple[str, str], str] = {
    # Infrastructuur. Deze paden staan ook in SKIP_AUTH_PREFIXES van de middleware, zodat er
    # een lijst is om te reviewen in plaats van twee.
    ("GET", "/health"): "healthcheck voor kubernetes, antwoordt zonder iets op te zoeken",
    ("GET", "/healthz"): "healthcheck voor kubernetes, antwoordt zonder iets op te zoeken",
    ("GET", "/readyz"): "readinesscheck voor kubernetes, antwoordt zonder iets op te zoeken",
    ("GET", "/version"): "de versie van het image, nodig om een uitrol te kunnen vaststellen",
    ("GET", "/static/lotc/{rel:path}"): "statische bestanden van de componentbibliotheek",
    ("GET", "/favicon.ico"): "statisch bestand dat elke browser ongevraagd opvraagt",
    ("GET", "/.well-known/security.txt"): "hoort publiek te zijn, dat is het doel van RFC 9116",
    # Het document. Zonder dit is de API niet te gebruiken, en het beschrijft de beveiliging
    # in plaats van die te omzeilen.
    ("GET", "/openapi.json"): "de API-beschrijving zelf",
    # De inlogflow. Die moet zonder sessie bereikbaar zijn, want hij maakt de sessie.
    ("GET", "/auth/login"): "begin van de inlogflow, per definitie zonder sessie",
    ("GET", "/auth/callback"): "terugkeer van Keycloak, legitimeert zich met de autorisatiecode",
    ("GET", "/auth/logout"): "uitloggen hoort te werken met een sessie die al verlopen is",
    # De uitnodigingsflow. De sleutel in het pad IS de herkenning: wie hem heeft, is
    # uitgenodigd. Een sessie bestaat hier nog niet, want de gebruiker wordt juist aangemaakt.
    ("GET", "/invite/{key}"): "de uitnodigingssleutel in het pad is de herkenning",
    ("GET", "/invite/{key}/error"): "foutpagina van de uitnodigingsflow",
    ("GET", "/invite/{key}/register"): "registratieformulier van de uitnodigingsflow",
    ("POST", "/invite/{key}/register"): "verwerkt het registratieformulier, sleutel in het pad",
    ("GET", "/invite/{key}/idp/{idp_alias}"): "doorverwijzing naar een identityprovider",
    ("GET", "/invite/{key}/sso"): "begin van de SSO-variant van de uitnodigingsflow",
    ("GET", "/invite/{key}/sso/callback"): "terugkeer van de SSO-variant",
    ("GET", "/invite/{key}/success"): "slotpagina van de uitnodigingsflow",
    # Publieke pagina's. Vaste inhoud, zonder iets uit een project.
    ("GET", "/"): "de voorpagina, met de inlogknop",
    ("GET", "/about"): "vaste inhoud",
    ("GET", "/introductie"): "vaste inhoud, bewust zonder sessie (tests/test_introductiepagina.py)",
    ("GET", "/eigen-domein"): "vaste uitleg over domeinnamen",
    ("GET", "/weergave"): "vaste uitleg over de weergave",
    ("GET", "/security"): "het beveiligingsbeleid; wie een kwetsbaarheid meldt is meestal niet ingelogd",
    ("GET", "/permission-denied"): "de pagina waar de middleware naartoe stuurt bij een weigering",
    # De proefopstelling van de componentbibliotheek. De router zegt zelf dat hij publiek is
    # en dat er daarom alleen verzonnen waarden in mogen (opi/web/lotc_router.py).
    ("GET", "/lotc/"): "proefopstelling van de componentbibliotheek, verzonnen waarden",
    ("GET", "/lotc/formulier"): "proefopstelling van de componentbibliotheek, verzonnen waarden",
    ("GET", "/lotc/bg/{slug}"): "proefopstelling van de componentbibliotheek, verzonnen waarden",
    ("GET", "/lotc/pagina/{slug:path}"): "proefopstelling van de componentbibliotheek, verzonnen waarden",
    # Blinde doorverwijzingen van oude adressen. Ze zoeken niets op en geven dus niets prijs:
    # het antwoord is voor elke naam hetzelfde, ook voor een project dat niet bestaat. De
    # pagina achter het nieuwe adres doet de autorisatie. Zie project_tab_oude_vorm.
    ("GET", "/projects/new"): "blinde doorverwijzing naar de wizard, zoekt niets op",
    ("GET", "/projects/details/{project_name}"): "blinde doorverwijzing naar het nieuwe adres",
    ("GET", "/projects/team/{project_name}"): "blinde doorverwijzing naar het nieuwe adres",
    ("GET", "/projects/componenten/{project_name}"): "blinde doorverwijzing naar het nieuwe adres",
    ("GET", "/projects/services/{project_name}"): "blinde doorverwijzing naar het nieuwe adres",
    ("GET", "/projects/services-info/{project_name}"): "blinde doorverwijzing naar het nieuwe adres",
    ("GET", "/projects/deployments/{project_name}"): "blinde doorverwijzing naar het nieuwe adres",
    ("GET", "/projects/metrics/{project_name}"): "blinde doorverwijzing naar het nieuwe adres",
    ("GET", "/projects/taken/{project_name}"): "blinde doorverwijzing naar het nieuwe adres",
    ("GET", "/projects/deployments/{project_name}/{deployment_name}"): "blinde doorverwijzing",
    ("GET", "/projects/metrics/{project_name}/{deployment_name}"): "blinde doorverwijzing",
    # Metadata van het platform, niet van een project. De endpoints zeggen zelf dat ze
    # projectonafhankelijk zijn: ze lezen de dienstencatalogus uit de code, niet uit een
    # projectbestand.
    ("GET", "/api/v2/services"): "de dienstencatalogus van het platform, projectonafhankelijk",
    ("GET", "/api/v2/services/{service_name}"): "beschrijving van een platformdienst, projectonafhankelijk",
}

#: Routes waar geen markering op past: per route de reden waarom niet, plus sporen,
#: tekstfragmenten die letterlijk in de bron van de handler moeten staan.
HANDGESCHREVEN_CONTROLES: dict[tuple[str, str], tuple[str, tuple[str, ...]]] = {
    ("GET", "/auth/user"): (
        "401-JSON in plaats van een omleiding naar de inlogpagina; eigen sessiecontrole in de handler (auth_routes.py)",
        ('request.session.get("user")',),
    ),
    ("GET", "/api/logs/pods/{project_name}"): (
        "de middleware leest geen markering onder /api/ (zie "
        "test_geen_api_route_leunt_op_de_sso_markering); eigen sessiecontrole in de handler (logs_router.py)",
        ("get_user(request)", "is_user_authorized_for_project("),
    ),
    (WEBSOCKET, "/api/logs/stream/{project_name}"): (
        "een websocket krijgt geen omleiding of afwijzing van de middleware; "
        "eigen sessiecontrole op de cookie (logs_websocket_router.py)",
        ("valid_session_user(", "is_user_authorized_for_project("),
    ),
    ("GET", "/api/tasks"): (
        "herkenning alleen via de X-API-Key-header, gehandhaafd met de handeling van de route (task_router.py)",
        ("handhaaf(",),
    ),
    ("GET", "/api/tasks/{task_id}"): (
        "herkenning alleen via de X-API-Key-header, gehandhaafd via _validate_task_access (task_router.py)",
        ("_validate_task_access(",),
    ),
    ("POST", "/api/tasks/{task_id}/:cancel"): (
        "herkenning alleen via de X-API-Key-header, gehandhaafd via _validate_task_access (task_router.py)",
        ("_validate_task_access(",),
    ),
    ("GET", "/api/sleep-mode/{project_name}/{deployment_name}/status"): (
        "twee bewijsmiddelen zijn legitiem (het wake-token van de waker of een token van een lid); "
        "_presented_token laat het eerste door naar flow en handhaaft het tweede",
        ("_presented_token(",),
    ),
    ("POST", "/api/sleep-mode/{project_name}/{deployment_name}/wake"): (
        "twee bewijsmiddelen zijn legitiem (het wake-token van de waker of een token van een lid); "
        "_presented_token laat het eerste door naar flow en handhaaft het tweede",
        ("_presented_token(",),
    ),
}


def ontbrekende_herkenning(routes: list[RouteHerkenning]) -> list[str]:
    """De routes die geen herkenning noemen en niet als publiek zijn verklaard.

    Een eigen functie en niet alleen een test, zodat de tweede test dezelfde controle op een
    eigen app kan uitvoeren en kan aantonen dat hij werkelijk rood wordt.
    """
    return [
        f"{route}: geen herkenning, en niet in PUBLIEKE_ROUTES"
        for route in routes
        if not route.herkenningen and route.sleutel not in PUBLIEKE_ROUTES
    ]


@pytest.fixture(scope="module")
def routes() -> list[RouteHerkenning]:
    return route_herkenningen(app)


def _parameters() -> list[Any]:
    """Elke route een eigen geval, zodat de uitslag per route leesbaar is."""
    return [pytest.param(route, id=f"{route.methode} {route.pad}") for route in route_herkenningen(app)]


@pytest.mark.parametrize("route", _parameters())
def test_de_route_noemt_een_herkenning(route: RouteHerkenning) -> None:
    """Precies een herkenning, of een reden om publiek te zijn.

    Precies een en niet minstens een: twee herkenningen op dezelfde route betekent dat er
    twee controles achter elkaar lopen, en welke van de twee weigert is dan niet te zeggen.
    """
    if route.sleutel in PUBLIEKE_ROUTES:
        assert not route.herkenningen, (
            f"{route} staat in PUBLIEKE_ROUTES maar eist {list(route.herkenningen)}; haal hem uit die lijst"
        )
        return

    if route.sleutel in HANDGESCHREVEN_CONTROLES:
        assert not route.herkenningen, (
            f"{route} draagt {list(route.herkenningen)} maar staat in HANDGESCHREVEN_CONTROLES; "
            f"verhuis de route naar de gewone plicht en haal de regel weg"
        )
        return

    assert not ontbrekende_herkenning([route]), (
        f"{route} noemt geen herkenning. Zet er een decorator op (een van "
        f"requires_sso, validate_api_token, validate_user_token, validate_platform_admin_token, "
        f"validate_metrics_scrape_token), of zet hem met een reden "
        f"in PUBLIEKE_ROUTES in dit bestand."
    )
    assert len(route.herkenningen) == 1, f"{route} eist er meer dan een: {list(route.herkenningen)}"


def _handgeschreven_parameters() -> list[Any]:
    """Elke regel uit HANDGESCHREVEN_CONTROLES een eigen geval, met de route erbij als die bestaat."""
    per_sleutel = {route.sleutel: route for route in route_herkenningen(app)}
    return [
        pytest.param(per_sleutel.get(sleutel), reden, sporen, id=f"{methode} {pad}")
        for sleutel, (reden, sporen) in HANDGESCHREVEN_CONTROLES.items()
        for methode, pad in [sleutel]
    ]


@pytest.mark.parametrize(("route", "reden", "sporen"), _handgeschreven_parameters())
def test_de_handgeschreven_controle_staat_er_nog(
    route: RouteHerkenning | None, reden: str, sporen: tuple[str, ...]
) -> None:
    """Zonder deze pin merkt niemand het als de controle uit de handler verdwijnt: de plicht
    hierboven kijkt voor deze routes alleen of er geen markering staat, en wie de controle
    weghaalt zet er geen markering bij.
    """
    assert route is not None, (
        "deze regel in HANDGESCHREVEN_CONTROLES beschrijft een route die niet meer bestaat; "
        "haal de regel weg of herzie hem"
    )
    # Een regel zonder sporen pint niets, en een leeg spoor staat in elke bron.
    assert sporen, f"{route}: een regel in HANDGESCHREVEN_CONTROLES eist minstens een spoor"
    assert all(sporen), f"{route}: een leeg spoor in HANDGESCHREVEN_CONTROLES pint niets"
    ontbrekend = [spoor for spoor in sporen if spoor not in inspect.getsource(route.endpoint)]
    assert not ontbrekend, (
        f"{route}: {ontbrekend} staat niet meer in de bron van de handler. De controle die de "
        f"reden beschrijft ({reden}) is mogelijk weggehaald; staat hij er nog maar anders, pas "
        f"dan de sporen aan."
    )


def test_geen_api_route_leunt_op_de_sso_markering(routes: list[RouteHerkenning]) -> None:
    """Onder ``/api/`` is ``_requires_sso`` een lege belofte.

    De middleware geeft elk pad onder ``/api/`` meteen door aan de handler en komt niet toe
    aan het lezen van die markering. Een route die daar toch op leunt is open, en leest in
    de code alsof hij afgeschermd is: het gevaarlijkste van de twee.
    """
    leunend = [str(route) for route in routes if route.pad.startswith("/api/") and "sso-sessie" in route.herkenningen]

    assert not leunend, (
        f"deze /api/-routes dragen _requires_sso, maar de middleware leest die daar niet: {leunend}. "
        f"Gebruik een controle in de route zelf."
    )


def test_de_lijsten_beschrijven_alleen_bestaande_routes(routes: list[RouteHerkenning]) -> None:
    """Een regel voor een route die niet meer bestaat houdt een uitzondering in leven.

    Verdwijnt een route, dan blijft haar regel staan en dekt hij later een nieuwe route met
    hetzelfde pad zonder dat iemand dat opnieuw beoordeeld heeft.
    """
    bestaand = {route.sleutel for route in routes}

    verdwenen = sorted(
        f"{methode} {pad}" for methode, pad in (PUBLIEKE_ROUTES.keys() | HANDGESCHREVEN_CONTROLES.keys()) - bestaand
    )

    assert not verdwenen, f"deze regels beschrijven een route die niet meer bestaat: {verdwenen}"


def test_de_controle_wordt_rood_van_een_route_zonder_herkenning() -> None:
    """De vergrendeling zelf: zonder deze test kan de controle stilletjes niets meer doen.

    Een app met een route die geen decorator draagt en niet in de lijst staat, hoort te
    worden opgemerkt. Daarmee staat vast dat een groene uitslag hierboven betekent dat de
    routes in orde zijn, en niet dat de controle niets vond om naar te kijken.
    """
    # Zonder de documentroutes van FastAPI zelf (/docs, /redoc, /openapi.json) meet deze
    # tegenproef alleen de route die zij neerzet.
    eigen_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @eigen_app.get("/een-nieuwe-route-zonder-decorator")
    async def nieuwe_route() -> dict[str, str]:
        return {}

    klachten = ontbrekende_herkenning(route_herkenningen(eigen_app))

    assert len(klachten) == 1
    assert "/een-nieuwe-route-zonder-decorator" in klachten[0]


def test_de_controle_is_tevreden_met_een_herkenning() -> None:
    """En het omgekeerde, anders zou een controle die altijd klaagt er ook door komen."""
    eigen_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @eigen_app.get("/een-nieuwe-route-met-decorator")
    @requires_sso
    async def nieuwe_route() -> dict[str, str]:
        return {}

    assert ontbrekende_herkenning(route_herkenningen(eigen_app)) == []
