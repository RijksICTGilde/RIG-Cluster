"""Keeps the connection's untouched peer address in the scope.

Must sit OUTSIDE ``ProxyHeadersMiddleware``: that one overwrites ``scope["client"]`` with a
value from ``X-Forwarded-For``, and with ``trusted_hosts=["*"]`` that is the leftmost value,
so the caller's own. Without this middleware the peer address can no longer be read
afterwards, and even ``TRUSTED_PROXY_HOPS=0`` would still count on a chosen address.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opi.utils.client_ip import PEER_SCOPE_KEY

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send


class PeerAddressMiddleware:
    """Puts the peer address in the scope before another middleware can overwrite it."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            client = scope.get("client")
            if client:
                scope[PEER_SCOPE_KEY] = client[0]
        await self.app(scope, receive, send)
