"""One word, one meaning: the sleep-mode status contract (RC-119).

Three things were wrong and are pinned here.

* Neither endpoint had a response model, so ``/openapi.json`` held no schema, no values
  and no explanation. A generated client saw an object.
* ``state`` meant two different things on two neighbouring endpoints: the waker's poll
  contract (``starting | ready``) on ``/status``, the real sleep state
  (``awake | sleeping | waking``) on ``/wake``.
* With sleep-mode switched off, ``/status`` returned a hardcoded ``starting`` without
  looking at a pod or at the stored state -- which is why the CLI saw ``starting``, always.

The fix is additive on purpose: ``state`` on ``/status`` is byte-identical to what it was,
because the waker image is pulled from a registry and can be older than this code. The
answer to the question a client is actually asking moved into ``sleep_state``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.api.openapi_choices import CHOICES_KEY
from opi.services.catalog.sleep_mode import flow
from opi.services.catalog.sleep_mode.router import sleep_mode_router

PROJECT = "demo"
DEPLOYMENT = "PR-1"
TOKEN_HEADER = {"X-Wake-Token": "presented-token"}
STATUS_URL = f"/api/sleep-mode/{PROJECT}/{DEPLOYMENT}/status"
WAKE_URL = f"/api/sleep-mode/{PROJECT}/{DEPLOYMENT}/wake"


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(sleep_mode_router)
    return TestClient(app)


def _project(deployment: dict, services: list | None = None) -> MagicMock:
    project = MagicMock()
    project.data = {
        "name": PROJECT,
        "deployments": [deployment],
        "services": services if services is not None else [],
    }
    return project


def _store(project: MagicMock) -> MagicMock:
    store = MagicMock()
    store.get.return_value = project
    return store


# --- the flow: what each situation reports -------------------------------------------


class TestSleepModeOff:
    """The case the CLI hit: a project that simply does not use sleep-mode."""

    @pytest.mark.asyncio
    async def test_reports_disabled_and_keeps_the_old_word(self) -> None:
        project = _project({"name": DEPLOYMENT, "cluster": "odcn-production", "namespace": "demo"})
        with patch("opi.services.project_store.get_project_store", return_value=_store(project)):
            result = await flow.status(PROJECT, DEPLOYMENT)

        assert result["sleep_state"] == "disabled"
        # Frozen: a waker image older than this code reads exactly this field.
        assert result["state"] == "starting"

    @pytest.mark.asyncio
    async def test_asks_no_pod_about_it(self) -> None:
        # There is nothing to be ready, so querying the cluster for it is a wasted call on
        # a route that is polled every few seconds.
        project = _project({"name": DEPLOYMENT, "cluster": "odcn-production", "namespace": "demo"})
        kubectl = MagicMock()
        kubectl.return_value.get_deployment_status = AsyncMock(return_value=[])
        with (
            patch("opi.services.project_store.get_project_store", return_value=_store(project)),
            patch("opi.connectors.kubectl.KubectlConnector", kubectl),
        ):
            await flow.status(PROJECT, DEPLOYMENT)

        kubectl.assert_not_called()


class TestSleepModeOn:
    """With sleep-mode configured, the two fields answer two different questions."""

    def _with_sleep_mode(self, stored_state: str) -> MagicMock:
        deployment = {
            "name": DEPLOYMENT,
            "cluster": "odcn-production",
            "namespace": "demo",
            "sleep": {"state": stored_state},
        }
        return _project(deployment)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stored", ["awake", "sleeping", "waking"])
    async def test_reports_the_stored_state(self, stored: str) -> None:
        project = self._with_sleep_mode(stored)
        config = MagicMock()
        config.matches.return_value = True
        kubectl = MagicMock()
        kubectl.return_value.get_deployment_status = AsyncMock(return_value=[{"ready": "1/1"}])
        with (
            patch("opi.services.project_store.get_project_store", return_value=_store(project)),
            patch("opi.services.catalog.sleep_mode.config.load", return_value=config),
            patch("opi.services.catalog.sleep_mode.manifests.select_waker_component", return_value="web"),
            patch("opi.connectors.kubectl.KubectlConnector", kubectl),
        ):
            result = await flow.status(PROJECT, DEPLOYMENT)

        assert result["sleep_state"] == stored
        assert result["state"] == "ready"

    @pytest.mark.asyncio
    async def test_a_pod_that_is_not_up_yet_is_still_starting(self) -> None:
        project = self._with_sleep_mode("waking")
        config = MagicMock()
        config.matches.return_value = True
        kubectl = MagicMock()
        kubectl.return_value.get_deployment_status = AsyncMock(return_value=[{"ready": "0/1"}])
        with (
            patch("opi.services.project_store.get_project_store", return_value=_store(project)),
            patch("opi.services.catalog.sleep_mode.config.load", return_value=config),
            patch("opi.services.catalog.sleep_mode.manifests.select_waker_component", return_value="web"),
            patch("opi.connectors.kubectl.KubectlConnector", kubectl),
        ):
            result = await flow.status(PROJECT, DEPLOYMENT)

        assert result == {"state": "starting", "sleep_state": "waking"}

    @pytest.mark.asyncio
    async def test_a_deployment_the_config_does_not_match_is_disabled(self) -> None:
        # Sleep-mode configured for the project but not for THIS deployment: it never
        # sleeps, so it has no sleep state of its own.
        project = self._with_sleep_mode("awake")
        config = MagicMock()
        config.matches.return_value = False
        with (
            patch("opi.services.project_store.get_project_store", return_value=_store(project)),
            patch("opi.services.catalog.sleep_mode.config.load", return_value=config),
        ):
            result = await flow.status(PROJECT, DEPLOYMENT)

        assert result["sleep_state"] == "disabled"


class TestWakeUsesTheSameWord:
    """``/wake`` has to say ``disabled`` where ``/status`` says it, or the word splits again."""

    @pytest.mark.asyncio
    async def test_a_deployment_the_config_does_not_match_is_disabled(self) -> None:
        # It reported the stored state (``awake``) here, while /status called the very
        # same situation ``disabled`` -- one word, two meanings, on the pair of endpoints
        # this field exists to keep aligned.
        deployment = {"name": DEPLOYMENT, "cluster": "odcn-production", "sleep": {"state": "awake"}}
        project = _project(deployment)
        manager = AsyncMock()
        manager.get_contents = AsyncMock(return_value=project.data)
        config = MagicMock()
        config.matches.return_value = False
        with (
            patch("opi.services.project_store.get_project_store", return_value=_store(project)),
            patch("opi.manager.project_manager.ProjectManager", lambda **kwargs: manager),
            patch("opi.services.catalog.sleep_mode.config.load", return_value=config),
        ):
            result = await flow.wake(PROJECT, DEPLOYMENT)

        assert result.changed is False
        assert result.state == "disabled"
        # A no-op writes nothing, exactly as before.
        manager.save_and_commit_project.assert_not_awaited()


# --- the endpoints and the document --------------------------------------------------


class TestWhatTheEndpointsAnswer:
    def test_status_carries_both_fields(self, client: TestClient) -> None:
        body = {"state": "ready", "sleep_state": "awake"}
        with patch.object(flow, "status", AsyncMock(return_value=body)):
            response = client.get(STATUS_URL, headers=TOKEN_HEADER)

        assert response.json() == body

    def test_wake_says_the_same_word_under_both_names(self, client: TestClient) -> None:
        with patch.object(flow, "wake", AsyncMock(return_value=flow.WakeResult(changed=True, state="waking"))):
            body = client.post(WAKE_URL, headers=TOKEN_HEADER).json()

        assert body["state"] == body["sleep_state"] == "waking"


class TestTheOpenApiDocument:
    """A client must be able to read the values off the spec instead of probing."""

    @pytest.fixture
    def schemas(self, client: TestClient) -> dict:
        return client.app.openapi()["components"]["schemas"]

    def _field(self, schemas: dict, model: str, name: str) -> dict:
        prop = schemas[model]["properties"][name]
        # A Literal reaches the document either inline or behind an allOf/$ref.
        if "allOf" in prop:
            ref = prop["allOf"][0]["$ref"].rsplit("/", 1)[-1]
            return {**schemas[ref], **prop}
        if "$ref" in prop:
            ref = prop["$ref"].rsplit("/", 1)[-1]
            return {**schemas[ref], **prop}
        return prop

    def test_both_endpoints_declare_a_schema(self, client: TestClient) -> None:
        paths = client.app.openapi()["paths"]
        for path, method in ((STATUS_URL, "get"), (WAKE_URL, "post")):
            template = path.replace(PROJECT, "{project_name}").replace(DEPLOYMENT, "{deployment_name}")
            content = paths[template][method]["responses"]["200"]["content"]["application/json"]
            assert "$ref" in content["schema"]

    def test_the_waker_field_lists_its_two_values(self, schemas: dict) -> None:
        assert self._field(schemas, "SleepStatusResponse", "state")["enum"] == ["starting", "ready"]

    def test_the_sleep_state_field_lists_all_four(self, schemas: dict) -> None:
        field = self._field(schemas, "SleepStatusResponse", "sleep_state")
        assert field["enum"] == ["awake", "sleeping", "waking", "disabled"]

    def test_every_value_carries_a_description(self, schemas: dict) -> None:
        for model, name in (
            ("SleepStatusResponse", "state"),
            ("SleepStatusResponse", "sleep_state"),
            ("WakeResponse", "sleep_state"),
        ):
            field = self._field(schemas, model, name)
            choices = field[CHOICES_KEY]
            assert [c["const"] for c in choices] == field["enum"]
            assert all(c["description"] for c in choices)
