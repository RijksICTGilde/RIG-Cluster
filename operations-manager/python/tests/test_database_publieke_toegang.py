"""Een database op de gedeelde PostgreSQL is alleen te openen door zijn eigen rollen (RC-285).

Vijf plekken waar dat iets doet: de connectorhelper (de statements en de drie standen van
``DATABASE_RESTRICT_PUBLIC_CONNECT``), de manager (beide rollen van een deployment, ook bij
een database die al bestond), de kloon van een externe bron, de platformdatabases bij de
start van OPI, en de database-console die niet meer op de read-write gebruiker terugvalt.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
import yaml
from opi.connectors.postgres import PostgresConnector, PostgresExecutionError, PostgresValidationError
from opi.core.config import Settings
from opi.core.platform_database_access import PLATFORM_DATABASE_ROLES, restrict_platform_databases
from opi.manager.database_manager import DatabaseManager
from opi.manager.db_console_manager import DbConsoleError, DbConsoleManager, DbConsoleTool

#: ``operations-manager/python/tests/`` -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DATABASES_YAML = _REPO_ROOT / "infrastructure/bootstrap/infrastructure/postgresql/database/base/databases.yaml"

_REVOKE = 'REVOKE CONNECT, TEMPORARY ON DATABASE "proj_test" FROM PUBLIC'


def _connector(*, bestaat: bool = True, verbonden: list[str] | None = None) -> tuple[PostgresConnector, AsyncMock]:
    connector = PostgresConnector("rig-db-rw", "postgres", "wachtwoord")
    conn = AsyncMock()
    conn.fetchval.return_value = 1 if bestaat else None
    conn.fetch.return_value = [{"usename": naam} for naam in (verbonden or [])]
    connector._get_or_create_connection = AsyncMock(return_value=conn)  # type: ignore[method-assign]
    return connector, conn


def _uitgevoerd(conn: AsyncMock) -> list[str]:
    return [call.args[0] for call in conn.execute.await_args_list]


# --- de connector -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_sluit_af_voor_public_en_geeft_elke_rol_connect() -> None:
    connector, conn = _connector()
    with patch("opi.connectors.postgres.settings.DATABASE_RESTRICT_PUBLIC_CONNECT", "on"):
        result = await connector.restrict_database_to_roles("proj_test", ["proj_test", "proj_test_ro"])

    assert result["status"] == "restricted"
    assert _uitgevoerd(conn) == [
        _REVOKE,
        'GRANT CONNECT ON DATABASE "proj_test" TO "proj_test"',
        'GRANT CONNECT ON DATABASE "proj_test" TO "proj_test_ro"',
    ]


@pytest.mark.asyncio
async def test_zonder_rollen_blijft_alleen_de_revoke_over() -> None:
    """De eigenaar houdt zijn rechten los van de GRANT-lijst, dus een lege lijst mag."""
    connector, conn = _connector()
    with patch("opi.connectors.postgres.settings.DATABASE_RESTRICT_PUBLIC_CONNECT", "on"):
        await connector.restrict_database_to_roles("proj_test", [])

    assert _uitgevoerd(conn) == [_REVOKE]


@pytest.mark.asyncio
async def test_dry_run_voert_niets_uit_en_meldt_wie_verbonden_is() -> None:
    connector, conn = _connector(verbonden=["proj_test", "ander_project"])
    with patch("opi.connectors.postgres.settings.DATABASE_RESTRICT_PUBLIC_CONNECT", "dry-run"):
        result = await connector.restrict_database_to_roles("proj_test", ["proj_test"])

    conn.execute.assert_not_awaited()
    assert result["status"] == "dry-run"
    assert result["statements"] == [_REVOKE, 'GRANT CONNECT ON DATABASE "proj_test" TO "proj_test"']
    assert result["connected_roles"] == ["proj_test", "ander_project"]


@pytest.mark.asyncio
async def test_off_raakt_de_server_niet() -> None:
    connector, conn = _connector()
    with patch("opi.connectors.postgres.settings.DATABASE_RESTRICT_PUBLIC_CONNECT", "off"):
        result = await connector.restrict_database_to_roles("proj_test", ["proj_test"])

    assert result["status"] == "disabled"
    conn.execute.assert_not_awaited()
    conn.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_een_database_die_niet_bestaat_wordt_gemeld_zonder_statements() -> None:
    connector, conn = _connector(bestaat=False)
    with patch("opi.connectors.postgres.settings.DATABASE_RESTRICT_PUBLIC_CONNECT", "on"):
        result = await connector.restrict_database_to_roles("proj_test", ["proj_test"])

    assert result["status"] == "not_found"
    conn.execute.assert_not_awaited()


@pytest.mark.parametrize("rol", ["proj; DROP DATABASE x", "proj test", "", "select"])
@pytest.mark.asyncio
async def test_een_ongeldige_rolnaam_komt_niet_in_ddl(rol: str) -> None:
    connector, conn = _connector()
    with (
        patch("opi.connectors.postgres.settings.DATABASE_RESTRICT_PUBLIC_CONNECT", "on"),
        pytest.raises(PostgresValidationError),
    ):
        await connector.restrict_database_to_roles("proj_test", [rol])
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_een_ongeldige_databasenaam_komt_niet_in_ddl() -> None:
    connector, conn = _connector()
    with (
        patch("opi.connectors.postgres.settings.DATABASE_RESTRICT_PUBLIC_CONNECT", "on"),
        pytest.raises(PostgresValidationError),
    ):
        await connector.restrict_database_to_roles('proj"; DROP DATABASE x', ["proj_test"])
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_een_databasefout_wordt_een_connectorfout() -> None:
    connector, conn = _connector()
    conn.execute.side_effect = asyncpg.InsufficientPrivilegeError("permission denied for database")
    with (
        patch("opi.connectors.postgres.settings.DATABASE_RESTRICT_PUBLIC_CONNECT", "on"),
        pytest.raises(PostgresExecutionError, match="permission denied for database"),
    ):
        await connector.restrict_database_to_roles("proj_test", ["proj_test"])


# --- de manager -------------------------------------------------------------------


class _Server:
    """De connectoraanroepen die de manager doet, met de afsluitingen die eruit komen."""

    def __init__(self) -> None:
        self.afsluitingen: list[tuple[str, list[str]]] = []

    async def create_user(self, username: str, password: str, **kwargs: Any) -> dict[str, str]:
        return {"status": "exists"}

    async def restrict_database_to_roles(self, database: str, roles: list[str]) -> dict[str, Any]:
        self.afsluitingen.append((database, roles))
        return {"status": "restricted"}

    async def set_connection_limit(self, username: str, connection_limit: int) -> dict[str, Any]:
        return {"status": "unchanged", "previous": connection_limit, "connection_limit": connection_limit}

    async def noop(self, *args: Any, **kwargs: Any) -> dict[str, str]:
        return {"status": "success"}


def _project(schemas: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    config: dict[str, Any] = {"schemas": schemas} if schemas else {}
    return {
        "name": "proj",
        "services": [{"name": "postgresql-database", "config": config}],
        "deployments": [{"name": "test", "cluster": "local", "services": ["postgresql-database"]}],
    }


def _manager(server: _Server) -> DatabaseManager:
    project_manager = SimpleNamespace(
        get_name=AsyncMock(return_value="proj"),
        get_progress_manager=lambda: None,
        _add_secret_to_create=lambda *args: None,
    )
    mgr = DatabaseManager(project_manager=project_manager, db_host="h", admin_username="a", admin_password="p")
    mgr._postgres_connector = SimpleNamespace(  # type: ignore[assignment]
        create_user=server.create_user,
        set_connection_limit=server.set_connection_limit,
        update_user_password=server.noop,
        update_user_privileges=server.noop,
        create_schema=server.noop,
        set_role_search_path=server.noop,
        grant_readonly_on_schema=server.noop,
        restrict_database_to_roles=server.restrict_database_to_roles,
    )
    return mgr


async def _verwerk(mgr: DatabaseManager, project: dict[str, Any]) -> None:
    """Een herverwerking van een deployment waarvan de database en de rollen al bestaan."""
    bestaand = SimpleNamespace(password="DummyWachtwoord123abc", ro_password="DummyWachtwoord123ro")
    state = SimpleNamespace(database="proj_test", schema="proj_test", password="DummyWachtwoord123abc")
    with (
        patch.object(DatabaseManager, "_deployment_uses_postgresql", AsyncMock(return_value=True)),
        patch.object(DatabaseManager, "_ensure_connection", lambda self: None),
        patch.object(DatabaseManager, "_get_existing_database_credentials_from_k8s", AsyncMock(return_value=bestaand)),
        patch.object(DatabaseManager, "_test_database_connection", AsyncMock(return_value=True)),
        patch.object(DatabaseManager, "_ensure_database_state", AsyncMock(return_value=state)),
        patch.object(DatabaseManager, "_get_deployment_database_generation", lambda self, *args: None),
        patch("opi.manager.database_manager.get_database_server", return_value="h"),
    ):
        await mgr.create_resources_for_deployment(project, project["deployments"][0], False)


@pytest.mark.asyncio
async def test_een_bestaande_database_wordt_bij_verwerking_afgesloten() -> None:
    server = _Server()
    await _verwerk(_manager(server), _project())

    assert server.afsluitingen == [("proj_test", ["proj_test", "proj_test_ro"])]


@pytest.mark.asyncio
async def test_extra_schemas_leveren_geen_extra_rollen_op() -> None:
    """De extra schema's (RC-17) zijn van de hoofdgebruiker, dus de lijst blijft twee rollen."""
    server = _Server()
    await _verwerk(_manager(server), _project([{"postfix": "rapportage"}, {"postfix": "archief"}]))

    assert server.afsluitingen == [("proj_test", ["proj_test", "proj_test_ro"])]


