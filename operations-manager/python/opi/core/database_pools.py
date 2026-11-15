"""The asyncpg pools of the application, created and closed with its lifecycle."""

import logging

import asyncpg

from opi.core.config import settings
from opi.core.connect_retry import retry_on_connect_error
from opi.core.database_pool import DatabasePool, DatabasePoolError

logger = logging.getLogger(__name__)

#: The ceiling of the main pool (with the SQLAlchemy engine: 10 connections at most).
MAIN_POOL_MAX_SIZE = 5

#: What a database that does not accept connections raises. PostgresError is in it for the
#: "starting up" and "too many connections" answers, and so also covers a wrong password,
#: which is therefore retried just as long (tests/test_database_pool_retry.py).
_CONNECT_ERRORS = (
    asyncpg.exceptions.PostgresError,
    asyncpg.exceptions.InterfaceError,
    OSError,
    TimeoutError,
)

#: Seven minutes, long enough for a CNPG failover and a cluster that is still starting.
_CONNECT_BUDGET_SECONDS = 420.0

_pools: dict[str, DatabasePool] = {}


async def _initialize_database_pool_with_retry(pool: DatabasePool, name: str) -> None:
    """``name`` only names the pool in the log."""

    async def _initialize() -> None:
        logger.info(f"Attempting to initialize database pool '{name}' (host: {pool.host}:{pool.port})")
        await pool.initialize()
        logger.info(f"Successfully initialized database pool '{name}'")

    await retry_on_connect_error(
        _initialize,
        _CONNECT_BUDGET_SECONDS,
        f"database pool '{name}' at {pool.host}:{pool.port}",
        retry_on=(*_CONNECT_ERRORS, DatabasePoolError),
    )


async def _create_database_if_missing() -> None:
    """Connects to the default 'postgres' database to check or create our own."""
    database_name = settings.DATABASE_NAME
    logger.info(f"Ensuring database '{database_name}' exists...")

    conn = await asyncpg.connect(
        host=settings.DATABASE_HOST,
        user=settings.DATABASE_ADMIN_NAME,
        password=settings.DATABASE_ADMIN_PASSWORD,
        database="postgres",
        port=5432,
        ssl=settings.DATABASE_SSLMODE,
    )

    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1",
            database_name,
        )

        if not exists:
            logger.info(f"Database '{database_name}' does not exist, creating it...")
            # Cannot use parameters for CREATE DATABASE, but database_name is from config
            await conn.execute(f'CREATE DATABASE "{database_name}"')
            logger.info(f"Database '{database_name}' created successfully")
        else:
            logger.info(f"Database '{database_name}' already exists")
    finally:
        await conn.close()


async def _ensure_database_exists() -> None:
    await retry_on_connect_error(
        _create_database_if_missing,
        _CONNECT_BUDGET_SECONDS,
        f"database '{settings.DATABASE_NAME}' on {settings.DATABASE_HOST}",
        retry_on=_CONNECT_ERRORS,
    )


async def _verify_app_role(pool: DatabasePool) -> None:
    """Refuse to start when the role holds superuser or create* privileges."""
    conn = await pool.acquire()
    try:
        role = await conn.fetchrow(
            "SELECT current_user AS naam, rolsuper, rolcreaterole, rolcreatedb "
            "FROM pg_roles WHERE rolname = current_user"
        )
    finally:
        await pool.release(conn)

    if role is None:  # pragma: no cover  # current_user is always in pg_roles
        return

    too_many = [privilege for privilege in ("rolsuper", "rolcreaterole", "rolcreatedb") if role[privilege]]
    if role["naam"] == settings.DATABASE_ADMIN_NAME:
        too_many.append("is de admin-rol zelf")
    if not too_many:
        return

    message = (
        f"DATABASE_APP_USER '{role['naam']}' heeft rechten die OPI's eigen database niet nodig heeft "
        f"({', '.join(too_many)}). Zet DATABASE_APP_USER/DATABASE_APP_PASSWORD op de rol "
        f"'{settings.DATABASE_APP_USER}' uit het secret operations-manager-db-credentials."
    )
    if settings.is_development_environment:
        logger.warning(message)
        return
    raise DatabasePoolError(message)


