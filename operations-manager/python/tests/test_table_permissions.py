#!/usr/bin/env python3
"""
Test actual table permissions using the deployment-3-database secret
"""

import asyncio
import base64

import asyncpg
from opi.core.config import settings
from opi.utils.age import decrypt_password_smart


async def test_table_permissions():
    """Test table-level permissions using the real Kubernetes secret."""

    target_database = "amt2_dev_deployment_3"
    target_schema = "amt2_dev_deployment_3"
    target_user = "amt2_dev_deployment_3"

    print("=== Testing Table Permissions with Real Secret ===")
    print(f"Database: {target_database}")
    print(f"Schema: {target_schema}")
    print(f"User: {target_user}")

    # Get the actual password from Kubernetes secret
    print("\n=== Step 1: Get password from Kubernetes secret ===")

    try:
        import json
        import subprocess

        # Get the secret from Kubernetes
        cmd = ["kubectl", "get", "secret", "deployment-3-database", "-n", "rig-amt2-dev", "-o", "json"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        secret_data = json.loads(result.stdout)

        # Decode the password - it's in DATABASE_PASSWORD field
        encoded_password = secret_data["data"]["DATABASE_PASSWORD"]
        decoded_password = base64.b64decode(encoded_password).decode("utf-8")

        # Decrypt if it's an AGE-encrypted password
        if decoded_password.startswith("age:") or decoded_password.startswith("base64+age:"):
            actual_password = decrypt_password_smart(decoded_password)
            print("✅ Retrieved and decrypted password from Kubernetes secret")
        else:
            actual_password = decoded_password
            print("✅ Retrieved plain password from Kubernetes secret")

    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to get Kubernetes secret: {e}")
        return
    except Exception as e:
        print(f"❌ Error processing secret: {e}")
        return

    # Test database connection
    print("\n=== Step 2: Test database connection ===")

    try:
        user_conn = await asyncpg.connect(
            host=settings.DATABASE_HOST, port=5432, user=target_user, password=actual_password, database=target_database
        )
        print(f"✅ Successfully connected to database as {target_user}")

    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return

    # Test schema access
    print("\n=== Step 3: Test schema and table visibility ===")

    try:
        # List all tables in the schema
        tables = await user_conn.fetch(f"""
            SELECT table_name, table_type
            FROM information_schema.tables 
            WHERE table_schema = '{target_schema}'
            ORDER BY table_name
        """)

        if tables:
            print(f"✅ User can see {len(tables)} tables in schema {target_schema}:")
            for table in tables:
                print(f"  - {table['table_name']} ({table['table_type']})")
        else:
            print(f"❌ User cannot see any tables in schema {target_schema}")
            await user_conn.close()
            return

    except Exception as e:
        print(f"❌ Error listing tables: {e}")
        await user_conn.close()
        return

    # Test specific table permissions - the problematic alembic_version table
    print("\n=== Step 4: Test alembic_version table permissions ===")

    alembic_table = f"{target_schema}.alembic_version"

    try:
        # Try the exact query that's failing
        version = await user_conn.fetchval(f"""
            SELECT version_num FROM {alembic_table}
        """)

        if version:
            print(f"✅ Successfully queried alembic_version table - version: {version}")
        else:
            print("✅ Successfully queried alembic_version table - no version found")

    except Exception as e:
        print(f"❌ Failed to query alembic_version table: {e}")

        # Check table owner and permissions
        try:
            print("\n=== Investigating alembic_version permissions ===")

            # Check table owner
            table_owner = await user_conn.fetchval(f"""
                SELECT tableowner 
                FROM pg_tables 
                WHERE schemaname = '{target_schema}' 
                AND tablename = 'alembic_version'
            """)

            print(f"Table owner: {table_owner}")

            if table_owner != target_user:
                print(f"❌ Table owner mismatch! Expected {target_user}, got {table_owner}")
            else:
                print("✅ Table owner is correct")

            # Check user's permissions on the table
            print(f"\nChecking {target_user}'s permissions on alembic_version:")

            # Check if user has SELECT permission
            has_select = await user_conn.fetchval(f"""
                SELECT has_table_privilege('{target_user}', '{alembic_table}', 'SELECT')
            """)
            print(f"  - SELECT permission: {has_select}")

            has_insert = await user_conn.fetchval(f"""
                SELECT has_table_privilege('{target_user}', '{alembic_table}', 'INSERT')
            """)
            print(f"  - INSERT permission: {has_insert}")

            has_update = await user_conn.fetchval(f"""
                SELECT has_table_privilege('{target_user}', '{alembic_table}', 'UPDATE')
            """)
            print(f"  - UPDATE permission: {has_update}")

            has_delete = await user_conn.fetchval(f"""
                SELECT has_table_privilege('{target_user}', '{alembic_table}', 'DELETE')
            """)
            print(f"  - DELETE permission: {has_delete}")

        except Exception as perm_e:
            print(f"❌ Error checking permissions: {perm_e}")

    # Test other tables for comparison
    print("\n=== Step 5: Test other tables for comparison ===")

    for table_info in tables[:3]:  # Test first 3 tables
        table_name = table_info["table_name"]
        if table_name == "alembic_version":
            continue

        full_table_name = f"{target_schema}.{table_name}"

        try:
            count = await user_conn.fetchval(f"""
                SELECT COUNT(*) FROM {full_table_name}
            """)
            print(f"✅ Can query {table_name} - {count} rows")

        except Exception as e:
            print(f"❌ Cannot query {table_name}: {e}")

            # Check this table's owner too
            try:
                table_owner = await user_conn.fetchval(f"""
                    SELECT tableowner 
                    FROM pg_tables 
                    WHERE schemaname = '{target_schema}' 
                    AND tablename = '{table_name}'
                """)
                print(f"  - {table_name} owner: {table_owner}")

            except Exception as owner_e:
                print(f"  - Error getting {table_name} owner: {owner_e}")

    await user_conn.close()

    print("\n=== Summary ===")
    print("The user can connect and see tables, but may have insufficient permissions")
    print("on specific tables. This suggests the schema ownership setting is not")
    print("automatically granting table-level permissions as expected.")


if __name__ == "__main__":
    asyncio.run(test_table_permissions())
