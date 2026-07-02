"""Tests for resource deletion in DatabaseManager and KeycloakConnector.

Verifies that:
- DatabaseManager.delete_resources_for_deployment uses the bound connector (not per-call credentials)
- KeycloakConnector.delete_deployment_client operates in the correct realm
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.connectors.postgres import PostgresConnector
from opi.manager.database_manager import DatabaseManager

# --- DatabaseManager deletion tests ---


def _make_database_manager(
    db_host: str = "shared-db.svc.cluster.local",
    admin_username: str = "postgres",
    admin_password: str = "admin-secret",  # noqa: S107
    uses_postgresql: bool = True,
) -> DatabaseManager:
    """Create a DatabaseManager with a mocked ProjectManager."""
    mock_pm = AsyncMock()
    mock_pm.get_name = AsyncMock(return_value="test-project")
    # Mock the project file handler's service check to avoid unawaited coroutine warnings
    mock_pm._project_file_handler.deployment_uses_service = MagicMock(return_value=uses_postgresql)
    # get_deployment_service_generation is sync, must return a plain value (not coroutine)
    mock_pm._project_file_handler.get_deployment_service_generation = MagicMock(return_value=None)
    dm = DatabaseManager(
        project_manager=mock_pm,
        db_host=db_host,
        admin_username=admin_username,
        admin_password=admin_password,
    )
    return dm


def _make_project_data(service_type: str = "postgresql-database") -> dict:
    """Create minimal project data with a database service."""
    return {
        "name": "test-project",
        "services": [service_type],
        "deployments": [
            {
                "name": "pr-123",
                "cluster": "odcn-production",
                "services": {service_type: {}},
                "components": [{"reference": "web"}],
            }
        ],
    }


def _make_deployment(name: str = "pr-123", cluster: str = "odcn-production") -> dict:
    return {
        "name": name,
        "cluster": cluster,
        "services": {"postgresql-database": {}},
        "components": [{"reference": "web"}],
    }


class TestDeleteResourcesForDeployment:
    """Test DatabaseManager.delete_resources_for_deployment."""

    @pytest.mark.asyncio
    async def test_shared_db_calls_connector_without_host_kwargs(self):
        """delete_database and delete_user must be called without host/admin kwargs.

        The PostgresConnector is bound to credentials at construction time.
        Passing host/admin_username/admin_password as kwargs would cause a TypeError.
        """
        db_manager = _make_database_manager()
        project_data = _make_project_data()
        deployment = _make_deployment()

        # Mock the connector
        mock_connector = AsyncMock(spec=PostgresConnector)
        mock_connector.delete_database = AsyncMock(return_value={"status": "deleted", "message": "ok"})
        mock_connector.delete_user = AsyncMock(return_value={"status": "deleted", "message": "ok"})
        db_manager._postgres_connector = mock_connector

        await db_manager.delete_resources_for_deployment(project_data, deployment)

        # Verify every delete_database call used ONLY database_name (no admin kwargs).
        # Note: the deletion scans for versioned databases beyond the YAML generation,
        # so delete_database is called multiple times (base name + versioned variants).
        assert mock_connector.delete_database.call_count >= 1
        called_names = []
        for call_args in mock_connector.delete_database.call_args_list:
            assert "host" not in (call_args.kwargs or {})
            assert "admin_username" not in (call_args.kwargs or {})
            assert "admin_password" not in (call_args.kwargs or {})
            assert set(call_args.kwargs.keys()) == {"database_name"}
            called_names.append(call_args.kwargs["database_name"])
        assert "test_project_pr_123" in called_names

        # Verify delete_user was called with ONLY username, for BOTH the main
        # user and its read-only ("_ro") companion role.
        assert mock_connector.delete_user.call_count == 2
        deleted_users = []
        for call_kwargs in mock_connector.delete_user.call_args_list:
            assert "host" not in (call_kwargs.kwargs or {})
            assert "admin_username" not in (call_kwargs.kwargs or {})
            assert set(call_kwargs.kwargs.keys()) == {"username"}
            deleted_users.append(call_kwargs.kwargs["username"])
        assert deleted_users == ["test_project_pr_123", "test_project_pr_123_ro"]

    @pytest.mark.asyncio
    async def test_uses_bound_connector_not_new_one(self):
        """delete_resources_for_deployment must use self.postgres_connector, not create a new one."""
        db_manager = _make_database_manager()
        project_data = _make_project_data()
        deployment = _make_deployment()

        mock_connector = AsyncMock(spec=PostgresConnector)
        mock_connector.delete_database = AsyncMock(return_value={"status": "deleted", "message": "ok"})
        mock_connector.delete_user = AsyncMock(return_value={"status": "deleted", "message": "ok"})
        db_manager._postgres_connector = mock_connector

        with patch("opi.manager.database_manager.create_postgres_connector") as mock_factory:
            await db_manager.delete_resources_for_deployment(project_data, deployment)
            # Factory should NOT be called - we use the bound connector
            mock_factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_deletion_returns_success(self):
        """Successful deletion should return success=True with no errors."""
        db_manager = _make_database_manager()
        project_data = _make_project_data()
        deployment = _make_deployment()

        mock_connector = AsyncMock(spec=PostgresConnector)
        mock_connector.delete_database = AsyncMock(return_value={"status": "deleted", "message": "ok"})
        mock_connector.delete_user = AsyncMock(return_value={"status": "deleted", "message": "ok"})
        db_manager._postgres_connector = mock_connector

        result = await db_manager.delete_resources_for_deployment(project_data, deployment)

        assert result["success"] is True
        assert len(result["errors"]) == 0
        # Versioned database cleanup scans beyond YAML generation, so there are
        # multiple database_deletion operations plus one database_user_deletion.
        types = [op["type"] for op in result["operations"]]
        # One deletion op for the main user plus one for its read-only "_ro" companion.
        assert types.count("database_user_deletion") == 2
        assert types.count("database_deletion") >= 1

    @pytest.mark.asyncio
    async def test_not_found_database_is_not_error(self):
        """If the database doesn't exist, it should not be treated as a hard error."""
        db_manager = _make_database_manager()
        project_data = _make_project_data()
        deployment = _make_deployment()

        mock_connector = AsyncMock(spec=PostgresConnector)
        mock_connector.delete_database = AsyncMock(
            return_value={"status": "not_found", "message": "Database does not exist"}
        )
        mock_connector.delete_user = AsyncMock(return_value={"status": "not_found", "message": "User does not exist"})
        db_manager._postgres_connector = mock_connector

        result = await db_manager.delete_resources_for_deployment(project_data, deployment)

        assert result["success"] is True
        assert len(result["errors"]) == 0

    @pytest.mark.asyncio
    async def test_skips_when_no_postgresql_service(self):
        """Should skip deletion when deployment doesn't use PostgreSQL."""
        db_manager = _make_database_manager(uses_postgresql=False)
        project_data = _make_project_data()
        deployment = _make_deployment()

        result = await db_manager.delete_resources_for_deployment(project_data, deployment)

        assert result["operations"][0]["status"] == "skipped"


