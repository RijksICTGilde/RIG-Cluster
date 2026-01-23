"""
Tests for the subdomain registry connector.

Tests the SubdomainConnector class that manages globally unique subdomains
for the nice URL feature.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from opi.connectors.subdomain import (
    SubdomainConnector,
    SubdomainError,
    SubdomainNotAvailableError,
    create_subdomain_connector,
    SUBDOMAIN_REGISTRY_TABLE_SQL,
)


class TestSubdomainConnectorBasics:
    """Basic tests for SubdomainConnector class."""

    def test_create_subdomain_connector(self):
        """create_subdomain_connector returns a SubdomainConnector instance."""
        connector = create_subdomain_connector()
        assert isinstance(connector, SubdomainConnector)

    def test_table_sql_creates_table(self):
        """SUBDOMAIN_REGISTRY_TABLE_SQL contains CREATE TABLE statement."""
        assert "CREATE TABLE IF NOT EXISTS subdomain_registry" in SUBDOMAIN_REGISTRY_TABLE_SQL

    def test_table_sql_creates_indexes(self):
        """SUBDOMAIN_REGISTRY_TABLE_SQL contains index creation."""
        assert "CREATE INDEX IF NOT EXISTS idx_subdomain_project" in SUBDOMAIN_REGISTRY_TABLE_SQL
        assert "CREATE INDEX IF NOT EXISTS idx_subdomain_deployment" in SUBDOMAIN_REGISTRY_TABLE_SQL

    def test_table_sql_has_unique_constraint(self):
        """SUBDOMAIN_REGISTRY_TABLE_SQL has unique constraint on subdomain+base_domain."""
        assert "UNIQUE (subdomain, base_domain)" in SUBDOMAIN_REGISTRY_TABLE_SQL


class TestSubdomainConnectorCheckAvailability:
    """Tests for SubdomainConnector.check_availability method."""

    @pytest.mark.asyncio
    async def test_check_availability_returns_true_when_available(self):
        """check_availability returns True when subdomain is not registered."""
        connector = SubdomainConnector()

        # Mock the database pool and connection
        mock_conn = AsyncMock()
        mock_conn.fetchval.return_value = None  # Not found = available

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with patch.object(connector, "_get_pool", return_value=mock_pool):
            result = await connector.check_availability("myapp", "rijks.app")

        assert result is True
        mock_conn.fetchval.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_availability_returns_false_when_taken(self):
        """check_availability returns False when subdomain is already registered."""
        connector = SubdomainConnector()

        # Mock the database pool and connection
        mock_conn = AsyncMock()
        mock_conn.fetchval.return_value = 1  # Found = not available

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with patch.object(connector, "_get_pool", return_value=mock_pool):
            result = await connector.check_availability("myapp", "rijks.app")

        assert result is False

    @pytest.mark.asyncio
    async def test_check_availability_lowercases_input(self):
        """check_availability lowercases subdomain and base_domain."""
        connector = SubdomainConnector()

        mock_conn = AsyncMock()
        mock_conn.fetchval.return_value = None

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with patch.object(connector, "_get_pool", return_value=mock_pool):
            await connector.check_availability("MyApp", "RIJKS.APP")

        # Verify the query was called with lowercase values
        call_args = mock_conn.fetchval.call_args
        assert call_args[0][1] == "myapp"  # subdomain
        assert call_args[0][2] == "rijks.app"  # base_domain


class TestSubdomainConnectorRegister:
    """Tests for SubdomainConnector.register method."""

    @pytest.mark.asyncio
    async def test_register_creates_new_registration(self):
        """register creates a new subdomain registration."""
        connector = SubdomainConnector()

        mock_result = {
            "id": 1,
            "subdomain": "myapp",
            "base_domain": "rijks.app",
            "project_name": "my-project",
            "deployment_name": "prod",
            "cluster": "odcn-production",
            "created_at": "2024-01-01T00:00:00Z",
            "created_by": "user@example.com",
        }

        mock_conn = AsyncMock()
        mock_conn.fetchval.return_value = None  # Available
        mock_conn.fetchrow.return_value = mock_result

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with patch.object(connector, "_get_pool", return_value=mock_pool):
            result = await connector.register(
                subdomain="myapp",
                base_domain="rijks.app",
                project_name="my-project",
                deployment_name="prod",
                cluster="odcn-production",
                created_by="user@example.com",
            )

        assert result["subdomain"] == "myapp"
        assert result["project_name"] == "my-project"

    @pytest.mark.asyncio
    async def test_register_raises_error_when_not_available(self):
        """register raises SubdomainNotAvailableError when subdomain is taken."""
        connector = SubdomainConnector()

        mock_conn = AsyncMock()
        mock_conn.fetchval.return_value = 1  # Not available
        mock_conn.fetchrow.return_value = {"project_name": "other-project"}

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with patch.object(connector, "_get_pool", return_value=mock_pool):
            with pytest.raises(SubdomainNotAvailableError) as exc_info:
                await connector.register(
                    subdomain="myapp",
                    base_domain="rijks.app",
                    project_name="my-project",
                    deployment_name="prod",
                    cluster="odcn-production",
                )

        assert "already registered" in str(exc_info.value)


class TestSubdomainConnectorGetBySubdomain:
    """Tests for SubdomainConnector.get_by_subdomain method."""

    @pytest.mark.asyncio
    async def test_get_by_subdomain_returns_registration(self):
        """get_by_subdomain returns registration details when found."""
        connector = SubdomainConnector()

        mock_result = {
            "id": 1,
            "subdomain": "myapp",
            "base_domain": "rijks.app",
            "project_name": "my-project",
            "deployment_name": "prod",
            "cluster": "odcn-production",
            "created_at": "2024-01-01T00:00:00Z",
            "created_by": "user@example.com",
        }

        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = mock_result

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with patch.object(connector, "_get_pool", return_value=mock_pool):
            result = await connector.get_by_subdomain("myapp", "rijks.app")

        assert result is not None
        assert result["subdomain"] == "myapp"
        assert result["project_name"] == "my-project"

    @pytest.mark.asyncio
    async def test_get_by_subdomain_returns_none_when_not_found(self):
        """get_by_subdomain returns None when subdomain is not registered."""
        connector = SubdomainConnector()

        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = None

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with patch.object(connector, "_get_pool", return_value=mock_pool):
            result = await connector.get_by_subdomain("nonexistent", "rijks.app")

        assert result is None


class TestSubdomainConnectorGetByProject:
    """Tests for SubdomainConnector.get_by_project method."""

    @pytest.mark.asyncio
    async def test_get_by_project_returns_registrations(self):
        """get_by_project returns all registrations for a project."""
        connector = SubdomainConnector()

        mock_results = [
            {
                "id": 1,
                "subdomain": "myapp",
                "base_domain": "rijks.app",
                "project_name": "my-project",
                "deployment_name": "prod",
                "cluster": "odcn-production",
                "created_at": None,
                "created_by": None,
            },
            {
                "id": 2,
                "subdomain": "myapp",
                "base_domain": "rijksapps.nl",
                "project_name": "my-project",
                "deployment_name": "staging",
                "cluster": "local",
                "created_at": None,
                "created_by": None,
            },
        ]

        mock_conn = AsyncMock()
        mock_conn.fetch.return_value = mock_results

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with patch.object(connector, "_get_pool", return_value=mock_pool):
            result = await connector.get_by_project("my-project")

        assert len(result) == 2
        assert all(r["project_name"] == "my-project" for r in result)


class TestSubdomainConnectorDelete:
    """Tests for SubdomainConnector.delete method."""

    @pytest.mark.asyncio
    async def test_delete_returns_true_when_deleted(self):
        """delete returns True when registration is deleted."""
        connector = SubdomainConnector()

        mock_conn = AsyncMock()
        mock_conn.execute.return_value = "DELETE 1"

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with patch.object(connector, "_get_pool", return_value=mock_pool):
            result = await connector.delete("myapp", "rijks.app")

        assert result is True

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_not_found(self):
        """delete returns False when registration is not found."""
        connector = SubdomainConnector()

        mock_conn = AsyncMock()
        mock_conn.execute.return_value = "DELETE 0"

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with patch.object(connector, "_get_pool", return_value=mock_pool):
            result = await connector.delete("nonexistent", "rijks.app")

        assert result is False


class TestSubdomainConnectorDeleteByProject:
    """Tests for SubdomainConnector.delete_by_project method."""

    @pytest.mark.asyncio
    async def test_delete_by_project_returns_count(self):
        """delete_by_project returns the number of deleted registrations."""
        connector = SubdomainConnector()

        mock_conn = AsyncMock()
        mock_conn.execute.return_value = "DELETE 3"

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock(return_value=mock_conn)
        mock_pool.release = AsyncMock()

        with patch.object(connector, "_get_pool", return_value=mock_pool):
            result = await connector.delete_by_project("my-project")

        assert result == 3
