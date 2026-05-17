"""Service layer for marked-for-deletion resource tracking.

Provides CRUD operations for marking persistent data resources (databases, buckets,
PVCs) for deferred deletion. Resources are held until the reconciliation job confirms
they are both orphaned and past the configured grace period.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opi.core.database_pool import DatabasePool

logger = logging.getLogger(__name__)


def _row_to_dict(row: Any) -> dict | None:
    """Convert an asyncpg Record to a dict with serializable types."""
    if row is None:
        return None
    result = dict(row)
    for key, value in result.items():
        if isinstance(value, uuid.UUID):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
    return result


class MarkedForDeletionService:
    """Service for managing resources marked for deferred deletion."""

    def __init__(self, pool: DatabasePool) -> None:
        self._pool = pool

    async def mark_resource(
        self,
        resource_type: str,
        resource_name: str,
        project_name: str,
        deployment_name: str,
        cluster: str,
        metadata: dict | None = None,
    ) -> dict:
        """Mark a resource for deferred deletion.

        Uses an upsert to handle the case where the same resource is marked
        again (e.g., after a failed previous deletion attempt). The marked_at
        timestamp is NOT updated on conflict to preserve the original grace
        period start.

        Args:
            resource_type: Type of resource (e.g., 'postgresql_database', 'minio_bucket').
            resource_name: Name of the resource.
            project_name: Project that owned the resource.
            deployment_name: Deployment that owned the resource.
            cluster: Cluster where the resource lives.
            metadata: Additional info needed for deletion (server, namespace, etc.).

        Returns:
            Dict representing the marked resource row.
        """
        conn = await self._pool.acquire()
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO marked_for_deletion
                    (resource_type, resource_name, project_name, deployment_name, cluster, metadata)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                ON CONFLICT (resource_type, resource_name, cluster)
                DO UPDATE SET
                    project_name = EXCLUDED.project_name,
                    deployment_name = EXCLUDED.deployment_name,
                    metadata = EXCLUDED.metadata
                RETURNING *
                """,
                resource_type,
                resource_name,
                project_name,
                deployment_name,
                cluster,
                json.dumps(metadata or {}),
            )
            logger.info(
                "Marked %s '%s' for deletion (project=%s, deployment=%s, cluster=%s)",
                resource_type,
                resource_name,
                project_name,
                deployment_name,
                cluster,
            )
            return _row_to_dict(row)
        finally:
            await self._pool.release(conn)

    async def unmark_resource(
        self,
        resource_type: str,
        resource_name: str,
        cluster: str,
    ) -> bool:
        """Remove a deletion mark (e.g., resource restored via git revert).

        Args:
            resource_type: Type of resource.
            resource_name: Name of the resource.
            cluster: Cluster where the resource lives.

        Returns:
            True if a mark was removed, False if no matching mark existed.
        """
        conn = await self._pool.acquire()
        try:
            result = await conn.execute(
                """
                DELETE FROM marked_for_deletion
                WHERE resource_type = $1 AND resource_name = $2 AND cluster = $3
                """,
                resource_type,
                resource_name,
                cluster,
            )
            deleted_count = int(result.split()[-1])
            if deleted_count > 0:
                logger.info(
                    "Unmarked %s '%s' on cluster '%s' (resource restored)",
                    resource_type,
                    resource_name,
                    cluster,
                )
            return deleted_count > 0
        finally:
            await self._pool.release(conn)

    async def get_expired_marks(self, grace_period_days: int, project_name: str | None = None) -> list[dict]:
        """Get marks that have passed the grace period.

        Args:
            grace_period_days: Number of days after marking before a resource
                is eligible for purging.
            project_name: If provided, only return marks for this project.

        Returns:
            List of mark dicts that are past the grace period.
        """
        conn = await self._pool.acquire()
        try:
            if project_name is not None:
                rows = await conn.fetch(
                    """
                    SELECT * FROM marked_for_deletion
                    WHERE marked_at < NOW() - make_interval(days => $1)
                      AND project_name = $2
                    ORDER BY marked_at ASC
                    """,
                    grace_period_days,
                    project_name,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT * FROM marked_for_deletion
                    WHERE marked_at < NOW() - make_interval(days => $1)
                    ORDER BY marked_at ASC
                    """,
                    grace_period_days,
                )
            return [_row_to_dict(row) for row in rows]
        finally:
            await self._pool.release(conn)

    async def get_marks_for_project(self, project_name: str) -> list[dict]:
        """Get all marks for a specific project.

        Args:
            project_name: The project to query.

        Returns:
            List of mark dicts for the project.
        """
        conn = await self._pool.acquire()
        try:
            rows = await conn.fetch(
                """
                SELECT * FROM marked_for_deletion
                WHERE project_name = $1
                ORDER BY marked_at ASC
                """,
                project_name,
            )
            return [_row_to_dict(row) for row in rows]
        finally:
            await self._pool.release(conn)

    async def get_marks_in_namespace(self, namespace: str, cluster: str) -> list[dict]:
        """Get all marks whose metadata references a specific namespace.

        Useful for checking if a namespace has pending PVC deletions before
        the namespace itself can be purged.

        Args:
            namespace: The Kubernetes namespace to check.
            cluster: The cluster to check.

        Returns:
            List of mark dicts referencing the namespace.
        """
        conn = await self._pool.acquire()
        try:
            rows = await conn.fetch(
                """
                SELECT * FROM marked_for_deletion
                WHERE cluster = $1
                  AND (
                    (resource_type = 'namespace' AND resource_name = $2)
                    OR (metadata->>'namespace' = $2)
                  )
                ORDER BY marked_at ASC
                """,
                cluster,
                namespace,
            )
            return [_row_to_dict(row) for row in rows]
        finally:
            await self._pool.release(conn)

    async def delete_mark(self, mark_id: str) -> bool:
        """Delete a mark after the resource has been purged.

        Args:
            mark_id: UUID of the mark to delete.

        Returns:
            True if the mark was deleted, False if not found.
        """
        conn = await self._pool.acquire()
        try:
            result = await conn.execute(
                "DELETE FROM marked_for_deletion WHERE id = $1",
                uuid.UUID(mark_id),
            )
            deleted_count = int(result.split()[-1])
            if deleted_count > 0:
                logger.info("Deleted mark %s after resource purge", mark_id)
            return deleted_count > 0
        finally:
            await self._pool.release(conn)

    async def get_all_marks(self) -> list[dict]:
        """Get all current deletion marks.

        Returns:
            List of all mark dicts.
        """
        conn = await self._pool.acquire()
        try:
            rows = await conn.fetch("SELECT * FROM marked_for_deletion ORDER BY marked_at ASC")
            return [_row_to_dict(row) for row in rows]
        finally:
            await self._pool.release(conn)
