"""Whether a portal session may still count: the absolute lifetime and revocation.

Both the HTTP middleware and the handshake of the logs WebSocket pass through here, so the
two entrances hold the same boundary.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from opi.core.config import settings
from opi.services.user_service import get_user_service

if TYPE_CHECKING:
    from collections.abc import MutableMapping

logger = logging.getLogger(__name__)


def valid_session_user(session: MutableMapping[str, Any] | None) -> dict[str, Any] | None:
    """The user from the session, or None when the session may no longer count.

    A session from before this check carries no ``login_at`` and therefore expires: after
    the rollout everyone logs in once more.
    """
    if not session:
        return None

    user = session.get("user")
    if not user:
        return None

    email = str(user.get("email", "onbekend"))
    login_at = user.get("login_at")
    if not isinstance(login_at, int | float):
        logger.info(f"Sessie geweigerd voor {email}: geen inlogtijdstip in de sessie")
        return None

    if time.time() - login_at > settings.SESSION_ABSOLUTE_MAX_SECONDS:
        logger.info(f"Sessie geweigerd voor {email}: voorbij de absolute levensduur")
        return None

    if get_user_service().are_sessions_revoked(email, login_at):
        logger.info(f"Sessie geweigerd voor {email}: de sessies van deze gebruiker zijn ingetrokken")
        return None

    return user


def drop_refused_session(session: MutableMapping[str, Any]) -> None:
    """Throw a refused session away in the browser as well.

    Only when it held a user: while logging in, the session carries authlib's OAuth state,
    and that has to survive the redirect to ``/auth/callback``.
    """
    if session.get("user"):
        session.clear()
