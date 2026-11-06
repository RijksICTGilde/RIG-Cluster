"""Migration 014 (api_step_ups) on a real Postgres: there and back, and matching the model."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import opi.services.persistence  # noqa: F401  -- registers the ORM models on Base.metadata
import pytest
from opi.services.persistence.api_step_ups import ApiStepUp
from sqlalchemy import create_engine, text
from tests.test_migration_009_user_otp import downgrade, upgrade

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def migration_db(_orm_db_url: str) -> Iterator[str]:
    server, _, _name = _orm_db_url.rpartition("/")
    server = server.replace("postgresql+asyncpg", "postgresql+psycopg2")
    name = f"zad_mig014_{os.getpid()}"
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


def _columns(conn: object) -> set[str]:
    query = text("SELECT column_name FROM information_schema.columns WHERE table_name = 'api_step_ups'")
    return {row[0] for row in conn.execute(query)}  # type: ignore[attr-defined]


def test_014_runs_there_and_back_and_matches_the_model(migration_db: str) -> None:
    engine = create_engine(migration_db)
    try:
        upgrade(engine, "014")
        with engine.connect() as conn:
            assert _columns(conn) == {column.name for column in ApiStepUp.__table__.columns}
        downgrade(engine, "013")
        with engine.connect() as conn:
            assert _columns(conn) == set()
    finally:
        engine.dispose()
