"""Shared SQLAlchemy declarative base for service-owned ORM models (RC-5).

This is the foundation for phased ORM adoption. Today the app talks to Postgres with
raw asyncpg SQL and hand-written Alembic migrations; the tables are created from
``*_TABLE_SQL`` constants. A service that owns a table can now ALSO declare it as an
ORM model on this ``Base``, which makes it the schema-as-code source of truth and lets
Alembic autogenerate future migrations for it.

Coexistence is deliberate and safe: ``migrations/env.py`` points autogenerate at
``Base.metadata`` but scopes it (via ``include_object``) to ONLY the tables declared as
ORM models here. The raw-SQL tables (async_tasks, users, runs, marked_for_deletion) are
not on this metadata and are left untouched by autogenerate. Services can be migrated to
the ORM one table at a time without a big-bang rewrite.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base every service-owned ORM model inherits from.

    Import the model modules so they register on ``Base.metadata`` before Alembic reads
    it (see ``opi/services/persistence/__init__.py``)."""


def include_orm_object(object_: Any, name: str, type_: str, reflected: bool, compare_to: Any) -> bool:
    """Alembic ``include_object`` hook that scopes autogenerate to ORM-managed tables.

    A table is managed iff it is on :data:`Base.metadata`; indexes/constraints/columns
    are managed iff their owning table is. Everything else -- the raw-SQL tables
    (async_tasks, users, runs, marked_for_deletion) and their objects -- is ignored, so
    autogenerate never proposes dropping them. Shared by ``migrations/env.py`` and the
    schema-drift check (``scripts/check_orm_schema.py``)."""
    if type_ == "table":
        return name in Base.metadata.tables
    table = getattr(object_, "table", None)
    if table is not None:
        return table.name in Base.metadata.tables
    return True
