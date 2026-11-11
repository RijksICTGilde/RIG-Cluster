"""De DatabaseManager over meer dan een deployment, tegen een echte PostgreSQL.

De connectortoetsen meten een methode per keer; hier doet de manager de stappen in de
volgorde waarin een verwerking ze doet, en na elke stap leest een invariant de hele
toestand terug: elke rol van een deployment komt precies bij zijn eigen database en
schema's, en bij geen andere. Dat is de toets die ook de fout vangt die we nog niet kennen,
want een GRANT die te ruim is, is in PostgreSQL geen fout.

Kubernetes heeft nog geen live groep, dus het secret-kanaal van de manager is hier een mock
(zie ``project_manager_stub``); alles wat hij met PostgreSQL doet is echt. De schemataken en
de kloontaak van begin tot eind, met git en ArgoCD erbij, staan als ``planned`` op
``tests/regression/actions.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import asyncpg
import pytest
from opi.core.config import settings
from opi.core.database_pools import close_database_pools
from opi.core.platform_database_access import restrict_platform_databases
from opi.core.readiness import ReadinessState
from opi.core.startup import _setup_database
from opi.utils.naming import generate_database_name, generate_extra_database_schema
from tests.integration.postgres_live import (
    PASSWORD,
    SERVICE_NAME,
    admin_credentials,
    connection_as,
    database_exists,
    drop_by_prefix,
    live_connector,
    manager,
    project_manager_stub,
    resolve_service_name,
    role_exists,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from opi.connectors.postgres import PostgresConnector

pytestmark = [pytest.mark.requires_infra, pytest.mark.postgres_live]

__all__ = ["live_connector", "resolve_service_name"]

#: Alles wat deze module maakt begint hiermee, en de opruiming gaat op dit voorvoegsel.
PREFIX = "zadlive"
PROJECT = PREFIX
CLUSTER = "sandboxed-local"
DEPLOYMENTS = ("d1", "d2")


@dataclass(frozen=True)
class Reachable:
    """Wat een deployment volgens zijn eigen secret mag bereiken."""

    database: str
    schemas: tuple[str, ...]
    username: str
    password: str
    ro_username: str
    ro_password: str


def project_data(*, schemas: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "name": PROJECT,
        "clusters": [CLUSTER],
        "services": [{"name": "postgresql-database", "config": {"schemas": [{"postfix": naam} for naam in schemas]}}],
        "components": [{"name": "app", "services": ["postgresql-database"]}],
        "deployments": [
            {
                "name": naam,
                "cluster": CLUSTER,
                "namespace": PREFIX,
                "components": [{"reference": "app"}],
            }
            for naam in DEPLOYMENTS
        ],
    }


def deployment(data: dict[str, Any], name: str) -> dict[str, Any]:
    return next(entry for entry in data["deployments"] if entry["name"] == name)


@pytest.fixture
async def schoon() -> AsyncIterator[None]:
    await drop_by_prefix(PREFIX)
    yield
    await drop_by_prefix(PREFIX)


@pytest.fixture
def shared_server(monkeypatch: pytest.MonkeyPatch, resolve_service_name: None) -> None:
    """Wijs de manager naar de wegwerpserver en zet de afsluiting aan.

    ``DATABASE_RESTRICT_PUBLIC_CONNECT`` staat standaard op ``dry-run``; de stand die de
    invariant hieronder meet is ``on``, want dat is de stand waarin een database alleen voor
    zijn eigen rollen open is.
    """
    username, password = admin_credentials()
    monkeypatch.setattr("opi.manager.database_manager.get_database_server", lambda _cluster: SERVICE_NAME)
    monkeypatch.setattr("opi.manager.database_manager.other_project_deployments", lambda _name: {})
    monkeypatch.setattr(settings, "DATABASE_ADMIN_NAME", username)
    monkeypatch.setattr(settings, "DATABASE_ADMIN_PASSWORD", password)
    monkeypatch.setattr(settings, "DATABASE_RESTRICT_PUBLIC_CONNECT", "on")


def reachable_from_secrets(stub_secrets: dict[tuple[str, str], Any], name: str) -> Reachable:
    secret = stub_secrets[(name, "database")]
    return Reachable(
        database=secret.database,
        schemas=(secret.schema, *(schema for _postfix, schema in secret.extra_schemas)),
        username=secret.username,
        password=secret.password,
        ro_username=secret.ro_username,
        ro_password=secret.ro_password,
    )


async def assert_each_role_reaches_only_its_own(reachable: list[Reachable], *, step: str) -> None:
    """Na elke stap: elke rol bij zijn eigen database en schema's, en bij geen andere."""
    for own in reachable:
        conn = await connection_as(own.username, own.password, own.database)
        try:
            assert await conn.fetchval("SELECT current_schema()") == own.schemas[0], (
                f"{step}: het search_path van {own.username} begint niet bij {own.schemas[0]}"
            )
            for schema in own.schemas:
                assert await conn.fetchval("SELECT has_schema_privilege($1, 'CREATE')", schema) is True, (
                    f"{step}: {own.username} mag niet schrijven in zijn eigen schema {schema}"
                )
        finally:
            await conn.close()

        read_only = await connection_as(own.ro_username, own.ro_password, own.database)
        try:
            for schema in own.schemas:
                assert await read_only.fetchval("SELECT has_schema_privilege($1, 'USAGE')", schema) is True, (
                    f"{step}: de leesrol van {own.username} komt niet in schema {schema}"
                )
                assert await read_only.fetchval("SELECT has_schema_privilege($1, 'CREATE')", schema) is False, (
                    f"{step}: de leesrol van {own.username} mag schrijven in schema {schema}"
                )
        finally:
            await read_only.close()

        for other in reachable:
            if other.database == own.database:
                continue
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection_as(own.username, own.password, other.database)
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection_as(own.ro_username, own.ro_password, other.database)


