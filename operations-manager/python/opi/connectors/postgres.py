"""Users, databases, schemas and schema clones on a PostgreSQL server.

Every identifier that lands in DDL goes through ``_validate_identifier`` and
``_quote_identifier`` first: these statements are composed as text, so nothing else keeps
SQL injection out.
"""

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass
from typing import Any, ClassVar

import asyncpg

from opi.core.config import settings
from opi.core.subprocess_env import child_env
from opi.utils.patterns import POSTGRES_CLONEABLE_SCHEMA, POSTGRES_IDENTIFIER

logger = logging.getLogger(__name__)

# Tokens without a quote, dollar or other delimiter: a SQL literal is always quoted, so such
# a token cannot be a password.
_SQL_PLAIN_TOKEN = re.compile(r"^[A-Za-z0-9_.]+$")


def _summarize_sql_statement(sql: str) -> str:
    """Secret-free summary of a SQL statement for logging: the statement kind and its target.

    Keeps the first three unquoted tokens, so "CREATE ROLE app_user" for a CREATE ROLE with a
    PASSWORD behind it. The password in such a statement may not reach the log, and the SQL
    comes out of the project file, so any shape can turn up.
    """
    tokens: list[str] = []
    for token in sql.split():
        if not _SQL_PLAIN_TOKEN.match(token):
            break
        tokens.append(token)
        if len(tokens) == 3:
            break
    return " ".join(tokens) if tokens else "unrecognised statement"


@dataclass(frozen=True)
class SchemaInventory:
    """What a database holds, as a clone has to look at it (RC-360).

    ``extension_namespaces`` are the schemas an extension lives in; ``public_objects``
    are the objects in ``public`` that no extension owns.
    """

    schemas: list[str]
    extension_namespaces: set[str]
    public_objects: list[str]


class PostgresExecutionError(Exception):
    """A statement or a clone against PostgreSQL failed."""


class PostgresValidationError(Exception):
    """An identifier, privilege, password, host or port does not meet the rules."""


