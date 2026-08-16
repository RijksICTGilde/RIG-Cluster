"""An attachment change must actually be rolled out (RC-119).

The five attachment routes wrote the project file and stopped. No task, no reprocess,
nothing: a replaced certificate was committed to ``zad-projects`` and no manifest was ever
generated from it, so the change reached the cluster only if something else happened to
process the project later. A ``rollout=true`` sent along was silently ignored, because the
routes had no such parameter at all.

What is measured here is the contract that replaces that: the write still happens
synchronously (the content is an upload), and every successful write enqueues the
processing as a task, with the same ``rollout`` meaning every other mutating endpoint has.
The task handler itself -- process, or note the deferral -- is measured at the bottom.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.core.async_task_service import TaskType
from opi.core.task_rollout import SKIPPED_REASON
from opi.services.catalog.attachments.task import handle_configure_attachment

PROJECT_URL = "/api/v2/projects/demo/services/attachments/attachment"
COMPONENT_URL = "/api/v2/projects/demo/services/attachments/component/backend/attachment"
ITEM_URL = f"{PROJECT_URL}/server-cert"
HEADERS = {"X-API-Key": "test-key"}


@pytest.fixture
def client() -> TestClient:
    from opi.api.v2.router import v2_router

    app = FastAPI()
    app.include_router(v2_router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _authorised_project():
    project = MagicMock()
    project.name = "demo"
    project.api_key = "test-key"
    store = MagicMock()
    store.get.return_value = project
    with patch("opi.api.endpoint_util.get_project_store", return_value=store):
        yield store


@pytest.fixture(autouse=True)
def manager():
    instance = MagicMock()
    instance.close = AsyncMock()
    instance.upsert_attachment = AsyncMock(
        return_value={"success": True, "attachment": "server-cert", "replaced": True, "component": None}
    )
    instance.remove_attachment = AsyncMock(return_value={"success": True, "changed": True, "uncoupled_from": []})
    with patch("opi.manager.project_manager.ProjectManager", return_value=instance):
        yield instance


@pytest.fixture(autouse=True)
def created_task():
    with patch("opi.api.v2.router.create_async_task", new=AsyncMock(return_value={"task_id": "t-1"})) as mock:
        yield mock


def _file() -> dict:
    return {"file": ("server.pem", b"cert-bytes", "application/x-pem-file")}


class TestEveryWriteIsRolledOut:
    """The bug, per route: a saved attachment that nothing ever processed."""

    def test_defining_one_enqueues_the_rollout(self, client, created_task) -> None:
        response = client.post(PROJECT_URL, headers=HEADERS, data={"attachment_id": "server-cert"}, files=_file())

        assert response.status_code == 202
        assert created_task.call_args.kwargs["task_type"] == TaskType.CONFIGURE_ATTACHMENT
        assert created_task.call_args.kwargs["project_name"] == "demo"

    def test_replacing_its_content_enqueues_the_rollout(self, client, created_task) -> None:
        assert client.put(ITEM_URL, headers=HEADERS, files=_file()).status_code == 202
        assert created_task.call_args.kwargs["task_type"] == TaskType.CONFIGURE_ATTACHMENT

    def test_coupling_one_to_a_component_enqueues_the_rollout(self, client, created_task) -> None:
        response = client.post(
            COMPONENT_URL,
            headers=HEADERS,
            data={"reference": "server-cert", "provide-as": "file", "path": "/etc/ssl/certs/server.pem"},
        )

        assert response.status_code == 202
        assert created_task.call_args.kwargs["payload"]["component"] == "backend"

    def test_deleting_one_enqueues_the_rollout(self, client, created_task) -> None:
        assert client.delete(ITEM_URL, headers=HEADERS).status_code == 202
        assert created_task.call_args.kwargs["task_type"] == TaskType.CONFIGURE_ATTACHMENT


class TestWhatTheCallerGetsBack:
    def test_the_task_can_be_followed(self, client) -> None:
        response = client.put(ITEM_URL, headers=HEADERS, files=_file())

        assert response.headers["Location"] == "/api/tasks/t-1"
        assert response.json()["task_id"] == "t-1"

    def test_the_synchronous_answer_is_not_lost(self, client) -> None:
        # The write happened in this request, so what it reported still travels along:
        # a 202 that only said "queued" would drop the answer to "did it replace one".
        body = client.put(ITEM_URL, headers=HEADERS, files=_file()).json()

        assert body["attachment"] == "server-cert"
        assert body["replaced"] is True

    def test_a_refusal_stays_synchronous(self, client, manager, created_task) -> None:
        manager.upsert_attachment.return_value = {
            "success": False,
            "error": "Bijlage 'server-cert' bestaat al",
            "error_type": "conflict",
        }

        response = client.post(PROJECT_URL, headers=HEADERS, data={"attachment_id": "server-cert"}, files=_file())

        assert response.status_code == 409
        created_task.assert_not_called()

    def test_a_rejected_upload_rolls_nothing_out(self, client, created_task) -> None:
        response = client.post(
            PROJECT_URL,
            headers=HEADERS,
            data={"attachment_id": "server-cert"},
            files={"file": ("x.pem", b"", "text/plain")},
        )

        assert response.status_code == 422
        created_task.assert_not_called()


class TestRolloutMeansWhatItMeansElsewhere:
    def test_rollout_defaults_to_true(self, client, created_task) -> None:
        client.put(ITEM_URL, headers=HEADERS, files=_file())

        assert created_task.call_args.kwargs["payload"]["rollout"] is True

    def test_rollout_false_travels_into_the_payload(self, client, created_task) -> None:
        # It used to be accepted and silently ignored, because the route had no such
        # parameter: the deferral has to be a fact the task carries.
        client.put(f"{ITEM_URL}?rollout=false", headers=HEADERS, files=_file())

        assert created_task.call_args.kwargs["payload"]["rollout"] is False

    def test_the_parameter_is_in_the_openapi_document(self, client) -> None:
        put = client.app.openapi()["paths"][
            "/api/v2/projects/{project_name}/services/attachments/attachment/{attachment_id}"
        ]["put"]

        assert "rollout" in [p["name"] for p in put["parameters"]]


class TestTheRolloutTask:
    @pytest.mark.asyncio
    async def test_it_processes_the_project(self) -> None:
        instance = MagicMock()
        instance.close = AsyncMock()
        instance.process_project_from_git = AsyncMock(return_value=True)
        with patch("opi.manager.project_manager.ProjectManager", return_value=instance):
            result = await handle_configure_attachment(
                {"project_name": "demo", "item_id": "server-cert", "rollout": True}, MagicMock()
            )

        assert result["status"] == "success"
        assert result["processing"]["status"] == "completed"
        assert instance.process_project_from_git.call_args.args[0] == "projects/demo.yaml"

    @pytest.mark.asyncio
    async def test_rollout_false_processes_nothing(self) -> None:
        instance = MagicMock()
        instance.close = AsyncMock()
        instance.process_project_from_git = AsyncMock(return_value=True)
        with patch("opi.manager.project_manager.ProjectManager", return_value=instance):
            result = await handle_configure_attachment(
                {"project_name": "demo", "item_id": "server-cert", "rollout": False}, MagicMock()
            )

        instance.process_project_from_git.assert_not_called()
        assert result["processing"]["reason"] == SKIPPED_REASON

    @pytest.mark.asyncio
    async def test_a_failed_processing_fails_the_task(self) -> None:
        instance = MagicMock()
        instance.close = AsyncMock()
        instance.process_project_from_git = AsyncMock(return_value=False)
        instance.get_processing_error = MagicMock(return_value="git kapot")
        with patch("opi.manager.project_manager.ProjectManager", return_value=instance):
            result = await handle_configure_attachment({"project_name": "demo", "rollout": True}, MagicMock())

        assert result["status"] == "failed"
        assert result["processing"]["error"] == "git kapot"
