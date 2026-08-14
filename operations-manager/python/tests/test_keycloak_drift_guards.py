"""Tests for the Keycloak drift guards.

Covers two bugs that historically caused project YAML and Keycloak to silently
diverge on the admin-user password:

1. KeycloakConnector.realm_exists() must distinguish 404 (genuine "missing")
   from any other failure (401, 5xx, connection error). Only 404 returns False.

2. KeycloakManager._setup_project_keycloak_realm() must refuse to proceed when
   the project admin user already exists in the master realm. Otherwise a stale
   regenerated password gets committed to YAML while Keycloak's credential
   stays untouched (the create_user call silently 409s).

And the counterpart of (2): the guard is a valve, not a wall. A realm that is
alive must never be sent down the re-create path in the first place, and an
admin user the project file already knows is not drift - both continue.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from keycloak.exceptions import KeycloakGetError
from opi.connectors.keycloak import KeycloakConnector
from opi.manager.keycloak_manager import KeycloakManager
from opi.utils.secrets import KeycloakSecret


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
        # The case the valve is for: an admin user in master that NO realm entry in
        # the project file knows, so his password is gone for good. Re-creating would
        # write a password to the file that Keycloak does not accept.
        project_manager = MagicMock()
        project_manager.get_contents = AsyncMock(return_value={})

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


class TestSetupProjectRealmImmediatePersist:
    """The generated admin password exists nowhere outside the project file.

    It must be committed and pushed immediately after the admin user is
    created, not at the end of the run: a later failure in the same task
    would otherwise orphan the admin user with an unrecoverable password
    and wedge every re-run on the duplicate-admin guard.
    """

    @pytest.mark.asyncio
    async def test_credentials_pushed_immediately_after_admin_creation(self) -> None:
        project_manager = MagicMock()
        project_manager.get_contents = AsyncMock(return_value={"config": {"age-public-key": "age1publickey"}})
        project_manager.save_and_commit_project = AsyncMock()

        manager = KeycloakManager(project_manager)

        fake_keycloak = AsyncMock()
        fake_keycloak.get_user_by_username = AsyncMock(return_value=None)
        fake_keycloak.create_user = AsyncMock(return_value={"id": "new-user-id"})
        fake_keycloak.assign_realm_admin_from_master = AsyncMock()

        with (
            patch(
                "opi.manager.keycloak_manager.create_keycloak_connector",
                return_value=fake_keycloak,
            ),
            patch("opi.manager.keycloak_manager.encrypt_age_content", AsyncMock(return_value="ENCRYPTED")),
            patch("opi.manager.keycloak_manager.KeycloakYamlHandler") as yaml_handler_cls,
            patch("opi.manager.keycloak_manager.get_keycloak_support_http", return_value=False),
        ):
            yaml_handler_cls.return_value.execute_config = AsyncMock()

            result = await manager._setup_project_keycloak_realm(
                project_name="regel-k4c",
                cluster="odcn-production",
                keycloak_url="https://keycloak.example",
                config={"template": "sso-support"},
            )

        project_manager.save_and_commit_project.assert_awaited_once()
        commit_message = project_manager.save_and_commit_project.await_args.args[1]
        assert "regel-k4c" in commit_message
        assert result["username"] == "regel_k4c_odcn_production_admin"
        assert result["password"]


class TestSetupProjectRealmAdoptsKnownAdmin:
    """An admin user the project file already knows is not drift.

    The valve exists to stop a freshly generated password from being written to the
    project file while Keycloak quietly keeps the old one. When the realm is there,
    the admin user is there, and the file already carries that admin's password,
    there is no new password and no new realm - nothing can diverge, so the run
    continues on what is already there instead of dead-ending the project.
    """

    @staticmethod
    def _project_file_with_realm() -> dict:
        return {
            "services": [
                {
                    "name": "keycloak",
                    "config": {
                        "realms": [
                            {
                                "host": "https://keycloak.example",
                                "realm": "vp-8bw-odcn-production",
                                "username": "vp_8bw_odcn_production_admin",
                                "password": "AGE-ENCRYPTED-PASSWORD",
                            }
                        ]
                    },
                }
            ]
        }

    @pytest.mark.asyncio
    async def test_continues_with_existing_realm_instead_of_recreating(self) -> None:
        project_manager = MagicMock()
        project_manager.get_contents = AsyncMock(return_value=self._project_file_with_realm())
        project_manager.save_and_commit_project = AsyncMock()

        manager = KeycloakManager(project_manager)

        fake_keycloak = AsyncMock()
        fake_keycloak.get_user_by_username = AsyncMock(return_value={"id": "preexisting-user-id"})
        fake_keycloak.realm_exists = AsyncMock(return_value=True)

        with (
            patch(
                "opi.manager.keycloak_manager.create_keycloak_connector",
                return_value=fake_keycloak,
            ),
            patch("opi.manager.keycloak_manager.generate_secure_password") as gen_pw,
            patch("opi.manager.keycloak_manager.KeycloakYamlHandler") as yaml_handler_cls,
        ):
            result = await manager._setup_project_keycloak_realm(
                project_name="vp-8bw",
                cluster="odcn-production",
                keycloak_url="https://keycloak.example",
                config={"template": "sso-only"},
            )

        assert result["realm"] == "vp-8bw-odcn-production"
        assert result["username"] == "vp_8bw_odcn_production_admin"
        assert result["password"] == "AGE-ENCRYPTED-PASSWORD"
        # Nothing created, nothing rewritten.
        gen_pw.assert_not_called()
        yaml_handler_cls.assert_not_called()
        fake_keycloak.create_user.assert_not_awaited()
        project_manager.save_and_commit_project.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_still_refuses_when_the_realm_itself_is_gone(self) -> None:
        # The file knows this admin, but the realm really is missing on both counts.
        # Re-creating would 409 on the admin user and write a password Keycloak does
        # not accept, so this stays a refusal.
        project_manager = MagicMock()
        project_manager.get_contents = AsyncMock(return_value=self._project_file_with_realm())

        manager = KeycloakManager(project_manager)

        fake_keycloak = AsyncMock()
        fake_keycloak.get_user_by_username = AsyncMock(return_value={"id": "preexisting-user-id"})
        fake_keycloak.realm_exists = AsyncMock(return_value=False)
        fake_keycloak.realm_discovery_available = AsyncMock(return_value=False)

        with (
            patch(
                "opi.manager.keycloak_manager.create_keycloak_connector",
                return_value=fake_keycloak,
            ),
            pytest.raises(RuntimeError, match="already exists in master realm"),
        ):
            await manager._setup_project_keycloak_realm(
                project_name="vp-8bw",
                cluster="odcn-production",
                keycloak_url="https://keycloak.example",
                config={"template": "sso-only"},
            )


class TestRealmPresenceIsEstablishedTwice:
    """A realm that is alive must never be sent down the re-create path.

    This is what wedged vp-8bw: one negative answer about the realm was enough to
    start creating it again, and that re-create walks straight into the guard above
    - forever, on every following run. So a negative from the admin API is now held
    against the realm's own discovery document, which needs no admin session.
    """

    def _manager(self) -> KeycloakManager:
        project_manager = MagicMock()
        project_manager._get_project_keycloak_config_for_cluster = AsyncMock(
            return_value={
                "host": "https://keycloak.example",
                "realm": "vp-8bw-odcn-production",
                "username": "vp_8bw_odcn_production_admin",
                "password": "AGE-ENCRYPTED-PASSWORD",
            }
        )
        project_manager._get_keycloak_url_for_cluster = MagicMock(return_value="https://keycloak.example")
        project_manager._get_secret_from_map = MagicMock(
            return_value=KeycloakSecret(
                client_id="vp-8bw-productie",
                client_secret="client-secret",
                public_client_id="",
                discovery_url=(
                    "https://keycloak.example/realms/vp-8bw-odcn-production/.well-known/openid-configuration"
                ),
                base_url="https://keycloak.example",
                realm="vp-8bw-odcn-production",
            )
        )

        manager = KeycloakManager(project_manager)
        # The idempotent reconciliation steps are not what these tests are about.
        for step in (
            "_ensure_realm_authentication_flow",
            "_ensure_idp_and_platform_client_configuration",
            "_ensure_realm_identity_providers",
            "_ensure_realm_self_service",
            "_ensure_realm_clients",
            "_ensure_admin_otp",
        ):
            setattr(manager, step, AsyncMock())
        manager._setup_project_keycloak_realm = AsyncMock()
        return manager

    async def _run(self, manager: KeycloakManager, keycloak: AsyncMock) -> dict:
        with (
            patch(
                "opi.manager.keycloak_manager.create_keycloak_connector",
                return_value=keycloak,
            ),
            patch(
                "opi.manager.keycloak_manager.get_keycloak_discovery_url",
                return_value="https://keycloak.example",
            ),
        ):
            return await manager._setup_sso_rijk_integration(
                project_name="vp-8bw",
                deployment_name="productie",
                ingress_hosts=["vp-8bw-productie.example"],
                cluster="odcn-production",
                config={"template": "sso-only"},
            )

    @pytest.mark.asyncio
    async def test_healthy_realm_is_not_recreated_when_the_admin_api_misses_it(self) -> None:
        manager = self._manager()
        keycloak = AsyncMock()
        keycloak.realm_exists = AsyncMock(return_value=False)
        keycloak.realm_discovery_available = AsyncMock(return_value=True)

        result = await self._run(manager, keycloak)

        manager._setup_project_keycloak_realm.assert_not_awaited()
        assert result["realm"] == "vp-8bw-odcn-production"

    @pytest.mark.asyncio
    async def test_realm_that_is_gone_on_both_counts_is_still_recreated(self) -> None:
        manager = self._manager()
        keycloak = AsyncMock()
        keycloak.realm_exists = AsyncMock(return_value=False)
        keycloak.realm_discovery_available = AsyncMock(return_value=False)

        await self._run(manager, keycloak)

        manager._setup_project_keycloak_realm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_realm_is_looked_for_on_the_clusters_own_keycloak(self) -> None:
        # Not on the host recorded in the project file: after a domain migration that
        # host answers "no such realm" for a realm that is alive on the current one.
        manager = self._manager()
        project_manager = manager.project_manager
        project_manager._get_project_keycloak_config_for_cluster = AsyncMock(
            return_value={
                "host": "https://keycloak.old-domain.example",
                "realm": "vp-8bw-odcn-production",
                "username": "vp_8bw_odcn_production_admin",
                "password": "AGE-ENCRYPTED-PASSWORD",
            }
        )
        project_manager.get_contents = AsyncMock(return_value={})
        project_manager.save_and_commit_project = AsyncMock()

        keycloak = AsyncMock()
        keycloak.realm_exists = AsyncMock(return_value=True)

        with (
            patch(
                "opi.manager.keycloak_manager.create_keycloak_connector",
                return_value=keycloak,
            ) as connector_factory,
            patch(
                "opi.manager.keycloak_manager.get_keycloak_discovery_url",
                return_value="https://keycloak.example",
            ),
        ):
            await manager._setup_sso_rijk_integration(
                project_name="vp-8bw",
                deployment_name="productie",
                ingress_hosts=["vp-8bw-productie.example"],
                cluster="odcn-production",
                config={"template": "sso-only"},
            )

        assert connector_factory.await_args_list[0].kwargs["keycloak_url"] == "https://keycloak.example"
        manager._setup_project_keycloak_realm.assert_not_awaited()


class TestRealmDiscoveryAvailable:
    """The second opinion answers on its own, without an admin session."""

    def _connector(self) -> KeycloakConnector:
        connector = KeycloakConnector.__new__(KeycloakConnector)
        connector.keycloak_url = "https://keycloak.example"
        return connector

    def _with_status(self, session_cls: MagicMock, status: int) -> None:
        response = MagicMock()
        response.status = status
        session = MagicMock()
        session.get.return_value.__aenter__ = AsyncMock(return_value=response)
        session.get.return_value.__aexit__ = AsyncMock(return_value=False)
        session_cls.return_value.__aenter__ = AsyncMock(return_value=session)
        session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

    @pytest.mark.asyncio
    async def test_true_on_http_200(self) -> None:
        with patch("opi.connectors.keycloak.aiohttp.ClientSession") as session_cls:
            self._with_status(session_cls, 200)
            assert await self._connector().realm_discovery_available("some-realm") is True

    @pytest.mark.asyncio
    async def test_false_on_http_404(self) -> None:
        with patch("opi.connectors.keycloak.aiohttp.ClientSession") as session_cls:
            self._with_status(session_cls, 404)
            assert await self._connector().realm_discovery_available("some-realm") is False

    @pytest.mark.asyncio
    async def test_false_when_the_endpoint_cannot_be_reached(self) -> None:
        # Only ever overrules a negative, so an unreachable endpoint leaves the
        # earlier answer standing rather than claiming the realm is there.
        with patch("opi.connectors.keycloak.aiohttp.ClientSession", side_effect=aiohttp.ClientError("boom")):
            assert await self._connector().realm_discovery_available("some-realm") is False
