"""
PostgreSQL connector for managing database users, schemas, and operations.

This module provides functionality to interact with PostgreSQL databases for user management,
schema operations, and database cloning operations following the connector pattern with comprehensive
security measures to prevent SQL injection and ensure safe database operations.
"""

import contextlib
import logging
import re
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


class PostgresConnectionError(Exception):
    """Exception raised when PostgreSQL connection is not available."""


class PostgresExecutionError(Exception):
    """Exception raised when PostgreSQL operation execution fails."""


class PostgresValidationError(Exception):
    """Exception raised when input validation fails."""


class PostgresConnector:
    """Connector for interacting with PostgreSQL databases using managed connections.

    This connector is bound to ONE PostgreSQL server with specific admin credentials.
    It maintains one internal connection that is created on first use and reused
    for subsequent operations. The connection is automatically validated and recreated if broken.
    """

    def __init__(self, host: str, admin_username: str, admin_password: str) -> None:
        """Initialize the PostgreSQL connector bound to one database server.

        Args:
            host: Database host (e.g., 'localhost', 'postgres-service.namespace.svc.cluster.local')
            admin_username: Admin username for database operations
            admin_password: Admin password for database operations

        Example:
            # For shared database
            connector = PostgresConnector("postgres.shared.svc", "postgres", "secret")

            # For namespace-specific database
            connector = PostgresConnector("myproject-db-rw.infra.svc", "postgres", "secret")
        """
        self._host = host
        self._admin_username = admin_username
        self._admin_password = admin_password
        self._conn: asyncpg.Connection | None = None
        self._current_database: str | None = None

    async def _get_or_create_connection(self, database: str = "postgres") -> asyncpg.Connection:
        """Get existing connection or create a new one to the specified database.

        Reuses the connection if targeting the same database. Switches databases
        if needed by closing old connection and creating new one.

        Args:
            database: Database name to connect to (defaults to 'postgres')

        Returns:
            Active database connection
        """
        # Need new connection if connecting to different database or no connection exists
        if self._conn is None or self._current_database != database:
            # Close old connection if exists
            if self._conn:
                with contextlib.suppress(OSError):
                    await self._conn.close()

            # Create new connection
            self._conn = await asyncpg.connect(
                host=self._host,
                user=self._admin_username,
                password=self._admin_password,
                database=database,
                port=5432,
            )
            self._current_database = database
            logger.debug(f"Created connection to {self._host}/{database}")
        else:
            # Test existing connection health
            try:
                await self._conn.fetchval("SELECT 1")
            except Exception:
                logger.warning(f"Connection to {self._host}/{database} broken, recreating")
                with contextlib.suppress(OSError):
                    await self._conn.close()
                self._conn = await asyncpg.connect(
                    host=self._host,
                    user=self._admin_username,
                    password=self._admin_password,
                    database=database,
                    port=5432,
                )
                logger.debug(f"Recreated connection to {self._host}/{database}")

        return self._conn

    async def close(self) -> None:
        """Close the managed connection and clear state."""
        if self._conn:
            try:
                await self._conn.close()
                logger.debug("Closed managed connection")
            except Exception as e:
                logger.warning(f"Error closing connection: {e}")
            finally:
                self._conn = None
                self._current_database = None

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

    async def test_connection(self, database: str = "postgres") -> bool:
        """Test database connection with the bound credentials.

        Args:
            database: Database name to connect to (defaults to 'postgres')

        Returns:
            True if connection successful, False otherwise
        """
        try:
            await self._get_or_create_connection(database)
            logger.info(f"Connection test successful for {self._admin_username}@{self._host}/{database}")
            return True
        except Exception as e:
            logger.debug(f"Connection test failed for {self._admin_username}@{self._host}/{database}: {e}")
            return False

    # User Management Operations

    async def create_user(
        self,
        username: str,
        password: str,
        database_privileges: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new database user using the bound admin credentials.

        Args:
            username: New username to create
            password: Password for new user
            database_privileges: List of database privileges to grant (e.g., ['CREATEDB', 'CREATEROLE'])

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

            # Get connection to postgres database
            conn = await self._get_or_create_connection("postgres")

            # Check if user already exists
            user_exists = await conn.fetchval("SELECT 1 FROM pg_user WHERE usename = $1", validated_username)

            if user_exists:
                logger.warning(f"User {validated_username} already exists on {self._host}")
                return {"status": "exists", "message": f"User {validated_username} already exists"}

            # Create user with password using safe identifier quoting
            quoted_username = self._quote_identifier(validated_username)
            # DDL statements like CREATE USER don't support parameters, so we need to escape the password as a literal
            escaped_password = validated_password.replace("'", "''")  # Escape single quotes
            create_sql = f"CREATE USER {quoted_username} WITH PASSWORD '{escaped_password}' CONNECTION LIMIT 20"
            await conn.execute(create_sql)

            # Grant privileges if specified
            if validated_privileges:
                for privilege in validated_privileges:
                    alter_sql = f"ALTER USER {quoted_username} {privilege}"
                    await conn.execute(alter_sql)

            logger.info(f"User {validated_username} created successfully on {self._host}")
            return {"status": "created", "message": f"User {validated_username} created successfully"}

        except PostgresValidationError:
            logger.exception("Validation failed for user creation")
            raise
        except Exception as e:
            logger.exception(f"Failed to create user {username} on {self._host}")
            raise PostgresExecutionError(f"User creation failed: {e}") from e

    async def delete_user(self, username: str) -> dict[str, Any]:
        """Delete a database user using the bound admin credentials.

        Args:
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

            # Get connection to postgres database
            conn = await self._get_or_create_connection("postgres")

            # Check if user exists
            user_exists = await conn.fetchval("SELECT 1 FROM pg_user WHERE usename = $1", validated_username)

            if not user_exists:
                logger.warning(f"User {validated_username} does not exist on {self._host}")
                return {"status": "not_found", "message": f"User {validated_username} does not exist"}

            # Drop owned objects first to avoid dependency issues
            quoted_username = self._quote_identifier(validated_username)
            await conn.execute(f"DROP OWNED BY {quoted_username} CASCADE")

            # Drop user
            await conn.execute(f"DROP USER {quoted_username}")

            logger.info(f"User {validated_username} deleted successfully from {self._host}")
            return {"status": "deleted", "message": f"User {validated_username} deleted successfully"}

        except PostgresValidationError:
            logger.exception("Validation failed for user deletion")
            raise
        except Exception as e:
            logger.exception(f"Failed to delete user {username} on {self._host}")
            raise PostgresExecutionError(f"User deletion failed: {e}") from e

    async def list_users(self) -> list[dict[str, Any]]:
        """List all database users using the bound admin credentials.

        Returns:
            List of user dictionaries with details

        Raises:
            PostgresExecutionError: If listing users fails
        """
        try:
            # Get connection to postgres database
            conn = await self._get_or_create_connection("postgres")

            users = await conn.fetch("""
                SELECT usename as username,
                       usesuper as is_superuser,
                       usecreatedb as can_create_db,
                       usecreaterole as can_create_role
                FROM pg_user
                ORDER BY usename
            """)

            user_list = [dict(user) for user in users]
            logger.debug(f"Retrieved {len(user_list)} users from {self._host}")
            return user_list

        except Exception as e:
            logger.exception(f"Failed to list users on {self._host}")
            raise PostgresExecutionError(f"User listing failed: {e}") from e

    async def update_user_password(self, username: str, new_password: str) -> dict[str, Any]:
        """Update user password using the bound admin credentials.

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

            # Get connection to postgres database
            conn = await self._get_or_create_connection("postgres")

            user_exists = await conn.fetchval("SELECT 1 FROM pg_user WHERE usename = $1", validated_username)

            if not user_exists:
                logger.warning(f"User {validated_username} does not exist on {self._host}")
                return {"status": "not_found", "message": f"User {validated_username} does not exist"}

            quoted_username = self._quote_identifier(validated_username)
            escaped_password = validated_password.replace("'", "''")
            await conn.execute(f"ALTER USER {quoted_username} WITH PASSWORD '{escaped_password}'")

            logger.info(f"Password updated for user {validated_username} on {self._host}")
            return {"status": "success", "message": f"Password updated for user {validated_username}"}

        except PostgresValidationError:
            logger.exception("Validation failed for password update")
            raise
        except Exception as e:
            logger.exception(f"Failed to update password for user {username} on {self._host}")
            raise PostgresExecutionError(f"Password update failed: {e}") from e

    async def update_user_privileges(self, username: str, database_privileges: list[str]) -> dict[str, Any]:
        """Update user privileges using the bound admin credentials.

        This method ensures the user has the specified privileges. It applies
        each privilege via ALTER USER, making it idempotent (safe to call
        multiple times with the same privileges).

        Args:
            username: Username to update
            database_privileges: List of privileges to apply (e.g., ["SUPERUSER", "CREATEDB"])

        Returns:
            Dictionary with operation status and details

        Raises:
            PostgresExecutionError: If privilege update fails
            PostgresValidationError: If input validation fails
        """
        try:
            # Validate inputs
            validated_username = self._validate_identifier(username, "username")
            validated_privileges = [self._validate_privilege(priv) for priv in database_privileges]

            if not validated_privileges:
                return {"status": "success", "message": "No privileges to apply"}

            # Get connection to postgres database
            conn = await self._get_or_create_connection("postgres")

            user_exists = await conn.fetchval("SELECT 1 FROM pg_user WHERE usename = $1", validated_username)

            if not user_exists:
                logger.warning(f"User {validated_username} does not exist on {self._host}")
                return {"status": "not_found", "message": f"User {validated_username} does not exist"}

            quoted_username = self._quote_identifier(validated_username)

            # Apply each privilege
            for privilege in validated_privileges:
                alter_sql = f"ALTER USER {quoted_username} {privilege}"
                await conn.execute(alter_sql)

            logger.info(f"Privileges {validated_privileges} applied to user {validated_username} on {self._host}")
            return {
                "status": "success",
                "message": f"Privileges updated for user {validated_username}",
                "privileges": validated_privileges,
            }

        except PostgresValidationError:
            logger.exception("Validation failed for privilege update")
            raise
        except Exception as e:
            logger.exception(f"Failed to update privileges for user {username} on {self._host}")
            raise PostgresExecutionError(f"Privilege update failed: {e}") from e

    # Database Management Operations

    async def create_database(self, database_name: str, owner: str | None = None) -> dict[str, Any]:
        """Create a new database using the bound admin credentials.

        Args:
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

            # Get connection to postgres database
            conn = await self._get_or_create_connection("postgres")

            # Check if database already exists
            database_exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", validated_database_name
            )

            if database_exists:
                logger.warning(f"Database {validated_database_name} already exists on {self._host}")
                return {"status": "exists", "message": f"Database {validated_database_name} already exists"}

            # Create database with safe identifier quoting
            quoted_database = self._quote_identifier(validated_database_name)
            if validated_owner:
                quoted_owner = self._quote_identifier(validated_owner)
                create_sql = f"CREATE DATABASE {quoted_database} OWNER {quoted_owner}"
            else:
                create_sql = f"CREATE DATABASE {quoted_database}"

            await conn.execute(create_sql)
            logger.info(f"Database {validated_database_name} created successfully on {self._host}")
            return {"status": "created", "message": f"Database {validated_database_name} created successfully"}

        except PostgresValidationError:
            logger.exception("Validation failed for database creation")
            raise
        except Exception as e:
            logger.exception(f"Failed to create database {database_name} on {self._host}")
            raise PostgresExecutionError(f"Database creation failed: {e}") from e

    async def execute_init_sql(self, database_name: str, sql_statements: list[str]) -> dict[str, Any]:
        """Execute initialization SQL statements on a specific database.

        This is typically used to create extensions or run other setup SQL
        after a database has been created.

        Args:
            database_name: Name of database to connect to
            sql_statements: List of SQL statements to execute

        Returns:
            Dictionary with operation status and details

        Raises:
            PostgresExecutionError: If SQL execution fails
            PostgresValidationError: If input validation fails
        """
        if not sql_statements:
            return {"status": "skipped", "message": "No SQL statements to execute"}

        try:
            validated_database_name = self._validate_identifier(database_name, "database name")

            # Connect to the target database (not postgres)
            conn = await self._get_or_create_connection(validated_database_name)

            executed = []
            for sql in sql_statements:
                try:
                    await conn.execute(sql)
                    executed.append(sql)
                    logger.debug(f"Executed init SQL on {validated_database_name}: {sql[:100]}...")
                except Exception as e:
                    logger.error(f"Failed to execute init SQL on {validated_database_name}: {sql[:100]}... - {e}")
                    raise PostgresExecutionError(f"Init SQL failed on {validated_database_name}: {e}") from e

            logger.info(f"Executed {len(executed)} init SQL statement(s) on database {validated_database_name}")
            return {
                "status": "success",
                "message": f"Executed {len(executed)} SQL statement(s)",
                "executed_count": len(executed),
            }

        except PostgresValidationError:
            logger.exception("Validation failed for init SQL execution")
            raise
        except PostgresExecutionError:
            raise
        except Exception as e:
            logger.exception(f"Failed to execute init SQL on {database_name}")
            raise PostgresExecutionError(f"Init SQL execution failed: {e}") from e

    async def delete_database(self, database_name: str) -> dict[str, Any]:
        """Delete a database using the bound admin credentials.

        Args:
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

            # Get connection to postgres database
            conn = await self._get_or_create_connection("postgres")

            # Check if database exists
            database_exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", validated_database_name
            )

            if not database_exists:
                logger.warning(f"Database {validated_database_name} does not exist on {self._host}")
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

            logger.info(f"Database {validated_database_name} deleted successfully from {self._host}")
            return {"status": "deleted", "message": f"Database {validated_database_name} deleted successfully"}

        except PostgresValidationError:
            logger.exception("Validation failed for database deletion")
            raise
        except Exception as e:
            logger.exception(f"Failed to delete database {database_name} on {self._host}")
            raise PostgresExecutionError(f"Database deletion failed: {e}") from e

    async def list_databases(self) -> list[dict[str, Any]]:
        """List all databases using the bound admin credentials.

        Returns:
            List of database dictionaries with details

        Raises:
            PostgresExecutionError: If listing databases fails
        """
        try:
            # Get connection to postgres database
            conn = await self._get_or_create_connection("postgres")

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
            logger.debug(f"Retrieved {len(database_list)} databases from {self._host}")
            return database_list

        except Exception as e:
            logger.exception(f"Failed to list databases on {self._host}")
            raise PostgresExecutionError(f"Database listing failed: {e}") from e

    # Schema Management Operations

    async def create_schema(self, schema_name: str, database: str, owner: str) -> dict[str, Any]:
        """Create a new database schema using the bound admin credentials.

        Args:
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
            validated_database = self._validate_identifier(database, "database")
            validated_owner = self._validate_identifier(owner, "owner username")

            # Get connection to the specific database
            conn = await self._get_or_create_connection(validated_database)

            # Check if schema already exists in the target database
            schema_exists = await conn.fetchval(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = $1", validated_schema_name
            )

            if schema_exists:
                logger.warning(f"Schema {validated_schema_name} already exists in database {validated_database}")
                return {"status": "exists", "message": f"Schema {validated_schema_name} already exists"}

            # Create schema with safe identifier quoting
            quoted_schema = self._quote_identifier(validated_schema_name)
            quoted_owner = self._quote_identifier(validated_owner)
            create_sql = f"CREATE SCHEMA {quoted_schema} AUTHORIZATION {quoted_owner}"

            await conn.execute(create_sql)
            logger.info(f"Schema {validated_schema_name} created successfully in database {validated_database}")
            return {"status": "created", "message": f"Schema {validated_schema_name} created successfully"}

        except PostgresValidationError:
            logger.exception("Validation failed for schema creation")
            raise
        except Exception as e:
            logger.exception(f"Failed to create schema {schema_name} in database {database}")
            raise PostgresExecutionError(f"Schema creation failed: {e}") from e

    async def set_role_search_path(self, username: str, database: str, schema: str) -> None:
        """Set the default search_path for a role scoped to a specific database.

        This ensures applications that don't explicitly set a search_path
        (e.g. via connection options) still resolve to the correct schema.

        Executes: ALTER ROLE <user> IN DATABASE <db> SET search_path TO <schema>

        Args:
            username: Role name to configure
            database: Database to scope the setting to
            schema: Schema to set as default search_path

        Raises:
            PostgresExecutionError: If the ALTER ROLE fails
            PostgresValidationError: If input validation fails
        """
        validated_username = self._validate_identifier(username, "username")
        validated_database = self._validate_identifier(database, "database")
        validated_schema = self._validate_identifier(schema, "schema")

        quoted_username = self._quote_identifier(validated_username)
        quoted_database = self._quote_identifier(validated_database)
        quoted_schema = self._quote_identifier(validated_schema)

        conn = await self._get_or_create_connection(validated_database)
        await conn.execute(
            f"ALTER ROLE {quoted_username} IN DATABASE {quoted_database} SET search_path TO {quoted_schema}"
        )
        logger.info(
            f"Set default search_path for role {validated_username} "
            f"in database {validated_database} to {validated_schema}"
        )

    async def delete_schema(self, schema_name: str, database: str, cascade: bool = False) -> dict[str, Any]:
        """Delete a database schema using the bound admin credentials.

        Args:
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

            # Get connection to the specific database
            conn = await self._get_or_create_connection(validated_database)

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
            logger.info(f"Schema {validated_schema_name} deleted successfully")
            return {"status": "deleted", "message": f"Schema {validated_schema_name} deleted successfully"}

        except PostgresValidationError:
            logger.exception("Validation failed for schema deletion")
            raise
        except Exception as e:
            logger.exception(f"Failed to delete schema {schema_name}")
            raise PostgresExecutionError(f"Schema deletion failed: {e}") from e

    async def list_schemas(self, database: str) -> list[dict[str, Any]]:
        """List all database schemas using the bound admin credentials.

        Args:
            database: Database name to list schemas from

        Returns:
            List of schema dictionaries with details

        Raises:
            PostgresExecutionError: If listing schemas fails
        """
        try:
            # Get connection to the specified database
            conn = await self._get_or_create_connection(database)

            schemas = await conn.fetch("""
                SELECT schema_name, schema_owner
                FROM information_schema.schemata
                WHERE schema_name NOT IN ('information_schema', 'pg_catalog', 'pg_toast')
                ORDER BY schema_name
            """)

            schema_list = [dict(schema) for schema in schemas]
            logger.debug(f"Retrieved {len(schema_list)} schemas from {database} on {self._host}")
            return schema_list

        except Exception as e:
            logger.exception(f"Failed to list schemas in database {database} on {self._host}")
            raise PostgresExecutionError(f"Schema listing failed: {e}") from e

    async def grant_schema_permissions(
        self, database: str, schema_name: str, username: str, permissions: list[str] | None = None
    ) -> dict[str, Any]:
        """Grant permissions on a schema to a user using the bound admin credentials.

        Args:
            database: Database name where schema exists
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

            # Get connection to the specified database
            conn = await self._get_or_create_connection(database)

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
                f"to user {validated_username} in database {database} on host {self._host}"
            )
            return {"status": "granted", "message": "Permissions granted successfully"}

        except PostgresValidationError:
            logger.exception("Validation failed for permission granting")
            raise
        except Exception as e:
            logger.exception(
                f"Failed to grant permissions on schema {schema_name} to user {username} in database {database} on host {self._host}"
            )
            raise PostgresExecutionError(f"Permission granting failed: {e}") from e

    async def _get_schema_extensions(
        self,
        database: str,
        schema: str,
        *,
        host: str | None = None,
        port: int = 5432,
        username: str | None = None,
        password: str | None = None,
    ) -> list[str]:
        """Query extensions installed in a specific schema of a database.

        When host/username/password are provided, creates a temporary connection
        to the specified server (for external sources). Otherwise uses the bound
        server connection.

        Args:
            database: Database name to query
            schema: Schema name to check for extensions
            host: Optional external host (defaults to bound server)
            port: Database port (default 5432)
            username: Optional external username (defaults to bound admin)
            password: Optional external password (defaults to bound admin)

        Returns:
            List of extension names installed in the schema
        """
        query = (
            "SELECT e.extname FROM pg_extension e JOIN pg_namespace n ON e.extnamespace = n.oid WHERE n.nspname = $1"
        )

        if host is not None:
            # External source: create temporary connection
            conn = await asyncpg.connect(
                host=host,
                port=port,
                user=username or self._admin_username,
                password=password or self._admin_password,
                database=database,
            )
            try:
                rows = await conn.fetch(query, schema)
            finally:
                await conn.close()
        else:
            conn = await self._get_or_create_connection(database)
            rows = await conn.fetch(query, schema)

        return [row["extname"] for row in rows]

    async def _precreate_extensions_for_clone(
        self,
        source_database: str,
        source_schema: str,
        target_database: str,
        target_schema: str,
        *,
        source_host: str | None = None,
        source_port: int = 5432,
        source_username: str | None = None,
        source_password: str | None = None,
    ) -> list[str]:
        """Pre-create extensions in the target schema before pg_dump clone.

        pg_dump -n does not include CREATE EXTENSION statements. If the source schema
        has extensions installed, the dumped DDL will reference extension functions
        (e.g. uuid_generate_v4()) that don't exist in the target, causing the restore
        to fail. This method queries the source for extensions and installs them in
        the target schema before the pg_dump pipeline runs.

        For same-server clones, leave source_host as None (uses bound server).
        For external-source clones, provide explicit source connection parameters.

        The target is always on the bound server (self._host).

        Args:
            source_database: Source database to query for extensions
            source_schema: Source schema containing the extensions
            target_database: Target database where extensions should be installed
            target_schema: Target schema name for the extensions (may differ from source)
            source_host: Optional external source host (defaults to bound server)
            source_port: Source database port (default 5432)
            source_username: Optional external source username
            source_password: Optional external source password

        Returns:
            List of extension names that were pre-created
        """
        extensions = await self._get_schema_extensions(
            source_database,
            source_schema,
            host=source_host,
            port=source_port,
            username=source_username,
            password=source_password,
        )
        if not extensions:
            logger.debug(f"No extensions found in {source_database}.{source_schema}")
            return []

        logger.info(
            f"Found {len(extensions)} extension(s) in {source_database}.{source_schema}: "
            f"{', '.join(extensions)}. Pre-creating in {target_database}.{target_schema}"
        )

        conn = await self._get_or_create_connection(target_database)
        quoted_schema = self._quote_identifier(target_schema)

        # Check if schema already exists - if it does, something is wrong (e.g. leftover
        # from a previous failed clone). This is the same safety check as in
        # _execute_pgdump_clone, but we must do it here before we create the schema.
        schema_exists = await conn.fetchval(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = $1",
            target_schema,
        )
        if schema_exists:
            raise PostgresExecutionError(
                f"Target schema '{target_schema}' already exists in database '{target_database}'. "
                f"This may be a leftover from a previous failed clone. "
                f"Drop the schema manually or delete the database to proceed: "
                f"DROP SCHEMA IF EXISTS {target_schema} CASCADE;"
            )

        await conn.execute(f"CREATE SCHEMA {quoted_schema}")

        for ext_name in extensions:
            # Extension names can contain hyphens (e.g. uuid-ossp) so we cannot use
            # _validate_identifier which rejects non-alphanumeric characters.
            # Quoting the name handles special characters safely.
            quoted_ext = self._quote_identifier(ext_name)
            await conn.execute(f"CREATE EXTENSION IF NOT EXISTS {quoted_ext} SCHEMA {quoted_schema}")
            logger.info(f"Pre-created extension {ext_name} in {target_database}.{target_schema}")

        return extensions

    async def clone_schema(
        self,
        source_database: str,
        target_database: str,
        source_schema: str,
        target_schema: str,
        target_owner: str,
        target_owner_password: str,
    ) -> dict[str, Any]:
        """Clone a schema from source database to target database on the bound server.

        This method clones within the SAME PostgreSQL server (the one this connector is bound to).
        It uses pg_dump to stream schema data, avoiding the PostgreSQL template limitation.
        The approach is production-safe and works with active databases.

        Args:
            source_database: Source database name (on bound server)
            target_database: Target database name (on bound server)
            source_schema: Source schema name
            target_schema: Target schema name
            target_owner: Owner for the cloned schema
            target_owner_password: Password for target owner (needed for pg_dump)

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

            conn = await self._get_or_create_connection("postgres")

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

            # Pre-create extensions from source schema in target before pg_dump
            # pg_dump -n does not include CREATE EXTENSION, so extension functions
            # (e.g. uuid_generate_v4()) would be missing in the target
            precreated_extensions = await self._precreate_extensions_for_clone(
                source_database=validated_source_db,
                source_schema=validated_source_schema,
                target_database=validated_target_db,
                target_schema=validated_source_schema,
            )

            # Clone within same server - use bound credentials for both source and target
            await self._execute_pgdump_clone(
                source_host=self._host,
                source_port=5432,
                source_username=self._admin_username,
                source_password=self._admin_password,
                source_database=validated_source_db,
                source_schema=validated_source_schema,
                target_host=self._host,
                target_port=5432,
                target_username=self._admin_username,
                target_password=self._admin_password,
                target_database=validated_target_db,
                target_schema=validated_target_schema,
                target_owner=validated_target_owner,
                target_owner_password=target_owner_password,
                schema_precreated=len(precreated_extensions) > 0,
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

    async def clone_schema_from_external(
        self,
        source_host: str,
        source_port: int,
        source_username: str,
        source_password: str,
        source_database: str,
        source_schema: str,
        target_database: str,
        target_schema: str,
        target_owner: str,
        target_owner_password: str,
        force_clone: bool = False,
    ) -> dict[str, Any]:
        """Clone a schema from an external database server to this connector's bound server.

        This method enables cloning databases from a DIFFERENT PostgreSQL server (external source)
        to the server this connector is bound to (target). Useful for:
        - Migrating data from external production to local staging
        - Cloning from databases exposed via port-forward, NodePort or LoadBalancer
        - Cross-cluster data synchronization

        The target server is the one this connector is bound to (self._host).

        Args:
            source_host: External source database host (IP or domain)
            source_port: External source database port (e.g., 15432 for port-forward, 5432 direct)
            source_username: Username for external source connection
            source_password: Password for external source connection
            source_database: Source database name on external cluster
            source_schema: Source schema name to clone
            target_database: Target database name on THIS connector's bound server
            target_schema: Target schema name on THIS connector's bound server
            target_owner: Owner for the cloned schema on target
            target_owner_password: Password for target owner
            force_clone: If True, drop existing target schema (default: False)

        Returns:
            Dictionary with operation status and details

        Raises:
            PostgresExecutionError: If schema cloning fails
            PostgresValidationError: If input validation fails

        Example:
            >>> # Connector is bound to local server
            >>> connector = PostgresConnector("localhost", "postgres", "local_admin_pass")
            >>>
            >>> # Clone FROM external TO bound server (localhost)
            >>> result = await connector.clone_schema_from_external(
            ...     source_host="external-db.example.com",  # External source
            ...     source_port=5432,
            ...     source_username="readonly_user",
            ...     source_password="external_password",
            ...     source_database="production_db",
            ...     source_schema="production_schema",
            ...     target_database="staging_db",  # On bound server (localhost)
            ...     target_schema="staging_schema",
            ...     target_owner="staging_owner",
            ...     target_owner_password="owner_password"
            ... )
        """
        try:
            # Validate all identifiers
            validated_source_db = self._validate_identifier(source_database, "source_database")
            validated_source_schema = self._validate_identifier(source_schema, "source_schema")
            validated_target_db = self._validate_identifier(target_database, "target_database")
            validated_target_schema = self._validate_identifier(target_schema, "target_schema")
            validated_target_owner = self._validate_identifier(target_owner, "target_owner")

            # Validate passwords
            self._validate_password(source_password)
            self._validate_password(target_owner_password)

            logger.info(
                f"Starting external clone from {source_host}:{source_port}/{validated_source_db}.{validated_source_schema} "
                f"to {self._host}:5432/{validated_target_db}.{validated_target_schema}"
            )

            conn = await self._get_or_create_connection("postgres")

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

            # Pre-create extensions from source schema in target before pg_dump
            # pg_dump -n does not include CREATE EXTENSION, so extension functions
            # (e.g. uuid_generate_v4()) would be missing in the target
            precreated_extensions = await self._precreate_extensions_for_clone(
                source_database=validated_source_db,
                source_schema=validated_source_schema,
                target_database=validated_target_db,
                target_schema=validated_source_schema,
                source_host=source_host,
                source_port=source_port,
                source_username=source_username,
                source_password=source_password,
            )

            # Execute cross-cluster clone using shared implementation
            # Source = external server, Target = bound server (self._host)
            await self._execute_pgdump_clone(
                source_host=source_host,
                source_port=source_port,
                source_username=source_username,
                source_password=source_password,
                source_database=validated_source_db,
                source_schema=validated_source_schema,
                target_host=self._host,
                target_port=5432,
                target_username=self._admin_username,
                target_password=self._admin_password,
                target_database=validated_target_db,
                target_schema=validated_target_schema,
                target_owner=validated_target_owner,
                target_owner_password=target_owner_password,
                force_clone=force_clone,
                schema_precreated=len(precreated_extensions) > 0,
            )

            logger.info(
                f"Successfully cloned schema from external source {source_host}:{validated_source_db}.{validated_source_schema} "
                f"to {self._host}:{validated_target_db}.{validated_target_schema}"
            )

            return {
                "status": "success",
                "message": f"External database cloned successfully from {source_host}:{validated_source_db} to {self._host}:{validated_target_db}",
                "source_host": source_host,
                "source_port": source_port,
                "source_database": validated_source_db,
                "source_schema": validated_source_schema,
                "target_host": self._host,
                "target_port": 5432,
                "target_database": validated_target_db,
                "target_schema": validated_target_schema,
            }

        except PostgresValidationError:
            logger.exception("Validation failed for external schema cloning")
            raise
        except Exception as e:
            logger.exception(
                f"Failed to clone schema from external source {source_host}:{source_database} to {target_database}"
            )
            raise PostgresExecutionError(f"External schema cloning failed: {e}") from e

    async def _execute_pgdump_clone(
        self,
        source_host: str,
        source_port: int,
        source_username: str,
        source_password: str,
        source_database: str,
        source_schema: str,
        target_host: str,
        target_port: int,
        target_username: str,
        target_password: str,
        target_database: str,
        target_schema: str,
        target_owner: str,
        target_owner_password: str,
        force_clone: bool = False,
        schema_precreated: bool = False,
    ) -> None:
        """Execute pg_dump clone from source to target with explicit connection parameters.

        This is the shared implementation used by both same-cluster and cross-cluster cloning.
        It uses pg_dump | psql streaming to copy schema data without requiring exclusive access.

        Args:
            source_host: Source database host
            source_port: Source database port
            source_username: Username for source database connection
            source_password: Password for source database connection
            source_database: Source database name
            source_schema: Source schema name
            target_host: Target database host
            target_port: Target database port
            target_username: Username for target database admin connection
            target_password: Password for target database admin connection
            target_database: Target database name
            target_schema: Target schema name
            target_owner: Owner for the cloned schema
            target_owner_password: Password for target owner
            force_clone: If True, allows dropping existing target schema (default: False)
            schema_precreated: If True, source schema was pre-created in target for extensions
                and the source schema existence check is skipped (default: False)

        Raises:
            Exception: If clone operation fails
        """
        import asyncio
        import os

        logger.info(
            f"Starting pg_dump streaming clone from {source_host}:{source_database}.{source_schema} "
            f"to {target_host}:{target_database}.{target_schema}"
        )

        try:
            # Set environment for target connection (used in multiple steps)
            env = os.environ.copy()
            env.update(
                {
                    "PGHOST": target_host,
                    "PGPORT": str(target_port),
                    "PGUSER": target_username,
                    "PGPASSWORD": target_password,
                }
            )

            # Step 1: Check if source schema exists in target database (safety check)
            # When schema_precreated=True, the schema was already verified and created by
            # _precreate_extensions_for_clone() - skip the check to avoid duplication.
            if schema_precreated:
                logger.info(
                    f"Source schema '{source_schema}' was pre-created in target database "
                    f"'{target_database}' for extensions - skipping existence check"
                )

                # The schema was created by admin via _precreate_extensions_for_clone().
                # Transfer ownership to target_owner so the pg_dump pipeline (which runs
                # as target_owner) has permission to create objects in the schema.
                grant_cmd = [
                    "psql",
                    "-d",
                    target_database,
                    "-c",
                    f"ALTER SCHEMA {source_schema} OWNER TO {target_owner};",
                ]
                grant_process = await asyncio.create_subprocess_exec(
                    *grant_cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                _, grant_err = await grant_process.communicate()
                if grant_process.returncode != 0:
                    raise Exception(f"Failed to transfer schema ownership to {target_owner}: {grant_err.decode()}")
                logger.info(f"Transferred ownership of pre-created schema '{source_schema}' to {target_owner}")
            else:
                check_source_schema_cmd = [
                    "psql",
                    "-d",
                    target_database,
                    "-t",
                    "-c",
                    f"SELECT 1 FROM information_schema.schemata WHERE schema_name = '{source_schema}';",
                ]
                check_process = await asyncio.create_subprocess_exec(
                    *check_source_schema_cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                check_out, _ = await check_process.communicate()
                source_schema_exists_in_target = bool(check_out and check_out.decode().strip())

                if source_schema_exists_in_target:
                    if source_schema == "public":
                        # Safe to drop 'public' schema - it's auto-created and we're about to recreate it
                        logger.info("Source schema 'public' exists in target database, dropping it (safe to recreate)")
                        drop_public_cmd = ["psql", "-d", target_database, "-c", "DROP SCHEMA IF EXISTS public CASCADE;"]
                        drop_public_process = await asyncio.create_subprocess_exec(
                            *drop_public_cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                        )
                        await drop_public_process.communicate()
                    else:
                        # Schema exists but was NOT pre-created - likely a leftover from a
                        # previous failed clone. This is a legitimate safety concern.
                        raise Exception(
                            f"Source schema '{source_schema}' already exists in target database '{target_database}'. "
                            f"Manual intervention required to prevent accidental data loss. "
                            f"Please manually drop the schema if you want to proceed: "
                            f"DROP SCHEMA IF EXISTS {source_schema} CASCADE;"
                        )

            # Step 3: Drop target schema if it exists, schemas are different, AND force_clone is True
            if source_schema != target_schema:
                if force_clone:
                    logger.info(f"force_clone=True: Preparing target schema {target_schema} (dropping if exists)")
                    drop_cmd = ["psql", "-d", target_database, "-c", f"DROP SCHEMA IF EXISTS {target_schema} CASCADE;"]
                    drop_process = await asyncio.create_subprocess_exec(
                        *drop_cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                    )
                    await drop_process.communicate()
                else:
                    # Check if target schema exists
                    check_target_cmd = [
                        "psql",
                        "-d",
                        target_database,
                        "-t",
                        "-c",
                        f"SELECT 1 FROM information_schema.schemata WHERE schema_name = '{target_schema}';",
                    ]
                    check_target_process = await asyncio.create_subprocess_exec(
                        *check_target_cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                    )
                    check_target_out, _ = await check_target_process.communicate()
                    target_schema_exists = bool(check_target_out and check_target_out.decode().strip())

                    if target_schema_exists:
                        raise Exception(
                            f"Target schema '{target_schema}' already exists in database '{target_database}' and force_clone=False. "
                            f"Set force_clone=True to overwrite existing schema."
                        )

            # Step 4: Stream complete schema (structure + data) in one step
            logger.info("Streaming complete schema using pg_dump pipeline")

            # Connection parameters are passed via the environment, never via the
            # command line, so attacker-controlled source host/username/password
            # values cannot be interpreted by a shell. pg_dump and psql are spawned
            # directly (no shell) and connected with an explicit pipe; the
            # CREATE SCHEMA -> CREATE SCHEMA IF NOT EXISTS rewrite (previously a
            # `sed` stage) is done in Python. The rewrite is needed because
            # extensions are pre-created in the target schema before pg_dump runs
            # (pg_dump -n omits CREATE EXTENSION), so without it the duplicate
            # CREATE SCHEMA would fail under ON_ERROR_STOP=1.
            source_env = os.environ.copy()
            source_env.update(
                {
                    "PGHOST": source_host,
                    "PGPORT": str(source_port),
                    "PGUSER": source_username,
                    "PGPASSWORD": source_password,
                }
            )
            target_env = os.environ.copy()
            target_env.update(
                {
                    "PGHOST": target_host,
                    "PGPORT": str(target_port),
                    "PGUSER": target_owner,
                    "PGPASSWORD": target_owner_password,
                }
            )

            from opi.core.metrics import track_subprocess_memory

            async with track_subprocess_memory("postgres-clone"):
                dump_process = await asyncio.create_subprocess_exec(
                    "pg_dump",
                    "-d",
                    source_database,
                    "-n",
                    source_schema,
                    "--no-owner",
                    "--no-privileges",
                    env=source_env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                restore_process = await asyncio.create_subprocess_exec(
                    "psql",
                    "-d",
                    target_database,
                    "-v",
                    "ON_ERROR_STOP=1",
                    env=target_env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                dump_stdout = dump_process.stdout
                dump_stderr = dump_process.stderr
                restore_stdin = restore_process.stdin
                if dump_stdout is None or dump_stderr is None or restore_stdin is None:
                    raise Exception("Failed to open pg_dump/psql pipes for streaming clone")

                async def _pump_dump_to_restore() -> None:
                    # Stream pg_dump -> psql line by line so the full dump is
                    # never held in memory. The line-anchored rewrite replaces
                    # the sed stage; extensions are pre-created in the target
                    # schema so CREATE SCHEMA must become CREATE SCHEMA IF NOT
                    # EXISTS to survive ON_ERROR_STOP=1. pg_dump emits each
                    # statement on its own line so readline is bounded.
                    create_schema = b"CREATE SCHEMA "
                    try:
                        while True:
                            line = await dump_stdout.readline()
                            if not line:
                                break
                            if line.startswith(create_schema):
                                line = b"CREATE SCHEMA IF NOT EXISTS " + line[len(create_schema) :]
                            restore_stdin.write(line)
                            await restore_stdin.drain()
                    finally:
                        restore_stdin.close()

                # Read pg_dump stdout (via the pump) and stderr concurrently;
                # do not use communicate() on the dump process because the
                # pump owns its stdout.
                pump_task = asyncio.create_task(_pump_dump_to_restore())
                dump_err_task = asyncio.create_task(dump_stderr.read())
                clone_out, clone_err = await restore_process.communicate()
                await pump_task
                dump_err = await dump_err_task
                await dump_process.wait()

            # Log only errors or important warnings
            combined_err = (dump_err or b"") + (clone_err or b"")
            if combined_err:
                stderr_text = combined_err.decode().strip()
                # Only log if it contains actual errors, not just version info
                if "error:" in stderr_text.lower() and "version mismatch" not in stderr_text.lower():
                    logger.error(f"pg_dump stderr: {stderr_text}")
                elif stderr_text:
                    logger.debug(f"pg_dump stderr: {stderr_text}")

            if dump_process.returncode != 0:
                raise Exception(f"pg_dump failed: {dump_err.decode()}")

            # Check for errors
            if restore_process.returncode != 0:
                raise Exception(f"Schema clone pipeline failed: {clone_err.decode()}")

            # Check if pg_dump actually produced any output
            if not clone_out and not clone_err:
                logger.warning("pg_dump produced no output - this might indicate an empty source schema")
            elif clone_out and len(clone_out.decode().strip()) < 500:
                logger.warning(
                    f"pg_dump produced very little output ({len(clone_out.decode())} bytes) - might be empty dump"
                )
                logger.debug(f"pg_dump output size: {len(clone_out)} bytes")

            logger.info("Schema cloned successfully (structure + data)")

            # Step 5: Check if target schema was created successfully

            check_target_schema_cmd = [
                "psql",
                "-d",
                target_database,
                "-c",
                f"SELECT 1 FROM information_schema.schemata WHERE schema_name = '{target_schema}';",
                "-t",
            ]

            check_process = await asyncio.create_subprocess_exec(
                *check_target_schema_cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            check_out, _ = await check_process.communicate()

            if not (check_out and check_out.decode().strip()):
                # Target schema doesn't exist - this is an error
                logger.error(f"Target schema '{target_schema}' was not created during pg_dump")

                # List what schemas DO exist for debugging
                list_cmd = [
                    "psql",
                    "-d",
                    target_database,
                    "-c",
                    "SELECT schema_name FROM information_schema.schemata "
                    "WHERE schema_name NOT IN ('information_schema', 'pg_catalog', 'pg_toast');",
                    "-t",
                ]
                list_process = await asyncio.create_subprocess_exec(
                    *list_cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                list_out, _ = await list_process.communicate()
                if list_out:
                    existing_schemas = list_out.decode().strip()
                    logger.error(f"Schemas that DO exist in target database: {existing_schemas}")

                    # Check if source schema was created instead
                    if source_schema in existing_schemas and source_schema != target_schema:
                        logger.info(
                            f"Found source schema '{source_schema}' in target database, renaming to '{target_schema}'"
                        )
                        rename_cmd = [
                            "psql",
                            "-d",
                            target_database,
                            "-c",
                            f"ALTER SCHEMA {source_schema} RENAME TO {target_schema};",
                        ]

                        rename_process = await asyncio.create_subprocess_exec(
                            *rename_cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                        )

                        _, rename_err = await rename_process.communicate()

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

            # Step 6: Set proper ownership of the final schema
            logger.info(f"Setting ownership of schema {target_schema} to {target_owner}")
            ownership_cmd = [
                "psql",
                "-d",
                target_database,
                "-c",
                f"ALTER SCHEMA {target_schema} OWNER TO {target_owner};",
            ]

            owner_process = await asyncio.create_subprocess_exec(
                *ownership_cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            _, owner_err = await owner_process.communicate()

            if owner_process.returncode != 0:
                logger.warning(f"Failed to set schema ownership: {owner_err.decode()}")
            else:
                logger.info(f"Successfully set schema ownership to {target_owner}")

        except Exception as e:
            logger.exception(f"pg_dump streaming clone failed: {e}")
            raise Exception(f"Failed to clone schema using pg_dump: {e}") from e


# Factory function for creating connector instances
def create_postgres_connector(host: str, admin_username: str, admin_password: str) -> PostgresConnector:
    """Factory function to create a PostgresConnector instance bound to specific credentials.

    Args:
        host: Database host
        admin_username: Admin username for database operations
        admin_password: Admin password for database operations

    Returns:
        PostgresConnector instance bound to the specified database server
    """
    return PostgresConnector(host, admin_username, admin_password)
