"""The component delete endpoint, end to end through the router (RC-73).

The machinery to remove a component was all there -- the task type, the handler, its
registration -- and no route ever reached it. From outside that is invisible: the path
``/v2/projects/{project}/components/{component}` answered PATCH and nothing else, so a
client could create and change a component but never take one away. This is the verb that
was missing, on the resource that was already there.

What belongs to this layer, and is therefore what is measured here:

* the route exists on the item path and is reachable with the project's API key;
* the answers map onto the status codes a client can act on -- 202 accepted, 404 never
  there, 409 in use;
* the 409 carries ``used_by``, because "no" without "where" makes the caller go hunting;
* the confirmation is a flag the caller sets, off by default, and it reaches the task
  payload as such -- a delete that quietly removed deployment entries would be exactly the
  surprise the design refuses.

What the deletion does to the project file is exercised in
tests/test_component_delete_manager.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.api.v2.router import v2_router

ITEM_URL = "/api/v2/projects/demo/components/web"
HEADERS = {"X-API-Key": "test-key"}


def _free_project() -> dict:
    """A project where 'web' exists and nothing references it."""
    return {
        "name": "demo",
        "components": [{"name": "web"}, {"name": "worker"}],
        "deployments": [{"name": "staging", "components": [{"reference": "worker"}]}],
    }


def _project_data(data: dict) -> MagicMock:
    project = MagicMock()
    project.name = "demo"
    project.api_key = "test-key"
    project.data = data
    return project


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(v2_router)
    return TestClient(app)


@pytest.fixture
def store():
    """The project store both the auth decorator and the endpoint read."""
    instance = MagicMock()
    instance.get.return_value = _project_data(_free_project())
    with (
        patch("opi.api.endpoint_util.get_project_store", return_value=instance),
        patch("opi.api.v2.router.get_project_store", return_value=instance),
    ):
        yield instance


@pytest.fixture(autouse=True)
def created_task():
    with patch("opi.api.v2.router.create_async_task", new=AsyncMock(return_value={"task_id": "t-1"})) as mock:
        yield mock


class TestTheRouteExists:
    def test_delete_sits_next_to_the_patch_on_the_same_path(self, client, store) -> None:
        # The whole point of the shape: one resource, addressed one way, several verbs.
        assert client.delete(ITEM_URL, headers=HEADERS).status_code == 202
        assert client.patch(ITEM_URL, headers=HEADERS, json={"image": "nginx:1"}).status_code == 202

    def test_the_api_key_is_required(self, client, store, created_task) -> None:
        assert client.delete(ITEM_URL).status_code == 401
        created_task.assert_not_awaited()

    def test_a_wrong_api_key_is_refused(self, client, store, created_task) -> None:
        assert client.delete(ITEM_URL, headers={"X-API-Key": "nope"}).status_code == 401
        created_task.assert_not_awaited()

    def test_it_runs_as_a_task(self, client, store, created_task) -> None:
        response = client.delete(ITEM_URL, headers=HEADERS)

        assert response.json()["task_id"] == "t-1"
        assert created_task.await_args.kwargs["task_type"] == "delete_component"
        assert created_task.await_args.kwargs["payload"]["component_name"] == "web"


class TestWhatIsNotThere:
    def test_an_unknown_project_never_reaches_the_endpoint(self, client, store, created_task) -> None:
        """There is no key to check the request against, so it is refused as unauthorised
        before anything reads the project -- the same answer every v2 endpoint gives."""
        store.get.return_value = None

        assert client.delete(ITEM_URL, headers=HEADERS).status_code == 401
        created_task.assert_not_awaited()

    def test_an_unknown_component_is_a_404(self, client, store, created_task) -> None:
        """Not a task that starts and fails: the caller can act on this one right now, and
        a silent success would tell them their name was right (CLI finding 6)."""
        response = client.delete("/api/v2/projects/demo/components/nope", headers=HEADERS)

        assert response.status_code == 404
        assert "nope" in response.json()["detail"]
        created_task.assert_not_awaited()


class TestAComponentInUse:
    def test_it_is_refused_with_409(self, client, store, created_task) -> None:
        project = _free_project()
        project["deployments"][0]["components"].append({"reference": "web"})
        store.get.return_value = _project_data(project)

        response = client.delete(ITEM_URL, headers=HEADERS)

        assert response.status_code == 409
        created_task.assert_not_awaited()

    def test_the_refusal_names_every_place(self, client, store) -> None:
        project = {
            "name": "demo",
            "components": [{"name": "web"}, {"name": "worker", "uses-components": ["web"]}],
            "deployments": [{"name": "staging", "components": [{"reference": "web"}]}],
        }
        store.get.return_value = _project_data(project)

        detail = client.delete(ITEM_URL, headers=HEADERS).json()["detail"]

        assert [u["label"] for u in detail["used_by"]] == ["component 'worker'", "deployment 'staging'"]
        assert "confirm_in_use" in detail["detail"]

    def test_the_confirmation_lets_it_through(self, client, store, created_task) -> None:
        project = {
            "name": "demo",
            "components": [{"name": "web"}],
            "deployments": [{"name": "staging", "components": [{"reference": "web"}]}],
        }
        store.get.return_value = _project_data(project)

        response = client.delete(f"{ITEM_URL}?confirm_in_use=true", headers=HEADERS)

        assert response.status_code == 202
        assert created_task.await_args.kwargs["payload"]["confirm_in_use"] is True

    def test_without_the_flag_the_payload_says_so(self, client, store, created_task) -> None:
        """It travels as what the caller stated, so the write layer decides on the same
        fact rather than on a default it invented."""
        client.delete(ITEM_URL, headers=HEADERS)

        assert created_task.await_args.kwargs["payload"]["confirm_in_use"] is False


class TestAComponentAWebAddressIsBuiltAround:
    def test_it_is_refused_even_with_the_confirmation(self, client, store, created_task) -> None:
        project = {
            "name": "demo",
            "components": [{"name": "web"}],
            "deployments": [
                {
                    "name": "staging",
                    "components": [{"reference": "web"}],
                    "services": [{"reference": "publish-on-web", "config": {"root-component": "web"}}],
                }
            ],
        }
        store.get.return_value = _project_data(project)

        response = client.delete(f"{ITEM_URL}?confirm_in_use=true", headers=HEADERS)

        assert response.status_code == 409
        assert "webadres" in response.json()["detail"]["detail"]
        created_task.assert_not_awaited()


class TestTheRolloutFlag:
    def test_rollout_false_is_refused_with_a_reason(self, client, store, created_task) -> None:
        """The reprocess runs inside the task, so deferring it is not something this
        operation can honour -- and saying so beats silently rolling out anyway."""
        response = client.delete(f"{ITEM_URL}?rollout=false", headers=HEADERS)

        assert response.status_code == 422
        assert "rollout=false is not supported" in response.json()["detail"]
        created_task.assert_not_awaited()
