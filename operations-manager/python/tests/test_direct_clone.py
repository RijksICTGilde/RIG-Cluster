#!/usr/bin/env python3
"""
Direct test of the clone operation using the postgres connector
"""

import asyncio
import logging

from opi.connectors.postgres import create_postgres_connector
from opi.core.config import settings
from opi.core.database_pools import close_database_pools, get_database_pool, initialize_database_pools

logger = logging.getLogger(__name__)


async def test_direct_clone():
    """Test clone operation directly."""

    source_database = "amt2_dev_deployment_1"
    source_schema = "amt2_dev_deployment_1"
    target_database = "amt2_dev_deployment_3"
    target_schema = "amt2_dev_deployment_3"
    owner = target_database  # Use target database name as owner

    print("=== Direct Clone Test ===")
    print(f"Source: {source_database}.{source_schema}")
    print(f"Target: {target_database}.{target_schema}")
    print(f"Owner: {owner}")

    # Initialize database pools
    await initialize_database_pools()
    postgres_pool = get_database_pool("main")
    postgres_connector = await create_postgres_connector(postgres_pool)

    try:
        # Test the clone operation directly
        print("\n=== Executing Clone Operation ===")

        clone_result = await postgres_connector.clone_schema(
            source_database=source_database,
            source_schema=source_schema,
            target_database=target_database,
            target_schema=target_schema,
            host=settings.DATABASE_HOST,
            admin_username=settings.DATABASE_ADMIN_NAME,
            admin_password=settings.DATABASE_ADMIN_PASSWORD,
            target_owner=owner,
            target_owner_password="tV7ItQqGCqqUA8Efhg9q",  # From the Kubernetes secret
        )

        print(f"\nClone result: {clone_result}")

        if clone_result.get("status") == "success":
            print("✅ Clone operation completed successfully!")
        else:
            print(f"❌ Clone failed: {clone_result.get('message', 'Unknown error')}")
            if clone_result.get("stdout"):
                print(f"STDOUT: {clone_result['stdout']}")
            if clone_result.get("stderr"):
                print(f"STDERR: {clone_result['stderr']}")

    except Exception as e:
        print(f"❌ Error during clone test: {e}")
        logger.exception("Error in clone test")

    finally:
        await postgres_connector.close()
        await close_database_pools()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(test_direct_clone())
