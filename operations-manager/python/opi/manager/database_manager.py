"""Database service manager for handling PostgreSQL resources."""

import logging
from typing import Any

from jsonpath_ng.ext import parse as jsonpath_parse

from opi.connectors.postgres import PostgresConnector, create_postgres_connector
from opi.core.config import settings
from opi.core.database_pool import DatabasePool
from opi.services import ServiceType
from opi.utils.naming import generate_resource_identifier
from opi.utils.passwords import generate_secure_password
from opi.utils.secrets import DatabaseSecret

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Manager for database-related operations and resources."""

    def __init__(self, project_manager: "ProjectManager", db_pool: DatabasePool) -> None:
        """
        Initialize the DatabaseManager with reference to ProjectManager and database pool.

        Args:
            project_manager: The main ProjectManager instance for accessing shared resources
            db_pool: DatabasePool instance for PostgreSQL connections
        """
        self.project_manager = project_manager
        self.db_pool = db_pool
        self._postgres_connector: PostgresConnector | None = None

    async def _ensure_connection(self) -> None:
        """Ensure we have an active PostgreSQL connection."""
        if self._postgres_connector is None:
            self._postgres_connector = await create_postgres_connector(self.db_pool)

    @property
    def postgres_connector(self) -> PostgresConnector:
        """Get the PostgreSQL connector. Must call _ensure_connection() first."""
        assert self._postgres_connector is not None, "Connection not initialized. Call _ensure_connection() first."
        return self._postgres_connector

    async def close(self) -> None:
        """Close the PostgreSQL connection."""
        if self._postgres_connector is not None:
            await self.postgres_connector.close()
            self._postgres_connector = None

    async def create_resources_for_deployment(self, project_data: dict[str, Any], deployment: dict[str, Any]) -> None:
        """
        Create database resources for a deployment that has PostgreSQL service enabled.

        This method follows an idempotent flow:
        1. Check for existing credentials
        2. Validate/create working credentials
        3. Ensure database exists (handle clone-from/force-clone)
        4. Ensure schema exists (unless cloned)
        5. Store final credentials

        On replay, the outcome should always be the same regardless of starting state.

        Args:
            project_data: The project configuration data
            deployment: The specific deployment configuration
        """
        project_name = project_data["name"]
        deployment_name = deployment["name"]

        # Check if this deployment has PostgreSQL service enabled
        if not await self._deployment_uses_postgresql(project_data, deployment_name):
            logger.debug(f"Deployment {deployment_name} does not use PostgreSQL service, skipping")
            return

        logger.info(f"Processing database resources for project: {project_name}, deployment: {deployment_name}")

        progress_manager = self.project_manager.get_progress_manager()
        database_task = None
        if progress_manager:
            database_task = progress_manager.add_task("Creating database resources")

        try:
            # Ensure we have a PostgreSQL connection for this deployment
            await self._ensure_connection()

            # Generate consistent database identifiers (username, schema, database all use same pattern)
            db_identifier = generate_resource_identifier(project_name, deployment_name, "_")
            db_username = db_identifier
            db_schema = db_identifier
            db_database = db_identifier  # Same as schema for isolation

            # PHASE 1: CREDENTIAL RESOLUTION - Determine working credentials
            logger.info(f"Phase 1: Resolving database credentials for {project_name}/{deployment_name}")
            db_password = await self._resolve_database_credentials(
                project_name, deployment_name, deployment, db_username, db_database, db_schema
            )

            # PHASE 2: DATABASE STATE VERIFICATION - Ensure database exists with correct state
            logger.info(f"Phase 2: Verifying database state for {project_name}/{deployment_name}")
            await self._ensure_database_state(
                project_name, deployment_name, deployment, db_database, db_schema, db_username, db_password
            )

            # PHASE 3: FINAL STATE STORAGE - Store working credentials
            logger.info(f"Phase 3: Storing final credentials for {project_name}/{deployment_name}")
            database_secret = DatabaseSecret(
                host=settings.DATABASE_HOST,
                port=5432,  # Standard PostgreSQL port
                username=db_username,
                password=db_password,
                schema=db_schema,
                database=db_database,  # Project-specific database name
            )
            self.project_manager._add_secret_to_create(
                deployment_name,
                "database",
                database_secret,
            )

            logger.info(f"Database resources ready for {deployment_name} (stored in secrets map)")

        finally:
            if progress_manager and database_task:
                progress_manager.complete_task(database_task)

    async def _create_or_update_user(self, db_username: str, postgres_conn) -> tuple[str, dict[str, Any]]:
        db_password = generate_secure_password(min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20)
        # Try to create the user first
        create_result = await postgres_conn.create_user(
            host=settings.DATABASE_HOST,
            admin_username=settings.DATABASE_ADMIN_NAME,
            admin_password=settings.DATABASE_ADMIN_PASSWORD,
            username=db_username,
            password=db_password,
        )

        if create_result["status"] == "exists":
            # User already exists, update password instead
            update_result = await postgres_conn.update_user_password(
                username=db_username,
                new_password=db_password,
            )
            return db_password, update_result
        else:
            # User was created (or error occurred)
            return db_password, create_result

    async def _resolve_database_credentials(
        self,
        project_name: str,
        deployment_name: str,
        deployment: dict[str, Any],
        db_username: str,
        db_database: str,
        db_schema: str,
    ) -> str:
        """
        Resolve working database credentials through the following steps:
        1. Check for existing credentials in Kubernetes
        2. Test credential validity if they exist
        3. Create or update credentials if needed

        Returns the working password.
        """
        # Step 1: Check for existing credentials
        existing_credentials = await self._get_existing_database_credentials_from_k8s(deployment_name, deployment)

        if existing_credentials:
            logger.info(f"Found existing database secret in Kubernetes for {project_name}/{deployment_name}")
            db_password = existing_credentials.password

            if not db_password:
                logger.error(f"Database secret exists but has no password for {project_name}/{deployment_name}")
                raise ValueError(f"Invalid database secret for {project_name}/{deployment_name}: missing password")

            # Step 2: Test existing credentials
            logger.info(f"Testing existing database credentials for {project_name}/{deployment_name}")
            credentials_valid = await self._test_database_connection(db_username, db_password, db_database, db_schema)

            if credentials_valid:
                logger.info(f"Existing database credentials are valid for {project_name}/{deployment_name}")
                return db_password
            else:
                # Step 3a: Handle invalid existing credentials
                if settings.RECREATE_PASSWORD_ON_AUTHENTICATION_FAILURE:
                    logger.warning(
                        f"Database credentials are invalid for {project_name}/{deployment_name}, updating password"
                    )
                    new_password, update_result = await self._create_or_update_user(
                        db_username, self.postgres_connector
                    )
                    if update_result["status"] not in ["updated", "created"]:
                        raise Exception(
                            f"Failed to update password for database user {db_username}: {update_result.get('message', 'Unknown error')}"
                        )
                    logger.info(f"Updated password for database user: {db_username}")
                    return new_password
                else:
                    raise Exception(
                        f"Database secret exists for {project_name}/{deployment_name} but credentials are invalid. "
                        f"Manual intervention required to fix database user or update secret."
                    )
        else:
            # Step 3b: No existing secret, create new credentials
            logger.info(f"No database secret found in Kubernetes for {project_name}/{deployment_name}")
            db_password, create_result = await self._create_or_update_user(db_username, self.postgres_connector)

            if create_result["status"] == "error":
                raise Exception(
                    f"Failed to create database user {db_username}: {create_result.get('message', 'Unknown error')}"
                )
            elif create_result["status"] == "exists":
                # User exists, but we don't have the password, update it
                logger.info(f"Database user {db_username} already exists, updating password")
                update_result = await self.postgres_connector.update_user_password(
                    username=db_username,
                    new_password=db_password,
                )
                if update_result["status"] != "success":
                    raise Exception(
                        f"Failed to update password for existing database user {db_username}: {update_result.get('message', 'Unknown error')}"
                    )
                logger.info(f"Updated password for existing database user: {db_username}")
            else:
                logger.info(f"Created new database user: {db_username}")

            return db_password

    async def _ensure_database_state(
        self,
        project_name: str,
        deployment_name: str,
        deployment: dict[str, Any],
        db_database: str,
        db_schema: str,
        db_username: str,
        db_password: str,
    ) -> None:
        """
        Ensure the database exists in the correct state, handling clone-from and force-clone logic.
        This runs regardless of whether credentials existed initially.
        """
        clone_from = deployment.get("clone-from")
        force_clone = deployment.get("force-clone", False)

        if clone_from:
            # Handle database cloning
            source_database = generate_resource_identifier(project_name, clone_from, "_")
            source_schema = generate_resource_identifier(project_name, clone_from, "_")
            logger.info(f"Clone requested from {source_database} to {db_database} (force={force_clone})")

            # STEP 1: Validate that source database and schema exist before cloning
            await self._validate_clone_source(source_database, source_schema)

            if force_clone:
                # Force clone: drop and recreate database
                logger.info(f"Force clone enabled, dropping existing database {db_database} if it exists")
                drop_result = await self.postgres_connector.delete_database(
                    host=settings.DATABASE_HOST,
                    admin_username=settings.DATABASE_ADMIN_NAME,
                    admin_password=settings.DATABASE_ADMIN_PASSWORD,
                    database_name=db_database,
                )
                if drop_result["status"] == "deleted":
                    logger.info(f"Dropped existing database: {db_database}")

                # Recreate database after dropping
                database_result = await self.postgres_connector.create_database(
                    host=settings.DATABASE_HOST,
                    admin_username=settings.DATABASE_ADMIN_NAME,
                    admin_password=settings.DATABASE_ADMIN_PASSWORD,
                    database_name=db_database,
                    owner=db_username,
                )
                if database_result["status"] != "created":
                    raise Exception(
                        f"Failed to recreate database {db_database} after force drop: {database_result.get('message', 'Unknown error')}"
                    )
                logger.info(f"Recreated database after force drop: {db_database}")
            else:
                # Regular clone: ensure database exists first
                database_result = await self.postgres_connector.create_database(
                    host=settings.DATABASE_HOST,
                    admin_username=settings.DATABASE_ADMIN_NAME,
                    admin_password=settings.DATABASE_ADMIN_PASSWORD,
                    database_name=db_database,
                    owner=db_username,
                )
                if database_result["status"] == "created":
                    logger.info(f"Created database for cloning: {db_database}")
                else:
                    logger.info(f"Database already exists for cloning: {db_database}")

            # STEP 2: Perform the clone operation using database template
            clone_result = await self.postgres_connector.clone_schema(
                source_database=source_database,
                target_database=db_database,
                source_schema=source_schema,
                target_schema=db_schema,
                host=settings.DATABASE_HOST,
                admin_username=settings.DATABASE_ADMIN_NAME,
                admin_password=settings.DATABASE_ADMIN_PASSWORD,
                target_owner=db_username,
                target_owner_password=db_password,
            )

            if clone_result["status"] != "success":
                raise RuntimeError(f"Failed to clone database: {clone_result.get('message', 'Unknown error')}")

            logger.info(f"Successfully cloned database from {source_database} to {db_database}")
            logger.info(f"Schema cloned with target name '{db_schema}' - no additional rename needed")
        else:
            # Normal flow: ensure database and schema exist
            database_result = await self.postgres_connector.create_database(
                host=settings.DATABASE_HOST,
                admin_username=settings.DATABASE_ADMIN_NAME,
                admin_password=settings.DATABASE_ADMIN_PASSWORD,
                database_name=db_database,
                owner=db_username,
            )

            if database_result["status"] not in ["created", "exists"]:
                raise Exception(
                    f"Failed to create database {db_database}: {database_result.get('message', 'Unknown error')}"
                )

            if database_result["status"] == "created":
                logger.info(f"Created database: {db_database}")
            else:
                logger.info(f"Database already exists: {db_database}")

            # Create or verify schema exists
            schema_result = await self.postgres_connector.create_schema(
                host=settings.DATABASE_HOST,
                admin_username=settings.DATABASE_ADMIN_NAME,
                admin_password=settings.DATABASE_ADMIN_PASSWORD,
                schema_name=db_schema,
                database=db_database,
                owner=db_username,
            )

            if schema_result["status"] == "created":
                logger.info(f"Created database schema: {db_schema}")
            else:
                logger.info(f"Database schema already exists: {db_schema}")

    async def _validate_clone_source(self, source_database: str, source_schema: str) -> None:
        """
        Validate that the source database and schema exist before attempting to clone.

        Args:
            source_database: Name of the source database
            source_schema: Name of the source schema that should exist

        Raises:
            Exception: If source database or schema doesn't exist
        """
        logger.info(f"Validating clone source: database={source_database}, schema={source_schema}")

        # Check if source database exists
        source_db_exists = await self.postgres_connector._get_connection().fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", source_database
        )

        if not source_db_exists:
            raise Exception(
                f"Cannot clone from {source_database}: source database does not exist. "
                f"Ensure the source deployment has been created successfully first."
            )

        # Connect to source database to check schema
        import asyncpg

        source_conn = None
        try:
            source_conn = await asyncpg.connect(
                host=settings.DATABASE_HOST,
                port=5432,
                user=settings.DATABASE_ADMIN_NAME,
                password=settings.DATABASE_ADMIN_PASSWORD,
                database=source_database,
            )

            # Check if source schema exists
            schema_exists = await source_conn.fetchval(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = $1", source_schema
            )

            if not schema_exists:
                raise Exception(
                    f"Cannot clone from {source_database}: source schema '{source_schema}' does not exist. "
                    f"The source deployment appears to be empty or incomplete. "
                    f"Ensure the source deployment has been properly initialized with data."
                )

            # Check if source schema has any tables (optional but recommended)
            table_count = await source_conn.fetchval(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = $1", source_schema
            )

            if table_count == 0:
                logger.warning(
                    f"Source schema '{source_schema}' exists but contains no tables. "
                    f"Cloning will result in an empty schema."
                )
            else:
                logger.info(f"Source schema '{source_schema}' validated successfully with {table_count} tables")

        except asyncpg.InvalidCatalogNameError:
            raise Exception(
                f"Cannot connect to source database {source_database}. "
                f"Database may not exist or may not be accessible."
            )
        finally:
            if source_conn:
                await source_conn.close()

    async def delete_resources_for_deployment(
        self, project_data: dict[str, Any], deployment: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Delete database resources for a deployment.

        Args:
            project_data: The project configuration data
            deployment: The specific deployment configuration

        Returns:
            Dictionary containing deletion results and status
        """
        project_name = project_data["name"]
        deployment_name = deployment["name"]

        deletion_results = {
            "service": "database",
            "deployment": deployment_name,
            "operations": [],
            "success": True,
            "errors": [],
        }

        # Check if this deployment uses PostgreSQL service
        if not await self._deployment_uses_postgresql(project_data, deployment_name):
            deletion_results["operations"].append(
                {
                    "type": "database_cleanup",
                    "status": "skipped",
                    "reason": "Deployment does not use PostgreSQL service",
                }
            )
            logger.debug(f"Deployment {deployment_name} does not use PostgreSQL service, skipping database cleanup")
            return deletion_results

        logger.info(f"Deleting database resources for project: {project_name}, deployment: {deployment_name}")

        try:
            # Ensure we have a PostgreSQL connection for this deployment
            await self._ensure_connection()

            db_identifier = generate_resource_identifier(project_name, deployment_name, "_")
            db_username = db_identifier
            db_database = db_identifier
            db_schema = db_identifier

            # Delete database (this will cascade delete all schemas and objects within it)
            try:
                database_result = await self.postgres_connector.delete_database(
                    host=settings.DATABASE_HOST,
                    admin_username=settings.DATABASE_ADMIN_NAME,
                    admin_password=settings.DATABASE_ADMIN_PASSWORD,
                    database_name=db_database,
                )

                if database_result["status"] == "success":
                    deletion_results["operations"].append(
                        {"type": "database_deletion", "target": db_database, "status": "success"}
                    )
                    logger.info(f"Successfully deleted database (with all schemas): {db_database}")
                else:
                    deletion_results["operations"].append(
                        {
                            "type": "database_deletion",
                            "target": db_database,
                            "status": "not_found"
                            if "does not exist" in database_result.get("message", "")
                            else "failed",
                            "error": database_result.get("message", "Unknown error"),
                        }
                    )
                    if "does not exist" not in database_result.get("message", ""):
                        deletion_results["errors"].append(
                            f"Failed to delete database {db_database}: {database_result.get('message')}"
                        )

            except Exception as e:
                deletion_results["operations"].append(
                    {"type": "database_deletion", "target": db_database, "status": "error", "error": str(e)}
                )
                deletion_results["errors"].append(f"Error deleting database {db_database}: {e}")
                logger.exception(f"Error deleting database {db_database}: {e}")

            # Delete user (do this last since it owns the database)
            try:
                update_result = await self.postgres_connector.delete_user(
                    host=settings.DATABASE_HOST,
                    admin_username=settings.DATABASE_ADMIN_NAME,
                    admin_password=settings.DATABASE_ADMIN_PASSWORD,
                    username=db_username,
                )

                if update_result["status"] == "success":
                    deletion_results["operations"].append(
                        {"type": "database_user_deletion", "target": db_username, "status": "success"}
                    )
                    logger.info(f"Successfully deleted database user: {db_username}")
                else:
                    deletion_results["operations"].append(
                        {
                            "type": "database_user_deletion",
                            "target": db_username,
                            "status": "not_found" if "does not exist" in update_result.get("message", "") else "failed",
                            "error": update_result.get("message", "Unknown error"),
                        }
                    )
                    if "does not exist" not in update_result.get("message", ""):
                        deletion_results["errors"].append(
                            f"Failed to delete user {db_username}: {update_result.get('message')}"
                        )

            except Exception as e:
                deletion_results["operations"].append(
                    {"type": "database_user_deletion", "target": db_username, "status": "error", "error": str(e)}
                )
                deletion_results["errors"].append(f"Error deleting user {db_username}: {e}")
                logger.exception(f"Error deleting database user {db_username}: {e}")

        except Exception as e:
            deletion_results["success"] = False
            deletion_results["errors"].append(f"Failed to connect to database: {e}")
            logger.exception(f"Error connecting to database for cleanup: {e}")

        # Update success status based on errors
        deletion_results["success"] = len(deletion_results["errors"]) == 0

        return deletion_results

    async def _deployment_uses_postgresql(self, project_data: dict[str, Any], deployment_name: str) -> bool:
        """
        Check if a deployment uses PostgreSQL service.

        Args:
            project_data: The project configuration data
            deployment_name: Name of the deployment to check

        Returns:
            True if deployment uses PostgreSQL service, False otherwise
        """
        # First get component references for this deployment
        component_refs_query = jsonpath_parse(f"$.deployments[?@.name=='{deployment_name}'].components[*].reference")
        component_refs = [match.value for match in component_refs_query.find(project_data)]

        # Then check if any of these components use PostgreSQL service
        for component_ref in component_refs:
            component_query = jsonpath_parse(f"$.components[?@.name=='{component_ref}']['uses-services']")
            component_services = [match.value for match in component_query.find(project_data)]
            # Flatten the services list (in case it's nested)
            all_services = []
            for services in component_services:
                if isinstance(services, list):
                    all_services.extend(services)
                else:
                    all_services.append(services)

            if ServiceType.POSTGRESQL_DATABASE.value in all_services:
                return True

        return False

    async def _get_existing_database_credentials_from_k8s(
        self, deployment_name: str, deployment: dict[str, Any]
    ) -> DatabaseSecret | None:
        """
        Get existing database credentials from Kubernetes secret.

        Args:
            deployment_name: Name of the deployment
            deployment: The deployment configuration containing namespace info

        Returns:
            DatabaseSecret if found, None otherwise
        """
        try:
            from opi.core.cluster_config import get_prefixed_namespace
            from opi.core.config import settings

            secret_name = DatabaseSecret.get_secret_name(deployment_name)
            kubectl_connector = self.project_manager._kubectl_connector

            # Calculate the namespace where the secret should be stored
            namespace = get_prefixed_namespace(settings.CLUSTER_MANAGER, deployment["namespace"])

            # Try to get the secret from Kubernetes
            secret_data = await kubectl_connector.get_secret(secret_name, namespace)
            if secret_data:
                return DatabaseSecret.from_k8s_secret_data(secret_data)
            return None
        except Exception as e:
            logger.debug(f"Could not retrieve database secret for {deployment_name}: {e}")
            return None

    @staticmethod
    async def _test_database_connection(
        username: str,
        password: str,
        database: str,
        schema: str,
    ) -> bool:
        """
        Test if database credentials are valid by attempting a direct connection with user credentials.

        This method creates its own connection since it needs to test with the user's credentials,
        not the admin credentials used by the pooled connections.

        Args:
            username: Database username to test
            password: Database password to test
            database: Database name to connect to
            schema: Schema name (for logging/context)

        Returns:
            True if connection successful, False otherwise
        """
        import asyncpg

        try:
            # Create a direct connection with the user's credentials
            conn = await asyncpg.connect(
                host=settings.DATABASE_HOST, port=5432, user=username, password=password, database=database
            )
            await conn.close()
            logger.debug(f"Connection test successful for {username}@{settings.DATABASE_HOST}/{database}")
            return True
        except Exception as e:
            logger.debug(f"Database connection test failed for user {username}: {e}")
            return False
