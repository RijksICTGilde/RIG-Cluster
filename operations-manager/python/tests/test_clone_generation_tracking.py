"""Tests for generation tracking across all clone paths.

When a clone creates a resource using the base name (no version suffix),
generation must be recorded as 0. Recording 1 causes the next upsert to
derive a versioned name (e.g. bucket-v1 or db_v1) that doesn't match
the actual resource - breaking the deployment.

Covers:
- MinIO internal clone (deployment-to-deployment)
- MinIO external clone (remote source)
- Database internal clone (deployment-to-deployment)
- Database external clone (remote source)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.manager.database_manager import DatabaseManager
from opi.manager.minio_manager import MinioManager
from opi.manager.revision_manager import RevisionManager
from opi.services import ServiceType
from opi.utils.naming import generate_bucket_name, generate_database_name

# --- Fixtures ---


@pytest.fixture
def project_data_with_minio_clone() -> dict:
    """Project with a deployment that clones MinIO from another deployment."""
    return {
        "name": "test-project",
        "services": [{"type": "minio-storage"}],
        "deployments": [
            {
                "name": "production",
                "cluster": "local",
                "namespace": "test-project",
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
            {
                "name": "staging",
                "cluster": "local",
                "namespace": "test-project",
                "components": [{"reference": "web", "image": "nginx:latest"}],
                "clone-from": {
                    "type": "deployment",
                    "reference": "production",
                    "mode": "once",
                },
            },
        ],
    }


@pytest.fixture
def project_data_with_db_clone() -> dict:
    """Project with a deployment that clones a database from another deployment."""
    return {
        "name": "test-project",
        "services": [{"type": "postgresql-database"}],
        "deployments": [
            {
                "name": "production",
                "cluster": "local",
                "namespace": "test-project",
                "components": [{"reference": "web", "image": "nginx:latest"}],
            },
            {
                "name": "staging",
                "cluster": "local",
                "namespace": "test-project",
                "components": [{"reference": "web", "image": "nginx:latest"}],
                "clone-from": {
                    "type": "deployment",
                    "reference": "production",
                    "mode": "once",
                },
            },
        ],
    }


@pytest.fixture
def mock_project_manager() -> MagicMock:
    pm = MagicMock()
    pm._revision_manager = RevisionManager(MagicMock())
    pm._project_file_handler = ProjectFileHandler()
    pm.report_clone_performed = MagicMock()
    pm.get_name = AsyncMock(return_value="test-project")
    pm._add_secret_to_create = MagicMock()
    return pm


# --- Naming contract tests ---


class TestGenerationNamingContract:
    """Verify the naming functions' generation contract that the clone code relies on."""

    def test_bucket_name_generation_none_is_base_name(self) -> None:
        """generation=None produces the base bucket name (no suffix)."""
        assert generate_bucket_name("proj", "deploy", None) == "proj-deploy"

    def test_bucket_name_generation_zero_is_base_name(self) -> None:
        """generation=0 produces the same base bucket name as None."""
        assert generate_bucket_name("proj", "deploy", 0) == "proj-deploy"

    def test_bucket_name_generation_one_is_versioned(self) -> None:
        """generation=1 produces a versioned bucket name with -v1 suffix."""
        assert generate_bucket_name("proj", "deploy", 1) == "proj-deploy-v1"

    def test_database_name_generation_none_is_base_name(self) -> None:
        """generation=None produces the base database name (no suffix)."""
        assert generate_database_name("proj", "deploy", None) == "proj_deploy"

    def test_database_name_generation_zero_is_base_name(self) -> None:
        """generation=0 produces the same base database name as None."""
        assert generate_database_name("proj", "deploy", 0) == "proj_deploy"

    def test_database_name_generation_one_is_versioned(self) -> None:
        """generation=1 produces a versioned database name with _v1 suffix."""
        assert generate_database_name("proj", "deploy", 1) == "proj_deploy_v1"


# --- MinIO clone generation tests ---