# --- de kloon van een externe bron ------------------------------------------------


class _KloonServer(_Server):
    """De kloonweg raakt meer connectormethodes dan een verwerking."""

    def __init__(self, bestaand: set[str] | None = None) -> None:
        super().__init__()
        self.bestaand = bestaand or set()

    async def create_database(self, database_name: str, owner: str) -> dict[str, str]:
        return {"status": "exists" if database_name in self.bestaand else "created"}

    async def clone_schema_from_external(self, **kwargs: Any) -> dict[str, str]:
        return {"status": "success"}


def _kloon_manager(server: _KloonServer) -> DatabaseManager:
    project_manager = SimpleNamespace(
        get_contents=AsyncMock(return_value=_project()),
        get_name=AsyncMock(return_value="proj"),
        mark_clone_started=AsyncMock(),
        _revision_manager=SimpleNamespace(record_clone=lambda **kwargs: None),
        _project_file_handler=SimpleNamespace(set_deployment_service_generation=lambda *args: None),
        _add_secret_to_create=lambda *args: None,
        _secrets_to_create={},
    )
    mgr = DatabaseManager(project_manager=project_manager, db_host="h", admin_username="a", admin_password="p")
    mgr._postgres_connector = SimpleNamespace(  # type: ignore[assignment]
        create_database=server.create_database,
        restrict_database_to_roles=server.restrict_database_to_roles,
        clone_schema_from_external=server.clone_schema_from_external,
        create_schema=server.noop,
        set_role_search_path=server.noop,
    )
    return mgr


