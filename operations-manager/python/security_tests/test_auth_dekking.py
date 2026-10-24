"""Elke route die herkenning nodig heeft, weigert een naamloze aanroeper.

De statische tegenhanger (``tests/test_elke_route_heeft_een_herkenning.py``) leest de
markering op het endpoint. Deze proef roept de routes echt aan op de draaiende sandbox, want
een markering is geen handhaving: de uitslag komt van het antwoord, niet van een decorator.

De lijst bewust-publieke routes komt uit die statische toets, zodat er een plek is waar een
reviewer naar kijkt in plaats van twee.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.api.route_herkenning import WEBSOCKET, route_herkenningen
from opi.server import app
from tests.test_elke_route_heeft_een_herkenning import PUBLIEKE_ROUTES

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx
    from opi.api.route_herkenning import RouteHerkenning

    from security_tests.doel import Doel

#: De vulwaarde voor elk padsegment. Een bestaand proefproject, zodat een route die zijn
#: project opzoekt niet op "niet gevonden" strandt voordat de herkenning aan de beurt is.
VULWAARDE = "proef-alfa"


class Uitkomst(StrEnum):
    """Wat het antwoord op een naamloze aanroep zegt over de herkenning."""

    GEWEIGERD = "geweigerd"
    ONBESLIST = "onbeslist"
    BEVINDING = "bevinding"


@dataclass(frozen=True)
class Antwoord:
    """Wat er van een aanroep terugkwam, zover de beoordeling het nodig heeft."""

    status: int
    location: str = ""


def beoordeel(antwoord: Antwoord) -> Uitkomst:
    """De uitkomst van een naamloze aanroep.

    401 en 403 zijn de weigering; een omleiding naar de inlogflow is de weigering van een
    paginaroute. 400 en 422 zijn onbeslist: de parameter- of bodyvalidatie van FastAPI loopt
    voor de handler, dus de herkenning kwam niet aan de beurt. Die routes worden met een
    geldige body beproefd in ``test_schemathesis.py``.

    429 is ook onbeslist, en om dezelfde reden: de ratelimiter van RC-368 weigert voor de
    handler, dus over de herkenning van die route zegt het antwoord niets. Een sweep langs
    alle routen gaat sneller dan de burst van API_RATE_LIMIT_PER_MINUTE, dus dit komt voor.
    """
    if antwoord.status in (401, 403):
        return Uitkomst.GEWEIGERD
    if antwoord.status in (301, 302, 303, 307, 308):
        return Uitkomst.GEWEIGERD if antwoord.location.startswith("/auth/login") else Uitkomst.BEVINDING
    if antwoord.status in (400, 422, 429):
        return Uitkomst.ONBESLIST
    return Uitkomst.BEVINDING


def vul_pad_in(pad: str) -> str:
    """Vervang elk padparameter door een vulwaarde, zodat de route te bereiken is."""
    return re.sub(r"\{[^}]+\}", VULWAARDE, pad)


def te_beproeven_routes() -> list[RouteHerkenning]:
    """Elke HTTP-route die niet bewust publiek is."""
    return [
        route
        for route in route_herkenningen(app)
        if route.methode != WEBSOCKET and route.sleutel not in PUBLIEKE_ROUTES
    ]


def loop_langs(routes: list[RouteHerkenning], vraag: Callable[[str, str], Antwoord]) -> dict[Uitkomst, list[str]]:
    """Roep elke route naamloos aan en groepeer de routes op uitkomst."""
    per_uitkomst: dict[Uitkomst, list[str]] = {uitkomst: [] for uitkomst in Uitkomst}
    for route in routes:
        antwoord = vraag(route.methode, vul_pad_in(route.pad))
        per_uitkomst[beoordeel(antwoord)].append(f"{route} -> {antwoord.status}")
    return per_uitkomst


def _vraag_via(client: httpx.Client | TestClient) -> Callable[[str, str], Antwoord]:
    def vraag(methode: str, pad: str) -> Antwoord:
        antwoord = client.request(methode, pad)
        return Antwoord(antwoord.status_code, antwoord.headers.get("location", ""))

    return vraag


def test_geen_route_laat_een_naamloze_aanroeper_door(anoniem: httpx.Client, doel: Doel) -> None:
    """Faalconditie: een route die geen bewust-publieke route is, weigert niet."""
    routes = te_beproeven_routes()
    assert routes, "geen routes gevonden om te beproeven"
    per_uitkomst = loop_langs(routes, _vraag_via(anoniem))
    bevindingen = per_uitkomst[Uitkomst.BEVINDING]
    assert not bevindingen, (
        f"{len(bevindingen)} van {len(routes)} routes weigeren een naamloze aanroeper niet:\n"
        + "\n".join(sorted(bevindingen))
    )
    # Zonder deze grens kan de sweep stil blind worden: als de ratelimiter de helft van de
    # routes weigert, is er over hun eigen herkenning niets gemeten en leest dat als groen.
    assert len(per_uitkomst[Uitkomst.GEWEIGERD]) > len(routes) // 2, (
        f"maar {len(per_uitkomst[Uitkomst.GEWEIGERD])} van {len(routes)} routes weigerden op hun eigen"
        " herkenning; de rest bleef onbeslist, dus deze sweep heeft te weinig gemeten"
    )


def test_de_dekkingsproef_merkt_een_open_route_op() -> None:
    """Negatief voorbeeld: tegen een app met een open route hoort de proef rood te staan."""
    kwetsbaar = FastAPI()

    @kwetsbaar.get("/geheim")
    async def geheim() -> dict[str, str]:
        return {"geheim": "open"}

    routes = [route for route in route_herkenningen(kwetsbaar) if route.pad == "/geheim"]
    with TestClient(kwetsbaar) as client:
        per_uitkomst = loop_langs(routes, _vraag_via(client))
    assert per_uitkomst[Uitkomst.BEVINDING] == ["GET /geheim (security_tests.test_auth_dekking) -> 200"]


@pytest.mark.parametrize(
    ("antwoord", "verwacht"),
    [
        (Antwoord(401), Uitkomst.GEWEIGERD),
        (Antwoord(403), Uitkomst.GEWEIGERD),
        (Antwoord(302, "/auth/login"), Uitkomst.GEWEIGERD),
        (Antwoord(302, "/dashboard"), Uitkomst.BEVINDING),
        (Antwoord(422), Uitkomst.ONBESLIST),
        (Antwoord(429), Uitkomst.ONBESLIST),
        (Antwoord(200), Uitkomst.BEVINDING),
        (Antwoord(500), Uitkomst.BEVINDING),
    ],
)
def test_beoordeling_van_een_antwoord(antwoord: Antwoord, verwacht: Uitkomst) -> None:
    assert beoordeel(antwoord) is verwacht
