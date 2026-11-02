"""Clear the browser cache for this origin once, on the first HTML page after this release.

Releases before 2 November 2026 served /static/lotc/ without Cache-Control. A browser
then decides itself how long its copy stays fresh, and keeps using it without asking.
The URLs in the page head carry ``?v=<hash>`` now, but global.css imports eight files by
a fixed name, so a browser with old copies ran the old fouc.css against the new nldd.js:
a white page until the user cleared the cache. Every unversioned file is ``no-cache``
now, so this cannot build up again; what is already in a browser has to go once.
"""

from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

COOKIE_NAME = "zad_cache_reset"
#: Raise this only if a cache in the field has to go again; every browser clears once per value.
COOKIE_VALUE = "2026-11-02"
CLEAR_SITE_DATA = '"cache"'


class CacheResetMiddleware(BaseHTTPMiddleware):
    """Send ``Clear-Site-Data: "cache"`` on an HTML page until the browser has the cookie."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        if request.cookies.get(COOKIE_NAME) == COOKIE_VALUE:
            return response
        if not response.headers.get("content-type", "").startswith("text/html"):
            return response
        response.headers["Clear-Site-Data"] = CLEAR_SITE_DATA
        response.set_cookie(
            COOKIE_NAME,
            COOKIE_VALUE,
            max_age=365 * 24 * 3600,
            path="/",
            secure=request.url.scheme == "https",
            httponly=True,
            samesite="lax",
        )
        return response
