"""CSRF protection utilities and central enforcement middleware for web forms.

This module implements defense-in-depth CSRF protection for all cookie/session
based web routes:

1. A double-submit cookie token: the value of the ``csrf_token`` cookie must
   match the token submitted in the ``X-CSRF-Token`` header or the
   ``csrf_token`` form field.
2. An Origin/Referer check against the request Host.

Enforcement is central (in :class:`CSRFMiddleware`), not opt-in per handler.
Every unsafe method (POST/PUT/PATCH/DELETE) on a non-exempt path must pass
both checks. API routes (under ``/api/``) are exempt because they authenticate
with an API key and are not cookie/session based; static assets and the OAuth
endpoints are exempt as well (the OAuth endpoints are GET-only).
"""

import logging
import secrets
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.responses import Response

logger = logging.getLogger(__name__)

CSRF_COOKIE_NAME = "csrf_token"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER_NAME = "x-csrf-token"

# HTTP methods that do not change state and therefore do not need CSRF checks.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Path prefixes that are exempt from CSRF enforcement.
# - /api/   : API-key authenticated, not cookie/session based.
# - /static/: static assets, no state change.
# - /auth/  : OAuth login/callback/logout, GET-only redirect flow.
# - infra   : health/metrics/ready probes.
CSRF_EXEMPT_PREFIXES = (
    "/api/",
    "/static/",
    "/auth/",
    "/health",
    "/metrics",
    "/ready",
)


class CSRFMiddleware(BaseHTTPMiddleware):
    """Enforce CSRF protection centrally and seed the CSRF cookie.

    For unsafe methods on non-exempt paths this validates both the
    double-submit token and the Origin/Referer header before the request
    reaches the route handler. For all responses it ensures a CSRF token
    cookie is present so forms rendered by GET requests can submit it back.
    """

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.method not in SAFE_METHODS and not _is_exempt_path(request.url.path):
            try:
                await _enforce_csrf(request)
            except HTTPException as exc:
                # Return a clean JSON 403 instead of letting it bubble to the
                # generic exception handler (which would log a stack trace).
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

        response = await call_next(request)

        # Ensure a CSRF token cookie exists. Either a handler minted a fresh
        # one via ensure_csrf_token(), or the request had no cookie at all and
        # we seed one now so the next state-changing request can succeed.
        new_token = getattr(request.state, "csrf_token_new", None)
        if new_token is None and not request.cookies.get(CSRF_COOKIE_NAME):
            new_token = secrets.token_urlsafe(32)

        if new_token is not None:
            response.set_cookie(
                key=CSRF_COOKIE_NAME,
                value=new_token,
                # Readable by JS so the double-submit token can be attached to
                # htmx/fetch requests as a header. The token cookie is not a
                # session credential; its only job is to be echoed back.
                httponly=False,
                samesite="strict",
                secure=request.url.scheme == "https",
            )

        return response


def _is_exempt_path(path: str) -> bool:
    """Return True if the path is exempt from CSRF enforcement."""
    return path.startswith(CSRF_EXEMPT_PREFIXES)


async def _enforce_csrf(request: Request) -> None:
    """Validate double-submit token and Origin/Referer for an unsafe request.

    Raises:
        HTTPException: 403 if either check fails.
    """
    submitted_token = await _extract_submitted_token(request)
    _validate_double_submit(request, submitted_token)
    validate_csrf_origin(request)


async def _extract_submitted_token(request: Request) -> str | None:
    """Read the CSRF token from the request header or, for form posts, the body.

    The header is preferred (works for htmx/fetch/JSON). Only form-encoded
    bodies are parsed for the ``csrf_token`` field; parsing a form is safe to
    repeat because Starlette caches it on the request.
    """
    header_token = request.headers.get(CSRF_HEADER_NAME)
    if header_token:
        return header_token

    content_type = request.headers.get("content-type", "")
    if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        form = await request.form()
        value = form.get(CSRF_FORM_FIELD)
        if isinstance(value, str):
            return value
    return None


def _validate_double_submit(request: Request, submitted_token: str | None) -> None:
    """Validate that the cookie token matches the submitted token.

    Raises:
        HTTPException: 403 if the token is missing or does not match.
    """
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    if not cookie_token or not submitted_token:
        logger.warning("CSRF check failed: token missing on %s %s", request.method, request.url.path)
        raise HTTPException(status_code=403, detail="CSRF token missing")
    if not secrets.compare_digest(cookie_token, submitted_token):
        logger.warning("CSRF check failed: token mismatch on %s %s", request.method, request.url.path)
        raise HTTPException(status_code=403, detail="CSRF token invalid")


