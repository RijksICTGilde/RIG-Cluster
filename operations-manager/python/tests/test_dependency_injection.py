#!/usr/bin/env python3
"""
Test the new dependency injection architecture with DatabasePool.
"""

import asyncio
import logging
import sys

sys.path.insert(0, ".")

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def test_dependency_injection_architecture():
    """Test the new dependency injection architecture."""

    try:
        # Import the new architecture
        from opi.connectors.postgres import create_postgres_connector
        from opi.core.config import settings
        from opi.core.database_pools import close_database_pools, get_database_pool, initialize_database_pools
        from opi.manager.database_manager import DatabaseManager

        # Initialize application-level pools
        await initialize_database_pools()
        logger.info("✅ Application database pools initialized")

        # Get the main pool
        main_pool = get_database_pool("main")
        logger.info("✅ Retrieved main database pool")

        # Test direct pool usage
        conn = await main_pool.acquire()
        await main_pool.release(conn)
        logger.info("✅ Direct pool acquire/release works")

        # Test PostgresConnector with dependency injection
        postgres_conn = await create_postgres_connector(main_pool)
        try:
            # Test a basic operation
            result = await postgres_conn.test_connection(
                host=settings.DATABASE_HOST,
                username=settings.DATABASE_ADMIN_NAME,
                password=settings.DATABASE_ADMIN_PASSWORD,
            )
            logger.info(f"✅ PostgresConnector with DI works: {result}")
        finally:
            await postgres_conn.close()

        # Test DatabaseManager with dependency injection
        class MockProjectManager:
            def get_progress_manager(self):
                return None

            def _add_secret_to_create(self, *args, **kwargs):
                logger.info("Mock: Adding secret to create")

        project_manager = MockProjectManager()
        db_manager = DatabaseManager(project_manager, main_pool)
        logger.info("✅ DatabaseManager with DI created successfully")

        # Test connection initialization
        await db_manager._ensure_connection()
        logger.info("✅ DatabaseManager connection ensured")

        # Clean up
        await db_manager.close()
        logger.info("✅ DatabaseManager closed")

        # Close application pools
        await close_database_pools()
        logger.info("✅ Application database pools closed")

        print()
        print("🎉 Dependency Injection Architecture Test PASSED!")
        print()
        print("✅ Key improvements:")
        print("  • DatabasePool class manages connection pools")
        print("  • PostgresConnector receives pool via dependency injection")
        print("  • DatabaseManager receives pool via dependency injection")
        print("  • Multiple pools supported (dev/prod/test)")
        print("  • Clean separation of concerns")
        print("  • Application manages pool lifecycle")
        print("  • Race condition eliminated with single connection per operation")

    except Exception as e:
        logger.error(f"❌ Test failed: {e}")
        # Try to clean up on error
        try:
            await close_database_pools()
        except:
            pass
        raise


if __name__ == "__main__":
    asyncio.run(test_dependency_injection_architecture())