async def _kloon(server: _KloonServer, *, force_clone: bool, generation: int | None = None) -> dict[str, Any]:
    """De handmatige kloon-API, de weg die buiten een verwerking om loopt."""
    mgr = _kloon_manager(server)
    with (
        patch.object(DatabaseManager, "_ensure_connection", lambda self: None),
        patch.object(DatabaseManager, "_validate_external_source", AsyncMock(return_value={"table_count": 3})),
        patch.object(DatabaseManager, "_deployment_uses_postgresql", AsyncMock(return_value=True)),
        patch.object(DatabaseManager, "_get_database_config_for_deployment", AsyncMock(return_value=("h", "a", "p"))),
        patch.object(DatabaseManager, "_get_deployment_database_generation", lambda self, *args: generation),
        patch.object(DatabaseManager, "_resolve_database_credentials", AsyncMock(return_value="DummyWachtwoord123abc")),
        patch.object(DatabaseManager, "_reconcile_connection_limit", AsyncMock()),
        patch.object(DatabaseManager, "_schema_names", AsyncMock(return_value=[])),
        patch.object(DatabaseManager, "_resolve_extra_schemas", lambda self, *args: []),
        patch.object(
            DatabaseManager,
            "_ensure_readonly_user",
            AsyncMock(return_value=("proj_test_ro", "DummyWachtwoord123ro")),
        ),
    ):
        return await mgr._execute_external_clone(
            project_name="proj",
            deployment_name="test",
            source_host="bron",
            source_port=5432,
            source_username="bron_user",
            source_password="bron_pw",
            source_database="bron_db",
            source_schema="public",
            force_clone=force_clone,
        )


