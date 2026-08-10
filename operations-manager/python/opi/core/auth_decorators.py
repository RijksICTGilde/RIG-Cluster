"""
Authentication decorators for marking routes with SSO requirements.

These decorators provide a clean way to annotate routes with their
authentication requirements instead of using URL-based checks.
"""

import logging
from functools import wraps
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


def requires_sso(func: Callable) -> Callable:
    """
    Decorator to mark a route as requiring SSO authentication.

    This decorator adds metadata to the function indicating that
    the route requires user authentication via Keycloak SSO.

    Args:
        func: The route handler function

    Returns:
        The decorated function with SSO requirement metadata
    """

    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        return await func(*args, **kwargs)

    # Add metadata to indicate SSO is required
    wrapper._requires_sso = True
    logger.debug(f"Marked function {func.__name__} as requiring SSO")
    return wrapper


def get_current_user(request) -> dict[str, Any] | None:
    """
    Utility function to get the current authenticated user from request.

    This is a helper function that route handlers can use to access
    the current user information that was set by the authorization middleware.

    Args:
        request: The FastAPI/Starlette request object

    Returns:
        User information dictionary if authenticated, None otherwise
    """
    user = getattr(request.state, "user", None)
    if user:
        logger.debug(f"Retrieved current user: {user.get('email', 'unknown')}")
    else:
        logger.debug("No current user found in request state")
    return user


def require_platform_admin(request) -> dict[str, Any]:
    """Return the current user, or refuse the request if they are not a platform admin.

    Hiding a link in the menu is presentation, not access control: a page meant for
    admins has to say so itself, or the URL is the way in.
    """
    from opi.services.user_service import get_user_service

    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Niet ingelogd")
    email = str(user.get("email", "")).lower()
    if not get_user_service().is_platform_admin(email):
        raise HTTPException(status_code=403, detail="Alleen beheerders hebben toegang")
    return user
