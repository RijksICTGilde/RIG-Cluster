"""Check that ORM-modeled tables match the live database (phased ORM adoption, RC-5).

Runs Alembic's autogenerate comparison scoped to ORM-managed tables (via
``include_orm_object``) against the configured database. Exits 0 when there is no drift
for those tables -- proving the SQLAlchemy models match the DB and that a future
``alembic revision --autogenerate`` would be empty for them. Any table not declared as an
ORM model is ignored, so it never shows up.

Run from ``operations-manager/python`` with DB access (e.g. inside the OPI pod):

    uv run python scripts/check_orm_schema.py
"""

from __future__ import annotations

import sys

import opi.services.persistence  # noqa: F401  -- registers service ORM models on Base.metadata
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from opi.core.config import settings
from opi.core.db import Base, include_orm_object
from sqlalchemy import create_engine


def _url() -> str:
    return (
        f"postgresql+psycopg2://{settings.DATABASE_ADMIN_NAME}:{settings.DATABASE_ADMIN_PASSWORD}"
        f"@{settings.DATABASE_HOST}:5432/{settings.DATABASE_NAME}"
    )


def main() -> int:
    engine = create_engine(_url())
    with engine.connect() as conn:
        ctx = MigrationContext.configure(
            conn,
            opts={"include_object": include_orm_object, "target_metadata": Base.metadata},
        )
        diff = compare_metadata(ctx, Base.metadata)
    if diff:
        print("ORM schema DRIFT detected (model != database):")
        for d in diff:
            print("   ", d)
        return 1
    print(f"OK: ORM-modeled tables match the database, no drift. Managed: {sorted(Base.metadata.tables)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
