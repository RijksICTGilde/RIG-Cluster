"""Shared SQLAlchemy declarative base for service-owned ORM models (RC-5).

A service that owns a table declares it as an ORM model on this ``Base``, which makes the
model the schema source of truth and lets Alembic autogenerate migrations for it. Next to
it the app still talks to Postgres with raw asyncpg SQL, and those tables are created from
``*_TABLE_SQL`` constants.

``migrations/env.py`` points autogenerate at ``Base.metadata`` but scopes it (via
``include_object``) to ONLY the tables declared here, so a table that is not an ORM model
is left untouched. Today every platform table (async_tasks, users, runs,
marked_for_deletion, subdomain_registry, project_reconciliation) is modeled.
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


# A dedicated SQLAlchemy async engine (asyncpg driver) for the service-owned ORM
# repositories, separate from the raw asyncpg pools in ``core/database_pools``: service
# persistence that is being migrated to the ORM uses this, everything else stays on the
# pools. Lazily created from settings; tests point it at a throwaway Postgres via
# ``configure_engine``.

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None

#: The ceiling of this engine: ``ORM_POOL_SIZE`` connections, without overflow. Together with
#: the main asyncpg pool (``MAIN_POOL_MAX_SIZE`` in ``opi/core/database_pools.py``) an OPI pod
#: opens at most 10 connections to its own database, and the ``connectionLimit`` of the role
#: ``operations_manager`` in CNPG is calculated on that (RC-410).
ORM_POOL_SIZE = 5
ORM_MAX_OVERFLOW = 0


def app_database_url(scheme: str, ssl_param: str) -> str:
    """The URL of OPI's own database, with the app role that owns its objects.

    The app role and not the superuser: OPI's own tables belong to the role that uses them.
    Managing tenant databases keeps the admin credentials.

    Shared with ``opi/migrations/env.py``, because the migrations run as that same role.
    ``ssl_param`` differs per driver: asyncpg calls it ``ssl``, psycopg2 ``sslmode``; the
    values are libpq's in both cases.
    """
    from opi.core.config import settings

    return (
        f"{scheme}://{settings.DATABASE_APP_USER}:{settings.database_app_password}"
        f"@{settings.DATABASE_HOST}:5432/{settings.DATABASE_NAME}"
        f"?{ssl_param}={settings.DATABASE_SSLMODE}"
    )


def _default_url() -> str:
    return app_database_url("postgresql+asyncpg", "ssl")


def configure_engine(url: str | None = None) -> None:
    """(Re)create the async engine + sessionmaker. ``url`` defaults to the app DB; tests
    pass a throwaway-Postgres URL. Safe to call more than once."""
    global _engine, _sessionmaker
    _engine = create_async_engine(
        url or _default_url(),
        pool_pre_ping=True,
        pool_size=ORM_POOL_SIZE,
        max_overflow=ORM_MAX_OVERFLOW,
    )
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
    are managed iff their owning table is. Everything else -- any table not declared as an
    ORM model, and its objects -- is ignored, so autogenerate never proposes dropping it.
    Shared by ``migrations/env.py`` and the schema-drift check
    (``scripts/check_orm_schema.py``)."""
    if type_ == "table":
        return name in Base.metadata.tables
    table = getattr(object_, "table", None)
    if table is not None:
        return table.name in Base.metadata.tables
    return True
