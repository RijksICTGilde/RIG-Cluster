"""The registration endpoint of the Fundament fieldlab: a throwaway feature in one directory.

A ZAD installation on Fundament asks once for its own domain under the fieldlab zone, a
wildcard certificate for it and the details to sign in through the production Keycloak. The
secrets that takes (a TransIP token and the secret of a Keycloak client) stay here, on the
cluster of the platform.

Deliberately loose from the rest of OPI: it has nothing to do with projects, and after the
fieldlab it leaves with this directory, the one line in ``opi/server.py``, the settings, the
lego binary in the Dockerfile and a migration that drops its tables. See
``features/fundament-registratie.md``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opi.core.config import settings
from opi.fundament_registration.configuration import missing_settings
from opi.fundament_registration.router import fundament_router

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

__all__ = ["register_fundament_registration"]


def register_fundament_registration(app: FastAPI) -> None:
    """Add the registration endpoint, but only on a cluster that has the fieldlab code.

    Without ``FUNDAMENT_REGISTRATION_CODE`` the route does not exist at all, so a cluster
    that does not take part answers 404. A code without the rest of the settings is a
    misconfiguration that the endpoint answers 503 to, and that is worth a line at startup.
    """
    if not settings.FUNDAMENT_REGISTRATION_CODE:
        return
    missing = missing_settings()
    if missing:
        logger.warning("Fundament-registratie staat aan maar mist instellingen: %s", ", ".join(missing))
    else:
        logger.info("Fundament-registratie staat aan voor zone %s", settings.FUNDAMENT_REGISTRATION_ZONE)
    app.include_router(fundament_router, include_in_schema=False)
