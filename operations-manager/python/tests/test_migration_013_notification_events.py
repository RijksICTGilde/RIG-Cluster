"""Migratie 013 (notification_events) op een echte Postgres: heen en terug.

Dezelfde opzet als ``test_migration_012_otp_herlogins.py``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import opi.services.persistence  # noqa: F401  -- registers the ORM models on Base.metadata
import pytest
from alembic.script import ScriptDirectory
from opi.services.persistence.notification_events import NotificationEvent
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from collections.abc import Iterator

from tests.test_migration_009_user_otp import _config, downgrade, upgrade

KOLOMMEN = {
    "id",
    "source",
    "specversion",
    "type",
    "subject",
    "time",
    "category",
    "severity",
    "cluster",
    "project",
    "deployment",
    "component",
    "actor",
    "actor_kind",
    "origin",
    "summary",
    "flow_id",
    "task_id",
    "thread_key",
    "dedup_key",
    "data",
}


@pytest.fixture
def migration_db(_orm_db_url: str) -> Iterator[str]:
    """Een lege database naast die van deze run."""
    server, _, _name = _orm_db_url.rpartition("/")
    server = server.replace("postgresql+asyncpg", "postgresql+psycopg2")
    name = f"zad_mig013_{os.getpid()}"
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


def test_013_draait_heen_en_terug(migration_db: str) -> None:
    engine = create_engine(migration_db)
    try:
        upgrade(engine, "head")
        with engine.connect() as conn:
            kolommen = {
                rij[0]
                for rij in conn.execute(
                    text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
                    {"t": "notification_events"},
                )
            }
        assert kolommen == KOLOMMEN

        downgrade(engine, "012")
        with engine.connect() as conn:
            weg = conn.execute(text("SELECT to_regclass('notification_events')")).scalar()
        assert weg is None

        upgrade(engine, "013")
        with engine.connect() as conn:
            terug = conn.execute(text("SELECT to_regclass('notification_events')")).scalar()
        assert terug is not None
    finally:
        engine.dispose()


def test_013_volgt_op_012_en_is_de_enige_head() -> None:
    script = ScriptDirectory.from_config(_config())
    revisie = script.get_revision("013")
    assert revisie.down_revision == "012"
    assert len(script.get_revisions("head")) == 1, "twee heads: iemand nam een nummer zonder vast te leggen"


def test_013_matcht_het_orm_model(migration_db: str) -> None:
    """Wat de migratie aanmaakt is wat het model verwacht: niet-nullable en de indexen."""
    engine = create_engine(migration_db)
    try:
        upgrade(engine, "013")
        with engine.connect() as conn:
            nietnul = {
                rij[0]
                for rij in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = :t AND is_nullable = 'NO'"
                    ),
                    {"t": NotificationEvent.__tablename__},
                )
            }
            indexen = {
                rij[0]
                for rij in conn.execute(
                    text("SELECT indexname FROM pg_indexes WHERE tablename = :t"),
                    {"t": NotificationEvent.__tablename__},
                )
            }
        verwacht_nietnul = {kolom.name for kolom in NotificationEvent.__table__.columns if not kolom.nullable}
        assert verwacht_nietnul == nietnul
        assert {index.name for index in NotificationEvent.__table__.indexes} <= indexen
    finally:
        engine.dispose()


def test_013_sweep_index_is_partieel(migration_db: str) -> None:
    """De sweep-index staat op het predicaat waar een gepseudonimiseerde rij uit valt."""
    engine = create_engine(migration_db)
    try:
        upgrade(engine, "013")
        with engine.connect() as conn:
            definitie = conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"),
                {"n": "idx_notification_events_actor_sweep"},
            ).scalar_one()
        assert "WHERE" in definitie
        assert "actor IS NOT NULL" in definitie
        assert "origin IS NOT NULL" in definitie
    finally:
        engine.dispose()
