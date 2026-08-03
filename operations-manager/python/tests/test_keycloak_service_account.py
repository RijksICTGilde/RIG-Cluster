"""Unit tests for OPI's Keycloak client-credentials service account.

Covered without a live Keycloak:
- the factory picks client-credentials vs admin-password based on config,
- the master service-account client payload + role grant,
- the connection_works() probe.
"""

from unittest.mock import AsyncMock, MagicMock

from keycloak.exceptions import KeycloakError
from opi.connectors import keycloak as keycloak_mod
from opi.connectors.keycloak import KeycloakConnector, create_keycloak_connector
from opi.core.config import settings


async def test_factory_uses_client_credentials_when_secret_set(monkeypatch) -> None:
    captured: dict = {}

    class _FakeConn:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(keycloak_mod, "KeycloakConnector", _FakeConn)
    monkeypatch.setattr(settings, "KEYCLOAK_ADMIN_CLIENT_ID", "opi-admin-service")
    monkeypatch.setattr(settings, "KEYCLOAK_ADMIN_CLIENT_SECRET", "sekret")

    await create_keycloak_connector()

    assert captured["client_id"] == "opi-admin-service"
    assert captured["client_secret"] == "sekret"
    assert "admin_password" not in captured


async def test_factory_falls_back_to_admin_password_without_secret(monkeypatch) -> None:
    captured: dict = {}

    class _FakeConn:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(keycloak_mod, "KeycloakConnector", _FakeConn)
    monkeypatch.setattr(settings, "KEYCLOAK_ADMIN_CLIENT_SECRET", "")

    await create_keycloak_connector()

    assert captured["admin_username"] == settings.KEYCLOAK_ADMIN_USERNAME
    assert "client_secret" not in captured


async def test_factory_force_admin_password_even_with_secret(monkeypatch) -> None:
    captured: dict = {}

    class _FakeConn:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(keycloak_mod, "KeycloakConnector", _FakeConn)
    monkeypatch.setattr(settings, "KEYCLOAK_ADMIN_CLIENT_SECRET", "sekret")

    await create_keycloak_connector(use_client_credentials=False)

    assert "client_secret" not in captured
    assert captured["admin_username"] == settings.KEYCLOAK_ADMIN_USERNAME


async def test_ensure_master_service_account_client_creates_and_grants_admin() -> None:
    conn = object.__new__(KeycloakConnector)
    conn.admin = MagicMock()
    # First get_clients() -> not present; second (after create) -> present.
    conn.admin.get_clients.side_effect = [
        [],
        [{"clientId": "opi-admin-service", "id": "cid"}],
    ]
    conn.admin.get_client_service_account_user.return_value = {"id": "sa-uid"}
    conn.assign_realm_roles_to_user = AsyncMock(return_value={"assigned": ["admin"], "not_found": []})

    await conn.ensure_master_service_account_client("opi-admin-service", "sekret")

    payload = conn.admin.create_client.call_args.kwargs["payload"]
    assert payload["clientId"] == "opi-admin-service"
    assert payload["secret"] == "sekret"
    assert payload["serviceAccountsEnabled"] is True
    assert payload["standardFlowEnabled"] is False
    assert payload["directAccessGrantsEnabled"] is False

    realm, user_id, roles = conn.assign_realm_roles_to_user.await_args.args
    assert realm == "master"
    assert user_id == "sa-uid"
    assert roles == ["admin"]


async def test_ensure_master_service_account_client_updates_when_present() -> None:
    conn = object.__new__(KeycloakConnector)
    conn.admin = MagicMock()
    conn.admin.get_clients.return_value = [{"clientId": "opi-admin-service", "id": "cid"}]
    conn.admin.get_client_service_account_user.return_value = {"id": "sa-uid"}
    conn.assign_realm_roles_to_user = AsyncMock(return_value={"assigned": ["admin"], "not_found": []})

    await conn.ensure_master_service_account_client("opi-admin-service", "sekret")

    conn.admin.update_client.assert_called_once()
    conn.admin.create_client.assert_not_called()


async def test_connection_works_true_then_false() -> None:
    conn = object.__new__(KeycloakConnector)
    conn.admin = MagicMock()

    assert await conn.connection_works() is True

    conn.admin.get_realm.side_effect = KeycloakError("no auth")
    assert await conn.connection_works() is False