@pytest.mark.asyncio
async def test_een_verse_kloondatabase_is_dicht_voor_public() -> None:
    server = _KloonServer()
    result = await _kloon(server, force_clone=False)

    assert result["errors"] == []
    assert server.afsluitingen == [("proj_test", ["proj_test"])]


@pytest.mark.asyncio
async def test_de_nieuwe_generatie_van_force_clone_is_dicht_voor_public() -> None:
    """force_clone maakt een _vN-database naast de bestaande, en die is ook nieuw."""
    server = _KloonServer(bestaand={"proj_test"})
    result = await _kloon(server, force_clone=True)

    assert result["errors"] == []
    assert server.afsluitingen == [("proj_test", ["proj_test"]), ("proj_test_v1", ["proj_test"])]


# --- de platformdatabases ---------------------------------------------------------


@pytest.mark.asyncio
async def test_elke_platformdatabase_wordt_afgesloten_met_zijn_eigen_rol() -> None:
    afsluitingen: list[tuple[str, list[str]]] = []

    async def restrict(database: str, roles: list[str]) -> dict[str, Any]:
        afsluitingen.append((database, roles))
        return {"status": "restricted"}

    connector = SimpleNamespace(restrict_database_to_roles=restrict, close=AsyncMock())
    with patch("opi.core.platform_database_access.create_postgres_connector", return_value=connector):
        await restrict_platform_databases()

    assert afsluitingen == [
        ("keycloak", ["keycloak"]),
        ("forgejo", ["forgejo"]),
        ("mailrelay", ["mailrelay"]),
        # De database van OPI zelf: eigenaar is de superuser, die heeft geen GRANT nodig.
        ("operations_manager", []),
    ]
    connector.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_een_weigerende_server_blokkeert_de_boot_niet() -> None:
    connector = SimpleNamespace(
        restrict_database_to_roles=AsyncMock(side_effect=PostgresExecutionError("server weg")),
        close=AsyncMock(),
    )
    with patch("opi.core.platform_database_access.create_postgres_connector", return_value=connector):
        await restrict_platform_databases()

    connector.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_een_fout_op_de_ene_database_slaat_de_andere_niet_over() -> None:
    afgesloten: list[str] = []

    async def restrict(database: str, roles: list[str]) -> dict[str, Any]:
        if database == "keycloak":
            raise PostgresExecutionError("keycloak weigert")
        afgesloten.append(database)
        return {"status": "restricted"}

    connector = SimpleNamespace(restrict_database_to_roles=restrict, close=AsyncMock())
    with patch("opi.core.platform_database_access.create_postgres_connector", return_value=connector):
        await restrict_platform_databases()

    assert afgesloten == ["forgejo", "mailrelay", "operations_manager"]


