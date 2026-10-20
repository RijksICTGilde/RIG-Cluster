"""Portaalsessies van een gebruiker intrekken via tabel en gebruikersgeheugen."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from opi.core.db import session_scope
from opi.services.persistence.session_revocations import SessionRevocation
from opi.services.user_service import get_user_service

logger = logging.getLogger(__name__)


async def revoke_sessions(email: str) -> datetime:
    """Maak elke lopende portaalsessie van dit e-mailadres ongeldig.

    Returns:
        Het tijdstip waarvoor sessies vervallen.
    """
    revoked_after = datetime.now(UTC)
    statement = insert(SessionRevocation).values(email=email.lower().strip(), revoked_after=revoked_after)
    statement = statement.on_conflict_do_update(
        index_elements=[SessionRevocation.email],
        set_={"revoked_after": revoked_after},
    )
    async with session_scope() as session:
        await session.execute(statement)

    get_user_service().set_sessions_revoked_after(email, revoked_after.timestamp())
    return revoked_after


async def load_revocations() -> int:
    """Lees de tabel in het geheugen van de ``UserService``. Returns: het aantal rijen."""
    async with session_scope() as session:
        rows = (await session.execute(select(SessionRevocation))).scalars().all()

    user_service = get_user_service()
    for row in rows:
        user_service.set_sessions_revoked_after(row.email, row.revoked_after.timestamp())
    logger.info(f"Loaded {len(rows)} session revocation(s) from database")
    return len(rows)
