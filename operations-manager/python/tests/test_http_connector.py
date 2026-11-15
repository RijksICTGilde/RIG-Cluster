"""HttpConnector against a real local server: what ``request`` returns, and that it gives up."""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import TYPE_CHECKING

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from opi.connectors import http as http_connector
from opi.connectors.http import HttpConnector
from tests.unresponsive_server import unresponsive_server

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


async def _json_body(request: web.Request) -> web.Response:
    return web.json_response({"ok": True}, status=201)


async def _text_body(request: web.Request) -> web.Response:
    return web.Response(text="plain words", status=200)


@pytest.fixture
async def server_url() -> AsyncIterator[str]:
    app = web.Application()
    app.router.add_get("/json", _json_body)
    app.router.add_get("/text", _text_body)
    server = TestServer(app)
    await server.start_server()
    try:
        yield str(server.make_url("")).rstrip("/")
    finally:
        await server.close()


async def test_json_response_is_parsed(server_url: str) -> None:
    async with HttpConnector(server_url) as connector:
        status, data = await connector.request("GET", "/json")

    assert status == 201
    assert data == {"ok": True}


async def test_non_json_response_comes_back_as_text(server_url: str) -> None:
    async with HttpConnector(server_url) as connector:
        status, data = await connector.request("GET", "/text")

    assert status == 200
    assert data == "plain words"


async def test_an_unresponsive_server_does_not_hang_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a timeout on the session this waits for aiohttp's own five minutes."""
    monkeypatch.setattr(http_connector, "HTTP_TIMEOUT_SECONDS", 0.2)

    with unresponsive_server() as url:
        started = monotonic()
        async with HttpConnector(url) as connector:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(connector.request("GET", "/json"), timeout=3)

    assert monotonic() - started < 1
