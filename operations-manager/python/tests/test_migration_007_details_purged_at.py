"""Migration 007 on a real Postgres: upgrade and downgrade van de ``details_purged_at`` kolom."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import opi.services.persistence  # noqa: F401  -- registers the ORM models on Base.metadata
import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from opi.core.db import Base, include_orm_object
from sqlalchemy import Engine, create_engine, inspect, text

if TYPE_CHECKING:
    from collections.abc import Iterator

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def _config() -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_INI.parent / "opi" / "migrations"))
    return config


def upgrade(engine: Engine, destination: str) -> None:
    """``alembic upgrade <destination>`` on this engine, without env.py's settings-built URL."""
    config = _config()
    script = ScriptDirectory.from_config(config)

    def steps(rev, _context):
        return script._upgrade_revs(destination, rev)

    with (
        engine.connect() as connection,
        EnvironmentContext(config, script, fn=steps, destination_rev=destination) as env,
    ):
        env.configure(connection=connection, target_metadata=Base.metadata)
        with env.begin_transaction():
            env.run_migrations()
        connection.commit()


def downgrade(engine: Engine, destination: str) -> None:
    """``alembic downgrade <destination>`` on this engine."""
    config = _config()
    script = ScriptDirectory.from_config(config)

    def steps(rev, _context):
        return script._downgrade_revs(destination, rev)

    with (
        engine.connect() as connection,
        EnvironmentContext(config, script, fn=steps, destination_rev=destination) as env,
    ):
        env.configure(connection=connection, target_metadata=Base.metadata)
        with env.begin_transaction():
            env.run_migrations()
        connection.commit()


@pytest.fixture
def migration_db(_orm_db_url: str) -> Iterator[str]:
    """An EMPTY database next to the one of this run, as a sync (psycopg2) URL."""
    server, _, _name = _orm_db_url.rpartition("/")
    server = server.replace("postgresql+asyncpg", "postgresql+psycopg2")
    # Eigen prefix: de weesopruimer in conftest leest een pid uit zad_test_<pid> en zou
    # een naam met achtervoegsel als dood opruimen terwijl deze run hem gebruikt.
    name = f"zad_mig007_{os.getpid()}"
    admin = create_engine(f"{server}/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield f"{server}/{name}"
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_upgrade_head_on_an_empty_database_matches_the_orm_model(migration_db: str) -> None:
    engine = create_engine(migration_db)
    try:
        upgrade(engine, "head")
        with engine.connect() as conn:
            context = MigrationContext.configure(
                conn, opts={"include_object": include_orm_object, "target_metadata": Base.metadata}
            )
            drift = [d for d in compare_metadata(context, Base.metadata) if "async_tasks" in repr(d)]
    finally:
        engine.dispose()

    assert drift == []


def test_upgrade_to_head_from_the_previous_revision_succeeds(migration_db: str) -> None:
    """Een database die al op 006 staat, moet probleemloos naar head kunnen.

    ``details_purged_at`` zit al in de baseline-SQL van 001 (zelfde patroon als
    ``affects_deployments`` in migratie 005), dus 007's ``ADD COLUMN IF NOT EXISTS`` is
    hier een no-op. Wat deze test dekt is dat die herhaling niet alsnog faalt.
    """
    engine = create_engine(migration_db)
    try:
        upgrade(engine, "006")
        upgrade(engine, "head")
        with engine.connect() as conn:
            columns = {c["name"] for c in inspect(conn).get_columns("async_tasks")}
    finally:
        engine.dispose()

    assert "details_purged_at" in columns


def test_downgrade_removes_the_column_and_upgrade_adds_it_back(migration_db: str) -> None:
    engine = create_engine(migration_db)
    try:
        upgrade(engine, "head")
        downgrade(engine, "-1")
        with engine.connect() as conn:
            columns_after_downgrade = {c["name"] for c in inspect(conn).get_columns("async_tasks")}

        upgrade(engine, "head")
        with engine.connect() as conn:
            columns_after_upgrade = {c["name"] for c in inspect(conn).get_columns("async_tasks")}
    finally:
        engine.dispose()

    assert "details_purged_at" not in columns_after_downgrade
    assert "details_purged_at" in columns_after_upgrade
