"""OPI opent zijn EIGEN database met een rol zonder superuser (RC-410).

OPI verbond met zijn eigen database (taken, sessies, audit, projectstore) als de
``postgres``-superuser op de gedeelde server waar ook elke tenantdatabase staat. Een foute
query of een injectie in die tabellen draaide daarmee als superuser op alles. Sinds deze
taak is er een eigen rol ``operations_manager``, eigenaar van de database en zonder
superuser, createdb of createrole.

Wat hier gemeten wordt:

* de verbindingen naar de EIGEN database gebruiken de app-credentials, en de paden die
  tenant- en platformdatabases beheren houden de admin-credentials;
* de opstartcontrole weigert buiten een ontwikkelomgeving een rol die te veel mag, en
  waarschuwt alleen lokaal;
* de overdrachtsstap verplaatst wat nog van de admin-rol is, en doet niets als het
  eigendom al klopt;
* ``restrict_platform_databases`` geeft de app-rol CONNECT op de eigen database, anders
  sluit OPI zich daar buiten zodra de restrictie ``on`` staat.

De manifesten (managed role, Database-owner, de env van de Deployment, decrypt-sops) staan
in ``tests/test_opi_databaserol_manifesten.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.connectors.postgres import PostgresExecutionError, PostgresValidationError
from opi.core.app_database_owner import ensure_app_database_owner
from opi.core.app_database_owner import main as owner_main
from opi.core.config import settings
from opi.core.database_pool import DatabasePoolError
from opi.core.database_pools import (
    MAIN_POOL_MAX_SIZE,
    _ensure_database_exists,
    _verify_app_role,
    initialize_database_pools,
)
from opi.core.db import ORM_MAX_OVERFLOW, ORM_POOL_SIZE, _default_url, app_database_url, configure_engine
from opi.core.platform_database_access import restrict_platform_databases

APP_USER = "operations_manager"
APP_PASSWORD = "app-geheim"


@pytest.fixture(autouse=True)
def app_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DATABASE_APP_USER", APP_USER)
    monkeypatch.setattr(settings, "DATABASE_APP_PASSWORD", APP_PASSWORD)
    monkeypatch.setattr(settings, "DATABASE_ADMIN_NAME", "postgres")
    monkeypatch.setattr(settings, "DATABASE_ADMIN_PASSWORD", "superuser-geheim")


# --- welke credentials waar ------------------------------------------------


def test_de_sqlalchemy_url_gebruikt_de_app_rol() -> None:
    url = _default_url()

    assert f"//{APP_USER}:{APP_PASSWORD}@" in url
    assert "postgres:superuser-geheim" not in url


def test_de_alembic_url_gebruikt_de_app_rol() -> None:
    """De migraties draaien als de rol die de tabellen bezit.

    De URL-bouwer is gedeeld met ``opi/migrations/env.py``; die module is buiten Alembic
    niet te importeren, dus hier staat de vorm die hij doorgeeft.
    """
    url = app_database_url("postgresql+psycopg2", "sslmode")

    assert url.startswith("postgresql+psycopg2://")
    assert "sslmode=" in url

    assert f"//{APP_USER}:{APP_PASSWORD}@" in url
    assert "postgres:superuser-geheim" not in url


def test_een_lege_app_instelling_is_een_fout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Geen stille terugval op de superuser: een vergeten instelling moet een fout zijn."""
    monkeypatch.setattr(settings, "DATABASE_APP_PASSWORD", None)

    with pytest.raises(ValueError, match="DATABASE_APP_PASSWORD"):
        _default_url()


@pytest.mark.asyncio
async def test_de_hoofdpool_krijgt_de_app_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    gebouwd: list[dict[str, Any]] = []

    class _Pool:
        def __init__(self, **kwargs: Any) -> None:
            gebouwd.append(kwargs)

    monkeypatch.setattr("opi.core.database_pools.DatabasePool", _Pool)
    monkeypatch.setattr("opi.core.database_pools._pools", {})
    monkeypatch.setattr("opi.core.database_pools._ensure_database_exists", AsyncMock())
    monkeypatch.setattr("opi.core.database_pools._initialize_database_pool_with_retry", AsyncMock())
    monkeypatch.setattr("opi.core.database_pools._verify_app_role", AsyncMock())

    await initialize_database_pools()

    assert gebouwd[0]["user"] == APP_USER
    assert gebouwd[0]["password"] == APP_PASSWORD
    assert gebouwd[0]["database"] == settings.DATABASE_NAME
    assert gebouwd[0]["max_size"] == MAIN_POOL_MAX_SIZE


