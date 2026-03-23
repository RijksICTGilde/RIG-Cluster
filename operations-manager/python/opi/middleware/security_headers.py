"""
Security headers middleware for internet.nl compliance.

Sets HTTP security headers on all responses. This is the application-level
fallback for environments where the ingress controller (e.g. HAProxy on
OpenShift) cannot inject response headers via annotations.

Headers set:
- Strict-Transport-Security (HSTS)
- X-Content-Type-Options
- X-Frame-Options
- Referrer-Policy
- Permissions-Policy
- Content-Security-Policy
"""

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security response headers to every HTTP response."""

    def __init__(self, app, *, keycloak_url: str = "") -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self._keycloak_host = self._extract_origin(keycloak_url)

    @staticmethod
    def _extract_origin(url: str) -> str:
        """Return scheme+host from a URL, e.g. 'https://keycloak.kind'."""
        if not url:
            return ""
        from urllib.parse import urlparse

        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else url.rstrip("/")

    def _build_csp(self) -> str:
        keycloak = f" {self._keycloak_host}" if self._keycloak_host else ""

        # Inline styles: ROOS components render style attributes; 'unsafe-inline' required.
        # Inline scripts: HTMX event handlers are inline; 'unsafe-inline' required.
        # cdn.jsdelivr.net: Chart.js loaded from CDN on project-details page.
        # unpkg.com: HTMX loaded from CDN by jinja-roos-components.
        parts = [
            "default-src 'self'",
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com",
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
            f"img-src 'self' data: https://fastapi.tiangolo.com{keycloak}",
            "font-src 'self'",
            f"connect-src 'self'{keycloak}",
            f"form-action 'self'{keycloak}",
            "frame-ancestors 'none'",
            "base-uri 'self'",
        ]
        return "; ".join(parts)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)

        # HSTS: only when accessed via HTTPS (ProxyHeadersMiddleware sets scheme from X-Forwarded-Proto)
        if request.url.scheme == "https":
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains; preload",
            )

        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "geolocation=(), microphone=(), camera=()",
        )
        response.headers.setdefault("Content-Security-Policy", self._build_csp())

        return response
