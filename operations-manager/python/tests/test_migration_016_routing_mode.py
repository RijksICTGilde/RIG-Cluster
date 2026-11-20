"""Migration 016 (routing_mode) on a real Postgres: there and back, and matching the model."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from opi.services.persistence.routing_mode import RoutingMode
from sqlalchemy import create_engine, text
from tests.test_migration_009_user_otp import downgrade, upgrade

if TYPE_CHECKING:
    from collections.abc import Iterator

TABLES = (RoutingMode,)


@pytest.fixture
def migration_db(_orm_db_url: str) -> Iterator[str]:
    server, _, _name = _orm_db_url.rpartition("/")
    server = server.replace("postgresql+asyncpg", "postgresql+psycopg2")
    name = f"zad_mig016_{os.getpid()}"
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


def _columns(conn: object, table: str) -> set[str]:
    query = text("SELECT column_name FROM information_schema.columns WHERE table_name = :table")
    return {row[0] for row in conn.execute(query, {"table": table})}  # type: ignore[attr-defined]


def test_016_runs_there_and_back_and_matches_the_model(migration_db: str) -> None:
    engine = create_engine(migration_db)
    try:
        upgrade(engine, "016")
        with engine.connect() as conn:
            for model in TABLES:
                expected = {column.name for column in model.__table__.columns}
                assert _columns(conn, model.__tablename__) == expected
        downgrade(engine, "015")
        with engine.connect() as conn:
            for model in TABLES:
                assert _columns(conn, model.__tablename__) == set()
    finally:
        engine.dispose()
