"""Test that exposes the generation tracking bug on second upsert.

After a first clone into the base-name database (no generational failover),
record_clone records generation=1 instead of generation=0. On the second
upsert, the system reads generation=1 and generates a versioned database
name (e.g. proj_deploy_v1), creating a new empty database instead of
reusing the existing one.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.manager.database_manager import DatabaseManager
from opi.manager.revision_manager import RevisionManager
from opi.utils.naming import generate_database_name


@pytest.fixture
def project_data() -> dict:
    return {
        "name": "test-project",
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
    pm.report_clone_performed = MagicMock()
    return pm


@pytest.fixture
def mock_postgres_connector() -> MagicMock:
    connector = MagicMock()
    connector.create_database = AsyncMock(return_value={"status": "created"})
    connector.clone_schema = AsyncMock(return_value={"status": "success"})
    connector.execute_init_sql = AsyncMock(return_value={"status": "success"})
    return connector


@pytest.mark.asyncio
async def test_first_clone_records_generation_zero_for_base_name(
    project_data: dict,
    mock_project_manager: MagicMock,
    mock_postgres_connector: MagicMock,
) -> None:
    """After cloning into the base-name database (no failover), generation must be 0.

    If generation is recorded as 1, the next upsert will generate a versioned
    database name (proj_deploy_v1) and create an empty database — the bug.
    """
    db_manager = DatabaseManager(
        mock_project_manager,
        db_host="localhost",
        admin_username="postgres",
        admin_password="postgres",
    )
    db_manager._postgres_connector = mock_postgres_connector

    deployment = project_data["deployments"][1]  # staging with clone-from

    # Simulate first clone: generation=None (new deployment), DB doesn't exist yet
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

    # Verify clone was executed
    mock_postgres_connector.clone_schema.assert_called_once()

    # Read back the generation that was recorded
    from opi.handlers.project_file_handler import ProjectFileHandler

    handler = ProjectFileHandler()
    recorded_gen = handler.get_deployment_service_generation(project_data, "staging", "postgresql-database")

    # Generation 0 means base name (no _v suffix).
    # Generation 1 would mean _v1 suffix — which is WRONG because the clone
    # went into the base-name database, not a versioned one.
    assert recorded_gen == 0, (
        f"Expected generation 0 (base name) after first clone, got {recorded_gen}. "
        f"This means the second upsert will use generate_database_name(..., {recorded_gen}) "
        f"= '{generate_database_name('test-project', 'staging', recorded_gen)}' "
        f"instead of the correct '{generate_database_name('test-project', 'staging', 0)}'"
    )


@pytest.mark.asyncio
async def test_second_upsert_reuses_same_database_name(
    project_data: dict,
    mock_project_manager: MagicMock,
    mock_postgres_connector: MagicMock,
) -> None:
    """After first clone + completed status, second upsert must use the same DB name.

    This is the end-to-end scenario: first upsert clones, then a second upsert
    (with clone status completed) should resolve to the exact same database name.
    """
    db_manager = DatabaseManager(
        mock_project_manager,
        db_host="localhost",
        admin_username="postgres",
        admin_password="postgres",
    )
    db_manager._postgres_connector = mock_postgres_connector

    deployment = project_data["deployments"][1]  # staging with clone-from

    # --- First upsert: clone into base-name database ---
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

    # Simulate what process_project does after clone: mark status completed
    from opi.handlers.project_file_handler import ProjectFileHandler

    handler = ProjectFileHandler()
    handler.set_clone_status(project_data, "staging", completed=True, timestamp="2026-01-01T00:00:00Z")

    # --- Second upsert: read back generation, compute database name ---
    recorded_gen = handler.get_deployment_service_generation(project_data, "staging", "postgresql-database")
    second_upsert_db_name = generate_database_name("test-project", "staging", recorded_gen)
    first_upsert_db_name = "test_project_staging"

    assert second_upsert_db_name == first_upsert_db_name, (
        f"Second upsert would use database '{second_upsert_db_name}' but first clone "
        f"created '{first_upsert_db_name}'. The app would connect to a wrong/empty database."
    )
