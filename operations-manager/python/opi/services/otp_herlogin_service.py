"""De herlogin bij Keycloak die het inrichten van een 2-factor vooraf gaat.

Zonder deze poort richt iedereen met een open sessie op een ontgrendelde computer een eigen
2-factor in onder de naam van een ander. Hoe de portaalroutes erom vragen staat in
``opi/web/router_otp.py``.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select

from opi.core.db import session_scope
from opi.services.persistence.otp_herlogin import OtpHerlogin

logger = logging.getLogger(__name__)

#: Hoe lang een gestarte herlogin mag duren: de tijd om bij Keycloak in te loggen.
HERLOGIN_TTL = timedelta(minutes=10)

#: Hoe lang een geslaagde herlogin het inrichten van een 2-factor dekt. Schuift niet mee.
HERLOGIN_VENSTER = timedelta(minutes=5)

#: Hoe oud de ``auth_time`` van Keycloak mag zijn. Met ``max_age=0`` is hij van zojuist.
AUTH_TIME_MAX_SECONDS = 60


@dataclass(frozen=True)
class Herlogin:
    """De OIDC-waarden van een lopende herlogin."""

    state: str
    nonce: str
    code_verifier: str


def herlogin_klopt(claims: Any, user: dict[str, Any], *, now: float | None = None) -> bool:
    """Of deze id_token-claims een verse login van DEZE ingelogde gebruiker zijn."""
    moment = time.time() if now is None else now
    email = str(user.get("email", "")).lower()
    if claims.get("email_verified") is not True:
        logger.warning("Herlogin geweigerd voor %s: e-mailadres niet geverifieerd", email)
        return False
    if claims.get("sub") != user.get("sub") or str(claims.get("email", "")).lower() != email:
        logger.warning("Herlogin geweigerd voor %s: een ander account dan de ingelogde gebruiker", email)
        return False
    auth_time = claims.get("auth_time")
    if not isinstance(auth_time, int | float):
        logger.warning("Herlogin geweigerd voor %s: de id_token mist auth_time (mapper in de client?)", email)
        return False
    if not -AUTH_TIME_MAX_SECONDS <= moment - auth_time <= AUTH_TIME_MAX_SECONDS:
        logger.warning("Herlogin geweigerd voor %s: auth_time is niet vers", email)
        return False
    return True


class OtpHerloginService:
    """Begin, keur goed en toets een herlogin. Alles in de tabel ``otp_herlogins``."""

    async def begin(self, email: str, login_at: float) -> Herlogin:
        herlogin = Herlogin(
            state=secrets.token_urlsafe(32),
            nonce=secrets.token_urlsafe(24),
            code_verifier=secrets.token_urlsafe(48),
        )
        now = datetime.now(UTC)
        async with session_scope() as session:
            session.add(
                OtpHerlogin(
                    state=herlogin.state,
                    email=email,
                    login_at=login_at,
                    nonce=herlogin.nonce,
                    code_verifier=herlogin.code_verifier,
                    created_at=now,
                    expires_at=now + HERLOGIN_TTL,
                )
            )
        return herlogin

    async def take(self, state: str, email: str, login_at: float) -> Herlogin | None:
        """De lopende herlogin bij deze state, of None als hij onbekend, verlopen of van een ander is."""
        async with session_scope() as session:
            row = await session.get(OtpHerlogin, state)
            if (
                row is None
                or row.verified_at is not None
                or row.expires_at <= datetime.now(UTC)
                or row.email != email
                or row.login_at != login_at
            ):
                return None
            return Herlogin(state=row.state, nonce=row.nonce, code_verifier=row.code_verifier)

    async def mark_verified(self, state: str) -> None:
        now = datetime.now(UTC)
        async with session_scope() as session:
            row = await session.get(OtpHerlogin, state)
            if row is not None:
                row.verified_at = now
                row.expires_at = now + HERLOGIN_VENSTER

    async def is_fresh(self, email: str, login_at: float | None) -> bool:
        """Of deze sessie minder dan :data:`HERLOGIN_VENSTER` geleden opnieuw inlogde."""
        if login_at is None:
            return False
        async with session_scope() as session:
            gevonden = (
                await session.execute(
                    select(OtpHerlogin.state)
                    .where(
                        OtpHerlogin.email == email,
                        OtpHerlogin.login_at == login_at,
                        OtpHerlogin.verified_at.is_not(None),
                        OtpHerlogin.expires_at > datetime.now(UTC),
                    )
                    .limit(1)
                )
            ).first()
            return gevonden is not None

    async def purge_expired(self) -> int:
        async with session_scope() as session:
            result = await session.execute(delete(OtpHerlogin).where(OtpHerlogin.expires_at <= datetime.now(UTC)))
            return result.rowcount or 0
