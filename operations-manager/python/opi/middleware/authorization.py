"""Authorization middleware for handling Keycloak SSO authentication.

Checks route annotations to determine if SSO is required and redirects
unauthenticated users to the login page.
"""

import logging
import typing
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Match

from opi.core.actor import set_actor, user_actor
from opi.core.logging_redact import redact_invite_key_in_path
from opi.http.auth.session_guard import drop_refused_session, valid_session_user
from opi.services.step_up import has_fresh_step_up, step_up_fresh
from opi.services.user_service import get_user_service

if typing.TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

# Exact paths that skip authorization checks. These MUST be exact matches and not
# prefixes, otherwise they would also match longer authenticated routes (e.g. the
# "/metrics" Prometheus scrape endpoint must not match the "/metrics-explorer" page).
# Prometheus has no SSO session, so /metrics is guarded in the route itself with
# PROMETHEUS_METRICS_AUTH_TOKEN (validate_metrics_scrape_token).
SKIP_AUTH_EXACT = ("/metrics",)

# URL prefixes that skip authorization checks and logging (health checks, static assets).
SKIP_AUTH_PREFIXES = (
    "/health",
    "/ready",
    "/version",
    "/static/",
)

RequestResponseEndpoint = typing.Callable[[Request], typing.Awaitable[Response]]


def get_user(request: Request) -> dict[str, Any] | None:
    """Extract user information from the session, or None when unauthenticated.

    A session past its absolute lifetime, or one revoked by a logout elsewhere, counts
    as unauthenticated (see opi/http/auth/session_guard.py).
    """
    if not hasattr(request, "session"):
        logger.debug("No session available in request")
        return None

    user = valid_session_user(request.session)
    if user:
        logger.debug(f"Found authenticated user: {user.get('email', 'unknown')}")
    else:
        logger.debug("No valid user found in session")
    return user


class AuthorizationMiddleware(BaseHTTPMiddleware):
    """Requires SSO on routes that are annotated for it, and redirects when it is missing."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        step_up_fresh.set(False)

        if path in SKIP_AUTH_EXACT or path.startswith(SKIP_AUTH_PREFIXES):
            return await call_next(request)

        # /api/ authenticates with X-API-Key in the routes themselves, so no SSO or
        # session check here. Browser-context JSON endpoints live under /ui/, not /api/.
        if path.startswith("/api/"):
            request.state.user = None
            return await call_next(request)

        user = get_user(request)
        if not user and hasattr(request, "session"):
            drop_refused_session(request.session)

        route_requires_sso = self._route_requires_sso(request)

        if route_requires_sso and not user:
            logger.info(f"Redirecting unauthenticated user to login from: {redact_invite_key_in_path(path)}")
            return RedirectResponse(url="/auth/login", status_code=302)

        if user:
            user_email = user.get("email")
            if user_email:
                user_service = get_user_service()
                user_service.store_user(user)

                if route_requires_sso and not user_service.is_email_allowed(user_email):
                    logger.warning(f"Access denied for user {user_email} - not in allowlist")
                    return RedirectResponse(url="/permission-denied", status_code=302)

        request.state.user = user
        # Before call_next: a contextvar travels into the task call_next starts, not back
        # into this middleware.
        if user and user.get("email"):
            set_actor(user_actor(user["email"]))
            step_up_fresh.set(has_fresh_step_up(request))

        return await call_next(request)

    def _route_requires_sso(self, request: Request) -> bool:
        """Read the ``_requires_sso`` annotation off the matching route; True when no route matches."""
        app: FastAPI = request.app

        # An invite key sits in the path and is a bearer secret, and these two lines run
        # on every request.
        logged_path = redact_invite_key_in_path(request.url.path)

        for route in app.router.routes:
            match, _ = route.matches({"type": "http", "path": request.url.path, "method": request.method})
            if match == Match.FULL:
                endpoint = getattr(route, "endpoint", None)
                if endpoint:
                    requires_sso = getattr(endpoint, "_requires_sso", False)
                    logger.debug(f"Route {logged_path} SSO requirement: {requires_sso}")
                    return requires_sso

        logger.debug(f"Could not determine SSO requirement for {logged_path}, defaulting to require SSO")
        return True
