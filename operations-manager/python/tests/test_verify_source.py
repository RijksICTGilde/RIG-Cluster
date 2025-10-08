#!/usr/bin/env python3
"""
Verify what's actually in the deployment-1 source database using direct pg_dump
"""

import asyncio
import os

from opi.core.config import settings


async def test_direct_pgdump():
    """Test pg_dump directly on deployment-1 to see what it produces."""

    source_database = "amt2_dev_deployment_1"
    source_schema = "amt2_dev_deployment_1"

    print(f"=== Testing pg_dump on {source_database}.{source_schema} ===")

    # Set environment variables for pg_dump
    env = os.environ.copy()
    env.update(
        {
            "PGHOST": settings.DATABASE_HOST,
            "PGPORT": "5432",
            "PGUSER": settings.DATABASE_ADMIN_NAME,
            "PGPASSWORD": settings.DATABASE_ADMIN_PASSWORD,
        }
    )

    print(f"Using connection: {settings.DATABASE_ADMIN_NAME}@{settings.DATABASE_HOST}")

    # Test 1: Check if schema exists in source database
    print("\n=== Test 1: Check if schema exists ===")
    schema_check_cmd = [
        "psql",
        "-d",
        source_database,
        "-t",
        "-c",
        f"SELECT EXISTS(SELECT 1 FROM information_schema.schemata WHERE schema_name = '{source_schema}');",
    ]

    schema_process = await asyncio.create_subprocess_exec(
        *schema_check_cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    schema_out, schema_err = await schema_process.communicate()

    if schema_process.returncode == 0:
        schema_exists = "t" in schema_out.decode().strip()
        print(f"Schema {source_schema} exists: {schema_exists}")
        if schema_err:
            print(f"Schema check warnings: {schema_err.decode()}")
    else:
        print(f"Schema check failed: {schema_err.decode()}")
        return

    if not schema_exists:
        print(f"❌ Schema {source_schema} does not exist in {source_database}!")
        return

    # Test 2: Count tables in schema
    print("\n=== Test 2: Count tables in schema ===")
    table_count_cmd = [
        "psql",
        "-d",
        source_database,
        "-t",
        "-c",
        f"SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = '{source_schema}';",
    ]

    table_process = await asyncio.create_subprocess_exec(
        *table_count_cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    table_out, table_err = await table_process.communicate()

    if table_process.returncode == 0:
        table_count = int(table_out.decode().strip())
        print(f"Tables in {source_schema}: {table_count}")
        if table_err:
            print(f"Table count warnings: {table_err.decode()}")
    else:
        print(f"Table count failed: {table_err.decode()}")
        return

    if table_count == 0:
        print(f"❌ Schema {source_schema} is EMPTY - no tables found!")
        return

    # Test 3: List table names
    print("\n=== Test 3: List tables ===")
    list_tables_cmd = [
        "psql",
        "-d",
        source_database,
        "-t",
        "-c",
        f"SELECT table_name FROM information_schema.tables WHERE table_schema = '{source_schema}' ORDER BY table_name;",
    ]

    list_process = await asyncio.create_subprocess_exec(
        *list_tables_cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    list_out, list_err = await list_process.communicate()

    if list_process.returncode == 0:
        tables = [t.strip() for t in list_out.decode().split("\n") if t.strip()]
        print(f"Table names: {tables}")
        if list_err:
            print(f"List tables warnings: {list_err.decode()}")
    else:
        print(f"List tables failed: {list_err.decode()}")
        return

    # Test 4: Try pg_dump on the schema
    print("\n=== Test 4: Test pg_dump output ===")
    pgdump_cmd = ["pg_dump", "-d", source_database, "-n", source_schema, "--no-owner", "--no-privileges"]

    dump_process = await asyncio.create_subprocess_exec(
        *pgdump_cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    dump_out, dump_err = await dump_process.communicate()

    print(f"pg_dump return code: {dump_process.returncode}")
    if dump_err:
        print(f"pg_dump stderr: {dump_err.decode()}")

    dump_output = dump_out.decode()
    print(f"pg_dump output length: {len(dump_output)} bytes")

    if len(dump_output) > 0:
        # Show first and last few lines
        lines = dump_output.split("\n")
        print("First 10 lines:")
        for line in lines[:10]:
            print(f"  {line}")
        if len(lines) > 20:
            print(f"  ... ({len(lines) - 20} more lines) ...")
            print("Last 10 lines:")
            for line in lines[-10:]:
                print(f"  {line}")
    else:
        print("❌ pg_dump produced NO OUTPUT - source schema is empty or dump failed")


if __name__ == "__main__":
    asyncio.run(test_direct_pgdump())
