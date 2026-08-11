"""
Tests for the security headers middleware and security.txt endpoint.
"""

import pytest
from opi.middleware.security_headers import SecurityHeadersMiddleware
from starlette.testclient import TestClient


class TestSecurityHeadersMiddleware:
    """Tests for SecurityHeadersMiddleware."""

    def test_extract_origin_full_url(self):
        """Extracts scheme+host from a full URL."""
        mw = SecurityHeadersMiddleware.__new__(SecurityHeadersMiddleware)
        assert mw._extract_origin("https://keycloak.example.com/realms/master") == "https://keycloak.example.com"

    def test_extract_origin_empty(self):
        """Returns empty string for empty input."""
        mw = SecurityHeadersMiddleware.__new__(SecurityHeadersMiddleware)
        assert mw._extract_origin("") == ""

    def test_extract_origin_bare_host(self):
        """Handles URL without path."""
        mw = SecurityHeadersMiddleware.__new__(SecurityHeadersMiddleware)
        assert mw._extract_origin("https://keycloak.kind") == "https://keycloak.kind"

    @staticmethod
    def _make_mw(keycloak_host: str = "", prometheus_host: str = "") -> SecurityHeadersMiddleware:
        mw = SecurityHeadersMiddleware.__new__(SecurityHeadersMiddleware)
        mw._keycloak_host = keycloak_host
        mw._prometheus_host = prometheus_host
        return mw

    def test_csp_includes_keycloak(self):
        """CSP connect-src and form-action include Keycloak origin."""
        mw = self._make_mw(keycloak_host="https://keycloak.example.com")
        csp = mw._build_csp()
        assert "connect-src 'self' https://keycloak.example.com" in csp
        assert "form-action 'self' https://keycloak.example.com" in csp

    def test_csp_without_keycloak(self):
        """CSP works without Keycloak URL."""
        mw = self._make_mw()
        csp = mw._build_csp()
        assert "connect-src 'self'" in csp
        assert "form-action 'self'" in csp
        # No trailing space after 'self'
        assert "connect-src 'self' " not in csp

    def test_csp_includes_jsdelivr(self):
        """CSP script-src includes jsdelivr for Chart.js."""
        mw = self._make_mw()
        csp = mw._build_csp()
        assert "https://cdn.jsdelivr.net" in csp

    def test_csp_does_not_include_unpkg(self):
        """unpkg.com stond in de CSP voor de HTMX die de oude schil van een CDN haalde.

        HTMX komt uit static/js/, dus die herkomst hoort niet meer toegestaan te zijn: een
        toestemming die niemand gebruikt is alleen nog een openstaande deur.
        """
        mw = self._make_mw()
        csp = mw._build_csp()
        assert "https://unpkg.com" not in csp

    def test_csp_img_src_includes_keycloak(self):
        """CSP img-src includes Keycloak origin for auth redirects."""
        mw = self._make_mw(keycloak_host="https://keycloak.example.com")
        csp = mw._build_csp()
        assert "img-src" in csp
        assert "https://keycloak.example.com" in csp.split("img-src")[1].split(";")[0]

    def test_csp_frame_ancestors_none(self):
        """CSP blocks framing via frame-ancestors 'none'."""
        mw = self._make_mw()
        csp = mw._build_csp()
        assert "frame-ancestors 'none'" in csp

    def test_csp_frame_src_includes_prometheus(self):
        """CSP frame-src allows the Prometheus origin so the metrics explorer
        iframe can load the external Prometheus UI."""
        mw = self._make_mw(prometheus_host="https://prometheus.example.com")
        csp = mw._build_csp()
        assert "frame-src 'self' https://prometheus.example.com" in csp

    def test_csp_frame_src_without_prometheus(self):
        """Without a configured Prometheus URL, frame-src is still emitted with
        just 'self' so iframes of same-origin content keep working and the
        directive is explicit rather than falling back to default-src."""
        mw = self._make_mw()
        csp = mw._build_csp()
        assert "frame-src 'self'" in csp
        assert "frame-src 'self' " not in csp


class TestSecurityHeadersIntegration:
    """Integration tests using the FastAPI test client."""

    @pytest.fixture
    def client(self):
        from fastapi import FastAPI
        from fastapi.responses import PlainTextResponse

        app = FastAPI()
        app.add_middleware(SecurityHeadersMiddleware, keycloak_url="https://keycloak.test")

        @app.get("/test")
        async def test_route():
            return PlainTextResponse("ok")

        return TestClient(app)

    def test_security_headers_present(self, client: TestClient):
        """All security headers are set on responses."""
        response = client.get("/test")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert "geolocation=()" in response.headers["Permissions-Policy"]
        assert "Content-Security-Policy" in response.headers

    def test_csp_header_value(self, client: TestClient):
        """CSP header contains expected directives."""
        response = client.get("/test")
        csp = response.headers["Content-Security-Policy"]
        assert "default-src 'self'" in csp
        assert "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net" in csp
        assert "https://keycloak.test" in csp

    def test_no_hsts_on_http(self, client: TestClient):
        """HSTS is not set for plain HTTP requests."""
        response = client.get("/test")
        assert "Strict-Transport-Security" not in response.headers

    def test_headers_do_not_override_existing(self):
        """Middleware does not overwrite headers already set by the application."""
        from fastapi import FastAPI
        from starlette.responses import Response

        app = FastAPI()
        app.add_middleware(SecurityHeadersMiddleware, keycloak_url="")

        @app.get("/custom")
        async def custom_route():
            resp = Response("ok")
            resp.headers["X-Frame-Options"] = "SAMEORIGIN"
            return resp

        client = TestClient(app)
        response = client.get("/custom")
        assert response.headers["X-Frame-Options"] == "SAMEORIGIN"


class TestSecurityTxt:
    """Tests for the /.well-known/security.txt endpoint."""

    @pytest.fixture
    def client(self):
        from opi.server import create_app

        app = create_app()
        return TestClient(app, raise_server_exceptions=False)

    def test_security_txt_redirects_to_ncsc(self, client: TestClient):
        response = client.get("/.well-known/security.txt", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == "https://www.ncsc.nl/.well-known/security.txt"