class TestMinioCloneGenerationTracking:
    """Verify MinIO clone records generation=0 for base-name buckets."""

    @pytest.mark.asyncio
    async def test_fresh_minio_clone_records_generation_zero(
        self,
        project_data_with_minio_clone: dict,
        mock_project_manager: MagicMock,
    ) -> None:
        """First MinIO clone (generation=None) must record generation 0, not 1.

        If recorded as 1, the next upsert generates bucket name 'proj-deploy-v1'
        instead of the actual 'proj-deploy' - the app gets AccessDenied.
        """
        minio_manager = MinioManager(mock_project_manager)
        project_data = project_data_with_minio_clone
        target_deployment = project_data["deployments"][1]  # staging

        mock_connector = MagicMock()
        mock_connector.configure_alias = AsyncMock(return_value=True)
        mock_connector.list_buckets = AsyncMock(return_value=(True, []))

        with (
            patch.object(minio_manager, "_get_deployment_bucket_generation", return_value=None),
            patch.object(minio_manager, "_check_bucket_exists", AsyncMock(side_effect=[True, False])),
            patch.object(minio_manager, "_validate_and_fix_credentials", AsyncMock(return_value=None)),
            patch.object(minio_manager, "_create_user_and_bucket_for_clone", AsyncMock()),
            patch.object(minio_manager, "_copy_bucket_data", AsyncMock()),
            patch.object(minio_manager, "_apply_bucket_versioning", AsyncMock()),
            patch.object(minio_manager, "_get_minio_service_config", return_value={}),
            patch("opi.manager.minio_manager.create_minio_connector", return_value=mock_connector),
            patch("opi.manager.minio_manager.settings") as mock_settings,
            patch("opi.manager.minio_manager.get_minio_host", return_value="minio.test"),
            patch("opi.manager.minio_manager.get_minio_port", return_value="9000"),
        ):
            mock_settings.MINIO_HOST = "minio.test"
            mock_settings.MINIO_ADMIN_ACCESS_KEY = "admin"
            mock_settings.MINIO_ADMIN_SECRET_KEY = "secret"
            mock_settings.MINIO_USE_TLS = False
            mock_settings.MINIO_REGION = "us-east-1"

            await minio_manager.clone_minio_from_deployment(
                project_data=project_data,
                target_deployment=target_deployment,
                source_deployment_name="production",
            )

        # Read back the generation that was recorded
        handler = ProjectFileHandler()
        recorded_gen = handler.get_deployment_service_generation(
            project_data, "staging", ServiceType.MINIO_STORAGE.value
        )

        # Generation 0 = base name (no -v suffix)
        # Generation 1 = versioned name (-v1) - WRONG for initial clone
        assert recorded_gen == 0, (
            f"Expected generation 0 (base name) after fresh MinIO clone, got {recorded_gen}. "
            f"Next upsert would derive bucket '{generate_bucket_name('test-project', 'staging', recorded_gen)}' "
            f"instead of the actual '{generate_bucket_name('test-project', 'staging', 0)}'"
        )

    @pytest.mark.asyncio
    async def test_second_upsert_uses_same_bucket_name(
        self,
        project_data_with_minio_clone: dict,
        mock_project_manager: MagicMock,
    ) -> None:
        """After first clone + second upsert, the bucket name must match."""
        minio_manager = MinioManager(mock_project_manager)
        project_data = project_data_with_minio_clone
        target_deployment = project_data["deployments"][1]

        mock_connector = MagicMock()
        mock_connector.configure_alias = AsyncMock(return_value=True)
        mock_connector.list_buckets = AsyncMock(return_value=(True, []))

        with (
            patch.object(minio_manager, "_get_deployment_bucket_generation", return_value=None),
            patch.object(minio_manager, "_check_bucket_exists", AsyncMock(side_effect=[True, False])),
            patch.object(minio_manager, "_validate_and_fix_credentials", AsyncMock(return_value=None)),
            patch.object(minio_manager, "_create_user_and_bucket_for_clone", AsyncMock()),
            patch.object(minio_manager, "_copy_bucket_data", AsyncMock()),
            patch.object(minio_manager, "_apply_bucket_versioning", AsyncMock()),
            patch.object(minio_manager, "_get_minio_service_config", return_value={}),
            patch("opi.manager.minio_manager.create_minio_connector", return_value=mock_connector),
            patch("opi.manager.minio_manager.settings") as mock_settings,
            patch("opi.manager.minio_manager.get_minio_host", return_value="minio.test"),
            patch("opi.manager.minio_manager.get_minio_port", return_value="9000"),
        ):
            mock_settings.MINIO_HOST = "minio.test"
            mock_settings.MINIO_ADMIN_ACCESS_KEY = "admin"
            mock_settings.MINIO_ADMIN_SECRET_KEY = "secret"
            mock_settings.MINIO_USE_TLS = False
            mock_settings.MINIO_REGION = "us-east-1"

            await minio_manager.clone_minio_from_deployment(
                project_data=project_data,
                target_deployment=target_deployment,
                source_deployment_name="production",
            )

        # Read back generation and derive bucket name for second upsert
        handler = ProjectFileHandler()
        recorded_gen = handler.get_deployment_service_generation(
            project_data, "staging", ServiceType.MINIO_STORAGE.value
        )
        second_upsert_bucket = generate_bucket_name("test-project", "staging", recorded_gen)
        first_clone_bucket = generate_bucket_name("test-project", "staging", None)

        assert second_upsert_bucket == first_clone_bucket, (
            f"Second upsert would use bucket '{second_upsert_bucket}' but first clone "
            f"created '{first_clone_bucket}'. The app would get AccessDenied on MinIO."
        )


