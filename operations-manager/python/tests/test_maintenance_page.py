"""The startup page tells an anonymous visitor nothing about the inside of the platform.

It used to list every critical service with the error text of its readiness check, and since
that text became the real exception it carried internal database addresses and role names,
unescaped, to anyone who could reach the port. The page now says only that ZAD is
temporarily unavailable; the readiness code logs what is wrong.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.core.readiness import ReadinessState
from opi.middleware.maintenance import MaintenanceMiddleware

INTERNAL_DETAIL = "postgres.rig-prd-operations.svc.cluster.local: role 'opi-writer' does not exist"


@pytest.fixture
def not_ready(monkeypatch: pytest.MonkeyPatch) -> ReadinessState:
    """A platform with one failing service, carrying a recognisable internal error."""
    import opi.core.readiness as readiness

    state = ReadinessState()
    state.keycloak.mark_ready()
    state.oauth.mark_ready()
    state.projects.mark_ready()
    state.database.mark_failed(INTERNAL_DETAIL)
    monkeypatch.setattr(readiness, "_state", state)
    return state


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()

    @app.get("/")
    async def index() -> str:
        return "the portal"

    @app.get("/healthz")
    async def healthz() -> str:
        return "ok"

    app.add_middleware(MaintenanceMiddleware)
    return TestClient(app)


def test_the_page_names_neither_the_service_nor_its_error(client: TestClient, not_ready: ReadinessState) -> None:
    response = client.get("/")

    assert response.status_code == 503
    assert INTERNAL_DETAIL not in response.text
    assert "postgres" not in response.text.lower()
    assert not_ready.database.display_name not in response.text
    assert "tijdelijk niet beschikbaar" in response.text


def test_the_readiness_state_still_holds_the_error_for_the_log(not_ready: ReadinessState) -> None:
    assert not_ready.database.error == INTERNAL_DETAIL


def test_a_probe_is_not_answered_with_the_page(client: TestClient, not_ready: ReadinessState) -> None:
    assert client.get("/healthz").status_code == 200


def test_a_ready_platform_is_served_normally(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import opi.core.readiness as readiness

    state = ReadinessState()
    for service in state.get_all_services():
        service.mark_ready()
    monkeypatch.setattr(readiness, "_state", state)

    assert client.get("/").json() == "the portal"
