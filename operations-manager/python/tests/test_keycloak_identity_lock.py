"""Tests for KeycloakConnector._lock_identity_fields.

Locking email/firstName/lastName to admin-only edit is the fix for the
impersonation post-mortem (self-editable email). It must fail closed: a realm
whose identity fields cannot be locked may not be silently provisioned in the
vulnerable configuration, so a user-profile API failure aborts provisioning
instead of being logged and forgotten.
"""

from unittest.mock import MagicMock

import pytest
from keycloak.exceptions import KeycloakError
from opi.connectors.keycloak import KeycloakConnector


def _connector_with_admin(admin: MagicMock) -> KeycloakConnector:
    """A KeycloakConnector without running __init__ (which connects), with a fake admin."""
    connector = KeycloakConnector.__new__(KeycloakConnector)
    connector.admin = admin
    return connector


async def test_lock_failure_aborts_provisioning() -> None:
    admin = MagicMock()
    admin.get_realm_users_profile.side_effect = KeycloakError("user-profile API unavailable")
    connector = _connector_with_admin(admin)

    with pytest.raises(KeycloakError):
        await connector._lock_identity_fields("some-realm")

    # Even on failure the admin client is switched back to the master realm.
    admin.change_current_realm.assert_called_with("master")


async def test_lock_sets_admin_only_edit_on_identity_fields() -> None:
    admin = MagicMock()
    admin.get_realm_users_profile.return_value = {
        "attributes": [
            {"name": "username", "permissions": {"edit": ["admin", "user"]}},
            {"name": "email", "permissions": {"edit": ["admin", "user"]}},
            {"name": "firstName", "permissions": {"edit": ["admin", "user"]}},
            {"name": "lastName"},
        ]
    }
    connector = _connector_with_admin(admin)

    await connector._lock_identity_fields("some-realm")

    payload = admin.update_realm_users_profile.call_args.kwargs["payload"]
    by_name = {attr["name"]: attr for attr in payload["attributes"]}
    assert by_name["email"]["permissions"]["edit"] == ["admin"]
    assert by_name["firstName"]["permissions"]["edit"] == ["admin"]
    assert by_name["lastName"]["permissions"]["edit"] == ["admin"]
    # Non-identity fields are left alone.
    assert by_name["username"]["permissions"]["edit"] == ["admin", "user"]
    admin.change_current_realm.assert_called_with("master")


async def test_lock_is_a_noop_when_already_locked() -> None:
    admin = MagicMock()
    admin.get_realm_users_profile.return_value = {
        "attributes": [
            {"name": "email", "permissions": {"edit": ["admin"]}},
        ]
    }
    connector = _connector_with_admin(admin)

    await connector._lock_identity_fields("some-realm")

    admin.update_realm_users_profile.assert_not_called()
    admin.change_current_realm.assert_called_with("master")
