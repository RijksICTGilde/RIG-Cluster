"""Bewaart het onaangetaste peer-adres van de verbinding in de scope.

Moet BUITEN ``ProxyHeadersMiddleware`` staan: die overschrijft ``scope["client"]`` met een
waarde uit ``X-Forwarded-For``, en met ``trusted_hosts=["*"]`` is dat de linkse waarde, dus
die van de aanroeper zelf. Zonder deze middleware is het peer-adres daarna niet meer te
lezen en zou ook ``TRUSTED_PROXY_HOPS=0`` nog op een gekozen adres tellen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opi.utils.client_ip import PEER_SCOPE_KEY

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send


class PeerAddressMiddleware:
    """Zet het peer-adres in de scope voordat een andere middleware het kan overschrijven."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            client = scope.get("client")
            if client:
                scope[PEER_SCOPE_KEY] = client[0]
        await self.app(scope, receive, send)
