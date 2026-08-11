"""Static file serving with cache headers that follow the content hash in the URL."""

from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from starlette.staticfiles import StaticFiles

if TYPE_CHECKING:
    from starlette.responses import Response
    from starlette.types import Scope

# A year, the maximum the spec advises. Only ever handed out for a URL that identifies
# its own contents through the ?v= hash, so a changed file is a different URL.
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
# Not "do not cache": the browser may keep the copy but must revalidate. That is what
# turns the ETag we already send into a 304 instead of a silently stale file.
REVALIDATE_CACHE_CONTROL = "no-cache"


class CacheControlledStaticFiles(StaticFiles):
    """Serves static files, pinning only URLs that carry a ``?v=`` content hash.

    The header hangs on the parameter, not on the path. That is deliberate: a reference
    that was forgotten when it was converted to ``static_url()`` then falls back to
    no-cache and is at worst suboptimal, never a file pinned for a year at everyone who
    already fetched it.

    De assets van het componentensysteem lopen hier niet langs: die worden onder
    /static/lotc/ door een eigen route geserveerd.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        versioned = bool(query.get("v", [""])[0])
        response.headers["cache-control"] = IMMUTABLE_CACHE_CONTROL if versioned else REVALIDATE_CACHE_CONTROL
        return response
