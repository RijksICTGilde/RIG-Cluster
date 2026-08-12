"""What the database and bucket restore endpoints require of a request.

Asked by the zad-cli project: ``POST /api/v1/restore/database/...`` answered 422 and
they suspected the ``reference_name`` in the path. It is not the path - the endpoints
take a JSON body naming the target to restore INTO. These tests pin what that body
must carry, so the answer stays true if the models change.

Since RC-81 the four target fields are OPTIONAL: a project cannot name the credentials
of its own database or bucket (the platform injects them into the pods and publishes
them nowhere), so a request that omits them restores into the project's own service.
Half a target is still a hard error - guessing the missing field would restore
somewhere the caller did not ask for. What is pinned here:

* an empty body validates, for both models;
* a full target survives unchanged - an existing call keeps its behaviour;
* a partial target is rejected and the message names the field that is missing;
* the endpoint without a target resolves the project's own service from the
  deployment secret and hands THOSE connection details to the backup manager;
* a reference that belongs to no deployment, and a deployment without a secret,
  both answer with a readable 404 instead of a stacktrace.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.api.restore_router import (
    BucketRestoreRequest,
    DatabaseRestoreRequest,
    restore_router,
)
from opi.manager.backup import BucketRestoreResult, DatabaseRestoreResult
from opi.utils.secrets import DatabaseSecret, MinIOSecret
from pydantic import ValidationError

API_KEY = "test-restore-api-key"
PROJECT = "restore-test"
NAMESPACE = f"rig-{PROJECT}"

OWN_DATABASE = DatabaseSecret(
    host="postgresql.rig-restore-test.svc.cluster.local",
    port=5432,
    username="restore_test_main",
    password="own-database-password",
    database="restore_test_main",
    schema="restore_test_main",
)
OWN_BUCKET = MinIOSecret(
    host="minio.rig-system.svc.cluster.local",
    port=9000,
    access_key="restore-test-main",
    secret_key="own-bucket-secret",
    bucket_name="restore-test-main",
)


def _missing_fields(exc: ValidationError) -> set[str]:
    return {str(error["loc"][0]) for error in exc.errors() if error["type"] == "missing"}


def _messages(exc: ValidationError) -> str:
    return " ".join(str(error["msg"]) for error in exc.errors())


def _project_file() -> dict[str, Any]:
    """A minimal v2 project file with one deployment using a database and a bucket."""
    return {
        "schema-version": 2,
        "name": PROJECT,
        "users": [{"email": "admin@example.com", "role": "admin"}],
        "clusters": ["local"],
        "components": [
            {
                "name": "web",
                "type": "single",
                "services": ["postgresql-database", "minio-storage"],
            }
        ],
        "deployments": [
            {
                "name": "main",
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
def mock_store() -> Any:
    store = _FakeStore(_project_file())
    with (
        patch("opi.api.endpoint_util.get_project_store", return_value=store),
        patch("opi.api.restore_router.get_project_store", return_value=store),
    ):
        yield store


@pytest.fixture
def mock_namespace() -> Any:
    with patch("opi.api.restore_router.get_prefixed_namespace", side_effect=lambda cluster, ns: f"rig-{ns}"):
        yield


@pytest.fixture
def mock_kubectl() -> Any:
    """A kubectl connector serving the deployment's own database and MinIO secrets."""
    secrets = {
        "main-database": OWN_DATABASE.to_k8s_secret_data(),
        "main-minio": OWN_BUCKET.to_k8s_secret_data(),
    }
    kubectl = MagicMock()
    kubectl.get_secret = AsyncMock(side_effect=lambda name, namespace: secrets.get(name))
    with patch("opi.api.restore_router.create_kubectl_connector", return_value=kubectl):
        yield kubectl


@pytest.fixture
def mock_database_backup_manager() -> Any:
    manager = MagicMock()
    manager.restore_database = AsyncMock(
        side_effect=lambda **kwargs: DatabaseRestoreResult(
            namespace=kwargs["namespace"],
            pvc_name=kwargs["reference_name"],
            reference_name=kwargs["reference_name"],
            target_database_name=kwargs["target_database_name"],
            success=True,
            snapshot_id=kwargs.get("snapshot_id"),
        )
    )
    with patch("opi.api.restore_router.create_database_backup_manager", return_value=manager):
        yield manager


@pytest.fixture
def mock_bucket_backup_manager() -> Any:
    manager = MagicMock()
    manager.restore_bucket = AsyncMock(
        side_effect=lambda **kwargs: BucketRestoreResult(
            namespace=kwargs["namespace"],
            pvc_name=kwargs["reference_name"],
            reference_name=kwargs["reference_name"],
            target_bucket_name=kwargs["target_bucket_name"],
            success=True,
            snapshot_id=kwargs.get("snapshot_id"),
        )
    )
    with patch("opi.api.restore_router.create_bucket_backup_manager", return_value=manager):
        yield manager


