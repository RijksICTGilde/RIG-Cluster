"""A restore that fails on the destination the CALLER named must say so, machine-readably.

Asked by the zad-cli project (question 9): a restore to a host that does not resolve
answered 500 with the pod logs in ``message`` and no category at all. Their exit code
is derived from the status code, so 500 became "platform, retry later" and a pipeline
kept retrying a typo in ``--target-host`` forever.

The signal is a dedicated EXIT CODE from the restore pod, not a search through its log
text. The pod already tests the destination before it touches any data (``psql -c
"SELECT 1"`` for a database, ``mc alias set`` for a bucket); that gate now exits with
``RESTORE_TARGET_UNUSABLE_EXIT_CODE``. Matching on ``could not translate host name``
would be the very mistake the CLI climbed out of at question 1: PostgreSQL is free to
reword its errors, an exit code we choose ourselves is not.

What is pinned here:

* both restore templates exit with the dedicated code on their destination gate, and
  the gate sits before any data is written;
* the managers turn that exit code -- and only that exit code -- into
  ``target_unusable`` on the result;
* the endpoints answer 400 + ``InvalidTarget`` for a caller-named destination, and
  keep 500 + ``Unknown`` for everything else;
* a restore WITHOUT target fields never gets the category, however the pod failed:
  the platform chose that destination, so it can never be the caller's fault (RC-81);
* the credential the caller supplied is not echoed back in the category path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from opi.api.restore_router import restore_router
from opi.api.v2.models import ErrorCategory
from opi.core.backup_constants import RESTORE_TARGET_UNUSABLE_EXIT_CODE
from opi.manager.backup import BucketRestoreResult, DatabaseRestoreResult
from opi.manager.backup.base import BackupConfig, BaseBackupManager
from opi.utils.secrets import DatabaseSecret, MinIOSecret

_MANIFESTS_DIR = Path(__file__).parent.parent / "manifests"

API_KEY = "test-restore-api-key"
PROJECT = "restore-test"
NAMESPACE = f"rig-{PROJECT}"

# The password the caller sends for an external destination. It must never come back.
CALLER_PASSWORD = "caller-supplied-password"

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

EXTERNAL_DATABASE = {
    "target_database_host": "doel.invalid",
    "target_database_name": "d",
    "target_database_user": "u",
    "target_database_password": CALLER_PASSWORD,
}
EXTERNAL_BUCKET = {
    "target_minio_endpoint": "http://doel.invalid:9000",
    "target_bucket_name": "b",
    "target_access_key": "a",
    "target_secret_key": CALLER_PASSWORD,
}


# ---------------------------------------------------------------------------
# The templates: the destination gate exits with the dedicated code
# ---------------------------------------------------------------------------


@pytest.fixture
def env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_MANIFESTS_DIR)),
        undefined=StrictUndefined,
        autoescape=False,
    )


_DATABASE_CTX: dict[str, Any] = {
    "pod_name": "db-restore-test",
    "namespace": NAMESPACE,
    "reference_name": "main-database",
    "target_db_host": "doel.invalid",
    "target_db_port": 5432,
    "target_db_name": "d",
    "target_db_user": "u",
    "target_db_password": CALLER_PASSWORD,
    "s3_endpoint": "minio.example:9000",
    "s3_bucket": "backups",
    "s3_access_key": "key",
    "s3_secret_key": "secret",
    "s3_disable_tls": True,
    "backup_prefix": "local/rig-test",
    "kopia_password": "pw",
    "snapshot_id": "",
    "timeout_seconds": 3600,
    "target_unusable_exit_code": RESTORE_TARGET_UNUSABLE_EXIT_CODE,
    # The default schema of the database the dump came from (RC-121); see
    # tests/test_restore_schema_rename.py for what the pod does with it.
    "source_schema": "restore_test_main",
}

_BUCKET_CTX: dict[str, Any] = {
    "pod_name": "bucket-restore-test",
    "namespace": NAMESPACE,
    "reference_name": "main-minio",
    "target_minio_endpoint": "http://doel.invalid:9000",
    "target_bucket_name": "b",
    "target_access_key": "a",
    "target_secret_key": CALLER_PASSWORD,
    "s3_endpoint": "minio.example:9000",
    "s3_bucket": "backups",
    "s3_access_key": "key",
    "s3_secret_key": "secret",
    "s3_disable_tls": True,
    "backup_prefix": "local/rig-test",
    "kopia_password": "pw",
    "snapshot_id": "",
    "timeout_seconds": 3600,
    "clear_target": False,
    "target_unusable_exit_code": RESTORE_TARGET_UNUSABLE_EXIT_CODE,
}


def _script(env: Environment, template: str, ctx: dict[str, Any]) -> str:
    pod = yaml.safe_load(env.get_template(template).render(**ctx))
    return pod["spec"]["containers"][0]["command"][-1]


class TestRestorePodTemplates:
    def test_database_gate_exits_with_the_dedicated_code(self, env: Environment) -> None:
        script = _script(env, "restore-database-pod.yaml.jinja", _DATABASE_CTX)

        assert f"exit {RESTORE_TARGET_UNUSABLE_EXIT_CODE}" in script
        # The gate is the connectivity probe, and it runs before anything is restored.
        assert script.index("Testing target database connectivity") < script.index("pg_restore \\")

    def test_bucket_gate_exits_with_the_dedicated_code(self, env: Environment) -> None:
        script = _script(env, "restore-bucket-pod.yaml.jinja", _BUCKET_CTX)

        assert f"exit {RESTORE_TARGET_UNUSABLE_EXIT_CODE}" in script
        # mc validates endpoint and keys when the alias is set, before any mirroring.
        assert script.index("Configuring MinIO client for target") < script.index("mc mirror")

    @pytest.mark.parametrize(
        ("template", "ctx"),
        [
            ("restore-database-pod.yaml.jinja", _DATABASE_CTX),
            ("restore-bucket-pod.yaml.jinja", _BUCKET_CTX),
        ],
    )
    def test_the_gate_message_names_the_fields_and_not_the_password(
        self, env: Environment, template: str, ctx: dict[str, Any]
    ) -> None:
        script = _script(env, template, ctx)
        gate_line = next(line for line in script.splitlines() if "ERROR: target" in line)

        assert CALLER_PASSWORD not in gate_line
        assert "rejected" in gate_line


# ---------------------------------------------------------------------------
# The managers: only the dedicated exit code becomes target_unusable
# ---------------------------------------------------------------------------


class _Manager(BaseBackupManager):
    """BaseBackupManager with a kubectl connector we drive by hand."""

    def __init__(self, pod_json: str, exit_code: int = 0) -> None:
        self.config = BackupConfig(
            s3_endpoint="minio.example:9000",
            s3_bucket="backups",
            s3_access_key="key",
            s3_secret_key="secret",
        )
        self.kubectl = MagicMock()
        self.kubectl.run_command = AsyncMock(return_value=(pod_json, "", exit_code))


def _pod_with_exit_code(exit_code: int | None) -> str:
    state: dict[str, Any] = {} if exit_code is None else {"terminated": {"exitCode": exit_code}}
    return json.dumps({"status": {"containerStatuses": [{"state": state}]}})


class TestExitCodeReading:
    async def test_the_dedicated_exit_code_means_the_target_was_unusable(self) -> None:
        manager = _Manager(_pod_with_exit_code(RESTORE_TARGET_UNUSABLE_EXIT_CODE))

        assert await manager._restore_target_was_unusable(NAMESPACE, "pod") is True

    @pytest.mark.parametrize("exit_code", [0, 1, 2, 137, None])
    async def test_every_other_outcome_stays_our_side(self, exit_code: int | None) -> None:
        manager = _Manager(_pod_with_exit_code(exit_code))

        assert await manager._restore_target_was_unusable(NAMESPACE, "pod") is False

    async def test_an_unreadable_pod_is_not_reported_as_the_callers_fault(self) -> None:
        manager = _Manager("", exit_code=1)

        assert await manager._restore_target_was_unusable(NAMESPACE, "pod") is False

    async def test_unparsable_pod_status_is_not_reported_as_the_callers_fault(self) -> None:
        manager = _Manager("not json")

        assert await manager._restore_target_was_unusable(NAMESPACE, "pod") is False


# ---------------------------------------------------------------------------
# The endpoints: 400 + InvalidTarget for a caller-named destination only
# ---------------------------------------------------------------------------


def _project_file() -> dict[str, Any]:
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
    secrets = {
        "main-database": OWN_DATABASE.to_k8s_secret_data(),
        "main-minio": OWN_BUCKET.to_k8s_secret_data(),
    }
    kubectl = MagicMock()
    kubectl.get_secret = AsyncMock(side_effect=lambda name, namespace: secrets.get(name))
    with patch("opi.api.restore_router.create_kubectl_connector", return_value=kubectl):
        yield kubectl


def _failing_database_manager(target_unusable: bool) -> Any:
    manager = MagicMock()
    manager.restore_database = AsyncMock(
        side_effect=lambda **kwargs: DatabaseRestoreResult(
            namespace=kwargs["namespace"],
            pvc_name=kwargs["reference_name"],
            reference_name=kwargs["reference_name"],
            target_database_name=kwargs["target_database_name"],
            success=False,
            error='Restore pod failed. Logs: psql: error: could not translate host name "doel.invalid"',
            target_unusable=target_unusable,
        )
    )
    return patch("opi.api.restore_router.create_database_backup_manager", return_value=manager)


def _failing_bucket_manager(target_unusable: bool) -> Any:
    manager = MagicMock()
    manager.restore_bucket = AsyncMock(
        side_effect=lambda **kwargs: BucketRestoreResult(
            namespace=kwargs["namespace"],
            pvc_name=kwargs["reference_name"],
            reference_name=kwargs["reference_name"],
            target_bucket_name=kwargs["target_bucket_name"],
            success=False,
            error="Restore pod failed. Logs: mc: Unable to initialize new alias",
            target_unusable=target_unusable,
        )
    )
    return patch("opi.api.restore_router.create_bucket_backup_manager", return_value=manager)


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


@pytest.mark.usefixtures("mock_store", "mock_namespace", "mock_kubectl")
class TestDatabaseRestoreFailureCategory:
    def test_an_unusable_caller_named_target_is_a_400_with_invalid_target(self, client: TestClient) -> None:
        with _failing_database_manager(target_unusable=True):
            response = _post_database(client, EXTERNAL_DATABASE)

        assert response.status_code == 400, response.text
        assert response.json()["error_category"] == ErrorCategory.InvalidTarget.value
        assert response.json()["status"] == "failed"

    def test_a_failure_on_our_side_stays_a_500_and_stays_distinguishable(self, client: TestClient) -> None:
        with _failing_database_manager(target_unusable=False):
            response = _post_database(client, EXTERNAL_DATABASE)

        assert response.status_code == 500, response.text
        assert response.json()["error_category"] == ErrorCategory.Unknown.value

    def test_without_target_fields_the_platform_chose_the_destination_so_it_is_never_the_caller(
        self, client: TestClient
    ) -> None:
        # Even when the pod reports the destination gate: the caller never named it (RC-81).
        with _failing_database_manager(target_unusable=True):
            response = _post_database(client, {})

        assert response.status_code == 500, response.text
        assert response.json()["error_category"] == ErrorCategory.Unknown.value

    def test_the_category_path_does_not_echo_the_supplied_password(self, client: TestClient) -> None:
        with _failing_database_manager(target_unusable=True):
            response = _post_database(client, EXTERNAL_DATABASE)

        assert CALLER_PASSWORD not in response.text

    def test_a_successful_restore_carries_no_category(self, client: TestClient) -> None:
        manager = MagicMock()
        manager.restore_database = AsyncMock(
            side_effect=lambda **kwargs: DatabaseRestoreResult(
                namespace=kwargs["namespace"],
                pvc_name=kwargs["reference_name"],
                reference_name=kwargs["reference_name"],
                target_database_name=kwargs["target_database_name"],
                success=True,
            )
        )
        with patch("opi.api.restore_router.create_database_backup_manager", return_value=manager):
            response = _post_database(client, EXTERNAL_DATABASE)

        assert response.status_code == 200, response.text
        assert "error_category" not in response.json()


@pytest.mark.usefixtures("mock_store", "mock_namespace", "mock_kubectl")
class TestBucketRestoreFailureCategory:
    def test_an_unusable_caller_named_target_is_a_400_with_invalid_target(self, client: TestClient) -> None:
        with _failing_bucket_manager(target_unusable=True):
            response = _post_bucket(client, EXTERNAL_BUCKET)

        assert response.status_code == 400, response.text
        assert response.json()["error_category"] == ErrorCategory.InvalidTarget.value

    def test_a_failure_on_our_side_stays_a_500(self, client: TestClient) -> None:
        with _failing_bucket_manager(target_unusable=False):
            response = _post_bucket(client, EXTERNAL_BUCKET)

        assert response.status_code == 500, response.text
        assert response.json()["error_category"] == ErrorCategory.Unknown.value

    def test_without_target_fields_it_is_never_the_caller(self, client: TestClient) -> None:
        with _failing_bucket_manager(target_unusable=True):
            response = _post_bucket(client, {})

        assert response.status_code == 500, response.text
        assert response.json()["error_category"] == ErrorCategory.Unknown.value

    def test_the_category_path_does_not_echo_the_supplied_secret_key(self, client: TestClient) -> None:
        with _failing_bucket_manager(target_unusable=True):
            response = _post_bucket(client, EXTERNAL_BUCKET)

        assert CALLER_PASSWORD not in response.text
