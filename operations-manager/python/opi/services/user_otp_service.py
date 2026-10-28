"""De tweede factor van een ZAD-gebruiker: registreren, verifieren en herstellen (RC-329).

Dit is de ENIGE plek die de ``otp_*``-kolommen op ``users`` leest of schrijft.
``tests/test_otp_kolommen_eigenaar.py`` bewaakt dat.

Niet te verwarren met de OTP op de gedeelde realm-admin van een projectrealm
(``features/keycloak-realm-admin-otp.md``): die beschermt de Keycloak-console en hangt
aan een service-account, deze hangt aan een persoon.
"""

from __future__ import annotations

import hmac
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select

from opi.core.config import settings
from opi.core.db import session_scope
from opi.services.persistence.otp_pending import OtpPendingRegistration
from opi.services.persistence.users import User
from opi.utils.age import decrypt_age_content, encrypt_age_content, get_global_private_key
from opi.utils.totp import build_otpauth_uri, generate_totp_secret, totp_base32, verify_totp

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Vijf fouten achter elkaar levert een blokkade van een kwartier. Een mislukte code is
# een beveiligingssignaal; dit remt het raden zonder iemand buiten te sluiten.
OTP_MAX_FAILED_ATTEMPTS = 5
OTP_LOCK_DURATION = timedelta(minutes=15)

#: De sleutel waarmee ``otp_secret`` is versleuteld. Een kolom ernaast houdt roteren
#: later mogelijk zonder te moeten raden waarmee een rij ooit is versleuteld.
OTP_KEY_VERSION = 1

#: Hoe lang een gestarte registratie zijn bevestiging accepteert.
PENDING_TTL = timedelta(minutes=10)

#: De kolommen die een reset leegmaakt. Ook de feature-documentatie noemt ze, voor wie
#: een gebruiker met de hand moet helpen.
OTP_COLUMNS_CLEARED_BY_RESET = (
    "otp_secret",
    "otp_key_version",
    "otp_confirmed_at",
    "otp_last_used_step",
    "otp_failed_attempts",
    "otp_locked_until",
)


class OtpVerification(StrEnum):
    """De uitkomst van :meth:`UserOtpService.verify`."""

    OK = "ok"
    INVALID = "invalid"
    LOCKED = "locked"
    NOT_REGISTERED = "not_registered"


@dataclass(frozen=True)
class OtpStatus:
    """Wat een pagina over de factor van een gebruiker mag weten."""

    registered: bool
    confirmed_at: datetime | None = None
    locked_until: datetime | None = None

    @property
    def locked(self) -> bool:
        return self.locked_until is not None and self.locked_until > datetime.now(UTC)


@dataclass(frozen=True)
class OtpRegistration:
    """Een nog niet bevestigde registratie.

    ``secret`` en ``otpauth_uri`` zijn voor de pagina die ze eenmalig toont;
    ``encrypted_secret`` is wat het formulier meedraagt tot de bevestiging.
    """

    secret: str
    encrypted_secret: str
    otpauth_uri: str
    base32_secret: str