class PostgresConnector:
    """Connector for interacting with PostgreSQL databases using managed connections.

    This connector is bound to ONE PostgreSQL server with specific admin credentials.
    It maintains one internal connection that is created on first use and reused
    for subsequent operations. The connection is automatically validated and recreated if broken.
    """

    def __init__(self, host: str, admin_username: str, admin_password: str) -> None:
        """``host`` is a service name or FQDN; the credentials are the server's admin."""
        self._host = host
        self._admin_username = admin_username
        self._admin_password = admin_password
        self._conn: asyncpg.Connection | None = None
        self._current_database: str | None = None

    async def _get_or_create_connection(self, database: str = "postgres") -> asyncpg.Connection:
        """The managed connection to ``database``, reused when it already points there.

        Another database closes the old connection and opens a new one; a broken one is
        replaced.
        """
        if self._conn is None or self._current_database != database:
            if self._conn:
                with contextlib.suppress(OSError):
                    await self._conn.close()

            self._conn = await asyncpg.connect(
                host=self._host,
                user=self._admin_username,
                password=self._admin_password,
                database=database,
                port=5432,
                ssl=settings.DATABASE_SSLMODE,
            )
            self._current_database = database
            logger.debug(f"Created connection to {self._host}/{database}")
        else:
            try:
                await self._conn.fetchval("SELECT 1")
            except asyncpg.PostgresError, asyncpg.InterfaceError, OSError:
                logger.warning(f"Connection to {self._host}/{database} broken, recreating")
                with contextlib.suppress(OSError):
                    await self._conn.close()
                self._conn = await asyncpg.connect(
                    host=self._host,
                    user=self._admin_username,
                    password=self._admin_password,
                    database=database,
                    port=5432,
                    ssl=settings.DATABASE_SSLMODE,
                )
                logger.debug(f"Recreated connection to {self._host}/{database}")

        return self._conn

    async def close(self) -> None:
        """Close the managed connection and clear state."""
        if self._conn:
            try:
                await self._conn.close()
                logger.debug("Closed managed connection")
            except (asyncpg.PostgresError, OSError) as e:
                logger.warning(f"Error closing connection: {e}")
            finally:
                self._conn = None
                self._current_database = None

    @staticmethod
    def _validate_identifier(identifier: str, identifier_type: str = "identifier") -> str:
        """Validate a SQL identifier (a username, a schema name).

        ``identifier_type`` only names the thing in the error message.

        Raises:
            PostgresValidationError: empty, over PostgreSQL's 63 characters, not starting
                with a letter or underscore, or a reserved word.
        """
        if not identifier:
            raise PostgresValidationError(f"{identifier_type} cannot be empty")

        if len(identifier) > 63:
            raise PostgresValidationError(f"{identifier_type} cannot exceed 63 characters")

        if not re.match(POSTGRES_IDENTIFIER, identifier):
            raise PostgresValidationError(
                f"{identifier_type} '{identifier}' contains invalid characters. "
                "Must start with letter/underscore and contain only letters, digits, underscores, and dollar signs."
            )

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

    @classmethod
    def validate_cloneable_schema_name(cls, schema_name: str) -> str:
        """Validate app-created schema names in clone operations (RC-360).

        Stricter than _validate_identifier: no uppercase or $ (used in unquoted psql statements).
        """
        if not re.match(POSTGRES_CLONEABLE_SCHEMA, schema_name):
            raise PostgresValidationError(
                f"schema '{schema_name}' is not a name the platform can clone: it accepts lowercase letters, "
                "digits and underscores, starting with a letter or an underscore"
            )
        if len(schema_name) > 63:
            raise PostgresValidationError(f"schema '{schema_name}' cannot exceed 63 characters")
        return cls._validate_identifier(schema_name, "schema")

    @staticmethod
    def _validate_connection_limit(connection_limit: int) -> int:
        """Guard the value that is formatted into DDL: a plain positive int, nothing else."""
        if isinstance(connection_limit, bool) or not isinstance(connection_limit, int) or connection_limit < 1:
            raise PostgresValidationError(f"Invalid connection limit: {connection_limit!r}")
        return connection_limit

    @staticmethod
    def _validate_privilege(privilege: str) -> str:
        """Validate a role privilege against the allowlist and return it in uppercase.

        Raises:
            PostgresValidationError: the privilege is not on the allowlist.
        """
        if not privilege:
            raise PostgresValidationError("Privilege cannot be empty")

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

        # CONNECTION LIMIT carries a number, so it is not a plain allowlist hit.
        if privilege_upper.startswith("CONNECTION LIMIT"):
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
        """Validate a schema privilege against the allowlist and return it in uppercase.

        Raises:
            PostgresValidationError: the privilege is not on the allowlist.
        """
        if not privilege:
            raise PostgresValidationError("Schema privilege cannot be empty")

        valid_schema_privileges = {"USAGE", "CREATE", "ALL", "ALL PRIVILEGES"}

        privilege_upper = privilege.upper().strip()

        if privilege_upper not in valid_schema_privileges:
            valid_privs = ", ".join(sorted(valid_schema_privileges))
            raise PostgresValidationError(f"Invalid schema privilege '{privilege}'. Valid privileges: {valid_privs}")

        return privilege_upper

    @staticmethod
    def _quote_identifier(identifier: str) -> str:
        """Quote a pre-validated identifier for use in DDL."""
        escaped = identifier.replace('"', '""')
        return f'"{escaped}"'

    @staticmethod
    def _validate_password(password: str) -> str:
        """Validate a password against the minimum requirements.

        Raises:
            PostgresValidationError: empty, shorter than 8, longer than 128, or without a digit.
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

    # K8s DNS-1123 label: a lowercase letter or digit, optionally hyphen plus more, at most
    # 63 characters. See _validate_in_cluster_host for what the pattern is for.
    _K8S_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    _IN_CLUSTER_HOST_RE = re.compile(
        rf"^{_K8S_LABEL}"  # service name
        rf"(?:\.{_K8S_LABEL}\.svc\.cluster\.local)?$"  # optional ns + cluster-DNS suffix
    )

    @staticmethod
    def _validate_in_cluster_host(host: str, field_name: str = "host") -> str:
        """Validate that a host points at an in-cluster K8s service.

        Only a bare K8s service name (``rig-db-rw``) or a full cluster-DNS FQDN
        (``svc.namespace.svc.cluster.local``) is accepted; an external domain, an IP literal
        and anything with a shell-special or control character is refused. This is the
        boundary ``_execute_pgdump_clone`` leans on: it keeps the streaming clone from ever
        reaching a host outside the cluster that could receive our data.
        """
        if not host:
            raise PostgresValidationError(f"{field_name} cannot be empty")
        if len(host) > 253:
            raise PostgresValidationError(f"{field_name} '{host}' exceeds 253 characters")
        # ipaddress.ip_address() detects an IPv4 or IPv6 literal; refusing those blocks
        # routing by IP entirely.
        try:
            import ipaddress

            ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            pass
        else:
            raise PostgresValidationError(
                f"{field_name} '{host}' is an IP address; only in-cluster service names allowed"
            )
        if not PostgresConnector._IN_CLUSTER_HOST_RE.match(host):
            raise PostgresValidationError(
                f"{field_name} '{host}' is not an in-cluster K8s service "
                f"(expected bare service name or '<svc>.<ns>.svc.cluster.local')"
            )
        return host

    @staticmethod
    def _validate_pg_password_safe(password: str, field_name: str = "password") -> str:
        """Reject NUL bytes and CR/LF in passwords so they cannot smuggle
        anything past env-var or libpq parsing. Laxer than ``_validate_password``
        (which also requires complexity); used for passwords we receive
        ready-made from upstream and only need to keep safe at the env
        boundary.
        """
        if not password:
            raise PostgresValidationError(f"{field_name} cannot be empty")
        if "\x00" in password or "\r" in password or "\n" in password:
            raise PostgresValidationError(f"{field_name} contains NUL or newline characters")
        return password

    # The TCP ports the clone pipeline may connect to. A new one is added here as a
    # reviewable change rather than passed in as an unbounded int.
    _ALLOWED_PG_PORTS: frozenset[int] = frozenset({5432})

    @classmethod
    def _validate_pg_port(cls, port: int, field_name: str = "port") -> int:
        """Validate a TCP port against the allow-list."""
        if not isinstance(port, int) or isinstance(port, bool):
            raise PostgresValidationError(f"{field_name} must be an int, got {type(port).__name__}")
        if port not in cls._ALLOWED_PG_PORTS:
            allowed = ", ".join(str(p) for p in sorted(cls._ALLOWED_PG_PORTS))
            raise PostgresValidationError(f"{field_name} {port} not in allow-list ({allowed})")
        return port

    async def test_connection(self, database: str = "postgres") -> bool:
        """Whether the bound credentials can connect to ``database``."""
        try:
            await self._get_or_create_connection(database)
            logger.info(f"Connection test successful for {self._admin_username}@{self._host}/{database}")
            return True
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as e:
            logger.debug(f"Connection test failed for {self._admin_username}@{self._host}/{database}: {e}")
            return False

    async def create_user(
        self,
        username: str,
        password: str,
        database_privileges: list[str] | None = None,
        *,
        connection_limit: int,
    ) -> dict[str, Any]:
        """Create a role; status "created" or "exists".

        ``connection_limit`` is the role's CONNECTION LIMIT, whose value the service decides.

        Raises:
            PostgresExecutionError: the CREATE ROLE failed.
            PostgresValidationError: a name, the password or a privilege does not hold up.
        """
        try:
            # Validate inputs
            validated_username = self._validate_identifier(username, "username")
            validated_password = self._validate_password(password)
            validated_limit = self._validate_connection_limit(connection_limit)

            validated_privileges = []
            if database_privileges:
                validated_privileges = [self._validate_privilege(priv) for priv in database_privileges]

            # Get connection to postgres database
            conn = await self._get_or_create_connection("postgres")

            # Check if user already exists
            user_exists = await conn.fetchval("SELECT 1 FROM pg_user WHERE usename = $1", validated_username)

            if user_exists:
                logger.debug(f"User {validated_username} already exists on {self._host}")
                return {"status": "exists", "message": f"User {validated_username} already exists"}

            # Create user with password using safe identifier quoting
            quoted_username = self._quote_identifier(validated_username)
            # DDL statements like CREATE USER don't support parameters, so we need to escape the password as a literal
            escaped_password = validated_password.replace("'", "''")  # Escape single quotes
            create_sql = (
                f"CREATE USER {quoted_username} WITH PASSWORD '{escaped_password}' CONNECTION LIMIT {validated_limit}"
            )
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

    async def set_connection_limit(self, username: str, connection_limit: int) -> dict[str, Any]:
        """Bring a role's CONNECTION LIMIT to ``connection_limit``, altering only on a difference.

        Returns ``{"status": "unchanged" | "updated" | "not_found", "previous": int | None,
        "connection_limit": int}``.

        Raises:
            PostgresExecutionError: reading or altering the role failed.
            PostgresValidationError: the name or the limit does not hold up.
        """
        validated_username = self._validate_identifier(username, "username")
        validated_limit = self._validate_connection_limit(connection_limit)
        try:
            conn = await self._get_or_create_connection("postgres")
            previous = await conn.fetchval("SELECT rolconnlimit FROM pg_roles WHERE rolname = $1", validated_username)
            if previous is None:
                return {"status": "not_found", "previous": None, "connection_limit": validated_limit}
            if previous == validated_limit:
                return {"status": "unchanged", "previous": previous, "connection_limit": validated_limit}
            quoted_username = self._quote_identifier(validated_username)
            await conn.execute(f"ALTER ROLE {quoted_username} CONNECTION LIMIT {validated_limit}")
        except asyncpg.PostgresError as e:
            logger.exception(f"Failed to set connection limit for {validated_username} on {self._host}")
            raise PostgresExecutionError(f"Setting connection limit failed: {e}") from e
        logger.info(
            f"Connection limit of {validated_username} on {self._host} changed from {previous} to {validated_limit}"
        )
        return {"status": "updated", "previous": previous, "connection_limit": validated_limit}

    async def delete_user(self, username: str) -> dict[str, Any]:
        """Delete a role; status "deleted" or "not_found".

        Raises:
            PostgresExecutionError: the DROP ROLE failed.
            PostgresValidationError: the name does not hold up.
        """
        try:
            # Validate inputs
            validated_username = self._validate_identifier(username, "username")

            # Get connection to postgres database
            conn = await self._get_or_create_connection("postgres")

            # Check if user exists
            user_exists = await conn.fetchval("SELECT 1 FROM pg_user WHERE usename = $1", validated_username)

            if not user_exists:
                # Idempotent delete: an already-absent user is the desired end state, not an anomaly.
                logger.debug(f"User {validated_username} does not exist on {self._host}, nothing to delete")
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
        """Every role on this server.

        Raises:
            PostgresExecutionError: the query failed.
        """
        try:
            # Get connection to postgres database
            conn = await self._get_or_create_connection("postgres")

            users = await conn.fetch("""
                SELECT rolname as username,
                       rolsuper as is_superuser,
                       rolcreatedb as can_create_db,
                       rolcreaterole as can_create_role
                FROM pg_roles
                WHERE rolcanlogin
                ORDER BY rolname
            """)

            user_list = [dict(user) for user in users]
            logger.debug(f"Retrieved {len(user_list)} users from {self._host}")
            return user_list

        except Exception as e:
            logger.exception(f"Failed to list users on {self._host}")
            raise PostgresExecutionError(f"User listing failed: {e}") from e

    async def update_user_password(self, username: str, new_password: str) -> dict[str, Any]:
        """Set a role's password; status "updated".

        Raises:
            PostgresExecutionError: the ALTER ROLE failed.
            PostgresValidationError: the name or the password does not hold up.
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
        """Apply ``database_privileges`` to a role, one ALTER USER each, so it is idempotent.

        Raises:
            PostgresExecutionError: an ALTER USER failed.
            PostgresValidationError: the name or a privilege does not hold up.
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

    async def create_database(self, database_name: str, owner: str | None = None) -> dict[str, Any]:
        """Create a database owned by ``owner``; status "created" or "exists".

        Raises:
            PostgresExecutionError: the CREATE DATABASE failed.
            PostgresValidationError: a name does not hold up.
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
                logger.debug(f"Database {validated_database_name} already exists on {self._host}")
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

    async def restrict_database_to_roles(self, database: str, roles: list[str]) -> dict[str, Any]:
        """Close a database off for ``PUBLIC`` and give only ``roles`` CONNECT.

        PostgreSQL grants a new database CONNECT and TEMPORARY to ``PUBLIC``, so without this
        REVOKE every role on the server can connect to every database and read its catalog.
        The owner keeps its rights independently of the GRANT list, because a REVOKE from
        ``PUBLIC`` does not touch the owner.

        Repeatable: REVOKE and GRANT are idempotent in PostgreSQL, so every processing round
        may do this again. The modes are documented at ``DATABASE_RESTRICT_PUBLIC_CONNECT``
        in ``opi/core/config.py``.

        Returns the mode, the statements, and on ``dry-run`` who is connected.

        Raises:
            PostgresExecutionError: the REVOKE or a GRANT failed.
            PostgresValidationError: a name does not hold up.
        """
        try:
            validated_database = self._validate_identifier(database, "database")
            validated_roles = [self._validate_identifier(role, "role") for role in roles]

            mode = settings.DATABASE_RESTRICT_PUBLIC_CONNECT
            if mode == "off":
                logger.debug(f"DATABASE_RESTRICT_PUBLIC_CONNECT=off, {validated_database} blijft open voor PUBLIC")
                return {"status": "disabled", "database": validated_database}

            quoted_database = self._quote_identifier(validated_database)
            statements = [
                f"REVOKE CONNECT, TEMPORARY ON DATABASE {quoted_database} FROM PUBLIC",
                *(
                    f"GRANT CONNECT ON DATABASE {quoted_database} TO {self._quote_identifier(role)}"
                    for role in validated_roles
                ),
            ]

            conn = await self._get_or_create_connection("postgres")

            database_exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", validated_database)
            if not database_exists:
                logger.debug(f"Database {validated_database} does not exist on {self._host}, nothing to restrict")
                return {"status": "not_found", "database": validated_database}

            if mode == "dry-run":
                connected = [
                    row["usename"]
                    for row in await conn.fetch(
                        "SELECT DISTINCT usename FROM pg_stat_activity "
                        "WHERE datname = $1 AND usename IS NOT NULL ORDER BY usename",
                        validated_database,
                    )
                ]
                # The roles NOT on the list are what `on` will shut out, and that is the line
                # an administrator looks for before switching over.
                losing_access = [role for role in connected if role not in validated_roles]
                logger.info(
                    f"dry-run voor database {validated_database} op {self._host}: "
                    f"{len(statements)} statement(s) {statements}, nu verbonden {connected}, "
                    f"zou de toegang verliezen {losing_access}"
                )
                return {
                    "status": "dry-run",
                    "database": validated_database,
                    "statements": statements,
                    "connected_roles": connected,
                }

            for statement in statements:
                await conn.execute(statement)

            logger.info(
                f"Database {validated_database} op {self._host} is afgesloten voor PUBLIC, "
                f"CONNECT voor {validated_roles}"
            )
            return {
                "status": "restricted",
                "database": validated_database,
                "statements": statements,
                "roles": validated_roles,
            }

        except PostgresValidationError:
            logger.exception("Validation failed for database restriction")
            raise
        except Exception as e:
            logger.exception(f"Failed to restrict database {database} on {self._host}")
            raise PostgresExecutionError(f"Database restriction failed: {e}") from e

    #: The catalog queries of :meth:`transfer_public_objects`, per object kind. Each query
    #: builds its own ALTER statement with ``quote_ident`` and PostgreSQL's own ``reg*``
    #: casts: those names come out of the catalog and are already written correctly there,
    #: overload signatures included, which an identifier cannot carry.
    #:
    #: $1 is the new owner, $2 the current one. An object that already belongs to the new
    #: owner yields no statement, so a second round does nothing.
    _OWNER_TRANSFER_QUERIES: ClassVar[dict[str, str]] = {
        "database": """
            SELECT 'ALTER DATABASE ' || quote_ident(d.datname) || ' OWNER TO ' || quote_ident($1)
            FROM pg_database d JOIN pg_roles o ON o.oid = d.datdba
            WHERE d.datname = current_database() AND o.rolname = $2
        """,
        "schema": """
            SELECT 'ALTER SCHEMA ' || quote_ident(n.nspname) || ' OWNER TO ' || quote_ident($1)
            FROM pg_namespace n JOIN pg_roles o ON o.oid = n.nspowner
            WHERE n.nspname = 'public' AND o.rolname = $2
        """,
        # Tables first, and without the sequences and indexes that hang off a table column:
        # PostgreSQL refuses to set such a sequence's owner separately and moves it along
        # with the table.
        "relation": """
            SELECT CASE c.relkind
                       WHEN 'v' THEN 'ALTER VIEW '
                       WHEN 'm' THEN 'ALTER MATERIALIZED VIEW '
                       WHEN 'S' THEN 'ALTER SEQUENCE '
                       ELSE 'ALTER TABLE '
                   END || c.oid::regclass::text || ' OWNER TO ' || quote_ident($1)
            FROM pg_class c JOIN pg_roles o ON o.oid = c.relowner
            WHERE c.relnamespace = 'public'::regnamespace
              AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
              AND o.rolname = $2
              AND NOT EXISTS (
                  SELECT 1 FROM pg_depend d
                  WHERE d.objid = c.oid AND d.classid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')
              )
            ORDER BY c.relkind
        """,
        # Standalone types only: a table's composite type and an element's array type follow
        # their owner by themselves.
        "type": """
            SELECT 'ALTER TYPE ' || t.oid::regtype::text || ' OWNER TO ' || quote_ident($1)
            FROM pg_type t JOIN pg_roles o ON o.oid = t.typowner
            WHERE t.typnamespace = 'public'::regnamespace
              AND t.typrelid = 0
              AND NOT EXISTS (SELECT 1 FROM pg_type e WHERE e.typarray = t.oid)
              AND o.rolname = $2
        """,
        "routine": """
            SELECT CASE p.prokind
                       WHEN 'p' THEN 'ALTER PROCEDURE '
                       WHEN 'a' THEN 'ALTER AGGREGATE '
                       ELSE 'ALTER FUNCTION '
                   END || p.oid::regprocedure::text || ' OWNER TO ' || quote_ident($1)
            FROM pg_proc p JOIN pg_roles o ON o.oid = p.proowner
            WHERE p.pronamespace = 'public'::regnamespace AND o.rolname = $2
        """,
    }

    async def transfer_public_objects(self, database: str, new_owner: str, current_owner: str) -> dict[str, int]:
        """Give ``database`` and everything in its ``public`` schema to ``new_owner``.

        For OPI's own database: the migrations run as the app role and therefore have to own
        every existing table, ``alembic_version`` included. Idempotent, and meant to run at
        every start: only what still belongs to ``current_owner`` moves.

        No ``REASSIGN OWNED BY``: that also touches objects outside this database, and on a
        superuser it is invalid.

        Returns the number of objects moved, per kind.

        Raises:
            PostgresExecutionError: an ALTER failed.
            PostgresValidationError: a name does not hold up.
        """
        try:
            validated_database = self._validate_identifier(database, "database")
            validated_new_owner = self._validate_identifier(new_owner, "new owner")
            validated_current_owner = self._validate_identifier(current_owner, "current owner")

            conn = await self._get_or_create_connection(validated_database)
            moved: dict[str, int] = {}
            for kind, query in self._OWNER_TRANSFER_QUERIES.items():
                statements = [row[0] for row in await conn.fetch(query, validated_new_owner, validated_current_owner)]
                for statement in statements:
                    await conn.execute(statement)
                moved[kind] = len(statements)

            if any(moved.values()):
                logger.info(
                    f"Eigendom in {validated_database} verplaatst van {validated_current_owner} "
                    f"naar {validated_new_owner}: {moved}"
                )
            else:
                logger.info(
                    f"Eigendom in {validated_database} staat al goed; niets van "
                    f"{validated_current_owner} te verplaatsen"
                )
            return moved

        except PostgresValidationError:
            logger.exception("Validation failed for owner transfer")
            raise
        except Exception as e:
            logger.exception(f"Failed to transfer ownership in {database} on {self._host}")
            raise PostgresExecutionError(f"Owner transfer failed: {e}") from e

    async def execute_init_sql(self, database_name: str, sql_statements: list[str]) -> dict[str, Any]:
        """Run the init statements of a freshly created database; status "success" or "skipped".

        Raises:
            PostgresExecutionError: a statement failed.
            PostgresValidationError: the database name does not hold up.
        """
        if not sql_statements:
            return {"status": "skipped", "message": "No SQL statements to execute"}

        try:
            validated_database_name = self._validate_identifier(database_name, "database name")

            conn = await self._get_or_create_connection(validated_database_name)

            executed = []
            for sql in sql_statements:
                try:
                    await conn.execute(sql)
                    executed.append(sql)
                    logger.debug(f"Executed init SQL on {validated_database_name}: {_summarize_sql_statement(sql)}")
                except Exception as e:
                    logger.error(
                        f"Failed to execute init SQL on {validated_database_name}: "
                        f"{_summarize_sql_statement(sql)} - {e}"
                    )
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
        """Delete a database; status "deleted" or "not_found".

        Raises:
            PostgresExecutionError: the DROP DATABASE failed.
            PostgresValidationError: the name does not hold up.
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
                # Idempotent delete: an already-absent database is the desired end state, not an anomaly
                # (the delete-cleanup loop deliberately probes generations that may never have existed).
                logger.debug(f"Database {validated_database_name} does not exist on {self._host}, nothing to delete")
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

    async def count_active_connections(self, database_name: str) -> int:
        """How many backends are connected to a database, this connection excluded."""
        validated_database_name = self._validate_identifier(database_name, "database name")
        conn = await self._get_or_create_connection("postgres")
        count = await conn.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid()",
            validated_database_name,
        )
        return int(count or 0)

    async def list_databases(self) -> list[dict[str, Any]]:
        """Every database on this server, with its owner, that owner's superuser bit and
        whether the database allows connections.

        Raises:
            PostgresExecutionError: the query failed.
        """
        try:
            # Get connection to postgres database
            conn = await self._get_or_create_connection("postgres")

            databases = await conn.fetch("""
                SELECT d.datname as name,
                       o.rolname as owner,
                       o.rolsuper as owner_is_superuser,
                       d.datallowconn as allows_connections,
                       d.encoding,
                       d.datcollate as collate,
                       d.datctype as ctype
                FROM pg_database d JOIN pg_roles o ON o.oid = d.datdba
                WHERE d.datistemplate = false
                ORDER BY d.datname
            """)

            database_list = [dict(database) for database in databases]
            logger.debug(f"Retrieved {len(database_list)} databases from {self._host}")
            return database_list

        except Exception as e:
            logger.exception(f"Failed to list databases on {self._host}")
            raise PostgresExecutionError(f"Database listing failed: {e}") from e

    async def create_schema(self, schema_name: str, database: str, owner: str) -> dict[str, Any]:
        """Create a schema owned by ``owner`` in ``database``; status "created" or "exists".

        Raises:
            PostgresExecutionError: the CREATE SCHEMA failed.
            PostgresValidationError: a name does not hold up.
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
                logger.debug(f"Schema {validated_schema_name} already exists in database {validated_database}")
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

    async def set_role_search_path(self, username: str, database: str, schemas: list[str]) -> None:
        """Set a role's default search_path, scoped to one database.

        An application that does not set a search_path of its own then still resolves to the
        right schema. ``schemas`` keeps its order, the primary one first, and ``public`` is
        always appended last; it must not be empty.

        Runs: ALTER ROLE <user> IN DATABASE <db> SET search_path TO <schemas...>, public

        Raises:
            PostgresExecutionError: the ALTER ROLE failed.
            PostgresValidationError: a name does not hold up.
        """
        if not schemas:
            raise PostgresValidationError("set_role_search_path requires at least one schema")

        validated_username = self._validate_identifier(username, "username")
        validated_database = self._validate_identifier(database, "database")
        validated_schemas = [self._validate_identifier(schema, "schema") for schema in schemas]

        quoted_username = self._quote_identifier(validated_username)
        quoted_database = self._quote_identifier(validated_database)
        search_path = ", ".join(self._quote_identifier(schema) for schema in validated_schemas)

        conn = await self._get_or_create_connection(validated_database)
        await conn.execute(
            f"ALTER ROLE {quoted_username} IN DATABASE {quoted_database} SET search_path TO {search_path}, public"
        )
        logger.info(
            f"Set default search_path for role {validated_username} "
            f"in database {validated_database} to {', '.join(validated_schemas)}"
        )

    #: The role settings OPI may set. ``ALTER ROLE ... SET`` takes no bind parameter,
    #: so only an allowlist pattern keeps SQL injection out.
    _ROLE_SETTING_VALUES: ClassVar[dict[str, re.Pattern[str]]] = {
        "statement_timeout": re.compile(r"^[1-9][0-9]*(ms|s|min|h)?$"),
    }

    async def set_role_setting(self, username: str, setting: str, value: str) -> None:
        """Set one server-wide default for a role.

        Runs: ALTER ROLE <user> SET <setting> = '<value>'. Idempotent, so every processing
        round may do this again. Only the settings in ``_ROLE_SETTING_VALUES`` are accepted.

        Raises:
            PostgresExecutionError: the ALTER ROLE failed.
            PostgresValidationError: the name, the setting or the value does not hold up.
        """
        validated_username = self._validate_identifier(username, "username")
        pattern = self._ROLE_SETTING_VALUES.get(setting)
        if pattern is None:
            raise PostgresValidationError(f"role setting '{setting}' is not one OPI sets")
        if not pattern.match(value):
            raise PostgresValidationError(f"value '{value}' does not hold up for role setting '{setting}'")

        quoted_username = self._quote_identifier(validated_username)
        conn = await self._get_or_create_connection("postgres")
        try:
            await conn.execute(f"ALTER ROLE {quoted_username} SET {setting} = '{value}'")
        except asyncpg.PostgresError as e:
            logger.exception(f"Failed to set {setting} for {validated_username} on {self._host}")
            raise PostgresExecutionError(f"Setting {setting} failed: {e}") from e
        logger.info(f"Role {validated_username} on {self._host} has {setting} = {value}")

    async def delete_schema(self, schema_name: str, database: str, cascade: bool = False) -> dict[str, Any]:
        """Delete a schema; ``cascade`` takes the objects in it with it.

        Raises:
            PostgresExecutionError: the DROP SCHEMA failed.
            PostgresValidationError: a name does not hold up.
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
                # Idempotent delete: an already-absent schema is the desired end state, not an anomaly.
                logger.debug(f"Schema {validated_schema_name} does not exist, nothing to delete")
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

    async def database_has_user_data(self, database: str) -> bool:
        """Whether a database holds any application object outside the system schemas.

        This is the bottom under a restore: restoring into a database that already holds
        the application's tables adds the backup ON TOP of the rows already there, which
        is how a repeated restore doubles a row count. A database that exists but is still
        empty is fine to restore into, which is what a retry after a restore that failed
        halfway looks like, so the question is "empty?", not "exists?".

        True when any table, view, sequence or foreign table exists in a non-system schema.

        Raises:
            PostgresExecutionError: the check could not be performed.
        """
        try:
            conn = await self._get_or_create_connection(database)
            has_data = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                      AND n.nspname NOT LIKE 'pg\\_toast%'
                      AND n.nspname NOT LIKE 'pg\\_temp%'
                      AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
                )
            """)
            return bool(has_data)

        except Exception as e:
            logger.exception(f"Failed to inspect database {database} on {self._host}")
            raise PostgresExecutionError(f"Database emptiness check failed: {e}") from e

    async def list_schemas(self, database: str) -> list[dict[str, Any]]:
        """Every schema in ``database``.

        Raises:
            PostgresExecutionError: the query failed.
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

    async def describe_schemas(self, database: str) -> SchemaInventory:
        """Inventory a database's schemas for a clone.

        A clone takes along every schema of the source, but not ``public`` (postInitSQL
        owns it in the target) and not a schema that only exists because an extension
        lives in it. The caller needs to tell those apart, and needs to know what sits in
        ``public`` to warn about what stays behind.
        """
        schemas = [row["schema_name"] for row in await self.list_schemas(database)]
        extension_namespaces = {schema for schema in schemas if await self._get_schema_extensions(database, schema)}

        conn = await self._get_or_create_connection(database)
        rows = await conn.fetch("""
            SELECT c.relname AS name
            FROM pg_class c JOIN pg_namespace n ON c.relnamespace = n.oid
            WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
              AND NOT EXISTS (
                SELECT 1 FROM pg_depend d
                WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e'
              )
            UNION
            SELECT p.proname
            FROM pg_proc p JOIN pg_namespace n ON p.pronamespace = n.oid
            WHERE n.nspname = 'public'
              AND NOT EXISTS (
                SELECT 1 FROM pg_depend d
                WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e'
              )
            UNION
            SELECT t.typname
            FROM pg_type t JOIN pg_namespace n ON t.typnamespace = n.oid
            WHERE n.nspname = 'public' AND t.typtype IN ('e', 'd', 'r', 'c')
              AND (t.typrelid = 0 OR (SELECT c2.relkind FROM pg_class c2 WHERE c2.oid = t.typrelid) = 'c')
              AND NOT EXISTS (
                SELECT 1 FROM pg_depend d
                WHERE d.classid = 'pg_type'::regclass AND d.objid = t.oid AND d.deptype = 'e'
              )
            ORDER BY name
        """)

        return SchemaInventory(
            schemas=schemas,
            extension_namespaces=extension_namespaces,
            public_objects=[row["name"] for row in rows],
        )

    async def grant_schema_permissions(
        self, database: str, schema_name: str, username: str, permissions: list[str] | None = None
    ) -> dict[str, Any]:
        """Grant ``permissions`` on a schema to a role; USAGE and CREATE when not given.

        Raises:
            PostgresExecutionError: a GRANT failed.
            PostgresValidationError: a name or a permission does not hold up.
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

    async def grant_readonly_on_schema(self, database: str, schema_name: str, username: str) -> dict[str, Any]:
        """Grant strictly read-only access on a schema to a role.

        Unlike ``grant_schema_permissions``, which grants ALL on the tables, this grants only
        CONNECT, USAGE and SELECT, and sets default privileges so a table created later stays
        readable. This is the server-side boundary for a read-only database console; the
        tool's own read-only flag is no more than a hint.

        Raises:
            PostgresExecutionError: a GRANT failed.
            PostgresValidationError: a name does not hold up.
        """
        try:
            validated_database = self._validate_identifier(database, "database")
            validated_schema_name = self._validate_identifier(schema_name, "schema name")
            validated_username = self._validate_identifier(username, "username")

            conn = await self._get_or_create_connection(validated_database)

            schema_exists = await conn.fetchval(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = $1", validated_schema_name
            )
            user_exists = await conn.fetchval("SELECT 1 FROM pg_user WHERE usename = $1", validated_username)

            # Raise (don't return a status dict) so a missing schema/user can't
            # silently produce a read-only console that can read nothing.
            if not schema_exists:
                raise PostgresExecutionError(f"Schema {validated_schema_name} does not exist")
            if not user_exists:
                raise PostgresExecutionError(f"User {validated_username} does not exist")

            quoted_database = self._quote_identifier(validated_database)
            quoted_schema = self._quote_identifier(validated_schema_name)
            quoted_username = self._quote_identifier(validated_username)

            statements = [
                f"GRANT CONNECT ON DATABASE {quoted_database} TO {quoted_username}",
                f"GRANT USAGE ON SCHEMA {quoted_schema} TO {quoted_username}",
                f"GRANT SELECT ON ALL TABLES IN SCHEMA {quoted_schema} TO {quoted_username}",
                f"GRANT SELECT ON ALL SEQUENCES IN SCHEMA {quoted_schema} TO {quoted_username}",
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {quoted_schema} GRANT SELECT ON TABLES TO {quoted_username}",
            ]
            for statement in statements:
                await conn.execute(statement)

            logger.info(
                f"Granted read-only access on schema {validated_schema_name} "
                f"to role {validated_username} in database {validated_database} on host {self._host}"
            )
            return {"status": "granted", "message": "Read-only access granted successfully"}

        except PostgresValidationError:
            logger.exception("Validation failed for read-only grant")
            raise
        except Exception as e:
            logger.exception(f"Failed to grant read-only access on schema {schema_name} on {self._host}")
            raise PostgresExecutionError(f"Read-only grant failed: {e}") from e

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
        """The extensions installed in one schema of a database.

        With ``host``, ``username`` and ``password`` this opens a temporary connection to
        that server, for an external source; without them it uses the bound connection.
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
        """Install the source schema's extensions in the target schema, before the pg_dump.

        ``pg_dump -n`` leaves CREATE EXTENSION out, so the dumped DDL references extension
        functions (``uuid_generate_v4()``) the target does not have and the restore fails.

        ``source_host`` and friends are only for an external source; without them the source
        is the bound server. The target is always the bound server.

        Returns the extensions that were created.
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
        additional_schemas: list[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Clone a schema between two databases on the bound server.

        Streamed through pg_dump, which works on an active database; the PostgreSQL template
        route does not.

        ``additional_schemas`` are extra schemas to clone into the same target database, as
        ``(source_name, target_name)`` pairs: the ones ZAD declared plus the ones the
        application created itself. They ride along in the SAME single pg_dump as the default
        schema, so a reference between two of them restores.

        Raises:
            PostgresExecutionError: the clone failed.
            PostgresValidationError: a name does not hold up.
        """
        try:
            # Validate inputs
            validated_source_db = self._validate_identifier(source_database, "source_database")
            validated_target_db = self._validate_identifier(target_database, "target_database")
            validated_source_schema = self._validate_identifier(source_schema, "source_schema")
            validated_target_schema = self._validate_identifier(target_schema, "target_schema")
            validated_target_owner = self._validate_identifier(target_owner, "target_owner")
            validated_additional = [
                (
                    self._validate_identifier(src, "additional_source_schema"),
                    self._validate_identifier(tgt, "additional_target_schema"),
                )
                for src, tgt in (additional_schemas or [])
            ]

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

            schema_pairs = [(validated_source_schema, validated_target_schema), *validated_additional]

            # Pre-create extensions per source schema in the target before pg_dump
            # pg_dump -n does not include CREATE EXTENSION, so extension functions
            # (e.g. uuid_generate_v4()) would be missing in the target
            precreated_schemas = {
                source
                for source, _ in schema_pairs
                if await self._precreate_extensions_for_clone(
                    source_database=validated_source_db,
                    source_schema=source,
                    target_database=validated_target_db,
                    target_schema=source,
                )
            }

            # Clone within same server - use bound credentials for both source and target
            await self._execute_pgdump_clone(
                source_host=self._host,
                source_username=self._admin_username,
                source_password=self._admin_password,
                source_database=validated_source_db,
                target_host=self._host,
                target_port=5432,
                target_username=self._admin_username,
                target_password=self._admin_password,
                target_database=validated_target_db,
                target_owner=validated_target_owner,
                target_owner_password=target_owner_password,
                schema_pairs=schema_pairs,
                precreated_schemas=precreated_schemas,
                source_port=5432,
            )

            logger.info(
                f"Successfully cloned schema '{validated_source_schema}' from '{validated_source_db}' to '{validated_target_db}' as '{validated_target_schema}'"
            )
            for extra_source, extra_target in validated_additional:
                logger.info(f"Cloned extra schema '{extra_source}' -> '{extra_target}' in '{validated_target_db}'")

            return {
                "status": "success",
                "message": f"Database cloned successfully from {validated_source_db} to {validated_target_db}",
                "source_database": validated_source_db,
                "target_database": validated_target_db,
                "source_schema": validated_source_schema,
                "target_schema": validated_target_schema,
                "additional_schemas": validated_additional,
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
        """Clone a schema from another PostgreSQL server onto the bound server.

        The source is the ``source_*`` server, the target always the bound one.
        ``force_clone`` drops an existing target schema first.

        Raises:
            PostgresExecutionError: the clone failed.
            PostgresValidationError: a name, host or port does not hold up.
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
            # Source = external server, Target = bound server (self._host).
            # Both ports go through the allow-list validator in the executor.
            await self._execute_pgdump_clone(
                source_host=source_host,
                source_port=source_port,
                source_username=source_username,
                source_password=source_password,
                source_database=validated_source_db,
                target_host=self._host,
                target_port=5432,
                target_username=self._admin_username,
                target_password=self._admin_password,
                target_database=validated_target_db,
                target_owner=validated_target_owner,
                target_owner_password=target_owner_password,
                schema_pairs=[(validated_source_schema, validated_target_schema)],
                precreated_schemas={validated_source_schema} if precreated_extensions else set(),
                force_clone=force_clone,
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

    async def _psql(
        self, env: dict[str, str], database: str, sql: str, *, tuples_only: bool = False
    ) -> tuple[int, str, str]:
        """Run one statement with psql, returning ``(returncode, stdout, stderr)``.

        The clone pipeline needs the same call a dozen times over a set of schemas;
        connection parameters come from ``env`` so no value reaches a command line.
        """
        cmd = ["psql", "-d", database, *(["-t"] if tuples_only else []), "-c", sql]
        process = await asyncio.create_subprocess_exec(
            *cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await process.communicate()
        return process.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")

    async def _execute_pgdump_clone(
        self,
        source_host: str,
        source_username: str,
        source_password: str,
        source_database: str,
        target_host: str,
        target_username: str,
        target_password: str,
        target_database: str,
        target_owner: str,
        target_owner_password: str,
        schema_pairs: list[tuple[str, str]],
        precreated_schemas: set[str] | None = None,
        source_port: int = 5432,
        target_port: int = 5432,
        force_clone: bool = False,
    ) -> None:
        """Execute pg_dump clone from source to target with explicit connection parameters.

        This is the shared implementation used by both same-cluster and cross-cluster cloning.
        It uses pg_dump | psql streaming to copy schema data without requiring exclusive access.

        All schemas go through ONE pg_dump with a ``-n`` per schema (RC-360). pg_dump
        orders objects across schemas itself and adds foreign keys last, so schemas that
        reference each other restore under ``ON_ERROR_STOP=1``; a run per schema has no
        working order for a cycle. The restore runs as ``target_owner``, so every restored
        object is owned by that role.

        The port is fixed at PostgreSQL's default: an upstream port is never accepted. A host
        has to point at an in-cluster K8s service, which ``_validate_in_cluster_host``
        enforces, so a tenant cannot aim the streaming pipeline at an endpoint of its own.

        Args:
            schema_pairs: The schemas to clone, as ``(source_name, target_name)`` pairs; a
                pair with equal names keeps its name in the target.
            precreated_schemas: Source names ``_precreate_extensions_for_clone`` already
                created in the target, so their existence check is skipped.
            force_clone: Allows dropping an existing target schema.

        Raises:
            Exception: the clone failed.
        """
        if not schema_pairs:
            raise PostgresValidationError("schema_pairs cannot be empty")
        precreated = precreated_schemas or set()

        logger.info(
            f"Starting pg_dump streaming clone from {source_host}:{source_database} "
            f"to {target_host}:{target_database} for schemas: "
            f"{', '.join(f'{src} -> {tgt}' for src, tgt in schema_pairs)}"
        )

        try:
            # Set environment for target connection (used in multiple steps)
            env = child_env()
            env.update(
                {
                    "PGHOST": target_host,
                    "PGPORT": str(target_port),
                    "PGUSER": target_username,
                    "PGPASSWORD": target_password,
                }
            )

            # Stacked defense at the subprocess boundary. The public
            # clone_schema entry point already validates, but a future refactor
            # could route around it. This runs before step 1 so a malicious value
            # gets a PostgresValidationError before the first process spawns, the
            # schema-existence checks included:
            #   * identifiers: PG identifier rules (letter/_ start, no special
            #     chars, <=63 chars) -- _validate_identifier
            #   * hosts: in-cluster K8s service only (bare service name or
            #     '<svc>.<ns>.svc.cluster.local'); IPs and external domains
            #     rejected so a tenant cannot stream data out of the cluster
            #     -- _validate_in_cluster_host
            #   * passwords: no NUL / CR / LF so they cannot smuggle anything
            #     past env-var or libpq parsing -- _validate_pg_password_safe
            identifiers: list[tuple[str, str]] = [
                (source_database, "source_database"),
                (target_database, "target_database"),
                (source_username, "source_username"),
                (target_username, "target_username"),
                (target_owner, "target_owner"),
            ]
            for source_schema, target_schema in schema_pairs:
                identifiers.append((source_schema, "source_schema"))
                identifiers.append((target_schema, "target_schema"))
            for ident_value, ident_name in identifiers:
                self._validate_identifier(ident_value, ident_name)
            self._validate_in_cluster_host(source_host, "source_host")
            self._validate_in_cluster_host(target_host, "target_host")
            self._validate_pg_port(source_port, "source_port")
            self._validate_pg_port(target_port, "target_port")
            self._validate_pg_password_safe(source_password, "source_password")
            self._validate_pg_password_safe(target_password, "target_password")
            self._validate_pg_password_safe(target_owner_password, "target_owner_password")

            # Step 1: Check if the source schemas exist in the target database (safety check)
            # For a pre-created schema the check was already done by
            # _precreate_extensions_for_clone() - skip it to avoid duplication.
            for source_schema, _ in schema_pairs:
                if source_schema in precreated:
                    # The schema was created by admin via _precreate_extensions_for_clone().
                    # Transfer ownership to target_owner so the pg_dump pipeline (which runs
                    # as target_owner) has permission to create objects in the schema.
                    rc, _, grant_err = await self._psql(
                        env, target_database, f"ALTER SCHEMA {source_schema} OWNER TO {target_owner};"
                    )
                    if rc != 0:
                        raise Exception(f"Failed to transfer schema ownership to {target_owner}: {grant_err}")
                    logger.info(f"Transferred ownership of pre-created schema '{source_schema}' to {target_owner}")
                    continue

                _, check_out, _ = await self._psql(
                    env,
                    target_database,
                    f"SELECT 1 FROM information_schema.schemata WHERE schema_name = '{source_schema}';",  # noqa: S608  # psql -c does not interpolate psql variables, and the name passed _validate_identifier at the top of this function
                    tuples_only=True,
                )
                if not check_out.strip():
                    continue

                if source_schema == "public":
                    # Safe to drop 'public' schema - it's auto-created and we're about to recreate it
                    logger.info("Source schema 'public' exists in target database, dropping it (safe to recreate)")
                    await self._psql(env, target_database, "DROP SCHEMA IF EXISTS public CASCADE;")
                else:
                    # Schema exists but was NOT pre-created - likely a leftover from a
                    # previous failed clone. This is a legitimate safety concern.
                    raise Exception(
                        f"Source schema '{source_schema}' already exists in target database '{target_database}'. "
                        f"Manual intervention required to prevent accidental data loss. "
                        f"Please manually drop the schema if you want to proceed: "
                        f"DROP SCHEMA IF EXISTS {source_schema} CASCADE;"
                    )

            # Step 3: Drop target schemas that exist, where the names differ, AND force_clone is True
            for source_schema, target_schema in schema_pairs:
                if source_schema == target_schema:
                    continue
                if force_clone:
                    logger.info(f"force_clone=True: Preparing target schema {target_schema} (dropping if exists)")
                    await self._psql(env, target_database, f"DROP SCHEMA IF EXISTS {target_schema} CASCADE;")
                    continue

                _, check_target_out, _ = await self._psql(
                    env,
                    target_database,
                    f"SELECT 1 FROM information_schema.schemata WHERE schema_name = '{target_schema}';",  # noqa: S608  # psql -c does not interpolate psql variables, and the name passed _validate_identifier at the top of this function
                    tuples_only=True,
                )
                if check_target_out.strip():
                    raise Exception(
                        f"Target schema '{target_schema}' already exists in database '{target_database}' and force_clone=False. "
                        f"Set force_clone=True to overwrite existing schema."
                    )

            # Step 4: Stream complete schemas (structure + data) in one step
            logger.info("Streaming complete schemas using pg_dump pipeline")

            # Connection parameters are passed via the environment, never via the
            # command line, so attacker-controlled source host/username/password
            # values cannot be interpreted by a shell. pg_dump and psql are spawned
            # directly (no shell) and connected with an explicit pipe; the
            # CREATE SCHEMA -> CREATE SCHEMA IF NOT EXISTS rewrite (previously a
            # `sed` stage) is done in Python. The rewrite is needed because
            # extensions are pre-created in the target schema before pg_dump runs
            # (pg_dump -n omits CREATE EXTENSION), so without it the duplicate
            # CREATE SCHEMA would fail under ON_ERROR_STOP=1.
            source_env = child_env()
            source_env.update(
                {
                    "PGHOST": source_host,
                    "PGPORT": str(source_port),
                    "PGUSER": source_username,
                    "PGPASSWORD": source_password,
                }
            )
            target_env = child_env()
            target_env.update(
                {
                    "PGHOST": target_host,
                    "PGPORT": str(target_port),
                    "PGUSER": target_owner,
                    "PGPASSWORD": target_owner_password,
                }
            )

            from opi.core.metrics import track_subprocess_memory

            dump_args = ["pg_dump", "-d", source_database]
            for source_schema, _ in schema_pairs:
                dump_args += ["-n", source_schema]
            dump_args += ["--no-owner", "--no-privileges"]

            async with track_subprocess_memory("postgres-clone"):
                # OS-pipe streaming pipeline: pg_dump -> sed -> psql.
                # Equivalent flow + buffering to a shell pipe, without any
                # shell involvement: subprocess.Popen takes argv lists (no
                # shell parsing of arguments) and env dicts (no shell parsing
                # of values). The sed regex is a fixed literal string.
                # Streamed via OS pipes so memory stays bounded by the kernel
                # pipe buffer (~64 KiB per stage) regardless of dump size.
                #
                # Wrapped in asyncio.to_thread because subprocess.Popen and
                # its .wait()/.communicate() block the running thread; the
                # event loop keeps running on the main thread.
                import subprocess

                def _run_clone_pipeline() -> tuple[int, bytes, int, bytes, bytes]:
                    dump = subprocess.Popen(  # noqa: S603  # no shell; the variable parts are separate argv elements
                        dump_args,
                        env=source_env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    rewrite = subprocess.Popen(
                        ["sed", "s/^CREATE SCHEMA /CREATE SCHEMA IF NOT EXISTS /g"],  # noqa: S607  # a tool from the image's PATH, not a fixed path
                        stdin=dump.stdout,
                        stdout=subprocess.PIPE,
                        env=child_env(),
                    )
                    # Close our handle so sed sees EOF when pg_dump exits.
                    if dump.stdout is None:
                        raise RuntimeError("pg_dump did not open a stdout pipe")
                    dump.stdout.close()
                    restore = subprocess.Popen(  # noqa: S603  # no shell; the variable parts are separate argv elements
                        ["psql", "-v", "ON_ERROR_STOP=1", "-d", target_database],  # noqa: S607  # a tool from the image's PATH, not a fixed path
                        env=target_env,
                        stdin=rewrite.stdout,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    # Close our handle so psql sees EOF when sed exits.
                    if rewrite.stdout is None:
                        raise RuntimeError("sed did not open a stdout pipe")
                    rewrite.stdout.close()

                    restore_out, restore_err = restore.communicate()
                    dump_stderr = dump.stderr.read() if dump.stderr else b""
                    dump.wait()
                    rewrite.wait()
                    return dump.returncode or 0, dump_stderr, restore.returncode or 0, restore_out, restore_err

                dump_rc, dump_err, restore_rc, clone_out, clone_err = await asyncio.to_thread(_run_clone_pipeline)

            # Log only errors or important warnings
            combined_err = (dump_err or b"") + (clone_err or b"")
            if combined_err:
                stderr_text = combined_err.decode(errors="replace").strip()
                # Only log if it contains actual errors, not just version info
                if "error:" in stderr_text.lower() and "version mismatch" not in stderr_text.lower():
                    logger.error(f"pg_dump stderr: {stderr_text}")
                elif stderr_text:
                    logger.debug(f"pg_dump stderr: {stderr_text}")

            if dump_rc != 0:
                raise Exception(f"pg_dump failed: {dump_err.decode(errors='replace')}")

            # Check for errors
            if restore_rc != 0:
                raise Exception(f"Schema clone pipeline failed: {clone_err.decode(errors='replace')}")

            # Check if pg_dump actually produced any output
            if not clone_out and not clone_err:
                logger.warning("pg_dump produced no output - this might indicate an empty source schema")
            elif clone_out and len(clone_out.decode().strip()) < 500:
                logger.warning(
                    f"pg_dump produced very little output ({len(clone_out.decode())} bytes) - might be empty dump"
                )
                logger.debug(f"pg_dump output size: {len(clone_out)} bytes")

            logger.info("Schemas cloned successfully (structure + data)")

            # Step 5: Check per schema that the target name is there, renaming where needed
            for source_schema, target_schema in schema_pairs:
                _, check_out, _ = await self._psql(
                    env,
                    target_database,
                    f"SELECT 1 FROM information_schema.schemata WHERE schema_name = '{target_schema}';",  # noqa: S608  # psql -c does not interpolate psql variables, and the name passed _validate_identifier at the top of this function
                    tuples_only=True,
                )
                if check_out.strip():
                    logger.debug(f"Target schema '{target_schema}' was created directly")
                    continue

                # pg_dump restores a schema under its SOURCE name -- it cannot
                # rename on the fly. So whenever source and target names differ
                # (the normal generational-clone case), the cloned schema arrives
                # as `source_schema` and we rename it to `target_schema` here.
                # This is the expected path, NOT a failure -- only a dump that
                # produced no schema at all is an error.
                _, list_out, _ = await self._psql(
                    env,
                    target_database,
                    "SELECT schema_name FROM information_schema.schemata "
                    "WHERE schema_name NOT IN ('information_schema', 'pg_catalog', 'pg_toast');",
                    tuples_only=True,
                )
                existing_schemas = list_out.strip()

                if source_schema in existing_schemas and source_schema != target_schema:
                    # Normal path: rename the freshly-restored schema to the target.
                    logger.info(f"Renaming cloned schema '{source_schema}' -> '{target_schema}'")
                    rc, _, rename_err = await self._psql(
                        env, target_database, f"ALTER SCHEMA {source_schema} RENAME TO {target_schema};"
                    )
                    if rc != 0:
                        raise Exception(f"Failed to rename cloned schema to '{target_schema}': {rename_err}")
                    logger.info(f"Cloned schema is now '{target_schema}'")
                else:
                    # Neither the target nor the source schema materialised: the
                    # dump genuinely produced nothing. THIS is the real error.
                    logger.error(
                        f"Clone produced no usable schema in target '{target_database}' "
                        f"(target '{target_schema}' absent; existing schemas: {existing_schemas or 'none'}); "
                        f"source schema '{source_schema}' may be empty"
                    )
                    raise Exception(
                        f"Schema '{target_schema}' was not created during clone - source '{source_schema}' may be empty"
                    )

            # Step 6: Set proper ownership of the final schemas
            for _, target_schema in schema_pairs:
                logger.info(f"Setting ownership of schema {target_schema} to {target_owner}")
                rc, _, owner_err = await self._psql(
                    env, target_database, f"ALTER SCHEMA {target_schema} OWNER TO {target_owner};"
                )
                if rc != 0:
                    logger.warning(f"Failed to set schema ownership: {owner_err}")
                else:
                    logger.info(f"Successfully set schema ownership to {target_owner}")

        except Exception as e:
            logger.exception(f"pg_dump streaming clone failed: {e}")
            raise Exception(f"Failed to clone schema using pg_dump: {e}") from e


def create_postgres_connector(host: str, admin_username: str, admin_password: str) -> PostgresConnector:
    """A connector bound to one server and one set of admin credentials."""
    return PostgresConnector(host, admin_username, admin_password)
