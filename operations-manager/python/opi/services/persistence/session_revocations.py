"""ORM-model voor ``session_revocations`` (RC-287, migratie 008).

Geschreven bij uitloggen en door de platformbeheerdersactie, eenmaal bij opstarten
gelezen in het geheugen van ``UserService``.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 (runtime-resolved by SQLAlchemy Mapped)

from sqlalchemy import DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from opi.core.db import Base


class SessionRevocation(Base):
    """Portaalsessies van dit e-mailadres die voor ``revoked_after`` begonnen zijn, gelden niet."""

    __tablename__ = "session_revocations"

    email: Mapped[str] = mapped_column(Text, primary_key=True)
    revoked_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
