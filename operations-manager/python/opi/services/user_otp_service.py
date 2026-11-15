"""A ZAD user's second factor: register it, verify it and recover it.

The ONLY place that reads or writes the ``otp_*`` columns on ``users``;
``tests/test_otp_kolommen_eigenaar.py`` guards that.

Not to be confused with the OTP on the shared realm admin of a project realm
(``features/keycloak-realm-admin-otp.md``): that one protects the Keycloak console and hangs
off a service account, this one hangs off a person.
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

# A failed code is a security signal; this slows guessing down without locking anyone out.
OTP_MAX_FAILED_ATTEMPTS = 5
OTP_LOCK_DURATION = timedelta(minutes=15)

#: The key ``otp_secret`` is encrypted with. A column next to it keeps rotating possible
#: later, without having to guess what a row was once encrypted with.
OTP_KEY_VERSION = 1

#: How long a started registration accepts its confirmation.
PENDING_TTL = timedelta(minutes=10)

#: The absolute lifetime of a started registration, renewals included. Without this limit an
#: abandoned registration page stays confirmable.
PENDING_MAX_AGE = 2 * PENDING_TTL

#: The columns a reset empties. The feature documentation names them too, for whoever has to
#: help a user by hand.
OTP_COLUMNS_CLEARED_BY_RESET = (
    "otp_secret",
    "otp_key_version",
    "otp_confirmed_at",
    "otp_last_used_step",
    "otp_failed_attempts",
    "otp_locked_until",
)


class OtpVerification(StrEnum):
    """The outcome of :meth:`UserOtpService.verify`."""

    OK = "ok"
    INVALID = "invalid"
    LOCKED = "locked"
    NOT_REGISTERED = "not_registered"


@dataclass(frozen=True)
class OtpStatus:
    """What a page may know about a user's factor."""

    registered: bool
    confirmed_at: datetime | None = None
    locked_until: datetime | None = None

    @property
    def locked(self) -> bool:
        return self.locked_until is not None and self.locked_until > datetime.now(UTC)


@dataclass(frozen=True)
class OtpRegistration:
    """A registration that has not been confirmed yet.

    ``secret`` and ``otpauth_uri`` are for the page that shows them once; ``encrypted_secret``
    is what the form carries along until the confirmation.
    """

    secret: str
    encrypted_secret: str
    otpauth_uri: str
    base32_secret: str


class UserOtpService:
    """A ZAD user's second factor."""

    async def status(self, email: str) -> OtpStatus:
        """Whether this user has a confirmed factor, and whether it is locked."""
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
        """Make a seed for a new factor. Writes nothing.

        The database holds only a CONFIRMED factor, so a new registration replaces the old one
        only once the user has entered a valid code from the new one. Where the seed sits until
        that moment: ``opi/web/router_otp.py``.
        """
        secret = generate_totp_secret()
        encrypted = await encrypt_age_content(secret, settings.SOPS_AGE_PUBLIC_KEY)
        logger.info("OTP-registratie gestart voor %s", email)
        return self._registration(email, secret, encrypted)

    async def resume_registration(self, email: str, encrypted_secret: str) -> OtpRegistration:
        """Show the same registration again, out of the encrypted seed that came along."""
        secret = await decrypt_age_content(encrypted_secret, get_global_private_key())
        return self._registration(email, secret, encrypted_secret)

    async def confirm_registration(
        self,
        email: str,
        code: str,
        encrypted_secret: str,
        full_name: str = "",
    ) -> bool:
        """Confirm a registration with a code from the new seed.

        ``encrypted_secret`` comes from :meth:`start_registration`. Only here is the factor
        stored, and only here does it replace any previous one.
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
        """Check a code from this user's confirmed factor.

        Counts failed attempts and locks after :data:`OTP_MAX_FAILED_ATTEMPTS` of them; a
        successful attempt sets the counter back to zero.
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
        """Empty this user's factor. They register again afterwards."""
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

    async def note_pending_registration(self, email: str, fingerprint: str) -> None:
        """Record that /start ran for this user, with the seed as a fingerprint.

        In the ``otp_pending_registrations`` table, with an expiry.
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
        """True when this user has a running registration that belongs to this fingerprint.

        An expired or absent registration gives False. The row STAYS for another attempt (the
        code from the app comes round again thirty seconds later); writing the factor itself is
        the confirmation.
        """
        async with session_scope() as session:
            row = (
                await session.execute(select(OtpPendingRegistration).where(OtpPendingRegistration.email == email))
            ).scalar_one_or_none()
            if row is None or row.expires_at <= datetime.now(UTC):
                return False
            return hmac.compare_digest(row.fingerprint, fingerprint)

    async def renew_expired_pending_registration(self, email: str, fingerprint: str) -> bool:
        """Give an EXPIRED registration another term, when the fingerprint belongs to the row.

        Only a row from a real /start can do that: an own seed has a different fingerprint, so
        the check on the current second factor in /start stays impossible to bypass.

        The renewal never runs past :data:`PENDING_MAX_AGE` after the /start. Renewing without
        a limit keeps an abandoned registration page confirmable, which is exactly the open
        session on the unlocked computer the herlogin on /start guards against.
        """
        now = datetime.now(UTC)
        async with session_scope() as session:
            row = (
                await session.execute(select(OtpPendingRegistration).where(OtpPendingRegistration.email == email))
            ).scalar_one_or_none()
            if row is None or row.expires_at > now or not hmac.compare_digest(row.fingerprint, fingerprint):
                return False
            latest = row.created_at + PENDING_MAX_AGE
            if latest <= now:
                return False
            row.expires_at = min(now + PENDING_TTL, latest)
            return True

    async def discard_pending_registration(self, email: str) -> None:
        async with session_scope() as session:
            row = (
                await session.execute(select(OtpPendingRegistration).where(OtpPendingRegistration.email == email))
            ).scalar_one_or_none()
            if row is not None:
                await session.delete(row)

    async def purge_expired_pending_registrations(self) -> int:
        """Sweep expired registrations; the task worker's cleanup loop calls this."""
        async with session_scope() as session:
            result = await session.execute(
                delete(OtpPendingRegistration).where(OtpPendingRegistration.expires_at <= datetime.now(UTC))
            )
            return result.rowcount or 0

    @staticmethod
    def _registration(email: str, secret: str, encrypted_secret: str) -> OtpRegistration:
        """What the registration page needs from a seed."""
        return OtpRegistration(
            secret=secret,
            encrypted_secret=encrypted_secret,
            otpauth_uri=build_otpauth_uri(secret, email, f"ZAD {settings.CLUSTER_MANAGER}"),
            base32_secret=totp_base32(secret),
        )

    @staticmethod
    async def _row(session: AsyncSession, email: str) -> User | None:
        """The row of this address, case-insensitively.

        An admin types the address as they know it, the login service delivers it lower case.
        Without this normalisation the same person gets two rows.
        """
        return (
            await session.execute(select(User).where(func.lower(User.email) == email.strip().lower()))
        ).scalar_one_or_none()

    @staticmethod
    async def _row_or_create(session: AsyncSession, email: str, full_name: str) -> User:
        """This user's row, created when it is not there yet.

        A signed-in user is not necessarily in ``users``: the allowlist also comes from
        ``ALLOWED_EMAILS``. Without a row, self-service would only work for whoever an admin
        entered beforehand.
        """
        row = await UserOtpService._row(session, email)
        if row is not None:
            return row
        row = User(id=uuid.uuid4(), email=email, full_name=full_name or email)
        session.add(row)
        await session.flush()
        logger.info("Gebruiker %s aangemaakt bij de OTP-registratie", email)
        return row
