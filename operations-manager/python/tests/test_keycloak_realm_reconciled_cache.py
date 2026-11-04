"""Tests voor het realm-brede werk dat KeycloakManager maar een keer per taak doet.

Het blok onder ``_setup_sso_rijk_integration`` (flow, IdP's, self-service, rollen,
extra clients, admin-OTP) is realm-breed en idempotent, maar liep voor RC-418 opnieuw
voor elke SSO-deployment in dezelfde taak: een eigen admin-token en acht
reconcilerondes voor precies hetzelfde resultaat. ``_realm_reconciled`` houdt bij welke
(realm, keycloak-url) in deze taak al zijn bijgetrokken; alleen de client-koppeling,
die per deployment verschilt (eigen redirect-URI's), gaat nog elke keer.
"""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.manager.keycloak_manager import KeycloakManager
from opi.utils.secrets import KeycloakSecret

KEYCLOAK_URL = "https://keycloak.example"
REALM = "proj-odcn-production"


def _manager(*, existing_credentials: KeycloakSecret | None) -> KeycloakManager:
    project_manager = MagicMock()
    project_manager._get_project_keycloak_config_for_cluster = AsyncMock(
        return_value={"realm": REALM, "host": KEYCLOAK_URL}
    )
    project_manager._get_keycloak_url_for_cluster = MagicMock(return_value=KEYCLOAK_URL)
    project_manager._get_secret_from_map = MagicMock(return_value=existing_credentials)
    project_manager._add_secret_to_create = MagicMock()
    return KeycloakManager(project_manager)


def _existing_credentials() -> KeycloakSecret:
    return KeycloakSecret(
        client_id="client-1",
        client_secret="s3cret",
        public_client_id=None,
        discovery_url=f"{KEYCLOAK_URL}/realms/{REALM}/.well-known/openid-configuration",
        base_url=KEYCLOAK_URL,
        realm=REALM,
    )


@contextlib.contextmanager
def _patched_realm_wide_methods(manager: KeycloakManager):
    """Patcht elke realm-brede stap, zodat een test kan tellen hoe vaak ze lopen."""
    with contextlib.ExitStack() as stack:
        for naam in (
            "_project_realm_is_present",
            "_ensure_realm_authentication_flow",
            "_ensure_idp_and_platform_client_configuration",
            "_ensure_realm_identity_providers",
            "_ensure_realm_self_service",
            "_ensure_realm_clients",
            "_ensure_admin_otp",
        ):
            waarde = True if naam == "_project_realm_is_present" else None
            stack.enter_context(patch.object(manager, naam, AsyncMock(return_value=waarde)))
        yield


@pytest.mark.asyncio
async def test_het_eerste_sso_deployment_doet_het_volle_realm_brede_werk() -> None:
    manager = _manager(existing_credentials=_existing_credentials())
    with (
        patch("opi.manager.keycloak_manager.create_keycloak_connector", AsyncMock(return_value=AsyncMock())),
        _patched_realm_wide_methods(manager),
    ):
        await manager._setup_sso_rijk_integration("proj", "deployment-1", ["proj.example"], "odcn-production", {})

        manager._ensure_realm_authentication_flow.assert_awaited_once()
        manager._ensure_realm_identity_providers.assert_awaited_once()
        manager._ensure_realm_self_service.assert_awaited_once()
        manager._ensure_admin_otp.assert_awaited_once()
        manager._ensure_realm_clients.assert_awaited_once()
        assert manager._realm_reconciled == {(REALM, KEYCLOAK_URL)}


@pytest.mark.asyncio
async def test_een_tweede_sso_deployment_in_dezelfde_taak_slaat_het_realm_brede_werk_over() -> None:
    manager = _manager(existing_credentials=_existing_credentials())
    manager._realm_reconciled = {(REALM, KEYCLOAK_URL)}

    with (
        patch("opi.manager.keycloak_manager.create_keycloak_connector", AsyncMock(return_value=AsyncMock())),
        _patched_realm_wide_methods(manager),
    ):
        await manager._setup_sso_rijk_integration("proj", "deployment-2", ["proj2.example"], "odcn-production", {})

        manager._ensure_realm_authentication_flow.assert_not_awaited()
        manager._ensure_realm_identity_providers.assert_not_awaited()
        manager._ensure_realm_self_service.assert_not_awaited()
        manager._ensure_admin_otp.assert_not_awaited()
        # De clients hangen aan DEZE deployment en gaan dus hoe dan ook door.
        manager._ensure_realm_clients.assert_awaited_once()


@pytest.mark.asyncio
async def test_een_verse_manager_heeft_nog_niets_in_de_cache() -> None:
    manager = _manager(existing_credentials=_existing_credentials())
    assert manager._realm_reconciled is None


@pytest.mark.asyncio
async def test_een_ander_realm_op_dezelfde_url_doet_toch_het_volle_werk() -> None:
    """De cachekey is (realm, url): een andere realm is geen cache-hit."""
    manager = _manager(existing_credentials=_existing_credentials())
    manager._realm_reconciled = {("een-andere-realm", KEYCLOAK_URL)}

    with (
        patch("opi.manager.keycloak_manager.create_keycloak_connector", AsyncMock(return_value=AsyncMock())),
        _patched_realm_wide_methods(manager),
    ):
        await manager._setup_sso_rijk_integration("proj", "deployment-1", ["proj.example"], "odcn-production", {})

        manager._ensure_realm_authentication_flow.assert_awaited_once()
