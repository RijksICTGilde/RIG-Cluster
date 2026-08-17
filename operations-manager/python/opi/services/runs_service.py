"""Registry for user-launched ephemeral Kubernetes workloads ("runs").

A run is the DB record + audit trail for a workload OPI provisions on request
and monitors via the cluster (the database console today; ad-hoc job pods next).
Live status always comes from the pod; this table records lifecycle milestones
and history. Unlike async_tasks, runs are NOT executed by the task worker -
they are driven by the console manager and the reaper.

ORM-backed (:class:`opi.services.persistence.runs.Run`) on the shared async engine.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from enum import StrEnum

from sqlalchemy import func, select, update

from opi.core.db import session_scope
from opi.services.persistence.runs import Run

_ACTIVE_STATES = ("starting", "running")


class RunKind(StrEnum):
    """Kind of user-launched workload."""

    DB_CONSOLE = "db-console"
    JOB = "job"  # planned: ad-hoc pod running an image + command


class RunStatus(StrEnum):
    """Run lifecycle. starting -> running -> one terminal state."""

    STARTING = "starting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"  # for jobs that complete 0-exit
    FAILED = "failed"
    STOPPED = "stopped"  # stopped by a user
    EXPIRED = "expired"  # reaped at TTL


class RunsService:
    """CRUD over the ``runs`` table (ORM-backed)."""

    async def create_run(
        self,
        *,
        kind: RunKind,
        session_id: str,
        cluster: str,
        project: str,
        deployment: str | None,
        namespace: str,
        name: str,
        spec: dict,
        url: str | None,
        started_by: str | None,
        expires_at: datetime | None,
    ) -> dict:
        """Insert a new run row in the 'starting' state."""
        async with session_scope() as session:
            row = Run(
                kind=str(kind),
                session_id=session_id,
                cluster=cluster,
                project=project,
                deployment=deployment,
                namespace=namespace,
                name=name,
                spec=spec,
                status=str(RunStatus.STARTING),
                url=url,
                started_by=started_by,
                expires_at=expires_at,
            )
            session.add(row)
            await session.flush()
            await session.refresh(row)  # server defaults (id, timestamps)
            return row.to_dict()

    async def list_runs(self, project: str, *, include_ended: bool = False, limit: int = 50) -> list[dict]:
        """List runs for a project, newest first. By default only active ones."""
        stmt = select(Run).where(Run.project == project)
        if not include_ended:
            stmt = stmt.where(Run.status.in_(_ACTIVE_STATES))
        stmt = stmt.order_by(Run.started_at.desc()).limit(limit)
        async with session_scope() as session:
            return [row.to_dict() for row in (await session.execute(stmt)).scalars().all()]

    async def list_active_runs(self, cluster: str) -> list[dict]:
        """Every active (starting/running) run on a cluster, across all projects."""
        async with session_scope() as session:
            result = await session.execute(
                select(Run)
                .where(Run.cluster == cluster, Run.status.in_(_ACTIVE_STATES))
                .order_by(Run.started_at.desc())
            )
            return [row.to_dict() for row in result.scalars().all()]

    async def get_latest_run(self, project: str, deployment: str, kind: RunKind) -> dict | None:
        """Most recent run for a project+deployment of a given kind (any status)."""
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(Run)
                    .where(Run.project == project, Run.deployment == deployment, Run.kind == str(kind))
                    .order_by(Run.started_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            return row.to_dict() if row else None

    async def mark_running(self, session_id: str, url: str | None = None) -> None:
        """Promote a starting run to running once its pod is ready (idempotent)."""
        values: dict = {"status": "running", "updated_at": func.now()}
        if url is not None:  # COALESCE: keep the existing url when none is given
            values["url"] = url
        async with session_scope() as session:
            await session.execute(
                update(Run).where(Run.session_id == session_id, Run.status == "starting").values(**values)
            )

    async def mark_ended(
        self, session_id: str, status: RunStatus, ended_by: str | None = None, error_message: str | None = None
    ) -> None:
        """Set a terminal status on a run (idempotent: only acts on active rows)."""
        async with session_scope() as session:
            await session.execute(
                update(Run)
                .where(Run.session_id == session_id, Run.status.in_(_ACTIVE_STATES))
                .values(
                    status=str(status),
                    ended_at=func.now(),
                    ended_by=ended_by,
                    error_message=error_message,
                    updated_at=func.now(),
                )
            )


_runs_service: RunsService | None = None


def get_runs_service() -> RunsService:
    """Return the process-wide RunsService."""
    global _runs_service
    if _runs_service is None:
        _runs_service = RunsService()
    return _runs_service
