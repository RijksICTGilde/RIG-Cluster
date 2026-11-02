"""A 4xx answers in the language the caller asked for: a page for a browser, JSON for the rest."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import APIRouter, HTTPException
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from collections.abc import Mapping

HTML = {"accept": "text/html,application/xhtml+xml"}
JSON = {"accept": "application/json"}

REFUSAL = "Geen toegang tot dit project"


@pytest.fixture
def refusing_client(mock_settings: object) -> TestClient:
    """Routes that refuse like the portal does. Under /health/ because that prefix skips
    the authorization middleware; elsewhere an unauthenticated browser is sent to login
    before the handler is reached. The same refusal under /api/ to prove no markup leaks there.
    """
    from opi.server import create_app

    app = create_app()
    router = APIRouter()

    @router.get("/health/refused")
    async def _refused() -> None:
        raise HTTPException(status_code=403, detail=REFUSAL)

    @router.get("/api/refused")
    async def _refused_api() -> None:
        raise HTTPException(status_code=403, detail=REFUSAL)

    @router.get("/health/conflict")
    async def _conflict() -> None:
        raise HTTPException(status_code=409, detail={"code": "busy"}, headers={"Retry-After": "5"})

    app.include_router(router)
    return TestClient(app)


def test_a_browser_gets_a_page_for_a_404(test_client: TestClient) -> None:
    """Under /static/, because that prefix skips the authorization middleware."""
    response = test_client.get("/static/does-not-exist.png", headers=HTML)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "Deze pagina bestaat niet" in response.text


def test_a_browser_gets_a_page_for_a_403_with_the_refusal(refusing_client: TestClient) -> None:
    response = refusing_client.get("/health/refused", headers=HTML)
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("text/html")
    assert "Geen toegang" in response.text
    assert REFUSAL in response.text
    assert "vraag het dan aan een beheerder" in response.text


def test_a_detail_that_is_not_text_falls_back_and_headers_survive(refusing_client: TestClient) -> None:
    response = refusing_client.get("/health/conflict", headers=HTML)
    assert response.status_code == 409
    assert response.headers["content-type"].startswith("text/html")
    assert "Dit verzoek kan niet worden uitgevoerd" in response.text
    assert "busy" not in response.text
    assert response.headers["retry-after"] == "5"


@pytest.mark.parametrize(
    ("path", "headers"),
    [
        ("/api/refused", HTML),  # even from a browser: /api never answers with markup
        ("/health/refused", JSON),
    ],
)
def test_an_api_caller_keeps_its_json(refusing_client: TestClient, path: str, headers: Mapping[str, str]) -> None:
    response = refusing_client.get(path, headers=headers)
    assert response.status_code == 403
    assert response.json() == {"detail": REFUSAL}


def test_an_api_404_keeps_its_json(test_client: TestClient) -> None:
    response = test_client.get("/api/does-not-exist", headers=HTML)
    assert response.status_code == 404
    assert response.json()["detail"]


def test_an_api_401_keeps_its_json(test_client: TestClient) -> None:
    response = test_client.get("/api/v2/projects", headers=HTML)
    assert response.status_code == 401
    assert response.json()["detail"]
