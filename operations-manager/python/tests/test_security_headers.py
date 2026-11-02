"""
Tests for the security headers middleware and security.txt endpoint.
"""

import re

import pytest
from opi.middleware.security_headers import SecurityHeadersMiddleware

# Request niet in een TYPE_CHECKING-blok: FastAPI leest de annotatie van de route hieronder
# bij het starten, en zonder de import op runtime wordt Request een queryparameter.
from starlette.requests import Request  # noqa: TC002
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

    def test_csp_noemt_geen_enkele_externe_scripthost(self):
        """Chart.js, mermaid, Swagger-UI en ReDoc komen uit static/.

        Een host in script-src laat ELKE bibliotheek van die host toe, dus zodra
        'unsafe-inline' ooit vervalt zou cdn.jsdelivr.net een opening blijven.
        """
        mw = self._make_mw()
        csp = mw._build_csp()
        assert "cdn.jsdelivr.net" not in csp
        assert "fastapi.tiangolo.com" not in csp

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
        assert response.headers["Cross-Origin-Resource-Policy"] == "same-origin"
        assert "Content-Security-Policy" in response.headers

    def test_csp_header_value(self, client: TestClient):
        """CSP header contains expected directives."""
        response = client.get("/test")
        csp = response.headers["Content-Security-Policy"]
        assert "default-src 'self'" in csp
        assert "script-src 'self' 'unsafe-inline'" in csp
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


class TestCspNonce:
    """De nonce per verzoek: uniek, en in de HTML dezelfde als in de header."""

    @pytest.fixture
    def client(self):
        from fastapi import FastAPI
        from fastapi.responses import PlainTextResponse
        from opi.middleware.security_headers import csp_nonce

        app = FastAPI()
        app.add_middleware(SecurityHeadersMiddleware)

        @app.get("/nonce")
        async def nonce_route(request: Request) -> PlainTextResponse:
            return PlainTextResponse(csp_nonce(request))

        return TestClient(app)

    def test_elk_verzoek_krijgt_een_eigen_nonce(self, client: TestClient):
        eerste = client.get("/nonce")
        tweede = client.get("/nonce")

        assert eerste.text
        assert eerste.text != tweede.text

    def test_de_nonce_van_het_verzoek_staat_in_het_report_only_beleid(self, client: TestClient):
        response = client.get("/nonce")

        assert f"'nonce-{response.text}'" in response.headers["Content-Security-Policy-Report-Only"]

    def test_het_afgedwongen_beleid_draagt_geen_nonce(self, client: TestClient):
        """Een nonce in script-src zet 'unsafe-inline' uit, en daarmee elke event-handler."""
        response = client.get("/nonce")

        assert "nonce-" not in response.headers["Content-Security-Policy"]
        assert "'unsafe-inline'" in response.headers["Content-Security-Policy"]

    def test_report_only_sluit_object_src_uit(self, client: TestClient):
        """<object> is onder 'strict-dynamic' de weg terug naar scriptuitvoering."""
        response = client.get("/nonce")

        assert "object-src 'none'" in response.headers["Content-Security-Policy-Report-Only"]

    def test_report_only_is_strikt_en_verder_gelijk(self, client: TestClient):
        response = client.get("/nonce")
        afgedwongen = response.headers["Content-Security-Policy"]
        report_only = response.headers["Content-Security-Policy-Report-Only"]

        assert "'strict-dynamic'" in report_only
        assert "'unsafe-inline'" not in report_only.split("style-src")[0]

        # Alleen script-src en object-src verschillen; elke andere directive is woord voor
        # woord gelijk.
        def zonder_scripts(beleid: str) -> list[str]:
            return [deel for deel in beleid.split("; ") if not deel.startswith(("script-src", "object-src"))]

        assert zonder_scripts(afgedwongen) == zonder_scripts(report_only)


class TestNonceInDeHtml:
    """De nonce die de sjablonen rendeerden is de nonce uit de header van datzelfde
    antwoord. Zonder die gelijkheid meet het Report-Only-beleid alleen zijn eigen fout."""

    def test_de_landingspagina_rendeert_de_nonce_uit_de_header(self):
        from opi.server import create_app

        client = TestClient(create_app(), raise_server_exceptions=False)
        response = client.get("/introductie")
        assert response.status_code == 200, response.status_code

        nonces = set(re.findall(r'nonce="([^"]+)"', response.text))
        assert nonces, "geen enkel <script> op de landingspagina draagt een nonce"
        assert len(nonces) == 1, f"meer dan een nonce in een antwoord: {nonces}"

        assert f"'nonce-{nonces.pop()}'" in response.headers["Content-Security-Policy-Report-Only"]
