#!/usr/bin/env python3
"""
Test script to verify database credentials for deployment-3
This script tests the exact same connection that the application would use.
"""

import asyncio
import sys

import asyncpg


async def test_database_connection() -> None:
    """Test database connection using deployment-3 credentials from Kubernetes secret."""

    # Credentials from deployment-3-database secret in rig-amt2-dev namespace
    # Using port forward: kubectl port-forward -n rig-system svc/rig-db-rw 5432:5432
    HOST = "localhost"
    PORT = 5432
    DATABASE = "amt2_dev_deployment_3"
    USERNAME = "amt2_dev_deployment_3"
    PASSWORD = "yBoR48TxlrrITe8wMWj5"
    SCHEMA = "amt2_dev_deployment_3"

    print("=== Database Connection Test for deployment-3 ===")
    print(f"Host: {HOST}")
    print(f"Database: {DATABASE}")
    print(f"Username: {USERNAME}")
    print(f"Schema: {SCHEMA}")
    print()

    try:
        print("1. Testing basic connection...")
        conn = await asyncpg.connect(host=HOST, port=PORT, user=USERNAME, password=PASSWORD, database=DATABASE)
        print("✅ Basic connection successful!")

        print("\n2. Testing schema access...")
        # Set search path to the expected schema
        await conn.execute(f"SET search_path = {SCHEMA}")
        print(f"✅ Set search_path to {SCHEMA}")

        print("\n3. Checking database ownership...")
        db_owner = await conn.fetchval(
            "SELECT pg_catalog.pg_get_userbyid(d.datdba) FROM pg_database d WHERE d.datname = $1", DATABASE
        )
        print(f"Database owner: {db_owner}")
        if db_owner == USERNAME:
            print("✅ Database is owned by the correct user")
        else:
            print(f"⚠️  Database owner mismatch! Expected: {USERNAME}, Got: {db_owner}")

        print("\n4. Checking schema ownership...")
        schema_owner = await conn.fetchval(
            "SELECT schema_owner FROM information_schema.schemata WHERE schema_name = $1", SCHEMA
        )
        print(f"Schema owner: {schema_owner}")
        if schema_owner == USERNAME:
            print("✅ Schema is owned by the correct user")
        else:
            print(f"⚠️  Schema owner mismatch! Expected: {USERNAME}, Got: {schema_owner}")

        print("\n5. Listing ALL schemas in database...")
        schemas = await conn.fetch("""
            SELECT schema_name, schema_owner 
            FROM information_schema.schemata 
            WHERE schema_name NOT IN ('information_schema', 'pg_catalog', 'pg_toast')
            ORDER BY schema_name
        """)
        for schema in schemas:
            print(f"  - {schema['schema_name']} (owner: {schema['schema_owner']})")

        print("\n5b. Checking if deployment-2 schema exists (clone source suspicion)...")
        deployment2_schema = "amt2_dev_deployment_2"
        deployment2_exists = any(s["schema_name"] == deployment2_schema for s in schemas)
        if deployment2_exists:
            print(f"✅ FOUND IT! Schema {deployment2_schema} exists in deployment-3 database!")
            print("This confirms the clone copied the source schema but didn't rename it.")
        else:
            print(f"❌ Schema {deployment2_schema} not found either.")

        print("\n6. Testing table access...")
        try:
            # Try to list tables in the schema
            tables = await conn.fetch(
                """
                SELECT table_name, table_type 
                FROM information_schema.tables 
                WHERE table_schema = $1
                ORDER BY table_name
            """,
                SCHEMA,
            )

            print(f"Found {len(tables)} tables in schema {SCHEMA}:")
            for table in tables:
                print(f"  - {table['table_name']} ({table['table_type']})")

            # Try to access alembic_version table specifically (the one that was failing)
            if any(table["table_name"] == "alembic_version" for table in tables):
                print("\n7. Testing alembic_version table access...")
                try:
                    version = await conn.fetchval(f"SELECT version_num FROM {SCHEMA}.alembic_version LIMIT 1")
                    print(f"✅ Successfully read alembic_version: {version}")
                except Exception as e:
                    print(f"❌ Failed to read alembic_version table: {e}")

                # Check table ownership
                table_owner = await conn.fetchval(
                    """
                    SELECT tableowner 
                    FROM pg_tables 
                    WHERE schemaname = $1 AND tablename = 'alembic_version'
                """,
                    SCHEMA,
                )
                print(f"alembic_version table owner: {table_owner}")
                if table_owner == USERNAME:
                    print("✅ alembic_version table is owned by the correct user")
                else:
                    print(f"⚠️  alembic_version table owner mismatch! Expected: {USERNAME}, Got: {table_owner}")
            else:
                print("❌ alembic_version table not found in schema")

        except Exception as e:
            print(f"❌ Error accessing tables: {e}")

        print("\n8. Testing user permissions...")
        try:
            # Check what permissions the user has
            permissions = await conn.fetch(
                """
                SELECT 
                    schemaname,
                    tablename,
                    tableowner,
                    hasinserts,
                    hasselects,
                    hasupdates,
                    hasdeletes
                FROM pg_tables 
                WHERE schemaname = $1
                ORDER BY tablename
            """,
                SCHEMA,
            )

            print("Table permissions:")
            for perm in permissions:
                print(
                    f"  - {perm['tablename']}: owner={perm['tableowner']}, "
                    f"select={perm['hasselects']}, insert={perm['hasinserts']}, "
                    f"update={perm['hasupdates']}, delete={perm['hasdeletes']}"
                )

        except Exception as e:
            print(f"❌ Error checking permissions: {e}")

        await conn.close()
        print("\n✅ All tests completed successfully!")

    except Exception as e:
        print(f"❌ Connection failed: {e}")
        print(f"Error type: {type(e).__name__}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(test_database_connection())