# --- KeycloakConnector deletion tests ---


class TestDeleteDeploymentClientRealm:
    """Test that delete_deployment_client operates in the correct realm."""

    @pytest.mark.asyncio
    async def test_delete_client_runs_in_correct_realm(self):
        """delete_client must execute in the project realm, not master.

        find_client_by_client_id switches back to master internally.
        delete_deployment_client must re-switch to the target realm before deleting.
        """
        from opi.connectors.keycloak import KeycloakConnector

        connector = KeycloakConnector.__new__(KeycloakConnector)
        connector.admin = MagicMock()

        # Track realm changes
        realm_changes: list[str] = []
        connector.admin.change_current_realm = MagicMock(side_effect=lambda r: realm_changes.append(r))

        # Mock find_client_by_client_id to return a confidential client and a public client
        # (this method internally switches to master before returning)
        mock_client = {"id": "internal-uuid-123", "clientId": "test-project-pr-123"}
        mock_public_client = {"id": "internal-uuid-456", "clientId": "test-project-pr-123-public"}
        connector.find_client_by_client_id = AsyncMock(side_effect=[mock_client, mock_public_client])

        # Mock delete_client to succeed
        connector.admin.delete_client = MagicMock()

        await connector.delete_deployment_client(
            deployment_name="pr-123",
            project_name="test-project",
            realm_name="test-project-odcn-production",
        )

        # Verify both clients were deleted
        connector.admin.delete_client.assert_any_call(client_id="internal-uuid-123")
        connector.admin.delete_client.assert_any_call(client_id="internal-uuid-456")
        assert connector.admin.delete_client.call_count == 2

        # Verify the realm was set to project realm before each delete
        assert "test-project-odcn-production" in realm_changes, (
            f"Project realm was never set. Realm changes: {realm_changes}"
        )

        # After the initial switch, find_client returns (which switched to master internally),
        # then we must see switches to project realm before each delete
        assert realm_changes.count("test-project-odcn-production") >= 3, (
            f"Expected at least 3 switches to project realm (initial + before each delete). "
            f"Realm changes: {realm_changes}"
        )

    @pytest.mark.asyncio
    async def test_returns_false_when_client_not_found(self):
        """Should return False without error when client doesn't exist."""
        from opi.connectors.keycloak import KeycloakConnector

        connector = KeycloakConnector.__new__(KeycloakConnector)
        connector.admin = MagicMock()
        connector.find_client_by_client_id = AsyncMock(return_value=None)

        result = await connector.delete_deployment_client(
            deployment_name="pr-999",
            project_name="test-project",
            realm_name="test-project-realm",
        )

        assert result is False
        connector.admin.delete_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_client_without_public_client(self):
        """Should delete only the confidential client when public client doesn't exist."""
        from opi.connectors.keycloak import KeycloakConnector

        connector = KeycloakConnector.__new__(KeycloakConnector)
        connector.admin = MagicMock()

        realm_changes: list[str] = []
        connector.admin.change_current_realm = MagicMock(side_effect=lambda r: realm_changes.append(r))

        mock_client = {"id": "internal-uuid-123", "clientId": "test-project-pr-123"}
        connector.find_client_by_client_id = AsyncMock(side_effect=[mock_client, None])

        connector.admin.delete_client = MagicMock()

        result = await connector.delete_deployment_client(
            deployment_name="pr-123",
            project_name="test-project",
            realm_name="test-project-realm",
        )

        assert result is True
        connector.admin.delete_client.assert_called_once_with(client_id="internal-uuid-123")
