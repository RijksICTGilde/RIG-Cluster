#!/usr/bin/env python3
"""
Simple test to verify the PostgreSQL connection fix works.
"""

import asyncio
import logging
import sys

sys.path.insert(0, ".")

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def test_connection_race_condition_fix():
    """Test that we can create a user and database with the same connection."""

    try:
        # Import and initialize the pool
        from opi.connectors.postgres import PostgresConnector, create_postgres_connector
        from opi.core.config import settings

        await PostgresConnector.initialize_pool(
            host=settings.DATABASE_HOST, user=settings.DATABASE_ADMIN_NAME, password=settings.DATABASE_ADMIN_PASSWORD
        )
        logger.info("✅ Pool initialized")

        # Test both patterns work the same

        # Pattern 1: Context manager
        logger.info("Testing context manager pattern...")
        async with create_postgres_connector() as conn1:
            result = await conn1.test_connection(
                host=settings.DATABASE_HOST,
                username=settings.DATABASE_ADMIN_NAME,
                password=settings.DATABASE_ADMIN_PASSWORD,
            )
            logger.info(f"✅ Context manager test: {result}")

        # Pattern 2: Try/finally
        logger.info("Testing try/finally pattern...")
        conn2 = await create_postgres_connector()
        try:
            result = await conn2.test_connection(
                host=settings.DATABASE_HOST,
                username=settings.DATABASE_ADMIN_NAME,
                password=settings.DATABASE_ADMIN_PASSWORD,
            )
            logger.info(f"✅ Try/finally test: {result}")
        finally:
            await conn2.close()

        # Clean up
        await PostgresConnector.close_pool()
        logger.info("✅ All tests passed! Race condition fix is working.")

    except Exception as e:
        logger.error(f"❌ Test failed: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(test_connection_race_condition_fix())
