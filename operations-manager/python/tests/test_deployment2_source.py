#!/usr/bin/env python3
"""
Check what's in the SOURCE database (deployment-2) that was cloned from
"""

import asyncio

import asyncpg


async def test_deployment2_source():
    """Check what schemas and tables exist in deployment-2 (clone source)."""

    HOST = "localhost"
    PORT = 5432
    DATABASE = "amt2_dev_deployment_2"
    USERNAME = "amt2_dev_deployment_2"
    PASSWORD = "wu5DYUA832zdkOFVeYhJ"

    print("=== Source Database Check (deployment-2) ===")
    print(f"Checking source database: {DATABASE}")

    try:
        conn = await asyncpg.connect(host=HOST, port=PORT, user=USERNAME, password=PASSWORD, database=DATABASE)
        print("✅ Connected to deployment-2 source database")

        # List all schemas
        schemas = await conn.fetch("""
            SELECT schema_name, schema_owner 
            FROM information_schema.schemata 
            WHERE schema_name NOT IN ('information_schema', 'pg_catalog', 'pg_toast')
            ORDER BY schema_name
        """)
        print("\nSchemas in deployment-2 source database:")
        for schema in schemas:
            print(f"  - {schema['schema_name']} (owner: {schema['schema_owner']})")

        # Check for the expected schema
        expected_schema = "amt2_dev_deployment_2"
        schema_exists = any(s["schema_name"] == expected_schema for s in schemas)

        if schema_exists:
            print(f"✅ Expected schema {expected_schema} EXISTS in source")

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
            if tables:
                for table in tables:
                    print(f"  - {table['table_name']} ({table['table_type']})")
                print(f"✅ Found {len(tables)} tables - source has content to clone!")
            else:
                print("❌ No tables found in source schema - empty schema!")
        else:
            print(f"❌ Expected schema {expected_schema} NOT FOUND in source database!")
            print("This explains why the clone appears empty - source is empty too!")

        await conn.close()

    except Exception as e:
        print(f"❌ Error connecting to deployment-2: {e}")


if __name__ == "__main__":
    asyncio.run(test_deployment2_source())