def test_de_orm_engine_neemt_het_vastgelegde_plafond(monkeypatch: pytest.MonkeyPatch) -> None:
    gebouwd: dict[str, Any] = {}

    def _engine(url: str, **kwargs: Any) -> MagicMock:
        gebouwd.update(kwargs)
        return MagicMock()

    monkeypatch.setattr("opi.core.db.create_async_engine", _engine)
    # Via monkeypatch, zodat de teardown de engine van de testsessie terugzet: andere
    # tests delen deze module-globals.
    monkeypatch.setattr("opi.core.db._engine", None)
    monkeypatch.setattr("opi.core.db._sessionmaker", None)

    configure_engine("postgresql+asyncpg://u:p@127.0.0.1:1/weg")

    assert gebouwd["pool_size"] == ORM_POOL_SIZE
    assert gebouwd["max_overflow"] == ORM_MAX_OVERFLOW


def test_een_pod_opent_hoogstens_tien_verbindingen_naar_de_eigen_database() -> None:
    """De connectionLimit van de managed role operations_manager is hierop gerekend: twee
    pods tijdens een uitrol plus Alembic. Gaat een pool omhoog, dan moet die limiet mee
    (het commentaar bij de rol in postgresql/database/base/cluster.yaml rekent het voor)."""
    assert MAIN_POOL_MAX_SIZE + ORM_POOL_SIZE + ORM_MAX_OVERFLOW <= 10


@pytest.mark.asyncio
async def test_het_aanmaken_van_de_database_blijft_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """CREATE DATABASE kan de app-rol niet, en hoort hij ook niet te kunnen."""
    connect = AsyncMock()
    connect.return_value.fetchval = AsyncMock(return_value=1)
    connect.return_value.close = AsyncMock()
    monkeypatch.setattr("opi.core.database_pools.asyncpg.connect", connect)

    await _ensure_database_exists()

    assert connect.await_args.kwargs["user"] == "postgres"
    assert connect.await_args.kwargs["password"] == "superuser-geheim"
    assert connect.await_args.kwargs["database"] == "postgres"


@pytest.mark.asyncio
async def test_de_eigen_database_geeft_de_app_rol_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zonder deze regel sluit DATABASE_RESTRICT_PUBLIC_CONNECT=on OPI zelf buiten."""
    connector = MagicMock()
    connector.restrict_database_to_roles = AsyncMock(return_value={"status": "restricted"})
    connector.close = AsyncMock()
    monkeypatch.setattr("opi.core.platform_database_access.create_postgres_connector", lambda *a: connector)
    monkeypatch.setattr("opi.core.platform_database_access.get_database_server", lambda _: "rig-db-rw")

    await restrict_platform_databases()

    rollen_per_database = {
        aanroep.args[0]: aanroep.args[1] for aanroep in connector.restrict_database_to_roles.await_args_list
    }
    assert APP_USER in rollen_per_database[settings.DATABASE_NAME]
    # De admin-credentials blijven de weg naar de server: hij moet de REVOKE mogen doen.
    assert connector.close.await_count == 1


# --- de opstartcontrole ----------------------------------------------------


def _pool_met_rol(**kolommen: Any) -> MagicMock:
    rij = {"naam": APP_USER, "rolsuper": False, "rolcreaterole": False, "rolcreatedb": False, **kolommen}
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=rij)
    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=conn)
    pool.release = AsyncMock()
    return pool


@pytest.mark.asyncio
async def test_een_rol_zonder_extra_rechten_komt_erdoor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "odcn-production")

    await _verify_app_role(_pool_met_rol())


@pytest.mark.parametrize("recht", ["rolsuper", "rolcreaterole", "rolcreatedb"])
@pytest.mark.asyncio
async def test_te_veel_rechten_weigert_de_start(recht: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "odcn-production")

    with pytest.raises(DatabasePoolError, match=recht):
        await _verify_app_role(_pool_met_rol(**{recht: True}))


@pytest.mark.asyncio
async def test_de_admin_rol_als_app_rol_weigert_de_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ook zonder superuser-vlag: wie als de admin-rol binnenkomt heeft het beheerpad."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "odcn-production")

    with pytest.raises(DatabasePoolError, match="admin-rol"):
        await _verify_app_role(_pool_met_rol(naam="postgres"))


@pytest.mark.asyncio
async def test_lokaal_blijft_het_een_waarschuwing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """docker-compose.dev.yaml draait met de admin-gebruiker als app-gebruiker."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")

    with caplog.at_level("WARNING", logger="opi.core.database_pools"):
        await _verify_app_role(_pool_met_rol(naam="postgres", rolsuper=True))

    assert any("DATABASE_APP_USER" in record.getMessage() for record in caplog.records)


# --- de overdracht van het eigendom ---------------------------------------


def _connector_met_statements(per_soort: dict[str, list[str]]) -> tuple[Any, MagicMock]:
    from opi.connectors.postgres import PostgresConnector

    conn = MagicMock()

    async def fetch(vraag: str, *_: Any) -> list[tuple[str]]:
        for soort, statements in per_soort.items():
            if PostgresConnector._OWNER_TRANSFER_QUERIES[soort] == vraag:
                return [(statement,) for statement in statements]
        return []

    conn.fetch = AsyncMock(side_effect=fetch)
    conn.execute = AsyncMock()
    connector = PostgresConnector("rig-db-rw", "postgres", "superuser-geheim")
    return connector, conn


