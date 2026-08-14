"""Auth-path tests for the three new sleep-mode wake endpoints.

Two live on the API router (``X-Wake-Token`` for the waker pod, ``X-API-Key`` for the
project owner):
- ``POST /api/sleep-mode/{project}/{deployment}/wake``
- ``GET  /api/sleep-mode/{project}/{deployment}/status``

The third lives on the web router (UI button, session + admin/owner role auth):
- ``POST /projects/{project}/deployments/{deployment}/wake``

These tests pin the endpoint boundary only: the header/role gate, the exception ->
status-code mapping, and the no-op-200-vs-transition-202 choice. The transition logic
itself is covered in ``test_sleep_mode_service.py``; here ``flow.wake``/``flow.status``
are replaced so each endpoint's contract can be exercised in isolation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from opi.services.catalog.sleep_mode import flow
from opi.services.catalog.sleep_mode.flow import DeploymentNotFound, InvalidWakeToken, WakeResult
from opi.services.catalog.sleep_mode.router import sleep_mode_router
from opi.web.router import sleep_deployment_web, wake_deployment_web

PROJECT = "demo"
DEPLOYMENT = "PR-1"
TOKEN_HEADER = {"X-Wake-Token": "presented-token"}


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(sleep_mode_router)
    return TestClient(app, raise_server_exceptions=True)


# --- API router: X-Wake-Token gate ---------------------------------------------------


def test_wake_missing_token_returns_401(client: TestClient) -> None:
    resp = client.post(f"/api/sleep-mode/{PROJECT}/{DEPLOYMENT}/wake")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "X-Wake-Token or X-API-Key header required"


def test_status_missing_token_returns_401(client: TestClient) -> None:
    resp = client.get(f"/api/sleep-mode/{PROJECT}/{DEPLOYMENT}/status")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "X-Wake-Token or X-API-Key header required"


def test_wake_wrong_deployment_token_returns_401(client: TestClient) -> None:
    # A token minted for another deployment does not match this deployment's stored
    # token, so flow raises InvalidWakeToken; the router must map that to 401.
    with patch.object(flow, "wake", AsyncMock(side_effect=InvalidWakeToken("Wake token mismatch"))):
        resp = client.post(f"/api/sleep-mode/{PROJECT}/{DEPLOYMENT}/wake", headers=TOKEN_HEADER)
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid wake token"


def test_status_wrong_deployment_token_returns_401(client: TestClient) -> None:
    with patch.object(flow, "status", AsyncMock(side_effect=InvalidWakeToken("Wake token mismatch"))):
        resp = client.get(f"/api/sleep-mode/{PROJECT}/{DEPLOYMENT}/status", headers=TOKEN_HEADER)
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid wake token"


# --- API router: de projectsleutel mag hier ook ---------------------------------------
#
# De melding (zad-cli, punt 4): deze twee endpoints wilden een wake-token dat geen API en
# geen pagina uitdeelt, en de header stond nergens in de spec. Een projecteigenaar kon zijn
# eigen deployment dus niet wekken. De sleutel van een ANDER project blijft geweigerd.


def _store_with_api_key(api_key: str) -> MagicMock:
    project = MagicMock()
    project.name = PROJECT
    project.api_key = api_key
    store = MagicMock()
    store.get.return_value = project
    return store


def test_wake_met_de_projectsleutel_wekt(client: TestClient) -> None:
    result = WakeResult(changed=True, state="waking")
    with (
        patch("opi.services.catalog.sleep_mode.router.get_project_store", return_value=_store_with_api_key("geheim")),
        patch.object(flow, "wake", AsyncMock(return_value=result)) as wake_mock,
    ):
        resp = client.post(f"/api/sleep-mode/{PROJECT}/{DEPLOYMENT}/wake", headers={"X-API-Key": "geheim"})

    assert resp.status_code == 202
    # Geen wake-token om te controleren: de sleutel is hier al nagekeken, net als bij de
    # sessie-geauthenticeerde webroute.
    assert wake_mock.await_args.kwargs["presented_token"] is None


def test_status_met_de_projectsleutel_antwoordt(client: TestClient) -> None:
    with (
        patch("opi.services.catalog.sleep_mode.router.get_project_store", return_value=_store_with_api_key("geheim")),
        patch.object(flow, "status", AsyncMock(return_value={"state": "ready"})),
    ):
        resp = client.get(f"/api/sleep-mode/{PROJECT}/{DEPLOYMENT}/status", headers={"X-API-Key": "geheim"})

    assert resp.status_code == 200
    assert resp.json() == {"state": "ready"}


@pytest.mark.parametrize("verb", ["wake", "status"])
def test_de_sleutel_van_een_ander_project_wordt_geweigerd(client: TestClient, verb: str) -> None:
    """De sleutel hoort bij het project in de URL, of hij hoort hier niet."""
    with (
        patch(
            "opi.services.catalog.sleep_mode.router.get_project_store",
            return_value=_store_with_api_key("sleutel-van-demo"),
        ),
        patch.object(flow, "wake", AsyncMock()) as wake_mock,
        patch.object(flow, "status", AsyncMock()) as status_mock,
    ):
        headers = {"X-API-Key": "sleutel-van-een-ander-project"}
        if verb == "wake":
            resp = client.post(f"/api/sleep-mode/{PROJECT}/{DEPLOYMENT}/wake", headers=headers)
        else:
            resp = client.get(f"/api/sleep-mode/{PROJECT}/{DEPLOYMENT}/status", headers=headers)

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid API key"
    wake_mock.assert_not_called()
    status_mock.assert_not_called()


def test_een_onbekend_project_geeft_dezelfde_401(client: TestClient) -> None:
    """Niet 404: dan zou een sleutel zonder rechten kunnen aftasten welke projecten bestaan."""
    store = MagicMock()
    store.get.return_value = None
    with (
        patch("opi.services.catalog.sleep_mode.router.get_project_store", return_value=store),
        patch.object(flow, "wake", AsyncMock()) as wake_mock,
    ):
        resp = client.post(f"/api/sleep-mode/{PROJECT}/{DEPLOYMENT}/wake", headers={"X-API-Key": "geheim"})

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid API key"
    wake_mock.assert_not_called()


@pytest.mark.parametrize(
    ("path", "verb"),
    [
        ("/api/sleep-mode/{project_name}/{deployment_name}/wake", "post"),
        ("/api/sleep-mode/{project_name}/{deployment_name}/status", "get"),
    ],
)
def test_de_spec_noemt_beide_manieren(path: str, verb: str) -> None:
    """Een gegenereerde client moet kunnen lezen wat hij mag sturen.

    ``X-Wake-Token`` stond nergens in het document -- zoeken op "wake token" leverde niets
    op -- en de projectsleutel stond er als securityschema terwijl hij geweigerd werd.
    """
    from opi.server import app

    operation = app.openapi()["paths"][path][verb]

    headers = {p["name"] for p in operation.get("parameters", []) if p["in"] == "header"}
    assert headers == {"X-Wake-Token", "X-API-Key"}
    assert operation["security"] == [{"APIKeyHeader": []}]


# --- API router: unknown project / deployment ----------------------------------------


def test_wake_unknown_deployment_returns_404(client: TestClient) -> None:
    with patch.object(flow, "wake", AsyncMock(side_effect=DeploymentNotFound("Deployment 'PR-1' not found"))):
        resp = client.post(f"/api/sleep-mode/{PROJECT}/{DEPLOYMENT}/wake", headers=TOKEN_HEADER)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Deployment 'PR-1' not found"


def test_status_unknown_project_returns_404(client: TestClient) -> None:
    with patch.object(flow, "status", AsyncMock(side_effect=DeploymentNotFound("Project 'demo' not found"))):
        resp = client.get(f"/api/sleep-mode/{PROJECT}/{DEPLOYMENT}/status", headers=TOKEN_HEADER)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Project 'demo' not found"


# --- API router: no-op 200 vs transition 202 -----------------------------------------


def test_wake_transition_returns_202(client: TestClient) -> None:
    result = WakeResult(changed=True, state="waking")
    with patch.object(flow, "wake", AsyncMock(return_value=result)):
        resp = client.post(f"/api/sleep-mode/{PROJECT}/{DEPLOYMENT}/wake", headers=TOKEN_HEADER)
    assert resp.status_code == 202
    assert resp.json() == {"state": "waking"}


def test_wake_noop_returns_200(client: TestClient) -> None:
    result = WakeResult(changed=False, state="awake")
    with patch.object(flow, "wake", AsyncMock(return_value=result)):
        resp = client.post(f"/api/sleep-mode/{PROJECT}/{DEPLOYMENT}/wake", headers=TOKEN_HEADER)
    assert resp.status_code == 200
    assert resp.json() == {"state": "awake"}


def test_status_returns_flow_body(client: TestClient) -> None:
    with patch.object(flow, "status", AsyncMock(return_value={"state": "ready"})):
        resp = client.get(f"/api/sleep-mode/{PROJECT}/{DEPLOYMENT}/status", headers=TOKEN_HEADER)
    assert resp.status_code == 200
    assert resp.json() == {"state": "ready"}


# --- Web router: admin/owner role gate -----------------------------------------------


def _request() -> object:
    """A stand-in request; the web handler only reads the user via a patched helper."""
    return object()


@pytest.mark.parametrize("role", ["member", "developer", "viewer"])
async def test_web_wake_non_privileged_role_forbidden(role: str) -> None:
    with (
        patch("opi.web.router.get_current_user", return_value={"email": "user@example.com"}),
        patch("opi.web.router.is_user_authorized_for_project", return_value=True),
        patch("opi.web.router.get_user_role_for_project", return_value=role),
        patch.object(flow, "wake", AsyncMock()) as wake_mock,
        pytest.raises(HTTPException) as exc,
    ):
        await wake_deployment_web(_request(), PROJECT, DEPLOYMENT)
    assert exc.value.status_code == 403
    wake_mock.assert_not_called()


async def test_web_wake_unauthorized_project_forbidden() -> None:
    with (
        patch("opi.web.router.get_current_user", return_value={"email": "stranger@example.com"}),
        patch("opi.web.router.is_user_authorized_for_project", return_value=False),
        patch.object(flow, "wake", AsyncMock()) as wake_mock,
        pytest.raises(HTTPException) as exc,
    ):
        await wake_deployment_web(_request(), PROJECT, DEPLOYMENT)
    assert exc.value.status_code == 403
    wake_mock.assert_not_called()


@pytest.mark.parametrize("role", ["admin", "owner"])
async def test_web_wake_privileged_role_starts_a_task(role: str) -> None:
    """Not an inline call any more: waking commits to git and then reprocesses,
    ArgoCD sync included, so it runs as a task the page can follow."""
    with (
        patch("opi.web.router.get_current_user", return_value={"email": "boss@example.com"}),
        patch("opi.web.router.is_user_authorized_for_project", return_value=True),
        patch("opi.web.router.get_user_role_for_project", return_value=role),
        patch("opi.web.router.get_project_store", return_value=_store_with_deployment()),
        patch("opi.web.router.create_task_and_render_progress", AsyncMock()) as start_task,
        patch.object(flow, "wake", AsyncMock()) as inline,
    ):
        await wake_deployment_web(_request(), PROJECT, DEPLOYMENT)

    inline.assert_not_called()
    payload = start_task.await_args.kwargs
    assert payload["task_type"] == "wake_deployment"
    assert payload["payload"]["direction"] == "wake"


async def test_web_wake_unknown_deployment_404() -> None:
    """An unknown deployment is a 404 at the click, not a task that fails later."""
    with (
        patch("opi.web.router.get_current_user", return_value={"email": "boss@example.com"}),
        patch("opi.web.router.is_user_authorized_for_project", return_value=True),
        patch("opi.web.router.get_user_role_for_project", return_value="admin"),
        patch("opi.web.router.get_project_store", return_value=_store_with_deployment("iets-anders")),
        pytest.raises(HTTPException) as exc,
    ):
        await wake_deployment_web(_request(), PROJECT, DEPLOYMENT)
    assert exc.value.status_code == 404


# --- Web router: manual sleep (the other half of the toggle) --------------------------


@pytest.mark.parametrize("role", ["member", "developer", "viewer"])
async def test_web_sleep_non_privileged_role_forbidden(role: str) -> None:
    with (
        patch("opi.web.router.get_current_user", return_value={"email": "user@example.com"}),
        patch("opi.web.router.is_user_authorized_for_project", return_value=True),
        patch("opi.web.router.get_user_role_for_project", return_value=role),
        patch.object(flow, "sleep", AsyncMock()) as sleep_mock,
        pytest.raises(HTTPException) as exc,
    ):
        await sleep_deployment_web(_request(), PROJECT, DEPLOYMENT)
    assert exc.value.status_code == 403
    sleep_mock.assert_not_called()


async def test_web_sleep_unauthorized_project_forbidden() -> None:
    with (
        patch("opi.web.router.get_current_user", return_value={"email": "stranger@example.com"}),
        patch("opi.web.router.is_user_authorized_for_project", return_value=False),
        patch.object(flow, "sleep", AsyncMock()) as sleep_mock,
        pytest.raises(HTTPException) as exc,
    ):
        await sleep_deployment_web(_request(), PROJECT, DEPLOYMENT)
    assert exc.value.status_code == 403
    sleep_mock.assert_not_called()


def _store_with_deployment(name: str = DEPLOYMENT):
    """Stub the project store: the web endpoints look the project up before creating a
    task, and check the deployment exists so an unknown name is a 404 at the click
    instead of a task that fails a moment later."""
    project = MagicMock()
    project.data = {"name": PROJECT, "deployments": [{"name": name}]}
    store = MagicMock()
    store.get.return_value = project
    return store


@pytest.mark.parametrize("role", ["admin", "owner"])
async def test_web_sleep_privileged_role_starts_a_task(role: str) -> None:
    """Not an inline call any more: putting a deployment to sleep commits to git and then reprocesses,
    ArgoCD sync included, so it runs as a task the page can follow."""
    with (
        patch("opi.web.router.get_current_user", return_value={"email": "boss@example.com"}),
        patch("opi.web.router.is_user_authorized_for_project", return_value=True),
        patch("opi.web.router.get_user_role_for_project", return_value=role),
        patch("opi.web.router.get_project_store", return_value=_store_with_deployment()),
        patch("opi.web.router.create_task_and_render_progress", AsyncMock()) as start_task,
        patch.object(flow, "sleep", AsyncMock()) as inline,
    ):
        await sleep_deployment_web(_request(), PROJECT, DEPLOYMENT)

    inline.assert_not_called()
    payload = start_task.await_args.kwargs
    assert payload["task_type"] == "sleep_deployment"
    assert payload["payload"]["direction"] == "sleep"


async def test_web_sleep_unknown_deployment_404() -> None:
    """An unknown deployment is a 404 at the click, not a task that fails later."""
    with (
        patch("opi.web.router.get_current_user", return_value={"email": "boss@example.com"}),
        patch("opi.web.router.is_user_authorized_for_project", return_value=True),
        patch("opi.web.router.get_user_role_for_project", return_value="admin"),
        patch("opi.web.router.get_project_store", return_value=_store_with_deployment("iets-anders")),
        pytest.raises(HTTPException) as exc,
    ):
        await sleep_deployment_web(_request(), PROJECT, DEPLOYMENT)
    assert exc.value.status_code == 404
