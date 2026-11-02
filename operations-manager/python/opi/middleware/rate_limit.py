"""Een grove bovengrens op het aantal verzoeken per client, op de API en de authroutes.

Vlakt de piek af: een stortvloed van goed gevormde verzoeken wordt geweigerd voordat
sessie, autorisatie en de route zelf er werk aan hebben. De grens staat ruim, zodat
normaal gebruik hem niet raakt, en de weigering is meteen (429 met ``Retry-After``),
nooit een vertraging: een opengehouden verzoek is juist wat OPI onderuit haalt.

De loginpaden krijgen hier geen eigen rem bovenop. Het raden van een wachtwoord wordt al
bij de bron afgeknepen, door ``bruteForceProtected`` op de Keycloak-realms.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse

from opi.api.rate_limiter import (
    API_FLOOD_LIMITER,
    FLOOD_COUNTED_IN_SCOPE,
    IPRateLimiter,
    retry_after_header,
)

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

#: Waar de grens geldt. De machine-API en de authroutes komen van buiten; de rest van het
#: portaal zit achter een sessie en wordt door de ingress afgedekt.
LIMITED_PREFIXES = ("/api/", "/auth/")


class RateLimitMiddleware:
    """Weigert een verzoek met 429 zodra de client over zijn grove grens is."""

    def __init__(self, app: ASGIApp, limiter: IPRateLimiter = API_FLOOD_LIMITER) -> None:
        self.app = app
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith(LIMITED_PREFIXES):
            await self.app(scope, receive, send)
            return

        # Een middleware buiten deze heeft het verzoek al afgeteld (de voorcontrole van de
        # image-push, die vóór de body weegt en dus buiten deze middleware staat).
        if scope.get(FLOOD_COUNTED_IN_SCOPE):
            await self.app(scope, receive, send)
            return

        client_id = IPRateLimiter.get_client_identifier(Request(scope))
        if self.limiter.is_allowed(client_id):
            await self.app(scope, receive, send)
            return

        response = JSONResponse(
            {"detail": "Too many requests"},
            status_code=429,
            headers=retry_after_header(self.limiter, client_id),
        )
        await response(scope, receive, send)
