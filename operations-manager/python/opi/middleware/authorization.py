"""
Authorization middleware for handling Keycloak SSO authentication.

This middleware checks route annotations to determine if SSO is required
and redirects unauthenticated users to the login page.
"""

import logging
import typing
from typing import Any

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Match

from opi.services.user_service import get_user_service

logger = logging.getLogger(__name__)

RequestResponseEndpoint = typing.Callable[[Request], typing.Awaitable[Response]]


def get_user(request: Request) -> dict[str, Any] | None:
    """
    Extract user information from the session.

    Args:
        request: The incoming HTTP request

    Returns:
        User information dictionary if authenticated, None otherwise
    """
    if not hasattr(request, "session"):
        logger.debug("No session available in request")
        return None

    user = request.session.get("user")
    if user:
        logger.debug(f"Found authenticated user: {user.get('email', 'unknown')}")
    else:
        logger.debug("No user found in session")
    return user


class AuthorizationMiddleware(BaseHTTPMiddleware):
    """
    Middleware to handle SSO authentication based on route annotations.

    This middleware:
    1. Checks if the route requires SSO authentication
    2. Verifies user authentication status
    3. Redirects to login if authentication is required but not present
    4. Stores user information in the user service
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """
        Process the request and handle authentication logic.

        Args:
            request: The incoming HTTP request
            call_next: The next middleware/handler in the chain

        Returns:
            HTTP response, potentially a redirect to login
        """
        path = request.url.path

        # Always allow static files
        if path.startswith("/static/"):
            return await call_next(request)

        # API routes should use API key authentication, not SSO by default
        if path.startswith("/api/"):
            # For API routes, only require SSO if explicitly marked with @requires_sso
            request.state.user = None  # API routes don't use session-based user
            return await call_next(request)

        # Get user from session
        user = get_user(request)

        # Check if the route requires SSO by examining route metadata
        route_requires_sso = self._route_requires_sso(request)

        if route_requires_sso and not user:
            logger.info(f"Redirecting unauthenticated user to login from: {path}")
            return RedirectResponse(url="/auth/login", status_code=302)

        # Store/update user information in the user service if authenticated
        if user and user.get("email"):
            user_service = get_user_service()
            user_service.store_user(user)

            # Check if user's email is allowed access
            user_email = user.get("email")
            if route_requires_sso and not user_service.is_email_allowed(user_email):
                logger.warning(f"Access denied for user {user_email} - not in allowlist")
                # Redirect to permission denied page instead of login
                return RedirectResponse(url="/permission-denied", status_code=302)

        # Add user to request state for use in handlers
        request.state.user = user

        return await call_next(request)

    def _route_requires_sso(self, request: Request) -> bool:
        """
        Determine if the current route requires SSO authentication.

        This method finds the matching route and checks its endpoint for
        the _requires_sso annotation.

        Args:
            request: The incoming HTTP request

        Returns:
            True if SSO is required, False otherwise
        """
        # Get the FastAPI app from the request
        app: FastAPI = request.app

        # Find the matching route by iterating through routes
        for route in app.router.routes:
            match, _ = route.matches({"type": "http", "path": request.url.path, "method": request.method})
            if match == Match.FULL:
                # Found the matching route
                endpoint = getattr(route, "endpoint", None)
                if endpoint:
                    # Check for our custom SSO requirement attribute
                    requires_sso = getattr(endpoint, "_requires_sso", False)  # Default to True
                    logger.debug(f"Route {request.url.path} SSO requirement: {requires_sso}")
                    return requires_sso

        # Default behavior for unmatched routes or routes without annotations
        logger.debug(f"Could not determine SSO requirement for {request.url.path}, defaulting to NOT require SSO")
        return True
