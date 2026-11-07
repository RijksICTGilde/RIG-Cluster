"""Een kloon neemt elk schema van de brondatabase mee (RC-360).

Een project dat zijn eigen schema's aanmaakt (OpenFSC zet zijn tabellen in ``peers`` en
``contracts``) kreeg in een preview alleen het standaardschema en de door ZAD
gedeclareerde extra schema's. De migratietabel kwam wel mee, dus de migratietool zag
werk dat al gedaan leek en de applicatie viel om op een tabel die er niet was. Een kloon
die over zichzelf liegt is erger dan een lege.

Wat hier tegen een echte PostgreSQL 17 gemeten wordt:

* de schema's die de applicatie zelf maakte komen onder hun eigen naam mee, het
  standaardschema en de ZAD-extra schema's onder hun doelnaam;
* foreign keys tussen die schema's in BEIDE richtingen, en een view eroverheen, werken in
  het doel. Dat is de reden voor de ene ``pg_dump`` met een ``-n`` per schema: voor een
  cyclus bestaat geen volgorde waarin een run per schema slaagt;
* de schema's en hun tabellen zijn eigendom van de doelrol, en die rol kan er schrijven;
* ``public`` van de bron gaat niet mee, en de objecten die daar staan worden bij naam
  genoemd;
* het schema waar een extensie in woont gaat niet mee: postInitSQL maakt dat in het doel;
* een schemanaam die het platform niet accepteert laat de kloon falen voordat er iets in
  het doel staat;
* een kloon die halverwege de restore faalt laat in een bestaande doeldatabase geen van
  zijn eigen schema's achter, en laat een schema dat er al stond staan.

Deze module hoort bij de groep ``postgres_live`` en draait mee met ``task test-postgres``,
tegen de image die het platform pint. Hij staat niet in ``tests/integration/`` omdat hij er
al stond; de opstelling (het DSN, de naamresolutie) komt uit
``tests/integration/postgres_live.py``, zodat er een opstartroute is en niet twee.

De server MOET op poort 5432 staan: de code onder test accepteert geen andere poort
(``PostgresConnector._ALLOWED_PG_PORTS``). De host geeft de test als in-cluster
servicenaam mee, zoals in productie, en laat die naam naar het DSN-adres wijzen met
``PGHOSTADDR`` (libpq) en een ``socket.getaddrinfo``-patch (asyncpg). Zo draaien de
egress-guards van ``_execute_pgdump_clone`` onveranderd mee.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from urllib.parse import urlparse

import asyncpg
import pytest
from opi.connectors.postgres import PostgresConnector, PostgresValidationError
from opi.manager.database_manager import DatabaseManager
from tests.integration.postgres_live import SERVICE_NAME, dsn, require_pg_client_tools, resolve_service_name

pytestmark = [pytest.mark.requires_infra, pytest.mark.postgres_live]

__all__ = ["resolve_service_name"]

SOURCE_DB = "rc360_src"
SOURCE_SCHEMA = SOURCE_DB
SOURCE_EXTRA = f"{SOURCE_DB}_rapportage"
TARGET_DB = "rc360_dst"
TARGET_SCHEMA = TARGET_DB
TARGET_EXTRA = f"{TARGET_DB}_rapportage"
TARGET_OWNER = "rc360_dst_owner"
TARGET_OWNER_PASSWORD = "rc360-owner-pw"

ALL_TEST_DATABASES = (SOURCE_DB, TARGET_DB, "rc360_badname_src", "rc360_badname_dst", "rc360_half_dst")


def _dsn() -> str:
    return dsn()


@pytest.fixture(autouse=True)
def _de_groep() -> None:
    """Zonder de opstelling slaat deze module over, en in CI faalt hij in plaats daarvan."""
    require_pg_client_tools()
    dsn()


async def _admin() -> asyncpg.Connection:
    return await asyncpg.connect(_dsn())


async def _connect(database: str, user: str | None = None, password: str | None = None) -> asyncpg.Connection:
    parsed = urlparse(_dsn())
    return await asyncpg.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        user=user or parsed.username,
        password=password or parsed.password,
        database=database,
    )


async def _drop_test_state() -> None:
    admin = await _admin()
    try:
        for name in ALL_TEST_DATABASES:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.execute(f'DROP ROLE IF EXISTS "{TARGET_OWNER}"')
    finally:
        await admin.close()


async def _create_target_owner() -> None:
    admin = await _admin()
    try:
        await admin.execute(f"CREATE ROLE \"{TARGET_OWNER}\" LOGIN PASSWORD '{TARGET_OWNER_PASSWORD}'")
    finally:
        await admin.close()


async def _build_source(database: str, *, app_schema: str = "peers", with_broken_default: bool = False) -> None:
    """Een brondatabase zoals een deployment die achterlaat.

    Het standaardschema met een migratietabel en een extensiefunctie, een ZAD-extra
    schema, twee schema's van de applicatie die naar elkaar verwijzen, een view eroverheen,
    een extensie in zijn eigen schema, en een tabel in ``public``.
    """
    admin = await _admin()
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
    finally:
        await admin.close()

    conn = await _connect(database)
    try:
        await conn.execute(f'CREATE SCHEMA "{SOURCE_SCHEMA}"')
        await conn.execute(f'CREATE EXTENSION "uuid-ossp" SCHEMA "{SOURCE_SCHEMA}"')
        await conn.execute("CREATE SCHEMA ext_ns")
        await conn.execute("CREATE EXTENSION pgcrypto SCHEMA ext_ns")
        await conn.execute(f'CREATE SCHEMA "{SOURCE_EXTRA}"')
        await conn.execute(f'CREATE SCHEMA "{app_schema}"')
        await conn.execute("CREATE SCHEMA contracts")

        await conn.execute(f'CREATE TABLE "{SOURCE_SCHEMA}".schema_migrations (version int primary key, dirty bool)')
        await conn.execute(f'INSERT INTO "{SOURCE_SCHEMA}".schema_migrations VALUES (25, false)')
        await conn.execute(
            f'CREATE TABLE "{SOURCE_SCHEMA}".orders ('
            f'  id uuid primary key default "{SOURCE_SCHEMA}".uuid_generate_v4(), label text)'
        )
        await conn.execute(f"INSERT INTO \"{SOURCE_SCHEMA}\".orders (label) VALUES ('een'), ('twee')")

        await conn.execute(f'CREATE TABLE "{SOURCE_EXTRA}".totalen (id int primary key, bedrag int)')
        await conn.execute(f'INSERT INTO "{SOURCE_EXTRA}".totalen VALUES (1, 100), (2, 200)')

        broken = ", nonce bytea default ext_ns.gen_random_bytes(4)" if with_broken_default else ""
        await conn.execute(f'CREATE TABLE "{app_schema}".peers (id int primary key, naam text{broken})')
        await conn.execute("CREATE TABLE contracts.contracts (id int primary key, peer_id int not null)")
        await conn.execute(f'ALTER TABLE "{app_schema}".peers ADD COLUMN contract_id int')
        await conn.execute(
            f'ALTER TABLE "{app_schema}".peers ADD CONSTRAINT peers_contract_fk '
            "FOREIGN KEY (contract_id) REFERENCES contracts.contracts (id)"
        )
        await conn.execute(
            "ALTER TABLE contracts.contracts ADD CONSTRAINT contracts_peer_fk "
            f'FOREIGN KEY (peer_id) REFERENCES "{app_schema}".peers (id)'
        )
        await conn.execute(f"INSERT INTO \"{app_schema}\".peers (id, naam) VALUES (1, 'alfa'), (2, 'beta')")
        await conn.execute("INSERT INTO contracts.contracts VALUES (10, 1), (11, 2)")
        await conn.execute(f'UPDATE "{app_schema}".peers SET contract_id = 10 WHERE id = 1')

        await conn.execute(
            f'ALTER TABLE "{SOURCE_SCHEMA}".orders ADD COLUMN peer_id int REFERENCES "{app_schema}".peers (id)'
        )
        await conn.execute(f'CREATE VIEW "{SOURCE_SCHEMA}".peer_namen AS SELECT id, naam FROM "{app_schema}".peers')

        await conn.execute("CREATE TABLE public.oud_logboek (id int primary key)")
    finally:
        await conn.close()


@pytest.fixture
async def schoon() -> Any:
    await _drop_test_state()
    yield
    await _drop_test_state()


def _connector() -> PostgresConnector:
    parsed = urlparse(_dsn())
    return PostgresConnector(
        host=SERVICE_NAME,
        admin_username=parsed.username or "postgres",
        admin_password=parsed.password or "",
    )


def _manager(connector: PostgresConnector) -> DatabaseManager:
    manager = DatabaseManager(
        project_manager=MagicMock(get_progress_manager=lambda: None),
        db_host=SERVICE_NAME,
        admin_username=connector._admin_username,
        admin_password=connector._admin_password,
    )
    manager._postgres_connector = connector
    return manager


async def _clone(connector: PostgresConnector, manager: DatabaseManager, target_database: str) -> list[tuple[str, str]]:
    """De kloon zoals de provisioning hem doet: eerst ontdekken, dan één clone_schema."""
    platform_pairs = [(SOURCE_EXTRA, TARGET_EXTRA)]
    discovered = await manager._resolve_application_schemas(
        source_database=SOURCE_DB,
        platform_sources={SOURCE_SCHEMA, SOURCE_EXTRA},
        platform_targets={TARGET_SCHEMA, TARGET_EXTRA},
    )
    result = await connector.clone_schema(
        source_database=SOURCE_DB,
        target_database=target_database,
        source_schema=SOURCE_SCHEMA,
        target_schema=TARGET_SCHEMA,
        target_owner=TARGET_OWNER,
        target_owner_password=TARGET_OWNER_PASSWORD,
        additional_schemas=platform_pairs + discovered,
    )
    assert result["status"] == "success"
    return discovered


# --- de kloon ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_een_kloon_neemt_de_schemas_van_de_applicatie_mee(resolve_service_name: None, schoon: None) -> None:
    await _build_source(SOURCE_DB)
    await _create_target_owner()
    connector = _connector()
    manager = _manager(connector)

    discovered = await _clone(connector, manager, TARGET_DB)
    await connector.close()

    assert discovered == [("contracts", "contracts"), ("peers", "peers")]

    doel = await _connect(TARGET_DB)
    try:
        schemas = {
            row["nspname"]
            for row in await doel.fetch(
                "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema'"
            )
        }
        assert {TARGET_SCHEMA, TARGET_EXTRA, "peers", "contracts", "public"} == schemas
        assert "ext_ns" not in schemas

        assert await doel.fetchval(f'SELECT version FROM "{TARGET_SCHEMA}".schema_migrations') == 25
        assert await doel.fetchval(f'SELECT count(*) FROM "{TARGET_SCHEMA}".orders') == 2
        assert await doel.fetchval(f'SELECT count(*) FROM "{TARGET_EXTRA}".totalen') == 2
        assert await doel.fetchval("SELECT count(*) FROM peers.peers") == 2
        assert await doel.fetchval("SELECT count(*) FROM contracts.contracts") == 2

        # De view over het schema van de applicatie werkt.
        assert await doel.fetchval(f'SELECT naam FROM "{TARGET_SCHEMA}".peer_namen WHERE id = 1') == "alfa"

        # Beide richtingen van de foreign key staan er, en handhaven.
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await doel.execute("INSERT INTO contracts.contracts VALUES (99, 404)")
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await doel.execute("INSERT INTO peers.peers (id, naam, contract_id) VALUES (9, 'x', 404)")
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await doel.execute(f"INSERT INTO \"{TARGET_SCHEMA}\".orders (label, peer_id) VALUES ('drie', 404)")

        # public van het doel is onaangeroerd: de tabel van de bron staat er niet.
        assert (
            await doel.fetchval(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON c.relnamespace = n.oid "
                "WHERE n.nspname = 'public' AND c.relkind = 'r'"
            )
            == 0
        )

        eigenaren = {
            row["nspname"]: row["eigenaar"]
            for row in await doel.fetch(
                "SELECT n.nspname, r.rolname AS eigenaar FROM pg_namespace n JOIN pg_roles r ON n.nspowner = r.oid "
                "WHERE n.nspname = ANY($1::text[])",
                [TARGET_SCHEMA, TARGET_EXTRA, "peers", "contracts"],
            )
        }
        assert set(eigenaren.values()) == {TARGET_OWNER}

        tabel_eigenaren = {
            row["eigenaar"]
            for row in await doel.fetch(
                "SELECT r.rolname AS eigenaar FROM pg_class c JOIN pg_namespace n ON c.relnamespace = n.oid "
                "JOIN pg_roles r ON c.relowner = r.oid "
                "WHERE n.nspname = ANY($1::text[]) AND c.relkind IN ('r', 'v')",
                [TARGET_SCHEMA, TARGET_EXTRA, "peers", "contracts"],
            )
        }
        assert tabel_eigenaren == {TARGET_OWNER}
    finally:
        await doel.close()

    # De doelrol kan in het schema van de applicatie schrijven en er een tabel maken.
    als_rol = await _connect(TARGET_DB, user=TARGET_OWNER, password=TARGET_OWNER_PASSWORD)
    try:
        await als_rol.execute("INSERT INTO peers.peers (id, naam) VALUES (3, 'gamma')")
        await als_rol.execute("CREATE TABLE peers.migraties (version int primary key)")
    finally:
        await als_rol.close()

    # De bron is onveranderd.
    bron = await _connect(SOURCE_DB)
    try:
        assert await bron.fetchval("SELECT count(*) FROM peers.peers") == 2
        assert await bron.fetchval("SELECT count(*) FROM public.oud_logboek") == 0
        assert await bron.fetchval("SELECT count(*) FROM pg_namespace WHERE nspname = $1", TARGET_SCHEMA) == 0
    finally:
        await bron.close()


@pytest.mark.asyncio
async def test_de_tabel_in_public_van_de_bron_wordt_bij_naam_genoemd(resolve_service_name: None, schoon: None) -> None:
    await _build_source(SOURCE_DB)
    connector = _connector()

    inventaris = await connector.describe_schemas(SOURCE_DB)
    await connector.close()

    assert "oud_logboek" in inventaris.public_objects
    assert inventaris.extension_namespaces == {SOURCE_SCHEMA, "ext_ns"}


# --- faalpaden -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_een_naam_die_het_platform_niet_accepteert_laat_de_kloon_falen(
    resolve_service_name: None, schoon: None
) -> None:
    """Falen voordat er iets in het doel staat: een halve kloon is het probleem zelf."""
    await _build_source(SOURCE_DB, app_schema="Peers")
    connector = _connector()
    manager = _manager(connector)

    with pytest.raises(PostgresValidationError, match="Peers"):
        await manager._resolve_application_schemas(
            source_database=SOURCE_DB,
            platform_sources={SOURCE_SCHEMA, SOURCE_EXTRA},
            platform_targets={TARGET_SCHEMA, TARGET_EXTRA},
        )

    admin = await _admin()
    try:
        assert await admin.fetchval("SELECT count(*) FROM pg_database WHERE datname = $1", TARGET_DB) == 0
    finally:
        await admin.close()
    await connector.close()


@pytest.mark.asyncio
async def test_een_kloon_die_halverwege_faalt_laat_geen_van_zijn_schemas_achter(
    resolve_service_name: None, schoon: None
) -> None:
    """De opruiming raakt alleen de schema's van de kloon zelf, in een levende database."""
    await _build_source(SOURCE_DB, with_broken_default=True)
    await _create_target_owner()
    connector = _connector()
    manager = _manager(connector)

    # Een doeldatabase die er al stond, met een schema van een andere sessie erin.
    admin = await _admin()
    try:
        await admin.execute(f'CREATE DATABASE "rc360_half_dst" OWNER "{TARGET_OWNER}"')
    finally:
        await admin.close()
    bestaand = await _connect("rc360_half_dst")
    try:
        await bestaand.execute("CREATE SCHEMA van_een_andere_sessie")
    finally:
        await bestaand.close()

    schemas_before = await manager._schema_names("rc360_half_dst")
    with pytest.raises(Exception, match="clone"):
        await _clone(connector, manager, "rc360_half_dst")

    clone_schemas = {
        TARGET_SCHEMA,
        TARGET_EXTRA,
        SOURCE_SCHEMA,
        SOURCE_EXTRA,
        "peers",
        "contracts",
    }
    await manager._drop_schemas_created_since("rc360_half_dst", schemas_before, clone_schemas)
    await connector.close()

    doel = await _connect("rc360_half_dst")
    try:
        overgebleven = {
            row["nspname"]
            for row in await doel.fetch(
                "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema'"
            )
        }
        assert overgebleven & clone_schemas == set()
        assert "van_een_andere_sessie" in overgebleven
    finally:
        await doel.close()
