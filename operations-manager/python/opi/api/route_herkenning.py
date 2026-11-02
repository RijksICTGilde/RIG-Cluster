"""Elke geregistreerde route met haar herkenning.

OPI herkent aanroepers op vijf manieren, elk met een markering op het endpoint. Deze module
leest die markeringen uit routes, zodat een test het kan afdwingen. Importeerbaar omdat
RC-322 en fase 1 dezelfde enumeratie nodig hebben.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from opi.api.endpoint_util import (
    REQUIRES_METRICS_SCRAPE_TOKEN,
    REQUIRES_PROJECT_NAME,
)
from opi.api.user_token_auth import REQUIRES_PLATFORM_ADMIN_TOKEN, REQUIRES_USER_TOKEN
from starlette.routing import WebSocketRoute

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.routing import BaseRoute

#: Het attribuut op het endpoint per manier waarop OPI een aanroeper herkent. De naam is de
#: korte aanduiding die in een foutmelding leesbaar blijft.
MARKERINGEN: dict[str, str] = {
    "sso-sessie": "_requires_sso",
    "projectsleutel": REQUIRES_PROJECT_NAME,
    "sso-bearertoken": REQUIRES_USER_TOKEN,
    "platformbeheerder-token": REQUIRES_PLATFORM_ADMIN_TOKEN,
    "metrics-token": REQUIRES_METRICS_SCRAPE_TOKEN,
}

#: Het methodelabel van een websocketroute, die geen HTTP-methode heeft.
WEBSOCKET = "WEBSOCKET"


class RouteHerkenning(NamedTuple):
    """Een geregistreerde route met de herkenningen die haar endpoint draagt."""

    methode: str
    pad: str
    module: str
    herkenningen: tuple[str, ...]
    #: Het geregistreerde endpoint zelf, zodat een bewaker de bron van de handler kan lezen.
    endpoint: object

    @property
    def sleutel(self) -> tuple[str, str]:
        """De vorm waarin een route in een lijst van uitzonderingen staat."""
        return (self.methode, self.pad)

    def __str__(self) -> str:
        return f"{self.methode} {self.pad} ({self.module})"


def _herkenningen_van(endpoint: object) -> tuple[str, ...]:
    return tuple(naam for naam, attribuut in MARKERINGEN.items() if getattr(endpoint, attribuut, False))


def _methoden_van(route: BaseRoute) -> list[str]:
    """De HTTP-methoden van een route, of WEBSOCKET voor een websocketroute (Starlette geeft haar geen methods)."""
    methoden = getattr(route, "methods", None)
    if methoden:
        # HEAD komt bij elke GET gratis mee en is geen eigen grens.
        return sorted(m for m in methoden if m != "HEAD")
    # Op de starlette-klasse en niet op de naam: FastAPI registreert een
    # ``APIWebSocketRoute``, die hiervan erft.
    if isinstance(route, WebSocketRoute):
        return [WEBSOCKET]
    return []


def route_herkenningen(app: FastAPI) -> list[RouteHerkenning]:
    """Elke route van app met een endpoint, en haar herkenning."""
    gevonden: list[RouteHerkenning] = []
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        pad = getattr(route, "path", "")
        module = getattr(endpoint, "__module__", "?")
        herkenningen = _herkenningen_van(endpoint)
        gevonden.extend(
            RouteHerkenning(methode, pad, module, herkenningen, endpoint) for methode in _methoden_van(route)
        )
    return gevonden
