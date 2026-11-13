"""The boot finds the Alembic configuration it migrates with, and a failed migration is not ready."""

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from opi.core import database_pools
from opi.core.readiness import ReadinessState
from opi.runtime.startup import startup
from opi.runtime.startup.startup import ALEMBIC_INI, _setup_database

if TYPE_CHECKING:
    import pytest


def test_the_alembic_ini_the_boot_migrates_with_exists() -> None:
    assert ALEMBIC_INI.is_file(), ALEMBIC_INI


async def test_a_failed_migration_leaves_the_database_not_ready_also_on_a_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    available = iter([False, True])
    monkeypatch.setattr(database_pools, "is_database_available", lambda: next(available))
    monkeypatch.setattr(startup, "initialize_database_pools", AsyncMock())

    def failing_upgrade() -> None:
        raise RuntimeError("relation users does not exist")

    monkeypatch.setattr(startup, "_run_alembic_migrations", failing_upgrade)
    readiness = ReadinessState()

    assert await _setup_database(readiness) is False
    assert readiness.database.ready is False
    assert await _setup_database(readiness) is False
    assert readiness.database.ready is False


async def test_the_migration_runs_again_when_the_pools_already_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(database_pools, "is_database_available", lambda: True)
    initialize = AsyncMock()
    monkeypatch.setattr(startup, "initialize_database_pools", initialize)
    migrations: list[None] = []
    monkeypatch.setattr(startup, "_run_alembic_migrations", lambda: migrations.append(None))
    readiness = ReadinessState()

    assert await _setup_database(readiness) is True
    assert readiness.database.ready is True
    assert len(migrations) == 1
    initialize.assert_not_awaited()
