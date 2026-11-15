"""Static file serving with cache headers that follow the content hash in the URL."""

import re
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


def cache_control_for(query_string: bytes) -> str:
    """The Cache-Control for a static URL: immutable with a ``?v=`` hash, else revalidate."""
    query = parse_qs(query_string.decode("latin-1"))
    return IMMUTABLE_CACHE_CONTROL if query.get("v", [""])[0] else REVALIDATE_CACHE_CONTROL


# The delivery directory of lord-of-the-components: /static/lotc/v<date>-<time>-NLDD<version>/...
# LOTC makes a new one on every change, so everything inside it is immutable by definition,
# including what global.css pulls in through @import. The pattern lives here instead of being
# imported: LOTC publishes it only in its test server (tests/visual/serve.py), not in the package.
LOTC_VERSIONED_PATH = re.compile(r"(^|/)v\d{8}-\d{4}-NLDD[^/]+/")


def lotc_cache_control(rel_path: str, query_string: bytes) -> str:
    """The Cache-Control for ``/static/lotc/<rel_path>``: immutable inside a version directory."""
    if LOTC_VERSIONED_PATH.search(rel_path):
        return IMMUTABLE_CACHE_CONTROL
    return cache_control_for(query_string)


class CacheControlledStaticFiles(StaticFiles):
    """Serves static files, pinning only URLs that carry a ``?v=`` content hash.

    The header hangs on the parameter, not on the path: a reference without ``?v=`` falls
    back to revalidate instead of being pinned for a year at everyone who already fetched
    it.

    The assets of the component system do not pass through here: they are served under
    /static/lotc/ by a route of their own, with lotc_cache_control.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["cache-control"] = cache_control_for(scope.get("query_string", b""))
        return response
