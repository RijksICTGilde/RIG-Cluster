#!/usr/bin/env python3

import asyncio
import logging
import sys

# Add the current directory to path
sys.path.insert(0, ".")

# Import directly to avoid other dependencies
from opi.connectors.postgres import PostgresConnector

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


async def test_connection_pool():
    """Test the new PostgresConnector with connection pooling."""

    try:
        # Initialize the connection pool
        await PostgresConnector.initialize_pool(
            host=settings.DATABASE_HOST, user=settings.DATABASE_ADMIN_NAME, password=settings.DATABASE_ADMIN_PASSWORD
        )

        logger.info("Connection pool initialized successfully")

        # Test context manager usage
        async with PostgresConnector() as conn:
            logger.info("Acquired connection from pool")

            # Test basic operation
            result = await conn.test_connection(
                host=settings.DATABASE_HOST,
                username=settings.DATABASE_ADMIN_NAME,
                password=settings.DATABASE_ADMIN_PASSWORD,
            )

            logger.info(f"Connection test result: {result}")

        logger.info("Connection returned to pool")

        # Close the pool
        await PostgresConnector.close_pool()
        logger.info("Connection pool closed")

    except Exception as e:
        logger.error(f"Test failed: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(test_connection_pool())
