"""The second opinion on a realm: the OIDC discovery document, served without a token."""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from opi.connectors import keycloak as keycloak_mod
from opi.connectors.keycloak import KeycloakConnector
from tests.unresponsive_server import unresponsive_server

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


async def _discovery(request: web.Request) -> web.Response:
    if request.match_info["realm"] == "rig-gone":
        return web.json_response({"error": "Realm does not exist"}, status=404)
    return web.json_response({"issuer": "https://kc/realms/rig-demo"})


@pytest.fixture
async def connector(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[KeycloakConnector]:
    app = web.Application()
    app.router.add_get("/realms/{realm}/.well-known/openid-configuration", _discovery)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setattr(keycloak_mod, "KeycloakOpenIDConnection", lambda **kw: MagicMock())
    monkeypatch.setattr(keycloak_mod, "KeycloakAdmin", lambda **kw: MagicMock())
    url = str(server.make_url("")).rstrip("/")
    try:
        yield KeycloakConnector(keycloak_url=url, client_id="opi-admin-service", client_secret="s")
    finally:
        await server.close()


async def test_a_served_document_confirms_the_realm(connector: KeycloakConnector) -> None:
    assert await connector.realm_discovery_available("rig-demo") is True


async def test_a_404_denies_the_realm(connector: KeycloakConnector) -> None:
    assert await connector.realm_discovery_available("rig-gone") is False


async def test_an_unreachable_keycloak_leaves_the_earlier_answer_standing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(keycloak_mod, "KeycloakOpenIDConnection", lambda **kw: MagicMock())
    monkeypatch.setattr(keycloak_mod, "KeycloakAdmin", lambda **kw: MagicMock())
    connector = KeycloakConnector(keycloak_url="http://127.0.0.1:1", client_id="opi-admin-service", client_secret="s")

    assert await connector.realm_discovery_available("rig-demo") is False


async def test_an_unresponsive_keycloak_also_leaves_the_earlier_answer_standing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read timeout is no aiohttp.ClientError, so it used to escape to the caller."""
    monkeypatch.setattr(keycloak_mod, "DISCOVERY_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(keycloak_mod, "KeycloakOpenIDConnection", lambda **kw: MagicMock())
    monkeypatch.setattr(keycloak_mod, "KeycloakAdmin", lambda **kw: MagicMock())

    with unresponsive_server() as url:
        connector = KeycloakConnector(keycloak_url=url, client_id="opi-admin-service", client_secret="s")
        started = monotonic()
        assert await asyncio.wait_for(connector.realm_discovery_available("rig-demo"), timeout=3) is False

    assert monotonic() - started < 1
