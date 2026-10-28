"""Migratie 012 (otp_herlogins) op een echte Postgres: heen en terug.

Dezelfde opzet als ``test_migration_009_user_otp.py`` en ``test_migration_007_details_purged_at.py``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import opi.services.persistence  # noqa: F401  -- registers the ORM models on Base.metadata
import pytest
from alembic.script import ScriptDirectory
from opi.services.persistence.otp_herlogin import OtpHerlogin
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine

from tests.test_migration_009_user_otp import _config, downgrade, upgrade

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


@pytest.fixture
def migration_db(_orm_db_url: str) -> Iterator[str]:
    """Een lege database naast die van deze run."""
    server, _, _name = _orm_db_url.rpartition("/")
    server = server.replace("postgresql+asyncpg", "postgresql+psycopg2")
    name = f"zad_mig012_{os.getpid()}"
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


def _head_has(deels: str, engine: Engine) -> bool:

    script = ScriptDirectory.from_config(_config())
    heads = script.get_revisions("head")
    return any(heads) and deels in repr(heads)


def test_012_draait_heen_en_terug(migration_db: str) -> None:
    engine = create_engine(migration_db)
    try:
        upgrade(engine, "head")
        with engine.connect() as conn:
            kolommen = {
                rij[0]
                for rij in conn.execute(
                    text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
                    {"t": "otp_herlogins"},
                )
            }
        assert {
            "state",
            "email",
            "login_at",
            "nonce",
            "code_verifier",
            "created_at",
            "expires_at",
            "verified_at",
        } <= kolommen

        downgrade(engine, "011")
        with engine.connect() as conn:
            weg = conn.execute(text("SELECT to_regclass('otp_herlogins')")).scalar()
        assert weg is None

        upgrade(engine, "012")
        with engine.connect() as conn:
            terug = conn.execute(text("SELECT to_regclass('otp_herlogins')")).scalar()
        assert terug is not None
    finally:
        engine.dispose()


def test_012_volgt_op_011_en_is_de_enige_head() -> None:

    script = ScriptDirectory.from_config(_config())
    revisie = script.get_revision("012")
    assert revisie.down_revision == "011"
    assert len(script.get_revisions("head")) == 1, "twee heads: iemand nam een nummer zonder vast te leggen"


def test_012_matcht_het_orm_model(migration_db: str) -> None:
    """Wat de migratie aanmaakt is wat het model verwacht (naam en niet-nullable samenstelling)."""
    engine = create_engine(migration_db)
    try:
        upgrade(engine, "012")
        with engine.connect() as conn:
            nietnul = {
                rij[0]
                for rij in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = :t AND is_nullable = 'NO'"
                    ),
                    {"t": OtpHerlogin.__tablename__},
                )
            }
        assert {"state", "email", "login_at", "nonce", "code_verifier", "created_at", "expires_at"} == nietnul
    finally:
        engine.dispose()