@pytest.mark.asyncio
async def test_de_overdracht_verhuist_wat_nog_van_de_admin_is() -> None:
    statements = {
        "database": ['ALTER DATABASE "operations_manager" OWNER TO "operations_manager"'],
        "schema": ['ALTER SCHEMA "public" OWNER TO "operations_manager"'],
        "relation": [
            'ALTER TABLE public.async_tasks OWNER TO "operations_manager"',
            'ALTER TABLE public.alembic_version OWNER TO "operations_manager"',
        ],
    }
    connector, conn = _connector_met_statements(statements)

    with patch.object(connector, "_get_or_create_connection", AsyncMock(return_value=conn)):
        verhuisd = await connector.transfer_public_objects(
            "operations_manager", new_owner=APP_USER, current_owner="postgres"
        )

    assert verhuisd == {"database": 1, "schema": 1, "relation": 2, "type": 0, "routine": 0}
    uitgevoerd = [aanroep.args[0] for aanroep in conn.execute.await_args_list]
    assert uitgevoerd == [*statements["database"], *statements["schema"], *statements["relation"]]


@pytest.mark.asyncio
async def test_de_overdracht_doet_niets_als_het_eigendom_al_klopt() -> None:
    """Idempotent, want de entrypoint draait hem bij elke start."""
    connector, conn = _connector_met_statements({})

    with patch.object(connector, "_get_or_create_connection", AsyncMock(return_value=conn)):
        verhuisd = await connector.transfer_public_objects(
            "operations_manager", new_owner=APP_USER, current_owner="postgres"
        )

    assert set(verhuisd.values()) == {0}
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_de_overdracht_vraagt_de_catalogus_met_de_twee_rollen() -> None:
    """De filter op de HUIDIGE eigenaar is wat de stap idempotent maakt."""
    connector, conn = _connector_met_statements({})

    with patch.object(connector, "_get_or_create_connection", AsyncMock(return_value=conn)):
        await connector.transfer_public_objects("operations_manager", new_owner=APP_USER, current_owner="postgres")

    for aanroep in conn.fetch.await_args_list:
        assert aanroep.args[1:] == (APP_USER, "postgres")


# --- de entrypoint-stap (opi/core/app_database_owner.py) ------------------


@pytest.mark.asyncio
async def test_ensure_app_database_owner_verbindt_als_admin_en_sluit_af(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """De stap draait als admin: alleen die rol mag het eigendom overdragen."""
    connector = MagicMock()
    connector.transfer_public_objects = AsyncMock(return_value={"relation": 2})
    connector.close = AsyncMock()
    gebouwd: list[tuple[Any, ...]] = []

    def _create(*args: Any) -> MagicMock:
        gebouwd.append(args)
        return connector

    monkeypatch.setattr("opi.core.app_database_owner.create_postgres_connector", _create)

    uitkomst = await ensure_app_database_owner()

    assert uitkomst == {"relation": 2}
    assert gebouwd == [(settings.DATABASE_HOST, "postgres", "superuser-geheim")]
    connector.transfer_public_objects.assert_awaited_once_with(
        settings.DATABASE_NAME, new_owner=APP_USER, current_owner="postgres"
    )
    connector.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_app_database_owner_sluit_ook_af_als_de_overdracht_faalt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = MagicMock()
    connector.transfer_public_objects = AsyncMock(side_effect=PostgresExecutionError("boom"))
    connector.close = AsyncMock()
    monkeypatch.setattr("opi.core.app_database_owner.create_postgres_connector", lambda *a: connector)

    with pytest.raises(PostgresExecutionError):
        await ensure_app_database_owner()

    connector.close.assert_awaited_once()


def test_main_geeft_0_bij_succes(monkeypatch: pytest.MonkeyPatch) -> None:
    def _run(coro: Any) -> dict[str, int]:
        coro.close()  # nooit echt starten, maar wel de coroutine-waarschuwing voorkomen
        return {"relation": 0}

    monkeypatch.setattr("opi.core.app_database_owner.asyncio.run", _run)

    assert owner_main() == 0


@pytest.mark.parametrize("fout", [PostgresExecutionError("boom"), PostgresValidationError("boom")])
def test_main_geeft_1_als_de_overdracht_faalt(fout: Exception, monkeypatch: pytest.MonkeyPatch) -> None:
    """De entrypoint leest alleen de afsluitcode: geen fout mag hier ontsnappen."""

    def _run(coro: Any) -> None:
        coro.close()
        raise fout

    monkeypatch.setattr("opi.core.app_database_owner.asyncio.run", _run)

    assert owner_main() == 1
