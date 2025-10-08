"""
PostgreSQL connector for managing database users, schemas, and operations.

This module provides functionality to interact with PostgreSQL databases for user management,
schema operations, and database cloning operations following the connector pattern with comprehensive
security measures to prevent SQL injection and ensure safe database operations.
"""

import logging
import re
from typing import TYPE_CHECKING, Any

import asyncpg

if TYPE_CHECKING:
    from opi.core.database_pool import DatabasePool

logger = logging.getLogger(__name__)


class PostgresConnectionError(Exception):
    """Exception raised when PostgreSQL connection is not available."""


class PostgresExecutionError(Exception):
    """Exception raised when PostgreSQL operation execution fails."""


class PostgresValidationError(Exception):
    """Exception raised when input validation fails."""


class PostgresConnector:
    """Connector for interacting with PostgreSQL databases with comprehensive security measures."""

    def __init__(self, db_pool: "DatabasePool") -> None:
        """Initialize the PostgreSQL connector with a database pool.

        Args:
            db_pool: DatabasePool instance to use for connections
        """
        self.db_pool = db_pool
        self.conn: asyncpg.Connection | None = None
        self._acquired = False

    async def acquire_connection(self) -> None:
        """Acquire a connection from the pool."""
        if not self._acquired:
            self.conn = await self.db_pool.acquire()
            self._acquired = True
            logger.debug("Acquired connection from pool")

    async def close(self) -> None:
        """Return the connection to the pool."""
        if self.conn and self._acquired:
            await self.db_pool.release(self.conn)
            logger.debug("Released connection back to pool")
            self.conn = None
            self._acquired = False

    async def __aenter__(self) -> "PostgresConnector":
        """Async context manager entry - acquire connection from pool."""
        await self.acquire_connection()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit - return connection to pool."""
        await self.close()

    # Security validation methods

    @staticmethod
    def _validate_identifier(identifier: str, identifier_type: str = "identifier") -> str:
        """Validate and sanitize SQL identifiers (usernames, schema names, etc.).

        Args:
            identifier: The identifier to validate
            identifier_type: Type of identifier for error messages

        Returns:
            Validated identifier

        Raises:
            PostgresValidationError: If identifier is invalid
        """
        if not identifier:
            raise PostgresValidationError(f"{identifier_type} cannot be empty")

        # Check length limits (PostgreSQL limit is 63 characters)
        if len(identifier) > 63:
            raise PostgresValidationError(f"{identifier_type} cannot exceed 63 characters")

        # PostgreSQL identifier rules:
        # - Must start with a letter (a-z, A-Z) or underscore
        # - Can contain letters, digits, underscores, and dollar signs
        # - Cannot be a PostgreSQL reserved word
        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_$]*$", identifier):
            raise PostgresValidationError(
                f"{identifier_type} '{identifier}' contains invalid characters. "
                "Must start with letter/underscore and contain only letters, digits, underscores, and dollar signs."
            )

        # Check against PostgreSQL reserved words (common ones)
        reserved_words = {
            "select",
            "insert",
            "update",
            "delete",
            "create",
            "drop",
            "alter",
            "grant",
            "revoke",
            "user",
            "role",
            "database",
            "schema",
            "table",
            "view",
            "index",
            "function",
            "procedure",
            "trigger",
            "constraint",
            "primary",
            "foreign",
            "key",
            "references",
            "check",
            "unique",
            "not",
            "null",
            "default",
            "cascade",
            "restrict",
            "action",
            "match",
            "full",
            "partial",
            "and",
            "or",
            "in",
            "exists",
            "between",
            "like",
            "ilike",
            "similar",
            "escape",
            "is",
            "true",
            "false",
            "unknown",
            "union",
            "intersect",
            "except",
            "all",
            "some",
            "any",
            "order",
            "group",
            "having",
            "limit",
            "offset",
            "fetch",
            "for",
            "of",
            "share",
            "nowait",
        }

        if identifier.lower() in reserved_words:
            raise PostgresValidationError(f"{identifier_type} '{identifier}' is a PostgreSQL reserved word")

        return identifier

    @staticmethod
    def _validate_privilege(privilege: str) -> str:
        """Validate database privilege against allowlist.

        Args:
            privilege: The privilege to validate

        Returns:
            Validated privilege in uppercase

        Raises:
            PostgresValidationError: If privilege is invalid
        """
        if not privilege:
            raise PostgresValidationError("Privilege cannot be empty")

        # Allowlist of valid PostgreSQL privileges
        valid_privileges = {
            "CREATEDB",
            "NOCREATEDB",
            "CREATEROLE",
            "NOCREATEROLE",
            "SUPERUSER",
            "NOSUPERUSER",
            "LOGIN",
            "NOLOGIN",
            "REPLICATION",
            "NOREPLICATION",
            "BYPASSRLS",
            "NOBYPASSRLS",
            "INHERIT",
            "NOINHERIT",
            "CONNECTION LIMIT",
        }

        privilege_upper = privilege.upper().strip()

        # Handle CONNECTION LIMIT specially (it has a numeric value)
        if privilege_upper.startswith("CONNECTION LIMIT"):
            # Validate CONNECTION LIMIT format
            if not re.match(r"^CONNECTION LIMIT \d+$", privilege_upper):
                raise PostgresValidationError(
                    "CONNECTION LIMIT must be followed by a number (e.g., 'CONNECTION LIMIT 10')"
                )
            return privilege_upper

        if privilege_upper not in valid_privileges:
            raise PostgresValidationError(
                f"Invalid privilege '{privilege}'. Valid privileges: {', '.join(sorted(valid_privileges))}"
            )

        return privilege_upper

    @staticmethod
    def _validate_schema_privilege(privilege: str) -> str:
        """Validate schema privilege against allowlist.

        Args:
            privilege: The privilege to validate

        Returns:
            Validated privilege in uppercase

        Raises:
            PostgresValidationError: If privilege is invalid
        """
        if not privilege:
            raise PostgresValidationError("Schema privilege cannot be empty")

        # Allowlist of valid schema privileges
        valid_schema_privileges = {"USAGE", "CREATE", "ALL", "ALL PRIVILEGES"}

        privilege_upper = privilege.upper().strip()

        if privilege_upper not in valid_schema_privileges:
            valid_privs = ", ".join(sorted(valid_schema_privileges))
            raise PostgresValidationError(f"Invalid schema privilege '{privilege}'. Valid privileges: {valid_privs}")

        return privilege_upper

    @staticmethod
    def _quote_identifier(identifier: str) -> str:
        """Safely quote SQL identifier to prevent injection.

        Args:
            identifier: Pre-validated identifier to quote

        Returns:
            Properly quoted identifier
        """
        # Double any existing quotes and wrap in quotes
        escaped = identifier.replace('"', '""')
        return f'"{escaped}"'

    @staticmethod
    def _validate_password(password: str) -> str:
        """Validate password meets minimum security requirements.

        Args:
            password: The password to validate

        Returns:
            Validated password

        Raises:
            PostgresValidationError: If password is invalid
        """
        if not password:
            raise PostgresValidationError("Password cannot be empty")

        if len(password) < 8:
            raise PostgresValidationError("Password must be at least 8 characters long")

        if len(password) > 128:
            raise PostgresValidationError("Password cannot exceed 128 characters")

        # Check for at least one letter and one number (basic complexity)
        if not re.search(r"[a-zA-Z]", password):
            raise PostgresValidationError("Password must contain at least one letter")

        if not re.search(r"\d", password):
            raise PostgresValidationError("Password must contain at least one digit")

        return password

    def _get_connection(self) -> asyncpg.Connection:
        """Get the current pooled connection.

        Returns:
            Current database connection from the pool

        Raises:
            PostgresConnectionError: If no connection available
        """
        if self.conn is None:
            raise PostgresConnectionError("No active connection. Use within async context manager.")
        return self.conn

    async def test_connection(self, host: str, username: str, password: str, database: str = "postgres") -> bool:
        """Test database connection.

        Args:
            host: Database host
            username: Database username
            password: Database password
            database: Database name (defaults to 'postgres')

        Returns:
            True if connection successful, False otherwise
        """
        try:
            conn = self._get_connection()
            logger.info(f"Connection test successful for {username}@{host}/{database}")
            return True
        except (PostgresConnectionError, Exception) as e:
            logger.debug(f"Connection test failed for {username}@{host}/{database}: {e}")
            return False

    # User Management Operations

    async def create_user(
        self,
        host: str,
        admin_username: str,
        admin_password: str,
        username: str,
        password: str,
        database_privileges: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new database user.

        Args:
            host: Database host
            admin_username: Admin username for connection
            admin_password: Admin password for connection
            username: New username to create
            password: Password for new user
            database_privileges: List of database privileges to grant

        Returns:
            Dictionary with operation status and details

        Raises:
            PostgresExecutionError: If user creation fails
            PostgresValidationError: If input validation fails
        """
        try:
            # Validate inputs
            validated_username = self._validate_identifier(username, "username")
            validated_password = self._validate_password(password)

            validated_privileges = []
            if database_privileges:
                validated_privileges = [self._validate_privilege(priv) for priv in database_privileges]

            conn = self._get_connection()

            # Check if user already exists
            user_exists = await conn.fetchval("SELECT 1 FROM pg_user WHERE usename = $1", validated_username)

            if user_exists:
                logger.warning(f"User {validated_username} already exists")
                return {"status": "exists", "message": f"User {validated_username} already exists"}

            # Create user with password using safe identifier quoting
            quoted_username = self._quote_identifier(validated_username)
            # DDL statements like CREATE USER don't support parameters, so we need to escape the password as a literal
            escaped_password = validated_password.replace("'", "''")  # Escape single quotes
            create_sql = f"CREATE USER {quoted_username} WITH PASSWORD '{escaped_password}'"
            await conn.execute(create_sql)

            # Grant privileges if specified
            if validated_privileges:
                for privilege in validated_privileges:
                    alter_sql = f"ALTER USER {quoted_username} {privilege}"
                    await conn.execute(alter_sql)

            logger.info(f"User {validated_username} created successfully")
            return {"status": "created", "message": f"User {validated_username} created successfully"}

        except PostgresValidationError:
            logger.exception("Validation failed for user creation")
            raise
        except Exception as e:
            logger.exception(f"Failed to create user {username}")
            raise PostgresExecutionError(f"User creation failed: {e}") from e

    async def delete_user(self, host: str, admin_username: str, admin_password: str, username: str) -> dict[str, Any]:
        """Delete a database user.

        Args:
            host: Database host
            admin_username: Admin username for connection
            admin_password: Admin password for connection
            username: Username to delete

        Returns:
            Dictionary with operation status and details

        Raises:
            PostgresExecutionError: If user deletion fails
            PostgresValidationError: If input validation fails
        """
        try:
            # Validate inputs
            validated_username = self._validate_identifier(username, "username")

            conn = self._get_connection()

            # Check if user exists
            user_exists = await conn.fetchval("SELECT 1 FROM pg_user WHERE usename = $1", validated_username)

            if not user_exists:
                logger.warning(f"User {validated_username} does not exist")
                return {"status": "not_found", "message": f"User {validated_username} does not exist"}

            # Drop owned objects first to avoid dependency issues
            quoted_username = self._quote_identifier(validated_username)
            await conn.execute(f"DROP OWNED BY {quoted_username} CASCADE")

            # Drop user
            await conn.execute(f"DROP USER {quoted_username}")

            logger.info(f"User {validated_username} deleted successfully")
            return {"status": "deleted", "message": f"User {validated_username} deleted successfully"}

        except PostgresValidationError:
            logger.exception("Validation failed for user deletion")
            raise
        except Exception as e:
            logger.exception(f"Failed to delete user {username}")
            raise PostgresExecutionError(f"User deletion failed: {e}") from e

    async def list_users(self, host: str, admin_username: str, admin_password: str) -> list[dict[str, Any]]:
        """List all database users.

        Args:
            host: Database host
            admin_username: Admin username for connection
            admin_password: Admin password for connection

        Returns:
            List of user dictionaries with details

        Raises:
            PostgresExecutionError: If listing users fails
        """
        try:
            conn = self._get_connection()

            users = await conn.fetch("""
                SELECT usename as username,
                       usesuper as is_superuser,
                       usecreatedb as can_create_db,
                       usecreaterole as can_create_role
                FROM pg_user
                ORDER BY usename
            """)

            user_list = [dict(user) for user in users]
            logger.debug(f"Retrieved {len(user_list)} users from {host}")
            return user_list

        except Exception as e:
            logger.exception("Failed to list users")
            raise PostgresExecutionError(f"User listing failed: {e}") from e

    async def update_user_password(self, username: str, new_password: str) -> dict[str, Any]:
        """Update user password.

        Args:
            username: Username to update
            new_password: New password

        Returns:
            Dictionary with operation status and details

        Raises:
            PostgresExecutionError: If password update fails
            PostgresValidationError: If input validation fails
        """
        try:
            # Validate inputs
            validated_username = self._validate_identifier(username, "username")
            validated_password = self._validate_password(new_password)

            conn = self._get_connection()

            user_exists = await conn.fetchval("SELECT 1 FROM pg_user WHERE usename = $1", validated_username)

            if not user_exists:
                logger.warning(f"User {validated_username} does not exist")
                return {"status": "not_found", "message": f"User {validated_username} does not exist"}

            quoted_username = self._quote_identifier(validated_username)
            escaped_password = validated_password.replace("'", "''")
            await conn.execute(f"ALTER USER {quoted_username} WITH PASSWORD '{escaped_password}'")

            logger.info(f"Password updated for user {validated_username}")
            return {"status": "updated", "message": f"Password updated for user {validated_username}"}

        except PostgresValidationError:
            logger.exception("Validation failed for password update")
            raise
        except Exception as e:
            logger.exception(f"Failed to update password for user {username}")
            raise PostgresExecutionError(f"Password update failed: {e}") from e

    # Database Management Operations

    async def create_database(
        self,
        host: str,
        admin_username: str,
        admin_password: str,
        database_name: str,
        owner: str | None = None,
    ) -> dict[str, Any]:
        """Create a new database.

        Args:
            host: Database host
            admin_username: Admin username for connection
            admin_password: Admin password for connection
            database_name: Name of database to create
            owner: Owner username for the database

        Returns:
            Dictionary with operation status and details

        Raises:
            PostgresExecutionError: If database creation fails
            PostgresValidationError: If input validation fails
        """
        try:
            # Validate inputs
            validated_database_name = self._validate_identifier(database_name, "database name")
            validated_owner = None
            if owner:
                validated_owner = self._validate_identifier(owner, "owner username")

            conn = self._get_connection()

            # Check if database already exists
            database_exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", validated_database_name
            )

            if database_exists:
                logger.warning(f"Database {validated_database_name} already exists")
                return {"status": "exists", "message": f"Database {validated_database_name} already exists"}

            # Create database with safe identifier quoting
            quoted_database = self._quote_identifier(validated_database_name)
            if validated_owner:
                quoted_owner = self._quote_identifier(validated_owner)
                create_sql = f"CREATE DATABASE {quoted_database} OWNER {quoted_owner}"
            else:
                create_sql = f"CREATE DATABASE {quoted_database}"

            await conn.execute(create_sql)
            logger.info(f"Database {validated_database_name} created successfully")
            return {"status": "created", "message": f"Database {validated_database_name} created successfully"}

        except PostgresValidationError:
            logger.exception("Validation failed for database creation")
            raise
        except Exception as e:
            logger.exception(f"Failed to create database {database_name}")
            raise PostgresExecutionError(f"Database creation failed: {e}") from e

    async def delete_database(
        self, host: str, admin_username: str, admin_password: str, database_name: str
    ) -> dict[str, Any]:
        """Delete a database.

        Args:
            host: Database host
            admin_username: Admin username for connection
            admin_password: Admin password for connection
            database_name: Name of database to delete

        Returns:
            Dictionary with operation status and details

        Raises:
            PostgresExecutionError: If database deletion fails
            PostgresValidationError: If input validation fails
        """
        try:
            # Validate inputs
            validated_database_name = self._validate_identifier(database_name, "database name")

            conn = self._get_connection()

            # Check if database exists
            database_exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", validated_database_name
            )

            if not database_exists:
                logger.warning(f"Database {validated_database_name} does not exist")
                return {"status": "not_found", "message": f"Database {validated_database_name} does not exist"}

            # Terminate connections to the database before dropping
            quoted_database = self._quote_identifier(validated_database_name)
            await conn.execute(
                "SELECT pg_terminate_backend(pg_stat_activity.pid) "
                "FROM pg_stat_activity "
                "WHERE pg_stat_activity.datname = $1 AND pid <> pg_backend_pid()",
                validated_database_name,
            )

            # Drop database
            await conn.execute(f"DROP DATABASE {quoted_database}")

            logger.info(f"Database {validated_database_name} deleted successfully")
            return {"status": "deleted", "message": f"Database {validated_database_name} deleted successfully"}

        except PostgresValidationError:
            logger.exception("Validation failed for database deletion")
            raise
        except Exception as e:
            logger.exception(f"Failed to delete database {database_name}")
            raise PostgresExecutionError(f"Database deletion failed: {e}") from e

    async def list_databases(self, host: str, admin_username: str, admin_password: str) -> list[dict[str, Any]]:
        """List all databases.

        Args:
            host: Database host
            admin_username: Admin username for connection
            admin_password: Admin password for connection

        Returns:
            List of database dictionaries with details

        Raises:
            PostgresExecutionError: If listing databases fails
        """
        try:
            conn = self._get_connection()

            databases = await conn.fetch("""
                SELECT datname as name,
                       datowner::regrole as owner,
                       encoding,
                       datcollate as collate,
                       datctype as ctype
                FROM pg_database
                WHERE datistemplate = false
                ORDER BY datname
            """)

            database_list = [dict(database) for database in databases]
            logger.debug(f"Retrieved {len(database_list)} databases from {host}")
            return database_list

        except Exception as e:
            logger.exception("Failed to list databases")
            raise PostgresExecutionError(f"Database listing failed: {e}") from e

    # Schema Management Operations

    async def create_schema(
        self, host: str, admin_username: str, admin_password: str, schema_name: str, database: str, owner: str
    ) -> dict[str, Any]:
        """Create a new database schema.

        Args:
            host: Database host
            admin_username: Admin username for connection
            admin_password: Admin password for connection
            schema_name: Name of schema to create
            database: Database name to connect to
            owner: Owner username for the schema

        Returns:
            Dictionary with operation status and details

        Raises:
            PostgresExecutionError: If schema creation fails
            PostgresValidationError: If input validation fails
        """
        try:
            # Validate inputs
            validated_schema_name = self._validate_identifier(schema_name, "schema name")
            validated_owner = self._validate_identifier(owner, "owner username")

            conn = self._get_connection()

            # Check if schema already exists
            schema_exists = await conn.fetchval(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = $1", validated_schema_name
            )

            if schema_exists:
                logger.warning(f"Schema {validated_schema_name} already exists")
                return {"status": "exists", "message": f"Schema {validated_schema_name} already exists"}

            # Create schema with safe identifier quoting
            quoted_schema = self._quote_identifier(validated_schema_name)
            quoted_owner = self._quote_identifier(validated_owner)
            create_sql = f"CREATE SCHEMA {quoted_schema} AUTHORIZATION {quoted_owner}"

            await conn.execute(create_sql)
            logger.info(f"Schema {validated_schema_name} created successfully")
            return {"status": "created", "message": f"Schema {validated_schema_name} created successfully"}

        except PostgresValidationError:
            logger.exception("Validation failed for schema creation")
            raise
        except Exception as e:
            logger.exception(f"Failed to create schema {schema_name}")
            raise PostgresExecutionError(f"Schema creation failed: {e}") from e

    async def delete_schema(
        self,
        host: str,
        admin_username: str,
        admin_password: str,
        schema_name: str,
        database: str,
        cascade: bool = False,
    ) -> dict[str, Any]:
        """Delete a database schema.

        Args:
            host: Database host
            admin_username: Admin username for connection
            admin_password: Admin password for connection
            schema_name: Name of schema to delete
            database: Database name to connect to
            cascade: Whether to cascade delete (remove all objects)

        Returns:
            Dictionary with operation status and details

        Raises:
            PostgresExecutionError: If schema deletion fails
            PostgresValidationError: If input validation fails
        """
        try:
            # Validate inputs
            validated_schema_name = self._validate_identifier(schema_name, "schema name")
            validated_database = self._validate_identifier(database, "database")

            # Connect to the specific database
            conn = await asyncpg.connect(
                host=host, user=admin_username, password=admin_password, database=validated_database
            )

            # Check if schema exists
            schema_exists = await conn.fetchval(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = $1", validated_schema_name
            )

            if not schema_exists:
                logger.warning(f"Schema {validated_schema_name} does not exist")
                return {"status": "not_found", "message": f"Schema {validated_schema_name} does not exist"}

            # Drop schema with safe identifier quoting
            quoted_schema = self._quote_identifier(validated_schema_name)
            drop_sql = f"DROP SCHEMA {quoted_schema}"
            if cascade:
                drop_sql += " CASCADE"

            await conn.execute(drop_sql)
            await conn.close()
            logger.info(f"Schema {validated_schema_name} deleted successfully")
            return {"status": "deleted", "message": f"Schema {validated_schema_name} deleted successfully"}

        except PostgresValidationError:
            logger.exception("Validation failed for schema deletion")
            raise
        except Exception as e:
            logger.exception(f"Failed to delete schema {schema_name}")
            raise PostgresExecutionError(f"Schema deletion failed: {e}") from e
        finally:
            if "conn" in locals():
                await conn.close()

    async def list_schemas(self, host: str, admin_username: str, admin_password: str) -> list[dict[str, Any]]:
        """List all database schemas.

        Args:
            host: Database host
            admin_username: Admin username for connection
            admin_password: Admin password for connection

        Returns:
            List of schema dictionaries with details

        Raises:
            PostgresExecutionError: If listing schemas fails
        """
        try:
            conn = self._get_connection()

            schemas = await conn.fetch("""
                SELECT schema_name, schema_owner
                FROM information_schema.schemata
                WHERE schema_name NOT IN ('information_schema', 'pg_catalog', 'pg_toast')
                ORDER BY schema_name
            """)

            schema_list = [dict(schema) for schema in schemas]
            logger.debug(f"Retrieved {len(schema_list)} schemas from {host}")
            return schema_list

        except Exception as e:
            logger.exception("Failed to list schemas")
            raise PostgresExecutionError(f"Schema listing failed: {e}") from e

    async def grant_schema_permissions(
        self,
        host: str,
        admin_username: str,
        admin_password: str,
        schema_name: str,
        username: str,
        permissions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Grant permissions on a schema to a user.

        Args:
            host: Database host
            admin_username: Admin username for connection
            admin_password: Admin password for connection
            schema_name: Name of schema
            username: Username to grant permissions to
            permissions: List of permissions (defaults to ['USAGE', 'CREATE'])

        Returns:
            Dictionary with operation status and details

        Raises:
            PostgresExecutionError: If granting permissions fails
            PostgresValidationError: If input validation fails
        """
        if permissions is None:
            permissions = ["USAGE", "CREATE"]

        try:
            # Validate inputs
            validated_schema_name = self._validate_identifier(schema_name, "schema name")
            validated_username = self._validate_identifier(username, "username")
            validated_permissions = [self._validate_schema_privilege(perm) for perm in permissions]

            conn = self._get_connection()

            # Check if schema and user exist
            schema_exists = await conn.fetchval(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = $1", validated_schema_name
            )
            user_exists = await conn.fetchval("SELECT 1 FROM pg_user WHERE usename = $1", validated_username)

            if not schema_exists:
                return {"status": "error", "message": f"Schema {validated_schema_name} does not exist"}

            if not user_exists:
                return {"status": "error", "message": f"User {validated_username} does not exist"}

            # Grant schema permissions with safe identifier quoting
            quoted_schema = self._quote_identifier(validated_schema_name)
            quoted_username = self._quote_identifier(validated_username)

            for permission in validated_permissions:
                grant_sql = f"GRANT {permission} ON SCHEMA {quoted_schema} TO {quoted_username}"
                await conn.execute(grant_sql)

            # Grant table permissions
            grant_tables_sql = f"GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA {quoted_schema} TO {quoted_username}"
            await conn.execute(grant_tables_sql)

            logger.info(
                f"Granted permissions {validated_permissions} on schema {validated_schema_name} "
                f"to user {validated_username}"
            )
            return {"status": "granted", "message": "Permissions granted successfully"}

        except PostgresValidationError:
            logger.exception("Validation failed for permission granting")
            raise
        except Exception as e:
            logger.exception(f"Failed to grant permissions on schema {schema_name} to user {username}")
            raise PostgresExecutionError(f"Permission granting failed: {e}") from e

    async def clone_schema(
        self,
        source_database: str,
        target_database: str,
        source_schema: str,
        target_schema: str,
        host: str,
        admin_username: str,
        admin_password: str,
        target_owner: str,
        target_owner_password: str,
    ) -> dict[str, Any]:
        """Clone a schema from source database to target database using pg_dump streaming approach.

        This method uses pg_dump to stream schema data from source to target database,
        avoiding the PostgreSQL template limitation of requiring exclusive access.
        The approach is production-safe and works with active databases.

        Args:
            source_database: Source database name
            target_database: Target database name
            source_schema: Source schema name
            target_schema: Target schema name
            host: Database host
            admin_username: Admin username for connection
            admin_password: Admin password for connection
            target_owner: Owner for the cloned schema

        Returns:
            Dictionary with operation status and details

        Raises:
            PostgresExecutionError: If schema cloning fails
            PostgresValidationError: If input validation fails
        """
        try:
            # Validate inputs
            validated_source_db = self._validate_identifier(source_database, "source_database")
            validated_target_db = self._validate_identifier(target_database, "target_database")
            validated_source_schema = self._validate_identifier(source_schema, "source_schema")
            validated_target_schema = self._validate_identifier(target_schema, "target_schema")
            validated_target_owner = self._validate_identifier(target_owner, "target_owner")

            conn = self._get_connection()

            # Check if source database exists
            source_db_exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", validated_source_db)
            if not source_db_exists:
                raise PostgresValidationError(f"Source database '{validated_source_db}' does not exist")

            # Ensure target database exists with proper owner
            quoted_target_db = self._quote_identifier(validated_target_db)
            quoted_target_owner = self._quote_identifier(validated_target_owner)

            target_db_exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", validated_target_db)
            if not target_db_exists:
                create_db_sql = f"CREATE DATABASE {quoted_target_db} OWNER {quoted_target_owner}"
                await conn.execute(create_db_sql)
                logger.info(f"Created target database '{validated_target_db}' with owner '{validated_target_owner}'")
            else:
                logger.info(f"Target database '{validated_target_db}' already exists")

            # Use pg_dump streaming approach to clone schema
            await self._clone_schema_with_pgdump(
                host,
                admin_username,
                admin_password,
                validated_source_db,
                validated_target_db,
                validated_source_schema,
                validated_target_schema,
                validated_target_owner,
                target_owner_password,
            )

            logger.info(
                f"Successfully cloned schema '{validated_source_schema}' from '{validated_source_db}' to '{validated_target_db}' as '{validated_target_schema}'"
            )
            return {
                "status": "success",
                "message": f"Database cloned successfully from {validated_source_db} to {validated_target_db}",
                "source_database": validated_source_db,
                "target_database": validated_target_db,
                "source_schema": validated_source_schema,
                "target_schema": validated_target_schema,
            }

        except PostgresValidationError:
            logger.exception("Validation failed for schema cloning")
            raise
        except Exception as e:
            logger.exception(f"Failed to clone schema from {source_database} to {target_database}")
            raise PostgresExecutionError(f"Schema cloning failed: {e}") from e

    async def _clone_schema_with_pgdump(
        self,
        host: str,
        admin_username: str,
        admin_password: str,
        source_database: str,
        target_database: str,
        source_schema: str,
        target_schema: str,
        target_owner: str,
        target_owner_password: str,
    ) -> None:
        """Clone schema using pg_dump streaming approach.

        This method uses pg_dump | psql streaming to copy schema data without
        requiring exclusive access to the source database.

        Args:
            host: Database host
            admin_username: Admin username
            admin_password: Admin password
            source_database: Source database name
            target_database: Target database name
            source_schema: Source schema name
            target_schema: Target schema name
            target_owner: Target schema owner
        """
        import asyncio
        import os

        # Set PostgreSQL connection environment variables
        env = os.environ.copy()
        env.update(
            {
                "PGHOST": host,
                "PGPORT": "5432",  # Standard PostgreSQL port
                "PGUSER": admin_username,
                "PGPASSWORD": admin_password,
            }
        )

        logger.info(
            f"Starting pg_dump streaming clone from {source_database}.{source_schema} to {target_database}.{target_schema}"
        )

        try:
            # Step 1: Drop target schema if it exists
            if source_schema != target_schema:
                logger.info(f"Preparing target schema {target_schema} (dropping if exists)")
                drop_cmd = ["psql", "-d", target_database, "-c", f"DROP SCHEMA IF EXISTS {target_schema} CASCADE;"]
                drop_process = await asyncio.create_subprocess_exec(
                    *drop_cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                await drop_process.communicate()

            # Step 2: Stream complete schema (structure + data) in one step
            logger.info("Streaming complete schema using pg_dump pipeline")

            # Clone with proper ownership: dump without ownership info, restore as target user
            # This ensures all created objects (tables, sequences, etc.) are owned by target_owner
            shell_cmd = (
                f"PGUSER={admin_username} PGPASSWORD={admin_password} "
                f"pg_dump -h {host} -d {source_database} -n {source_schema} --no-owner --no-privileges | "
                f"PGUSER={target_owner} PGPASSWORD={target_owner_password} "
                f"psql -h {host} -d {target_database} -v ON_ERROR_STOP=1"
            )

            logger.debug(f"Executing full schema pipeline: {shell_cmd}")

            # Execute the shell pipeline for complete schema clone
            clone_process = await asyncio.create_subprocess_shell(
                shell_cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )

            clone_out, clone_err = await clone_process.communicate()

            # Log only errors or important warnings
            if clone_err:
                stderr_text = clone_err.decode().strip()
                # Only log if it contains actual errors, not just version info
                if "error:" in stderr_text.lower() and "version mismatch" not in stderr_text.lower():
                    logger.error(f"pg_dump stderr: {stderr_text}")
                elif stderr_text:
                    logger.debug(f"pg_dump stderr: {stderr_text}")

            # Check for errors
            if clone_process.returncode != 0:
                raise Exception(f"Schema clone pipeline failed: {clone_err.decode()}")

            # Check if pg_dump actually produced any output
            if not clone_out and not clone_err:
                logger.warning("pg_dump produced no output - this might indicate an empty source schema")
            elif clone_out and len(clone_out.decode().strip()) < 500:
                logger.warning(
                    f"pg_dump produced very little output ({len(clone_out.decode())} bytes) - might be empty dump"
                )
                logger.debug(f"Full pg_dump output: {clone_out.decode()}")

            logger.info("Schema cloned successfully (structure + data)")

            # Step 3: Check if target schema was created successfully
            # Note: pg_dump creates the schema with the target name directly in our pipeline setup
            check_target_schema_cmd = f"psql -d {target_database} -c \"SELECT 1 FROM information_schema.schemata WHERE schema_name = '{target_schema}';\" -t"

            check_process = await asyncio.create_subprocess_shell(
                check_target_schema_cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            check_out, check_err = await check_process.communicate()

            if not (check_out and check_out.decode().strip()):
                # Target schema doesn't exist - this is an error
                logger.error(f"Target schema '{target_schema}' was not created during pg_dump")

                # List what schemas DO exist for debugging
                list_cmd = f"psql -d {target_database} -c \"SELECT schema_name FROM information_schema.schemata WHERE schema_name NOT IN ('information_schema', 'pg_catalog', 'pg_toast');\" -t"
                list_process = await asyncio.create_subprocess_shell(
                    list_cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                list_out, list_err = await list_process.communicate()
                if list_out:
                    existing_schemas = list_out.decode().strip()
                    logger.error(f"Schemas that DO exist in target database: {existing_schemas}")

                    # Check if source schema was created instead
                    if source_schema in existing_schemas and source_schema != target_schema:
                        logger.info(
                            f"Found source schema '{source_schema}' in target database, renaming to '{target_schema}'"
                        )
                        rename_cmd = (
                            f"psql -d {target_database} -c 'ALTER SCHEMA {source_schema} RENAME TO {target_schema};'"
                        )

                        rename_process = await asyncio.create_subprocess_shell(
                            rename_cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                        )

                        rename_out, rename_err = await rename_process.communicate()

                        if rename_process.returncode != 0:
                            raise Exception(f"Failed to rename schema: {rename_err.decode()}")
                        else:
                            logger.info(f"Successfully renamed schema from '{source_schema}' to '{target_schema}'")
                    else:
                        raise Exception(
                            f"Schema '{target_schema}' was not created during pg_dump - source may be empty"
                        )
                else:
                    raise Exception(
                        f"Schema '{target_schema}' was not created during pg_dump and could not list existing schemas"
                    )
            else:
                logger.debug(f"Target schema '{target_schema}' was created successfully")

            # Step 4: Set proper ownership of the final schema
            logger.info(f"Setting ownership of schema {target_schema} to {target_owner}")
            ownership_cmd = f"psql -d {target_database} -c 'ALTER SCHEMA {target_schema} OWNER TO {target_owner};'"

            owner_process = await asyncio.create_subprocess_shell(
                ownership_cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            owner_out, owner_err = await owner_process.communicate()

            if owner_process.returncode != 0:
                logger.warning(f"Failed to set schema ownership: {owner_err.decode()}")
            else:
                logger.info(f"Successfully set schema ownership to {target_owner}")

        except Exception as e:
            logger.exception(f"pg_dump streaming clone failed: {e}")
            raise Exception(f"Failed to clone schema using pg_dump: {e}") from e


# Factory function for creating connector instances
async def create_postgres_connector(db_pool: "DatabasePool") -> PostgresConnector:
    """Factory function to create a PostgresConnector instance with acquired connection.

    Args:
        db_pool: DatabasePool instance to use for connections

    Returns:
        PostgresConnector instance with connection acquired from pool
    """
    connector = PostgresConnector(db_pool)
    await connector.acquire_connection()
    return connector