async def test_two_deployments_each_reach_only_their_own_database(
    live_connector: PostgresConnector, shared_server: None, schoon: None
) -> None:
    data = project_data()
    stub = project_manager_stub(PROJECT)
    database_manager = manager(live_connector, stub)

    for name in DEPLOYMENTS:
        await database_manager.create_resources_for_deployment(data, deployment(data, name))
        await assert_each_role_reaches_only_its_own(
            [
                reachable_from_secrets(stub.stored_secrets, done)
                for done in DEPLOYMENTS
                if (done, "database") in stub.stored_secrets
            ],
            step=f"na het aanmaken van {name}",
        )

    assert {reachable_from_secrets(stub.stored_secrets, name).database for name in DEPLOYMENTS} == {
        generate_database_name(PROJECT, name, None) for name in DEPLOYMENTS
    }


async def test_an_extra_schema_comes_and_goes_without_touching_the_rest(
    live_connector: PostgresConnector, shared_server: None, schoon: None
) -> None:
    """Een schema erbij, opnieuw verwerken, en eraf: de invariant houdt na elke stap."""
    stub = project_manager_stub(PROJECT)
    database_manager = manager(live_connector, stub)

    plain = project_data()
    for name in DEPLOYMENTS:
        await database_manager.create_resources_for_deployment(plain, deployment(plain, name))

    with_schema = project_data(schemas=("rapport",))
    for name in DEPLOYMENTS:
        await database_manager.create_resources_for_deployment(with_schema, deployment(with_schema, name))
    reachable = [reachable_from_secrets(stub.stored_secrets, name) for name in DEPLOYMENTS]
    await assert_each_role_reaches_only_its_own(reachable, step="na het extra schema")
    for name in DEPLOYMENTS:
        assert (
            generate_extra_database_schema(PROJECT, name, "rapport")
            in reachable_from_secrets(stub.stored_secrets, name).schemas
        )

    # Opnieuw verwerken verandert niets: hetzelfde wachtwoord, dezelfde rechten.
    before = reachable_from_secrets(stub.stored_secrets, DEPLOYMENTS[0])
    await database_manager.create_resources_for_deployment(with_schema, deployment(with_schema, DEPLOYMENTS[0]))
    assert reachable_from_secrets(stub.stored_secrets, DEPLOYMENTS[0]) == before
    await assert_each_role_reaches_only_its_own(reachable, step="na het opnieuw verwerken")

    # Het schema eraf halen laat het schema staan (de data blijft), maar het search_path
    # van de rol verliest het niet: dat is wat de volgende verwerking opnieuw zet.
    marked = project_data()
    for name in DEPLOYMENTS:
        await database_manager.create_resources_for_deployment(marked, deployment(marked, name))
    await assert_each_role_reaches_only_its_own(
        [reachable_from_secrets(stub.stored_secrets, name) for name in DEPLOYMENTS],
        step="na het weghalen van het extra schema",
    )