def test_de_lijst_spiegelt_databases_yaml() -> None:
    """Tegen het echte bestand, niet tegen een tweede kopie van de lijst in deze toets.

    Een database-resource met ``owner: postgres`` (de eigen database van OPI) staat niet in
    ``PLATFORM_DATABASE_ROLES``: zijn eigenaar is de superuser, die geen GRANT nodig heeft.
    """
    documenten = [doc for doc in yaml.safe_load_all(_DATABASES_YAML.read_text(encoding="utf-8")) if doc]
    uit_yaml = {
        doc["spec"]["name"]: (doc["spec"]["owner"],) for doc in documenten if doc["spec"]["owner"] != "postgres"
    }

    assert uit_yaml == PLATFORM_DATABASE_ROLES


def test_de_standaard_is_dry_run() -> None:
    """De REVOKE raakt elke database op de server; dry-run moet de eerste stand blijven
    tot een beheerder hem bewust omzet (zie DATABASE_RESTRICT_PUBLIC_CONNECT in config.py)."""
    assert Settings().DATABASE_RESTRICT_PUBLIC_CONNECT == "dry-run"


# --- de console -------------------------------------------------------------------


class _Genoeg(Exception):
    """Stopt de uitrol zodra de rol gekozen is: de rest van de bundel hoort niet bij deze toets."""


def _console_secret(ro: bool) -> SimpleNamespace:
    return SimpleNamespace(
        username="proj_test",
        password="DummyWachtwoord123abc",
        ro_username="proj_test_ro" if ro else "",
        ro_password="DummyWachtwoord123ro" if ro else "",
    )


async def _open_console(secret: SimpleNamespace, gebruikt: list[str]) -> None:
    """Opent een console; ``gebruikt`` krijgt de rol waarmee hij zou verbinden."""
    manager = DbConsoleManager.__new__(DbConsoleManager)
    manager._kubectl = SimpleNamespace()  # type: ignore[attr-defined]
    manager._zad_realm = lambda: "zad"  # type: ignore[method-assign]

    def tool_config(tool: Any, db_secret: Any, user: str, password: str) -> None:
        gebruikt.append(user)
        raise _Genoeg

    manager._tool_config = tool_config  # type: ignore[method-assign]
    deployment = {"name": "test", "cluster": "local"}
    project = SimpleNamespace(data={"deployments": [deployment]})
    with (
        patch("opi.manager.db_console_manager.get_project_store", return_value=SimpleNamespace(get=lambda _: project)),
        patch("opi.manager.db_console_manager.find_deployment", return_value=deployment),
        patch("opi.manager.db_console_manager.DatabaseSecret.get_data", AsyncMock(return_value=secret)),
        patch(
            "opi.manager.db_console_manager.create_keycloak_connector",
            AsyncMock(return_value=SimpleNamespace(create_oidc_client=AsyncMock(return_value={"secret": "geheim"}))),
        ),
        patch("opi.manager.db_console_manager.mark_client_created", lambda client_id: None),
    ):
        await manager._provision_bundle("proj", "test", "sessie", DbConsoleTool.PGWEB, "iemand@example.com")


@pytest.mark.asyncio
async def test_zonder_read_only_rol_opent_de_console_niet() -> None:
    gebruikt: list[str] = []
    with pytest.raises(DbConsoleError, match="read-only databaserol"):
        await _open_console(_console_secret(ro=False), gebruikt)

    assert gebruikt == []


@pytest.mark.asyncio
async def test_met_read_only_rol_verbindt_de_console_als_die_rol() -> None:
    gebruikt: list[str] = []
    with pytest.raises(_Genoeg):
        await _open_console(_console_secret(ro=True), gebruikt)

    assert gebruikt == ["proj_test_ro"]
