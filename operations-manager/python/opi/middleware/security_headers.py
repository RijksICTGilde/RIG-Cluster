"""Security headers middleware for internet.nl compliance.

Sets HTTP security headers on all responses. This is the application-level
fallback for environments where the ingress controller (e.g. HAProxy on
OpenShift) cannot inject response headers via annotations.

The enforced policy allows inline scripts; the Report-Only policy does not. Why those
two sit side by side and what still stands in the way of closing the gap:
features/security-headers.md.
"""

import secrets
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security response headers to every HTTP response."""

    def __init__(self, app, *, keycloak_url: str = "", prometheus_url: str = "") -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self._keycloak_host = self._extract_origin(keycloak_url)
        self._prometheus_host = self._extract_origin(prometheus_url)

    @staticmethod
    def _extract_origin(url: str) -> str:
        """Return scheme+host from a URL, e.g. 'https://keycloak.kind'."""
        if not url:
            return ""
        from urllib.parse import urlparse

        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else url.rstrip("/")

    def _build_csp(self, *, nonce: str = "") -> str:
        """Build CSP policy. With nonce: strict report-only variant (nonce and unsafe-inline conflict)."""
        keycloak = f" {self._keycloak_host}" if self._keycloak_host else ""
        prometheus = f" {self._prometheus_host}" if self._prometheus_host else ""

        # Inline styles/scripts: components render style attributes and @click to onclick.
        # Frame-src: Prometheus metrics UI in an iframe.
        script_src = (
            f"script-src 'self' 'nonce-{nonce}' 'strict-dynamic'" if nonce else "script-src 'self' 'unsafe-inline'"
        )
        # object-src only in the target policy: the portal has no plug-ins, and under
        # 'strict-dynamic' <object> is the way back to script execution.
        object_src = ["object-src 'none'"] if nonce else []
        parts = [
            "default-src 'self'",
            script_src,
            *object_src,
            "style-src 'self' 'unsafe-inline'",
            f"img-src 'self' data:{keycloak}",
            "font-src 'self'",
            f"connect-src 'self'{keycloak}",
            f"form-action 'self'{keycloak}",
            f"frame-src 'self'{prometheus}",
            "frame-ancestors 'none'",
            "base-uri 'self'",
        ]
        return "; ".join(parts)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Before call_next: the templates read it while rendering (``csp_nonce()`` in
        # opi/http/render/templates_lotc.py).
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce

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
        # ZAP 90004: without this header another site may load a response from the portal
        # into a document of its own. The portal serves nothing for third parties.
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "geolocation=(), microphone=(), camera=()",
        )
        response.headers.setdefault("Content-Security-Policy", self._build_csp())
        # Measures what an enforced nonce policy would still block, without breaking anything.
        response.headers.setdefault("Content-Security-Policy-Report-Only", self._build_csp(nonce=nonce))

        return response
