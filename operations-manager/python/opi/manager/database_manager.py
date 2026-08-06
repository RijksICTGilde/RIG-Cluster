"""Database service manager for handling PostgreSQL resources."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opi.manager.project_manager import ProjectManager
    from opi.services.marked_for_deletion_service import MarkedForDeletionService

from opi.connectors.postgres import PostgresConnector, create_postgres_connector
from opi.core.cluster_config import get_database_server
from opi.core.config import settings
from opi.services import CloneFromType, ServiceType
from opi.services.catalog.shared.postgres import DedicatedPostgresFields
from opi.services.postgres_scope import (
    get_dedicated_postgres_config,
    get_postgres_schemas,
    project_uses_dedicated_postgres,
)
from opi.services.project import Project
from opi.utils.naming import generate_database_name, generate_extra_database_schema
from opi.utils.passwords import generate_secure_password
from opi.utils.secrets import DatabaseSecret

logger = logging.getLogger(__name__)


@dataclass
class DatabaseStateResult:
    """Result of _ensure_database_state with the final resolved identifiers."""

    database: str
    schema: str
    password: str


class DatabaseManager:
    """Manager for database-related operations and resources.

    DatabaseManager is bound to ONE PostgreSQL server with specific admin credentials.
    All database operations are performed on this bound server.
    """

    def __init__(self, project_manager: ProjectManager, db_host: str, admin_username: str, admin_password: str) -> None:
        """
        Initialize the DatabaseManager bound to one PostgreSQL server.

        Args:
            project_manager: The main ProjectManager instance for accessing shared resources
            db_host: Database host (e.g., shared or namespace-specific service endpoint)
            admin_username: Admin username for database operations
            admin_password: Admin password for database operations
        """
        self.project_manager = project_manager
        self._db_host = db_host
        self._admin_username = admin_username
        self._admin_password = admin_password
        self._postgres_connector: PostgresConnector | None = None

    def _ensure_connection(self) -> None:
        """Ensure we have a PostgreSQL connector instance bound to our credentials."""
        if self._postgres_connector is None:
            self._postgres_connector = create_postgres_connector(
                host=self._db_host, admin_username=self._admin_username, admin_password=self._admin_password
            )

    @property
    def postgres_connector(self) -> PostgresConnector:
        """Get the PostgreSQL connector. Ensures connection is initialized."""
        if self._postgres_connector is None:
            self._ensure_connection()
        if self._postgres_connector is None:
            raise RuntimeError("PostgreSQL connector failed to initialize")
        return self._postgres_connector

    async def close(self) -> None:
        """Close the PostgreSQL connection."""
        if self._postgres_connector is not None:
            await self._postgres_connector.close()
            self._postgres_connector = None

    def _get_deployment_database_generation(
        self, project_data: dict[str, Any], deployment_name: str, service_type: str | None = None
    ) -> int | None:
        """
        Get the database generation for a deployment from the project file.

        Reads from the deployment-level services block:
        deployments[name].services.{service_type}.generation

        Args:
            project_data: The project configuration data
            deployment_name: Name of the deployment
            service_type: Service type (uses ServiceType enum values). If None, determines
                          from project config (namespace-postgresql-database or postgresql-database)

        Returns:
            Generation number if set, None otherwise
        """
        if service_type is None:
            # Determine service type from project configuration
            if self._project_uses_namespace_postgresql(project_data):
                service_type = ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value
            else:
                service_type = ServiceType.POSTGRESQL_DATABASE.value

        project_file_handler = self.project_manager._project_file_handler
        return project_file_handler.get_deployment_service_generation(project_data, deployment_name, service_type)

    async def create_resources_for_deployment(
        self,
        project_data: dict[str, Any],
        deployment: dict[str, Any],
        force_clone: bool = False,
    ) -> None:
        """
        Create database resources for a deployment that has PostgreSQL service enabled.

        This method handles both shared and namespace-specific PostgreSQL databases:
        - Shared: Creates database/user on shared cluster (settings.DATABASE_HOST)
        - Namespace-specific: Creates database/user on project's dedicated cluster

        Flow:
        1. Check for existing credentials
        2. Validate/create working credentials
        3. Ensure database exists (handle clone-from/force-clone)
        4. Ensure schema exists (unless cloned)
        5. Store final credentials with correct service endpoint

        On replay, the outcome should always be the same regardless of starting state.

        Args:
            project_data: The project configuration data
            deployment: The specific deployment configuration
            force_clone: If True, force clone even if target already exists (runtime override)
        """
        project_name = await self.project_manager.get_name()
        deployment_name = deployment["name"]
        cluster_name = deployment.get("cluster")

        if not cluster_name:
            raise ValueError(f"Deployment {deployment_name} is missing required 'cluster' field")

        # Check if this deployment has PostgreSQL service enabled
        if not await self._deployment_uses_postgresql(project_data, deployment_name):
            logger.debug(f"Deployment {deployment_name} does not use PostgreSQL service, skipping")
            return

        logger.info(f"Processing database resources for project: {project_name}, deployment: {deployment_name}")

        progress_manager = self.project_manager.get_progress_manager()
        database_task = None
        if progress_manager:
            database_task = progress_manager.add_task("Database klaarmaken")

        try:
            # Determine placement: a dedicated (project-scoped) cluster or the shared
            # instance. Both namespace-postgresql-database and postgresql-database with
            # scope: project mean "dedicated" (RC-17).
            uses_dedicated_postgresql = project_uses_dedicated_postgres(project_data)

            # Get appropriate configuration
            if uses_dedicated_postgresql:
                # Dedicated-cluster configuration
                cluster_config = self._get_database_cluster_config(project_data, cluster_name)
                db_host = cluster_config["service_endpoint"]
                infrastructure_namespace = cluster_config["infrastructure_namespace"]

                # Get superuser credentials from infrastructure namespace
                logger.info(
                    f"Using dedicated PostgreSQL cluster for {project_name} "
                    f"(infrastructure: {infrastructure_namespace}, endpoint: {db_host})"
                )
                admin_username, admin_password = await self._get_infrastructure_superuser_credentials(
                    project_name, infrastructure_namespace
                )
            else:
                # Shared database configuration - use cluster-specific fully qualified hostname
                # for cross-namespace DNS resolution (user pods are in different namespaces)
                db_host = get_database_server(cluster_name)
                admin_username = settings.DATABASE_ADMIN_NAME
                admin_password = settings.DATABASE_ADMIN_PASSWORD
                logger.info(f"Using shared PostgreSQL for {project_name} (host: {db_host})")

            # Ensure we have a PostgreSQL connector for this deployment
            self._ensure_connection()

            # Get database service config (includes privileges if specified)
            service_config = self._get_database_service_config(project_data) if uses_dedicated_postgresql else {}
            database_privileges = service_config.get("privileges", [])

            if database_privileges:
                logger.info(f"Database privileges specified for {project_name}: {database_privileges}")

            # Get the generation from project file if set (for versioned databases after restore)
            generation = self._get_deployment_database_generation(project_data, deployment_name)
            if generation is not None and generation > 0:
                logger.info(f"Using database generation {generation} for {deployment_name}")

            # Generate consistent database identifiers
            # Username is never versioned (same user accesses all versions)
            # Database and schema use generation if set (for versioned restore)
            db_username = generate_database_name(project_name, deployment_name, None)  # Username never versioned
            db_database = generate_database_name(project_name, deployment_name, generation)
            db_schema = db_database  # Schema matches database name

            # PHASE 1: CREDENTIAL RESOLUTION - Determine working credentials
            logger.info(f"Phase 1: Resolving database credentials for {project_name}/{deployment_name}")
            db_password = await self._resolve_database_credentials(
                project_name,
                deployment_name,
                deployment,
                db_username,
                db_database,
                db_schema,
                db_host=db_host,
                admin_username=admin_username,
                admin_password=admin_password,
                database_privileges=database_privileges,
            )

            # PHASE 2: DATABASE STATE VERIFICATION - Ensure database exists with correct state
            logger.info(f"Phase 2: Verifying database state for {project_name}/{deployment_name}")
            db_state = await self._ensure_database_state(
                project_name,
                deployment_name,
                deployment,
                db_database,
                db_schema,
                db_username,
                db_password,
                project_data,
                force_clone,
                generation,
            )

            # Use the final identifiers from _ensure_database_state, which may differ
            # from the initial values if a generational failover occurred during clone.
            db_database = db_state.database
            db_schema = db_state.schema
            final_password = db_state.password

            # Extra schemas (RC-17), project-wide, all living in this same database so
            # generations/clones/backups keep working. Create them idempotently (a clone
            # already carries them; a fresh database gets them empty), owned by the main
            # user. The default schema stays first in every search_path.
            extra_schema_pairs = self._resolve_extra_schemas(project_data, project_name, deployment_name)
            for _postfix, extra_schema in extra_schema_pairs:
                await self.postgres_connector.create_schema(
                    schema_name=extra_schema, database=db_database, owner=db_username
                )
            all_schemas = [db_schema, *(name for _, name in extra_schema_pairs)]

            # Set the default search_path for the role scoped to this database.
            # This ensures applications that don't explicitly set a search_path
            # still resolve to the correct schema. Idempotent and safe on every sync.
            await self.postgres_connector.set_role_search_path(
                username=db_username,
                database=db_database,
                schemas=all_schemas,
            )

            # Ensure the persistent read-only role exists alongside the main user, with
            # SELECT on every schema so the read-only console can browse all of them.
            ro_username, ro_password = await self._ensure_readonly_user(
                deployment_name=deployment_name,
                deployment=deployment,
                main_username=db_username,
                database=db_database,
                schemas=all_schemas,
            )

            # PHASE 3: FINAL STATE STORAGE - Store working credentials with correct host
            logger.info(f"Phase 3: Storing final credentials for {project_name}/{deployment_name}")
            database_secret = DatabaseSecret(
                host=db_host,  # Use namespace-specific or shared host
                port=5432,  # Standard PostgreSQL port
                username=db_username,
                password=final_password,
                schema=db_schema,
                database=db_database,
                ro_username=ro_username,
                ro_password=ro_password,
                extra_schemas=extra_schema_pairs,
            )
            self.project_manager._add_secret_to_create(
                deployment_name,
                "database",
                database_secret,
            )

            logger.info(f"Database resources ready for {deployment_name} (host: {db_host}, stored in secrets map)")

        finally:
            if progress_manager and database_task:
                progress_manager.complete_task(database_task)

    async def _create_or_update_user(
        self, db_username: str, postgres_conn: PostgresConnector, database_privileges: list[str] | None = None
    ) -> tuple[str, dict[str, Any]]:
        """Create or update a database user using the bound connector.

        Args:
            db_username: Username to create or update
            postgres_conn: PostgresConnector instance (bound to server)
            database_privileges: List of PostgreSQL privileges to grant (e.g., ["SUPERUSER", "CREATEDB"])

        Returns:
            Tuple of (password, result_dict)
        """
        db_password = generate_secure_password(min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20)
        # Try to create the user first
        create_result = await postgres_conn.create_user(
            username=db_username,
            password=db_password,
            database_privileges=database_privileges,
        )

        if create_result["status"] == "exists":
            # User already exists, update password and privileges
            update_result = await postgres_conn.update_user_password(
                username=db_username,
                new_password=db_password,
            )
            # Also ensure privileges are applied (idempotent)
            if database_privileges:
                await postgres_conn.update_user_privileges(
                    username=db_username,
                    database_privileges=database_privileges,
                )
            return db_password, update_result
        else:
            # User was created (or error occurred)
            return db_password, create_result

    def _resolve_extra_schemas(
        self, project_data: dict[str, Any], project_name: str, deployment_name: str
    ) -> list[tuple[str, str]]:
        """The deployment's extra schemas as ``(postfix, full_schema_name)`` pairs.

        Schemas are project-wide (RC-17 decision 10.5), so the same postfixes apply to
        every deployment; the full name is per-deployment
        (``{project}_{deployment}_{postfix}``). Schemas marked for deletion are excluded
        so provisioning stops managing them while leaving the schema in place.
        """
        return [
            (entry["postfix"], generate_extra_database_schema(project_name, deployment_name, entry["postfix"]))
            for entry in get_postgres_schemas(project_data)
        ]

    async def _ensure_readonly_user(
        self,
        deployment_name: str,
        deployment: dict[str, Any],
        main_username: str,
        database: str,
        schemas: list[str],
    ) -> tuple[str, str]:
        """Ensure a persistent read-only role exists for the deployment's database.

        The role mirrors the main user's lifecycle: created alongside it, granted
        SELECT-only on every schema of the deployment (the default schema plus any
        extra schemas, RC-17 decision 10.4), with a default search_path over the same
        list. The read-only database console (and read-only application use) connect as
        this role, so writes are impossible server-side and the console can browse every
        schema. Its password is kept stable across syncs by reusing the value already
        stored in the deployment's database secret.

        Args:
            schemas: The deployment's schemas, primary first; must be non-empty.

        Returns:
            Tuple of (ro_username, ro_password).
        """
        ro_username = f"{main_username}_ro"

        # Reuse the stored read-only password to avoid churning the secret (and
        # any consumer of it) on every reprocess; generate one only if absent.
        existing = await self._get_existing_database_credentials_from_k8s(deployment_name, deployment)
        ro_password = existing.ro_password if existing and existing.ro_password else ""
        if not ro_password:
            ro_password = generate_secure_password(min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20)

        create_result = await self.postgres_connector.create_user(username=ro_username, password=ro_password)
        if create_result["status"] == "exists":
            await self.postgres_connector.update_user_password(username=ro_username, new_password=ro_password)

        for schema in schemas:
            await self.postgres_connector.grant_readonly_on_schema(database, schema, ro_username)
        await self.postgres_connector.set_role_search_path(username=ro_username, database=database, schemas=schemas)

        return ro_username, ro_password

    async def _resolve_database_credentials(
        self,
        project_name: str,
        deployment_name: str,
        deployment: dict[str, Any],
        db_username: str,
        db_database: str,
        db_schema: str,
        db_host: str,
        admin_username: str,
        admin_password: str,
        database_privileges: list[str] | None = None,
    ) -> str:
        """
        Resolve working database credentials through the following steps:
        1. Check for existing credentials in Kubernetes
        2. Test credential validity if they exist
        3. Create or update credentials if needed

        Args:
            project_name: Name of the project
            deployment_name: Name of the deployment
            deployment: Deployment configuration dict
            db_username: Database username to create/verify
            db_database: Database name
            db_schema: Schema name
            db_host: Database host (shared or namespace-specific service endpoint)
            admin_username: Admin username for database operations
            admin_password: Admin password for database operations
            database_privileges: List of PostgreSQL privileges to grant to created user

        Returns:
            The working password for the database user
        """

        logger.info(f"Checking database credentials for {project_name}/{deployment_name} at host {db_host}")

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
            credentials_valid = await self._test_database_connection(
                username=db_username, password=db_password, database=db_database, schema=db_schema, host=db_host
            )

            if credentials_valid:
                logger.info(f"Existing database credentials are valid for {project_name}/{deployment_name}")
                # Ensure privileges are up-to-date (idempotent)
                if database_privileges:
                    logger.info(f"Ensuring database privileges for {db_username}: {database_privileges}")
                    await self.postgres_connector.update_user_privileges(
                        username=db_username,
                        database_privileges=database_privileges,
                    )
                return db_password
            else:
                # Step 3a: Handle invalid existing credentials
                if settings.RECREATE_PASSWORD_ON_AUTHENTICATION_FAILURE:
                    logger.warning(
                        f"Database credentials are invalid for {project_name}/{deployment_name}, updating password"
                    )
                    new_password, update_result = await self._create_or_update_user(
                        db_username=db_username,
                        postgres_conn=self.postgres_connector,
                        database_privileges=database_privileges,
                    )
                    if update_result["status"] not in ["updated", "created", "success"]:
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
            db_password, create_result = await self._create_or_update_user(
                db_username=db_username,
                postgres_conn=self.postgres_connector,
                database_privileges=database_privileges,
            )

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
        project_data: dict[str, Any] | None = None,
        force_clone_override: bool = False,
        generation: int | None = None,
    ) -> DatabaseStateResult:
        """
        Ensure the database exists in the correct state, handling clone-from and force-clone logic.
        This runs regardless of whether credentials existed initially.

        Uses the DatabaseManager's bound credentials for all operations.
        Handles both local deployment clones and remote-source clones with Chisel tunnels.

        Clone modes:
        - mode: "once" (default): Clone only if status.completed is not True
        - mode: "always": Clone every sync, implies force_clone=True

        After successful clone, updates the clone-from.status in project_data and saves the project file.

        Args:
            project_name: Name of the project
            deployment_name: Name of the deployment
            deployment: Deployment configuration dict
            db_database: Database name to create
            db_schema: Schema name to create
            db_username: Owner username for the database
            db_password: Password for the owner
            project_data: Project configuration data (needed for remote-source clones)
            force_clone_override: Runtime override for force_clone (from API)

        Returns:
            DatabaseStateResult with the final database, schema, and password.
            These may differ from the input values if a generational failover occurred.
        """
        clone_from = deployment.get("clone-from")
        # Use runtime override if True, otherwise fall back to deployment config
        force_clone = force_clone_override or deployment.get("force-clone", False)

        # clone-from must be a dict with type/reference/mode keys
        if clone_from and not isinstance(clone_from, dict):
            raise ValueError(
                f"clone-from for deployment '{deployment_name}' must be a dict with 'type', 'reference', "
                f"and 'mode' keys, got: {type(clone_from).__name__} = {clone_from!r}"
            )

        # Handle clone mode and status checking
        if clone_from:
            clone_mode = clone_from.get("mode", "once")
            status = clone_from.get("status", {})
            clone_status_completed = status.get("completed", False) if isinstance(status, dict) else False

            # mode: "always" implies force_clone=True
            if clone_mode == "always":
                force_clone = True
                logger.info(f"Clone mode 'always' for {deployment_name}, forcing clone")

            # mode: "once" with completed status - skip clone entirely (unless force_clone is True)
            if clone_mode == "once" and clone_status_completed and not force_clone:
                logger.info(
                    f"Clone mode 'once' for {deployment_name} and clone already completed "
                    f"(timestamp: {status.get('timestamp', 'unknown')}), skipping clone"
                )
                # Still need to ensure database exists (in case cluster was rebuilt)
                # but skip the clone operation
                clone_from = None  # Set to None to skip clone logic but continue to normal flow
            elif clone_mode == "once" and clone_status_completed and force_clone:
                logger.info(f"Clone mode 'once' for {deployment_name} but force_clone=True, proceeding with clone")

        # Handle clone-from by type
        clone_source_ref: str | None = None
        if clone_from:
            clone_type = clone_from.get("type")
            if clone_type == CloneFromType.REMOTE_SOURCE.value:
                # Handle remote source cloning directly
                remote_source_name = clone_from.get("reference")
                if not remote_source_name:
                    raise ValueError(f"remote-source clone without a reference: {deployment.get('name')}")
                if project_data is None:
                    raise ValueError(f"project_data is required for remote-source clone: {deployment.get('name')}")

                remote_source = self._get_remote_source_config(project_data, remote_source_name)
                if not remote_source:
                    raise ValueError(f"Remote source '{remote_source_name}' not found in project configuration")

                db_config = remote_source.get("services", {}).get("postgresql-database", {})
                if not db_config:
                    logger.debug(
                        f"No postgresql-database service in remote source '{remote_source_name}', "
                        "skipping database clone"
                    )
                    return DatabaseStateResult(database=db_database, schema=db_schema, password=db_password)

                chisel_config = remote_source.get("chisel")

                # Decrypt source password using project's AGE key
                from opi.utils.age import decrypt_password_smart, get_decoded_project_private_key

                project_private_key = await get_decoded_project_private_key(project_data)
                source_password = await decrypt_password_smart(db_config.get("password", ""), project_private_key)

                logger.info(
                    f"Cloning database from remote source '{remote_source_name}' for deployment {deployment_name}"
                )

                result = await self.clone_database_from_external_source(
                    project_name=project_name,
                    deployment_name=deployment_name,
                    source_host=db_config["host"],
                    source_port=db_config.get("port", 5432),
                    source_username=db_config["username"],
                    source_password=source_password,
                    source_database=db_config["database"],
                    source_schema=db_config["schema"],
                    force_clone=force_clone,
                    chisel_config=chisel_config,
                    project_data=project_data,
                )

                if not result.get("success"):
                    raise RuntimeError(f"Remote source clone failed: {result.get('errors', ['Unknown error'])}")

                # Report clone to project_manager for status tracking
                self.project_manager.report_clone_performed(
                    deployment_name, ServiceType.POSTGRESQL_DATABASE.value, generation
                )

                # Return the final identifiers from the clone operation
                target = result.get("target", {})
                return DatabaseStateResult(
                    database=target.get("database", db_database),
                    schema=target.get("schema", db_schema),
                    password=result.get("resolved_password", db_password),
                )
            elif clone_type == CloneFromType.DEPLOYMENT.value:
                # Local deployment clone - extract reference name
                clone_source_ref = clone_from.get("reference")
            elif clone_type == CloneFromType.BACKUP.value:
                # Backup restore: restore database from Kopia backup
                backup_items = clone_from.get("backup_items", [])
                db_item = next(
                    (item for item in backup_items if item.get("resource_type") == "database"),
                    None,
                )
                if db_item:
                    logger.info(
                        f"Clone-from type 'backup' for deployment '{deployment_name}': "
                        f"restoring database from snapshot {db_item.get('snapshot_id', '?')}"
                    )
                    from opi.core.cluster_config import get_prefixed_namespace
                    from opi.core.config import settings
                    from opi.manager.backup.database_backup import create_database_backup_manager

                    # Ensure the target database exists before restoring into it
                    self._ensure_connection()
                    await self.postgres_connector.create_database(
                        database_name=db_database,
                        owner=db_username,
                    )
                    await self.postgres_connector.create_schema(
                        schema_name=db_schema,
                        database=db_database,
                        owner=db_username,
                    )
                    logger.info(f"Created database {db_database} for backup restore")

                    db_backup_manager = create_database_backup_manager()
                    dep_cluster = deployment.get("cluster", settings.CLUSTER_MANAGER)
                    raw_ns = deployment.get("namespace", project_name)
                    app_namespace = get_prefixed_namespace(dep_cluster, raw_ns)

                    # The restore pod runs in the project namespace, so the
                    # database host must be a FQDN (not a short service name).
                    from opi.utils.naming import ensure_fqdn

                    restore_db_host = ensure_fqdn(self._db_host)
                    restore_db_port = 5432
                    if ":" in restore_db_host:
                        restore_db_host, port_str = restore_db_host.rsplit(":", 1)
                        restore_db_port = int(port_str) if port_str.isdigit() else 5432

                    result = await db_backup_manager.restore_database(
                        cluster=dep_cluster,
                        namespace=app_namespace,
                        reference_name=db_item.get("reference_name", ""),
                        target_database_host=restore_db_host,
                        target_database_port=restore_db_port,
                        target_database_name=db_database,
                        target_database_user=db_username,
                        target_database_password=db_password,
                        snapshot_id=db_item.get("snapshot_id"),
                        project_name=project_name,
                    )
                    if result.success:
                        logger.info(f"Database restored from backup for {deployment_name}")
                        self.project_manager.report_clone_performed(
                            deployment_name, ServiceType.POSTGRESQL_DATABASE.value, generation
                        )
                    else:
                        raise RuntimeError(f"Database restore from backup failed: {result.error}")
                else:
                    logger.info(
                        f"Clone-from type 'backup' for deployment '{deployment_name}': "
                        "no database backup item found, skipping"
                    )
                clone_from = None
            else:
                raise ValueError(f"Unknown clone-from type '{clone_type}' for deployment '{deployment_name}'")

        if clone_source_ref:
            # Handle local database cloning (type: deployment)
            # Source uses base name (None generation) - versioned sources would need explicit handling
            source_database = generate_database_name(project_name, clone_source_ref, None)
            source_schema = source_database  # Schema matches database name
            # Extra schemas (RC-17) live in the same database and must come along, or a
            # clone would lose their data. Their names embed the deployment, so the
            # source uses the source deployment and the target uses this deployment.
            if project_data is None:
                msg = (
                    f"project_data is required to clone '{deployment_name}': without it the extra schemas "
                    "cannot be resolved, and a clone that silently drops them loses their data"
                )
                raise ValueError(msg)
            extra_clone_pairs = [
                (
                    generate_extra_database_schema(project_name, clone_source_ref, postfix),
                    generate_extra_database_schema(project_name, deployment_name, postfix),
                )
                for postfix, _ in self._resolve_extra_schemas(project_data, project_name, deployment_name)
            ]
            logger.info(f"Clone requested from {source_database} to {db_database} (force={force_clone})")

            # Determine service type from project configuration
            service_type = (
                ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value
                if project_data and self._project_uses_namespace_postgresql(project_data)
                else ServiceType.POSTGRESQL_DATABASE.value
            )

            # STEP 1: Validate that source database and schema exist before cloning
            await self._validate_clone_source(source_database, source_schema)

            # Check if target database exists by attempting to create it
            database_result = await self.postgres_connector.create_database(
                database_name=db_database,
                owner=db_username,
            )
            target_db_exists = database_result["status"] == "exists"

            # Track whether we created the database in this operation (for cleanup on failure)
            database_created_here = not target_db_exists

            # Track the final generation used for this clone
            final_generation = generation

            if target_db_exists and (force_clone or generation is None):
                # Generational failover: create a new versioned database instead of
                # cloning into an existing (potentially broken/limbo) one.
                # Triggers when:
                # - force_clone=True: explicit user request
                # - generation is None: database exists but no generation was ever recorded,
                #   indicating a previous clone failed before recording its generation
                new_generation = (generation or 0) + 1

                # Find an available generation - previous failed attempts may have left
                # orphaned versioned databases that were not cleaned up
                max_attempts = 5
                for _attempt in range(max_attempts):
                    db_database = generate_database_name(project_name, deployment_name, new_generation)
                    db_schema = db_database

                    logger.info(
                        f"Generational failover: creating database {db_database} "
                        f"(generation {generation} -> {new_generation}, "
                        f"force_clone={force_clone})"
                    )

                    database_result = await self.postgres_connector.create_database(
                        database_name=db_database,
                        owner=db_username,
                    )
                    if database_result["status"] == "created":
                        break

                    # Versioned database already exists. This is usually a prior
                    # successful generation, not a failure; advance to the next free one.
                    logger.info(
                        f"Database {db_database} already exists (prior generation), "
                        f"trying generation {new_generation + 1}"
                    )
                    new_generation += 1
                else:
                    raise Exception(
                        f"Could not find an available generation after {max_attempts} attempts "
                        f"(tried up to generation {new_generation}). Manual cleanup required."
                    )

                logger.info(f"Created new versioned database: {db_database}")
                final_generation = new_generation
                database_created_here = True
            elif database_result["status"] == "created":
                logger.info(f"Created database for cloning: {db_database}")
            else:
                logger.info(f"Database already exists for cloning: {db_database}")

            # Always execute postInitSQL before clone (idempotent - uses IF NOT EXISTS)
            # Extensions must exist before schema clone to avoid dependency issues
            if project_data:
                service_config = self._get_database_service_config(project_data)
                post_init_sql = service_config.get("postInitSQL", [])
                if post_init_sql:
                    logger.info(f"Ensuring {len(post_init_sql)} postInitSQL statement(s) on {db_database}")
                    init_result = await self.postgres_connector.execute_init_sql(
                        database_name=db_database,
                        sql_statements=post_init_sql,
                    )
                    if init_result["status"] == "success":
                        logger.info(f"PostInitSQL executed successfully on {db_database}")
                    else:
                        logger.warning(f"PostInitSQL execution issue: {init_result}")

            # STEP 2: Perform the clone operation
            # If clone fails and we created the database in this operation, clean it up
            # to prevent a limbo state with a partially-created database
            try:
                clone_result = await self.postgres_connector.clone_schema(
                    source_database=source_database,
                    target_database=db_database,
                    source_schema=source_schema,
                    target_schema=db_schema,
                    target_owner=db_username,
                    target_owner_password=db_password,
                    additional_schemas=extra_clone_pairs,
                )

                if clone_result["status"] != "success":
                    raise RuntimeError(f"Failed to clone database: {clone_result.get('message', 'Unknown error')}")
            except Exception:
                if database_created_here:
                    logger.warning(
                        f"Clone failed for {db_database}, cleaning up freshly created database to prevent limbo state"
                    )
                    try:
                        await self.postgres_connector.delete_database(db_database)
                        logger.info(f"Cleaned up database {db_database} after failed clone")
                    except Exception as cleanup_err:
                        logger.error(f"Failed to clean up database {db_database} after failed clone: {cleanup_err}")
                raise

            logger.info(f"Successfully cloned database from {source_database} to {db_database}")
            logger.info(f"Schema cloned with target name '{db_schema}' - no additional rename needed")

            # Record revision in project file (handles both generation and revision tracking)
            if project_data:
                self.project_manager._revision_manager.record_clone(
                    project_data=project_data,
                    deployment_name=deployment_name,
                    service_type=service_type,
                    generation=final_generation,
                    resource_name=db_database,
                    source=f"deployment:{clone_source_ref}",
                )

            # Report clone to project_manager for status tracking
            self.project_manager.report_clone_performed(deployment_name, service_type, final_generation)
        else:
            # Normal flow: ensure database and schema exist
            database_result = await self.postgres_connector.create_database(
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

            # Always execute postInitSQL if configured (idempotent - uses IF NOT EXISTS)
            # This ensures extensions are present even on existing databases during refresh
            if project_data:
                service_config = self._get_database_service_config(project_data)
                post_init_sql = service_config.get("postInitSQL", [])
                if post_init_sql:
                    logger.info(f"Ensuring {len(post_init_sql)} postInitSQL statement(s) on {db_database}")
                    init_result = await self.postgres_connector.execute_init_sql(
                        database_name=db_database,
                        sql_statements=post_init_sql,
                    )
                    if init_result["status"] == "success":
                        logger.info(f"PostInitSQL executed successfully on {db_database}")
                    else:
                        logger.warning(f"PostInitSQL execution issue: {init_result}")

            # Create or verify schema exists
            schema_result = await self.postgres_connector.create_schema(
                schema_name=db_schema,
                database=db_database,
                owner=db_username,
            )

            if schema_result["status"] not in ["created", "exists"]:
                raise Exception(
                    f"Failed to create schema {db_schema} in database {db_database}: {schema_result.get('message', 'Unknown error')}"
                )

            if schema_result["status"] == "created":
                logger.info(f"Created database schema: {db_schema}")
            else:
                logger.info(f"Database schema already exists: {db_schema}")

        return DatabaseStateResult(database=db_database, schema=db_schema, password=db_password)

    async def _validate_clone_source(self, source_database: str, source_schema: str) -> None:
        """
        Validate that the source database and schema exist before attempting to clone.

        Uses the DatabaseManager's bound connector for validation.

        Args:
            source_database: Name of the source database
            source_schema: Name of the source schema that should exist

        Raises:
            Exception: If source database or schema doesn't exist
        """
        logger.info(f"Validating clone source: database={source_database}, schema={source_schema}")

        # Check if source database exists using postgres database connection
        conn = await self.postgres_connector._get_or_create_connection("postgres")
        source_db_exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", source_database)

        if not source_db_exists:
            raise Exception(
                f"Cannot clone from {source_database}: source database does not exist. "
                f"Ensure the source deployment has been created successfully first."
            )

        # Connect to source database to check schema
        conn = await self.postgres_connector._get_or_create_connection(source_database)

        # Check if source schema exists
        schema_exists = await conn.fetchval(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = $1", source_schema
        )

        if not schema_exists:
            raise Exception(
                f"Cannot clone from {source_database}: source schema '{source_schema}' does not exist. "
                f"The source deployment appears to be empty or incomplete. "
                f"Ensure the source deployment has been properly initialized with data."
            )

        # Check if source schema has any tables (optional but recommended)
        table_count = await conn.fetchval(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = $1", source_schema
        )

        if table_count == 0:
            logger.warning(
                f"Source schema '{source_schema}' exists but contains no tables. "
                f"Cloning will result in an empty schema."
            )
        else:
            logger.info(f"Source schema '{source_schema}' validated successfully with {table_count} tables")

    async def _get_database_config_for_deployment(
        self, project_data: dict[str, Any], deployment: dict[str, Any]
    ) -> tuple[str, str, str]:
        """
        Get database configuration (host, admin username, admin password) for a deployment.

        This method determines whether the project uses namespace-specific or shared PostgreSQL
        and returns the appropriate credentials.

        Args:
            project_data: The project configuration data
            deployment: The deployment configuration

        Returns:
            Tuple of (db_host, admin_username, admin_password)
        """
        project_name = await self.project_manager.get_name()
        cluster_name = deployment.get("cluster")

        if not cluster_name:
            raise ValueError(f"Deployment {deployment['name']} is missing required 'cluster' field")

        # Determine placement: a dedicated (project-scoped) cluster or the shared instance.
        uses_dedicated_postgresql = project_uses_dedicated_postgres(project_data)

        if uses_dedicated_postgresql:
            # Dedicated-cluster configuration
            cluster_config = self._get_database_cluster_config(project_data, cluster_name)
            db_host = cluster_config["service_endpoint"]
            infrastructure_namespace = cluster_config["infrastructure_namespace"]

            # Get superuser credentials from infrastructure namespace
            logger.info(
                f"Using dedicated PostgreSQL cluster for {project_name} "
                f"(infrastructure: {infrastructure_namespace}, endpoint: {db_host})"
            )
            admin_username, admin_password = await self._get_infrastructure_superuser_credentials(
                project_name, infrastructure_namespace
            )
        else:
            # Shared database configuration - use cluster-specific fully qualified hostname
            # for cross-namespace DNS resolution (user pods are in different namespaces)
            db_host = get_database_server(cluster_name)
            admin_username = settings.DATABASE_ADMIN_NAME
            admin_password = settings.DATABASE_ADMIN_PASSWORD
            logger.info(f"Using shared PostgreSQL for {project_name} (host: {db_host})")

        return db_host, admin_username, admin_password

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
        project_name = await self.project_manager.get_name()
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
            # Ensure we have a PostgreSQL connector for this deployment
            self._ensure_connection()

            db_username = generate_database_name(project_name, deployment_name, None)

            # Build list of all databases to delete: base name + all generational versions.
            # Check both the YAML generation and discover versions from PostgreSQL
            # to handle cases where the YAML is out of sync (e.g. partial delete).
            base_db_name = generate_database_name(project_name, deployment_name, None)
            databases_to_delete = [base_db_name]
            current_generation = self._get_deployment_database_generation(project_data, deployment_name)
            max_gen = current_generation or 0
            # Also discover versions that may exist beyond what the YAML records
            for gen in range(1, max_gen + 10):
                versioned_name = generate_database_name(project_name, deployment_name, gen)
                if versioned_name not in databases_to_delete:
                    databases_to_delete.append(versioned_name)
            if len(databases_to_delete) > 1:
                logger.info(f"Will attempt to delete {len(databases_to_delete)} database(s): {databases_to_delete}")

            # Delete all databases (base + generational versions)
            for db_database in databases_to_delete:
                try:
                    database_result = await self.postgres_connector.delete_database(
                        database_name=db_database,
                    )

                    if database_result["status"] in ["success", "deleted"]:
                        deletion_results["operations"].append(
                            {"type": "database_deletion", "target": db_database, "status": "success"}
                        )
                        logger.info(f"Successfully deleted database (with all schemas): {db_database}")
                    elif database_result["status"] == "not_found" or "does not exist" in database_result.get(
                        "message", ""
                    ):
                        deletion_results["operations"].append(
                            {"type": "database_deletion", "target": db_database, "status": "not_found"}
                        )
                        logger.debug(f"Database {db_database} does not exist, skipping")
                    else:
                        deletion_results["operations"].append(
                            {
                                "type": "database_deletion",
                                "target": db_database,
                                "status": "failed",
                                "error": database_result.get("message", "Unknown error"),
                            }
                        )
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
                    username=db_username,
                )

                if update_result["status"] in ["success", "deleted"]:
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

            # Delete the read-only companion role (created alongside the main user).
            ro_username = f"{db_username}_ro"
            try:
                ro_result = await self.postgres_connector.delete_user(username=ro_username)
                if ro_result["status"] in ["success", "deleted"]:
                    deletion_results["operations"].append(
                        {"type": "database_user_deletion", "target": ro_username, "status": "success"}
                    )
                    logger.info(f"Successfully deleted read-only database user: {ro_username}")
                else:
                    not_found = "does not exist" in ro_result.get("message", "")
                    deletion_results["operations"].append(
                        {
                            "type": "database_user_deletion",
                            "target": ro_username,
                            "status": "not_found" if not_found else "failed",
                            "error": ro_result.get("message", "Unknown error"),
                        }
                    )
                    if not not_found:
                        deletion_results["errors"].append(
                            f"Failed to delete user {ro_username}: {ro_result.get('message')}"
                        )
            except Exception as e:
                deletion_results["operations"].append(
                    {"type": "database_user_deletion", "target": ro_username, "status": "error", "error": str(e)}
                )
                deletion_results["errors"].append(f"Error deleting user {ro_username}: {e}")
                logger.exception(f"Error deleting read-only database user {ro_username}: {e}")

        except Exception as e:
            deletion_results["success"] = False
            deletion_results["errors"].append(f"Failed to connect to database: {e}")
            logger.exception(f"Error connecting to database for cleanup: {e}")

        # Update success status based on errors
        deletion_results["success"] = len(deletion_results["errors"]) == 0

        return deletion_results

    async def handle_service_removal(
        self,
        project_name: str,
        deployment_name: str,
        deployment_data: dict[str, Any],
        project_data: dict[str, Any],
        marked_for_deletion_service: MarkedForDeletionService | None = None,
    ) -> dict[str, Any]:
        """Handle cleanup when PostgreSQL service is removed from a deployment.

        If a ``MarkedForDeletionService`` is provided the database and user are
        marked for deferred deletion (allowing recovery).  Otherwise they are
        deleted immediately via ``delete_resources_for_deployment``.

        Args:
            project_name: Name of the project.
            deployment_name: Name of the deployment losing the service.
            deployment_data: The deployment dict from the *previous* YAML.
            project_data: The *previous* project YAML (so internal service
                checks still pass).
            marked_for_deletion_service: Optional service for deferred deletion.

        Returns:
            Structured result dict with operations, errors, success.
        """
        cluster = deployment_data.get("cluster", "")

        result: dict[str, Any] = {
            "service": "database",
            "deployment": deployment_name,
            "trigger": "service_removal",
            "operations": [],
            "success": True,
            "errors": [],
        }

        if marked_for_deletion_service is not None:
            db_name = generate_database_name(project_name, deployment_name)
            for rtype, rname in [
                ("postgresql_database", db_name),
                ("postgresql_user", db_name),
            ]:
                await marked_for_deletion_service.mark_resource(
                    resource_type=rtype,
                    resource_name=rname,
                    project_name=project_name,
                    deployment_name=deployment_name,
                    cluster=cluster,
                    metadata={"server": settings.DATABASE_HOST},
                )
            result["operations"].append(
                {
                    "type": "mark_for_deletion",
                    "resource_type": "postgresql_database",
                    "resource_name": db_name,
                    "status": "marked",
                }
            )
            logger.info(
                "Marked database resources for deferred deletion: %s (project=%s, deployment=%s)",
                db_name,
                project_name,
                deployment_name,
            )
        else:
            result = await self.delete_resources_for_deployment(project_data, deployment_data)
            result["trigger"] = "service_removal"

        return result

    def _project_uses_namespace_postgresql(self, project_data: dict[str, Any]) -> bool:
        """
        Check if project uses namespace-specific PostgreSQL database service.

        This checks if the project-level services configuration includes
        NAMESPACE_POSTGRESQL_DATABASE, which requires a dedicated database
        cluster in the project's infrastructure namespace.

        Args:
            project_data: The project configuration data

        Returns:
            True if project uses namespace-specific PostgreSQL, False otherwise
        """
        # Form-agnostic (bare string / legacy name-as-key / new {name, config} record).
        return Project(project_data).uses_service(ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value)

    def _get_database_service_config(self, project_data: dict[str, Any]) -> dict[str, Any]:
        """
        Extract and validate database configuration from project-level services.

        Service configuration is defined at project level in the 'services:' section:
        ```yaml
        services:
          - namespace-postgresql-database:
              config:
                image: ghcr.io/cloudnative-pg/postgresql:17
                instances: 2
                storage: 20Gi
                privileges:
                  - SUPERUSER
                  - CREATEDB
        ```

        Configuration is merged with hardcoded defaults. All fields are required in final config.

        Required config keys:
        - image: PostgreSQL CNPG image (e.g., "ghcr.io/cloudnative-pg/postgresql:17")
        - instances: Number of database instances (e.g., 1, 2, 3)
        - storage: Storage size (e.g., "10Gi", "20Gi")
        - resources: Resource limits and requests (nested dict)

        Optional config keys:
        - privileges: List of PostgreSQL privileges for created users
          (e.g., ["SUPERUSER"], ["CREATEDB", "CREATEROLE"])
        - postInitSQL: List of SQL statements to run after database initialization
          (e.g., ["CREATE EXTENSION IF NOT EXISTS unaccent;"])
        - registry: Name of the registry to use for pulling PostgreSQL image
          (e.g., "github-packages", "docker-hub")

        Args:
            project_data: The project configuration data

        Returns:
            Dictionary with complete database configuration

        Raises:
            ValueError: If service configuration is invalid or missing required fields
        """
        # RC-5 Phase 2: defaults + validation live in the typed config model,
        # replacing the previous hand-rolled DEFAULT_CONFIG merge and manual
        # field/privilege checks. RC-17: the dedicated-cluster config can come from
        # either service (namespace-postgresql-database, or postgresql-database with
        # scope: project); get_dedicated_postgres_config reads whichever is present.
        # For a shared database (no dedicated service) there is no CNPG cluster, so we
        # return the model defaults -- same result the old namespace read produced when
        # the service was absent, so postInitSQL/etc. stay empty.
        if project_uses_dedicated_postgres(project_data):
            merged_config = get_dedicated_postgres_config(project_data)
        else:
            merged_config = DedicatedPostgresFields().model_dump(mode="json")
        logger.debug(
            f"Database config (validated via provider): instances={merged_config.get('instances')}, "
            f"storage={merged_config.get('storage')}, image={merged_config.get('image')}, "
            f"privileges={len(merged_config.get('privileges') or [])}, "
            f"postInitSQL={len(merged_config.get('postInitSQL') or [])}"
        )
        return merged_config

    def _get_database_cluster_config(self, project_data: dict[str, Any], cluster_name: str) -> dict[str, Any]:
        """
        Build complete database cluster configuration combining service config and cluster settings.

        This method merges user-specified database configuration with cluster-specific
        settings to create a complete configuration dict for:
        - PostgreSQL cluster template rendering
        - Database secret creation with correct service endpoints
        - Infrastructure resource creation

        Args:
            project_data: The project configuration data
            cluster_name: Name of the cluster (e.g., "local", "odcn-production")

        Returns:
            Dictionary with complete database cluster configuration containing:
            - database_config: User-specified config (image, instances, storage)
            - infrastructure_namespace: Cluster-aware infrastructure namespace
            - service_endpoint: Full qualified database service DNS name
            - storage_class: Cluster-specific storage class
            - cluster_name: Name of the cluster
            - database_operator_namespace: Namespace of the CNPG operator

        Example:
            {
                "database_config": {"image": "ghcr.io/cloudnative-pg/postgresql:17", "instances": 2, "storage": "20Gi"},
                "infrastructure_namespace": "rig-myproject-infrastructure",
                "service_endpoint": "myproject-db-rw.rig-myproject-infrastructure.svc.cluster.local",
                "storage_class": "standard",
                "cluster_name": "local"
            }
        """
        from opi.core.cluster_config import (
            get_database_cluster_service_endpoint,
            get_database_operator_namespace,
            get_infrastructure_namespace,
            get_storage_class_name,
        )

        project_name = project_data.get("name")
        if not project_name:
            raise ValueError("Project name is required in project_data")

        # Get user-specified database configuration
        database_config = self._get_database_service_config(project_data)

        # Get cluster-specific settings
        infrastructure_namespace = get_infrastructure_namespace(cluster_name, project_name)
        service_endpoint = get_database_cluster_service_endpoint(cluster_name, project_name)
        storage_class = get_storage_class_name(cluster_name)

        # Build complete configuration
        cluster_config = {
            "database_config": database_config,
            "infrastructure_namespace": infrastructure_namespace,
            "service_endpoint": service_endpoint,
            "storage_class": storage_class,
            "cluster_name": cluster_name,
            # The DB subsystem owns this: the CNPG operator's namespace must be
            # allowed into the infra namespace so it can extract instance status,
            # or the dedicated Cluster never becomes Ready.
            "database_operator_namespace": get_database_operator_namespace(cluster_name),
        }

        logger.debug(
            f"Built database cluster config for {project_name} in {cluster_name}: "
            f"namespace={infrastructure_namespace}, endpoint={service_endpoint}, storage_class={storage_class}"
        )
        return cluster_config

    async def _get_infrastructure_superuser_credentials(
        self, project_name: str, infrastructure_namespace: str
    ) -> tuple[str, str]:
        """
        Get superuser credentials from infrastructure namespace.

        These credentials are stored in the infrastructure namespace and used by
        the CloudNativePG operator to bootstrap the database cluster. Applications
        typically don't use these credentials directly - they use deployment-specific
        credentials created by the database manager.

        Args:
            project_name: Name of the project
            infrastructure_namespace: Infrastructure namespace where credentials are stored

        Returns:
            Tuple of (username, password) for the superuser account

        Raises:
            RuntimeError: If secret is not found or is invalid

        Example:
            username, password = await db_manager._get_infrastructure_superuser_credentials(
                "myproject", "rig-myproject-infrastructure"
            )
        """
        from opi.utils.naming import _sanitize_for_lowercase

        # Secret name follows pattern: {project}-postgres-superuser
        project_clean = _sanitize_for_lowercase(project_name)
        secret_name = f"{project_clean}-postgres-superuser"

        logger.debug(f"Retrieving superuser credentials from {infrastructure_namespace}/{secret_name}")

        try:
            # Get the secret from Kubernetes
            kubectl_connector = self.project_manager._kubectl_connector
            secret_data = await kubectl_connector.get_secret(secret_name, infrastructure_namespace)

            if not secret_data:
                raise RuntimeError(
                    f"Superuser secret '{secret_name}' not found in namespace '{infrastructure_namespace}'. "
                    f"Infrastructure resources may not be deployed yet."
                )

            # Extract credentials from secret data
            username = secret_data.get("username")
            password = secret_data.get("password")

            if not username or not password:
                raise RuntimeError(
                    f"Superuser secret '{secret_name}' is missing username or password fields. "
                    f"Secret may be corrupted or incorrectly formatted."
                )

            logger.debug(f"Successfully retrieved superuser credentials for {project_name}")
            return username, password

        except Exception as e:
            logger.error(f"Failed to retrieve superuser credentials for {project_name}: {e}")
            raise RuntimeError(
                f"Cannot retrieve superuser credentials from {infrastructure_namespace}/{secret_name}: {e}"
            ) from e

    async def _deployment_uses_postgresql(self, project_data: dict[str, Any], deployment_name: str) -> bool:
        """
        Check if a deployment uses PostgreSQL service.

        Args:
            project_data: The project configuration data
            deployment_name: Name of the deployment to check

        Returns:
            True if deployment uses PostgreSQL service, False otherwise
        """
        return self.project_manager._project_file_handler.deployment_uses_service(
            project_data,
            deployment_name,
            [ServiceType.POSTGRESQL_DATABASE.value, ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value],
        )

    def _get_remote_source_config(self, project_data: dict[str, Any], remote_source_name: str) -> dict[str, Any] | None:
        """
        Get remote source configuration by name from project data.

        Args:
            project_data: Project configuration data
            remote_source_name: Name of the remote source to find

        Returns:
            Remote source configuration dict or None if not found
        """
        remote_sources = project_data.get("remote-sources", [])
        for source in remote_sources:
            if source.get("name") == remote_source_name:
                return source
        return None

    async def _get_existing_database_credentials_from_k8s(
        self, deployment_name: str, deployment: dict[str, Any]
    ) -> DatabaseSecret | None:
        """
        Get existing database credentials from Kubernetes secret.

        Args:
            deployment_name: Name of the deployment
            deployment: The deployment configuration containing namespace info

        Returns:
            DatabaseSecret if the secret exists, None if it is absent. Re-raises on a
            retrieval error (it does not swallow the failure into a None result).
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
            raise

    @staticmethod
    async def _test_database_connection(
        username: str,
        password: str,
        database: str,
        schema: str,
        host: str,
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
            host: Database host (shared or namespace-specific service endpoint)

        Returns:
            True if connection successful, False otherwise
        """
        import asyncpg

        try:
            # Create a direct connection with the user's credentials
            conn = await asyncpg.connect(host=host, port=5432, user=username, password=password, database=database)
            await conn.close()
            logger.debug(f"Connection test successful for {username}@{host}/{database}")
            return True
        except Exception as e:
            logger.debug(f"Database connection test failed for user {username}: {e}")
            return False

    @staticmethod
    async def _validate_external_source(
        source_host: str,
        source_port: int,
        source_username: str,
        source_password: str,
        source_database: str,
        source_schema: str,
    ) -> dict[str, Any]:
        """
        Validate external source database connectivity and schema existence.

        Single responsibility: Connect to external source and verify schema exists.

        Args:
            source_host: External source database host
            source_port: External source database port
            source_username: Username for external source connection
            source_password: Password for external source connection
            source_database: Source database name
            source_schema: Source schema name

        Returns:
            Dictionary with validation results:
                - table_count: int - Number of tables in source schema

        Raises:
            Exception: If connection or validation fails
        """
        import asyncpg

        logger.info(f"Validating external source: {source_host}:{source_port}/{source_database}.{source_schema}")

        source_conn = None
        try:
            # Connect to external source database
            source_conn = await asyncpg.connect(
                host=source_host,
                port=source_port,
                user=source_username,
                password=source_password,
                database=source_database,
            )

            # Check if source schema exists
            schema_exists = await source_conn.fetchval(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = $1", source_schema
            )

            if not schema_exists:
                raise Exception(f"Source schema '{source_schema}' does not exist in database '{source_database}'")

            # Count tables in source schema
            table_count = await source_conn.fetchval(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = $1", source_schema
            )

            logger.info(
                f"External source validated: {source_host}:{source_port}/{source_database}.{source_schema} "
                f"({table_count} tables)"
            )

            return {"table_count": table_count}

        except asyncpg.InvalidCatalogNameError as e:
            raise Exception(f"Source database '{source_database}' does not exist at {source_host}:{source_port}") from e
        except asyncpg.InvalidPasswordError as e:
            raise Exception(f"Authentication failed for {source_username}@{source_host}:{source_port}") from e
        except Exception as e:
            raise Exception(
                f"Failed to connect to external source {source_host}:{source_port}/{source_database}: {e!s}"
            ) from e
        finally:
            if source_conn:
                await source_conn.close()

    async def _clone_database_with_chisel_tunnel(
        self,
        project_name: str,
        deployment_name: str,
        source_host: str,
        source_port: int,
        source_username: str,
        source_password: str,
        source_database: str,
        source_schema: str,
        force_clone: bool,
        chisel_config: dict[str, Any],
        project_data: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """
        Clone database from external source via Chisel tunnel.

        Internal method that handles tunnel setup/teardown and delegates the actual
        clone operation to the direct clone implementation.
        """
        from opi.utils.chisel_helper import chisel_tunnel

        logger.info(
            f"Starting database clone via Chisel tunnel: {project_name}/{deployment_name} "
            f"<- {source_host}:{source_port}/{source_database}.{source_schema} "
            f"(via {chisel_config['server-url']})"
        )

        async with chisel_tunnel(chisel_config, source_host, source_port, project_data) as endpoint:
            logger.info(f"Tunnel established: {endpoint['host']}:{endpoint['port']} -> {source_host}:{source_port}")

            # Call the direct clone method with tunneled connection
            result = await self._execute_external_clone(
                project_name=project_name,
                deployment_name=deployment_name,
                source_host=endpoint["host"],
                source_port=endpoint["port"],
                source_username=source_username,
                source_password=source_password,
                source_database=source_database,
                source_schema=source_schema,
                force_clone=force_clone,
            )

            # Add tunnel info to result
            result["tunnel"] = {
                "used": True,
                "server": chisel_config["server-url"],
                "local_endpoint": f"{endpoint['host']}:{endpoint['port']}",
                "remote_endpoint": f"{source_host}:{source_port}",
            }

            return result

    async def clone_database_from_external_source(
        self,
        project_name: str,
        deployment_name: str,
        source_host: str,
        source_port: int,
        source_username: str,
        source_password: str,
        source_database: str,
        source_schema: str,
        force_clone: bool = False,
        chisel_config: dict[str, Any] | None = None,
        project_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Orchestrate cloning a database from an external source into a target deployment.

        This orchestrator method validates, prepares, and executes cross-cluster database cloning
        by calling existing single-responsibility methods in sequence.

        If chisel_config is provided, a Chisel tunnel is automatically established to reach
        the source database through the tunnel.

        Args:
            project_name: Name of the target project
            deployment_name: Name of the target deployment
            source_host: External source database host (e.g., localhost for port-forward)
            source_port: External source database port (e.g., 15432)
            source_username: Username for external source connection
            source_password: Password for external source connection
            source_database: Source database name
            source_schema: Source schema name
            force_clone: If True, drop existing target database before cloning
            chisel_config: Optional Chisel tunnel configuration dict with keys:
                - server-url: Chisel server URL
                - username: Chisel auth username
                - password: Chisel auth password (may be encrypted)
            project_data: Optional project data for password decryption context

        Returns:
            Dictionary containing operation results with status and operations list

        Example:
            >>> result = await db_manager.clone_database_from_external_source(
            ...     project_name="amt",
            ...     deployment_name="production",
            ...     source_host="localhost",
            ...     source_port=15432,
            ...     source_username="postgres",
            ...     source_password="password",
            ...     source_database="amt_staging",
            ...     source_schema="amt_staging",
            ...     force_clone=True
            ... )
        """
        if chisel_config:
            # Use Chisel tunnel for the connection
            return await self._clone_database_with_chisel_tunnel(
                project_name=project_name,
                deployment_name=deployment_name,
                source_host=source_host,
                source_port=source_port,
                source_username=source_username,
                source_password=source_password,
                source_database=source_database,
                source_schema=source_schema,
                force_clone=force_clone,
                chisel_config=chisel_config,
                project_data=project_data,
            )

        # Direct connection (no tunnel)
        return await self._execute_external_clone(
            project_name=project_name,
            deployment_name=deployment_name,
            source_host=source_host,
            source_port=source_port,
            source_username=source_username,
            source_password=source_password,
            source_database=source_database,
            source_schema=source_schema,
            force_clone=force_clone,
        )

    async def _execute_external_clone(
        self,
        project_name: str,
        deployment_name: str,
        source_host: str,
        source_port: int,
        source_username: str,
        source_password: str,
        source_database: str,
        source_schema: str,
        force_clone: bool,
    ) -> dict[str, Any]:
        """
        Execute the actual database clone operation.

        Internal method that performs the clone without tunnel handling.
        Called by clone_database_from_external_source for direct connections
        and by _clone_database_with_chisel_tunnel for tunneled connections.
        """
        logger.info(
            f"Starting external database clone: {project_name}/{deployment_name} "
            f"<- {source_host}:{source_port}/{source_database}.{source_schema}"
        )

        result = {
            "project": project_name,
            "deployment": deployment_name,
            "source": {"host": source_host, "port": source_port, "database": source_database, "schema": source_schema},
            "operations": [],
            "success": False,
            "errors": [],
        }

        try:
            self._ensure_connection()

            # STEP 1: Validate external source
            try:
                validation = await self._validate_external_source(
                    source_host, source_port, source_username, source_password, source_database, source_schema
                )
                result["operations"].append(
                    {
                        "type": "source_validation",
                        "status": "success",
                        "table_count": validation["table_count"],
                    }
                )
            except Exception as e:
                result["errors"].append(f"Source validation failed: {e!s}")
                result["operations"].append({"type": "source_validation", "status": "failed", "error": str(e)})
                return result

            # STEP 2: Validate target deployment
            try:
                project_data = await self.project_manager.get_contents()
                if not project_data:
                    raise Exception(f"Project '{project_name}' not found")

                deployment = next(
                    (d for d in project_data.get("deployments", []) if d.get("name") == deployment_name), None
                )
                if not deployment:
                    raise Exception(f"Deployment '{deployment_name}' not found")

                if not await self._deployment_uses_postgresql(project_data, deployment_name):
                    raise Exception(f"Deployment '{deployment_name}' does not use PostgreSQL service")

                result["operations"].append({"type": "target_validation", "status": "success"})
            except Exception as e:
                result["errors"].append(f"Target validation failed: {e!s}")
                result["operations"].append({"type": "target_validation", "status": "failed", "error": str(e)})
                return result

            # STEP 3: Get appropriate database configuration (namespace-specific or shared)
            db_host, admin_username, admin_password = await self._get_database_config_for_deployment(
                project_data, deployment
            )

            # Get database service config (includes privileges if specified). Placement
            # (dedicated vs shared) decides whether there is a CNPG config to read;
            # the revision service-name below is a separate question (which service the
            # deployment entry is stored under), so keep both flags.
            uses_dedicated_postgresql = project_uses_dedicated_postgres(project_data)
            uses_namespace_postgresql = self._project_uses_namespace_postgresql(project_data)
            service_config = self._get_database_service_config(project_data) if uses_dedicated_postgresql else {}
            database_privileges = service_config.get("privileges", [])

            # Get current generation from project file (for generational versioning)
            generation = self._get_deployment_database_generation(project_data, deployment_name)

            # STEP 4: Resolve target credentials (reuse existing method)
            # Username is never versioned (same user accesses all versions)
            target_username = generate_database_name(project_name, deployment_name, None)
            # Database/schema use current generation (may be updated in STEP 5 if force_clone)
            target_database = generate_database_name(project_name, deployment_name, generation)
            target_schema = target_database

            try:
                target_password = await self._resolve_database_credentials(
                    project_name,
                    deployment_name,
                    deployment,
                    target_username,
                    target_database,
                    target_schema,
                    db_host=db_host,
                    admin_username=admin_username,
                    admin_password=admin_password,
                    database_privileges=database_privileges,
                )
                result["target"] = {"database": target_database, "schema": target_schema, "username": target_username}
                result["resolved_password"] = target_password  # For callers that need the resolved password
                result["operations"].append({"type": "credentials_resolved", "status": "success"})
            except Exception as e:
                result["errors"].append(f"Credential resolution failed: {e!s}")
                result["operations"].append({"type": "credentials_resolved", "status": "failed", "error": str(e)})
                return result

            # STEP 5: Prepare target database (generational approach for force_clone)
            try:
                # Check if target database exists by attempting to create it
                create_result = await self.postgres_connector.create_database(
                    database_name=target_database,
                    owner=target_username,
                )
                target_db_exists = create_result["status"] == "exists"

                if force_clone and target_db_exists:
                    # Generational approach: increment generation and create new versioned database
                    # Instead of destroying the old database, create a new versioned one
                    new_generation = (generation or 0) + 1
                    target_database = generate_database_name(project_name, deployment_name, new_generation)
                    target_schema = target_database  # Schema matches database name

                    logger.info(
                        f"force_clone=True: Using generational approach. "
                        f"Creating new database {target_database} (generation {generation} -> {new_generation})"
                    )

                    # Create new versioned database (old database is preserved)
                    create_result = await self.postgres_connector.create_database(
                        database_name=target_database,
                        owner=target_username,
                    )
                    if create_result["status"] != "created":
                        raise Exception(
                            f"Failed to create versioned database {target_database}: {create_result.get('message', 'Unknown error')}"
                        )
                    logger.info(f"Created new versioned database: {target_database}")

                    # Update generation in project file
                    service_type = (
                        ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value
                        if uses_namespace_postgresql
                        else ServiceType.POSTGRESQL_DATABASE.value
                    )
                    project_file_handler = self.project_manager._project_file_handler
                    project_file_handler.set_deployment_service_generation(
                        project_data, deployment_name, service_type, new_generation
                    )
                    logger.info(f"Updated database generation in project file: {new_generation}")

                    # Record revision for the new generation
                    source_desc = f"external:{source_host}:{source_port}/{source_database}"
                    self.project_manager._revision_manager.record_clone(
                        project_data=project_data,
                        deployment_name=deployment_name,
                        service_type=service_type,
                        generation=new_generation,
                        resource_name=target_database,
                        source=source_desc,
                    )

                    result["operations"].append(
                        {
                            "type": "database_versioned",
                            "status": "success",
                            "generation": new_generation,
                            "database": target_database,
                        }
                    )
                    # Update result target info with new database name
                    result["target"]["database"] = target_database
                    result["target"]["schema"] = target_schema
                elif create_result["status"] == "created":
                    logger.info(f"Created database for cloning: {target_database}")
                    result["operations"].append({"type": "database_prepared", "status": "created"})

                    # Record revision for initial creation
                    current_gen = generation if generation is not None else 0
                    service_type = (
                        ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value
                        if uses_namespace_postgresql
                        else ServiceType.POSTGRESQL_DATABASE.value
                    )
                    source_desc = f"external:{source_host}:{source_port}/{source_database}"
                    self.project_manager._revision_manager.record_clone(
                        project_data=project_data,
                        deployment_name=deployment_name,
                        service_type=service_type,
                        generation=current_gen,
                        resource_name=target_database,
                        source=source_desc,
                    )
                else:
                    logger.info(f"Database already exists for cloning: {target_database}")
                    result["operations"].append({"type": "database_prepared", "status": "exists"})
            except Exception as e:
                result["errors"].append(f"Database preparation failed: {e!s}")
                result["operations"].append({"type": "database_prepared", "status": "failed", "error": str(e)})
                return result

            # STEP 6: Execute clone (reuse existing connector method)
            # Source credentials passed as parameters, target uses bound connector
            try:
                clone_result = await self.postgres_connector.clone_schema_from_external(
                    source_host=source_host,
                    source_port=source_port,
                    source_username=source_username,
                    source_password=source_password,
                    source_database=source_database,
                    source_schema=source_schema,
                    target_database=target_database,
                    target_schema=target_schema,
                    target_owner=target_username,
                    target_owner_password=target_password,
                    force_clone=force_clone,
                )

                if clone_result["status"] != "success":
                    raise Exception(f"Clone failed: {clone_result.get('message')}")

                result["operations"].append({"type": "database_cloned", "status": "success"})
            except Exception as e:
                result["errors"].append(f"Database clone failed: {e!s}")
                result["operations"].append({"type": "database_cloned", "status": "failed", "error": str(e)})
                return result

            # STEP 7: Store credentials in memory map
            try:
                # Extra schemas (RC-17) declared by the project, created empty on the
                # clone target (the external source only populates the default schema),
                # so the search_path, grants and variables match the normal path.
                extra_schema_pairs = self._resolve_extra_schemas(project_data, project_name, deployment_name)
                for _postfix, extra_schema in extra_schema_pairs:
                    await self.postgres_connector.create_schema(
                        schema_name=extra_schema, database=target_database, owner=target_username
                    )
                all_schemas = [target_schema, *(name for _, name in extra_schema_pairs)]
                await self.postgres_connector.set_role_search_path(
                    username=target_username, database=target_database, schemas=all_schemas
                )

                # Ensure the persistent read-only role is granted on every schema of the
                # (possibly newly versioned) clone target.
                ro_username, ro_password = await self._ensure_readonly_user(
                    deployment_name=deployment_name,
                    deployment=deployment,
                    main_username=target_username,
                    database=target_database,
                    schemas=all_schemas,
                )
                database_secret = DatabaseSecret(
                    host=self._db_host,
                    port=5432,
                    username=target_username,
                    password=target_password,
                    schema=target_schema,
                    database=target_database,
                    ro_username=ro_username,
                    ro_password=ro_password,
                    extra_schemas=extra_schema_pairs,
                )
                self.project_manager._add_secret_to_create(deployment_name, "database", database_secret)
                result["operations"].append({"type": "credentials_stored_in_memory", "status": "success"})
            except Exception as e:
                logger.warning(f"Failed to store credentials in memory (clone succeeded): {e}")
                result["operations"].append(
                    {"type": "credentials_stored_in_memory", "status": "warning", "error": str(e)}
                )

            # STEP 8: Sync secrets to Git if credentials were updated
            # Check if secrets were added to _secrets_to_create and need to be written to Git
            if deployment_name in self.project_manager._secrets_to_create:
                logger.info(f"External clone updated secrets for {deployment_name}, syncing to Git for ArgoCD")
                try:
                    # Regenerate manifests for this deployment to write secrets to Git.
                    # This commits and pushes the manifests to the deployments repo itself.
                    #
                    # There used to be an extra commit_and_push here on the *project files*
                    # connector, which was wrong twice over: the manifests do not live in
                    # that repo, so it committed nothing of its own, and because that
                    # connector is now the shared warm working copy, its `git add -A` would
                    # sweep whatever unrelated state happened to be dirty there into a
                    # commit labelled "Update database credentials".
                    await self.project_manager._process_application_manifests(deployment_name)

                    result["operations"].append(
                        {
                            "type": "secrets_synced_to_git",
                            "status": "success",
                            "message": "Updated database credentials committed to Git for ArgoCD sync",
                        }
                    )
                    logger.info(f"Successfully synced updated secrets to Git for {deployment_name}")
                except Exception as e:
                    logger.warning(f"Failed to sync updated secrets to Git: {e}")
                    result["operations"].append(
                        {
                            "type": "secrets_synced_to_git",
                            "status": "warning",
                            "error": str(e),
                            "message": "Database clone succeeded but failed to sync credentials to Git. Manual sync may be required.",
                        }
                    )

            result["success"] = True
            logger.info(f"External database clone completed successfully: {project_name}/{deployment_name}")
            return result

        except Exception as e:
            logger.exception(f"Unexpected error during external clone: {e}")
            result["errors"].append(f"Unexpected error: {e!s}")
            return result
