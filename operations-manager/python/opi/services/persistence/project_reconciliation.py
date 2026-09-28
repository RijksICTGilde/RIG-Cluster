"""ORM model for ``project_reconciliation`` (RC-188, migration 006).

Written by ``ProjectManager.process_project``, read by ``AsyncTaskService.get_deferred_rollouts``.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 (runtime-resolved by SQLAlchemy Mapped)

from sqlalchemy import DateTime, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from opi.core.db import Base

#: The expression the unique index is on. The upsert names it as its conflict target,
#: and Postgres only infers an expression index from the identical expression.
SCOPE_KEY_SQL = "COALESCE(deployment_name, '')"


class ProjectReconciliation(Base):
    """When a run last read the project file and reconciled this scope from it.

    ``deployment_name`` NULL is project-wide, as in ``scope_of()``.
    """

    __tablename__ = "project_reconciliation"

    project_name: Mapped[str] = mapped_column(String(63), nullable=False)
    deployment_name: Mapped[str | None] = mapped_column(String(63))
    reconciled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Geen PK in de tabel (zie migratie 006); de mapper heeft alleen een identiteit nodig.
    __mapper_args__ = {"primary_key": [project_name, deployment_name]}  # noqa: RUF012

    __table_args__ = (
        Index("idx_project_reconciliation_scope", text("project_name"), text(SCOPE_KEY_SQL), unique=True),
    )
