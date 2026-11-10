"""Revoke a user's portal sessions, through the table and the in-memory user state."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from opi.core.db import session_scope
from opi.services.persistence.session_revocations import SessionRevocation
from opi.services.session_store import get_session_store
from opi.services.user_service import get_user_service

logger = logging.getLogger(__name__)


async def revoke_sessions(email: str) -> datetime:
    """Invalidate every running portal session of this email address, and return the moment
    before which they expire.

    Two guards: the revocation row refuses cookies from before that moment, and the
    server-side session rows are deleted as well, which is a hard refusal on the next
    request.
    """
    revoked_after = datetime.now(UTC)
    statement = insert(SessionRevocation).values(email=email.lower().strip(), revoked_after=revoked_after)
    statement = statement.on_conflict_do_update(
        index_elements=[SessionRevocation.email],
        set_={"revoked_after": revoked_after},
    )
    async with session_scope() as session:
        await session.execute(statement)

    removed = await get_session_store().delete_by_email(email)
    if removed:
        logger.info("Also removed %d server-side session row(s) of %s", removed, email)

    get_user_service().set_sessions_revoked_after(email, revoked_after.timestamp())
    return revoked_after


async def load_revocations() -> int:
    """Read the table into the ``UserService``'s memory; returns the number of rows."""
    async with session_scope() as session:
        rows = (await session.execute(select(SessionRevocation))).scalars().all()

    user_service = get_user_service()
    for row in rows:
        user_service.set_sessions_revoked_after(row.email, row.revoked_after.timestamp())
    logger.info(f"Loaded {len(rows)} session revocation(s) from database")
    return len(rows)
