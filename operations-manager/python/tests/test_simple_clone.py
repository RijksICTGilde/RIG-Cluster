#!/usr/bin/env python3
"""
Simple test to verify deployment-1 has content and trigger a clone to deployment-3
"""

import asyncio
import logging

from opi.connectors.postgres import create_postgres_connector
from opi.core.database_pools import close_database_pools, get_database_pool, initialize_database_pools
from opi.utils.naming import generate_resource_identifier

logger = logging.getLogger(__name__)


async def test_clone_operation():
    """Test cloning from deployment-1 to deployment-3."""

    project_name = "amt2-dev"
    source_deployment = "deployment-1"  # The source we're cloning from
    target_deployment = "deployment-3"  # The target we're cloning to

    # Generate expected names
    source_database = generate_resource_identifier(project_name, source_deployment, "_")
    source_schema = generate_resource_identifier(project_name, source_deployment, "_")
    target_database = generate_resource_identifier(project_name, target_deployment, "_")
    target_schema = generate_resource_identifier(project_name, target_deployment, "_")

    print("=== Testing Clone Operation ===")
    print(f"Source: {source_database}.{source_schema}")
    print(f"Target: {target_database}.{target_schema}")

    # Initialize database pools
    await initialize_database_pools()
    postgres_pool = get_database_pool("main")
    postgres_connector = await create_postgres_connector(postgres_pool)

    try:
        # Check source database and schema exist
        source_db_exists = await postgres_connector.database_exists(source_database)
        print(f"Source database exists: {source_db_exists}")

        if not source_db_exists:
            print(f"❌ Source database {source_database} does not exist!")
            return

        source_schema_exists = await postgres_connector.schema_exists(source_database, source_schema)
        print(f"Source schema exists: {source_schema_exists}")

        if not source_schema_exists:
            print(f"❌ Source schema {source_schema} does not exist!")
            return

        # Test the actual clone operation using the postgres connector
        print("\n=== Testing Clone Operation ===")

        clone_result = await postgres_connector.clone_schema(
            source_database=source_database,
            source_schema=source_schema,
            target_database=target_database,
            target_schema=target_schema,
            owner=target_database,  # Use target database name as owner
        )

        print(f"Clone result: {clone_result}")

        if clone_result.get("status") == "success":
            print("✅ Clone operation completed successfully!")

            # Verify the cloned schema exists
            cloned_schema_exists = await postgres_connector.schema_exists(target_database, source_schema)
            print(f"Cloned schema {source_schema} exists in target: {cloned_schema_exists}")

            # Check if we need to rename (this should happen if source_schema != target_schema)
            if source_schema != target_schema and cloned_schema_exists:
                print(f"Need to rename schema from {source_schema} to {target_schema}")

        else:
            print(f"❌ Clone failed: {clone_result.get('message', 'Unknown error')}")

    except Exception as e:
        print(f"❌ Error during clone test: {e}")
        logger.exception("Error in clone test")

    finally:
        await postgres_connector.close()
        await close_database_pools()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(test_clone_operation())
