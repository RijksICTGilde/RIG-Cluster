"""ORM model for the ``marked_for_deletion`` table (deferred resource deletion, RC-5).

Schema-as-code mirror of ``MARKED_FOR_DELETION_TABLE_SQL``. The repository lives in
``opi/services/marked_for_deletion_service.py``.
"""

from __future__ import annotations

import uuid  # noqa: TC003 (runtime-resolved by SQLAlchemy Mapped)
from datetime import datetime  # noqa: TC003
from typing import Any

from sqlalchemy import DateTime, Index, String, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from opi.core.db import Base


class MarkedForDeletion(Base):
    """A resource marked for deferred deletion. Matches ``MARKED_FOR_DELETION_TABLE_SQL``."""

    __tablename__ = "marked_for_deletion"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_name: Mapped[str] = mapped_column(String(255), nullable=False)
    project_name: Mapped[str] = mapped_column(String(255), nullable=False)
    deployment_name: Mapped[str] = mapped_column(String(255), nullable=False)
    cluster: Mapped[str] = mapped_column(String(100), nullable=False)
    marked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
    # 'metadata' is reserved on DeclarativeBase, so the attribute is renamed but the
    # column stays 'metadata'. Nullable to match the DDL (the '{}' server default means
    # a real NULL never actually occurs).
    resource_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, server_default=text("'{}'::jsonb"))

    __table_args__ = (
        Index("idx_marked_for_deletion_project", "project_name"),
        Index("idx_marked_for_deletion_marked_at", "marked_at"),
        Index("idx_marked_for_deletion_unique", "resource_type", "resource_name", "cluster", unique=True),
    )

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict with the shape the callers expect (str id, ISO marked_at,
        ``metadata`` as a dict)."""
        return {
            "id": str(self.id),
            "resource_type": self.resource_type,
            "resource_name": self.resource_name,
            "project_name": self.project_name,
            "deployment_name": self.deployment_name,
            "cluster": self.cluster,
            "marked_at": self.marked_at.isoformat() if self.marked_at else None,
            "metadata": self.resource_metadata,
        }
