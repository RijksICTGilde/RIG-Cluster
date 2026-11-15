"""ORM model for ``otp_pending_registrations`` (running second-factor registrations).

A row is the proof that /start ran: only the fingerprint of the AGE-encrypted seed (which
travels along in the form), plus expiry. No seed and so no secret at rest: whoever holds a
row can do nothing with it without the matching code from the user's app.

The table carries a TTL and is swept periodically.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 (runtime-resolved by SQLAlchemy Mapped)

from sqlalchemy import DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from opi.core.db import Base


class OtpPendingRegistration(Base):
    """One running OTP registration of this email address."""

    __tablename__ = "otp_pending_registrations"

    email: Mapped[str] = mapped_column(Text, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
