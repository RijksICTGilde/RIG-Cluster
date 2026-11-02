"""De databases van het platform zelf zijn alleen te openen door hun eigen rol.

Keycloak, Forgejo en de mailrelay staan op dezelfde gedeelde PostgreSQL als elk project, en
PostgreSQL geeft een nieuwe database CONNECT en TEMPORARY aan ``PUBLIC``. Elke projectrol
kan daarmee met die databases verbinden.

Waarom hier en niet declaratief: de ``Database``-resource van CNPG beschrijft naam,
eigenaar, encoding, locale, allowConnections, connectionLimit, schemas en extensies, en
geen rechten per rol. Er is dus geen CNPG-weg voor een REVOKE op een bestaande database.
Dit draait daarom bij de start van OPI, met dezelfde connectorhelper die een
projectdatabase afsluit, en volgt dus ook ``DATABASE_RESTRICT_PUBLIC_CONNECT``.
"""

from __future__ import annotations

import logging

from opi.connectors.postgres import PostgresExecutionError, create_postgres_connector
from opi.core.cluster_config import get_database_server
from opi.core.config import settings

logger = logging.getLogger(__name__)

#: Platformdatabase -> de rollen die expliciet CONNECT krijgen. Spiegelt
#: ``infrastructure/bootstrap/infrastructure/postgresql/database/base/databases.yaml``.
#: De database van OPI zelf komt uit ``settings.DATABASE_NAME`` en staat hier niet bij,
#: want haar rol is een instelling; zie ``restrict_platform_databases``.
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
    """Sluit elke platformdatabase af voor ``PUBLIC``.

    Niet-fataal: rechten op een platformdatabase mogen de boot niet blokkeren, dus een
    onbereikbare of weigerende server wordt gelogd en niet geworpen. De try staat binnen de
    lus, zodat een fout op de ene database de andere niet overslaat.
    """
    connector = create_postgres_connector(
        get_database_server(settings.CLUSTER_MANAGER),
        settings.DATABASE_ADMIN_NAME,
        settings.DATABASE_ADMIN_PASSWORD,
    )
    # De app-rol van OPI zelf hoort er expliciet bij: zodra DATABASE_RESTRICT_PUBLIC_CONNECT
    # op "on" staat neemt deze functie CONNECT van PUBLIC af, en zonder deze regel sluit
    # OPI zichzelf buiten zijn eigen database (RC-410).
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
