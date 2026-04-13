"""CSRF protection utilities for web forms."""

import secrets
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from starlette.responses import Response

CSRF_COOKIE_NAME = "csrf_token"
CSRF_FORM_FIELD = "csrf_token"


class CSRFMiddleware(BaseHTTPMiddleware):
    """Middleware to set CSRF cookie when a new token is generated."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        # If a new CSRF token was generated, set it as a cookie
        if hasattr(request.state, "csrf_token_new"):
            response.set_cookie(
                key=CSRF_COOKIE_NAME,
                value=request.state.csrf_token_new,
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
            )

        return response


def ensure_csrf_token(request: Request) -> str:
    """Get existing CSRF token from cookie or generate new one.

    Args:
        request: The FastAPI request object

    Returns:
        The CSRF token string
    """
    token = request.cookies.get(CSRF_COOKIE_NAME)
    if not token:
        token = secrets.token_urlsafe(32)
        # Store new token in request state for response middleware to set cookie
        request.state.csrf_token_new = token
    return token


def validate_csrf_token(request: Request, form_data: dict | None) -> None:
    """Validate CSRF token from form matches cookie.

    Args:
        request: The FastAPI request object
        form_data: Dictionary containing form data with csrf_token field

    Raises:
        HTTPException: If CSRF token is missing or invalid
    """
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    form_token = form_data.get(CSRF_FORM_FIELD) if form_data else None

    if not cookie_token or not form_token:
        raise HTTPException(status_code=403, detail="CSRF token missing")
    if not secrets.compare_digest(cookie_token, form_token):
        raise HTTPException(status_code=403, detail="CSRF token invalid")
