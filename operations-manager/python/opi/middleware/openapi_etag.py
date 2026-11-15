"""An ``ETag`` on ``/openapi.json``, so a client can cache on change instead of on time.

FastAPI serves ``/openapi.json`` itself and ``/docs`` points at that same URL, so this is a
middleware and not a route of its own: replacing the route would cost the documentation page.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request

#: The path FastAPI uses for the document. Stands apart here so the middleware does not
#: concern itself with other requests.
OPENAPI_PATH = "/openapi.json"


def _etag_for(document: dict[str, object]) -> tuple[str, bytes]:
    """The ETag and the body that belongs to it.

    ``sort_keys`` because a dict order means nothing to a reader: without it a restart with a
    different insertion order would yield a new ETag and make every client download again.
    """
    body = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return f'"{hashlib.sha256(body).hexdigest()[:32]}"', body


class OpenApiETagMiddleware(BaseHTTPMiddleware):
    """Answers ``GET /openapi.json`` with an ETag, and with 304 when nothing changed."""

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.url.path != OPENAPI_PATH or request.method not in ("GET", "HEAD"):
            return await call_next(request)

        etag, body = _etag_for(request.app.openapi())

        # A client may send several values, and "*" means "whatever you have".
        presented = [part.strip() for part in request.headers.get("if-none-match", "").split(",") if part.strip()]
        if etag in presented or "*" in presented:
            # A 304 carries no body but does carry the ETag: otherwise the client does not know
            # which revision it is now on.
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})

        return JSONResponse(
            content=json.loads(body),
            headers={
                "ETag": etag,
                # no-cache is not "do not cache" but "cache, and ask every time whether it
                # still holds".
                "Cache-Control": "no-cache",
            },
        )
