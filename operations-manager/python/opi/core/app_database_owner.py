"""Geef OPI's eigen database aan de app-rol, vóór ``alembic upgrade head``.

De database ``operations_manager`` en haar tabellen waren van de superuser, omdat OPI
daar als ``postgres`` verbond. Sinds RC-410 opent OPI haar met een eigen rol zonder
superuser, en die rol moet dus bezitten wat de migraties wijzigen, inclusief
``alembic_version``.

Een eigen stap in ``operations-manager/docker-entrypoint.sh`` en niet in de applicatie
zelf: dit is het enige wat nog admin-credentials nodig heeft op deze database, en het moet
klaar zijn voordat Alembic als de app-rol begint. Idempotent, dus elke start mag hem
draaien.

Draaien: ``python -m opi.core.app_database_owner``
"""

from __future__ import annotations

import asyncio
import logging
import sys

from opi.connectors.postgres import PostgresExecutionError, PostgresValidationError, create_postgres_connector
from opi.core.config import settings

logger = logging.getLogger(__name__)


async def ensure_app_database_owner() -> dict[str, int]:
    """Verhuis de database en alles in ``public`` naar ``DATABASE_APP_USER``.

    Returns:
        Het aantal verhuisde objecten per soort; alles nul als het eigendom al klopte.
    """
    # DATABASE_HOST en niet de clusterconfiguratie: dit is dezelfde host waarmee OPI zijn
    # eigen database opent, en de entrypoint heeft die instelling al.
    connector = create_postgres_connector(
        settings.DATABASE_HOST,
        settings.DATABASE_ADMIN_NAME,
        settings.DATABASE_ADMIN_PASSWORD,
    )
    try:
        return await connector.transfer_public_objects(
            settings.DATABASE_NAME,
            new_owner=settings.DATABASE_APP_USER,
            current_owner=settings.DATABASE_ADMIN_NAME,
        )
    finally:
        await connector.close()


def main() -> int:
    """Afsluitcode voor de entrypoint: 0 bij succes, 1 als de overdracht faalde."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(ensure_app_database_owner())
    except PostgresExecutionError, PostgresValidationError:
        logger.exception("Eigendom van %s kon niet naar %s", settings.DATABASE_NAME, settings.DATABASE_APP_USER)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
