"""Gedeelde hulp voor de groep ``postgres_live``: OPI tegen een echte PostgreSQL.

`task test-postgres` start de image die het platform pint en zet ``POSTGRES_LIVE_DSN``. De
toetsen praten met de connector zoals productie dat doet, en lezen de uitkomst terug uit de
catalogus met een eigen verbinding: een returnwaarde zegt niets over wat de server opsloeg.

De connector accepteert alleen een in-cluster servicenaam en alleen poort 5432. In plaats
van die grendels uit te zetten wijst ``resolve_service_name`` de naam naar het adres uit het
DSN, zodat ze onveranderd meedraaien.
"""

from __future__ import annotations

import os
import shutil
import socket
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlparse

import asyncpg
import pytest
from opi.connectors.postgres import PostgresConnector
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.manager.database_manager import DatabaseManager
from tests.integration.live import POSTGRES

if TYPE_CHECKING:
    from collections.abc import Iterator

#: De naam waaronder OPI de gedeelde server in het cluster ziet.
SERVICE_NAME = "rig-db-rw"

#: Vorm die ``_validate_password`` accepteert (lengte en soorten tekens), en een
#: plaatshouder zodat de secret-scan hem als zodanig herkent.
PASSWORD = "example-Wachtwoord-1234"
OTHER_PASSWORD = "example-Wachtwoord-5678"


def dsn() -> str:
    """Het DSN van de wegwerpserver, of een skip (in CI een fout)."""
    return POSTGRES.omgeving("POSTGRES_LIVE_DSN")


def assert_local() -> None:
    """Weiger tegen iets anders dan een wegwerpserver op deze machine te draaien.

    Ook in ``drop`` en ``drop_by_prefix``: een toets die geen connector vraagt komt niet
    langs de fixture, en dan zou alleen de volgorde van de fixtures de grendel aanroepen.

    Geeft het DSN ongeparsed door: ``LiveGroup.assert_local`` parset zelf. Een eerste parse
    hier zou een IPv6-loopback (``::1``) breken, want die herkent ``_hostname`` alleen uit
    een URL met ``://``, niet uit een los adres.
    """
    POSTGRES.assert_local(dsn())


def pg_client_tools() -> bool:
    """``psql`` en ``pg_dump``: de kloonpijplijn roept ze aan als losse processen."""
    return all(shutil.which(tool) for tool in ("psql", "pg_dump"))


def require_pg_client_tools() -> None:
    """Zonder psql en pg_dump geen kloontoets, en in CI is dat een fout en geen skip."""
    if pg_client_tools():
        return
    melding = "psql en pg_dump zijn nodig: de kloonpijplijn roept ze aan als losse processen"
    if os.environ.get(POSTGRES.required_variable):
        pytest.fail(melding)
    pytest.skip(melding)


async def admin_connection(database: str = "postgres") -> asyncpg.Connection:
    """Een eigen verbinding voor het terugleren, los van de connector onder toets."""
    parsed = urlparse(dsn())
    return await asyncpg.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        user=parsed.username,
        password=parsed.password,
        database=database,
    )


async def connection_as(username: str, password: str, database: str) -> asyncpg.Connection:
    parsed = urlparse(dsn())
    return await asyncpg.connect(
        host=parsed.hostname, port=parsed.port or 5432, user=username, password=password, database=database
    )


def admin_credentials() -> tuple[str, str]:
    parsed = urlparse(dsn())
    return parsed.username or "postgres", parsed.password or ""


def connector() -> PostgresConnector:
    """De connector zoals OPI hem bouwt: op de servicenaam, met de superuser."""
    username, password = admin_credentials()
    return PostgresConnector(host=SERVICE_NAME, admin_username=username, admin_password=password)


def project_manager_stub(project_name: str) -> MagicMock:
    """Een ProjectManager die alleen antwoordt wat de DatabaseManager hem vraagt.

    Kubernetes heeft nog geen live groep, dus het secret-kanaal is hier een dubbel: wat de
    manager als secret opslaat, geeft ``get_secret`` daarna terug, zoals ArgoCD het secret
    op het cluster zet. Zonder dat ziet elke verwerking "geen secret" en zet de manager een
    nieuw wachtwoord, wat productie niet doet. De projectbestand-lezer is wel de echte,
    want hij beslist of een deployment PostgreSQL gebruikt.
    """
    stub = MagicMock()
    stub.get_name = AsyncMock(return_value=project_name)
    stub.get_progress_manager.return_value = None
    stub._project_file_handler = ProjectFileHandler()
    stub.stored_secrets = {}

    async def get_secret(secret_name: str, namespace: str) -> dict[str, str] | None:
        for (deployment_name, _kind), secret in stub.stored_secrets.items():
            if type(secret).get_secret_name(deployment_name) == secret_name:
                return secret.to_k8s_secret_data()
        return None

    stub._kubectl_connector.get_secret = get_secret
    stub._add_secret_to_create = lambda deployment, naam, secret: stub.stored_secrets.__setitem__(
        (deployment, naam), secret
    )
    return stub


def manager(postgres: PostgresConnector, project_manager: MagicMock) -> DatabaseManager:
    """De DatabaseManager op een echte server.

    Wat hij met PostgreSQL doet gaat volledig door de connector hieronder; alleen de
    ProjectManager eromheen is een stub.
    """
    username, password = admin_credentials()
    built = DatabaseManager(
        project_manager=project_manager,
        db_host=SERVICE_NAME,
        admin_username=username,
        admin_password=password,
    )
    built._postgres_connector = postgres
    return built


@pytest.fixture
def resolve_service_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Laat ``SERVICE_NAME`` naar de wegwerpserver wijzen, zonder de guards aan te raken.

    ``PGHOSTADDR`` voor pg_dump en psql, een ``getaddrinfo``-patch voor asyncpg.
    """
    parsed = urlparse(dsn())
    address = socket.gethostbyname(parsed.hostname or "localhost")
    monkeypatch.setenv("PGHOSTADDR", address)

    echt = socket.getaddrinfo

    def resolve(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        return echt(address if host == SERVICE_NAME else host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


@pytest.fixture
async def live_connector(resolve_service_name: None) -> Iterator[PostgresConnector]:
    assert_local()
    built = connector()
    try:
        yield built
    finally:
        await built.close()


async def role_exists(name: str) -> bool:
    conn = await admin_connection()
    try:
        return bool(await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", name))
    finally:
        await conn.close()


async def database_exists(name: str) -> bool:
    conn = await admin_connection()
    try:
        return bool(await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name))
    finally:
        await conn.close()


async def drop_by_prefix(prefix: str) -> None:
    """Haal elke database en rol met dit voorvoegsel weg, ook die van een afgebroken run."""
    assert_local()
    conn = await admin_connection()
    try:
        for name in await conn.fetch("SELECT datname FROM pg_database WHERE datname LIKE $1", f"{prefix}%"):
            await conn.execute(f'DROP DATABASE IF EXISTS "{name["datname"]}" WITH (FORCE)')
        for role in await conn.fetch("SELECT rolname FROM pg_roles WHERE rolname LIKE $1", f"{prefix}%"):
            await conn.execute(f'DROP ROLE IF EXISTS "{role["rolname"]}"')
    finally:
        await conn.close()


async def drop(*, databases: tuple[str, ...] = (), roles: tuple[str, ...] = ()) -> None:
    """Haal weg wat een toets maakte, ook als hij faalde."""
    assert_local()
    conn = await admin_connection()
    try:
        for database in databases:
            await conn.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        for role in roles:
            await conn.execute(f'DROP ROLE IF EXISTS "{role}"')
    finally:
        await conn.close()