async def initialize_database_pools() -> None:
    """Retries with exponential backoff: the application cannot function without a database,
    so a database that is not up yet may not fail the startup either.
    """
    logger.info("Starting database pool initialization with retry logic...")

    try:
        await _ensure_database_exists()
    except Exception as e:
        logger.error(f"Failed to ensure database exists: {e}")
        raise DatabasePoolError(f"Failed to create/verify database: {e}") from e

    # Main PostgreSQL pool for operations, using the app role (not superuser).
    # _ensure_database_exists above handles database creation with admin credentials.
    main_pool = DatabasePool(
        host=settings.DATABASE_HOST,
        user=settings.DATABASE_APP_USER,
        password=settings.database_app_password,
        database=settings.DATABASE_NAME,
        port=5432,
        min_size=0,
        max_size=MAIN_POOL_MAX_SIZE,
        max_inactive_connection_lifetime=300.0,
        sslmode=settings.DATABASE_SSLMODE,
    )

    try:
        await _initialize_database_pool_with_retry(main_pool, "main")
        _pools["main"] = main_pool
        logger.info("All database pools initialized successfully")
    except Exception as e:
        logger.error(
            f"CRITICAL: Failed to initialize database pools after all retries. "
            f"Application cannot function without database connectivity. Error: {e}"
        )
        raise DatabasePoolError(f"Database pool initialization failed after all retries: {e}") from e

    # Outside the try above, so this refusal keeps its own message instead of reading as
    # "failed after all retries".
    await _verify_app_role(main_pool)


async def close_database_pools() -> None:
    logger.info("Closing database pools...")

    for name, pool in _pools.items():
        try:
            await pool.close()
            logger.info(f"Closed database pool: {name}")
        except (DatabasePoolError, OSError, asyncpg.PostgresError) as e:
            logger.error(f"Error closing database pool {name}: {e}")

    _pools.clear()
    logger.info("All database pools closed")


def get_database_pool(name: str = "main") -> DatabasePool:
    """Raises ``KeyError`` for an unknown pool, ``ValueError`` for one that is not up."""
    if name not in _pools:
        raise KeyError(f"Database pool '{name}' not found. Available pools: {list(_pools.keys())}")

    pool = _pools[name]
    if not pool.is_initialized:
        raise ValueError(f"Database pool '{name}' is not initialized")

    return pool


def get_all_pool_stats() -> dict[str, dict[str, any]]:
    """Per pool name its statistics."""
    return {name: pool.get_connection_stats() for name, pool in _pools.items()}


def log_active_connections(name: str = "main") -> None:
    """Log the active connections of one pool."""
    if name not in _pools:
        logger.error(f"Pool '{name}' not found. Available pools: {list(_pools.keys())}")
        return

    pool = _pools[name]
    pool.log_active_connections()


async def recover_database_pool(name: str = "main") -> bool:
    """Reinitialize one pool, with the same retry logic as the startup.

    Meant to be called at runtime, once database operations start failing.
    """
    if name not in _pools:
        logger.error(f"Cannot recover database pool '{name}' - pool not found. Available pools: {list(_pools.keys())}")
        return False

    pool = _pools[name]
    logger.warning(f"🔄 Attempting to recover database pool '{name}' (host: {pool.host}:{pool.port})")

    try:
        if pool.is_initialized:
            try:
                await pool.close()
                logger.info(f"Closed existing pool '{name}' for recovery")
            except (DatabasePoolError, OSError, asyncpg.PostgresError) as e:
                logger.warning(f"Error closing pool '{name}' during recovery: {e}")

        await _initialize_database_pool_with_retry(pool, name)
        logger.info(f"Successfully recovered database pool '{name}'")
        return True

    except (DatabasePoolError, OSError, asyncpg.PostgresError) as e:
        logger.error(f"Failed to recover database pool '{name}': {e}")
        return False


async def check_and_recover_all_pools() -> dict[str, bool]:
    """Per pool name whether it is healthy, or whether recovering it worked."""
    if not _pools:
        logger.warning("No database pools to check for recovery")
        return {}

    recovery_results = {}

    for name, pool in _pools.items():
        try:
            stats = pool.get_connection_stats()
            if "error" in stats:
                logger.warning(f"Database pool '{name}' appears unhealthy: {stats['error']}")
                recovery_results[name] = await recover_database_pool(name)
            else:
                logger.debug(f"Database pool '{name}' is healthy")
                recovery_results[name] = True

        except (DatabasePoolError, OSError, asyncpg.PostgresError) as e:
            logger.error(f"Health check failed for pool '{name}': {e}")
            recovery_results[name] = await recover_database_pool(name)

    return recovery_results


def is_database_available() -> bool:
    """True when at least one pool is up."""
    if not _pools:
        return False

    return any(pool.is_initialized for pool in _pools.values())
