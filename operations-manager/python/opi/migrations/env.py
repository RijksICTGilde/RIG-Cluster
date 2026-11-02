from alembic import context
from sqlalchemy import create_engine, pool

import opi.services.persistence  # noqa: F401  -- registers service ORM models on Base.metadata
from opi.core.db import Base, app_database_url, include_orm_object

# Autogenerate target. Only tables declared as ORM models on Base.metadata are managed;
# any table not declared as an ORM model is left untouched by autogenerate via
# include_orm_object. This is phased ORM adoption -- see opi/core/db.py.
target_metadata = Base.metadata


def _build_url() -> str:
    """De URL van OPI's eigen database, met de app-rol die haar objecten bezit.

    De migraties draaien als die rol en niet als de superuser; de overdrachtsstap in
    ``opi/core/app_database_owner.py`` zorgt dat ze alles bezit wat ze wijzigt (RC-410).
    """
    return app_database_url("postgresql+psycopg2", "sslmode")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode for SQL script generation."""
    context.configure(
        url=_build_url(),
        target_metadata=target_metadata,
        include_object=include_orm_object,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database."""
    connectable = create_engine(_build_url(), poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_orm_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