# --- Database clone generation tests ---


class TestDatabaseCloneGenerationTracking:
    """Verify database clone records generation=0 for base-name databases."""

    @pytest.mark.asyncio
    async def test_fresh_database_clone_records_generation_zero(
        self,
        project_data_with_db_clone: dict,
        mock_project_manager: MagicMock,
    ) -> None:
        """First database clone (generation=None) must record generation 0, not 1.

        If recorded as 1, the next upsert generates database name 'proj_deploy_v1'
        instead of the actual 'proj_deploy' - the app connects to an empty database.
        """
        mock_postgres = MagicMock()
        mock_postgres.create_database = AsyncMock(return_value={"status": "created"})
        mock_postgres.clone_schema = AsyncMock(return_value={"status": "success"})
        mock_postgres.execute_init_sql = AsyncMock(return_value={"status": "success"})

        db_manager = DatabaseManager(
            mock_project_manager,
            db_host="localhost",
            admin_username="postgres",
            admin_password="postgres",
        )
        db_manager._postgres_connector = mock_postgres

        project_data = project_data_with_db_clone
        deployment = project_data["deployments"][1]  # staging

        with patch.object(db_manager, "_validate_clone_source", new_callable=AsyncMock):
            await db_manager._ensure_database_state(
                project_name="test-project",
                deployment_name="staging",
                deployment=deployment,
                db_database="test_project_staging",
                db_schema="test_project_staging",
                db_username="test_project_staging",
                db_password="secret",
                project_data=project_data,
                force_clone_override=False,
                generation=None,
            )

        handler = ProjectFileHandler()
        recorded_gen = handler.get_deployment_service_generation(
            project_data, "staging", ServiceType.POSTGRESQL_DATABASE.value
        )

        assert recorded_gen == 0, (
            f"Expected generation 0 (base name) after fresh DB clone, got {recorded_gen}. "
            f"Next upsert would derive DB '{generate_database_name('test-project', 'staging', recorded_gen)}' "
            f"instead of the actual '{generate_database_name('test-project', 'staging', 0)}'"
        )

    @pytest.mark.asyncio
    async def test_second_upsert_uses_same_database_name(
        self,
        project_data_with_db_clone: dict,
        mock_project_manager: MagicMock,
    ) -> None:
        """After first clone, second upsert must resolve to the same database name."""
        mock_postgres = MagicMock()
        mock_postgres.create_database = AsyncMock(return_value={"status": "created"})
        mock_postgres.clone_schema = AsyncMock(return_value={"status": "success"})
        mock_postgres.execute_init_sql = AsyncMock(return_value={"status": "success"})

        db_manager = DatabaseManager(
            mock_project_manager,
            db_host="localhost",
            admin_username="postgres",
            admin_password="postgres",
        )
        db_manager._postgres_connector = mock_postgres

        project_data = project_data_with_db_clone
        deployment = project_data["deployments"][1]

        with patch.object(db_manager, "_validate_clone_source", new_callable=AsyncMock):
            await db_manager._ensure_database_state(
                project_name="test-project",
                deployment_name="staging",
                deployment=deployment,
                db_database="test_project_staging",
                db_schema="test_project_staging",
                db_username="test_project_staging",
                db_password="secret",
                project_data=project_data,
                force_clone_override=False,
                generation=None,
            )

        handler = ProjectFileHandler()
        recorded_gen = handler.get_deployment_service_generation(
            project_data, "staging", ServiceType.POSTGRESQL_DATABASE.value
        )
        second_upsert_db = generate_database_name("test-project", "staging", recorded_gen)
        first_clone_db = "test_project_staging"

        assert second_upsert_db == first_clone_db, (
            f"Second upsert would use database '{second_upsert_db}' but first clone "
            f"created '{first_clone_db}'. The app would connect to a wrong/empty database."
        )
