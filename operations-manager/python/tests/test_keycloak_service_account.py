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


class TestTokenRealmSurvivesARealmSwitch:
    """The service-account token must always be fetched from master.

    python-keycloak resolves the token realm as ``user_realm_name or realm_name``
    (openid_connection.py), and ``KeycloakAdmin.change_current_realm()`` -- which OPI
    calls for every project realm it touches -- reassigns ``realm_name``. Without
    ``user_realm_name`` pinned, the next token refresh asks a PROJECT realm for
    ``opi-admin-service``; that client only lives in master, so Keycloak answers
    ``client_not_found`` and every admin call starts failing with 401/403.

    Seen live on the sandbox: realm creation failing with 403 while Keycloak logged
    ``CLIENT_LOGIN_ERROR ... clientId="opi-admin-service" error="client_not_found"``
    against the ``rig-platform`` and ``operations-manager`` realms. It cannot fail at
    startup -- only after a realm switch plus a token expiry -- which is why the
    bootstrap looked healthy.
    """

    def test_client_credentials_connection_pins_the_token_realm(self, monkeypatch) -> None:
        captured: dict = {}

        class _FakeConnection:
            def __init__(self, **kwargs) -> None:
                captured.update(kwargs)

        monkeypatch.setattr(keycloak_mod, "KeycloakOpenIDConnection", _FakeConnection)
        monkeypatch.setattr(keycloak_mod, "KeycloakAdmin", lambda **kwargs: MagicMock())

        KeycloakConnector(keycloak_url="https://kc.example", client_id="opi-admin-service", client_secret="sekret")

        assert captured["user_realm_name"] == "master", (
            "client-credentials connection does not pin user_realm_name to master; a "
            "change_current_realm() would send the next token request to a project realm"
        )

    def test_the_token_realm_still_resolves_to_master_after_change_current_realm(self, monkeypatch) -> None:
        # The behaviour the pin buys, reproduced against python-keycloak's own resolution
        # rule rather than trusting the constructor argument in isolation.
        captured: dict = {}

        class _FakeConnection:
            def __init__(self, **kwargs) -> None:
                captured.update(kwargs)
                self.realm_name = kwargs.get("realm_name")
                self.user_realm_name = kwargs.get("user_realm_name")

            def change_current_realm(self, realm: str) -> None:
                self.realm_name = realm

            @property
            def token_realm(self) -> str:
                return self.user_realm_name or self.realm_name or "master"

        holder: dict = {}

        def _fake_admin(**kwargs):
            holder["connection"] = kwargs.get("connection")
            return MagicMock()

        monkeypatch.setattr(keycloak_mod, "KeycloakOpenIDConnection", _FakeConnection)
        monkeypatch.setattr(keycloak_mod, "KeycloakAdmin", _fake_admin)

        KeycloakConnector(keycloak_url="https://kc.example", client_id="opi-admin-service", client_secret="sekret")
        connection = holder["connection"]

        connection.change_current_realm("rig-platform")
        assert connection.token_realm == "master", (
            "after switching to a project realm the token would be requested there, "
            "where opi-admin-service does not exist"
        )

    def test_admin_password_path_keeps_pinning_master(self, monkeypatch) -> None:
        # The path that was always correct; asserted so the two cannot drift apart again.
        captured: dict = {}
        monkeypatch.setattr(keycloak_mod, "KeycloakAdmin", lambda **kwargs: captured.update(kwargs) or MagicMock())

        KeycloakConnector(keycloak_url="https://kc.example", admin_username="admin", admin_password="pw")

        assert captured["user_realm_name"] == "master"


class TestTokenIsRefreshedAfterRealmCreation:
    """A token minted before a realm exists cannot administer that realm.

    Keycloak creates a ``<realm>-realm`` client in master alongside every new realm and
    carries its admin roles in the token's ``resource_access``. A token issued earlier
    does not have them, so the very next call on the fresh realm answers 403.

    Measured on the sandbox with one service account holding master 'admin':
    ``POST /admin/realms`` -> 201, then ``GET /admin/realms/<new>/users/profile`` -> 403
    with the token that created it and 200 with a token minted a second later. An admin
    USER is immune (super-admin covers realms created after its token), which is why this
    only appeared once OPI moved to a client-credentials service account.
    """

    def _connector(self, monkeypatch) -> tuple[KeycloakConnector, MagicMock]:
        admin = MagicMock()
        monkeypatch.setattr(keycloak_mod, "KeycloakOpenIDConnection", lambda **kw: MagicMock())
        monkeypatch.setattr(keycloak_mod, "KeycloakAdmin", lambda **kw: admin)
        conn = KeycloakConnector(keycloak_url="https://kc.example", client_id="opi-admin-service", client_secret="s")
        return conn, admin

    async def test_realm_creation_refreshes_the_token_before_touching_the_realm(self, monkeypatch) -> None:
        conn, admin = self._connector(monkeypatch)
        calls: list[str] = []
        admin.create_realm.side_effect = lambda **kw: calls.append("create_realm")
        admin.connection.get_token.side_effect = lambda: calls.append("get_token")
        admin.get_realm.side_effect = lambda **kw: calls.append("get_realm") or {}
        admin.get_realm_users_profile.side_effect = lambda: calls.append("users_profile") or {"attributes": []}

        await conn.create_realm(realm_name="proj-cluster")

        assert "get_token" in calls, "no token refresh after creating the realm"
        assert calls.index("create_realm") < calls.index("get_token") < calls.index("users_profile"), (
            f"the refresh must sit between creating the realm and using it, got {calls}"
        )

    async def test_a_failing_refresh_does_not_mask_the_real_error(self, monkeypatch) -> None:
        # Best-effort: if the refresh itself fails the caller carries on, so whatever goes
        # wrong next reports its own error instead of this one.
        conn, admin = self._connector(monkeypatch)
        admin.connection.get_token.side_effect = KeycloakError("token endpoint down")
        admin.get_realm.return_value = {"realm": "proj-cluster"}
        admin.get_realm_users_profile.return_value = {"attributes": []}

        result = await conn.create_realm(realm_name="proj-cluster")

        assert result is not None
