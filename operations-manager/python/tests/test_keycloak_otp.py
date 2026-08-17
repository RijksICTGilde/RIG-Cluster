"""Unit tests for shared-OTP provisioning on Keycloak realm-admin accounts.

These avoid a live Keycloak by bypassing the connecting __init__ and mocking the
underlying KeycloakAdmin, asserting on the payload sent to create_user, and on
the KEYCLOAK_ENFORCE_ADMIN_OTP gate for the retrofit path.
"""

from unittest.mock import AsyncMock, MagicMock

from opi.connectors.keycloak import KeycloakConnector
from opi.core.config import settings
from opi.manager.keycloak_manager import KeycloakManager
from opi.services.catalog.keycloak.config_model import KeycloakRealm


def test_keycloak_realm_accepts_totp_secret() -> None:
    """The realm-admin connection model carries the shared OTP seed as a string."""
    realm = KeycloakRealm(
        host="https://kc.example",
        realm="proj-local",
        username="proj_local_admin",
        password="plain:pw",
        totp_secret="plain:seed",
    )
    assert realm.totp_secret == "plain:seed"


def test_keycloak_realm_totp_secret_defaults_none() -> None:
    """Pre-OTP realms have no totp_secret; it must default to None, not error."""
    realm = KeycloakRealm(
        host="https://kc.example",
        realm="proj-local",
        username="proj_local_admin",
        password="plain:pw",
    )
    assert realm.totp_secret is None


def _connector_with_mock_admin() -> tuple[KeycloakConnector, dict]:
    conn = object.__new__(KeycloakConnector)
    conn.admin = MagicMock()
    captured: dict = {}

    def _create_user(payload: dict) -> None:
        captured["payload"] = payload

    conn.admin.create_user.side_effect = _create_user
    conn.get_user_by_username = AsyncMock(return_value={"id": "user-id", "username": "u"})
    return conn, captured


async def test_create_user_with_totp_appends_otp_credential() -> None:
    conn, captured = _connector_with_mock_admin()

    await conn.create_user("master", "u", "pw", totp_secret="RAWSECRET")

    creds = captured["payload"]["credentials"]
    assert [c["type"] for c in creds] == ["password", "otp"]
    assert creds[0]["value"] == "pw"
    assert creds[1]["userLabel"] == "ZAD shared OTP"


async def test_create_user_without_totp_has_only_password() -> None:
    conn, captured = _connector_with_mock_admin()

    await conn.create_user("master", "u", "pw")

    creds = captured["payload"]["credentials"]
    assert [c["type"] for c in creds] == ["password"]


async def test_ensure_admin_otp_noop_when_disabled(monkeypatch) -> None:
    """With KEYCLOAK_ENFORCE_ADMIN_OTP off (default), the retrofit does nothing."""
    monkeypatch.setattr(settings, "KEYCLOAK_ENFORCE_ADMIN_OTP", False)

    mgr = object.__new__(KeycloakManager)
    mgr.project_manager = MagicMock()
    mgr.project_manager.get_contents = AsyncMock()

    await mgr._ensure_admin_otp("proj", "local", "proj-local", "https://kc.example")

    mgr.project_manager.get_contents.assert_not_awaited()


async def test_ensure_admin_otp_noop_when_already_provisioned(monkeypatch) -> None:
    """When the realm already carries a totp_secret, the retrofit short-circuits
    without touching Keycloak or saving."""
    monkeypatch.setattr(settings, "KEYCLOAK_ENFORCE_ADMIN_OTP", True)

    project_data = {
        "services": [
            {
                "keycloak": {
                    "config": {
                        "realms": [
                            {
                                "host": "https://kc.example",
                                "realm": "proj-local",
                                "username": "proj_local_admin",
                                "password": "plain:pw",
                                "totp_secret": "plain:already",
                            }
                        ]
                    }
                }
            }
        ]
    }

    mgr = object.__new__(KeycloakManager)
    mgr.project_manager = MagicMock()
    mgr.project_manager.get_contents = AsyncMock(return_value=project_data)
    mgr.project_manager.save_and_commit_project = AsyncMock()

    await mgr._ensure_admin_otp("proj", "local", "proj-local", "https://kc.example")

    mgr.project_manager.save_and_commit_project.assert_not_awaited()
