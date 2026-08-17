"""ORM model for the ``runs`` table (user-launched ephemeral workloads, RC-5).

Schema-as-code mirror of ``RUNS_TABLE_SQL``. The repository + the RunKind/RunStatus
enums live in ``opi/services/runs_service.py``.
"""

from __future__ import annotations

import uuid  # noqa: TC003 (runtime-resolved by SQLAlchemy Mapped)
from datetime import datetime  # noqa: TC003
from typing import Any

from sqlalchemy import DateTime, Index, String, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from opi.core.db import Base


class Run(Base):
    """A user-launched workload's DB record. Matches ``RUNS_TABLE_SQL`` exactly."""

    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    session_id: Mapped[str] = mapped_column(String(32), nullable=False)
    cluster: Mapped[str] = mapped_column(String(63), nullable=False)
    project: Mapped[str] = mapped_column(String(63), nullable=False)
    deployment: Mapped[str | None] = mapped_column(String(63))
    namespace: Mapped[str] = mapped_column(String(63), nullable=False)
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    spec: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'starting'"))
    url: Mapped[str | None] = mapped_column(Text)
    started_by: Mapped[str | None] = mapped_column(String(255))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_by: Mapped[str | None] = mapped_column(String(255))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        Index("idx_runs_session", "session_id", unique=True),
        Index(
            "idx_runs_active",
            "cluster",
            "status",
            postgresql_where=text("status IN ('starting', 'running')"),
        ),
        Index("idx_runs_project", text("project"), text("started_at DESC")),
    )

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict (str id, ISO timestamps, ``spec`` as a dict)."""
        return {
            "id": str(self.id),
            "kind": self.kind,
            "session_id": self.session_id,
            "cluster": self.cluster,
            "project": self.project,
            "deployment": self.deployment,
            "namespace": self.namespace,
            "name": self.name,
            "spec": self.spec,
            "status": self.status,
            "url": self.url,
            "started_by": self.started_by,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "ended_by": self.ended_by,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