async def test_deleting_a_deployment_leaves_no_database_or_role_behind(
    live_connector: PostgresConnector, shared_server: None, schoon: None
) -> None:
    data = project_data()
    stub = project_manager_stub(PROJECT)
    database_manager = manager(live_connector, stub)
    for name in DEPLOYMENTS:
        await database_manager.create_resources_for_deployment(data, deployment(data, name))

    gone = reachable_from_secrets(stub.stored_secrets, DEPLOYMENTS[0])
    result = await database_manager.delete_resources_for_deployment(data, deployment(data, DEPLOYMENTS[0]))
    assert result["success"] is True, result

    assert await database_exists(gone.database) is False
    assert await role_exists(gone.username) is False
    assert await role_exists(gone.ro_username) is False

    # En de andere deployment staat er onveranderd, met zijn eigen grenzen.
    await assert_each_role_reaches_only_its_own(
        [reachable_from_secrets(stub.stored_secrets, DEPLOYMENTS[1])], step="na het verwijderen van de eerste"
    )


async def test_the_startup_phase_creates_its_own_database_and_migrates_it(
    resolve_service_name: None, monkeypatch: pytest.MonkeyPatch, schoon: None
) -> None:
    """``_setup_database`` uit de opstart: de eigen database, de pools en de migraties.

    Dit is de stap die in productie de alembic-migraties over de database van OPI legt. Op
    de gepinde image, want een migratie die op de ene PostgreSQL slaagt en op de andere
    niet, hoort hier rood te worden en niet in de uitrol.
    """
    username, password = admin_credentials()
    own_database = f"{PREFIX}_opi"
    monkeypatch.setattr(settings, "DATABASE_HOST", SERVICE_NAME)
    monkeypatch.setattr(settings, "DATABASE_ADMIN_NAME", username)
    monkeypatch.setattr(settings, "DATABASE_ADMIN_PASSWORD", password)
    monkeypatch.setattr(settings, "DATABASE_NAME", own_database)
    monkeypatch.setattr(settings, "DATABASE_APP_USER", username)
    monkeypatch.setattr(settings, "DATABASE_APP_PASSWORD", password)

    readiness = ReadinessState()
    try:
        assert await _setup_database(readiness) is True
        assert readiness.database.ready is True
        conn = await connection_as(username, password, own_database)
        try:
            assert await conn.fetchval("SELECT count(*) FROM alembic_version") == 1
        finally:
            await conn.close()
    finally:
        await close_database_pools()


async def test_restrict_platform_databases_locks_every_database_to_its_own_role(
    live_connector: PostgresConnector, resolve_service_name: None, monkeypatch: pytest.MonkeyPatch, schoon: None
) -> None:
    """De opstartstap die de platformdatabases afsluit voor PUBLIC (RC-410).

    De namen van de platformdatabases staan hier op wegwerpnamen, zodat de toets geen
    database "keycloak" naast de echte zet; wat gemeten wordt is de stap zelf.
    """
    username, password = admin_credentials()
    platform = f"{PREFIX}_keycloak"
    own = f"{PREFIX}_opi"
    monkeypatch.setattr(settings, "DATABASE_ADMIN_NAME", username)
    monkeypatch.setattr(settings, "DATABASE_ADMIN_PASSWORD", password)
    monkeypatch.setattr(settings, "DATABASE_NAME", own)
    monkeypatch.setattr(settings, "DATABASE_APP_USER", f"{PREFIX}_app")
    monkeypatch.setattr(settings, "DATABASE_RESTRICT_PUBLIC_CONNECT", "on")
    monkeypatch.setattr("opi.core.platform_database_access.get_database_server", lambda _cluster: SERVICE_NAME)
    monkeypatch.setattr(
        "opi.core.platform_database_access.PLATFORM_DATABASE_ROLES", {platform: (f"{PREFIX}_keycloak_role",)}
    )
    monkeypatch.setattr("opi.core.platform_database_access.PLATFORM_BACKUP_ROLE", f"{PREFIX}_backup")

    for role in (f"{PREFIX}_keycloak_role", f"{PREFIX}_app", f"{PREFIX}_backup", f"{PREFIX}_vreemd"):
        await live_connector.create_user(role, PASSWORD, connection_limit=5)
    for database in (platform, own):
        await live_connector.create_database(database, username)

    conn = await connection_as(f"{PREFIX}_vreemd", PASSWORD, platform)
    await conn.close()

    await restrict_platform_databases()

    for database, allowed in ((platform, f"{PREFIX}_keycloak_role"), (own, f"{PREFIX}_app")):
        eigen = await connection_as(allowed, PASSWORD, database)
        await eigen.close()
        backup = await connection_as(f"{PREFIX}_backup", PASSWORD, database)
        await backup.close()
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection_as(f"{PREFIX}_vreemd", PASSWORD, database)
