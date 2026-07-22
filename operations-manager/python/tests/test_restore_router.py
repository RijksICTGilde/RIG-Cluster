"""Tests for the restore router's write path.

The three restore endpoints mutate cluster state (new-generation PVC/database/
bucket) and must then persist the generation bump to the project file through
the store. A leftover reference to a removed per-request git connector made all
three crash with a NameError after the cluster-side restore, orphaning the
restored resource outside GitOps desired state -- and no test executed these
endpoints, so the suite stayed green. These tests drive each endpoint through
its happy path and pin the read-mutate-save wiring:

* the response is a success, not a 500 from a crash after the restore;
* save_and_commit_project receives the SAME dict get_contents() returned
  (the manager records that read as the compare-and-swap base, so a save of any
  other dict would either lose the mutation or bypass the merge);
* the persisted dict carries the bumped generation.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.api.restore_router import restore_router
from opi.handlers.project_file_handler import create_project_file_handler
from opi.manager.backup import RestoreResult, SnapshotInfo

API_KEY = "test-restore-api-key"
PROJECT = "restore-test"


def _project_file() -> dict[str, Any]:
    """A minimal v2 project file with one deployment and one persistent storage."""
    return {
        "schema-version": 2,
        "name": PROJECT,
        "users": [{"email": "admin@example.com", "role": "admin"}],
        "clusters": ["local"],
        "components": [
            {
                "name": "web",
                "type": "single",
                "services": [
                    {
                        "persistent-storage": {
                            "config": [{"name": "data", "size": "1Gi", "mount-path": "/data"}],
                        }
                    }
                ],
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
def project_data() -> dict[str, Any]:
    return _project_file()


@pytest.fixture
def mock_manager(project_data: dict[str, Any]) -> Any:
    """Patch ProjectManager in the router with a manager whose read returns project_data."""
    manager = MagicMock()
    manager.get_contents = AsyncMock(return_value=project_data)
    manager.save_and_commit_project = AsyncMock()
    manager.process_project_from_git = AsyncMock(return_value={"status": "ok"})
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
def mock_backup_manager() -> Any:
    backup_manager = MagicMock()

    def _restore(**kwargs: Any) -> RestoreResult:
        return RestoreResult(
            namespace=kwargs.get("namespace", ""),
            pvc_name=kwargs.get("source_pvc_name", ""),
            success=True,
            target_pvc_name=kwargs.get("target_pvc_name"),
            snapshot_id=kwargs.get("snapshot_id"),
        )

    backup_manager.restore_to_project_pvc = AsyncMock(side_effect=_restore)
    backup_manager.list_snapshots = AsyncMock(return_value=[])
    with patch("opi.api.restore_router.create_backup_manager", return_value=backup_manager):
        yield backup_manager


@pytest.fixture
def mock_cluster_functions() -> Any:
    with (
        patch("opi.api.restore_router.get_prefixed_namespace", side_effect=lambda cluster, ns: f"rig-{ns}"),
        patch("opi.api.restore_router.get_storage_class_name", return_value="standard"),
        patch("opi.api.restore_router.get_storage_access_modes", return_value=["ReadWriteOnce"]),
    ):
        yield


def _assert_read_mutate_save(mock_manager: Any, project_data: dict[str, Any]) -> None:
    """The save must persist the very dict the manager read and the handler mutated,
    and that dict must carry the bumped storage generation."""
    mock_manager.save_and_commit_project.assert_awaited_once()
    saved = mock_manager.save_and_commit_project.await_args.args[0]
    assert saved is project_data
    generation = create_project_file_handler().get_storage_generation(saved, "main", "web", "data")
    assert generation == 1


class TestRestoreProjectPvc:
    def test_happy_path_persists_generation_bump(
        self,
        client: TestClient,
        project_data: dict[str, Any],
        mock_store: Any,
        mock_manager: Any,
        mock_backup_manager: Any,
        mock_cluster_functions: Any,
    ) -> None:
        response = client.post(
            f"/api/v1/restore/project/{PROJECT}",
            headers={"X-API-Key": API_KEY},
            json={"deployment_name": "main", "component_name": "web", "storage_name": "data"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "success"
        assert body["project_updated"] is True
        assert body["new_generation"] == 1

        _assert_read_mutate_save(mock_manager, project_data)

    def test_missing_project_file_is_404(
        self,
        client: TestClient,
        mock_store: Any,
        mock_manager: Any,
        mock_backup_manager: Any,
        mock_cluster_functions: Any,
    ) -> None:
        mock_manager.get_contents = AsyncMock(side_effect=ValueError("Project file not found"))
        response = client.post(
            f"/api/v1/restore/project/{PROJECT}",
            headers={"X-API-Key": API_KEY},
            json={"deployment_name": "main", "component_name": "web", "storage_name": "data"},
        )
        assert response.status_code == 404
        mock_manager.save_and_commit_project.assert_not_awaited()


class TestRestoreBackupRun:
    def test_happy_path_persists_generation_bump(
        self,
        client: TestClient,
        project_data: dict[str, Any],
        mock_store: Any,
        mock_manager: Any,
        mock_backup_manager: Any,
        mock_cluster_functions: Any,
    ) -> None:
        mock_backup_manager.list_snapshots = AsyncMock(
            return_value=[
                SnapshotInfo(
                    snapshot_id="snap-1",
                    pvc_name="pvc-main-web-data-0",
                    timestamp="2026-07-21T00:00:00Z",
                    component_name="web",
                    storage_name="data",
                    generation=0,
                    backup_run_id="run-1",
                    resource_type="pvc",
                )
            ]
        )
        response = client.post(
            f"/api/v1/restore/project/{PROJECT}/deployment/main/run/run-1",
            headers={"X-API-Key": API_KEY},
            json={},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "success"
        assert body["project_updated"] is True

        _assert_read_mutate_save(mock_manager, project_data)


class TestRestoreDeploymentResource:
    def test_happy_path_persists_generation_bump(
        self,
        client: TestClient,
        project_data: dict[str, Any],
        mock_store: Any,
        mock_manager: Any,
        mock_backup_manager: Any,
        mock_cluster_functions: Any,
    ) -> None:
        response = client.post(
            f"/api/v1/restore/project/{PROJECT}/deployment/main",
            headers={"X-API-Key": API_KEY},
            json={
                "resource_type": "pvc",
                "component_name": "web",
                "reference_name": "data",
                "snapshot_id": "snap-1",
                "update_deployment": False,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "success"
        assert body["project_updated"] is True
        assert body["new_generation"] == 1

        _assert_read_mutate_save(mock_manager, project_data)
