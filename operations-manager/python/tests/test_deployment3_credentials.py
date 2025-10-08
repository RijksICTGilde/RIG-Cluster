#!/usr/bin/env python3
"""
Test deployment3 database access using the actual credentials that should have been created
"""

import asyncio
import logging

from opi.connectors.postgres import create_postgres_connector
from opi.core.config import settings
from opi.core.database_pools import close_database_pools, get_database_pool, initialize_database_pools
from opi.manager.database_manager import DatabaseManager
from opi.manager.project_manager import ProjectManager

logger = logging.getLogger(__name__)


async def test_deployment3_database_access():
    """Test actual database access for deployment3 using real credentials."""

    target_database = "amt2_dev_deployment_3"
    target_schema = "amt2_dev_deployment_3"
    target_user = "amt2_dev_deployment_3"

    print("=== Testing Deployment3 Database Access ===")
    print(f"Database: {target_database}")
    print(f"Schema: {target_schema}")
    print(f"User: {target_user}")

    # Initialize database pools
    await initialize_database_pools()
    postgres_pool = get_database_pool("main")
    postgres_connector = await create_postgres_connector(postgres_pool)

    try:
        # Test 1: Verify user exists and get info
        print("\n=== Test 1: Check user existence and properties ===")

        # Connect as admin to check user
        admin_conn = postgres_connector._get_connection()

        # Switch to postgres database to check users
        await admin_conn.execute("SELECT 1")  # Test connection

        user_info = await admin_conn.fetch(
            """
            SELECT u.usename, u.usecreatedb, u.usesuper, u.userepl,
                   array_agg(DISTINCT r.rolname) as member_of_roles
            FROM pg_user u
            LEFT JOIN pg_auth_members m ON u.usesysid = m.member
            LEFT JOIN pg_roles r ON m.roleid = r.oid
            WHERE u.usename = $1
            GROUP BY u.usename, u.usecreatedb, u.usesuper, u.userepl
        """,
            target_user,
        )

        if user_info:
            user = user_info[0]
            print(f"✅ User {target_user} exists")
            print(f"  - Can create databases: {user['usecreatedb']}")
            print(f"  - Is superuser: {user['usesuper']}")
            print(f"  - Can replicate: {user['userepl']}")
            print(f"  - Member of roles: {user['member_of_roles']}")
        else:
            print(f"❌ User {target_user} does NOT exist")
            return

        # Test 2: Check database and schema existence + ownership
        print("\n=== Test 2: Check database and schema ownership ===")

        # Check if target database exists
        db_exists = await admin_conn.fetchval(
            """
            SELECT 1 FROM pg_database WHERE datname = $1
        """,
            target_database,
        )

        if db_exists:
            print(f"✅ Database {target_database} exists")

            # Get database owner
            db_owner = await admin_conn.fetchval(
                """
                SELECT pg_get_userbyid(datdba) as owner 
                FROM pg_database 
                WHERE datname = $1
            """,
                target_database,
            )
            print(f"  - Database owner: {db_owner}")

        else:
            print(f"❌ Database {target_database} does not exist")
            return

        # Connect directly to the target database to check schema
        await admin_conn.close()
        import asyncpg

        admin_conn = await asyncpg.connect(
            host=settings.DATABASE_HOST,
            port=5432,
            user=settings.DATABASE_ADMIN_NAME,
            password=settings.DATABASE_ADMIN_PASSWORD,
            database=target_database,
        )

        # Check schema existence and ownership
        schema_info = await admin_conn.fetch(
            """
            SELECT s.schema_name, s.schema_owner,
                   n.nspowner::regrole as owner_role
            FROM information_schema.schemata s
            JOIN pg_namespace n ON n.nspname = s.schema_name  
            WHERE s.schema_name = $1
        """,
            target_schema,
        )

        if schema_info:
            schema = schema_info[0]
            print(f"✅ Schema {target_schema} exists")
            print(f"  - Schema owner (info_schema): {schema['schema_owner']}")
            print(f"  - Schema owner (pg_namespace): {schema['owner_role']}")

            # Check if schema owner matches target user
            if str(schema["owner_role"]) == target_user:
                print("✅ Schema ownership is correct")
            else:
                print(f"❌ Schema ownership is WRONG - expected {target_user}, got {schema['owner_role']}")
        else:
            print(f"❌ Schema {target_schema} does not exist")
            return

        # Test 3: Try to authenticate as the target user
        print("\n=== Test 3: Test user authentication ===")
        print(f"Note: We need to find the actual password for user {target_user}")

        # Create a project manager to simulate the credential lookup process
        project_manager = ProjectManager()
        database_manager = DatabaseManager(project_manager, postgres_pool)
        await database_manager._ensure_connection()

        try:
            # Try to get credentials like the real deployment would
            project_data = {"name": "amt2-dev"}
            deployment = {"name": "deployment-3"}

            # This should return the actual password being used
            existing_credentials = await database_manager._get_existing_database_credentials_from_k8s(
                "deployment-3", deployment
            )

            if existing_credentials and existing_credentials.password:
                print("✅ Found existing credentials in Kubernetes")
                actual_password = existing_credentials.password

                # Test connection with actual password
                print("Testing connection with retrieved password...")

                connection_valid = await database_manager._test_database_connection(
                    target_user, actual_password, target_database, target_schema
                )

                if connection_valid:
                    print("✅ Authentication SUCCESSFUL with retrieved password")

                    # Test schema access
                    print("Testing schema access...")

                    # Connect as the target user
                    import asyncpg

                    user_conn = await asyncpg.connect(
                        host=settings.DATABASE_HOST,
                        port=5432,
                        user=target_user,
                        password=actual_password,
                        database=target_database,
                    )

                    # Test table access
                    tables = await user_conn.fetch(f"""
                        SELECT table_name, table_type
                        FROM information_schema.tables 
                        WHERE table_schema = '{target_schema}'
                        ORDER BY table_name
                        LIMIT 10
                    """)

                    if tables:
                        print(f"✅ User can access schema - found {len(tables)} tables:")
                        for table in tables:
                            print(f"  - {table['table_name']} ({table['table_type']})")

                        # Test actual data access
                        try:
                            # Try to select from first table
                            first_table = tables[0]["table_name"]
                            count = await user_conn.fetchval(f"""
                                SELECT COUNT(*) FROM {target_schema}.{first_table}
                            """)
                            print(f"✅ User can query data - table {first_table} has {count} rows")

                        except Exception as e:
                            print(f"❌ User cannot query data from {first_table}: {e}")
                    else:
                        print(f"❌ User cannot see any tables in schema {target_schema}")

                    await user_conn.close()

                else:
                    print("❌ Authentication FAILED with retrieved password")
                    print("This suggests password mismatch between K8s secret and database")

            else:
                print("❌ No existing credentials found in Kubernetes")
                print("This suggests the credential creation step failed")

        except Exception as e:
            print(f"❌ Error during credential testing: {e}")
            logger.exception("Credential test error")

        await database_manager.close()

    except Exception as e:
        print(f"❌ Error during database access test: {e}")
        logger.exception("Database access test error")

    finally:
        await postgres_connector.close()
        await close_database_pools()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(test_deployment3_database_access())
