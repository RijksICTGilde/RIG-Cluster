"""The reaper is DB-driven: it only touches namespaces that have an active run,
so an idle cluster costs one DB query and zero kubectl calls.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from opi.core.db_console_reaper import DbConsoleReaper


async def _run_sweep(active_runs: list[dict], pods_by_ns: dict | None = None):
    pods_by_ns = pods_by_ns or {}
    reaper = DbConsoleReaper(cluster="odcn-production")
    reaper._reap_orphans = AsyncMock()  # exercised elsewhere; not the focus here
    reaper._gc_orphan_clients = AsyncMock()

    scanned: list[str] = []

    async def _get(_kind, namespace, _label):
        scanned.append(namespace)
        return pods_by_ns.get(namespace, [])

    kubectl = MagicMock()
    kubectl.get_resources_by_label = AsyncMock(side_effect=_get)

    runs_svc = MagicMock()
    runs_svc.list_active_runs = AsyncMock(return_value=active_runs)

    with (
        patch("opi.core.db_console_reaper.get_runs_service", return_value=runs_svc),
        patch("opi.core.db_console_reaper.create_kubectl_connector", return_value=kubectl),
        patch("opi.core.db_console_reaper.get_db_console_manager", return_value=MagicMock()),
    ):
        await reaper._sweep()

    runs_svc.list_active_runs.assert_awaited_once_with("odcn-production")
    return scanned, reaper


async def test_idle_cluster_does_zero_kubectl_scans():
    scanned, reaper = await _run_sweep([])
    assert scanned == []  # no active runs -> no namespace listed in Kubernetes
    reaper._gc_orphan_clients.assert_awaited_once()


async def test_only_namespaces_with_active_runs_are_scanned():
    runs = [
        {"namespace": "rig-prd-foo", "session_id": "s1"},
        {"namespace": "rig-prd-foo", "session_id": "s2"},  # same ns -> scanned once
        {"namespace": "rig-prd-bar", "session_id": "s3"},
    ]
    scanned, reaper = await _run_sweep(runs)
    assert sorted(scanned) == ["rig-prd-bar", "rig-prd-foo"]
    reaper._gc_orphan_clients.assert_awaited_once()
