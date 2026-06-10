"""Tests for central CSRF enforcement in CSRFMiddleware.

These exercise the middleware directly on a minimal Starlette app so the
heavy application import chain (jinja-roos) is not required.

Red-green coverage:
- a state-changing POST without a valid CSRF token is rejected (403)
- a state-changing POST with a foreign Origin is rejected (403)
- a state-changing POST with a valid token + matching Origin passes
- the Referer fallback rejects a foreign Referer and accepts a matching one
- a GET still mints/seeds the CSRF cookie; a POST response keeps it usable
- /api/ routes are exempt (API-key auth, not cookie/session based)
- the module imports at runtime (no __future__ annotations regression)
"""

from typing import Any
from unittest.mock import patch

import pytest
from opi.utils.csrf import CSRF_COOKIE_NAME, CSRFMiddleware
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    async def home(request: Any) -> PlainTextResponse:
        return PlainTextResponse("ok")

    async def change(request: Any) -> JSONResponse:
        return JSONResponse({"changed": True})

    async def api_change(request: Any) -> JSONResponse:
        return JSONResponse({"api": True})

    async def echo_form(request: Any) -> JSONResponse:
        # Reads the form body the same way real handlers do. Under
        # BaseHTTPMiddleware the body must survive the middleware's own
        # form parse, otherwise these fields come back empty.
        form = await request.form()
        return JSONResponse({"keys": sorted(form.keys()), "email": form.get("email")})

    app = Starlette(
        routes=[
            Route("/", home, methods=["GET"]),
            Route("/projects/delete/foo", change, methods=["POST"]),
            Route("/admin/users/create", echo_form, methods=["POST"]),
            Route("/api/v2/projects", api_change, methods=["POST"]),
        ]
    )
    app.add_middleware(CSRFMiddleware)

    # validate_csrf_origin reads settings.DEBUG; force production-like behavior
    # (no localhost bypass) so the Origin check is actually exercised.
    with patch("opi.core.config.settings") as mock_settings:
        mock_settings.DEBUG = False
        # raise_server_exceptions=False so a bubbled error surfaces as 500,
        # not a pytest crash; the middleware returns clean 403s anyway.
        with TestClient(app, base_url="http://opi.example.nl") as test_client:
            yield test_client


