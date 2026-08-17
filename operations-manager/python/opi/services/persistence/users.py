"""ORM model for the ``users`` table (platform user admin, RC-5 persistence).

Schema-as-code mirror of ``USERS_TABLE_SQL`` (opi/core/user_schema.py, still used by the
Alembic baseline). Alembic autogenerate targets this model (see migrations/env.py). The
repository lives in ``opi/services/user_admin_service.py``.
"""

from __future__ import annotations

import uuid  # noqa: TC003 (runtime-resolved by SQLAlchemy Mapped)
from datetime import datetime  # noqa: TC003 (runtime-resolved by SQLAlchemy Mapped)

from sqlalchemy import DateTime, Index, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from opi.core.db import Base


class User(Base):
    """A platform user (email + full name). Matches ``USERS_TABLE_SQL`` exactly."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)

    __table_args__ = (Index("idx_users_email", "email"),)

    def to_dict(self) -> dict:
        """Serializable dict with the shape the callers expect (str id, ISO timestamps)."""
        return {
            "id": str(self.id),
            "email": self.email,
            "full_name": self.full_name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
