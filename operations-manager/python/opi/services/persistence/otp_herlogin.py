"""ORM-model voor ``otp_herlogins``: een herlogin bij Keycloak voor het inrichten van een 2-factor.

Een rij is een lopende of geslaagde herlogin. De OIDC-state (state, nonce, PKCE-verifier)
staat hier en niet in de sessiecookie: authlib zet die normaal in de sessie, en op een sessie
met een volle id_token loopt die cookie dan over de ~4 KB die een browser geruisloos weglaat.

``login_at`` bindt de rij aan de sessie waarin de herlogin begon, zodat een andere sessie van
dezelfde gebruiker er niets aan heeft. ``expires_at`` is eerst de tijd om bij Keycloak in te
loggen en wordt na een geslaagde herlogin het einde van het venster.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 (runtime-resolved by SQLAlchemy Mapped)

from sqlalchemy import DateTime, Float, Text
from sqlalchemy.orm import Mapped, mapped_column

from opi.core.db import Base


class OtpHerlogin(Base):
    """Een herlogin van dit e-mailadres, in deze sessie."""

    __tablename__ = "otp_herlogins"

    state: Mapped[str] = mapped_column(Text, primary_key=True)
    email: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    login_at: Mapped[float] = mapped_column(Float, nullable=False)
    nonce: Mapped[str] = mapped_column(Text, nullable=False)
    code_verifier: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
