"""Een ArgoConnector bouwen doet geen netwerkverkeer.

WAT ER MISGING

``__init__`` logde synchroon in met ``requests.post`` (timeout 10 s, ongeveer 700 ms aan
bcrypt) zolang de procesbrede token-cache leeg was. Dat gebeurde op de event loop, bij elk
van de 22 aanroepers van ``create_argo_connector``, en zolang ArgoCD onbereikbaar was
kostte elke constructie de volle timeout. ``ensure_authenticated`` logt async in, met
lock, op het moment dat de token echt nodig is.
"""

from __future__ import annotations

import ssl
from unittest.mock import AsyncMock, patch

import pytest
from opi.connectors import argo as argo_module
from opi.connectors.argo import ArgoConnector


@pytest.fixture(autouse=True)
def _lege_token_cache():
    argo_module._token_cache.clear()
    yield
    argo_module._token_cache.clear()


def test_de_constructor_praat_niet_met_het_netwerk() -> None:
    """Geen requests, geen aiohttp: de constructor zet alleen velden."""
    with patch("aiohttp.ClientSession", side_effect=AssertionError("de constructor opende een sessie")):
        connector = ArgoConnector()

    assert connector.auth_token is None
    assert not hasattr(ArgoConnector, "_perform_login"), "de synchrone login hoort weg te zijn"


def test_requests_is_niet_meer_geimporteerd() -> None:
    """De synchrone HTTP-client had hier geen andere gebruiker."""
    assert not hasattr(argo_module, "requests")


@pytest.mark.asyncio
async def test_de_eerste_async_aanroep_logt_in() -> None:
    connector = ArgoConnector()

    async def _login(self: ArgoConnector) -> bool:
        self.auth_token = "token-1"
        self._store_token("token-1")
        return True

    with patch.object(ArgoConnector, "login", _login):
        assert await connector.ensure_authenticated() is True

    assert connector.auth_token == "token-1"


@pytest.mark.asyncio
async def test_een_tweede_connector_neemt_de_token_over_zonder_login() -> None:
    ArgoConnector()._store_token("token-1")

    tweede = ArgoConnector()
    with patch.object(ArgoConnector, "login", AsyncMock(side_effect=AssertionError("onnodige login"))):
        assert await tweede.ensure_authenticated() is True

    assert tweede.auth_token == "token-1"


def test_de_ssl_context_wordt_een_keer_gemaakt() -> None:
    """``ssl.create_default_context`` las de CA-bundel van schijf, per request."""
    argo_module._ssl_context_voor.cache_clear()

    with patch("ssl.create_default_context", wraps=ssl.create_default_context) as maak:
        eerste = ArgoConnector(verify_ssl=True)._ssl_context()
        tweede = ArgoConnector(verify_ssl=True)._ssl_context()

    assert eerste is tweede
    assert maak.call_count == 1


def test_een_andere_verify_stand_krijgt_een_eigen_context() -> None:
    assert ArgoConnector(verify_ssl=True)._ssl_context() is not ArgoConnector(verify_ssl=False)._ssl_context()
