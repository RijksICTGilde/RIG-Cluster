"""Migratie 009 op een echte Postgres: heen en terug, leeg en gevuld (RC-329).

Dezelfde opzet als ``test_migration_006_project_reconciliation.py``: een lege database
naast die van de run, en ``alembic upgrade`` op die database in plaats van op de URL die
``env.py`` uit de settings bouwt.
"""

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
from opi.services.user_otp_service import OTP_COLUMNS_CLEARED_BY_RESET
from sqlalchemy import Engine, create_engine, text

if TYPE_CHECKING:
    from collections.abc import Iterator

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def _config() -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_INI.parent / "opi" / "migrations"))
    return config


def _run(engine: Engine, destination: str, down: bool) -> None:
    config = _config()
    script = ScriptDirectory.from_config(config)

    def steps(rev, _context):
        return script._downgrade_revs(destination, rev) if down else script._upgrade_revs(destination, rev)

    with (
        engine.connect() as connection,
        EnvironmentContext(config, script, fn=steps, destination_rev=destination) as env,
    ):
        env.configure(connection=connection, target_metadata=Base.metadata)
        with env.begin_transaction():
            env.run_migrations()
        connection.commit()


def upgrade(engine: Engine, destination: str) -> None:
    """``alembic upgrade <destination>`` op deze engine, zonder de URL uit env.py."""
    _run(engine, destination, down=False)


def downgrade(engine: Engine, destination: str) -> None:
    """``alembic downgrade <destination>`` op deze engine."""
    _run(engine, destination, down=True)


@pytest.fixture
def migration_db(_orm_db_url: str) -> Iterator[str]:
    """Een LEGE database naast die van deze run, als sync (psycopg2) URL."""
    server, _, _name = _orm_db_url.rpartition("/")
    server = server.replace("postgresql+asyncpg", "postgresql+psycopg2")
    name = f"zad_mig009_{os.getpid()}"
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


def _columns(engine: Engine, table: str) -> dict[str, str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = :t"),
            {"t": table},
        ).all()
    return dict(rows)


def test_upgrade_head_on_an_empty_database_matches_the_orm_models(migration_db: str) -> None:
    engine = create_engine(migration_db)
    try:
        upgrade(engine, "head")
        with engine.connect() as conn:
            context = MigrationContext.configure(
                conn, opts={"include_object": include_orm_object, "target_metadata": Base.metadata}
            )
            drift = [d for d in compare_metadata(context, Base.metadata) if "otp" in repr(d).lower()]
        users = _columns(engine, "users")
    finally:
        engine.dispose()

    assert drift == []
    for kolom in OTP_COLUMNS_CLEARED_BY_RESET:
        assert kolom in users
    assert users["otp_last_used_step"] == "bigint"


def test_upgrade_on_a_filled_users_table_keeps_the_rows(migration_db: str) -> None:
    """Een bestaande database heeft de kolommen nog niet: 009 voegt ze toe met defaults."""
    engine = create_engine(migration_db)
    try:
        upgrade(engine, "008")
        with engine.connect() as conn:
            conn.execute(text("INSERT INTO users (email, full_name) VALUES ('a@example.nl', 'Alice')"))
            conn.commit()
        upgrade(engine, "head")
        with engine.connect() as conn:
            rij = conn.execute(
                text(
                    "SELECT full_name, otp_secret, otp_key_version, otp_confirmed_at, "
                    "otp_last_used_step, otp_failed_attempts, otp_locked_until FROM users"
                )
            ).one()
    finally:
        engine.dispose()

    assert rij == ("Alice", None, 1, None, None, 0, None)


def test_downgrade_removes_the_columns_and_keeps_the_users(migration_db: str) -> None:
    """Terug naar 008, niet ``-1``: met een migratie bovenop 009 draait ``-1`` die terug."""
    engine = create_engine(migration_db)
    try:
        upgrade(engine, "head")
        with engine.connect() as conn:
            conn.execute(text("INSERT INTO users (email, full_name) VALUES ('a@example.nl', 'Alice')"))
            conn.commit()
        downgrade(engine, "008")
        users = _columns(engine, "users")
        with engine.connect() as conn:
            namen = conn.execute(text("SELECT full_name FROM users")).scalars().all()
    finally:
        engine.dispose()

    assert [k for k in users if k.startswith("otp")] == []
    assert namen == ["Alice"]
