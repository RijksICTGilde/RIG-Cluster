"""POST /api/v2/admin/projects/:reprocess-all queues every project of this cluster.

What a switch of the routing API needs: render every deployment again, so the publication of
the new API lands in git and the files of the old one disappear from it. The guard in front
of it is the point of these tests: nobody points every project at an entrypoint that is not
there.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import opi.core.config
import opi.services.project_reprocess as reprocess_module
import pytest
from fastapi import HTTPException
from opi.api import admin_router as admin_module
from opi.api.admin_router import reprocess_all_projects
from opi.services.routing_readiness import RoutingEntrypointMissingError, RoutingEntrypointUnknownError


@pytest.fixture
def live_settings(monkeypatch):
    """Point admin_router at the live settings object, as test_admin_projects_reconcile does."""
    real_settings = opi.core.config.settings
    monkeypatch.setattr(admin_module, "settings", real_settings)
    return real_settings


def _request(platformbeheerder_token, verse_otp) -> AsyncMock:
    request = AsyncMock()
    request.headers = platformbeheerder_token["headers"] | verse_otp["headers"]
    request.url = SimpleNamespace(path="/api/v2/admin/projects/:reprocess-all")
    return request


def _store(*names: str) -> MagicMock:
    store = MagicMock()
    store.get_all = MagicMock(return_value=[SimpleNamespace(name=name) for name in names])
    return store


def _task_service() -> MagicMock:
    service = MagicMock()
    service.create_task = AsyncMock(side_effect=lambda **kwargs: {"task_id": f"task-{kwargs['project_name']}"})
    return service


@pytest.mark.asyncio
async def test_every_project_gets_a_task(live_settings, platformbeheerder_token, verse_otp) -> None:
    task_service = _task_service()

    with (
        patch.object(reprocess_module, "get_project_store", return_value=_store("alpha", "beta")),
        patch.object(admin_module, "get_task_service", return_value=task_service),
        patch.object(admin_module, "verify_routing_entrypoint", AsyncMock(return_value="Gateway is Programmed")),
    ):
        response = await reprocess_all_projects(request=_request(platformbeheerder_token, verse_otp))

    body = json.loads(response.body)
    assert response.status_code == 202
    assert body["scheduled"] == 2
    assert body["tasks"] == {"alpha": "task-alpha", "beta": "task-beta"}
    assert body["entrypoint"] == "Gateway is Programmed"
    assert task_service.create_task.await_count == 2


@pytest.mark.asyncio
async def test_an_absent_entrypoint_gives_409_and_queues_nothing(
    live_settings, platformbeheerder_token, verse_otp
) -> None:
    task_service = _task_service()
    missing = RoutingEntrypointMissingError("Gateway 'rig-gateway/rig-gateway' bestaat niet")

    with (
        patch.object(reprocess_module, "get_project_store", return_value=_store("alpha")),
        patch.object(admin_module, "get_task_service", return_value=task_service),
        patch.object(admin_module, "verify_routing_entrypoint", AsyncMock(side_effect=missing)),
        pytest.raises(HTTPException) as exc,
    ):
        await reprocess_all_projects(request=_request(platformbeheerder_token, verse_otp))

    assert exc.value.status_code == 409
    assert "bestaat niet" in exc.value.detail
    task_service.create_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_check_gives_503_and_never_409(live_settings, platformbeheerder_token, verse_otp) -> None:
    """A timeout or an apiserver that is gone may never read as "the entrypoint is absent"."""
    task_service = _task_service()
    unknown = RoutingEntrypointUnknownError("Kon niet vaststellen of Gateway bestaat")

    with (
        patch.object(reprocess_module, "get_project_store", return_value=_store("alpha")),
        patch.object(admin_module, "get_task_service", return_value=task_service),
        patch.object(admin_module, "verify_routing_entrypoint", AsyncMock(side_effect=unknown)),
        pytest.raises(HTTPException) as exc,
    ):
        await reprocess_all_projects(request=_request(platformbeheerder_token, verse_otp))

    assert exc.value.status_code == 503
    task_service.create_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_nothing_gets_through_without_a_platform_admin(live_settings, platformbeheerder_token) -> None:
    task_service = _task_service()
    request = AsyncMock()
    request.headers = platformbeheerder_token["headers_gewone_gebruiker"]

    with (
        patch.object(admin_module, "get_task_service", return_value=task_service),
        patch.object(admin_module, "verify_routing_entrypoint", AsyncMock(return_value="ok")),
        pytest.raises(HTTPException) as exc,
    ):
        await reprocess_all_projects(request=request)

    assert exc.value.status_code == 403
    task_service.create_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_shared_key_is_no_identification_here(live_settings, platformbeheerder_token) -> None:
    request = AsyncMock()
    request.headers = {"X-API-Key": "een-gedeelde-sleutel"}

    with (
        patch.object(admin_module, "verify_routing_entrypoint", AsyncMock(return_value="ok")),
        pytest.raises(HTTPException) as exc,
    ):
        await reprocess_all_projects(request=request)

    assert exc.value.status_code == 401
