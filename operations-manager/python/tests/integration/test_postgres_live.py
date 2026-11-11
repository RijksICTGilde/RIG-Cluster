"""OPI's PostgreSQL-connector tegen een echte PostgreSQL in de gepinde versie.

Elke methode van ``PostgresConnector`` die ``opi/`` aanroept, draait hier ten minste een
keer, en na elke schrijfactie leest een eigen verbinding de catalogus terug: een
returnwaarde van de connector zegt niets over wat de server opsloeg. Dat is het gat waar
Keycloak 26.8.1 door kwam, en PostgreSQL heeft hetzelfde gat: een GRANT die niets doet geeft
geen fout.

Draai hem met ``task test-postgres``; zonder ``POSTGRES_LIVE_DSN`` slaat de groep zichzelf
over. ``tests/test_live_group_coverage.py`` bewaakt dat er geen methode ontbreekt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg
import pytest
from opi.connectors.postgres import PostgresConnector, PostgresExecutionError
from opi.core.config import settings
from tests.integration.postgres_live import (
    OTHER_PASSWORD,
    PASSWORD,
    SERVICE_NAME,
    admin_connection,
    admin_credentials,
    connection_as,
    database_exists,
    drop,
    live_connector,
    require_pg_client_tools,
    resolve_service_name,
    role_exists,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = [pytest.mark.requires_infra, pytest.mark.postgres_live]

# De fixtures komen uit de gedeelde module; pytest vindt ze via deze namen.
__all__ = ["live_connector", "resolve_service_name"]

#: Elk object dat deze groep maakt begint hiermee, zodat een mens ziet wat van de toets is.
PREFIX = "zadlive"

OWNER = f"{PREFIX}_owner"
OTHER = f"{PREFIX}_other"
READER = f"{PREFIX}_reader"
DATABASE = f"{PREFIX}_app"
SCHEMA = f"{PREFIX}_app"


@pytest.fixture
async def schoon() -> AsyncIterator[None]:
    """Weg met alles van deze groep, voor en na elke toets."""
    await _drop_everything()
    yield
    await _drop_everything()


async def _drop_everything() -> None:
    await drop(databases=(DATABASE, f"{DATABASE}_kloon", f"{DATABASE}_bron"), roles=(OWNER, OTHER, READER))


async def _fetchval(database: str, sql: str, *args: object) -> object:
    conn = await admin_connection(database)
    try:
        return await conn.fetchval(sql, *args)
    finally:
        await conn.close()


async def test_create_user_stores_a_login_role_with_its_connection_limit(
    live_connector: PostgresConnector, schoon: None
) -> None:
    result = await live_connector.create_user(OWNER, PASSWORD, ["CREATEDB"], connection_limit=5)
    assert result["status"] == "created"

    row = await _fetchval(
        "postgres",
        "SELECT rolcanlogin || ',' || rolconnlimit || ',' || rolcreatedb FROM pg_roles WHERE rolname = $1",
        OWNER,
    )
    assert row == "true,5,true"

    # Dezelfde aanroep nog eens: de verwerking van een project draait elke keer opnieuw.
    assert (await live_connector.create_user(OWNER, PASSWORD, ["CREATEDB"], connection_limit=5))["status"] == "exists"

    assert OWNER in {user["username"] for user in await live_connector.list_users()}


async def test_set_connection_limit_only_alters_on_a_difference(
    live_connector: PostgresConnector, schoon: None
) -> None:
    await live_connector.create_user(OWNER, PASSWORD, connection_limit=5)

    raised = await live_connector.set_connection_limit(OWNER, 11)
    assert (raised["status"], raised["previous"], raised["connection_limit"]) == ("updated", 5, 11)
    assert await _fetchval("postgres", "SELECT rolconnlimit FROM pg_roles WHERE rolname = $1", OWNER) == 11

    assert (await live_connector.set_connection_limit(OWNER, 11))["status"] == "unchanged"
    assert (await live_connector.set_connection_limit(f"{PREFIX}_bestaat_niet", 11))["status"] == "not_found"


async def test_update_user_password_lets_the_role_in_with_the_new_one(
    live_connector: PostgresConnector, schoon: None
) -> None:
    await live_connector.create_user(OWNER, PASSWORD, connection_limit=5)
    await live_connector.create_database(DATABASE, OWNER)

    assert (await live_connector.update_user_password(OWNER, OTHER_PASSWORD))["status"] == "success"

    conn = await connection_as(OWNER, OTHER_PASSWORD, DATABASE)
    await conn.close()
    with pytest.raises(asyncpg.InvalidPasswordError):
        await connection_as(OWNER, PASSWORD, DATABASE)


async def test_update_user_privileges_applies_every_named_privilege(
    live_connector: PostgresConnector, schoon: None
) -> None:
    await live_connector.create_user(OWNER, PASSWORD, connection_limit=5)
    assert await _fetchval("postgres", "SELECT rolcreatedb FROM pg_roles WHERE rolname = $1", OWNER) is False

    result = await live_connector.update_user_privileges(OWNER, ["CREATEDB", "CREATEROLE"])
    assert result["status"] == "success"
    assert (
        await _fetchval("postgres", "SELECT rolcreatedb AND rolcreaterole FROM pg_roles WHERE rolname = $1", OWNER)
        is True
    )

    assert (await live_connector.update_user_privileges(f"{PREFIX}_bestaat_niet", ["CREATEDB"]))[
        "status"
    ] == "not_found"


async def test_create_database_makes_the_named_role_its_owner(live_connector: PostgresConnector, schoon: None) -> None:
    await live_connector.create_user(OWNER, PASSWORD, connection_limit=5)
    assert (await live_connector.create_database(DATABASE, OWNER))["status"] == "created"

    assert (
        await _fetchval(
            "postgres",
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = $1",
            DATABASE,
        )
        == OWNER
    )
    assert DATABASE in {database["name"] for database in await live_connector.list_databases()}
    assert (await live_connector.create_database(DATABASE, OWNER))["status"] == "exists"


async def test_restrict_database_to_roles_locks_out_every_other_role(
    live_connector: PostgresConnector, schoon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """De stand ``on``: PUBLIC verliest CONNECT en alleen de genoemde rollen komen binnen.

    PostgreSQL geeft een nieuwe database CONNECT aan PUBLIC, dus zonder deze REVOKE leest
    elke projectrol de catalogus van elke andere database (RC-410).
    """
    monkeypatch.setattr(settings, "DATABASE_RESTRICT_PUBLIC_CONNECT", "on")
    await live_connector.create_user(OWNER, PASSWORD, connection_limit=5)
    await live_connector.create_user(OTHER, OTHER_PASSWORD, connection_limit=5)
    await live_connector.create_database(DATABASE, OWNER)

    # Voor de REVOKE komt de vreemde rol binnen; dat is de toestand die dit repareert.
    conn = await connection_as(OTHER, OTHER_PASSWORD, DATABASE)
    await conn.close()

    result = await live_connector.restrict_database_to_roles(DATABASE, [OWNER])
    assert result["status"] == "restricted", result

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await connection_as(OTHER, OTHER_PASSWORD, DATABASE)
    eigen = await connection_as(OWNER, PASSWORD, DATABASE)
    await eigen.close()

    assert (await live_connector.restrict_database_to_roles(f"{PREFIX}_bestaat_niet", [OWNER]))["status"] == "not_found"


async def test_a_schema_and_a_search_path_put_the_role_in_its_own_schema(
    live_connector: PostgresConnector, schoon: None
) -> None:
    await live_connector.create_user(OWNER, PASSWORD, connection_limit=5)
    await live_connector.create_database(DATABASE, OWNER)
    assert (await live_connector.create_schema(SCHEMA, DATABASE, OWNER))["status"] == "created"
    await live_connector.set_role_search_path(OWNER, DATABASE, [SCHEMA])

    assert (
        await _fetchval(
            DATABASE,
            "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = $1",
            SCHEMA,
        )
        == OWNER
    )

    # Het gevolg, niet de aanroep: een tabel zonder schemanaam landt in het eigen schema.
    conn = await connection_as(OWNER, PASSWORD, DATABASE)
    try:
        await conn.execute("CREATE TABLE eigen (id int)")
        assert await conn.fetchval("SELECT current_schema()") == SCHEMA
    finally:
        await conn.close()

    assert {schema["schema_name"] for schema in await live_connector.list_schemas(DATABASE)} >= {SCHEMA, "public"}
    assert (await live_connector.create_schema(SCHEMA, DATABASE, OWNER))["status"] == "exists"


async def test_grant_readonly_on_schema_allows_select_and_refuses_insert(
    live_connector: PostgresConnector, schoon: None
) -> None:
    await live_connector.create_user(OWNER, PASSWORD, connection_limit=5)
    await live_connector.create_user(READER, OTHER_PASSWORD, connection_limit=5)
    await live_connector.create_database(DATABASE, OWNER)
    await live_connector.create_schema(SCHEMA, DATABASE, OWNER)

    conn = await connection_as(OWNER, PASSWORD, DATABASE)
    try:
        await conn.execute(f'CREATE TABLE "{SCHEMA}".regels (id int)')
        await conn.execute(f'INSERT INTO "{SCHEMA}".regels VALUES (1)')
    finally:
        await conn.close()

    assert (await live_connector.grant_readonly_on_schema(DATABASE, SCHEMA, READER))["status"] == "granted"

    reader = await connection_as(READER, OTHER_PASSWORD, DATABASE)
    try:
        assert await reader.fetchval(f'SELECT count(*) FROM "{SCHEMA}".regels') == 1
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await reader.execute(f'INSERT INTO "{SCHEMA}".regels VALUES (2)')
    finally:
        await reader.close()

    with pytest.raises(PostgresExecutionError):
        await live_connector.grant_readonly_on_schema(DATABASE, f"{PREFIX}_geen_schema", READER)


async def test_describe_schemas_separates_an_extension_schema_from_the_rest(
    live_connector: PostgresConnector, schoon: None
) -> None:
    """Wat een kloon mee moet nemen, en wat niet: dat onderscheid komt hiervandaan (RC-360)."""
    await live_connector.create_user(OWNER, PASSWORD, connection_limit=5)
    await live_connector.create_database(DATABASE, OWNER)
    await live_connector.create_schema(SCHEMA, DATABASE, OWNER)

    assert (await live_connector.execute_init_sql(DATABASE, ["CREATE SCHEMA ext_ns"]))["status"] == "success"
    await live_connector.execute_init_sql(DATABASE, ["CREATE EXTENSION pgcrypto SCHEMA ext_ns"])
    conn = await admin_connection(DATABASE)
    try:
        await conn.execute("CREATE TABLE public.blijft_staan (id int)")
    finally:
        await conn.close()

    inventory = await live_connector.describe_schemas(DATABASE)
    assert SCHEMA in inventory.schemas
    assert inventory.extension_namespaces == {"ext_ns"}
    assert inventory.public_objects == ["blijft_staan"]


async def test_database_has_user_data_turns_true_with_the_first_table(
    live_connector: PostgresConnector, schoon: None
) -> None:
    await live_connector.create_user(OWNER, PASSWORD, connection_limit=5)
    await live_connector.create_database(DATABASE, OWNER)
    assert await live_connector.database_has_user_data(DATABASE) is False

    await live_connector.execute_init_sql(DATABASE, ["CREATE TABLE public.eerste (id int)"])
    assert await live_connector.database_has_user_data(DATABASE) is True


async def test_transfer_public_objects_moves_only_what_the_current_owner_has(
    live_connector: PostgresConnector, schoon: None
) -> None:
    """De migraties van OPI draaien als de app-rol en moeten elke bestaande tabel bezitten (RC-410)."""
    admin_username, _ = admin_credentials()
    await live_connector.create_user(OWNER, PASSWORD, connection_limit=5)
    await live_connector.create_database(DATABASE, admin_username)
    await live_connector.execute_init_sql(DATABASE, ["CREATE TABLE public.alembic_version (version_num text)"])

    moved = await live_connector.transfer_public_objects(DATABASE, OWNER, admin_username)
    assert moved["relation"] >= 1, moved
    assert (
        await _fetchval(DATABASE, "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname = 'alembic_version'")
        == OWNER
    )

    # Nog een keer: niets van de oude eigenaar over, dus niets te verhuizen.
    assert not any((await live_connector.transfer_public_objects(DATABASE, OWNER, admin_username)).values())


async def test_count_active_connections_counts_an_open_connection(
    live_connector: PostgresConnector, schoon: None
) -> None:
    await live_connector.create_user(OWNER, PASSWORD, connection_limit=5)
    await live_connector.create_database(DATABASE, OWNER)
    assert await live_connector.count_active_connections(DATABASE) == 0

    conn = await connection_as(OWNER, PASSWORD, DATABASE)
    try:
        assert await live_connector.count_active_connections(DATABASE) == 1
    finally:
        await conn.close()


async def test_deleting_schema_database_and_user_leaves_nothing_behind(
    live_connector: PostgresConnector, schoon: None
) -> None:
    await live_connector.create_user(OWNER, PASSWORD, connection_limit=5)
    await live_connector.create_database(DATABASE, OWNER)
    await live_connector.create_schema(SCHEMA, DATABASE, OWNER)
    await live_connector.execute_init_sql(DATABASE, [f'CREATE TABLE "{SCHEMA}".regels (id int)'])

    assert (await live_connector.delete_schema(SCHEMA, DATABASE, cascade=True))["status"] == "deleted"
    assert await _fetchval(DATABASE, "SELECT 1 FROM pg_namespace WHERE nspname = $1", SCHEMA) is None

    assert (await live_connector.delete_database(DATABASE))["status"] == "deleted"
    assert await database_exists(DATABASE) is False

    assert (await live_connector.delete_user(OWNER))["status"] == "deleted"
    assert await role_exists(OWNER) is False

    assert (await live_connector.delete_schema(SCHEMA, "postgres"))["status"] == "not_found"
    assert (await live_connector.delete_database(DATABASE))["status"] == "not_found"
    assert (await live_connector.delete_user(OWNER))["status"] == "not_found"


async def test_clone_schema_copies_the_rows_to_the_target_owner(
    live_connector: PostgresConnector, schoon: None
) -> None:
    require_pg_client_tools()
    source = f"{DATABASE}_bron"
    target = f"{DATABASE}_kloon"
    await live_connector.create_user(OWNER, PASSWORD, connection_limit=5)
    await live_connector.create_database(source, OWNER)
    await live_connector.create_schema(SCHEMA, source, OWNER)
    await live_connector.execute_init_sql(
        source, [f'CREATE TABLE "{SCHEMA}".regels (id int)', f'INSERT INTO "{SCHEMA}".regels VALUES (1), (2)']
    )
    await live_connector.create_database(target, OWNER)

    assert PostgresConnector.validate_cloneable_schema_name(SCHEMA) == SCHEMA
    result = await live_connector.clone_schema(
        source_database=source,
        target_database=target,
        source_schema=SCHEMA,
        target_schema=SCHEMA,
        target_owner=OWNER,
        target_owner_password=PASSWORD,
    )
    assert result["status"] == "success", result

    assert await _fetchval(target, f'SELECT count(*) FROM "{SCHEMA}".regels') == 2
    assert (
        await _fetchval(
            target,
            "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname = 'regels'",
        )
        == OWNER
    )


async def test_clone_schema_from_external_copies_between_two_databases(
    live_connector: PostgresConnector, schoon: None
) -> None:
    """Dezelfde pijplijn met expliciete bronverbinding, zoals een kloon uit een ander cluster."""
    require_pg_client_tools()
    source = f"{DATABASE}_bron"
    target = f"{DATABASE}_kloon"
    admin_username, admin_password = admin_credentials()
    await live_connector.create_user(OWNER, PASSWORD, connection_limit=5)
    await live_connector.create_database(source, OWNER)
    await live_connector.create_schema(SCHEMA, source, OWNER)
    await live_connector.execute_init_sql(source, [f'CREATE TABLE "{SCHEMA}".regels (id int)'])
    await live_connector.create_database(target, OWNER)

    result = await live_connector.clone_schema_from_external(
        source_host=SERVICE_NAME,
        source_port=5432,
        source_username=admin_username,
        source_password=admin_password,
        source_database=source,
        source_schema=SCHEMA,
        target_database=target,
        target_schema=SCHEMA,
        target_owner=OWNER,
        target_owner_password=PASSWORD,
    )
    assert result["status"] == "success", result
    assert await _fetchval(target, f'SELECT count(*) FROM "{SCHEMA}".regels') == 0
