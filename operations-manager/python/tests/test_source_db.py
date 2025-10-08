#!/usr/bin/env python3
"""
Test script to check what's in the SOURCE database (deployment-1) that should be cloned
"""

import asyncio

import asyncpg


async def test_source_database():
    """Check what schemas and tables exist in the source database."""

    # Source database credentials (deployment-1)
    HOST = "localhost"
    PORT = 5432
    DATABASE = "amt2_dev_deployment_1"  # Source database
    USERNAME = "amt2_dev_deployment_1"
    # We need the password - let me get it

    print("=== Source Database Check (deployment-1) ===")
    print(f"Checking source database: {DATABASE}")

    try:
        conn = await asyncpg.connect(
            host=HOST, port=PORT, user=USERNAME, password="87JxccK0FXkPuByRkc3s", database=DATABASE
        )
        print("✅ Connected to source database")

        # List all schemas
        schemas = await conn.fetch("""
            SELECT schema_name, schema_owner 
            FROM information_schema.schemata 
            WHERE schema_name NOT IN ('information_schema', 'pg_catalog', 'pg_toast')
            ORDER BY schema_name
        """)
        print("\nSchemas in source database:")
        for schema in schemas:
            print(f"  - {schema['schema_name']} (owner: {schema['schema_owner']})")

        # Check for the expected schema
        expected_schema = "amt2_dev_deployment_1"
        schema_exists = any(s["schema_name"] == expected_schema for s in schemas)

        if schema_exists:
            # List tables in the expected schema
            tables = await conn.fetch(
                """
                SELECT table_name, table_type 
                FROM information_schema.tables 
                WHERE table_schema = $1
                ORDER BY table_name
            """,
                expected_schema,
            )

            print(f"\nTables in schema {expected_schema}:")
            for table in tables:
                print(f"  - {table['table_name']} ({table['table_type']})")
        else:
            print(f"❌ Expected schema {expected_schema} NOT FOUND in source database!")

        await conn.close()

    except Exception as e:
        print(f"❌ Error: {e}")


if __name__ == "__main__":
    asyncio.run(test_source_database())
