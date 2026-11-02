"""Clear-Site-Data once per browser, on an HTML page: the stale LOTC CSS of 2 November 2026."""

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.testclient import TestClient
from opi.middleware.cache_reset import CLEAR_SITE_DATA, COOKIE_NAME, COOKIE_VALUE, CacheResetMiddleware


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.add_middleware(CacheResetMiddleware)

    @app.get("/page")
    async def page() -> HTMLResponse:
        return HTMLResponse("<html></html>")

    @app.get("/static/x.css")
    async def asset() -> PlainTextResponse:
        return PlainTextResponse("body{}", media_type="text/css")

    return TestClient(app)


def test_first_page_clears_the_cache_and_sets_the_cookie(client: TestClient) -> None:
    response = client.get("/page")
    assert response.headers["clear-site-data"] == CLEAR_SITE_DATA
    assert response.cookies.get(COOKIE_NAME) == COOKIE_VALUE


def test_a_browser_with_the_cookie_keeps_its_cache(client: TestClient) -> None:
    client.cookies.set(COOKIE_NAME, COOKIE_VALUE)
    assert "clear-site-data" not in client.get("/page").headers


def test_an_older_cookie_value_clears_again(client: TestClient) -> None:
    client.cookies.set(COOKIE_NAME, "1999-01-01")
    assert client.get("/page").headers["clear-site-data"] == CLEAR_SITE_DATA


def test_an_asset_never_clears_the_cache(client: TestClient) -> None:
    """Only a page: a stylesheet that clears the cache would throw away the files next to it."""
    assert "clear-site-data" not in client.get("/static/x.css").headers


def test_the_real_app_sends_it_on_a_public_page() -> None:
    from opi.server import app

    response = TestClient(app).get("/introductie")
    assert response.status_code == 200
    assert response.headers["clear-site-data"] == CLEAR_SITE_DATA
