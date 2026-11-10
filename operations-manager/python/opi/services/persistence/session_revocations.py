"""ORM model for ``session_revocations`` (migration 008).

Written on logout and by the platform-admin action, read once at startup into the memory of
``UserService``.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 (runtime-resolved by SQLAlchemy Mapped)

from sqlalchemy import DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from opi.core.db import Base


class SessionRevocation(Base):
    """Portal sessions of this email address that started before ``revoked_after`` do not count."""

    __tablename__ = "session_revocations"

    email: Mapped[str] = mapped_column(Text, primary_key=True)
    revoked_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
