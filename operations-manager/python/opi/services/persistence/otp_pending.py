"""ORM-model voor ``otp_pending_registrations`` (lopende registraties van een tweede factor).

Een rij is het bewijs dat /start draaide: alleen de vingerafdruk van het
AGE-versleutelde zaad (die in het formulier meereist), plus verloop. Geen zaad, geen
geheim in rust - de leverancier van een rij kan er niets mee zonder de bijbehorende
code uit de app van de gebruiker.

Server-side, omdat dit eerst in de sessiecookie zat: op een sessie met een volle
id_token liep die cookie daardoor over de ~4 KB die een browser geruisloos weglaat,
en de bevestiging deed naar stille niets (27-10-2026). Dezelfde aanpak als AMT/TAD:
een eigen tabel met TTL, plus een periodieke schoonmaak.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 (runtime-resolved by SQLAlchemy Mapped)

from sqlalchemy import DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from opi.core.db import Base


class OtpPendingRegistration(Base):
    """Een lopende OTP-registratie van dit e-mailadres."""

    __tablename__ = "otp_pending_registrations"

    email: Mapped[str] = mapped_column(Text, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
