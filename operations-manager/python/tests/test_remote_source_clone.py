"""
Tests for remote-source clone functionality.

Tests that DatabaseManager and MinioManager correctly handle clone-from
with type: remote-source by calling the appropriate methods with Chisel tunnel support.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

# Load the test project file
TEST_PROJECT_PATH = Path(
    "/Users/robbertuittenbroek/IdeaProjects/rig-cluster-test-git-repositories/"
    "rig-cluster-projects-github/projects/amt-136.yaml"
)


@pytest.fixture
def project_data() -> dict:
    """Load the amt-136 project data for testing."""
    with open(TEST_PROJECT_PATH) as f:
        return yaml.safe_load(f)


@pytest.fixture
def deployment(project_data: dict) -> dict:
    """Get the deployment-1 configuration."""
    return project_data["deployments"][0]


@pytest.fixture
def mock_project_manager() -> MagicMock:
    """Create a mock ProjectManager."""
    pm = MagicMock()
    pm.get_name = AsyncMock(return_value="amt-136")
    pm.get_contents = AsyncMock()
    pm._add_secret_to_create = MagicMock()
    pm._secrets_to_create = {}
    pm.get_progress_manager = MagicMock(return_value=None)
    return pm


@pytest.fixture
def mock_postgres_connector() -> MagicMock:
    """Create a mock PostgresConnector."""
    connector = MagicMock()
    connector.create_user = AsyncMock(return_value={"status": "created"})
    connector.create_database = AsyncMock(return_value={"status": "created"})
    connector.clone_schema = AsyncMock(return_value={"status": "success"})
    return connector


class TestDatabaseManagerRemoteSourceClone:
    """Tests for DatabaseManager remote-source clone handling."""

    @pytest.mark.asyncio
    async def test_ensure_database_state_calls_clone_for_remote_source(
        self, project_data: dict, deployment: dict, mock_project_manager: MagicMock, mock_postgres_connector: MagicMock
    ):
        """Test that _ensure_database_state correctly handles remote-source clone."""
        from opi.manager.database_manager import DatabaseManager

        # Setup
        mock_project_manager.get_contents.return_value = project_data

        with (
            patch.object(DatabaseManager, "clone_database_from_external_source", new_callable=AsyncMock) as mock_clone,
            patch("opi.utils.age.get_decoded_project_private_key", new_callable=AsyncMock) as mock_get_key,
            patch("opi.utils.age.decrypt_password_smart", new_callable=AsyncMock) as mock_decrypt,
        ):
            # Configure mocks
            mock_clone.return_value = {"success": True, "operations": []}
            mock_get_key.return_value = "AGE-SECRET-KEY-FAKE"
            mock_decrypt.return_value = "decrypted_password"

            db_manager = DatabaseManager(
                mock_project_manager,
                db_host="localhost",
                admin_username="postgres",
                admin_password="postgres",
            )
            db_manager._postgres_connector = mock_postgres_connector

            # Execute
            await db_manager._ensure_database_state(
                project_name="amt-136",
                deployment_name="deployment-1",
                deployment=deployment,
                db_database="amt_136_deployment_1",
                db_schema="amt_136_deployment_1",
                db_username="amt_136_deployment_1",
                db_password="test_password",
                project_data=project_data,
            )

            # Verify clone_database_from_external_source was called
            mock_clone.assert_called_once()
            call_kwargs = mock_clone.call_args.kwargs

            # Verify correct parameters
            assert call_kwargs["project_name"] == "amt-136"
            assert call_kwargs["deployment_name"] == "deployment-1"
            assert call_kwargs["source_host"] == "amt-cluster-db-r"
            assert call_kwargs["source_port"] == 5432
            assert call_kwargs["source_username"] == "tad"
            assert call_kwargs["source_database"] == "tad"
            assert call_kwargs["source_schema"] == "public"
            assert call_kwargs["force_clone"] is False
            # Chisel config should be passed
            assert call_kwargs["chisel_config"] is not None
            assert call_kwargs["chisel_config"]["server-url"] == "https://chisel-server.prd.apps.digilab.network"
            assert call_kwargs["chisel_config"]["username"] == "admin"

    @pytest.mark.asyncio
    async def test_clone_database_from_external_source_with_chisel(
        self, project_data: dict, mock_project_manager: MagicMock, mock_postgres_connector: MagicMock
    ):
        """Test that clone_database_from_external_source uses chisel tunnel when config provided."""
        from opi.manager.database_manager import DatabaseManager

        mock_project_manager.get_contents.return_value = project_data

        chisel_config = {
            "server-url": "https://chisel-server.example.com",
            "username": "admin",
            "password": "chisel_pass",
        }

        with patch.object(
            DatabaseManager, "_clone_database_with_chisel_tunnel", new_callable=AsyncMock
        ) as mock_tunnel_clone:
            mock_tunnel_clone.return_value = {"success": True, "operations": []}

            db_manager = DatabaseManager(
                mock_project_manager,
                db_host="localhost",
                admin_username="postgres",
                admin_password="postgres",
            )
            db_manager._postgres_connector = mock_postgres_connector

            # Execute with chisel_config
            result = await db_manager.clone_database_from_external_source(
                project_name="amt-136",
                deployment_name="deployment-1",
                source_host="db.example.com",
                source_port=5432,
                source_username="user",
                source_password="pass",
                source_database="mydb",
                source_schema="myschema",
                chisel_config=chisel_config,
                project_data=project_data,
            )

            # Should have called the tunnel method
            mock_tunnel_clone.assert_called_once()
            call_kwargs = mock_tunnel_clone.call_args.kwargs
            assert call_kwargs["chisel_config"] == chisel_config

    @pytest.mark.asyncio
    async def test_clone_database_from_external_source_without_chisel(
        self, project_data: dict, mock_project_manager: MagicMock, mock_postgres_connector: MagicMock
    ):
        """Test that clone_database_from_external_source works directly without chisel."""
        from opi.manager.database_manager import DatabaseManager

        mock_project_manager.get_contents.return_value = project_data

        with patch.object(DatabaseManager, "_execute_external_clone", new_callable=AsyncMock) as mock_execute:
            mock_execute.return_value = {"success": True, "operations": []}

            db_manager = DatabaseManager(
                mock_project_manager,
                db_host="localhost",
                admin_username="postgres",
                admin_password="postgres",
            )
            db_manager._postgres_connector = mock_postgres_connector

            # Execute without chisel_config
            result = await db_manager.clone_database_from_external_source(
                project_name="amt-136",
                deployment_name="deployment-1",
                source_host="db.example.com",
                source_port=5432,
                source_username="user",
                source_password="pass",
                source_database="mydb",
                source_schema="myschema",
                chisel_config=None,  # No chisel
            )

            # Should have called the direct execute method
            mock_execute.assert_called_once()


class TestMinioManagerRemoteSourceClone:
    """Tests for MinioManager remote-source clone handling."""

    @pytest.mark.asyncio
    async def test_handle_remote_source_clone_calls_clone_method(
        self, project_data: dict, mock_project_manager: MagicMock
    ):
        """Test that _handle_remote_source_clone correctly calls clone_bucket_from_external_source."""
        from opi.manager.minio_manager import MinioManager

        with (
            patch.object(MinioManager, "clone_bucket_from_external_source", new_callable=AsyncMock) as mock_clone,
            patch("opi.utils.age.get_decoded_project_private_key", new_callable=AsyncMock) as mock_get_key,
            patch("opi.utils.age.decrypt_password_smart", new_callable=AsyncMock) as mock_decrypt,
        ):
            mock_clone.return_value = {"success": True, "operations": []}
            mock_get_key.return_value = "AGE-SECRET-KEY-FAKE"
            mock_decrypt.return_value = "decrypted_value"

            minio_manager = MinioManager(mock_project_manager)

            # Execute
            await minio_manager._handle_remote_source_clone(
                project_name="amt-136",
                deployment_name="deployment-1",
                remote_source_name="odcn-production",
                project_data=project_data,
                force_clone=False,
            )

            # Verify clone_bucket_from_external_source was called
            mock_clone.assert_called_once()
            call_kwargs = mock_clone.call_args.kwargs

            # Verify correct parameters from project file
            assert call_kwargs["project_name"] == "amt-136"
            assert call_kwargs["deployment_name"] == "deployment-1"
            assert call_kwargs["source_host"] == "amt-svc-minio"
            assert call_kwargs["source_port"] == 9000
            assert call_kwargs["source_bucket"] == "amt"
            assert call_kwargs["source_secure"] is False
            assert call_kwargs["force_clone"] is False
            # Chisel config should be passed
            assert call_kwargs["chisel_config"] is not None
            assert call_kwargs["chisel_config"]["server-url"] == "https://chisel-server.prd.apps.digilab.network"

    @pytest.mark.asyncio
    async def test_clone_bucket_from_external_source_with_chisel(
        self, project_data: dict, mock_project_manager: MagicMock
    ):
        """Test that clone_bucket_from_external_source uses chisel tunnel when config provided."""
        from opi.manager.minio_manager import MinioManager

        chisel_config = {
            "server-url": "https://chisel-server.example.com",
            "username": "admin",
            "password": "chisel_pass",
        }

        with patch.object(
            MinioManager, "_clone_bucket_with_chisel_tunnel", new_callable=AsyncMock
        ) as mock_tunnel_clone:
            mock_tunnel_clone.return_value = {"success": True, "operations": []}

            minio_manager = MinioManager(mock_project_manager)

            # Execute with chisel_config
            result = await minio_manager.clone_bucket_from_external_source(
                project_name="amt-136",
                deployment_name="deployment-1",
                source_host="minio.example.com",
                source_port=9000,
                source_access_key="access",
                source_secret_key="secret",
                source_bucket="mybucket",
                chisel_config=chisel_config,
                project_data=project_data,
            )

            # Should have called the tunnel method
            mock_tunnel_clone.assert_called_once()


class TestChiselTunnelHelper:
    """Tests for the chisel_tunnel helper."""

    @pytest.mark.asyncio
    async def test_chisel_tunnel_decrypts_password_with_project_key(self, project_data: dict):
        """Test that chisel_tunnel uses project key to decrypt password."""
        from opi.utils.chisel_helper import chisel_tunnel

        chisel_config = {
            "server-url": "https://chisel.example.com",
            "username": "admin",
            "password": "encrypted_password",
        }

        with (
            patch("opi.utils.chisel_helper.get_decoded_project_private_key", new_callable=AsyncMock) as mock_get_key,
            patch("opi.utils.chisel_helper.decrypt_password_smart", new_callable=AsyncMock) as mock_decrypt,
            patch("opi.utils.chisel_helper.ChiselConnector") as mock_connector_class,
        ):
            mock_get_key.return_value = "AGE-SECRET-KEY-PROJECT"
            mock_decrypt.return_value = "decrypted_chisel_password"

            mock_connector = MagicMock()
            mock_connector.get_local_endpoint.return_value = {"host": "localhost", "port": 12345}
            mock_connector_class.return_value = mock_connector

            async with chisel_tunnel(chisel_config, "remote.host", 5432, project_data) as endpoint:
                # Verify password was decrypted with project key
                mock_get_key.assert_called_once_with(project_data)
                mock_decrypt.assert_called_once_with("encrypted_password", "AGE-SECRET-KEY-PROJECT")

                # Verify connector was created with decrypted password
                mock_connector_class.assert_called_once_with(
                    server_url="https://chisel.example.com",
                    username="admin",
                    password="decrypted_chisel_password",
                )

                # Verify tunnel was started
                mock_connector.start_tunnel.assert_called_once_with(remote_host="remote.host", remote_port=5432)

                assert endpoint == {"host": "localhost", "port": 12345}

            # Verify tunnel was stopped on exit
            mock_connector.stop_tunnel.assert_called_once()

    @pytest.mark.asyncio
    async def test_chisel_tunnel_without_project_data(self):
        """Test that chisel_tunnel works without project_data (no decryption)."""
        from opi.utils.chisel_helper import chisel_tunnel

        chisel_config = {
            "server-url": "https://chisel.example.com",
            "username": "admin",
            "password": "plain_password",
        }

        with (
            patch("opi.utils.chisel_helper.get_decoded_project_private_key", new_callable=AsyncMock) as mock_get_key,
            patch("opi.utils.chisel_helper.decrypt_password_smart", new_callable=AsyncMock) as mock_decrypt,
            patch("opi.utils.chisel_helper.ChiselConnector") as mock_connector_class,
        ):
            mock_decrypt.return_value = "plain_password"  # No encryption, returns as-is

            mock_connector = MagicMock()
            mock_connector.get_local_endpoint.return_value = {"host": "localhost", "port": 12345}
            mock_connector_class.return_value = mock_connector

            async with chisel_tunnel(chisel_config, "remote.host", 5432, None) as endpoint:
                # get_decoded_project_private_key should NOT be called when project_data is None
                mock_get_key.assert_not_called()
                # decrypt_password_smart should be called with None as key
                mock_decrypt.assert_called_once_with("plain_password", None)


class TestRemoteSourceConfigExtraction:
    """Tests for remote source configuration extraction."""

    def test_get_remote_source_config_database_manager(self, project_data: dict, mock_project_manager: MagicMock):
        """Test that DatabaseManager._get_remote_source_config correctly extracts config."""
        from opi.manager.database_manager import DatabaseManager

        db_manager = DatabaseManager(
            mock_project_manager,
            db_host="localhost",
            admin_username="postgres",
            admin_password="postgres",
        )

        # Get the remote source config
        config = db_manager._get_remote_source_config(project_data, "odcn-production")

        assert config is not None
        assert config["name"] == "odcn-production"
        assert "chisel" in config
        assert config["chisel"]["server-url"] == "https://chisel-server.prd.apps.digilab.network"
        assert "services" in config
        assert "postgresql-database" in config["services"]
        assert config["services"]["postgresql-database"]["host"] == "amt-cluster-db-r"

    def test_get_remote_source_config_minio_manager(self, project_data: dict, mock_project_manager: MagicMock):
        """Test that MinioManager._get_remote_source_config correctly extracts config."""
        from opi.manager.minio_manager import MinioManager

        minio_manager = MinioManager(mock_project_manager)

        # Get the remote source config
        config = minio_manager._get_remote_source_config(project_data, "odcn-production")

        assert config is not None
        assert config["name"] == "odcn-production"
        assert "services" in config
        assert "minio-storage" in config["services"]
        assert config["services"]["minio-storage"]["host"] == "amt-svc-minio"
        assert config["services"]["minio-storage"]["bucket"] == "amt"

    def test_get_remote_source_config_not_found(self, project_data: dict, mock_project_manager: MagicMock):
        """Test that _get_remote_source_config returns None for unknown source."""
        from opi.manager.database_manager import DatabaseManager

        db_manager = DatabaseManager(
            mock_project_manager,
            db_host="localhost",
            admin_username="postgres",
            admin_password="postgres",
        )

        config = db_manager._get_remote_source_config(project_data, "non-existent-source")

        assert config is None
