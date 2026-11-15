"""Hand OPI's own database to the app role, before ``alembic upgrade head``.

OPI opens its own database with a role without superuser, so that role has to own what
the migrations change, ``alembic_version`` included.

Runs from ``operations-manager/docker-entrypoint.sh`` and not from the application: it
needs admin credentials on this database, and it has to be done before Alembic starts as
the app role. Idempotent, so every start may run it.

To run: ``python -m opi.runtime.startup.app_database_owner``
"""

from __future__ import annotations

import asyncio
import logging
import sys

from opi.connectors.postgres import PostgresExecutionError, PostgresValidationError, create_postgres_connector
from opi.core.config import settings

logger = logging.getLogger(__name__)


async def ensure_app_database_owner() -> dict[str, int]:
    """Move the database and everything in ``public`` to ``DATABASE_APP_USER``.

    Returns how many objects moved per kind; all zero when ownership was already right.
    """
    # DATABASE_HOST and not the cluster configuration: this is the same host OPI opens its own
    # database with, and the entrypoint already holds that setting.
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
    """Exit code for the entrypoint: 0 on success, 1 when the transfer failed."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(ensure_app_database_owner())
    except PostgresExecutionError, PostgresValidationError:
        logger.exception("Eigendom van %s kon niet naar %s", settings.DATABASE_NAME, settings.DATABASE_APP_USER)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
