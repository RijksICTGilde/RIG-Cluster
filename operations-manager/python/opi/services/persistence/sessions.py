"""ORM model for ``sessions`` (migration 011): one row is one portal session.

``expires_at`` is the sliding inactivity window. The only reader and writer is
``opi/services/session_store.py``.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 (runtime-resolved by SQLAlchemy Mapped)
from typing import Any

from sqlalchemy import DateTime, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from opi.core.db import Base


class PortalSession(Base):
    """The contents of one portal session, behind the id in the session cookie."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
