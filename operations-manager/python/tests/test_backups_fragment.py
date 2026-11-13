"""Unit tests for the backups fragment in ``opi.services.catalog.shared.backups``.

``backups_fragment`` and its helper ``_snapshots_by_deployment`` had no test that actually
executed them: a move of this code introduced a real ``NameError`` that went undetected by
the suite until a manual read. ``test_detail_page_backup_laziness.py`` only inspects the
source as text, so a renamed or broken call passes it unnoticed. These tests call the route
the way ``test_image_registries_detail_block.py`` calls its own fragment, which exercises
the real control flow.

The handler imports what it needs inside itself (the catalog may not pull a manager in at
import time), so each name is patched on the module it comes FROM.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from opi.core import cluster_config, config
from opi.http.auth import authz
from opi.http.render import lotc_switch
from opi.manager import backup as backup_manager_module
from opi.manager.backup.base import SnapshotInfo
from opi.services import project_authorization, project_store
from opi.services.catalog.shared.backups import backups_fragment

PROJECT = "demo"


def _project_data(deployments: list[dict[str, Any]]) -> dict[str, Any]:
    return {"name": PROJECT, "deployments": deployments}


def _request() -> Any:
    return SimpleNamespace(state=SimpleNamespace(user={"email": "admin@rijksoverheid.nl"}))


def _snapshot(deployment_name: str, snapshot_id: str) -> SnapshotInfo:
    return SnapshotInfo(
        snapshot_id=snapshot_id,
        pvc_name="data",
        timestamp="2026-01-01T00:00:00Z",
        deployment_name=deployment_name,
    )


def _patch(
    monkeypatch: pytest.MonkeyPatch, *, project_data: dict[str, Any], list_snapshots: Any
) -> list[dict[str, Any]]:
    project = SimpleNamespace(data=project_data)
    store = SimpleNamespace(get=lambda name: project if name == PROJECT else None)

    monkeypatch.setattr(project_store, "get_project_store", lambda: store)
    monkeypatch.setattr(project_authorization, "is_user_authorized_for_project", lambda project_name, email: True)
    monkeypatch.setattr(cluster_config, "get_prefixed_namespace", lambda cluster, namespace: namespace)
    monkeypatch.setattr(authz, "may_on_any_deployment", lambda request, *, action, project_name: True)
    monkeypatch.setattr(config.settings, "CLUSTER_MANAGER", "local")
    monkeypatch.setattr(backup_manager_module, "BackupManager", lambda: SimpleNamespace(list_snapshots=list_snapshots))

    rendered: list[dict[str, Any]] = []

    def _render(request: Any, *, template: str, context: dict[str, Any]) -> Any:
        rendered.append(context)
        return HTMLResponse("")

    monkeypatch.setattr(lotc_switch, "render", _render)
    return rendered


@pytest.mark.asyncio
async def test_a_shared_namespace_is_listed_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two deployments in one namespace open ONE Kopia repository, not two."""
    deployments = [
        {"name": "dep-a", "cluster": "local", "namespace": "shared-ns"},
        {"name": "dep-b", "cluster": "local", "namespace": "shared-ns"},
    ]
    calls: list[str] = []

    async def list_snapshots(cluster: str, namespace: str, project_name: str | None = None) -> list[SnapshotInfo]:
        calls.append(namespace)
        return [_snapshot("dep-a", "snap-a"), _snapshot("dep-b", "snap-b")]

    rendered = _patch(monkeypatch, project_data=_project_data(deployments), list_snapshots=list_snapshots)

    await backups_fragment(_request(), PROJECT)

    assert calls == ["shared-ns"]
    (context,) = rendered
    assert context["backups_error"] is None
    assert [s["snapshot_id"] for s in context["backups_by_deployment"]["dep-a"]] == ["snap-a"]
    assert [s["snapshot_id"] for s in context["backups_by_deployment"]["dep-b"]] == ["snap-b"]


@pytest.mark.asyncio
async def test_deployments_on_another_cluster_are_not_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    deployments = [
        {"name": "dep-here", "cluster": "local", "namespace": "ns-here"},
        {"name": "dep-elsewhere", "cluster": "odcn-production", "namespace": "ns-elsewhere"},
    ]
    calls: list[str] = []

    async def list_snapshots(cluster: str, namespace: str, project_name: str | None = None) -> list[SnapshotInfo]:
        calls.append(namespace)
        return [_snapshot("dep-here", "snap-here")]

    rendered = _patch(monkeypatch, project_data=_project_data(deployments), list_snapshots=list_snapshots)

    await backups_fragment(_request(), PROJECT)

    assert calls == ["ns-here"]
    (context,) = rendered
    assert list(context["backups_by_deployment"]) == ["dep-here"]


@pytest.mark.asyncio
async def test_a_failed_lookup_shows_a_fixed_message_instead_of_crashing(monkeypatch: pytest.MonkeyPatch) -> None:
    deployments = [{"name": "dep-a", "cluster": "local", "namespace": "ns"}]

    async def list_snapshots(cluster: str, namespace: str, project_name: str | None = None) -> list[SnapshotInfo]:
        raise RuntimeError("kopia connect failed to 172.30.1.2:51515")

    rendered = _patch(monkeypatch, project_data=_project_data(deployments), list_snapshots=list_snapshots)

    await backups_fragment(_request(), PROJECT)

    (context,) = rendered
    assert context["backups_error"]
    assert "172.30.1.2" not in context["backups_error"], "the raw exception text may not reach the screen"
    assert context["backups_by_deployment"]["dep-a"] == []


@pytest.mark.asyncio
async def test_an_unauthorized_caller_gets_403_and_no_snapshots_are_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    async def list_snapshots(cluster: str, namespace: str, project_name: str | None = None) -> list[SnapshotInfo]:
        raise AssertionError("an unauthorized request must not fetch any snapshots")

    rendered = _patch(monkeypatch, project_data=_project_data([]), list_snapshots=list_snapshots)
    monkeypatch.setattr(project_authorization, "is_user_authorized_for_project", lambda project_name, email: False)

    with pytest.raises(HTTPException) as caught:
        await backups_fragment(_request(), PROJECT)

    assert caught.value.status_code == 403
    assert rendered == []


@pytest.mark.asyncio
async def test_an_unknown_project_gets_404(monkeypatch: pytest.MonkeyPatch) -> None:
    store = SimpleNamespace(get=lambda name: None)
    monkeypatch.setattr(project_store, "get_project_store", lambda: store)
    monkeypatch.setattr(project_authorization, "is_user_authorized_for_project", lambda project_name, email: True)

    with pytest.raises(HTTPException) as caught:
        await backups_fragment(_request(), "unknown")

    assert caught.value.status_code == 404


def test_the_block_endpoint_is_mounted_on_the_web_router() -> None:
    """Every backupable service offers the same router through ``web_routers()``; without
    that wiring the block loads a path that does not exist."""
    from opi.web.router import web_router

    paths = {getattr(route, "path", "") for route in web_router.routes}
    assert "/projects/details/{project_name}/backups" in paths
