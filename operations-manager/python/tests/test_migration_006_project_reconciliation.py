"""Migration 006 on a real Postgres: the table, its index, and the ORM model agree."""

from __future__ import annotations

import opi.services.persistence  # noqa: F401  -- registers the ORM models on Base.metadata
import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from opi.core.async_task_service import AsyncTaskService
from opi.core.db import Base, configure_engine, dispose_engine, include_orm_object
from opi.services.project_reconciliation_service import record_reconciliation
from sqlalchemy import Engine, create_engine, text
from tests.conftest import alembic_upgrade


def test_upgrade_head_on_an_empty_database_matches_the_orm_models(migration_db: str) -> None:
    engine = create_engine(migration_db)
    try:
        alembic_upgrade(engine, "head")
        with engine.connect() as conn:
            context = MigrationContext.configure(
                conn, opts={"include_object": include_orm_object, "target_metadata": Base.metadata}
            )
            drift = [d for d in compare_metadata(context, Base.metadata) if "project_reconciliation" in repr(d)]
            index = conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_project_reconciliation_scope'")
            ).scalar_one()
    finally:
        engine.dispose()

    assert drift == []
    assert "UNIQUE" in index
    assert "COALESCE(deployment_name, ''::character varying)" in index


def test_project_wide_is_unique_although_it_is_null(migration_db: str) -> None:
    """NULL is not equal to NULL in a plain unique constraint; the COALESCE index makes it so."""
    engine = create_engine(migration_db)
    try:
        alembic_upgrade(engine, "head")
        insert = text(
            "INSERT INTO project_reconciliation (project_name, deployment_name, reconciled_at) "
            "VALUES ('p1', :dep, now())"
        )
        with engine.connect() as conn:
            conn.execute(insert, {"dep": None})
            conn.execute(insert, {"dep": "d1"})
            conn.commit()
            with pytest.raises(Exception, match="idx_project_reconciliation_scope"):
                conn.execute(insert, {"dep": None})
    finally:
        engine.dispose()


# The task history of mpfm-w3h, reduced to what the count reads: the last refresh before the
# changes, the fifteen changes saved with rollout=false after it, and the full processing
# runs of other task types that the old measurement did not see.
_LAST_REFRESH = ("refresh_project", None, None, "2026-09-10 10:58:00+00", "2026-09-10 11:10:00+00")
_CLEARED_BEFORE = ("update_image", '{"rollout": false}', "{dev}", None, "2026-09-10 09:00:00+00")
_WAITING = [
    ("update_image", '{"rollout": false}', "{dev}", None, "2026-09-10 12:24:00+00"),
    *[("configure_service", '{"rollout": false}', "{dev}", None, f"2026-09-11 0{i}:00:00+00") for i in range(7)],
    *[("update_component", '{"rollout": false}', None, None, f"2026-09-14 0{i}:00:00+00") for i in range(7)],
]
_FULL_RUNS_NOT_COUNTED = [
    ("update_component", "{}", None, "2026-09-10 12:26:00+00", "2026-09-10 12:27:00+00"),
    ("add_service", "{}", None, "2026-09-14 18:34:00+00", "2026-09-14 18:36:00+00"),
]


def _insert_tasks(engine: Engine, project: str, tasks: list[tuple]) -> None:
    with engine.connect() as conn:
        for task_type, payload, affects, started, completed in tasks:
            conn.execute(
                text(
                    "INSERT INTO async_tasks (task_type, project_name, cluster, status, payload, "
                    "affects_deployments, started_at, completed_at) VALUES (:type, :project, 'c1', "
                    "'completed', CAST(:payload AS jsonb), CAST(:affects AS varchar(63)[]), :started, :completed)"
                ),
                {
                    "type": task_type,
                    "project": project,
                    "payload": payload or "{}",
                    "affects": affects,
                    "started": started,
                    "completed": completed,
                },
            )
        conn.commit()


async def test_the_backfill_reads_the_same_count_and_the_next_full_run_clears_it(migration_db: str) -> None:
    engine = create_engine(migration_db)
    try:
        alembic_upgrade(engine, "005")
        _insert_tasks(engine, "mpfm-w3h", [_LAST_REFRESH, _CLEARED_BEFORE, *_WAITING, *_FULL_RUNS_NOT_COUNTED])
        _insert_tasks(engine, "nooit-ververst", [_WAITING[0], _WAITING[-1]])
        alembic_upgrade(engine, "head")
        with engine.connect() as conn:
            backfilled = conn.execute(
                text("SELECT project_name, deployment_name, reconciled_at::text FROM project_reconciliation")
            ).all()
    finally:
        engine.dispose()

    assert backfilled == [("mpfm-w3h", None, "2026-09-10 10:58:00+00")]

    configure_engine(migration_db.replace("postgresql+psycopg2", "postgresql+asyncpg"))
    try:
        svc = AsyncTaskService(cluster="c1")
        pending = await svc.get_deferred_rollouts("mpfm-w3h")
        assert pending["count"] == 15
        assert pending["since"] == "2026-09-10T12:24:00+00:00"
        assert (await svc.get_deferred_rollouts("nooit-ververst"))["count"] == 2

        await record_reconciliation("mpfm-w3h", ["dev"], project_wide=True, read_seconds_ago=0)

        assert (await svc.get_deferred_rollouts("mpfm-w3h"))["count"] == 0
    finally:
        await dispose_engine()
