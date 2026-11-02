"""RC-275: de verbindingen die OPI zelf naar een database van het platform opzet dragen
DATABASE_SSLMODE: de PostgresConnector, de asyncpg-pool, de SQLAlchemy-engine, Alembic en
de credentialtoets in de DatabaseManager.

Zonder dit verbindt OPI onversleuteld naar de gedeelde database, want CNPG biedt TLS wel
aan maar dwingt het niet af. Zie features/postgresql-tls.md voor wat er nog buiten valt.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
from opi.connectors.postgres import create_postgres_connector
from opi.core.config import Settings, settings
from opi.core.database_pool import DatabasePool
from opi.core.db import _default_url
from opi.manager.database_manager import DatabaseManager


@pytest.mark.asyncio
async def test_connector_passes_sslmode_to_asyncpg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DATABASE_SSLMODE", "require")
    connector = create_postgres_connector("rig-db-rw", "postgres", "secret")

    with patch("opi.connectors.postgres.asyncpg.connect", new=AsyncMock()) as connect:
        await connector._get_or_create_connection("operations_manager")

    assert connect.await_args is not None
    assert connect.await_args.kwargs["ssl"] == "require"


@pytest.mark.asyncio
async def test_connector_honours_a_configured_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DATABASE_SSLMODE", "disable")
    connector = create_postgres_connector("localhost", "postgres", "secret")

    with patch("opi.connectors.postgres.asyncpg.connect", new=AsyncMock()) as connect:
        await connector._get_or_create_connection()

    assert connect.await_args is not None
    assert connect.await_args.kwargs["ssl"] == "disable"


@pytest.mark.asyncio
async def test_pool_passes_sslmode_to_create_pool() -> None:
    pool = DatabasePool(host="rig-db-rw", user="postgres", password="secret", sslmode="require")

    with patch("opi.core.database_pool.asyncpg.create_pool", new=AsyncMock()) as create_pool:
        await pool.initialize()

    assert create_pool.await_args is not None
    assert create_pool.await_args.kwargs["ssl"] == "require"


def test_orm_url_carries_the_asyncpg_spelling(monkeypatch: pytest.MonkeyPatch) -> None:
    # asyncpg leest de queryparameter ssl, niet sslmode.
    monkeypatch.setattr(settings, "DATABASE_SSLMODE", "require")
    assert _default_url().endswith("?ssl=require")


def test_sslmode_defaults_to_require() -> None:
    assert Settings.model_fields["DATABASE_SSLMODE"].default == "require"


@pytest.mark.asyncio
async def test_credential_check_passes_sslmode_to_asyncpg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DATABASE_SSLMODE", "require")

    with patch("opi.manager.database_manager.asyncpg.connect", new=AsyncMock()) as connect:
        valid = await DatabaseManager._test_database_connection(
            username="proj_dep", password="secret", database="proj_dep", schema="proj_dep", host="rig-db-rw"
        )

    assert valid is True
    assert connect.await_args is not None
    assert connect.await_args.kwargs["ssl"] == "require"


@pytest.mark.asyncio
async def test_credential_check_reports_rejected_credentials_as_invalid() -> None:
    failing = AsyncMock(side_effect=asyncpg.InvalidPasswordError("password authentication failed"))

    with patch("opi.manager.database_manager.asyncpg.connect", new=failing):
        valid = await DatabaseManager._test_database_connection(
            username="proj_dep", password="wrong", database="proj_dep", schema="proj_dep", host="rig-db-rw"
        )

    assert valid is False


@pytest.mark.asyncio
async def test_credential_check_does_not_read_a_tls_failure_as_invalid_credentials() -> None:
    # Anders zet de aanroeper het wachtwoord opnieuw omdat de handshake mislukte.
    failing = AsyncMock(side_effect=ConnectionError("server does not support SSL, but SSL was required"))

    with patch("opi.manager.database_manager.asyncpg.connect", new=failing), pytest.raises(ConnectionError):
        await DatabaseManager._test_database_connection(
            username="proj_dep", password="secret", database="proj_dep", schema="proj_dep", host="rig-db-rw"
        )
