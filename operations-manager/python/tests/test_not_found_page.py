"""A 404 answers in the language the caller asked for."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

HTML = {"accept": "text/html,application/xhtml+xml"}


def test_a_browser_gets_a_page(test_client: TestClient) -> None:
    """Under /static/, because that prefix skips the authorization middleware.

    Anywhere else an unauthenticated browser is sent to the login page before routing
    ever reaches a 404, so the path has to be one the middleware lets through to see
    the handler at all.
    """
    response = test_client.get("/static/does-not-exist.png", headers=HTML)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "Deze pagina bestaat niet" in response.text


def test_an_api_path_keeps_its_json(test_client: TestClient) -> None:
    """Even from a browser: a client parsing /api must not suddenly get markup."""
    response = test_client.get("/api/does-not-exist", headers=HTML)
    assert response.status_code == 404
    assert response.json()["detail"]


def test_a_caller_that_does_not_want_html_keeps_its_json(test_client: TestClient) -> None:
    response = test_client.get("/static/does-not-exist.png", headers={"accept": "application/json"})
    assert response.status_code == 404
    assert response.json()["detail"]


def test_other_errors_are_untouched(test_client: TestClient) -> None:
    """Only 404 is rewritten; a 401 keeps the body its callers expect."""
    response = test_client.get("/api/v2/projects", headers=HTML)
    assert response.status_code == 401
    assert response.json()["detail"]
