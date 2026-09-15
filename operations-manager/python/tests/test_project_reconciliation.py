"""What ``process_project`` records as reconciled (RC-188), on a real Postgres.

The drift count reads these rows, so they must say exactly what the run processed: the
in-scope deployments of this cluster, the project-wide row only for an unscoped run, and
nothing for a deployment whose manifests failed.
"""

from __future__ import annotations

import asyncio
from datetime import datetime  # noqa: TC003
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from opi.core.config import settings
from opi.core.db import configure_engine, dispose_engine, session_scope
from opi.manager.project_manager import DeploymentResult, ProjectManager
from opi.services.persistence.project_reconciliation import ProjectReconciliation
from opi.services.project_reconciliation_service import record_reconciliation
from sqlalchemy import func, select, text

HERE = settings.CLUSTER_MANAGER


def _project() -> dict[str, Any]:
    return {
        "name": "demo",
        "deployments": [
            {"name": "dev", "cluster": HERE},
            {"name": "prod", "cluster": HERE},
            {"name": "elders", "cluster": f"{HERE}-other"},
        ],
    }


def _manager(monkeypatch: pytest.MonkeyPatch, *, manifests: Any = None) -> ProjectManager:
    """A manager whose every cluster- and git-facing step is a mock; the write is real."""
    monkeypatch.setattr("opi.manager.project_manager.provisioning_services", list)
    pm = ProjectManager(project_file_relative_path="projects/demo.yaml")
    pm.get_contents = AsyncMock(side_effect=lambda **_: _project())
    pm.get_name = AsyncMock(return_value="demo")
    pm.has_deployments_for_current_cluster = AsyncMock(return_value=True)
    pm._project_file_handler = MagicMock()
    pm._project_file_handler.reenable_components_with_changed_image.return_value = []
    pm.check_and_create_namespaces = AsyncMock()
    pm.check_and_create_sops_secrets_in_namespaces = AsyncMock()
    pm._ensure_database_manager = AsyncMock()
    pm._process_application_manifests = AsyncMock(side_effect=manifests)
    pm.save_and_commit_project = AsyncMock()
    pm._argo_manager = MagicMock()
    pm._argo_manager.create_argocd_resources = AsyncMock()
    pm._bootstrap_manager = MagicMock()
    pm._bootstrap_manager.execute_bootstrap_for_deployment = AsyncMock()
    return pm


async def _rows() -> dict[str | None, datetime]:
    async with session_scope() as session:
        result = await session.execute(
            select(ProjectReconciliation.deployment_name, ProjectReconciliation.reconciled_at).where(
                ProjectReconciliation.project_name == "demo"
            )
        )
        return dict(result.tuples().all())


async def test_a_full_run_records_the_project_and_each_deployment_of_this_cluster(orm_db, monkeypatch) -> None:
    pm = _manager(monkeypatch)

    assert await pm.process_project() is True

    rows = await _rows()
    assert set(rows) == {None, "dev", "prod"}
    assert len(set(rows.values())) == 1


async def test_a_scoped_run_records_only_its_targets(orm_db, monkeypatch) -> None:
    pm = _manager(monkeypatch)

    assert await pm.process_project(deployment_names=["prod"]) is True

    assert set(await _rows()) == {"prod"}


async def test_a_deployment_on_another_cluster_is_never_recorded(orm_db, monkeypatch) -> None:
    pm = _manager(monkeypatch)

    assert await pm.process_project(deployment_names=["elders"]) is True

    assert await _rows() == {}


async def test_a_deployment_whose_manifests_failed_gets_no_row(orm_db, monkeypatch) -> None:
    """And no project-wide row either: that one would clear the failed deployment too."""

    async def manifests(**_: Any) -> None:
        pm._deployment_results["dev"] = DeploymentResult("dev", HERE, "demo", status="failed")

    pm = _manager(monkeypatch, manifests=manifests)

    assert await pm.process_project() is True

    assert set(await _rows()) == {"prod"}


async def test_a_run_that_fails_records_nothing(orm_db, monkeypatch) -> None:
    pm = _manager(monkeypatch)
    pm._argo_manager.create_argocd_resources = AsyncMock(side_effect=RuntimeError("argocd weg"))

    assert await pm.process_project() is False

    assert await _rows() == {}


async def test_a_project_with_nothing_on_this_cluster_is_reconciled_project_wide(orm_db, monkeypatch) -> None:
    """Otherwise a deferred change to a project without deployments here waits forever."""
    pm = _manager(monkeypatch)
    pm.has_deployments_for_current_cluster = AsyncMock(return_value=False)

    assert await pm.process_project() is True

    assert set(await _rows()) == {None}


async def test_the_stored_moment_is_when_the_run_read_the_project_file(orm_db, monkeypatch) -> None:
    """A change saved while the run generated manifests is not in its snapshot (RC-82)."""
    during: list[datetime] = []

    async def manifests(**_: Any) -> None:
        await asyncio.sleep(0.3)
        async with session_scope() as session:
            during.append((await session.execute(select(func.clock_timestamp()))).scalar_one())

    pm = _manager(monkeypatch, manifests=manifests)

    assert await pm.process_project() is True

    assert all(at < during[0] for at in (await _rows()).values())


async def test_an_earlier_read_never_moves_a_scope_back(orm_db) -> None:
    """Two runs overlap and the one that read first finishes last."""
    await record_reconciliation("demo", ["dev"], project_wide=True, read_seconds_ago=0)
    first = await _rows()

    await record_reconciliation("demo", ["dev"], project_wide=True, read_seconds_ago=3600)

    assert await _rows() == first


async def _unreachable_database() -> None:
    await dispose_engine()
    # Poort 1: niets luistert, asyncpg geeft dan een kale ConnectionRefusedError (OSError).
    configure_engine("postgresql+asyncpg://postgres:x@127.0.0.1:1/weg")


async def _table_not_migrated() -> None:
    async with session_scope() as session:
        await session.execute(text("DROP TABLE project_reconciliation"))


@pytest.mark.parametrize(
    "break_database", [_unreachable_database, _table_not_migrated], ids=["unreachable", "no-table"]
)
async def test_a_database_that_fails_does_not_fail_the_rollout(orm_db, monkeypatch, break_database) -> None:
    """The rollout happened; a missing row only makes the count over-report."""
    pm = _manager(monkeypatch)
    await break_database()

    assert await pm.process_project() is True
    assert pm._processing_error is None