class TestDatabaseRestoreRequest:
    def test_empty_body_restores_into_the_projects_own_database(self):
        request = DatabaseRestoreRequest()

        assert request.target_database_host is None
        assert request.target_database_name is None
        assert request.target_database_user is None
        assert request.target_database_password is None

    def test_snapshot_id_is_optional_and_defaults_to_the_latest(self):
        request = DatabaseRestoreRequest(
            target_database_host="postgresql.rig-p0.svc.cluster.local",
            target_database_name="app",
            target_database_user="app",
            target_database_password="secret",
        )

        assert request.snapshot_id is None
        assert request.target_database_port == 5432

    def test_a_partial_target_is_rejected_naming_the_missing_field(self):
        with pytest.raises(ValidationError) as excinfo:
            DatabaseRestoreRequest(
                target_database_host="postgresql.rig-p0.svc.cluster.local",
                target_database_name="app",
                target_database_user="app",
            )

        message = _messages(excinfo.value)
        assert "target_database_password" in message
        assert not _missing_fields(excinfo.value)

    def test_the_rejection_does_not_echo_the_supplied_values(self):
        with pytest.raises(ValidationError) as excinfo:
            DatabaseRestoreRequest(
                target_database_host="postgresql.rig-p0.svc.cluster.local",
                target_database_name="app",
                target_database_user="app",
            )

        assert "postgresql.rig-p0.svc.cluster.local" not in _messages(excinfo.value)


class TestBucketRestoreRequest:
    def test_empty_body_restores_into_the_projects_own_bucket(self):
        request = BucketRestoreRequest()

        assert request.target_minio_endpoint is None
        assert request.target_bucket_name is None
        assert request.target_access_key is None
        assert request.target_secret_key is None
        assert request.clear_target is False

    def test_a_full_target_survives_unchanged(self):
        request = BucketRestoreRequest(
            target_minio_endpoint="http://minio.example:9000",
            target_bucket_name="restored",
            target_access_key="access",
            target_secret_key="secret",
        )

        assert request.target_bucket_name == "restored"

    def test_a_partial_target_is_rejected_naming_the_missing_field(self):
        with pytest.raises(ValidationError) as excinfo:
            BucketRestoreRequest(
                target_minio_endpoint="http://minio.example:9000",
                target_bucket_name="restored",
                target_access_key="access",
            )

        assert "target_secret_key" in _messages(excinfo.value)


class TestRestoreDatabaseEndpoint:
    def test_without_a_target_it_restores_into_the_projects_own_database(
        self,
        client: TestClient,
        mock_store: Any,
        mock_namespace: Any,
        mock_kubectl: Any,
        mock_database_backup_manager: Any,
    ) -> None:
        response = client.post(
            f"/api/v1/restore/database/local/{NAMESPACE}/main-database?project_name={PROJECT}",
            headers={"X-API-Key": API_KEY},
            json={},
        )

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "success"
        assert response.json()["result"]["target_database_name"] == OWN_DATABASE.database

        call = mock_database_backup_manager.restore_database.await_args.kwargs
        assert call["target_database_host"] == OWN_DATABASE.host
        assert call["target_database_port"] == OWN_DATABASE.port
        assert call["target_database_name"] == OWN_DATABASE.database
        assert call["target_database_user"] == OWN_DATABASE.username
        assert call["target_database_password"] == OWN_DATABASE.password

    def test_an_explicit_target_still_wins(
        self,
        client: TestClient,
        mock_store: Any,
        mock_namespace: Any,
        mock_kubectl: Any,
        mock_database_backup_manager: Any,
    ) -> None:
        response = client.post(
            f"/api/v1/restore/database/local/{NAMESPACE}/main-database?project_name={PROJECT}",
            headers={"X-API-Key": API_KEY},
            json={
                "target_database_host": "postgresql.elsewhere:5432",
                "target_database_port": 6432,
                "target_database_name": "elsewhere",
                "target_database_user": "elsewhere",
                "target_database_password": "elsewhere-secret",
            },
        )

        assert response.status_code == 200, response.text
        call = mock_database_backup_manager.restore_database.await_args.kwargs
        assert call["target_database_host"] == "postgresql.elsewhere:5432"
        assert call["target_database_port"] == 6432
        assert call["target_database_name"] == "elsewhere"
        mock_kubectl.get_secret.assert_not_awaited()

    def test_a_partial_target_is_a_422_naming_the_missing_field(
        self,
        client: TestClient,
        mock_store: Any,
        mock_namespace: Any,
        mock_kubectl: Any,
        mock_database_backup_manager: Any,
    ) -> None:
        response = client.post(
            f"/api/v1/restore/database/local/{NAMESPACE}/main-database?project_name={PROJECT}",
            headers={"X-API-Key": API_KEY},
            json={
                "target_database_host": "postgresql.elsewhere:5432",
                "target_database_name": "elsewhere",
                "target_database_user": "elsewhere",
            },
        )

        assert response.status_code == 422, response.text
        assert "target_database_password" in response.text
        mock_database_backup_manager.restore_database.assert_not_awaited()

    def test_the_component_service_reference_resolves_to_the_same_deployment(
        self,
        client: TestClient,
        mock_store: Any,
        mock_namespace: Any,
        mock_kubectl: Any,
        mock_database_backup_manager: Any,
    ) -> None:
        """A backup registered under the component's service reference (``main-postgresql``)
        resolves to deployment ``main`` just as the deployment-level fallback
        (``main-database``) does - the two naming schemes both occur in backup runs."""
        response = client.post(
            f"/api/v1/restore/database/local/{NAMESPACE}/main-postgresql?project_name={PROJECT}",
            headers={"X-API-Key": API_KEY},
            json={},
        )

        assert response.status_code == 200, response.text
        call = mock_database_backup_manager.restore_database.await_args.kwargs
        assert call["target_database_name"] == OWN_DATABASE.database

    def test_an_unknown_reference_is_a_readable_404(
        self,
        client: TestClient,
        mock_store: Any,
        mock_namespace: Any,
        mock_kubectl: Any,
        mock_database_backup_manager: Any,
    ) -> None:
        response = client.post(
            f"/api/v1/restore/database/local/{NAMESPACE}/no-such-backup?project_name={PROJECT}",
            headers={"X-API-Key": API_KEY},
            json={},
        )

        assert response.status_code == 404, response.text
        assert "no-such-backup" in response.json()["detail"]
        mock_database_backup_manager.restore_database.assert_not_awaited()

    def test_a_deployment_without_a_database_secret_is_a_readable_404(
        self,
        client: TestClient,
        mock_store: Any,
        mock_namespace: Any,
        mock_kubectl: Any,
        mock_database_backup_manager: Any,
    ) -> None:
        mock_kubectl.get_secret = AsyncMock(return_value=None)

        response = client.post(
            f"/api/v1/restore/database/local/{NAMESPACE}/main-database?project_name={PROJECT}",
            headers={"X-API-Key": API_KEY},
            json={},
        )

        assert response.status_code == 404, response.text
        assert "main" in response.json()["detail"]
        mock_database_backup_manager.restore_database.assert_not_awaited()


