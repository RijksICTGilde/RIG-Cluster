"""The backup-create route of the v1 API, measured through the router.

The orchestration behind ``POST /api/v1/backup/project/{p}/deployment/{d}`` had no test of
its own: the e2e suite used the route as a tool, and the unit tests only checked which
action it carries. Since RC-475 the route queues a backup task and waits for it (decision
B1), so what is measured here is the translation: which target it refuses before queueing,
what it puts in the payload, and how it maps a finished task onto 200, 207, 409 or 500.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.api.backup_router import backup_router
from opi.manager.backup import BackupResult, BucketBackupResult, DatabaseBackupResult

URL = "/api/v1/backup/project/demo/deployment/prod"
HEADERS = {"X-API-Key": "test-key"}

_BR = "opi.api.backup_router"
_TH = "opi.http.task_helpers"


def _project_data() -> dict[str, Any]:
    return {
        "name": "demo",
        "deployments": [{"name": "prod", "cluster": "local", "namespace": "demo"}],
    }


def _project(data: dict[str, Any] | None = None) -> MagicMock:
    project = MagicMock()
    project.name = "demo"
    project.api_key = "test-key"
    project.data = _project_data() if data is None else data
    return project


def _pvc_result(*, success: bool) -> BackupResult:
    return BackupResult(
        namespace="rig-demo",
        pvc_name="prod-web-data-v1",
        success=success,
        snapshot_name="snap-1" if success else None,
        error=None if success else "pod mislukt",
        duration_seconds=1.0,
    )


def _database_result(*, success: bool) -> DatabaseBackupResult:
    return DatabaseBackupResult(
        namespace="rig-demo",
        pvc_name="web-db",
        success=success,
        error=None if success else "pg_dump mislukt",
        database_name="demo-prod",
        reference_name="web-db",
        source_type="shared",
    )


def _bucket_result(*, success: bool) -> BucketBackupResult:
    return BucketBackupResult(
        namespace="rig-demo",
        pvc_name="web-bucket",
        success=success,
        error=None if success else "mirror mislukt",
        bucket_name="demo-prod",
        reference_name="web-bucket",
        source_type="shared",
    )


def _task_result(
    *,
    pvc: list[BackupResult] | None = None,
    database: list[DatabaseBackupResult] | None = None,
    bucket: list[BucketBackupResult] | None = None,
    failures: list[str] | None = None,
    skipped: list[dict[str, str]] | None = None,
    lock_error: str | None = None,
) -> dict[str, Any]:
    """A task answer in the shape ``backup_run_answer`` produces."""
    return {
        "success": not failures and all(r.success for r in [*(pvc or []), *(database or []), *(bucket or [])]),
        "total_results": len(pvc or []) + len(database or []) + len(bucket or []),
        "backup_run_id": "20261110120000",
        "pvc_results": [asdict(r) for r in pvc or []],
        "database_results": [asdict(r) for r in database or []],
        "bucket_results": [asdict(r) for r in bucket or []],
        "failures": failures or [],
        "skipped": skipped or [],
        "lock_error": lock_error,
    }


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(backup_router)
    return TestClient(app)


@pytest.fixture
def store():
    instance = MagicMock()
    instance.get.return_value = _project()
    with (
        patch("opi.http.auth.endpoint_util.get_project_store", return_value=instance),
        patch("opi.manager.backup.run.get_project_store", return_value=instance),
    ):
        yield instance


@pytest.fixture
def backup(store):
    """The resolve the route runs, plus the task it queues and waits for."""
    with (
        patch("opi.manager.backup.run.create_project_file_handler") as handler,
        patch(f"{_BR}.create_async_task", new_callable=AsyncMock) as created,
        patch(f"{_TH}.wait_for_task_completion", new_callable=AsyncMock) as finished,
    ):
        handler.return_value.extract_deployment_namespace.return_value = "demo"
        handler.return_value.extract_deployment_cluster.return_value = "local"
        created.return_value = {"task_id": "task-1"}
        finished.return_value = {
            "task_id": "task-1",
            "status": "completed",
            "result": _task_result(
                pvc=[_pvc_result(success=True)],
                database=[_database_result(success=True)],
                bucket=[_bucket_result(success=True)],
            ),
        }
        yield {"handler": handler.return_value, "created": created, "finished": finished}


class TestWhatTheRouteQueues:
    def test_the_answer_is_the_outcome_of_the_task_it_waited_for(self, client, backup) -> None:
        response = client.post(URL, headers=HEADERS, json={})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "success"
        assert len(body["pvc_results"]) == 1
        assert len(body["database_results"]) == 1
        assert len(body["bucket_results"]) == 1
        backup["finished"].assert_awaited_once()
        assert backup["finished"].await_args.kwargs["task_id"] == "task-1"

    def test_the_payload_carries_the_resource_types_and_the_manual_trigger(self, client, backup) -> None:
        client.post(URL, headers=HEADERS, json={"resource_types": ["pvc"]})

        payload = backup["created"].await_args.kwargs["payload"]
        assert payload == {
            "project_name": "demo",
            "deployment_name": "prod",
            "resource_types": ["pvc"],
            "trigger": "manual",
        }

    def test_without_a_body_every_resource_type_is_queued(self, client, backup) -> None:
        client.post(URL, headers=HEADERS, json={})

        assert backup["created"].await_args.kwargs["payload"]["resource_types"] == ["pvc", "database", "minio"]

    def test_the_task_is_a_backup_task_for_this_deployment(self, client, backup) -> None:
        client.post(URL, headers=HEADERS, json={})

        kwargs = backup["created"].await_args.kwargs
        assert kwargs["task_type"] == "backup"
        assert kwargs["project_name"] == "demo"
        assert kwargs["deployment_name"] == "prod"


class TestHowTheOutcomeMapsOntoStatusCodes:
    def test_a_failing_pvc_next_to_two_successes_is_207(self, client, backup) -> None:
        backup["finished"].return_value["result"] = _task_result(
            pvc=[_pvc_result(success=False)],
            database=[_database_result(success=True)],
            bucket=[_bucket_result(success=True)],
        )

        response = client.post(URL, headers=HEADERS, json={})

        assert response.status_code == 207, response.text
        assert response.json()["status"] == "partial"
        assert response.json()["message"].startswith("Backed up 2/3")

    def test_every_step_failing_is_500(self, client, backup) -> None:
        backup["finished"].return_value["result"] = _task_result(
            pvc=[_pvc_result(success=False)],
            database=[_database_result(success=False)],
            bucket=[_bucket_result(success=False)],
        )

        response = client.post(URL, headers=HEADERS, json={})

        assert response.status_code == 500, response.text
        assert response.json()["status"] == "failed"

    def test_nothing_to_back_up_is_200(self, client, backup) -> None:
        backup["finished"].return_value["result"] = _task_result()

        response = client.post(URL, headers=HEADERS, json={})

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "success"

    def test_a_held_lock_with_nothing_backed_up_is_409(self, client, backup) -> None:
        lock = "Could not acquire backup lock - another backup is running"
        backup["finished"].return_value["result"] = _task_result(failures=[lock], lock_error=lock)

        response = client.post(URL, headers=HEADERS, json={})

        assert response.status_code == 409, response.text
        assert response.json()["detail"] == lock

    def test_a_task_that_never_ran_its_steps_is_500(self, client, backup) -> None:
        backup["finished"].return_value = {
            "task_id": "task-1",
            "status": "failed",
            "result": {"success": False, "error": "Project 'demo' niet gevonden"},
        }

        with (
            patch(f"{_TH}.TASK_ANSWER_GRACE_SECONDS", 0.0),
            patch(f"{_TH}.get_task_service", return_value=MagicMock()),
        ):
            response = client.post(URL, headers=HEADERS, json={})

        assert response.status_code == 500, response.text

    def test_an_answer_that_lands_after_the_status_is_still_read(self, client, backup) -> None:
        """The terminal status is written before the worker adds its answer (RC-475).

        The progress manager stamps the task done without a result, so a route that reads the
        result straight away sees nothing and used to call a successful backup a failure.
        """
        done = _task_result(pvc=[_pvc_result(success=True)])
        backup["finished"].return_value = {"task_id": "task-1", "status": "completed", "result": None}
        task_service = MagicMock()
        task_service.get_task = AsyncMock(return_value={"task_id": "task-1", "status": "completed", "result": done})

        with patch(f"{_TH}.get_task_service", return_value=task_service):
            response = client.post(URL, headers=HEADERS, json={})

        assert response.status_code == 200, response.text
        assert len(response.json()["pvc_results"]) == 1

    def test_waiting_longer_than_the_call_can_is_504(self, client, backup) -> None:
        backup["finished"].side_effect = TimeoutError("task did not complete")

        response = client.post(URL, headers=HEADERS, json={})

        assert response.status_code == 504, response.text


class TestTheTargetItRefusesBeforeQueueing:
    def test_a_project_without_data_is_404(self, client, store, backup) -> None:
        store.get.return_value = _project(data={})

        response = client.post(URL, headers=HEADERS, json={})

        assert response.status_code == 404, response.text
        backup["created"].assert_not_awaited()

    def test_an_unknown_deployment_is_404(self, client, backup) -> None:
        backup["handler"].extract_deployment_namespace.return_value = None

        response = client.post(URL, headers=HEADERS, json={})

        assert response.status_code == 404, response.text
        backup["created"].assert_not_awaited()

    def test_a_deployment_without_a_cluster_is_400(self, client, backup) -> None:
        backup["handler"].extract_deployment_cluster.return_value = None

        response = client.post(URL, headers=HEADERS, json={})

        assert response.status_code == 400, response.text
        assert "has no cluster configured" in response.json()["detail"]

    def test_a_deployment_on_another_cluster_is_400(self, client, backup) -> None:
        backup["handler"].extract_deployment_cluster.return_value = "odcn-production"

        response = client.post(URL, headers=HEADERS, json={})

        assert response.status_code == 400, response.text
        assert "Cannot backup cross-cluster" in response.json()["detail"]

    def test_without_a_key_the_route_answers_401(self, client, store) -> None:
        assert client.post(URL, json={}).status_code == 401


class TestWhatUsedToBeSwallowed:
    """Decision B2: a broken database or object-storage step is a failure, not a warning.

    The route answered 200 "success" for it, because it caught the exception, logged a
    warning and reported on the results that were left.
    """

    def test_a_broken_database_step_next_to_a_good_pvc_is_207(self, client, backup) -> None:
        backup["finished"].return_value["result"] = _task_result(
            pvc=[_pvc_result(success=True)],
            failures=["Connection refused"],
        )

        response = client.post(URL, headers=HEADERS, json={})

        assert response.status_code == 207, response.text
        assert response.json()["status"] == "partial"
        assert response.json()["message"].startswith("Backed up 1/2")

    def test_a_broken_step_on_its_own_is_500(self, client, backup) -> None:
        backup["finished"].return_value["result"] = _task_result(failures=["Connection refused"])

        response = client.post(URL, headers=HEADERS, json={})

        assert response.status_code == 500, response.text
        assert response.json()["status"] == "failed"


class TestASkippedResourceReachesTheAnswer:
    """Decision B3: a resource without a secret is skipped with a reason, not passed over."""

    def test_the_reason_is_in_the_body_next_to_a_successful_run(self, client, backup) -> None:
        backup["finished"].return_value["result"] = _task_result(
            pvc=[_pvc_result(success=True)],
            skipped=[{"resource_type": "database", "reason": "Overgeslagen: er is geen secret"}],
        )

        response = client.post(URL, headers=HEADERS, json={})

        assert response.status_code == 200, response.text
        assert response.json()["skipped"] == [
            {"resource_type": "database", "reason": "Overgeslagen: er is geen secret"}
        ]

    def test_a_run_without_skips_answers_an_empty_list(self, client, backup) -> None:
        response = client.post(URL, headers=HEADERS, json={})

        assert response.json()["skipped"] == []


class TestAnUnknownResourceType:
    """Decision B6: a resource type that does not exist is refused, not dropped."""

    def test_an_unknown_type_is_422_and_queues_nothing(self, client, backup) -> None:
        response = client.post(URL, headers=HEADERS, json={"resource_types": ["geen-type"]})

        assert response.status_code == 422, response.text
        backup["created"].assert_not_awaited()

    def test_an_unknown_type_next_to_a_known_one_is_refused_too(self, client, backup) -> None:
        response = client.post(URL, headers=HEADERS, json={"resource_types": ["pvc", "geen-type"]})

        assert response.status_code == 422, response.text
        backup["created"].assert_not_awaited()

    def test_the_three_known_types_pass(self, client, backup) -> None:
        for resource_type in ["pvc", "database", "minio"]:
            response = client.post(URL, headers=HEADERS, json={"resource_types": [resource_type]})
            assert response.status_code == 200, response.text
