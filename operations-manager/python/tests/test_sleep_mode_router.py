"""Auth-path tests for the three new sleep-mode wake endpoints.

Two live on the API router (waker pod, ``X-Wake-Token`` auth):
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

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from opi.services.catalog.sleep_mode import flow
from opi.services.catalog.sleep_mode.flow import DeploymentNotFound, InvalidWakeToken, WakeResult
from opi.services.catalog.sleep_mode.router import sleep_mode_router
from opi.web.router import wake_deployment_web

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
    assert resp.json()["detail"] == "X-Wake-Token header required"


def test_status_missing_token_returns_401(client: TestClient) -> None:
    resp = client.get(f"/api/sleep-mode/{PROJECT}/{DEPLOYMENT}/status")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "X-Wake-Token header required"


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
async def test_web_wake_privileged_role_wakes(role: str) -> None:
    result = WakeResult(changed=True, state="waking")
    with (
        patch("opi.web.router.get_current_user", return_value={"email": "boss@example.com"}),
        patch("opi.web.router.is_user_authorized_for_project", return_value=True),
        patch("opi.web.router.get_user_role_for_project", return_value=role),
        patch.object(flow, "wake", AsyncMock(return_value=result)) as wake_mock,
    ):
        resp = await wake_deployment_web(_request(), PROJECT, DEPLOYMENT)
    assert resp.status_code == 200
    wake_mock.assert_awaited_once_with(PROJECT, DEPLOYMENT)


async def test_web_wake_unknown_deployment_404() -> None:
    with (
        patch("opi.web.router.get_current_user", return_value={"email": "boss@example.com"}),
        patch("opi.web.router.is_user_authorized_for_project", return_value=True),
        patch("opi.web.router.get_user_role_for_project", return_value="admin"),
        patch.object(flow, "wake", AsyncMock(side_effect=DeploymentNotFound("Deployment 'PR-1' not found"))),
        pytest.raises(HTTPException) as exc,
    ):
        await wake_deployment_web(_request(), PROJECT, DEPLOYMENT)
    assert exc.value.status_code == 404
