"""
Database pool management for the application.

This module provides centralized database pool management with application lifecycle integration.
"""

import asyncio
import logging

import asyncpg
from tenacity import (
    after_log,
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from opi.core.config import settings
from opi.core.database_pool import DatabasePool, DatabasePoolError

logger = logging.getLogger(__name__)

# Global registry of database pools
_pools: dict[str, DatabasePool] = {}


@retry(
    stop=stop_after_attempt(12),  # Try up to 12 times (about 8 minutes total)
    wait=wait_exponential(multiplier=2, min=2, max=60),  # 2s, 4s, 8s, 16s, 32s, 60s, 60s...
    retry=retry_if_exception_type(
        (
            asyncpg.exceptions.PostgresError,
            asyncpg.exceptions.InterfaceError,
            asyncpg.exceptions.InvalidAuthorizationSpecificationError,
            asyncpg.exceptions.ConnectionDoesNotExistError,
            asyncpg.exceptions.ConnectionFailureError,
            ConnectionError,
            OSError,
            DatabasePoolError,
            asyncio.TimeoutError,
        )
    ),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    after=after_log(logger, logging.INFO),
)
async def _initialize_database_pool_with_retry(pool: DatabasePool, name: str) -> None:
    """Initialize a single database pool with retry logic.

    Args:
        pool: The DatabasePool instance to initialize
        name: The name of the pool for logging
    """
    logger.info(f"Attempting to initialize database pool '{name}' (host: {pool.host}:{pool.port})")
    await pool.initialize()
    logger.info(f"Successfully initialized database pool '{name}'")


async def initialize_database_pools() -> None:
    """Initialize all database pools used by the application with robust retry logic.

    This function is critical for application startup and will retry multiple times
    with exponential backoff if the database is not available. The application
    cannot function without database connectivity.
    """
    logger.info("Starting database pool initialization with retry logic...")

    # Main PostgreSQL pool for operations
    main_pool = DatabasePool(
        host=settings.DATABASE_HOST,
        user=settings.DATABASE_ADMIN_NAME,
        password=settings.DATABASE_ADMIN_PASSWORD,
        database="postgres",  # Connect to default postgres database
        port=5432,
        min_size=2,
        max_size=10,
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
        # Re-raise to fail application startup
        raise DatabasePoolError(f"Database pool initialization failed after all retries: {e}") from e


async def close_database_pools() -> None:
    """Close all database pools."""
    logger.info("Closing database pools...")

    for name, pool in _pools.items():
        try:
            await pool.close()
            logger.info(f"Closed database pool: {name}")
        except Exception as e:
            logger.error(f"Error closing database pool {name}: {e}")

    _pools.clear()
    logger.info("All database pools closed")


def get_database_pool(name: str = "main") -> DatabasePool:
    """Get a database pool by name.

    Args:
        name: Name of the pool to retrieve

    Returns:
        DatabasePool instance

    Raises:
        KeyError: If the pool doesn't exist
        ValueError: If the pool is not initialized
    """
    if name not in _pools:
        raise KeyError(f"Database pool '{name}' not found. Available pools: {list(_pools.keys())}")

    pool = _pools[name]
    if not pool.is_initialized:
        raise ValueError(f"Database pool '{name}' is not initialized")

    return pool


def list_database_pools() -> dict[str, bool]:
    """List all database pools and their initialization status.

    Returns:
        Dictionary mapping pool names to their initialization status
    """
    return {name: pool.is_initialized for name, pool in _pools.items()}


def get_pool_stats(name: str = "main") -> dict[str, any]:
    """Get detailed statistics for a specific database pool.

    Args:
        name: Name of the pool to get stats for

    Returns:
        Dictionary containing pool statistics
    """
    if name not in _pools:
        return {"error": f"Pool '{name}' not found. Available pools: {list(_pools.keys())}"}

    pool = _pools[name]
    return pool.get_connection_stats()


def get_all_pool_stats() -> dict[str, dict[str, any]]:
    """Get statistics for all database pools.

    Returns:
        Dictionary mapping pool names to their statistics
    """
    return {name: pool.get_connection_stats() for name, pool in _pools.items()}


def log_active_connections_for_all_pools() -> None:
    """Log active connection information for all pools."""
    if not _pools:
        logger.info("No database pools available")
        return

    logger.info("=== ACTIVE CONNECTION REPORT FOR ALL POOLS ===")
    for name, pool in _pools.items():
        logger.info(f"Pool '{name}':")
        pool.log_active_connections()
    logger.info("=== END ACTIVE CONNECTION REPORT ===")


def log_active_connections(name: str = "main") -> None:
    """Log active connection information for a specific pool.

    Args:
        name: Name of the pool to log connections for
    """
    if name not in _pools:
        logger.error(f"Pool '{name}' not found. Available pools: {list(_pools.keys())}")
        return

    pool = _pools[name]
    pool.log_active_connections()


async def recover_database_pool(name: str = "main") -> bool:
    """Recover a specific database pool if it has failed.

    This function can be called during runtime if database operations start failing.
    It will attempt to reinitialize the pool with retry logic.

    Args:
        name: Name of the pool to recover

    Returns:
        True if recovery successful, False otherwise
    """
    if name not in _pools:
        logger.error(f"Cannot recover database pool '{name}' - pool not found. Available pools: {list(_pools.keys())}")
        return False

    pool = _pools[name]
    logger.warning(f"🔄 Attempting to recover database pool '{name}' (host: {pool.host}:{pool.port})")

    try:
        # First try to close the existing pool gracefully
        if pool.is_initialized:
            try:
                await pool.close()
                logger.info(f"Closed existing pool '{name}' for recovery")
            except Exception as e:
                logger.warning(f"Error closing pool '{name}' during recovery: {e}")

        # Attempt to reinitialize with retry logic
        await _initialize_database_pool_with_retry(pool, name)
        logger.info(f"Successfully recovered database pool '{name}'")
        return True

    except Exception as e:
        logger.error(f"Failed to recover database pool '{name}': {e}")
        return False


async def check_and_recover_all_pools() -> dict[str, bool]:
    """Check health of all pools and attempt recovery for any that are unhealthy.

    Returns:
        Dictionary mapping pool names to recovery success status
    """
    if not _pools:
        logger.warning("No database pools to check for recovery")
        return {}

    recovery_results = {}

    for name, pool in _pools.items():
        try:
            # Simple health check - try to get pool stats
            stats = pool.get_connection_stats()
            if "error" in stats:
                logger.warning(f"Database pool '{name}' appears unhealthy: {stats['error']}")
                recovery_results[name] = await recover_database_pool(name)
            else:
                logger.debug(f"Database pool '{name}' is healthy")
                recovery_results[name] = True

        except Exception as e:
            logger.error(f"Health check failed for pool '{name}': {e}")
            recovery_results[name] = await recover_database_pool(name)

    return recovery_results


def is_database_available() -> bool:
    """Quick check if any database pools are available and initialized.

    Returns:
        True if at least one pool is available, False otherwise
    """
    if not _pools:
        return False

    return any(pool.is_initialized for pool in _pools.values())
