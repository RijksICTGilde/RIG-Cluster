"""The step-up state of a bearer token, the API's counterpart of ``step_up_at`` in a session."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError

from opi.core.db import session_scope
from opi.core.step_up import STEP_UP_WINDOW
from opi.services.persistence.api_step_ups import ApiStepUp
from opi.services.project_leden import hash_van

logger = logging.getLogger(__name__)


def sso_session_credential(sid: str) -> str:
    """The step-up key of an SSO token: its login session, which survives a token refresh."""
    return f"sid:{sid}"


def personal_token_credential(token: str) -> str:
    """The step-up key of a personal token, which has no login session: its stored hash."""
    return f"token:{hash_van(token)}"


class ApiStepUpService:
    """Record, read and end the step-up of an API credential."""

    async def record(self, credential: str, email: str) -> datetime:
        """Start a new window for this credential and return when it ends."""
        now = datetime.now(UTC)
        async with session_scope() as session:
            await session.execute(delete(ApiStepUp).where(ApiStepUp.stepped_up_at <= now - STEP_UP_WINDOW))
            await session.merge(ApiStepUp(credential=credential, email=email.lower(), stepped_up_at=now))
        return now + STEP_UP_WINDOW

    async def is_fresh(self, credential: str, email: str) -> bool:
        """Whether this credential gave a valid code within the window.

        A database failure reads as no: the caller then has the rights of an ordinary user.
        """
        try:
            async with session_scope() as session:
                row = await session.get(ApiStepUp, credential)
        except SQLAlchemyError:
            logger.warning("Step-up state unreadable for %s, treated as absent", email, exc_info=True)
            return False
        return row is not None and row.email == email.lower() and datetime.now(UTC) < row.stepped_up_at + STEP_UP_WINDOW

    async def clear(self, credential: str) -> None:
        async with session_scope() as session:
            await session.execute(delete(ApiStepUp).where(ApiStepUp.credential == credential))
