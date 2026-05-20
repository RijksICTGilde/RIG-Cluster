"""Tests for the Keycloak drift guards.

Covers two bugs that historically caused project YAML and Keycloak to silently
diverge on the admin-user password:

1. KeycloakConnector.realm_exists() must distinguish 404 (genuine "missing")
   from any other failure (401, 5xx, connection error). Only 404 returns False.

2. KeycloakManager._setup_project_keycloak_realm() must refuse to proceed when
   the project admin user already exists in the master realm. Otherwise a stale
   regenerated password gets committed to YAML while Keycloak's credential
   stays untouched (the create_user call silently 409s).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from keycloak.exceptions import KeycloakGetError
from opi.connectors.keycloak import KeycloakConnector
from opi.manager.keycloak_manager import KeycloakManager


class TestRealmExists:
    """KeycloakConnector.realm_exists must not mask transient errors as 'missing'."""

    def _make_connector(self, get_realm_side_effect) -> KeycloakConnector:
        connector = KeycloakConnector.__new__(KeycloakConnector)
        connector.admin = MagicMock()
        connector.admin.get_realm = MagicMock(side_effect=get_realm_side_effect)
        return connector

    @pytest.mark.asyncio
    async def test_returns_true_when_realm_present(self) -> None:
        connector = self._make_connector(get_realm_side_effect=lambda realm_name: {"realm": realm_name})
        assert await connector.realm_exists("any-realm") is True

    @pytest.mark.asyncio
    async def test_returns_false_only_on_404(self) -> None:
        err = KeycloakGetError(error_message="not found", response_code=404)
        connector = self._make_connector(get_realm_side_effect=err)
        assert await connector.realm_exists("missing-realm") is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("response_code", [401, 403, 500, 502, 503, None])
    async def test_raises_on_non_404_errors(self, response_code: int | None) -> None:
        err = KeycloakGetError(error_message="boom", response_code=response_code)
        connector = self._make_connector(get_realm_side_effect=err)
        with pytest.raises(KeycloakGetError):
            await connector.realm_exists("any-realm")


class TestSetupProjectRealmDriftGuard:
    """_setup_project_keycloak_realm must not regenerate a password when the
    admin user already exists. Otherwise YAML and Keycloak silently diverge."""

    @pytest.mark.asyncio
    async def test_raises_when_admin_user_already_exists(self) -> None:
        project_manager = MagicMock()
        project_manager.get_contents = AsyncMock(return_value={})
        project_manager.save_project_data = AsyncMock()

        manager = KeycloakManager(project_manager)

        fake_keycloak = AsyncMock()
        fake_keycloak.get_user_by_username = AsyncMock(return_value={"id": "preexisting-user-id"})

        with (
            patch(
                "opi.manager.keycloak_manager.create_keycloak_connector",
                return_value=fake_keycloak,
            ),
            patch("opi.manager.keycloak_manager.generate_secure_password") as gen_pw,
            patch("opi.manager.keycloak_manager.encrypt_age_content") as enc,
        ):
            with pytest.raises(RuntimeError, match="already exists in master realm"):
                await manager._setup_project_keycloak_realm(
                    project_name="regel-k4c",
                    cluster="odcn-production",
                    keycloak_url="https://keycloak.example",
                    config={"template": "sso-only"},
                )

            gen_pw.assert_not_called()
            enc.assert_not_called()

        project_manager.save_project_data.assert_not_called()
