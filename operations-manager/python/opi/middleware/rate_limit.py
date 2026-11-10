"""A coarse upper bound on the number of requests per client, on the API and the auth routes.

Flattens the peak: a flood of well-formed requests is refused before session, authorization
and the route itself have work to do. The limit is wide, so normal use does not touch it,
and the refusal is immediate (429 with ``Retry-After``), never a delay: a request held open
is exactly what takes OPI down.

The login paths get no extra brake of their own here. Password guessing is already throttled
at the source, by ``bruteForceProtected`` on the Keycloak realms.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse

from opi.http.rate_limiter import (
    API_FLOOD_LIMITER,
    FLOOD_COUNTED_IN_SCOPE,
    IPRateLimiter,
    retry_after_header,
)

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

#: Where the limit applies. The machine API and the auth routes come from outside; the rest
#: of the portal sits behind a session and is covered by the ingress.
LIMITED_PREFIXES = ("/api/", "/auth/")


class RateLimitMiddleware:
    """Refuses a request with 429 as soon as the client is over its coarse limit."""

    def __init__(self, app: ASGIApp, limiter: IPRateLimiter = API_FLOOD_LIMITER) -> None:
        self.app = app
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith(LIMITED_PREFIXES):
            await self.app(scope, receive, send)
            return

        # A middleware outside this one already counted the request (the image push
        # pre-check, which weighs before the body and so sits outside this middleware).
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
