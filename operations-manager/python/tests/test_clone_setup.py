#!/usr/bin/env python3
"""
Test that deployment-1 has content and verify clone will work
"""

import asyncio
import logging

from opi.connectors.postgres import create_postgres_connector
from opi.core.database_pools import get_database_pools
from opi.utils.naming import generate_resource_identifier

logger = logging.getLogger(__name__)


async def test_clone_setup():
    """Test that deployment-1 has content and clone setup will work."""

    project_name = "amt2-dev"
    source_deployment = "deployment-1"  # The source we're cloning from
    target_deployment = "deployment-3"  # The target we're cloning to

    # Generate expected names
    source_database = generate_resource_identifier(project_name, source_deployment, "_")
    source_schema = generate_resource_identifier(project_name, source_deployment, "_")
    target_database = generate_resource_identifier(project_name, target_deployment, "_")
    target_schema = generate_resource_identifier(project_name, target_deployment, "_")

    print("=== Clone Setup Verification ===")
    print(f"Source: {source_database}.{source_schema}")
    print(f"Target: {target_database}.{target_schema}")

    # Get database pools
    db_pools = await get_database_pools()
    postgres_connector = await create_postgres_connector(db_pools.postgres)

    try:
        # Check source database exists
        source_db_exists = await postgres_connector.database_exists(source_database)
        print(f"✅ Source database exists: {source_db_exists}")

        if not source_db_exists:
            print(f"❌ Source database {source_database} does not exist!")
            return

        # Check source schema exists
        source_schema_exists = await postgres_connector.schema_exists(source_database, source_schema)
        print(f"✅ Source schema exists: {source_schema_exists}")

        if not source_schema_exists:
            print(f"❌ Source schema {source_schema} does not exist in {source_database}!")
            return

        # Count tables in source schema
        table_count_query = """
            SELECT COUNT(*) as count 
            FROM information_schema.tables 
            WHERE table_schema = %s
        """

        table_count_result = await postgres_connector.execute_query(
            source_database, table_count_query, (source_schema,)
        )

        if table_count_result and len(table_count_result) > 0:
            table_count = table_count_result[0].get("count", 0)
            print(f"✅ Tables in source schema: {table_count}")

            if table_count > 0:
                # List some table names
                tables_query = """
                    SELECT table_name 
                    FROM information_schema.tables 
                    WHERE table_schema = %s 
                    ORDER BY table_name 
                    LIMIT 10
                """

                tables_result = await postgres_connector.execute_query(source_database, tables_query, (source_schema,))

                print(f"✅ Sample tables in {source_schema}:")
                for table in tables_result:
                    print(f"  - {table['table_name']}")

                print("✅ Source database is ready for cloning!")
            else:
                print(f"❌ Source schema {source_schema} is empty - no tables found!")
        else:
            print(f"❌ Could not query table count for {source_schema}")

        # Check target database status
        target_db_exists = await postgres_connector.database_exists(target_database)
        print(f"Target database exists: {target_db_exists}")

        if target_db_exists:
            target_schema_exists = await postgres_connector.schema_exists(target_database, target_schema)
            print(f"Target schema exists: {target_schema_exists}")

    except Exception as e:
        print(f"❌ Error during test: {e}")
        logger.exception("Error in clone setup test")

    finally:
        await postgres_connector.close()
        await db_pools.close_all()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(test_clone_setup())
