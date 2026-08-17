"""POST /api/v2/admin/projects/:reconcile pulls the projects repo into the store on demand.

The store re-reads ``zad-projects`` on a timer, so a file committed there by anything other
than ZAD -- an import of production files, a hand edit, another cluster -- stays invisible
until the next tick, and every call for it answers "project not found". Waiting out the poll
is the only alternative, which is why this endpoint exists.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import opi.core.config
import pytest
from fastapi import HTTPException
from opi.api import admin_router as admin_module
from opi.api import endpoint_util
from opi.api.admin_router import reconcile_projects


@pytest.fixture
def live_settings(monkeypatch):
    """Point both modules at the live settings object.

    ``admin_router`` and ``endpoint_util`` bind ``settings`` by reference at import time, so
    in a full-suite run they can be left pointing at another test's mock -- which shows up
    here as a spurious 501. Same guard as test_service_orphan_sweep.
    """
    real_settings = opi.core.config.settings
    monkeypatch.setattr(admin_module, "settings", real_settings)
    monkeypatch.setattr(endpoint_util, "settings", real_settings)
    monkeypatch.setattr(real_settings, "ADMIN_API_KEY", "test-admin-key")
    return real_settings


def _store(head_before: str | None, head_after: str | None) -> MagicMock:
    store = MagicMock()
    store.reconcile = AsyncMock()
    store.cache_head = MagicMock(side_effect=[head_before, head_after])
    return store


@pytest.mark.asyncio
async def test_reconcile_reports_the_new_head_when_the_repo_moved(live_settings) -> None:
    request = AsyncMock()
    request.headers = {"X-API-Key": "test-admin-key"}
    store = _store("aaaa1111", "bbbb2222")

    with patch.object(admin_module, "get_project_store", return_value=store):
        response = await reconcile_projects(request=request)

    store.reconcile.assert_awaited_once()
    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["head_before"] == "aaaa1111"
    assert body["head_after"] == "bbbb2222"
    assert body["changed"] is True


@pytest.mark.asyncio
async def test_reconcile_is_a_no_op_when_nothing_changed(live_settings) -> None:
    request = AsyncMock()
    request.headers = {"X-API-Key": "test-admin-key"}
    store = _store("aaaa1111", "aaaa1111")

    with patch.object(admin_module, "get_project_store", return_value=store):
        response = await reconcile_projects(request=request)

    body = json.loads(response.body)
    assert body["changed"] is False


@pytest.mark.asyncio
async def test_reconcile_refuses_a_wrong_key(live_settings) -> None:
    request = AsyncMock()
    request.headers = {"X-API-Key": "not-the-admin-key"}
    store = _store("aaaa1111", "bbbb2222")

    with patch.object(admin_module, "get_project_store", return_value=store), pytest.raises(HTTPException) as exc:
        await reconcile_projects(request=request)

    assert exc.value.status_code == 401
    store.reconcile.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_refuses_a_missing_key(live_settings) -> None:
    request = AsyncMock()
    request.headers = {}
    store = _store("aaaa1111", "bbbb2222")

    with patch.object(admin_module, "get_project_store", return_value=store), pytest.raises(HTTPException) as exc:
        await reconcile_projects(request=request)

    assert exc.value.status_code == 401
    store.reconcile.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_is_disabled_when_no_admin_key_is_configured(monkeypatch) -> None:
    """The default everywhere today: no ADMIN_API_KEY means the endpoint is off, not open."""
    real_settings = opi.core.config.settings
    monkeypatch.setattr(admin_module, "settings", real_settings)
    monkeypatch.setattr(endpoint_util, "settings", real_settings)
    monkeypatch.setattr(real_settings, "ADMIN_API_KEY", None)

    request = AsyncMock()
    request.headers = {"X-API-Key": "anything"}
    store = _store("aaaa1111", "bbbb2222")

    with patch.object(admin_module, "get_project_store", return_value=store), pytest.raises(HTTPException) as exc:
        await reconcile_projects(request=request)

    assert exc.value.status_code == 501
    store.reconcile.assert_not_awaited()
