"""Migratie 011 (sessions) op een echte Postgres: heen en terug.

Dezelfde opzet als ``test_migration_010_otp_pending.py`` en de oudere migratietoetsen.
"""

from __future__ import annotations

import opi.services.persistence  # noqa: F401  -- registreert de ORM-modellen op Base.metadata
from sqlalchemy import create_engine, text
from tests.conftest import alembic_downgrade, alembic_script_directory, alembic_upgrade


def test_011_runs_there_and_back(migration_db: str) -> None:
    engine = create_engine(migration_db)
    try:
        alembic_upgrade(engine, "head")
        with engine.connect() as conn:
            columns = {
                rij[0]
                for rij in conn.execute(
                    text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
                    {"t": "sessions"},
                )
            }
        assert {"id", "data", "created_at", "expires_at", "last_seen"} <= columns

        alembic_downgrade(engine, "010")
        with engine.connect() as conn:
            weg = conn.execute(text("SELECT to_regclass('sessions')")).scalar()
        assert weg is None

        alembic_upgrade(engine, "011")
        with engine.connect() as conn:
            terug = conn.execute(text("SELECT to_regclass('sessions')")).scalar()
        assert terug is not None
    finally:
        engine.dispose()


def test_011_follows_010_and_is_the_only_head() -> None:
    script = alembic_script_directory()
    revisie = script.get_revision("011")
    assert revisie.down_revision == "010"
    assert len(script.get_revisions("head")) == 1, "twee heads: iemand nam een nummer zonder vast te leggen"


def test_011_matches_the_orm_model(migration_db: str) -> None:
    """Wat de migratie aanmaakt is wat het model verwacht (niet-nullable samenstelling)."""
    engine = create_engine(migration_db)
    try:
        alembic_upgrade(engine, "011")
        with engine.connect() as conn:
            non_nullable = {
                rij[0]
                for rij in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = :t AND is_nullable = 'NO'"
                    ),
                    {"t": "sessions"},
                )
            }
        assert {"id", "data", "created_at", "expires_at", "last_seen"} == non_nullable
    finally:
        engine.dispose()


def test_011_column_types_are_session_shaped(migration_db: str) -> None:
    engine = create_engine(migration_db)
    try:
        alembic_upgrade(engine, "011")
        with engine.connect() as conn:
            column_types = dict(
                conn.execute(
                    text("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'sessions'")
                ).all()
            )
    finally:
        engine.dispose()

    assert column_types["id"] == "text"
    assert column_types["data"] == "jsonb"
    assert column_types["expires_at"] == "timestamp with time zone"
