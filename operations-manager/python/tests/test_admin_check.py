#!/usr/bin/env python3
"""
Check deployment-3 database as admin to see what schemas and tables actually exist
"""

import asyncio

import asyncpg
from opi.core.config import settings


async def test_admin_check():
    """Check deployment-3 database as admin."""

    target_database = "amt2_dev_deployment_3"
    target_schema = "amt2_dev_deployment_3"

    print(f"=== Admin Check of {target_database} ===")

    try:
        # Connect as admin
        admin_conn = await asyncpg.connect(
            host=settings.DATABASE_HOST,
            port=5432,
            user=settings.DATABASE_ADMIN_NAME,
            password=settings.DATABASE_ADMIN_PASSWORD,
            database=target_database,
        )
        print(f"✅ Connected as admin to {target_database}")

        # List all schemas in the database
        print(f"\n=== All Schemas in {target_database} ===")
        schemas = await admin_conn.fetch("""
            SELECT schema_name, schema_owner
            FROM information_schema.schemata
            WHERE schema_name NOT IN ('information_schema', 'pg_catalog', 'pg_toast')
            ORDER BY schema_name
        """)

        for schema in schemas:
            print(f"  - {schema['schema_name']} (owner: {schema['schema_owner']})")

        # For each schema, list tables
        for schema in schemas:
            schema_name = schema["schema_name"]
            print(f"\n=== Tables in schema {schema_name} ===")

            tables = await admin_conn.fetch(
                """
                SELECT table_name, table_type, 
                       (SELECT tableowner FROM pg_tables WHERE schemaname = t.table_schema AND tablename = t.table_name) as table_owner
                FROM information_schema.tables t
                WHERE table_schema = $1
                ORDER BY table_name
            """,
                schema_name,
            )

            if tables:
                print(f"Found {len(tables)} tables:")
                for table in tables:
                    print(f"  - {table['table_name']} ({table['table_type']}) - owner: {table['table_owner']}")

                    # For key tables, show row counts
                    if table["table_name"] in ["alembic_version", "user", "algorithm"]:
                        try:
                            count = await admin_conn.fetchval(f"""
                                SELECT COUNT(*) FROM {schema_name}.{table['table_name']}
                            """)
                            print(f"    └─ {count} rows")
                        except Exception as e:
                            print(f"    └─ Error counting rows: {e}")
            else:
                print("  No tables found")

        # Specifically check what happens when we try to query alembic_version as the target user
        print("\n=== Testing Target User Access to alembic_version ===")

        try:
            # Check if alembic_version exists and is accessible
            alembic_owner = await admin_conn.fetchval(f"""
                SELECT tableowner 
                FROM pg_tables 
                WHERE schemaname = '{target_schema}' 
                AND tablename = 'alembic_version'
            """)

            if alembic_owner:
                print(f"alembic_version table owner: {alembic_owner}")

                # Check permissions for target user
                has_select = await admin_conn.fetchval(f"""
                    SELECT has_table_privilege('amt2_dev_deployment_3', '{target_schema}.alembic_version', 'SELECT')
                """)
                print(f"amt2_dev_deployment_3 has SELECT on alembic_version: {has_select}")

                # Show the actual table definition
                table_acl = await admin_conn.fetch(f"""
                    SELECT grantee, privilege_type, is_grantable
                    FROM information_schema.table_privileges
                    WHERE table_schema = '{target_schema}'
                    AND table_name = 'alembic_version'
                    ORDER BY grantee, privilege_type
                """)

                if table_acl:
                    print("Table privileges on alembic_version:")
                    for priv in table_acl:
                        print(f"  - {priv['grantee']}: {priv['privilege_type']} (grantable: {priv['is_grantable']})")
                else:
                    print("No explicit table privileges found on alembic_version")

            else:
                print("❌ alembic_version table not found")

        except Exception as e:
            print(f"❌ Error checking alembic_version: {e}")

        await admin_conn.close()

    except Exception as e:
        print(f"❌ Error during admin check: {e}")


if __name__ == "__main__":
    asyncio.run(test_admin_check())