class UserOtpService:
    """De tweede factor van een ZAD-gebruiker."""

    async def status(self, email: str) -> OtpStatus:
        """Of deze gebruiker een bevestigde factor heeft, en of hij geblokkeerd is."""
        async with session_scope() as session:
            row = await self._row(session, email)
            if row is None or row.otp_confirmed_at is None:
                return OtpStatus(registered=False)
            return OtpStatus(
                registered=True,
                confirmed_at=row.otp_confirmed_at,
                locked_until=row.otp_locked_until,
            )

    async def start_registration(self, email: str) -> OtpRegistration:
        """Maak een zaad voor een nieuwe factor. Schrijft niets.

        De database houdt alleen een BEVESTIGDE factor: zo vervangt een nieuwe
        registratie de oude pas als de gebruiker een geldige code van de nieuwe heeft
        ingevoerd. Waar het zaad tot dat moment staat: ``opi/web/router_otp.py``.
        """
        secret = generate_totp_secret()
        encrypted = await encrypt_age_content(secret, settings.SOPS_AGE_PUBLIC_KEY)
        logger.info("OTP-registratie gestart voor %s", email)
        return self._registration(email, secret, encrypted)

    async def resume_registration(self, email: str, encrypted_secret: str) -> OtpRegistration:
        """Dezelfde registratie opnieuw tonen, uit het meegestuurde versleutelde zaad."""
        secret = await decrypt_age_content(encrypted_secret, get_global_private_key())
        return self._registration(email, secret, encrypted_secret)

    async def confirm_registration(
        self,
        email: str,
        code: str,
        encrypted_secret: str,
        full_name: str = "",
    ) -> bool:
        """Bevestig een registratie met een code van het nieuwe zaad.

        ``encrypted_secret`` komt uit :meth:`start_registration`. Pas hier wordt de
        factor opgeslagen, en vervangt hij een eventuele vorige.
        """
        secret = await decrypt_age_content(encrypted_secret, get_global_private_key())
        step = verify_totp(secret, code)
        if step is None:
            logger.warning("OTP-bevestiging geweigerd voor %s: code niet geldig", email)
            return False

        async with session_scope() as session:
            row = await self._row_or_create(session, email, full_name)
            row.otp_secret = encrypted_secret
            row.otp_key_version = OTP_KEY_VERSION
            row.otp_confirmed_at = datetime.now(UTC)
            row.otp_last_used_step = step
            row.otp_failed_attempts = 0
            row.otp_locked_until = None
        logger.info("OTP bevestigd voor %s", email)
        return True

    async def verify(self, email: str, code: str) -> OtpVerification:
        """Toets een code van de bevestigde factor van deze gebruiker.

        Telt mislukte pogingen en blokkeert na :data:`OTP_MAX_FAILED_ATTEMPTS` fouten;
        een geslaagde poging zet de teller op nul.
        """
        async with session_scope() as session:
            row = await self._row(session, email)
            if row is None or row.otp_confirmed_at is None or not row.otp_secret:
                return OtpVerification.NOT_REGISTERED

            now = datetime.now(UTC)
            if row.otp_locked_until is not None and row.otp_locked_until > now:
                logger.warning("OTP geweigerd voor %s: geblokkeerd tot %s", email, row.otp_locked_until.isoformat())
                return OtpVerification.LOCKED

            secret = await decrypt_age_content(row.otp_secret, get_global_private_key())
            step = verify_totp(secret, code, last_used_step=row.otp_last_used_step)
            if step is None:
                row.otp_failed_attempts += 1
                if row.otp_failed_attempts >= OTP_MAX_FAILED_ATTEMPTS:
                    row.otp_locked_until = now + OTP_LOCK_DURATION
                    logger.warning(
                        "OTP geblokkeerd voor %s na %d mislukte pogingen, tot %s",
                        email,
                        row.otp_failed_attempts,
                        row.otp_locked_until.isoformat(),
                    )
                else:
                    logger.warning(
                        "OTP geweigerd voor %s: code niet geldig (poging %d van %d)",
                        email,
                        row.otp_failed_attempts,
                        OTP_MAX_FAILED_ATTEMPTS,
                    )
                return OtpVerification.INVALID

            row.otp_last_used_step = step
            row.otp_failed_attempts = 0
            row.otp_locked_until = None
            logger.info("OTP geaccepteerd voor %s", email)
            return OtpVerification.OK

    async def reset(self, email: str, by: str) -> bool:
        """Maak de factor van deze gebruiker leeg. Daarna registreert hij opnieuw."""
        async with session_scope() as session:
            row = await self._row(session, email)
            if row is None:
                return False
            row.otp_secret = None
            row.otp_key_version = OTP_KEY_VERSION
            row.otp_confirmed_at = None
            row.otp_last_used_step = None
            row.otp_failed_attempts = 0
            row.otp_locked_until = None
        logger.info("OTP gereset voor %s door %s", email, by)
        return True

    # ---- Lopende registraties (server-side, eigen tabel) -------------------------------------

    async def note_pending_registration(self, email: str, fingerprint: str) -> None:
        """Leg vast dat /start voor deze gebruiker draaide, met het zaad als afdruk.

        Tot 27-10-2026 zat die afdruk in de sessiecookie (zie router_otp.py). Nu in de
        tabel ``otp_pending_registrations``, met verloop, volgens het patroon van AMT/TAD.
        """
        now = datetime.now(UTC)
        async with session_scope() as session:
            row = (
                await session.execute(select(OtpPendingRegistration).where(OtpPendingRegistration.email == email))
            ).scalar_one_or_none()
            if row is None:
                session.add(
                    OtpPendingRegistration(
                        email=email, fingerprint=fingerprint, created_at=now, expires_at=now + PENDING_TTL
                    )
                )
            else:
                row.fingerprint = fingerprint
                row.created_at = now
                row.expires_at = now + PENDING_TTL

    async def consume_pending_registration(self, email: str, fingerprint: str) -> bool:
        """True als deze gebruiker een lopende registratie heeft die bij deze afdruk hoort.

        Een verlopen of afwezige registratie geeft False. De rij blijft staan voor een
        nieuwe poging (de code uit de app komt dertig seconden later weer); het schrijven
        van de factor zelf is de bevestiging.
        """
        async with session_scope() as session:
            row = (
                await session.execute(select(OtpPendingRegistration).where(OtpPendingRegistration.email == email))
            ).scalar_one_or_none()
            if row is None or row.expires_at <= datetime.now(UTC):
                return False
            return hmac.compare_digest(row.fingerprint, fingerprint)

    async def renew_expired_pending_registration(self, email: str, fingerprint: str) -> bool:
        """Geef een VERLOPEN registratie nog een termijn, als de afdruk bij de rij hoort.

        Alleen een rij van een echte /start kan dat: een eigen seed heeft een andere afdruk,
        dus de controle op de huidige 2-factor in /start blijft niet te omzeilen.
        """
        now = datetime.now(UTC)
        async with session_scope() as session:
            row = (
                await session.execute(select(OtpPendingRegistration).where(OtpPendingRegistration.email == email))
            ).scalar_one_or_none()
            if row is None or row.expires_at > now or not hmac.compare_digest(row.fingerprint, fingerprint):
                return False
            row.expires_at = now + PENDING_TTL
            return True

    async def discard_pending_registration(self, email: str) -> None:
        async with session_scope() as session:
            row = (
                await session.execute(select(OtpPendingRegistration).where(OtpPendingRegistration.email == email))
            ).scalar_one_or_none()
            if row is not None:
                await session.delete(row)

    async def purge_expired_pending_registrations(self) -> int:
        """Ruim verlopen registraties op; de cleanup-lus van de taakwerker roept dit."""
        async with session_scope() as session:
            result = await session.execute(
                delete(OtpPendingRegistration).where(OtpPendingRegistration.expires_at <= datetime.now(UTC))
            )
            return result.rowcount or 0

    @staticmethod
    def _registration(email: str, secret: str, encrypted_secret: str) -> OtpRegistration:
        """Wat de registratiepagina van een zaad nodig heeft."""
        return OtpRegistration(
            secret=secret,
            encrypted_secret=encrypted_secret,
            otpauth_uri=build_otpauth_uri(secret, email, f"ZAD {settings.CLUSTER_MANAGER}"),
            base32_secret=totp_base32(secret),
        )

    @staticmethod
    async def _row(session: AsyncSession, email: str) -> User | None:
        """De rij van dit adres, hoofdletterongevoelig.

        Een beheerder typt het adres zoals hij het kent, de inlogdienst levert het
        kleingeletterd. Zonder deze normalisatie krijgt dezelfde persoon twee rijen.
        """
        return (
            await session.execute(select(User).where(func.lower(User.email) == email.strip().lower()))
        ).scalar_one_or_none()

    @staticmethod
    async def _row_or_create(session: AsyncSession, email: str, full_name: str) -> User:
        """De rij van deze gebruiker, aangemaakt als hij er nog niet is.

        Een ingelogde gebruiker staat niet per se in ``users``: de allowlist komt ook uit
        ``ALLOWED_EMAILS``. Zonder rij zou zelfbediening alleen werken voor wie een
        beheerder eerder heeft ingevoerd.
        """
        row = await UserOtpService._row(session, email)
        if row is not None:
            return row
        row = User(id=uuid.uuid4(), email=email, full_name=full_name or email)
        session.add(row)
        await session.flush()
        logger.info("Gebruiker %s aangemaakt bij de OTP-registratie", email)
        return row
