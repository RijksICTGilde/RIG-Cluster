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

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncEngine


class Base(DeclarativeBase):
    """Declarative base every service-owned ORM model inherits from.

    Import the model modules so they register on ``Base.metadata`` before Alembic reads
    it (see ``opi/services/persistence/__init__.py``)."""


# --- async engine / session for ORM query execution (RC-5 persistence phase 2) -------
# A dedicated SQLAlchemy async engine (asyncpg driver) used by service-owned ORM
# repositories. It is separate from the raw asyncpg pools in ``core/database_pools`` --
# service persistence being migrated to the ORM uses this; everything else stays on the
# pools. Lazily created from settings; tests point it at a throwaway Postgres via
# ``configure_engine``.

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _default_url() -> str:
    from opi.core.config import settings

    return (
        f"postgresql+asyncpg://{settings.DATABASE_ADMIN_NAME}:{settings.DATABASE_ADMIN_PASSWORD}"
        f"@{settings.DATABASE_HOST}:5432/{settings.DATABASE_NAME}"
    )


def configure_engine(url: str | None = None) -> None:
    """(Re)create the async engine + sessionmaker. ``url`` defaults to the app DB; tests
    pass a throwaway-Postgres URL. Safe to call more than once."""
    global _engine, _sessionmaker
    _engine = create_async_engine(url or _default_url(), pool_pre_ping=True)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """The session factory, creating the engine from settings on first use."""
    if _sessionmaker is None:
        configure_engine()
    if _sessionmaker is None:  # pragma: no cover -- configure_engine always sets it
        raise RuntimeError("async engine not configured")
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession]:
    """Async session context manager that commits on success, rolls back on error.

    Repositories use ``async with session_scope() as session:``; a query-only block
    still commits harmlessly (no-op)."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def _current_engine() -> AsyncEngine:
    get_sessionmaker()  # ensures the engine exists
    if _engine is None:  # pragma: no cover
        raise RuntimeError("async engine not configured")
    return _engine


async def create_all_orm_tables() -> None:
    """Create every ORM-modeled table on the current engine (used by tests). Idempotent."""
    async with _current_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def dispose_engine() -> None:
    """Dispose the engine's connection pool (call on shutdown / test teardown)."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


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
