"""An external restore target has to stand on a public address (RC-482, decision R9).

The database and bucket restore take a destination from the caller: host, endpoint and
credentials. That path had no egress guard while the clone routes do, and a restore WRITES
data to that destination. A target in private or cluster-internal address space, or a name
that does not resolve, let a caller look past the edge of the cluster and put the backup
there.

What is pinned here: a destination the CALLER named is refused with 422, and the project's
own service is left alone, because the platform chose that one itself.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.api.restore_router import restore_router
from opi.manager.backup import BucketRestoreResult, DatabaseRestoreResult
from opi.services.secrets import DatabaseSecret, MinIOSecret
from opi.utils.egress_guard import REFUSED_EXTERNAL_TARGET_REASON, endpoint_host

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

PRIVATE_DATABASE = {
    "target_database_host": "db.intern.local",
    "target_database_name": "d",
    "target_database_user": "u",
    "target_database_password": "p",
}
PRIVATE_BUCKET = {
    "target_minio_endpoint": "http://minio.intern.local:9000",
    "target_bucket_name": "doelbucket",
    "target_access_key": "a",
    "target_secret_key": "s",
}


def _project_file() -> dict[str, Any]:
    return {
        "schema-version": 2,
        "name": PROJECT,
        "users": [{"email": "admin@example.com", "roles": ["admin", "ops"]}],
        "clusters": ["local"],
        "services": ["postgresql-database", "minio-storage"],
        "components": [{"name": "web", "type": "single", "services": ["postgresql-database", "minio-storage"]}],
        "deployments": [
            {"name": "main", "cluster": "local", "namespace": PROJECT, "components": [{"reference": "web"}]}
        ],
    }


class _FakeStore:
    def __init__(self) -> None:
        project = MagicMock()
        project.name = PROJECT
        project.api_key = API_KEY
        project.filename = f"{PROJECT}.yaml"
        project.data = _project_file()
        self._project = project

    def get(self, project_name: str) -> Any:
        return self._project if project_name == PROJECT else None


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(restore_router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def restore_environment() -> Any:
    """The store, the namespace, the own secrets, and two managers that would succeed."""
    store = _FakeStore()
    kubectl = MagicMock()
    secrets = {
        "main-database": OWN_DATABASE.to_k8s_secret_data(),
        "main-minio": OWN_BUCKET.to_k8s_secret_data(),
    }
    kubectl.get_secret = AsyncMock(side_effect=lambda name, namespace: secrets.get(name))

    database_manager = MagicMock()
    database_manager.restore_database = AsyncMock(
        side_effect=lambda **kwargs: DatabaseRestoreResult(
            namespace=kwargs["namespace"],
            pvc_name=kwargs["reference_name"],
            reference_name=kwargs["reference_name"],
            target_database_name=kwargs["target_database_name"],
            success=True,
        )
    )
    bucket_manager = MagicMock()
    bucket_manager.restore_bucket = AsyncMock(
        side_effect=lambda **kwargs: BucketRestoreResult(
            namespace=kwargs["namespace"],
            pvc_name=kwargs["reference_name"],
            reference_name=kwargs["reference_name"],
            target_bucket_name=kwargs["target_bucket_name"],
            success=True,
        )
    )
    with (
        patch("opi.http.auth.endpoint_util.get_project_store", return_value=store),
        patch("opi.manager.backup.restore_targets.get_project_store", return_value=store),
        patch("opi.api.restore_router.get_prefixed_namespace", side_effect=lambda cluster, ns: f"rig-{ns}"),
        patch("opi.manager.backup.restore_targets.create_kubectl_connector", return_value=kubectl),
        patch("opi.api.restore_router.create_database_backup_manager", return_value=database_manager),
        patch("opi.api.restore_router.create_bucket_backup_manager", return_value=bucket_manager),
    ):
        yield {"database": database_manager, "bucket": bucket_manager}


def _post_database(client: TestClient, body: dict[str, Any]) -> Any:
    return client.post(
        f"/api/v1/restore/database/local/{NAMESPACE}/main-database?project_name={PROJECT}",
        headers={"X-API-Key": API_KEY},
        json=body,
    )


def _post_bucket(client: TestClient, body: dict[str, Any]) -> Any:
    return client.post(
        f"/api/v1/restore/bucket/local/{NAMESPACE}/main-minio?project_name={PROJECT}",
        headers={"X-API-Key": API_KEY},
        json=body,
    )


class TestEndpointHost:
    """The hostname out of the three shapes a destination arrives in."""

    def test_a_url(self) -> None:
        assert endpoint_host("http://minio.publiek.nl:9000") == "minio.publiek.nl"

    def test_a_host_with_a_port(self) -> None:
        assert endpoint_host("db.publiek.nl:5432") == "db.publiek.nl"

    def test_a_bare_host(self) -> None:
        assert endpoint_host("db.publiek.nl") == "db.publiek.nl"


class TestACallerNamedTargetIsWeighed:
    def test_a_private_database_target_is_refused(self, client: TestClient, restore_environment: Any) -> None:
        response = _post_database(client, PRIVATE_DATABASE)

        assert response.status_code == 422, response.text
        assert response.json()["detail"] == REFUSED_EXTERNAL_TARGET_REASON
        restore_environment["database"].restore_database.assert_not_awaited()

    def test_a_private_bucket_target_is_refused(self, client: TestClient, restore_environment: Any) -> None:
        response = _post_bucket(client, PRIVATE_BUCKET)

        assert response.status_code == 422, response.text
        assert response.json()["detail"] == REFUSED_EXTERNAL_TARGET_REASON
        restore_environment["bucket"].restore_bucket.assert_not_awaited()

    def test_a_public_database_target_is_allowed(self, client: TestClient, restore_environment: Any) -> None:
        with patch("opi.utils.egress_guard.host_is_allowed", AsyncMock(return_value=True)):
            response = _post_database(client, PRIVATE_DATABASE)

        assert response.status_code == 200, response.text
        restore_environment["database"].restore_database.assert_awaited_once()


class TestTheProjectsOwnServiceIsNotWeighed:
    """The platform picks that destination itself, and it is cluster-internal by definition."""

    def test_a_restore_without_target_fields_reaches_the_own_database(
        self, client: TestClient, restore_environment: Any
    ) -> None:
        response = _post_database(client, {})

        assert response.status_code == 200, response.text
        call = restore_environment["database"].restore_database.await_args.kwargs
        assert call["target_database_host"] == OWN_DATABASE.host

    def test_a_restore_without_target_fields_reaches_the_own_bucket(
        self, client: TestClient, restore_environment: Any
    ) -> None:
        response = _post_bucket(client, {})

        assert response.status_code == 200, response.text
        call = restore_environment["bucket"].restore_bucket.await_args.kwargs
        assert call["target_bucket_name"] == OWN_BUCKET.bucket_name