class TestRestoreBucketEndpoint:
    def test_without_a_target_it_restores_into_the_projects_own_bucket(
        self,
        client: TestClient,
        mock_store: Any,
        mock_namespace: Any,
        mock_kubectl: Any,
        mock_bucket_backup_manager: Any,
    ) -> None:
        response = client.post(
            f"/api/v1/restore/bucket/local/{NAMESPACE}/main-minio?project_name={PROJECT}",
            headers={"X-API-Key": API_KEY},
            json={},
        )

        assert response.status_code == 200, response.text
        assert response.json()["result"]["target_bucket_name"] == OWN_BUCKET.bucket_name

        call = mock_bucket_backup_manager.restore_bucket.await_args.kwargs
        assert call["target_minio_endpoint"] == OWN_BUCKET.endpoint_url
        assert call["target_bucket_name"] == OWN_BUCKET.bucket_name
        assert call["target_access_key"] == OWN_BUCKET.access_key
        assert call["target_secret_key"] == OWN_BUCKET.secret_key

    def test_an_explicit_target_still_wins(
        self,
        client: TestClient,
        mock_store: Any,
        mock_namespace: Any,
        mock_kubectl: Any,
        mock_bucket_backup_manager: Any,
    ) -> None:
        response = client.post(
            f"/api/v1/restore/bucket/local/{NAMESPACE}/main-minio?project_name={PROJECT}",
            headers={"X-API-Key": API_KEY},
            json={
                "target_minio_endpoint": "http://minio.elsewhere:9000",
                "target_bucket_name": "elsewhere",
                "target_access_key": "access",
                "target_secret_key": "secret",
            },
        )

        assert response.status_code == 200, response.text
        call = mock_bucket_backup_manager.restore_bucket.await_args.kwargs
        assert call["target_bucket_name"] == "elsewhere"
        mock_kubectl.get_secret.assert_not_awaited()

    def test_a_partial_target_is_a_422_naming_the_missing_field(
        self,
        client: TestClient,
        mock_store: Any,
        mock_namespace: Any,
        mock_kubectl: Any,
        mock_bucket_backup_manager: Any,
    ) -> None:
        response = client.post(
            f"/api/v1/restore/bucket/local/{NAMESPACE}/main-minio?project_name={PROJECT}",
            headers={"X-API-Key": API_KEY},
            json={
                "target_minio_endpoint": "http://minio.elsewhere:9000",
                "target_bucket_name": "elsewhere",
                "target_access_key": "access",
            },
        )

        assert response.status_code == 422, response.text
        assert "target_secret_key" in response.text
        mock_bucket_backup_manager.restore_bucket.assert_not_awaited()

    def test_a_project_without_a_bucket_is_a_readable_404(
        self,
        client: TestClient,
        mock_store: Any,
        mock_namespace: Any,
        mock_kubectl: Any,
        mock_bucket_backup_manager: Any,
    ) -> None:
        response = client.post(
            f"/api/v1/restore/bucket/local/{NAMESPACE}/no-such-bucket?project_name={PROJECT}",
            headers={"X-API-Key": API_KEY},
            json={},
        )

        assert response.status_code == 404, response.text
        assert "no-such-bucket" in response.json()["detail"]
        mock_bucket_backup_manager.restore_bucket.assert_not_awaited()
