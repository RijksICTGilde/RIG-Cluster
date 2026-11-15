"""The platform's own databases can only be opened by their own role.

Keycloak, Forgejo and the mail relay sit on the same shared PostgreSQL as every project, and
PostgreSQL grants a new database CONNECT and TEMPORARY to ``PUBLIC``. Every project role can
therefore connect to those databases.

Why here and not declaratively: CNPG's ``Database`` resource describes name, owner, encoding,
locale, allowConnections, connectionLimit, schemas and extensions, and no privileges per role.
There is no CNPG route for a REVOKE on an existing database. This therefore runs at OPI's
start, with the same connector helper that closes off a project database, and so it follows
``DATABASE_RESTRICT_PUBLIC_CONNECT`` as well.
"""

from __future__ import annotations

import logging

from opi.connectors.postgres import PostgresExecutionError, create_postgres_connector
from opi.core.cluster_config import get_database_server
from opi.core.config import settings

logger = logging.getLogger(__name__)

#: Platform database -> the roles that get CONNECT explicitly. Mirrors
#: ``infrastructure/bootstrap/infrastructure/postgresql/database/base/databases.yaml``.
#: OPI's own database comes from ``settings.DATABASE_NAME`` and is not listed here, because its
#: role is a setting; see ``restrict_platform_databases``.
PLATFORM_DATABASE_ROLES: dict[str, tuple[str, ...]] = {
    "keycloak": ("keycloak",),
    "forgejo": ("forgejo",),
    "mailrelay": ("mailrelay",),
}

#: Read role of the nightly platform backup (managed role in ``cluster.yaml``).
#: ``pg_read_all_data`` does not grant CONNECT, so without this GRANT the backup dumps
#: nothing once the restriction is ``on``.
PLATFORM_BACKUP_ROLE = "platform_backup"


async def restrict_platform_databases() -> None:
    """Close off every platform database for ``PUBLIC``.

    Not fatal: privileges on a platform database may not block the boot, so an unreachable or
    refusing server is logged and not raised. The try sits inside the loop, so an error on one
    database does not skip the others.
    """
    connector = create_postgres_connector(
        get_database_server(settings.CLUSTER_MANAGER),
        settings.DATABASE_ADMIN_NAME,
        settings.DATABASE_ADMIN_PASSWORD,
    )
    # OPI's own app role belongs in here explicitly: once DATABASE_RESTRICT_PUBLIC_CONNECT is
    # "on" this function takes CONNECT away from PUBLIC, and without this line OPI locks itself
    # out of its own database (RC-410).
    databases = {**PLATFORM_DATABASE_ROLES, settings.DATABASE_NAME: (settings.DATABASE_APP_USER,)}
    try:
        for database, roles in databases.items():
            try:
                result = await connector.restrict_database_to_roles(database, [*roles, PLATFORM_BACKUP_ROLE])
                logger.info(f"Platformdatabase {database} afsluiten voor PUBLIC: {result['status']}")
            except PostgresExecutionError:
                logger.exception(f"Platformdatabase {database} afsluiten voor PUBLIC is niet gelukt")
    finally:
        await connector.close()
