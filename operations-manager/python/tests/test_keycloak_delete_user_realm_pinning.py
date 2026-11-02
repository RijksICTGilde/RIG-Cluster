"""Regression for delete_user_by_username deleting against the wrong realm (RC-413).

get_user_by_username leaves the admin client pinned to master once it returns. Without
re-pinning to realm_name, delete_user_by_username's own admin.delete_user call then runs
against master instead of the realm it was asked to delete from.
"""

from unittest.mock import MagicMock

from opi.connectors.keycloak import KeycloakConnector


def _connector_with_admin(admin: MagicMock) -> KeycloakConnector:
    connector = KeycloakConnector.__new__(KeycloakConnector)
    connector.admin = admin
    return connector


async def test_delete_user_by_username_deletes_in_the_target_realm() -> None:
    admin = MagicMock()
    admin.get_users.return_value = [{"id": "user-1"}]

    current_realm: list[str] = []
    admin.change_current_realm.side_effect = current_realm.append

    def _delete_user(user_id: str) -> None:
        assert current_realm[-1] == "proj-realm", (
            f"delete_user ran while pinned to '{current_realm[-1]}', not the target realm"
        )

    admin.delete_user.side_effect = _delete_user
    connector = _connector_with_admin(admin)

    result = await connector.delete_user_by_username("proj-realm", "bob")

    assert result is True
    admin.delete_user.assert_called_once_with(user_id="user-1")
    assert current_realm[-1] == "master"


async def test_delete_user_by_username_returns_false_without_deleting_when_absent() -> None:
    admin = MagicMock()
    admin.get_users.return_value = []
    connector = _connector_with_admin(admin)

    assert await connector.delete_user_by_username("proj-realm", "bob") is False
    admin.delete_user.assert_not_called()
    admin.change_current_realm.assert_called_with("master")
