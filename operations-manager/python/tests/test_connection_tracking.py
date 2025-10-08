#!/usr/bin/env python3
"""
Test the enhanced connection tracking functionality to detect leaks.
"""

import asyncio
import logging
import sys

# Add the current directory to Python path for imports
sys.path.insert(0, ".")

from opi.core.config import settings
from opi.core.database_pools import (
    close_database_pools,
    get_all_pool_stats,
    get_pool_stats,
    initialize_database_pools,
    log_active_connections_for_all_pools,
)

# Set up logging
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


async def test_connection_tracking():
    """Test the connection tracking functionality."""

    try:
        logger.info("🧪 Starting connection tracking test...")

        # Initialize the database pools
        await initialize_database_pools()
        logger.info("✅ Database pools initialized")

        # Show initial pool stats
        stats = get_all_pool_stats()
        logger.info(f"📊 Initial pool stats: {stats}")

        # Test 1: Normal connection usage (should not leak)
        logger.info("\n🔬 Test 1: Normal connection acquisition and release")
        from opi.connectors.postgres import create_postgres_connector
        from opi.core.database_pools import get_database_pool

        main_pool = get_database_pool("main")

        async with await create_postgres_connector(main_pool) as conn:
            logger.info("Connection acquired in context manager")
            result = await conn.test_connection(
                host=settings.DATABASE_HOST,
                username=settings.DATABASE_ADMIN_NAME,
                password=settings.DATABASE_ADMIN_PASSWORD,
            )
            logger.info(f"Test connection result: {result}")

        logger.info("Connection should be automatically released")
        log_active_connections_for_all_pools()

        # Test 2: Simulated leak (acquire but don't release)
        logger.info("\n🔬 Test 2: Simulated connection leak")

        leaked_conn = await create_postgres_connector(main_pool)
        logger.info("Connection acquired WITHOUT context manager - this will leak!")

        # Show active connections
        log_active_connections_for_all_pools()
        stats_after_leak = get_pool_stats("main")
        logger.info(f"📊 Pool stats after leak: {stats_after_leak}")

        # Test 3: Acquire multiple connections and leak some
        logger.info("\n🔬 Test 3: Multiple connections with partial leaks")

        # Acquire 3 more connections
        leaked_conn2 = await create_postgres_connector(main_pool)
        leaked_conn3 = await create_postgres_connector(main_pool)

        # Properly release one
        good_conn = await create_postgres_connector(main_pool)
        await good_conn.close()
        logger.info("Released one connection properly")

        # Show all active connections
        logger.info("\n📋 Final active connections report:")
        log_active_connections_for_all_pools()

        final_stats = get_pool_stats("main")
        logger.info(f"📊 Final pool stats: {final_stats}")

        # Attempt to close with leaks (this should show detailed leak info)
        logger.info("\n🔥 Attempting to close pools with active connections (will show leak details)...")

    except Exception as e:
        logger.error(f"❌ Test failed with error: {e}")
        raise

    finally:
        # This should trigger the leak detection logging
        await close_database_pools()
        logger.info("🏁 Test completed")


if __name__ == "__main__":
    asyncio.run(test_connection_tracking())
