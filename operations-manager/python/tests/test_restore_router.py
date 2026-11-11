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

import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from opi.api.restore_router import restore_router
from opi.manager.backup import RestoreResult, SnapshotInfo
from opi.services.project_file_handler import create_project_file_handler

API_KEY = "test-restore-api-key"
PROJECT = "restore-test"


def _project_file() -> dict[str, Any]:
    """A minimal v2 project file with one deployment and one persistent storage."""
    return {
        "schema-version": 2,
        "name": PROJECT,
        "users": [{"email": "admin@example.com", "roles": ["admin", "ops"]}],
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
    """Patch the ProjectManager the restore use case reads, mutates and commits through."""
    manager = MagicMock()
    manager.get_contents = AsyncMock(return_value=project_data)
    manager.save_and_commit_project = AsyncMock()
    manager.process_project_from_git = AsyncMock(return_value=True)
    manager.close = AsyncMock()
    with (
        patch("opi.manager.backup.restore_run.ProjectManager", return_value=manager),
        patch("opi.runtime.backup.backup_tasks.create_project_manager", return_value=manager),
    ):
        yield manager


@pytest.fixture
def mock_store(project_data: dict[str, Any]) -> Any:
    store = _FakeStore(project_data)
    with (
        patch("opi.http.auth.endpoint_util.get_project_store", return_value=store),
        patch("opi.manager.backup.restore_targets.get_project_store", return_value=store),
        patch("opi.manager.backup.run.get_project_store", return_value=store),
    ):
        yield store


@pytest.fixture(autouse=True)
def inline_task() -> Any:
    """Run the restore task the route queues inline, so the route is measured end to end.

    The routes answer synchronously through the task queue (RC-482); without a worker in
    the test the route would wait for a task nobody runs.
    """
    from opi.runtime.backup.task_handlers_backup import handle_restore

    answers: dict[str, Any] = {}

    async def create(request: Any, task_type: str, project_name: str, deployment_name: str, payload: Any) -> Any:
        answers["result"] = await handle_restore(payload, MagicMock())
        return {"task_id": "restore-task"}

    async def finished(request: Any, task_id: str, answer_key: str) -> Any:
        return {"task_id": task_id, "status": "completed", "result": answers.get("result")}

    with (
        patch("opi.api.restore_router.create_async_task", side_effect=create),
        patch("opi.api.restore_router.wait_for_task_answer", side_effect=finished),
    ):
        yield answers


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
    with (
        patch("opi.api.restore_router.create_backup_manager", return_value=backup_manager),
        patch("opi.manager.backup.restore.create_backup_manager", return_value=backup_manager),
        patch("opi.manager.backup.runs.BackupManager", return_value=backup_manager),
    ):
        yield backup_manager


@pytest.fixture
def mock_cluster_functions() -> Any:
    with (
        patch("opi.api.restore_router.get_prefixed_namespace", side_effect=lambda cluster, ns: f"rig-{ns}"),
        patch("opi.manager.backup.run.get_prefixed_namespace", side_effect=lambda cluster, ns: f"rig-{ns}"),
        patch("opi.manager.backup.restore.get_storage_class_name", return_value="standard"),
        patch("opi.manager.backup.restore.get_storage_access_modes", return_value=["ReadWriteOnce"]),
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

    def test_unknown_deployment_is_404(
        self,
        client: TestClient,
        mock_store: Any,
        mock_manager: Any,
        mock_backup_manager: Any,
        mock_cluster_functions: Any,
    ) -> None:
        response = client.post(
            f"/api/v1/restore/project/{PROJECT}",
            headers={"X-API-Key": API_KEY},
            json={"deployment_name": "ghost", "component_name": "web", "storage_name": "data"},
        )
        assert response.status_code == 404, response.text
        assert "ghost" in response.json()["detail"]
        mock_manager.save_and_commit_project.assert_not_awaited()

    def test_unknown_component_is_404(
        self,
        client: TestClient,
        mock_store: Any,
        mock_manager: Any,
        mock_backup_manager: Any,
        mock_cluster_functions: Any,
    ) -> None:
        response = client.post(
            f"/api/v1/restore/project/{PROJECT}",
            headers={"X-API-Key": API_KEY},
            json={"deployment_name": "main", "component_name": "ghost", "storage_name": "data"},
        )
        assert response.status_code == 404, response.text
        assert "ghost" in response.json()["detail"]
        mock_manager.save_and_commit_project.assert_not_awaited()

    def test_an_unreadable_project_file_is_a_failed_restore(
        self,
        client: TestClient,
        mock_store: Any,
        mock_manager: Any,
        mock_backup_manager: Any,
        mock_cluster_functions: Any,
    ) -> None:
        """The data lands first, so a file the generation cannot reach is a 500, not a 404.

        The read belongs to the commit of the generation, which happens after the restore
        pod ran, and the caller has to learn that the data is there while the deployment
        still points at the previous resource.
        """
        mock_manager.get_contents = AsyncMock(side_effect=ValueError("Project file not found"))
        response = client.post(
            f"/api/v1/restore/project/{PROJECT}",
            headers={"X-API-Key": API_KEY},
            json={"deployment_name": "main", "component_name": "web", "storage_name": "data"},
        )
        assert response.status_code == 500, response.text
        assert response.json()["status"] == "failed"
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

    def test_a_run_with_no_snapshots_is_a_404_before_anything_is_queued(
        self,
        client: TestClient,
        mock_store: Any,
        mock_manager: Any,
        mock_backup_manager: Any,
        mock_cluster_functions: Any,
    ) -> None:
        """Looked up here, not only inside the task (RC-482, decision R11): a run that is
        gone is the caller's 404, not a task that gets created and then fails."""
        response = client.post(
            f"/api/v1/restore/project/{PROJECT}/deployment/main/run/gone-run",
            headers={"X-API-Key": API_KEY},
            json={},
        )

        assert response.status_code == 404, response.text
        assert "gone-run" in response.json()["detail"]
        mock_manager.save_and_commit_project.assert_not_awaited()


class TestTheBackupRunRouteWeighsTheSourceDeployment:
    """The run a restore reads belongs to a deployment of its own (RC-482, decision R8).

    The route gate weighs only the deployment in the PATH, the one the data lands in. A run
    of production restored into a developer's own deployment reads production, and the portal
    has always weighed that source separately.
    """

    @staticmethod
    def _snapshot_of(deployment: str) -> SnapshotInfo:
        return SnapshotInfo(
            snapshot_id="snap-1",
            pvc_name="pvc-main-web-data-0",
            timestamp="2026-07-21T00:00:00Z",
            deployment_name=deployment,
            component_name="web",
            storage_name="data",
            generation=0,
            backup_run_id="run-1",
            resource_type="pvc",
        )

    def _post(self, client: TestClient) -> Any:
        return client.post(
            f"/api/v1/restore/project/{PROJECT}/deployment/main/run/run-1",
            headers={"X-API-Key": API_KEY},
            json={},
        )

    def test_the_source_of_the_run_is_weighed(
        self,
        client: TestClient,
        mock_store: Any,
        mock_manager: Any,
        mock_backup_manager: Any,
        mock_cluster_functions: Any,
    ) -> None:
        mock_backup_manager.list_snapshots = AsyncMock(return_value=[self._snapshot_of("productie")])

        with patch("opi.api.restore_router.require_restore_source_access") as gate:
            response = self._post(client)

        assert response.status_code == 200, response.text
        assert gate.call_args.args[1:] == (PROJECT, "productie")

    def test_a_run_without_a_deployment_tag_still_weighs_a_source(
        self,
        client: TestClient,
        mock_store: Any,
        mock_manager: Any,
        mock_backup_manager: Any,
        mock_cluster_functions: Any,
    ) -> None:
        """An untagged snapshot names no source, and skipping the weighing would let the
        target gate alone decide. The empty name weighs against the highest marking."""
        untagged = self._snapshot_of("productie")
        untagged.deployment_name = None
        mock_backup_manager.list_snapshots = AsyncMock(return_value=[untagged])

        with patch("opi.api.restore_router.require_restore_source_access") as gate:
            response = self._post(client)

        assert response.status_code == 200, response.text
        assert gate.call_args.args[1:] == (PROJECT, "")

    def test_a_refused_source_stops_the_restore(
        self,
        client: TestClient,
        mock_store: Any,
        mock_manager: Any,
        mock_backup_manager: Any,
        mock_cluster_functions: Any,
    ) -> None:
        mock_backup_manager.list_snapshots = AsyncMock(return_value=[self._snapshot_of("productie")])
        refusal = HTTPException(status_code=403, detail="Onvoldoende rechten")

        with patch("opi.api.restore_router.require_restore_source_access", side_effect=refusal):
            response = self._post(client)

        assert response.status_code == 403, response.text
        mock_manager.save_and_commit_project.assert_not_awaited()


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


class TestRefreshFailureIsReported:
    """A restore that lands on the cluster but whose follow-up refresh falls over is not
    a success. The refresh is what regenerates the manifests and secrets; without it the
    deployment keeps running on the pre-restore ones. The response used to say
    ``"status": "success"`` with ``"refresh_triggered": true`` regardless -- the return
    value of process_project_from_git is a bool, and it was tested with ``is not None``,
    which is true for False as well. A caller cannot read the OPI log, so the response
    was the only place this could ever surface, and it did not.
    """

    def test_deployment_restore_reports_a_failed_refresh(
        self,
        client: TestClient,
        mock_store: Any,
        mock_manager: Any,
        mock_backup_manager: Any,
        mock_cluster_functions: Any,
    ) -> None:
        mock_manager.process_project_from_git = AsyncMock(return_value=False)

        response = client.post(
            f"/api/v1/restore/project/{PROJECT}/deployment/main",
            headers={"X-API-Key": API_KEY},
            json={
                "resource_type": "pvc",
                "component_name": "web",
                "reference_name": "data",
                "snapshot_id": "snap-1",
                "update_deployment": True,
            },
        )
        assert response.status_code == 207, response.text
        body = response.json()
        assert body["status"] == "partial"
        assert body["refresh_triggered"] is True
        assert body["refresh_succeeded"] is False
        assert "deployment" in body["message"]

    def test_deployment_restore_reports_a_successful_refresh(
        self,
        client: TestClient,
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
                "update_deployment": True,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "success"
        assert body["refresh_succeeded"] is True

    def test_no_refresh_requested_leaves_the_field_null(
        self,
        client: TestClient,
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
        assert body["refresh_triggered"] is False
        assert body["refresh_succeeded"] is None

    def test_pvc_restore_reports_a_failed_refresh(
        self,
        client: TestClient,
        mock_store: Any,
        mock_manager: Any,
        mock_backup_manager: Any,
        mock_cluster_functions: Any,
    ) -> None:
        mock_manager.process_project_from_git = AsyncMock(return_value=False)

        response = client.post(
            f"/api/v1/restore/project/{PROJECT}",
            headers={"X-API-Key": API_KEY},
            json={"deployment_name": "main", "component_name": "web", "storage_name": "data"},
        )
        assert response.status_code == 207, response.text
        body = response.json()
        assert body["status"] == "partial"
        assert body["refresh_succeeded"] is False

    def test_backup_run_restore_reports_a_failed_refresh(
        self,
        client: TestClient,
        mock_store: Any,
        mock_manager: Any,
        mock_backup_manager: Any,
        mock_cluster_functions: Any,
    ) -> None:
        mock_manager.process_project_from_git = AsyncMock(return_value=False)
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
        assert response.status_code == 207, response.text
        body = response.json()
        assert body["status"] == "partial"
        assert body["refresh_succeeded"] is False


class TestEenMislukteTerugzetActieGeeftNietsPrijs:
    """Wat er in het antwoord op een mislukt terugzetten staat.

    Twee wegen, allebei met de infrastructuur erin. De ene: de hulpfunctie voor een
    database ving alles af en gaf ``f"Database restore error: {e}"`` terug, die de
    aanroeper als ``message`` in een 500 kreeg. De andere: een terugzetpod die niet
    aankwam leverde ``f"Restore pod failed. Logs: {logs[-500:]}"`` op -- vijfhonderd
    tekens podlog, langs dezelfde weg naar buiten.

    Wat er nu uitkomt is de lezerszin met het kenmerk; het hele verhaal staat in de log.
    """

    #: De storing uit de melding, in de vorm waarin de laag eronder hem doorgaf.
    STORING = "[Errno 111] Connect call failed ('172.30.19.11', 5432)"

    LEKT = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}|\b5432\b|Errno|Restore pod failed")

    def test_de_podlog_komt_niet_in_de_500(
        self,
        client: TestClient,
        mock_store: Any,
        mock_manager: Any,
        mock_backup_manager: Any,
        mock_cluster_functions: Any,
    ) -> None:
        mock_backup_manager.restore_to_project_pvc = AsyncMock(
            return_value=RestoreResult(
                namespace=PROJECT,
                pvc_name="pvc-main-web-data-0",
                success=False,
                error=f"Restore pod failed. Logs: {self.STORING}",
            )
        )

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

        assert response.status_code == 500, response.text
        assert response.json()["message"].startswith("Het terugzetten is niet gelukt")
        assert not self.LEKT.search(response.text), "de terugzetpod staat in het antwoord"

    def test_een_uitzondering_komt_niet_in_de_500(
        self,
        client: TestClient,
        mock_store: Any,
        mock_manager: Any,
        mock_backup_manager: Any,
        mock_cluster_functions: Any,
    ) -> None:
        """De brede vangst van het eindpunt zelf: wat de keten ook opgooit."""
        mock_backup_manager.restore_to_project_pvc = AsyncMock(side_effect=OSError(self.STORING))

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

        assert response.status_code == 500, response.text
        assert not self.LEKT.search(response.text), "de uitzondering staat in het antwoord"

    async def test_de_hulpfunctie_voor_een_database_geeft_de_lezerszin_terug(self) -> None:
        """De deur die de grendel niet kende: een dict die verderop een antwoord wordt."""
        from opi.manager.backup import restore as module

        handler = MagicMock()
        handler.get_database_generation.return_value = 0

        with (
            patch("opi.core.cluster_config.get_database_server", return_value="db.intern"),
            patch("opi.connectors.postgres.create_postgres_connector", side_effect=OSError(self.STORING)),
        ):
            result = await module.restore_database_with_versioning(
                project_name=PROJECT,
                deployment_name="main",
                component_name="web",
                reference_name="data",
                snapshot_id="snap-1",
                deployment_cluster="local",
                namespace=PROJECT,
                project_data=_project_file(),
                project_file_handler=handler,
            )

        assert result["success"] is False
        assert not self.LEKT.search(result["error"]), result["error"]
