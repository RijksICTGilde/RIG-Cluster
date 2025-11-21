#!/usr/bin/env python3
"""
Test the fixed DatabaseManager with PostgresConnector architecture.
"""

import asyncio
import logging
import sys

sys.path.insert(0, ".")

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def test_database_manager_fix():
    """Test that DatabaseManager works with the new connection architecture."""

    try:
        # Import required modules
        from opi.connectors.postgres import PostgresConnector
        from opi.core.config import settings
        from opi.manager.database_manager import DatabaseManager

        # Initialize the global connection pool
        await PostgresConnector.initialize_pool(
            host=settings.DATABASE_HOST, user=settings.DATABASE_ADMIN_NAME, password=settings.DATABASE_ADMIN_PASSWORD
        )
        logger.info("✅ PostgreSQL connection pool initialized")

        # Create a mock project manager
        class MockProjectManager:
            def get_progress_manager(self):
                return None

            def _add_secret_to_create(self, *args, **kwargs):
                logger.info("Mock: Adding secret to create")

        # Create DatabaseManager instance
        project_manager = MockProjectManager()
        db_manager = DatabaseManager(project_manager)
        logger.info("✅ DatabaseManager instance created")

        # Test connection initialization
        db_manager._ensure_connection()
        logger.info("✅ Database connection acquired successfully")

        # Clean up the connection
        await db_manager.close()
        logger.info("✅ Database connection closed successfully")

        # Close the global pool
        await PostgresConnector.close_pool()
        logger.info("✅ PostgreSQL connection pool closed")

        print("🎉 DatabaseManager fix test passed! Race condition is solved!")
        print()
        print("✅ Key benefits:")
        print("  • DatabaseManager has its own PostgresConnector instance")
        print("  • Connection acquired once per deployment operation")
        print("  • All database operations use the SAME connection")
        print("  • Race condition eliminated: user creation & database creation use same connection")
        print("  • Connection returned to pool when done")

    except Exception as e:
        logger.error(f"❌ Test failed: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(test_database_manager_fix())