class TestCsrfEnforcement:
    def test_get_seeds_csrf_cookie(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert CSRF_COOKIE_NAME in response.cookies
        assert response.cookies[CSRF_COOKIE_NAME]

    def test_post_without_token_is_rejected(self, client: TestClient) -> None:
        # Seed a cookie via GET first, then POST without echoing it back.
        client.get("/")
        response = client.post(
            "/projects/delete/foo",
            headers={"Origin": "http://opi.example.nl"},
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "CSRF token missing"

    def test_post_with_foreign_origin_is_rejected(self, client: TestClient) -> None:
        client.get("/")
        token = client.cookies[CSRF_COOKIE_NAME]
        response = client.post(
            "/projects/delete/foo",
            headers={
                "X-CSRF-Token": token,
                "Origin": "http://attacker.example.nl",
            },
        )
        assert response.status_code == 403
        assert "invalid origin" in response.json()["detail"]

    def test_post_with_token_mismatch_is_rejected(self, client: TestClient) -> None:
        client.get("/")
        response = client.post(
            "/projects/delete/foo",
            headers={
                "X-CSRF-Token": "not-the-cookie-value",
                "Origin": "http://opi.example.nl",
            },
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "CSRF token invalid"

    def test_post_with_valid_token_and_origin_passes(self, client: TestClient) -> None:
        client.get("/")
        token = client.cookies[CSRF_COOKIE_NAME]
        response = client.post(
            "/projects/delete/foo",
            headers={
                "X-CSRF-Token": token,
                "Origin": "http://opi.example.nl",
            },
        )
        assert response.status_code == 200
        assert response.json() == {"changed": True}

    def test_post_with_valid_token_via_form_field_passes(self, client: TestClient) -> None:
        client.get("/")
        token = client.cookies[CSRF_COOKIE_NAME]
        response = client.post(
            "/projects/delete/foo",
            data={"csrf_token": token},
            headers={"Origin": "http://opi.example.nl"},
        )
        assert response.status_code == 200

    def test_form_body_reaches_handler_after_token_parse(self, client: TestClient) -> None:
        # Regression: a plain <form> POST carries its CSRF token in a form
        # field, so the middleware parses the body to validate it. Under
        # BaseHTTPMiddleware that parse must not consume the body away from
        # the downstream handler, otherwise every field reads as empty and
        # required-field validation fails ("Dit veld is verplicht") even
        # though the user filled them in.
        client.get("/")
        token = client.cookies[CSRF_COOKIE_NAME]
        response = client.post(
            "/admin/users/create",
            data={"csrf_token": token, "email": "a@b.nl", "full_name": "A B"},
            headers={"Origin": "http://opi.example.nl"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["email"] == "a@b.nl"
        assert body["keys"] == ["csrf_token", "email", "full_name"]

    def test_post_without_origin_or_referer_is_rejected(self, client: TestClient) -> None:
        client.get("/")
        token = client.cookies[CSRF_COOKIE_NAME]
        response = client.post(
            "/projects/delete/foo",
            headers={"X-CSRF-Token": token},
        )
        # httpx may not send Origin; Referer also absent -> rejected.
        assert response.status_code == 403
        assert "missing origin information" in response.json()["detail"]

    def test_api_route_is_exempt(self, client: TestClient) -> None:
        # No CSRF token, foreign origin: still allowed because /api/ uses
        # API-key auth and is not cookie/session based.
        response = client.post(
            "/api/v2/projects",
            headers={"Origin": "http://attacker.example.nl"},
        )
        assert response.status_code == 200
        assert response.json() == {"api": True}

    def test_post_with_foreign_referer_is_rejected(self, client: TestClient) -> None:
        # When no Origin is sent the middleware falls back to Referer; a
        # cross-site Referer must still be rejected.
        client.get("/")
        token = client.cookies[CSRF_COOKIE_NAME]
        response = client.post(
            "/projects/delete/foo",
            headers={
                "X-CSRF-Token": token,
                "Referer": "http://attacker.example.nl/x",
            },
        )
        assert response.status_code == 403
        assert "invalid referer" in response.json()["detail"]

    def test_post_with_matching_referer_passes(self, client: TestClient) -> None:
        client.get("/")
        token = client.cookies[CSRF_COOKIE_NAME]
        response = client.post(
            "/projects/delete/foo",
            headers={
                "X-CSRF-Token": token,
                "Referer": "http://opi.example.nl/projects",
            },
        )
        assert response.status_code == 200

    def test_get_seeded_cookie_is_usable_for_subsequent_post(self, client: TestClient) -> None:
        # Real flow: a GET renders the page and seeds the cookie; the cookie
        # the browser holds is exactly what a later POST must echo back.
        get_response = client.get("/")
        seeded = get_response.cookies[CSRF_COOKIE_NAME]
        post_response = client.post(
            "/projects/delete/foo",
            headers={"X-CSRF-Token": seeded, "Origin": "http://opi.example.nl"},
        )
        assert post_response.status_code == 200
        assert post_response.json() == {"changed": True}


# ---------------------------------------------------------------------------
# Exempt-list scope: exact match for probe routes, slash-suffixed prefixes
# for directory-style subtrees. Bare-prefix matching is NOT supported.
# ---------------------------------------------------------------------------


@pytest.fixture
def probe_client() -> TestClient:
    """A client whose app exposes both probe routes and lookalike user pages
    so we can prove the exempt list does not over-match."""

    async def probe(request: Any) -> JSONResponse:
        return JSONResponse({"probe": True})

    async def lookalike(request: Any) -> JSONResponse:
        return JSONResponse({"lookalike": True})

    app = Starlette(
        routes=[
            # Real probe routes (these MUST be exempt).
            Route("/health", probe, methods=["GET", "POST"]),
            Route("/healthz", probe, methods=["GET", "POST"]),
            Route("/readyz", probe, methods=["GET", "POST"]),
            Route("/metrics", probe, methods=["GET", "POST"]),
            # Lookalike user pages (these MUST NOT be exempt).
            Route("/metrics-explorer", lookalike, methods=["POST"]),
            Route("/healthcheck-admin", lookalike, methods=["POST"]),
            Route("/readyness-report", lookalike, methods=["POST"]),
            # Invite registration (NOT under /api/ -- must be enforced).
            Route("/invite/abc123/register", lookalike, methods=["POST"]),
        ]
    )
    app.add_middleware(CSRFMiddleware)

    with patch("opi.core.config.settings") as mock_settings:
        mock_settings.DEBUG = False
        with TestClient(app, base_url="http://opi.example.nl") as test_client:
            yield test_client


class TestExemptListScope:
    @pytest.mark.parametrize("path", ["/health", "/healthz", "/readyz", "/metrics"])
    def test_probe_routes_are_exempt(self, probe_client: TestClient, path: str) -> None:
        """The named probe paths must be exempt (POST without token succeeds)."""
        response = probe_client.post(path, headers={"Origin": "http://attacker.example.nl"})
        assert response.status_code == 200
        assert response.json() == {"probe": True}

    @pytest.mark.parametrize(
        "path",
        [
            "/metrics-explorer",
            "/healthcheck-admin",
            "/readyness-report",
        ],
    )
    def test_lookalike_paths_are_not_exempt(self, probe_client: TestClient, path: str) -> None:
        """A user-facing page that shares a prefix with a probe MUST still be
        CSRF-protected. Previously bare-prefix matching exempted these."""
        probe_client.get("/health")  # seed cookie via the exempt probe
        response = probe_client.post(path, headers={"Origin": "http://opi.example.nl"})
        assert response.status_code == 403
        assert response.json()["detail"] == "CSRF token missing"

    def test_invite_register_is_not_exempt(self, probe_client: TestClient) -> None:
        """The invite-registration POST goes through CSRF; the template must
        therefore include csrf.js so the form has a token.

        This pins the policy. The corresponding template fix (adding
        additionalJs='<script src=/static/js/csrf.js></script>' on the
        invite-register c-page) is in the same commit.
        """
        probe_client.get("/health")
        response = probe_client.post(
            "/invite/abc123/register",
            headers={"Origin": "http://opi.example.nl"},
            data={"email": "x@y.nl"},
        )
        assert response.status_code == 403


class TestJsonPostWithHeaderToken:
    """The wizard uses htmx + json-enc.js, which POSTs Content-Type:
    application/json. The middleware must accept the token via X-CSRF-Token
    header on JSON requests just like on form requests."""

    def test_json_post_with_header_token_passes(self, client: TestClient) -> None:
        client.get("/")
        token = client.cookies[CSRF_COOKIE_NAME]
        response = client.post(
            "/projects/delete/foo",
            json={"action": "delete"},
            headers={
                "X-CSRF-Token": token,
                "Origin": "http://opi.example.nl",
            },
        )
        assert response.status_code == 200
        assert response.json() == {"changed": True}

    def test_json_post_without_token_is_rejected(self, client: TestClient) -> None:
        client.get("/")
        response = client.post(
            "/projects/delete/foo",
            json={"action": "delete"},
            headers={"Origin": "http://opi.example.nl"},
        )
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Server-rendered token: middleware exposes request.state.csrf_token so
# templates render it directly into hx-headers / hidden fields, and the
# cookie can stay httponly=True (JavaScript never reads it).
# ---------------------------------------------------------------------------


class TestServerRenderedToken:
    def test_state_csrf_token_is_set_on_get(self) -> None:
        """Middleware must expose the token on request.state for templates."""
        captured: dict[str, Any] = {}

        async def endpoint(request: Any) -> PlainTextResponse:
            captured["state_token"] = request.state.csrf_token
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/", endpoint, methods=["GET"])])
        app.add_middleware(CSRFMiddleware)

        with (
            patch("opi.core.config.settings") as mock_settings,
            TestClient(app, base_url="http://opi.example.nl") as client,
        ):
            mock_settings.DEBUG = False
            client.get("/")

        assert captured["state_token"]
        assert len(captured["state_token"]) > 20

    def test_state_csrf_token_matches_cookie_on_subsequent_requests(self) -> None:
        """A second request reuses the cookie value -- one token per session,
        consistent across all tabs/forms that render at different moments."""
        seen: list[str] = []

        async def endpoint(request: Any) -> PlainTextResponse:
            seen.append(request.state.csrf_token)
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/", endpoint, methods=["GET"])])
        app.add_middleware(CSRFMiddleware)

        with (
            patch("opi.core.config.settings") as mock_settings,
            TestClient(app, base_url="http://opi.example.nl") as client,
        ):
            mock_settings.DEBUG = False
            client.get("/")
            client.get("/")
            client.get("/")

        assert len(seen) == 3
        # Same token across requests in the same session (cookie reuse).
        assert seen[0] == seen[1] == seen[2]

    def test_cookie_is_httponly(self) -> None:
        """The CSRF cookie is httponly=True; templates render the token
        server-side, so JavaScript never needs to read document.cookie."""

        async def endpoint(request: Any) -> PlainTextResponse:
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/", endpoint, methods=["GET"])])
        app.add_middleware(CSRFMiddleware)

        with (
            patch("opi.core.config.settings") as mock_settings,
            TestClient(app, base_url="http://opi.example.nl") as client,
        ):
            mock_settings.DEBUG = False
            response = client.get("/")

        set_cookie = response.headers.get("set-cookie", "")
        assert CSRF_COOKIE_NAME in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=strict" in set_cookie.lower() or "samesite=strict" in set_cookie.lower()


def test_csrf_module_has_no_unresolved_runtime_annotations() -> None:
    """Regression: dispatch() is annotated with Callable/Awaitable/Response.

    The module has no ``from __future__ import annotations``, so those names
    must be importable at runtime, not only under TYPE_CHECKING. Importing the
    module fresh must not raise NameError.
    """
    import importlib

    import opi.utils.csrf as csrf_module

    importlib.reload(csrf_module)
    assert hasattr(csrf_module, "CSRFMiddleware")
    assert "from __future__ import annotations" not in (csrf_module.__doc__ or "")
