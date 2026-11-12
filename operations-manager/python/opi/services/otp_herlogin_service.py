"""The Keycloak herlogin that precedes setting up a second factor.

Without this gate, anyone with an open session on an unlocked computer sets up their own
second factor under someone else's name. How the portal routes ask for it is in
``opi/web/router_otp.py``.

"herlogin" is the domain term here, because it names the ``otp_herlogins`` table.
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

#: How long a started herlogin may take: the time to log in at Keycloak.
HERLOGIN_TTL = timedelta(minutes=10)

#: How long a succeeded herlogin covers setting up a second factor. Does not slide.
HERLOGIN_WINDOW = timedelta(minutes=5)

#: How old Keycloak's ``auth_time`` may be. With ``max_age=0`` it is from just now.
AUTH_TIME_MAX_SECONDS = 60


@dataclass(frozen=True)
class Herlogin:
    """The OIDC values of a running herlogin."""

    state: str
    nonce: str
    code_verifier: str


def herlogin_is_valid(claims: Any, user: dict[str, Any], *, now: float | None = None) -> bool:  # noqa: ANN401  # the JWT claims
    """Whether these id_token claims are a fresh login of THIS signed-in user."""
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
    """Start, approve and check a herlogin. All of it in the ``otp_herlogins`` table."""

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
        """The running herlogin at this state, or None when it is unknown, expired or someone else's."""
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
                row.expires_at = now + HERLOGIN_WINDOW

    async def is_fresh(self, email: str, login_at: float | None) -> bool:
        """Whether this session logged in again less than :data:`HERLOGIN_WINDOW` ago."""
        if login_at is None:
            return False
        async with session_scope() as session:
            found = (
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
            return found is not None

    async def purge_expired(self) -> int:
        async with session_scope() as session:
            result = await session.execute(delete(OtpHerlogin).where(OtpHerlogin.expires_at <= datetime.now(UTC)))
            return result.rowcount or 0
