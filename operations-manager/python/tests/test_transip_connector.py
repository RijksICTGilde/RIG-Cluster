"""The TransIP connector against a real local server: what it returns and how it fails."""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import TYPE_CHECKING

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from opi.connectors import transip
from opi.connectors.transip import TransIPConnector, TransIPError
from tests.unresponsive_server import unresponsive_server

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


async def _domains(request: web.Request) -> web.Response:
    return web.json_response({"domains": [{"name": "rijksapps.nl"}, {"name": "rijksapp.dev"}]})


async def _dns(request: web.Request) -> web.Response:
    if request.method == "POST":
        return web.Response(status=201)
    return web.json_response({"dnsEntries": [{"name": "@", "type": "CAA", "content": '0 issue "x"'}]})


async def _forbidden(request: web.Request) -> web.Response:
    return web.json_response({"error": "not whitelisted"}, status=403)


def _authenticated(base_url: str) -> TransIPConnector:
    """A connector with a token in hand, so a test exercises _request and not the signing."""
    connector = TransIPConnector("account", "unused-key", base_url=base_url)
    connector._token = "a-token"
    return connector


@pytest.fixture
async def connector() -> AsyncIterator[TransIPConnector]:
    app = web.Application()
    app.router.add_get("/domains", _domains)
    app.router.add_get("/domains/rijksapps.nl/dns", _dns)
    app.router.add_post("/domains/rijksapps.nl/dns", _dns)
    app.router.add_get("/domains/verboden.nl/dns", _forbidden)
    server = TestServer(app)
    await server.start_server()
    try:
        yield _authenticated(str(server.make_url("")).rstrip("/"))
    finally:
        await server.close()


async def test_list_domains_returns_the_names(connector: TransIPConnector) -> None:
    assert await connector.list_domains() == ["rijksapps.nl", "rijksapp.dev"]


async def test_get_dns_entries_returns_the_zone_as_transip_gives_it(connector: TransIPConnector) -> None:
    entries = await connector.get_dns_entries("rijksapps.nl")

    assert entries == [{"name": "@", "type": "CAA", "content": '0 issue "x"'}]


async def test_add_dns_entry_accepts_an_empty_body(connector: TransIPConnector) -> None:
    await connector.add_dns_entry("rijksapps.nl", "@", "CAA", '0 issue "x"', 300)


async def test_an_error_status_becomes_a_transip_error(connector: TransIPConnector) -> None:
    with pytest.raises(TransIPError, match="403"):
        await connector.get_dns_entries("verboden.nl")


async def test_an_unreachable_api_becomes_a_transip_error() -> None:
    """A caller that handles TransIPError should not have to know aiohttp as well."""
    with pytest.raises(TransIPError, match="/domains"):
        await _authenticated("http://127.0.0.1:1").list_domains()


async def test_an_unresponsive_api_does_not_hang_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a timeout on the session this waits for aiohttp's own five minutes."""
    monkeypatch.setattr(transip, "TRANSIP_TIMEOUT_SECONDS", 0.2)

    with unresponsive_server() as url:
        started = monotonic()
        with pytest.raises(TransIPError):
            await asyncio.wait_for(_authenticated(url).list_domains(), timeout=3)

    assert monotonic() - started < 1
