#!/usr/bin/env python3
"""
Test schema access permissions and search path issues
"""

import asyncio

import asyncpg
from opi.core.config import settings


async def test_schema_permissions():
    """Test schema access permissions and search path."""

    target_database = "amt2_dev_deployment_3"
    target_schema = "amt2_dev_deployment_3"
    target_user = "amt2_dev_deployment_3"
    password = "tV7ItQqGCqqUA8Efhg9q"  # From the Kubernetes secret

    print("=== Testing Schema Access Permissions ===")
    print(f"Database: {target_database}")
    print(f"Schema: {target_schema}")
    print(f"User: {target_user}")

    try:
        user_conn = await asyncpg.connect(
            host=settings.DATABASE_HOST, port=5432, user=target_user, password=password, database=target_database
        )
        print(f"✅ Successfully connected to database as {target_user}")

    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return

    # Test 1: Check current search_path
    print("\n=== Test 1: Check search_path ===")

    try:
        search_path = await user_conn.fetchval("SHOW search_path")
        print(f"Current search_path: {search_path}")

        # Check if our schema is in the search path
        if target_schema in search_path:
            print("✅ Target schema is in search_path")
        else:
            print("❌ Target schema NOT in search_path")

    except Exception as e:
        print(f"❌ Error checking search_path: {e}")

    # Test 2: Check schema permissions
    print("\n=== Test 2: Check schema permissions ===")

    try:
        # Check if user has USAGE permission on schema
        has_usage = await user_conn.fetchval(f"""
            SELECT has_schema_privilege('{target_user}', '{target_schema}', 'USAGE')
        """)
        print(f"USAGE permission on {target_schema}: {has_usage}")

        has_create = await user_conn.fetchval(f"""
            SELECT has_schema_privilege('{target_user}', '{target_schema}', 'CREATE')
        """)
        print(f"CREATE permission on {target_schema}: {has_create}")

    except Exception as e:
        print(f"❌ Error checking schema permissions: {e}")

    # Test 3: List all schemas user can see
    print("\n=== Test 3: List all visible schemas ===")

    try:
        schemas = await user_conn.fetch("""
            SELECT schema_name, schema_owner
            FROM information_schema.schemata
            ORDER BY schema_name
        """)

        print("All visible schemas:")
        for schema in schemas:
            print(f"  - {schema['schema_name']} (owner: {schema['schema_owner']})")

        # Check if our target schema is visible
        target_found = any(s["schema_name"] == target_schema for s in schemas)
        if target_found:
            print(f"✅ Target schema {target_schema} is visible")
        else:
            print(f"❌ Target schema {target_schema} is NOT visible")

    except Exception as e:
        print(f"❌ Error listing schemas: {e}")

    # Test 4: Try to list tables using explicit schema notation
    print("\n=== Test 4: Check tables with explicit schema reference ===")

    try:
        # Use explicit schema notation
        tables = await user_conn.fetch(f"""
            SELECT table_name, table_type
            FROM information_schema.tables 
            WHERE table_schema = '{target_schema}'
            ORDER BY table_name
        """)

        if tables:
            print(f"✅ Found {len(tables)} tables using explicit schema reference:")
            for table in tables:
                print(f"  - {table['table_name']} ({table['table_type']})")
        else:
            print("❌ No tables found using explicit schema reference")

    except Exception as e:
        print(f"❌ Error listing tables with explicit schema: {e}")

    # Test 5: Check if we can connect with correct search_path set
    print("\n=== Test 5: Connect with explicit search_path ===")

    try:
        await user_conn.close()

        # Reconnect with explicit search_path
        user_conn = await asyncpg.connect(
            host=settings.DATABASE_HOST,
            port=5432,
            user=target_user,
            password=password,
            database=target_database,
            server_settings={"search_path": target_schema},
        )

        print(f"✅ Reconnected with search_path={target_schema}")

        # Check new search_path
        search_path = await user_conn.fetchval("SHOW search_path")
        print(f"New search_path: {search_path}")

        # Try listing tables again
        tables = await user_conn.fetch("""
            SELECT table_name, table_type
            FROM information_schema.tables 
            WHERE table_schema = CURRENT_SCHEMA()
            ORDER BY table_name
        """)

        if tables:
            print(f"✅ With correct search_path, found {len(tables)} tables:")
            for table in tables:
                print(f"  - {table['table_name']} ({table['table_type']})")

            # Test the problematic alembic_version query
            print("\n=== Test 6: Test alembic_version query with search_path ===")

            try:
                # The exact query that's failing in the application
                version = await user_conn.fetchval("""
                    SELECT version_num FROM alembic_version
                """)

                if version:
                    print(f"✅ Successfully queried alembic_version - version: {version}")
                else:
                    print("✅ Successfully queried alembic_version - no version found")

            except Exception as e:
                print(f"❌ Still cannot query alembic_version: {e}")

                # Check table ownership
                try:
                    table_owner = await user_conn.fetchval("""
                        SELECT tableowner 
                        FROM pg_tables 
                        WHERE schemaname = CURRENT_SCHEMA()
                        AND tablename = 'alembic_version'
                    """)
                    print(f"alembic_version table owner: {table_owner}")

                    # Check specific table permissions
                    has_select = await user_conn.fetchval(f"""
                        SELECT has_table_privilege('{target_user}', 'alembic_version', 'SELECT')
                    """)
                    print(f"SELECT permission on alembic_version: {has_select}")

                except Exception as perm_e:
                    print(f"Error checking table permissions: {perm_e}")
        else:
            print("❌ Still no tables found even with correct search_path")

    except Exception as e:
        print(f"❌ Error testing search_path connection: {e}")

    await user_conn.close()

    print("\n=== Summary ===")
    print("If search_path is the issue, the application needs to connect with")
    print(f"server_settings={{'search_path': '{target_schema}'}} or use explicit schema names")
    print("If table permissions are the issue, we need to grant proper permissions on cloned tables")


if __name__ == "__main__":
    asyncio.run(test_schema_permissions())
