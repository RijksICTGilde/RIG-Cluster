"""Registry for user-launched ephemeral Kubernetes workloads ("runs").

A run is the DB record + audit trail for a workload OPI provisions on request
and monitors via the cluster (the database console today; ad-hoc job pods next).
Live status always comes from the pod; this table records lifecycle milestones
and history. Unlike async_tasks, runs are NOT executed by the task worker -
they are driven by the console manager and the reaper.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from opi.core.database_pools import get_database_pool

if TYPE_CHECKING:
    from opi.core.database_pool import DatabasePool

logger = logging.getLogger(__name__)


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


def _row_to_dict(row: Any) -> dict | None:
    """Convert an asyncpg Record to a JSON-serializable dict."""
    if row is None:
        return None
    result = dict(row)
    for key, value in result.items():
        if isinstance(value, uuid.UUID):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
        elif key == "spec" and isinstance(value, str):
            result[key] = json.loads(value)
    return result


class RunsService:
    """CRUD over the `runs` table."""

    def __init__(self, pool: DatabasePool) -> None:
        self._pool = pool

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
        conn = await self._pool.acquire()
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO runs
                    (kind, session_id, cluster, project, deployment, namespace, name,
                     spec, status, url, started_by, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11, $12)
                RETURNING *
                """,
                str(kind),
                session_id,
                cluster,
                project,
                deployment,
                namespace,
                name,
                json.dumps(spec),
                str(RunStatus.STARTING),
                url,
                started_by,
                expires_at,
            )
            result = _row_to_dict(row)
            if result is None:
                raise RuntimeError("INSERT INTO runs returned no row")
            logger.info(
                "Created run %s kind=%s session=%s for %s/%s", result["id"], kind, session_id, project, deployment
            )
            return result
        finally:
            await self._pool.release(conn)

    async def list_runs(self, project: str, *, include_ended: bool = False, limit: int = 50) -> list[dict]:
        """List runs for a project, newest first. By default only active ones."""
        conn = await self._pool.acquire()
        try:
            if include_ended:
                rows = await conn.fetch(
                    "SELECT * FROM runs WHERE project = $1 ORDER BY started_at DESC LIMIT $2", project, limit
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM runs WHERE project = $1 AND status IN ('starting', 'running') "
                    "ORDER BY started_at DESC LIMIT $2",
                    project,
                    limit,
                )
            return [r for r in (_row_to_dict(row) for row in rows) if r is not None]
        finally:
            await self._pool.release(conn)

    async def get_latest_run(self, project: str, deployment: str, kind: RunKind) -> dict | None:
        """Most recent run for a project+deployment of a given kind (any status).

        Used by the status poll to show 'starting'/'failed' before the pod exists
        (the slow provisioning runs in the background).
        """
        conn = await self._pool.acquire()
        try:
            row = await conn.fetchrow(
                "SELECT * FROM runs WHERE project = $1 AND deployment = $2 AND kind = $3 "
                "ORDER BY started_at DESC LIMIT 1",
                project,
                deployment,
                str(kind),
            )
            return _row_to_dict(row)
        finally:
            await self._pool.release(conn)

    async def mark_running(self, session_id: str, url: str | None = None) -> None:
        """Promote a starting run to running once its pod is ready (idempotent)."""
        conn = await self._pool.acquire()
        try:
            await conn.execute(
                """
                UPDATE runs
                SET status = 'running',
                    url = COALESCE($2, url),
                    updated_at = NOW()
                WHERE session_id = $1 AND status = 'starting'
                """,
                session_id,
                url,
            )
        finally:
            await self._pool.release(conn)

    async def mark_ended(
        self, session_id: str, status: RunStatus, ended_by: str | None = None, error_message: str | None = None
    ) -> None:
        """Set a terminal status on a run (idempotent: only acts on active rows)."""
        conn = await self._pool.acquire()
        try:
            await conn.execute(
                """
                UPDATE runs
                SET status = $2,
                    ended_at = NOW(),
                    ended_by = $3,
                    error_message = $4,
                    updated_at = NOW()
                WHERE session_id = $1 AND status IN ('starting', 'running')
                """,
                session_id,
                str(status),
                ended_by,
                error_message,
            )
        finally:
            await self._pool.release(conn)


_runs_service: RunsService | None = None


def get_runs_service() -> RunsService:
    """Return the process-wide RunsService bound to the main DB pool."""
    global _runs_service
    if _runs_service is None:
        _runs_service = RunsService(get_database_pool("main"))
    return _runs_service