def ensure_csrf_token(request: Request) -> str:
    """Get existing CSRF token from cookie or generate a new one.

    Args:
        request: The FastAPI request object

    Returns:
        The CSRF token string
    """
    token = request.cookies.get(CSRF_COOKIE_NAME)
    if not token:
        token = secrets.token_urlsafe(32)
        # Store new token in request state for the response middleware to set the cookie.
        request.state.csrf_token_new = token
    return token


def validate_csrf_token(request: Request, form_data: dict | None) -> None:
    """Validate CSRF token from form/header matches cookie.

    Kept for backwards compatibility with call sites that validate explicitly.
    Enforcement is now central in :class:`CSRFMiddleware`; calling this in a
    handler is idempotent (the middleware already validated the same token).

    Args:
        request: The FastAPI request object
        form_data: Dictionary containing form data with csrf_token field

    Raises:
        HTTPException: If CSRF token is missing or invalid
    """
    submitted = None
    if form_data:
        candidate = form_data.get(CSRF_FORM_FIELD)
        if isinstance(candidate, str):
            submitted = candidate
    if submitted is None:
        submitted = request.headers.get(CSRF_HEADER_NAME)
    _validate_double_submit(request, submitted)


def validate_csrf_origin(request: Request) -> None:
    """Validate the Origin/Referer header against the request Host.

    This is the primary defense against a same-site sibling-subdomain tenant
    attacker: an exact host match means a request from ``tenant.example.nl``
    to ``opi.example.nl`` is rejected even though it is "same-site".

    Args:
        request: The FastAPI request object

    Raises:
        HTTPException: If Origin/Referer validation fails
    """
    from opi.core.config import settings

    host = (request.headers.get("host") or "").strip()
    origin = (request.headers.get("origin") or "").strip()
    referer = (request.headers.get("referer") or "").strip()

    if not origin and not referer:
        logger.warning("CSRF check failed: no Origin or Referer header present")
        raise HTTPException(status_code=403, detail="Request rejected: missing origin information")

    request_host = (host.split(":")[0] or "").strip()
    if not request_host:
        logger.warning("CSRF check failed: empty or missing Host header")
        raise HTTPException(status_code=403, detail="Request rejected: invalid request")

    # Only allow a localhost bypass in DEBUG mode AND when the request itself
    # is to localhost, so DEBUG cannot weaken security on a real host.
    is_localhost_request = request_host in ("localhost", "127.0.0.1", "::1")
    allow_localhost = settings.DEBUG and is_localhost_request

    if settings.DEBUG and not is_localhost_request:
        logger.warning(
            "SECURITY: DEBUG mode enabled but request is to non-localhost host '%s'. "
            "Localhost CSRF bypass is DISABLED for this request.",
            request_host,
        )

    if origin:
        origin_host = (urlparse(origin).netloc.split(":")[0] or "").strip()
        if not origin_host:
            logger.warning("CSRF check failed: Origin '%s' has empty host", origin)
            raise HTTPException(status_code=403, detail="Request rejected: invalid origin")
        origin_matches = origin_host == request_host
        localhost_allowed = allow_localhost and origin_host in ("localhost", "127.0.0.1", "::1")
        if not origin_matches and not localhost_allowed:
            logger.warning("CSRF check failed: Origin '%s' does not match host '%s'", origin, host)
            raise HTTPException(status_code=403, detail="Request rejected: invalid origin")
    elif referer:
        referer_host = (urlparse(referer).netloc.split(":")[0] or "").strip()
        if not referer_host:
            logger.warning("CSRF check failed: Referer '%s' has empty host", referer)
            raise HTTPException(status_code=403, detail="Request rejected: invalid referer")
        referer_matches = referer_host == request_host
        localhost_allowed = allow_localhost and referer_host in ("localhost", "127.0.0.1", "::1")
        if not referer_matches and not localhost_allowed:
            logger.warning("CSRF check failed: Referer '%s' does not match host '%s'", referer, host)
            raise HTTPException(status_code=403, detail="Request rejected: invalid referer")
