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

from __future__ import annotations

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

# CSRF exemption is split into two kinds so a probe path (e.g. /health) does
# not accidentally exempt a user-facing page that starts with the same prefix
# (/healthz is intentional; /metrics-explorer would NOT have been).
#
# - prefix exemptions (`/api/`, `/static/`, `/auth/`): always end in a slash
#   so they only match directory-style subtrees, never bare-prefix matches.
# - exact exemptions: the probe routes registered in `server.py` and
#   `prometheus_router.py`. Any new probe route must be added here
#   explicitly; bare-prefix matching is intentionally not used.
CSRF_EXEMPT_PREFIXES = (
    "/api/",
    "/static/",
    "/auth/",
)
CSRF_EXEMPT_EXACT = frozenset(
    {
        "/health",
        "/healthz",
        "/readyz",
        "/metrics",
    }
)


class CSRFMiddleware(BaseHTTPMiddleware):
    """Enforce CSRF protection centrally and seed the CSRF cookie.

    For unsafe methods on non-exempt paths this validates both the
    double-submit token and the Origin/Referer header before the request
    reaches the route handler. For all responses it ensures a CSRF token
    cookie is present so forms rendered by GET requests can submit it back.
    """

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        # Resolve the request's CSRF token before the handler runs so templates
        # can render it server-side (into hx-headers / hidden form field /
        # meta-tag) without reading the cookie from JavaScript. Single value
        # per session: an existing cookie is reused; a missing cookie causes
        # one to be minted now and persisted on the response.
        cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
        if cookie_token:
            request.state.csrf_token = cookie_token
        else:
            new_token = secrets.token_urlsafe(32)
            request.state.csrf_token = new_token
            request.state.csrf_token_new = new_token

        if request.method not in SAFE_METHODS and not _is_exempt_path(request.url.path):
            try:
                await _enforce_csrf(request)
            except HTTPException as exc:
                # Return a clean JSON 403 instead of letting it bubble to the
                # generic exception handler (which would log a stack trace).
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

        response = await call_next(request)

        new_token = getattr(request.state, "csrf_token_new", None)
        if new_token is not None:
            response.set_cookie(
                key=CSRF_COOKIE_NAME,
                value=new_token,
                # httponly=True is safe here because the token never needs to
                # be read by client-side JavaScript: templates render it
                # server-side into hx-headers, hidden form fields, or a
                # <meta> tag. The cookie's only job is to be echoed back
                # automatically by the browser for the double-submit check.
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
            )

        return response


def _is_exempt_path(path: str) -> bool:
    """Return True if the path is exempt from CSRF enforcement.

    Two kinds of exemption: exact match for the named probe paths, and
    slash-suffixed prefix match for directory-style subtrees. A bare-prefix
    match (e.g. `/health` matching `/healthxyz`) is intentionally NOT
    supported; any new probe path must be added to ``CSRF_EXEMPT_EXACT``.
    """
    return path in CSRF_EXEMPT_EXACT or path.startswith(CSRF_EXEMPT_PREFIXES)


async def _enforce_csrf(request: Request) -> None:
    """Validate double-submit token and Origin/Referer for an unsafe request.

    Raises:
        HTTPException: 403 if either check fails.
    """
    submitted_token = await _extract_submitted_token(request)
    _validate_double_submit(request, submitted_token)
    validate_csrf_origin(request)


def reject_misfired_form_get(request: Request) -> None:
    """Refuse a GET that looks like a misfired wizard form submission.

    If htmx fails to attach to a wizard form (e.g. an external tool
    rewrites the DOM, or the form tag lacks a ``method`` attribute and
    htmx isn't ready yet), the browser falls back to a native submit.
    The wizard form has no ``method`` attribute, so the native submit
    ends up as a GET with the form fields URL-encoded as query
    parameters in JSONPath syntax (``users[0]/email=...``,
    ``components[0]/name=...``).

    No legitimate caller of the wizard step GET handlers sends such
    query parameters. Receiving them means a form fell back to GET, so
    we refuse rather than silently render a step from stale state
    (which would also bypass this middleware because GET is safe).
    """
    for key in request.query_params:
        if "[" in key or "/" in key:
            logger.warning(
                "Refused wizard step GET with form-data-style query params on %s (key=%r)",
                request.url.path,
                key,
            )
            raise HTTPException(
                status_code=400,
                detail=(
                    "Het wizard-formulier kon niet via htmx worden ingediend en is "
                    "teruggevallen op een GET. Herlaad de pagina en probeer opnieuw."
                ),
            )


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
        # Cache the raw body before parsing the form. Under Starlette's
        # BaseHTTPMiddleware the request is a ``_CachedRequest`` that only
        # replays the body to the downstream handler when ``_body`` is set
        # (i.e. ``request.body()`` was called). ``request.form()`` consumes
        # ``request.stream()`` WITHOUT caching ``_body``, so the handler would
        # then receive an empty body and every form field would read as
        # missing ("Dit veld is verplicht"). Reading the body first makes the
        # form re-parse from the cache and lets the handler read it too.
        await request.body()
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
    """Return the CSRF token for this request.

    CSRFMiddleware sets ``request.state.csrf_token`` on every request
    (existing cookie value or a freshly minted one). This helper is kept
    for backward compatibility with handlers that read the token
    explicitly; new code should reference ``request.state.csrf_token``
    directly, or in templates ``{{ request.state.csrf_token }}``.
    """
    return request.state.csrf_token


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
