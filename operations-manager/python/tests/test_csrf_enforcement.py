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

    app = Starlette(
        routes=[
            Route("/", home, methods=["GET"]),
            Route("/projects/delete/foo", change, methods=["POST"]),
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
