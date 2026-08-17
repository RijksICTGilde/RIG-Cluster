"""Where a database's and a bucket's generation is stored, and what a repeated restore does.

A restore is supposed to put the recovered data in a resource with a NEW name -- ``{db}``,
``{db}_v1``, ``{db}_v2`` -- so the previous generation stays untouched and the deployment is
switched over by the follow-up refresh. That did not happen: every restore round reported
``0 -> 1`` again, because the write and the read used two different places in the project file.

* the write went to the DEPLOYMENT-level services block
  (``set_deployment_service_generation``, restore_router);
* the read came from the COMPONENT-level reference/config block
  (``get_database_generation`` -> ``_get_service_config_generation``).

So the second restore computed the same target name as the first, found the database already
there, and dumped the backup into it a second time. pg_restore adds rows, so they doubled.

A database and a bucket are named after the project and the DEPLOYMENT only
(``{project}_{deployment}_v{gen}``), so their generation is a property of the deployment; a PVC
carries the component in its name and keeps its component-level generation. These tests pin
that placement, the migration of files written under the old one, and the refusal to write into
a target that already holds data.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.api.restore_router import restore_router
from opi.handlers.project_file_handler import create_project_file_handler
from opi.manager.backup import BucketRestoreResult, DatabaseRestoreResult
from opi.services.schema_migration import relocate_resource_generations_to_deployment

API_KEY = "test-restore-api-key"
PROJECT = "restore-gen"
DEPLOYMENT = "main"


def _project_file() -> dict[str, Any]:
    """A minimal v2 project with one deployment using the shared PostgreSQL and MinIO."""
    return {
        "schema-version": 2,
        "name": PROJECT,
        "users": [{"email": "admin@example.com", "role": "admin"}],
        "clusters": ["local"],
        "services": ["postgresql-database", "minio-storage"],
        "components": [
            {
                "name": "web",
                "type": "single",
                "services": ["postgresql-database", "minio-storage"],
            }
        ],
        "deployments": [
            {
                "name": DEPLOYMENT,
                "cluster": "local",
                "namespace": PROJECT,
                "components": [{"reference": "web"}],
            }
        ],
    }


class _FakeStore:
    """Just enough of the ProjectStore read interface for auth and project lookup."""

    def __init__(self, data: dict[str, Any]) -> None:
        project = MagicMock()
        project.name = PROJECT
        project.api_key = API_KEY
        project.filename = f"{PROJECT}.yaml"
        project.data = data
        self._project = project

    def get(self, project_name: str) -> Any:
        return self._project if project_name == PROJECT else None


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(restore_router)
    return TestClient(app)


@pytest.fixture
def project_data() -> dict[str, Any]:
    return _project_file()


@pytest.fixture
def mock_manager(project_data: dict[str, Any]) -> Any:
    manager = MagicMock()
    manager.get_contents = AsyncMock(return_value=project_data)
    manager.save_and_commit_project = AsyncMock()
    manager.process_project_from_git = AsyncMock(return_value=True)
    with patch("opi.api.restore_router.ProjectManager", return_value=manager):
        yield manager


@pytest.fixture
def mock_store(project_data: dict[str, Any]) -> Any:
    store = _FakeStore(project_data)
    with (
        patch("opi.api.endpoint_util.get_project_store", return_value=store),
        patch("opi.api.restore_router.get_project_store", return_value=store),
    ):
        yield store


@pytest.fixture
def postgres_connector() -> Any:
    """A postgres connector whose target database is created fresh and stays empty."""
    connector = MagicMock()
    connector.create_user = AsyncMock(return_value={"status": "created"})
    connector.update_user_password = AsyncMock(return_value={"status": "updated"})
    connector.create_database = AsyncMock(return_value={"status": "created"})
    connector.create_schema = AsyncMock(return_value={"status": "created"})
    connector.database_has_user_data = AsyncMock(return_value=False)
    connector.close = AsyncMock()
    return connector


@pytest.fixture
def minio_connector() -> Any:
    connector = MagicMock()
    connector.configure_alias = AsyncMock(return_value=True)
    connector.create_bucket = AsyncMock(return_value={"status": "created"})
    connector.create_user = AsyncMock(return_value={"status": "created"})
    connector.grant_bucket_access = AsyncMock(return_value={"status": "granted"})
    connector.bucket_has_objects = AsyncMock(return_value=False)
    return connector


@pytest.fixture
def restore_environment(postgres_connector: Any, minio_connector: Any) -> Any:
    """Everything the database and bucket restore paths reach outside the project file."""
    database_backup_manager = MagicMock()
    database_backup_manager.restore_database = AsyncMock(
        return_value=DatabaseRestoreResult(namespace="rig-" + PROJECT, pvc_name="", success=True, snapshot_id="snap-1")
    )
    bucket_backup_manager = MagicMock()
    bucket_backup_manager.restore_bucket = AsyncMock(
        return_value=BucketRestoreResult(namespace="rig-" + PROJECT, pvc_name="", success=True, snapshot_id="snap-1")
    )
    secret = MagicMock()
    secret.password = "existing-password"
    with (
        patch("opi.connectors.postgres.create_postgres_connector", return_value=postgres_connector),
        patch("opi.connectors.minio_mc.create_minio_connector", return_value=minio_connector),
        patch("opi.core.cluster_config.get_database_server", return_value="postgres.local"),
        patch("opi.api.restore_router.get_prefixed_namespace", side_effect=lambda cluster, ns: f"rig-{ns}"),
        patch("opi.api.restore_router.create_kubectl_connector", return_value=MagicMock()),
        patch("opi.api.restore_router.DatabaseSecret.get_data", AsyncMock(return_value=secret)),
        patch("opi.api.restore_router.create_database_backup_manager", return_value=database_backup_manager),
        patch("opi.api.restore_router.create_bucket_backup_manager", return_value=bucket_backup_manager),
    ):
        yield {
            "database_backup_manager": database_backup_manager,
            "bucket_backup_manager": bucket_backup_manager,
        }


def _restore(client: TestClient, resource_type: str) -> Any:
    return client.post(
        f"/api/v1/restore/project/{PROJECT}/deployment/{DEPLOYMENT}",
        headers={"X-API-Key": API_KEY},
        json={
            "resource_type": resource_type,
            "component_name": "web",
            "reference_name": "db" if resource_type == "database" else "bucket",
            "snapshot_id": "snap-1",
            "update_deployment": False,
        },
    )


class TestSecondRestoreTargetsANewResource:
    """The regression this task exists for: restore twice, get two different targets."""

    def test_two_database_restores_produce_two_different_databases(
        self,
        client: TestClient,
        project_data: dict[str, Any],
        mock_store: Any,
        mock_manager: Any,
        restore_environment: Any,
        postgres_connector: Any,
    ) -> None:
        first = _restore(client, "database")
        assert first.status_code == 200, first.text
        assert first.json()["old_generation"] == 0
        assert first.json()["new_generation"] == 1

        # The saved project file is what the next restore reads back. Before the fix the
        # generation landed somewhere the reader never looked, so this second round
        # recomputed 0 -> 1 and aimed at the database the first round had just filled.
        second = _restore(client, "database")
        assert second.status_code == 200, second.text
        assert second.json()["old_generation"] == 1
        assert second.json()["new_generation"] == 2

        first_target = first.json()["new_resource_name"]
        second_target = second.json()["new_resource_name"]
        assert first_target != second_target
        assert first_target.endswith("_v1")
        assert second_target.endswith("_v2")
        # And the second round restored INTO the new name, not the old one.
        assert (
            restore_environment["database_backup_manager"].restore_database.await_args.kwargs["target_database_name"]
            == second_target
        )

    def test_two_bucket_restores_produce_two_different_buckets(
        self,
        client: TestClient,
        project_data: dict[str, Any],
        mock_store: Any,
        mock_manager: Any,
        restore_environment: Any,
    ) -> None:
        first = _restore(client, "minio")
        assert first.status_code == 200, first.text
        second = _restore(client, "minio")
        assert second.status_code == 200, second.text

        assert second.json()["old_generation"] == 1
        assert second.json()["new_generation"] == 2
        assert first.json()["new_resource_name"].endswith("-v1")
        assert second.json()["new_resource_name"].endswith("-v2")

    def test_generation_is_written_where_provisioning_reads_it(
        self,
        client: TestClient,
        project_data: dict[str, Any],
        mock_store: Any,
        mock_manager: Any,
        restore_environment: Any,
    ) -> None:
        """database_manager decides the live database name from the deployment-level block."""
        assert _restore(client, "database").status_code == 200

        handler = create_project_file_handler()
        assert handler.get_deployment_service_generation(project_data, DEPLOYMENT, "postgresql-database") == 1
        assert handler.get_database_generation(project_data, DEPLOYMENT) == 1

    def test_namespace_postgresql_generation_lands_under_its_own_service_name(
        self,
        client: TestClient,
        project_data: dict[str, Any],
        mock_store: Any,
        mock_manager: Any,
        restore_environment: Any,
    ) -> None:
        """A dedicated-cluster project stores the generation under the service it declares."""
        project_data["services"] = ["namespace-postgresql-database"]
        assert _restore(client, "database").status_code == 200

        handler = create_project_file_handler()
        assert handler.get_deployment_service_generation(project_data, DEPLOYMENT, "namespace-postgresql-database") == 1
        assert handler.get_deployment_service_generation(project_data, DEPLOYMENT, "postgresql-database") is None


class TestRestoreRefusesANonEmptyTarget:
    """Even with the generation right, a restore must never dump into populated data.

    The wrong number was only the trigger. The damage was the write, so the refusal sits at
    the write. An existing but EMPTY target is a half-finished earlier attempt and is allowed.
    """

    def test_database_restore_refuses_when_the_target_holds_data(
        self,
        client: TestClient,
        project_data: dict[str, Any],
        mock_store: Any,
        mock_manager: Any,
        restore_environment: Any,
        postgres_connector: Any,
    ) -> None:
        postgres_connector.create_database = AsyncMock(return_value={"status": "exists"})
        postgres_connector.database_has_user_data = AsyncMock(return_value=True)

        response = _restore(client, "database")
        assert response.status_code == 500, response.text
        assert "already exists and is not empty" in response.json()["message"]
        restore_environment["database_backup_manager"].restore_database.assert_not_awaited()
        mock_manager.save_and_commit_project.assert_not_awaited()

    def test_database_restore_continues_into_an_existing_empty_database(
        self,
        client: TestClient,
        project_data: dict[str, Any],
        mock_store: Any,
        mock_manager: Any,
        restore_environment: Any,
        postgres_connector: Any,
    ) -> None:
        postgres_connector.create_database = AsyncMock(return_value={"status": "exists"})
        postgres_connector.database_has_user_data = AsyncMock(return_value=False)

        response = _restore(client, "database")
        assert response.status_code == 200, response.text
        restore_environment["database_backup_manager"].restore_database.assert_awaited_once()

    def test_bucket_restore_refuses_when_the_target_holds_objects(
        self,
        client: TestClient,
        project_data: dict[str, Any],
        mock_store: Any,
        mock_manager: Any,
        restore_environment: Any,
        minio_connector: Any,
    ) -> None:
        minio_connector.create_bucket = AsyncMock(return_value={"status": "exists"})
        minio_connector.bucket_has_objects = AsyncMock(return_value=True)

        response = _restore(client, "minio")
        assert response.status_code == 500, response.text
        assert "already exists and is not empty" in response.json()["message"]
        restore_environment["bucket_backup_manager"].restore_bucket.assert_not_awaited()


def _with_component_generation(service_type: str, generation: int) -> dict[str, Any]:
    """A project file as the old writer left it: the generation under the component."""
    data = _project_file()
    data["deployments"][0]["components"][0]["services"] = {
        service_type: [{"reference": "db", "config": {"generation": generation}}]
    }
    return data


class TestGenerationRelocation:
    """Files written under the old placement must arrive at the new one intact."""

    def test_component_generation_moves_up_to_the_deployment(self) -> None:
        data = _with_component_generation("postgresql-database", 3)

        assert relocate_resource_generations_to_deployment(data) is True

        handler = create_project_file_handler()
        assert handler.get_database_generation(data, DEPLOYMENT) == 3
        assert "services" not in data["deployments"][0]["components"][0]

    def test_bucket_generation_moves_up_to_the_deployment(self) -> None:
        data = _project_file()
        data["deployments"][0]["components"][0]["services"] = {
            "minio-storage": [{"reference": "bucket", "config": {"generation": 2}}]
        }

        assert relocate_resource_generations_to_deployment(data) is True
        assert create_project_file_handler().get_bucket_generation(data, DEPLOYMENT) == 2

    def test_the_higher_of_two_conflicting_generations_wins(self, caplog: pytest.LogCaptureFixture) -> None:
        """A value lower than reality names a database that already exists -- never pick it."""
        data = _with_component_generation("postgresql-database", 5)
        data["deployments"][0]["services"] = [{"reference": "postgresql-database", "config": {"generation": 2}}]

        with caplog.at_level("WARNING"):
            assert relocate_resource_generations_to_deployment(data) is True

        assert create_project_file_handler().get_database_generation(data, DEPLOYMENT) == 5
        # Not silent: both numbers and the choice are on the record.
        assert "Conflicting" in caplog.text
        assert "deployment-level 2" in caplog.text
        assert "component-level 5" in caplog.text

    def test_a_deployment_level_value_higher_than_the_component_one_is_kept(self) -> None:
        data = _with_component_generation("postgresql-database", 1)
        data["deployments"][0]["services"] = [{"reference": "postgresql-database", "config": {"generation": 4}}]

        assert relocate_resource_generations_to_deployment(data) is True
        assert create_project_file_handler().get_database_generation(data, DEPLOYMENT) == 4

    def test_a_bare_string_service_entry_keeps_its_place_and_gains_the_generation(self) -> None:
        data = _with_component_generation("postgresql-database", 2)
        data["deployments"][0]["services"] = ["publish-on-web", "postgresql-database"]

        assert relocate_resource_generations_to_deployment(data) is True

        services = data["deployments"][0]["services"]
        assert services[0] == "publish-on-web"
        assert services[1]["config"]["generation"] == 2
        assert len(services) == 2

    def test_storage_generations_are_left_alone(self) -> None:
        """A PVC name carries the component, so its generation stays component-level."""
        data = _project_file()
        data["deployments"][0]["components"][0]["services"] = {
            "persistent-storage": [{"reference": "data", "config": {"generation": 2}}]
        }

        assert relocate_resource_generations_to_deployment(data) is False
        assert create_project_file_handler().get_storage_generation(data, DEPLOYMENT, "web", "data") == 2

    def test_relocation_is_idempotent(self) -> None:
        data = _with_component_generation("postgresql-database", 3)

        assert relocate_resource_generations_to_deployment(data) is True
        assert relocate_resource_generations_to_deployment(data) is False
        assert create_project_file_handler().get_database_generation(data, DEPLOYMENT) == 3

    def test_a_file_without_generations_is_untouched(self) -> None:
        data = _project_file()
        assert relocate_resource_generations_to_deployment(data) is False

    def test_relocation_runs_on_load(self) -> None:
        """migrate_to_latest is what every read path goes through, so the repair is automatic."""
        from opi.services.schema_migration import migrate_to_latest

        data = _with_component_generation("postgresql-database", 3)
        migrated, was_migrated = migrate_to_latest(data)

        assert was_migrated is True
        assert create_project_file_handler().get_database_generation(migrated, DEPLOYMENT) == 3


class TestDeploymentServiceGenerationSetter:
    """The setter must find the same entry the getter does, whatever form it is written in."""

    @pytest.mark.parametrize(
        "entry",
        [
            "minio-storage",
            {"name": "minio-storage"},
            {"reference": "minio-storage"},
            {"minio-storage": {"config": {}}},
        ],
    )
    def test_every_entry_form_is_updated_in_place(self, entry: Any) -> None:
        data = _project_file()
        data["deployments"][0]["services"] = [entry]
        handler = create_project_file_handler()

        handler.set_deployment_service_generation(data, DEPLOYMENT, "minio-storage", 7)

        services = data["deployments"][0]["services"]
        assert len(services) == 1, "a second entry for the same service hides the first from the getter"
        assert handler.get_deployment_service_generation(data, DEPLOYMENT, "minio-storage") == 7
