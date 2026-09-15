"""Migration 006 on a real Postgres: the table, its index, and the ORM model agree."""

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
from sqlalchemy import Engine, create_engine, text

if TYPE_CHECKING:
    from collections.abc import Iterator

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def upgrade(engine: Engine, destination: str) -> None:
    """``alembic upgrade <destination>`` on this engine, without env.py's settings-built URL."""
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_INI.parent / "opi" / "migrations"))
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


@pytest.fixture
def migration_db(_orm_db_url: str) -> Iterator[str]:
    """An EMPTY database next to the one of this run, as a sync (psycopg2) URL."""
    server, _, _name = _orm_db_url.rpartition("/")
    server = server.replace("postgresql+asyncpg", "postgresql+psycopg2")
    # Eigen prefix: de weesopruimer in conftest leest een pid uit zad_test_<pid> en zou
    # een naam met achtervoegsel als dood opruimen terwijl deze run hem gebruikt.
    name = f"zad_mig_{os.getpid()}"
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


def test_upgrade_head_on_an_empty_database_matches_the_orm_models(migration_db: str) -> None:
    engine = create_engine(migration_db)
    try:
        upgrade(engine, "head")
        with engine.connect() as conn:
            context = MigrationContext.configure(
                conn, opts={"include_object": include_orm_object, "target_metadata": Base.metadata}
            )
            drift = [d for d in compare_metadata(context, Base.metadata) if "project_reconciliation" in repr(d)]
            index = conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_project_reconciliation_scope'")
            ).scalar_one()
    finally:
        engine.dispose()

    assert drift == []
    assert "UNIQUE" in index
    assert "COALESCE(deployment_name, ''::character varying)" in index


def test_project_wide_is_unique_although_it_is_null(migration_db: str) -> None:
    """NULL is not equal to NULL in a plain unique constraint; the COALESCE index makes it so."""
    engine = create_engine(migration_db)
    try:
        upgrade(engine, "head")
        insert = text(
            "INSERT INTO project_reconciliation (project_name, deployment_name, reconciled_at) "
            "VALUES ('p1', :dep, now())"
        )
        with engine.connect() as conn:
            conn.execute(insert, {"dep": None})
            conn.execute(insert, {"dep": "d1"})
            conn.commit()
            with pytest.raises(Exception, match="idx_project_reconciliation_scope"):
                conn.execute(insert, {"dep": None})
    finally:
        engine.dispose()
