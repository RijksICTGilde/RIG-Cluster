"""OPI komt bij ArgoCD binnen met het API-token van het account `opi` (RC-345).

Het admin-account was tot nu toe de weg, met `admin`/`admin` als default: een vergeten
instelling probeerde dus stil het beheeraccount. Deze toetsen pinnen de drie eigenschappen
van de tokenweg vast:

  - met een token wordt er niet ingelogd: geen POST naar /api/v1/session;
  - elke aanroep draagt het token als bearer, ook een aanroep van een tweede connector;
  - zonder token en zonder wachtwoord weigert de fabriek met een duidelijke fout, in plaats
    van een poging met admin/admin.
"""

from unittest.mock import AsyncMock, patch

import pytest
from opi.connectors import argo as argo_module
from opi.connectors.argo import ArgoAuthConfigurationError, ArgoConnector, create_argo_connector


@pytest.fixture(autouse=True)
def _clear_token_cache():
    argo_module._token_cache.clear()
    yield
    argo_module._token_cache.clear()


@pytest.mark.asyncio
async def test_een_token_slaat_de_login_over() -> None:
    connector = ArgoConnector(token="token-opi")

    with patch.object(ArgoConnector, "_login_once", AsyncMock(return_value=True)) as login:
        assert await connector.ensure_authenticated() is True
        assert await connector.login() is True

    login.assert_not_awaited()
    assert connector.auth_token == "token-opi"


def test_een_token_komt_niet_in_de_gedeelde_cache() -> None:
    """De cache bestaat om logins te vermijden; een token hoeft hij niet te kennen, en een
    tweede connector krijgt hem toch uit de configuratie."""
    ArgoConnector(token="token-opi")

    assert argo_module._token_cache == {}


class _Antwoord:
    """Het antwoord van aiohttp als async context manager, zonder netwerk."""

    status = 200

    async def __aenter__(self) -> _Antwoord:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def text(self) -> str:
        return "[]"


@pytest.mark.asyncio
async def test_elke_aanroep_draagt_het_token_als_bearer() -> None:
    connector = ArgoConnector(token="token-opi")
    gezien: list[dict[str, str]] = []

    def _request(_self, _method, _url, **kwargs):
        gezien.append(kwargs["headers"])
        return _Antwoord()

    with patch("aiohttp.ClientSession.request", _request):
        status, _text = await connector._make_authenticated_request("GET", "http://argocd/api/v1/applications")

    assert status == 200
    assert gezien, "er ging geen aanroep uit"
    assert gezien[0]["Authorization"] == "Bearer token-opi"


@pytest.mark.asyncio
async def test_een_afgewezen_token_leidt_niet_tot_een_login() -> None:
    """Een 401 op een token betekent dat het token is ingetrokken of verlopen; opnieuw
    aanbieden of inloggen helpt niet, en een loginpoging zou met None-credentials gaan."""
    connector = ArgoConnector(token="token-opi")

    with (
        patch.object(argo_module, "retry_on_connect_error", AsyncMock(return_value=(401, "token revoked"))),
        patch.object(ArgoConnector, "login", AsyncMock(return_value=True)) as login,
    ):
        status, text = await connector._make_authenticated_request("GET", "http://argocd/api/v1/applications")

    login.assert_not_awaited()
    assert status == 401
    assert "revoked" in text


def test_de_fabriek_weigert_zonder_token_en_zonder_wachtwoord(monkeypatch: pytest.MonkeyPatch) -> None:
    from opi.core.config import settings

    monkeypatch.setattr(settings, "ARGOCD_TOKEN", None)
    monkeypatch.setattr(settings, "ARGOCD_PASSWORD", None)
    monkeypatch.setattr(settings, "ARGOCD_USERNAME", None)

    with pytest.raises(ArgoAuthConfigurationError):
        create_argo_connector()


@pytest.mark.asyncio
async def test_het_token_wint_van_een_wachtwoord() -> None:
    """De sandbox draagt beide: een token zodra het is uitgegeven, en het admin-account als
    terugval. Dan mag er geen login meer uitgaan, anders is de terugval geen terugval maar de
    weg."""
    connector = ArgoConnector(token="token-opi", username="admin", password="admin1234")

    with patch.object(ArgoConnector, "_login_once", AsyncMock(return_value=True)) as login:
        assert await connector.login() is True

    login.assert_not_awaited()
    assert connector.auth_token == "token-opi"


def test_de_fabriek_kiest_het_token_boven_het_wachtwoord(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zoals de sandbox-overlay hem zet: alle drie gevuld, en het token is de autorisatie."""
    from opi.core.config import settings

    monkeypatch.setattr(settings, "ARGOCD_TOKEN", "token-opi")
    monkeypatch.setattr(settings, "ARGOCD_USERNAME", "admin")
    monkeypatch.setattr(settings, "ARGOCD_PASSWORD", "admin1234")

    connector = create_argo_connector()

    assert connector.auth_token == "token-opi"


def test_de_fabriek_geeft_het_token_door(monkeypatch: pytest.MonkeyPatch) -> None:
    from opi.core.config import settings

    monkeypatch.setattr(settings, "ARGOCD_TOKEN", "token-opi")
    monkeypatch.setattr(settings, "ARGOCD_PASSWORD", None)
    monkeypatch.setattr(settings, "ARGOCD_USERNAME", None)

    connector = create_argo_connector()

    assert connector.token == "token-opi"
    assert connector.auth_token == "token-opi"
