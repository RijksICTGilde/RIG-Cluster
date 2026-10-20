"""Of een portaalsessie nog mag gelden: de absolute levensduur en het intrekken.

De HTTP-middleware en de handshake van de logs-WebSocket lopen hier beide langs, zodat
de twee ingangen dezelfde grens aanhouden.
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
    """De gebruiker uit de sessie, of None als de sessie niet meer mag gelden.

    Een sessie van voor deze controle draagt geen ``login_at`` en vervalt daarmee: na de
    uitrol logt iedereen een keer opnieuw in.
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
    """Gooi een geweigerde sessie ook in de browser weg.

    Alleen als er een gebruiker in stond: tijdens het inloggen draagt de sessie de
    OAuth-state van authlib, en die moet de omleiding naar ``/auth/callback`` overleven.
    """
    if session.get("user"):
        session.clear()
