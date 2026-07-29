"""Service layer for marked-for-deletion resource tracking.

Provides CRUD operations for marking persistent data resources (databases, buckets,
PVCs) for deferred deletion, via the SQLAlchemy ORM
(:class:`opi.services.persistence.marked_for_deletion.MarkedForDeletion`). Resources are
held until the reconciliation job confirms they are both orphaned and past the grace
period.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from opi.core.db import session_scope
from opi.services.persistence.marked_for_deletion import MarkedForDeletion

logger = logging.getLogger(__name__)


class MarkedForDeletionService:
    """Service for managing resources marked for deferred deletion (ORM-backed)."""

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

        Upsert on (resource_type, resource_name, cluster): re-marking the same resource
        refreshes project/deployment/metadata but NOT ``marked_at``, so the original
        grace-period start is preserved.
        """
        payload = metadata or {}
        async with session_scope() as session:
            stmt = (
                pg_insert(MarkedForDeletion)
                .values(
                    resource_type=resource_type,
                    resource_name=resource_name,
                    project_name=project_name,
                    deployment_name=deployment_name,
                    cluster=cluster,
                    resource_metadata=payload,
                )
                .on_conflict_do_update(
                    index_elements=["resource_type", "resource_name", "cluster"],
                    # set_ keys are raw column names -> use "metadata", not the attr name.
                    set_={
                        "project_name": project_name,
                        "deployment_name": deployment_name,
                        "metadata": payload,
                    },
                )
                .returning(MarkedForDeletion.id)
            )
            mark_id = (await session.execute(stmt)).scalar_one()
            row = await session.get(MarkedForDeletion, mark_id)
            logger.info(
                "Marked %s '%s' for deletion (project=%s, deployment=%s, cluster=%s)",
                resource_type,
                resource_name,
                project_name,
                deployment_name,
                cluster,
            )
            return row.to_dict()

    async def unmark_resource(self, resource_type: str, resource_name: str, cluster: str) -> bool:
        """Remove a deletion mark (e.g. resource restored via git revert)."""
        async with session_scope() as session:
            result = await session.execute(
                delete(MarkedForDeletion).where(
                    MarkedForDeletion.resource_type == resource_type,
                    MarkedForDeletion.resource_name == resource_name,
                    MarkedForDeletion.cluster == cluster,
                )
            )
            deleted = result.rowcount > 0
            if deleted:
                logger.info(
                    "Unmarked %s '%s' on cluster '%s' (resource restored)", resource_type, resource_name, cluster
                )
            return deleted

    async def get_expired_marks(self, grace_period_days: int, project_name: str | None = None) -> list[dict]:
        """Marks past the grace period (server-side ``NOW() - make_interval(days => N)``)."""
        cutoff = func.now() - func.make_interval(0, 0, 0, grace_period_days)
        async with session_scope() as session:
            stmt = select(MarkedForDeletion).where(MarkedForDeletion.marked_at < cutoff)
            if project_name is not None:
                stmt = stmt.where(MarkedForDeletion.project_name == project_name)
            stmt = stmt.order_by(MarkedForDeletion.marked_at.asc())
            return [row.to_dict() for row in (await session.execute(stmt)).scalars().all()]

    async def get_marks_for_project(self, project_name: str) -> list[dict]:
        """All marks for a specific project."""
        async with session_scope() as session:
            result = await session.execute(
                select(MarkedForDeletion)
                .where(MarkedForDeletion.project_name == project_name)
                .order_by(MarkedForDeletion.marked_at.asc())
            )
            return [row.to_dict() for row in result.scalars().all()]

    async def get_marks_in_namespace(self, namespace: str, cluster: str) -> list[dict]:
        """Marks that reference a namespace (as a namespace resource, or in metadata)."""
        async with session_scope() as session:
            result = await session.execute(
                select(MarkedForDeletion)
                .where(
                    MarkedForDeletion.cluster == cluster,
                    or_(
                        (MarkedForDeletion.resource_type == "namespace")
                        & (MarkedForDeletion.resource_name == namespace),
                        MarkedForDeletion.resource_metadata["namespace"].astext == namespace,
                    ),
                )
                .order_by(MarkedForDeletion.marked_at.asc())
            )
            return [row.to_dict() for row in result.scalars().all()]

    async def get_marks_for_deployment(
        self, project_name: str, deployment_name: str, cluster: str, resource_type: str
    ) -> list[dict]:
        """Marks for one ``(project, deployment, cluster)`` of a given ``resource_type``.

        This is the deployment-scoped selector used to reconcile file-backed marks (e.g.
        PVCs) against the manifests actually on disk: the caller drops the rows whose
        backing file is gone. Selecting on project + deployment + type keeps it independent
        of how the individual resource name is encoded.
        """
        async with session_scope() as session:
            result = await session.execute(
                select(MarkedForDeletion)
                .where(
                    MarkedForDeletion.project_name == project_name,
                    MarkedForDeletion.deployment_name == deployment_name,
                    MarkedForDeletion.cluster == cluster,
                    MarkedForDeletion.resource_type == resource_type,
                )
                .order_by(MarkedForDeletion.marked_at.asc())
            )
            return [row.to_dict() for row in result.scalars().all()]

    async def delete_mark(self, mark_id: str) -> bool:
        """Delete a mark after the resource has been purged."""
        async with session_scope() as session:
            result = await session.execute(delete(MarkedForDeletion).where(MarkedForDeletion.id == uuid.UUID(mark_id)))
            deleted = result.rowcount > 0
            if deleted:
                logger.info("Deleted mark %s after resource purge", mark_id)
            return deleted

    async def get_all_marks(self) -> list[dict]:
        """All current deletion marks."""
        async with session_scope() as session:
            result = await session.execute(select(MarkedForDeletion).order_by(MarkedForDeletion.marked_at.asc()))
            return [row.to_dict() for row in result.scalars().all()]
