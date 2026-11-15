"""ORM model for ``otp_herlogins``: a Keycloak re-login for setting up a second factor.

A row is a running or a succeeded re-login, holding the OIDC state (state, nonce, PKCE
verifier).

``login_at`` binds the row to the session the re-login started in, so another session of the
same user cannot use it. ``expires_at`` is first the time to log in at Keycloak, and becomes
the end of the window once the re-login succeeds.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 (runtime-resolved by SQLAlchemy Mapped)

from sqlalchemy import DateTime, Float, Text
from sqlalchemy.orm import Mapped, mapped_column

from opi.core.db import Base


class OtpHerlogin(Base):
    """One re-login of this email address, in this session."""

    __tablename__ = "otp_herlogins"

    state: Mapped[str] = mapped_column(Text, primary_key=True)
    email: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    login_at: Mapped[float] = mapped_column(Float, nullable=False)
    nonce: Mapped[str] = mapped_column(Text, nullable=False)
    code_verifier: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
