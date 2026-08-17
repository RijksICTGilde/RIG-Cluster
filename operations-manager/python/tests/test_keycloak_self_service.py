"""Tests for the identity self-service restrictions on project realms.

A user who can set their own password and unlink the IdP logs in locally afterwards,
bypassing SSO Rijk (features/futures/keycloak-sso-bypass-voorkomen.md). Two independent
knobs close that off, and both must fail closed: a realm provisioned without them is
provisioned in the vulnerable configuration.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from keycloak.exceptions import KeycloakError
from opi.connectors.keycloak import KeycloakConnector
from opi.handlers.keycloak_yaml_handler import KeycloakYamlHandler

BLUEPRINT_DIR = Path(__file__).parent.parent / "opi" / "configs" / "keycloak"


def _connector_with_admin(admin: MagicMock) -> KeycloakConnector:
    """A KeycloakConnector without running __init__ (which connects), with a fake admin."""
    connector = KeycloakConnector.__new__(KeycloakConnector)
    connector.admin = admin
    return connector


# --- set_required_action_enabled -------------------------------------------------


async def test_disabling_required_action_writes_the_flipped_representation() -> None:
    admin = MagicMock()
    admin.get_required_action_by_alias.return_value = {"alias": "UPDATE_PASSWORD", "enabled": True, "priority": 30}
    connector = _connector_with_admin(admin)

    await connector.set_required_action_enabled("some-realm", "UPDATE_PASSWORD", enabled=False)

    payload = admin.update_required_action.call_args.kwargs["payload"]
    assert payload["enabled"] is False
    # The rest of the representation is preserved, not rebuilt from scratch.
    assert payload["priority"] == 30
    admin.change_current_realm.assert_called_with("master")


async def test_disabling_required_action_is_a_noop_when_already_disabled() -> None:
    admin = MagicMock()
    admin.get_required_action_by_alias.return_value = {"alias": "UPDATE_PASSWORD", "enabled": False}
    connector = _connector_with_admin(admin)

    await connector.set_required_action_enabled("some-realm", "UPDATE_PASSWORD", enabled=False)

    admin.update_required_action.assert_not_called()
    admin.change_current_realm.assert_called_with("master")


async def test_unknown_required_action_alias_does_not_write() -> None:
    admin = MagicMock()
    admin.get_required_action_by_alias.return_value = None
    connector = _connector_with_admin(admin)

    await connector.set_required_action_enabled("some-realm", "NO_SUCH_ACTION", enabled=False)

    admin.update_required_action.assert_not_called()
    admin.change_current_realm.assert_called_with("master")


async def test_required_action_failure_aborts_provisioning() -> None:
    admin = MagicMock()
    admin.get_required_action_by_alias.side_effect = KeycloakError("authentication API unavailable")
    connector = _connector_with_admin(admin)

    with pytest.raises(KeycloakError):
        await connector.set_required_action_enabled("some-realm", "UPDATE_PASSWORD", enabled=False)

    admin.change_current_realm.assert_called_with("master")


# --- remove_default_role ---------------------------------------------------------


def _admin_with_composites(composites: list[dict]) -> MagicMock:
    admin = MagicMock()
    admin.get_client_id.return_value = "account-uuid"
    admin.get_default_realm_role_id.return_value = "default-roles-uuid"
    admin.get_role_composites_by_id.return_value = composites
    return admin


async def test_removing_default_role_deletes_the_matching_composite() -> None:
    manage_account = {"id": "role-1", "name": "manage-account", "clientRole": True, "containerId": "account-uuid"}
    admin = _admin_with_composites(
        [
            {"id": "role-0", "name": "view-profile", "clientRole": True, "containerId": "account-uuid"},
            manage_account,
        ]
    )
    connector = _connector_with_admin(admin)

    await connector.remove_default_role("some-realm", "account", "manage-account")

    assert admin.remove_realm_default_roles.call_args.kwargs["payload"] == [manage_account]
    admin.change_current_realm.assert_called_with("master")


async def test_removing_default_role_ignores_a_same_named_role_of_another_client() -> None:
    # Matching on name alone would strip an unrelated role out of the composite.
    admin = _admin_with_composites(
        [{"id": "role-9", "name": "manage-account", "clientRole": True, "containerId": "other-client-uuid"}]
    )
    connector = _connector_with_admin(admin)

    await connector.remove_default_role("some-realm", "account", "manage-account")

    admin.remove_realm_default_roles.assert_not_called()
    admin.change_current_realm.assert_called_with("master")


async def test_removing_default_role_is_a_noop_when_already_gone() -> None:
    admin = _admin_with_composites(
        [{"id": "role-0", "name": "view-profile", "clientRole": True, "containerId": "account-uuid"}]
    )
    connector = _connector_with_admin(admin)

    await connector.remove_default_role("some-realm", "account", "manage-account")

    admin.remove_realm_default_roles.assert_not_called()
    admin.change_current_realm.assert_called_with("master")


async def test_removing_default_role_failure_aborts_provisioning() -> None:
    admin = MagicMock()
    admin.get_client_id.side_effect = KeycloakError("clients API unavailable")
    connector = _connector_with_admin(admin)

    with pytest.raises(KeycloakError):
        await connector.remove_default_role("some-realm", "account", "manage-account")

    admin.change_current_realm.assert_called_with("master")


# --- blueprint wiring ------------------------------------------------------------


def _handler_with_fake_connector() -> tuple[KeycloakYamlHandler, MagicMock]:
    keycloak = MagicMock()
    keycloak.set_required_action_enabled = AsyncMock()
    keycloak.remove_default_role = AsyncMock()
    return KeycloakYamlHandler(keycloak), keycloak


@pytest.mark.parametrize("template", ["sso-only", "sso-support"])
async def test_project_blueprints_close_both_knobs(template: str) -> None:
    handler, keycloak = _handler_with_fake_connector()

    await handler.ensure_realm_self_service(
        BLUEPRINT_DIR / f"{template}.yaml",
        {"project_realm_name": "rig-demo", "project_display_name": "demo"},
    )

    keycloak.set_required_action_enabled.assert_awaited_once_with("rig-demo", "UPDATE_PASSWORD", enabled=False)
    keycloak.remove_default_role.assert_awaited_once_with("rig-demo", "account", "manage-account")


async def test_local_only_blueprint_keeps_password_self_service() -> None:
    # algoritmeregister has no identity providers at all, so there is no SSO to bypass and
    # closing off password self-service would be a cost without a benefit.
    handler, keycloak = _handler_with_fake_connector()

    await handler.ensure_realm_self_service(
        BLUEPRINT_DIR / "algoritmeregister.yaml",
        {"realm_name": "algoritmeregister", "realm_display_name": "Algoritmeregister"},
    )

    keycloak.set_required_action_enabled.assert_not_awaited()
    keycloak.remove_default_role.assert_not_awaited()


async def test_malformed_default_role_entry_is_skipped_not_guessed() -> None:
    handler, keycloak = _handler_with_fake_connector()

    await handler._apply_realm_self_service("rig-demo", {"removeFromDefaultRoles": ["manage-account"]})

    keycloak.remove_default_role.assert_not_awaited()
