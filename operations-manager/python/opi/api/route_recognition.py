"""Every registered route with the ways its endpoint recognises a caller.

OPI recognises callers in five ways, each a marker on the endpoint. This module reads those
markers off the routes, so a test can enforce that every route carries one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from starlette.routing import WebSocketRoute

from opi.http.auth.endpoint_util import (
    REQUIRES_METRICS_SCRAPE_TOKEN,
    REQUIRES_PROJECT_NAME,
)
from opi.http.auth.user_token_auth import REQUIRES_PLATFORM_ADMIN_TOKEN, REQUIRES_USER_TOKEN

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.routing import BaseRoute

#: The endpoint attribute per way OPI recognises a caller. The key is the short label that
#: stays readable in an error message.
MARKERS: dict[str, str] = {
    "sso-sessie": "_requires_sso",
    "projectsleutel": REQUIRES_PROJECT_NAME,
    "sso-bearertoken": REQUIRES_USER_TOKEN,
    "platformbeheerder-token": REQUIRES_PLATFORM_ADMIN_TOKEN,
    "metrics-token": REQUIRES_METRICS_SCRAPE_TOKEN,
}

#: The method label of a websocket route, which has no HTTP method.
WEBSOCKET = "WEBSOCKET"


class RouteRecognition(NamedTuple):
    method: str
    path: str
    module: str
    recognitions: tuple[str, ...]
    #: The registered endpoint itself, so a guard can read the source of the handler.
    endpoint: object

    @property
    def key(self) -> tuple[str, str]:
        """The shape a route has in a list of exceptions."""
        return (self.method, self.path)

    def __str__(self) -> str:
        return f"{self.method} {self.path} ({self.module})"


def _recognitions_of(endpoint: object) -> tuple[str, ...]:
    return tuple(name for name, attribute in MARKERS.items() if getattr(endpoint, attribute, False))


def _methods_of(route: BaseRoute) -> list[str]:
    """The HTTP methods of a route, or WEBSOCKET for a websocket route, which Starlette gives none."""
    methods = getattr(route, "methods", None)
    if methods:
        # HEAD comes along with every GET and is not a boundary of its own.
        return sorted(m for m in methods if m != "HEAD")
    # On the starlette class and not on the name: FastAPI registers an
    # ``APIWebSocketRoute``, which inherits from this one.
    if isinstance(route, WebSocketRoute):
        return [WEBSOCKET]
    return []


def route_recognitions(app: FastAPI) -> list[RouteRecognition]:
    """Every route that has an endpoint, once per HTTP method."""
    found: list[RouteRecognition] = []
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        path = getattr(route, "path", "")
        module = getattr(endpoint, "__module__", "?")
        recognitions = _recognitions_of(endpoint)
        found.extend(RouteRecognition(method, path, module, recognitions, endpoint) for method in _methods_of(route))
    return found
