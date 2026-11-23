"""Database service manager for handling PostgreSQL resources."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import asyncpg

if TYPE_CHECKING:
    from opi.manager.project_manager import ProjectManager
    from opi.services.marked_for_deletion_service import MarkedForDeletionService

from opi.connectors.postgres import (
    PostgresConnector,
    PostgresExecutionError,
    PostgresValidationError,
    create_postgres_connector,
)
from opi.core.cluster_config import get_database_server
from opi.core.config import settings
from opi.manager.backup.runs import clone_backup_items
from opi.services import CloneFromType, ServiceType
from opi.services.catalog.postgresql_database.connection_limit import deployment_connection_limit
from opi.services.catalog.shared.postgres import DedicatedPostgresFields
from opi.services.clone_validation import (
    composite_name_collision,
    other_project_deployments,
    resolve_clone_source_deployment,
)
from opi.services.postgres_scope import (
    database_generation_service_type,
    get_dedicated_postgres_config,
    get_postgres_schemas,
    project_uses_dedicated_postgres,
)
from opi.services.project import Project
from opi.services.secrets import DatabaseSecret
from opi.services.tasks import progress_steps as steps
from opi.utils.egress_guard import UNREACHABLE_SOURCE_REASON, clone_source_refusal
from opi.utils.naming import generate_database_name, generate_extra_database_schema
from opi.utils.passwords import generate_secure_password

logger = logging.getLogger(__name__)

READONLY_STATEMENT_TIMEOUT = "5min"


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
        self.project_manager = project_manager
        self._db_host = db_host
        self._admin_username = admin_username
        self._admin_password = admin_password
        self._postgres_connector: PostgresConnector | None = None

    def _ensure_connection(self) -> None:
        if self._postgres_connector is None:
            self._postgres_connector = create_postgres_connector(
                host=self._db_host, admin_username=self._admin_username, admin_password=self._admin_password
            )

    @property
    def postgres_connector(self) -> PostgresConnector:
        if self._postgres_connector is None:
            self._ensure_connection()
        if self._postgres_connector is None:
            raise RuntimeError("PostgreSQL connector failed to initialize")
        return self._postgres_connector

    async def close(self) -> None:
        if self._postgres_connector is not None:
            await self._postgres_connector.close()
            self._postgres_connector = None

    def _get_deployment_database_generation(
        self, project_data: dict[str, Any], deployment_name: str, service_type: str | None = None
    ) -> int | None:
        """From ``deployments[name].services.{service_type}.generation``, or None when unset.

        Args:
            service_type: None reads it from the project config, which names either
                namespace-postgresql-database or postgresql-database.
        """
        if service_type is None:
            service_type = database_generation_service_type(project_data)

        project_file_handler = self.project_manager._project_file_handler
        return project_file_handler.get_deployment_service_generation(project_data, deployment_name, service_type)

    async def create_resources_for_deployment(
        self,
        project_data: dict[str, Any],
        deployment: dict[str, Any],
        force_clone: bool = False,
        clone_interrupted: bool = False,
        rotate_password: bool = False,
    ) -> None:
        """Create the database resources of a deployment that has the PostgreSQL service.

        Works for both placements: a shared cluster, and a project's own dedicated one. The
        order is credentials, then the database (with its ``clone-from`` or force-clone), then
        the schema, then the final credentials with the right service endpoint. Replay-safe:
        the outcome is the same whatever the starting state.

        Args:
            force_clone: Runtime override that clones even when the target exists.
            clone_interrupted: A previous run started this clone and did not record it as
                completed.
            rotate_password: Replace the user's password instead of reusing the stored one.
        """
        project_name = await self.project_manager.get_name()
        deployment_name = deployment["name"]
        cluster_name = deployment.get("cluster")

        if not cluster_name:
            raise ValueError(f"Deployment {deployment_name} is missing required 'cluster' field")

        if not await self._deployment_uses_postgresql(project_data, deployment_name):
            logger.debug(f"Deployment {deployment_name} does not use PostgreSQL service, skipping")
            return

        # Second layer: a role or database with this name may belong to another project, so
        # establish that it belongs to this one before setting its password.
        refusal = composite_name_collision(project_name, deployment_name, other_project_deployments(project_name))
        if refusal:
            raise ValueError(refusal)

        logger.info(f"Processing database resources for project: {project_name}, deployment: {deployment_name}")

        progress_manager = self.project_manager.get_progress_manager()
        database_task = None
        if progress_manager:
            database_task = progress_manager.add_task(steps.DATABASE_CONFIG_APPLY, subject=deployment_name)

        try:
            # Determine placement: a dedicated (project-scoped) cluster or the shared
            # instance. Both namespace-postgresql-database and postgresql-database with
            # scope: project mean "dedicated" (RC-17).
            uses_dedicated_postgresql = project_uses_dedicated_postgres(project_data)

            if uses_dedicated_postgresql:
                cluster_config = self._get_database_cluster_config(project_data, cluster_name)
                db_host = cluster_config["service_endpoint"]
                infrastructure_namespace = cluster_config["infrastructure_namespace"]

                logger.info(
                    f"Using dedicated PostgreSQL cluster for {project_name} "
                    f"(infrastructure: {infrastructure_namespace}, endpoint: {db_host})"
                )
                admin_username, admin_password = await self._get_infrastructure_superuser_credentials(
                    project_name, infrastructure_namespace
                )
            else:
                db_host = get_database_server(cluster_name)
                admin_username = settings.DATABASE_ADMIN_NAME
                admin_password = settings.DATABASE_ADMIN_PASSWORD
                logger.info(f"Using shared PostgreSQL for {project_name} (host: {db_host})")

            self._ensure_connection()

            service_config = self._get_database_service_config(project_data) if uses_dedicated_postgresql else {}
            database_privileges = service_config.get("privileges", [])

            if database_privileges:
                logger.info(f"Database privileges specified for {project_name}: {database_privileges}")

            generation = self._get_deployment_database_generation(project_data, deployment_name)
            if generation is not None and generation > 0:
                logger.info(f"Using database generation {generation} for {deployment_name}")

            # The username is never versioned: one role reaches every generation.
            db_username = generate_database_name(project_name, deployment_name, None)
            db_database = generate_database_name(project_name, deployment_name, generation)
            db_schema = db_database
            connection_limit = deployment_connection_limit(project_data, deployment_name)

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
                connection_limit=connection_limit,
                database_privileges=database_privileges,
                rotate_password=rotate_password,
            )
            # Not in the password branch: an existing role with valid credentials never
            # reaches it, and a limit change must not rotate credentials.
            await self._reconcile_connection_limit(db_username, connection_limit)

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
                clone_interrupted=clone_interrupted,
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
                connection_limit=connection_limit,
                db_host=db_host,
                rotate_password=rotate_password,
            )

            # Only these two roles open this database. Here and not in create_database,
            # because the roles are not known there yet, and on every processing round, so a
            # database from before RC-285 is closed too.
            await self.postgres_connector.restrict_database_to_roles(db_database, [db_username, ro_username])

            logger.info(f"Phase 3: Storing final credentials for {project_name}/{deployment_name}")
            database_secret = DatabaseSecret(
                host=db_host,
                port=5432,
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
        self,
        db_username: str,
        postgres_conn: PostgresConnector,
        connection_limit: int,
        database_privileges: list[str] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Create or update a database user, and return (password, result).

        Args:
            connection_limit: CONNECTION LIMIT for a role this call creates.
            database_privileges: PostgreSQL privileges to grant, e.g. ["SUPERUSER"].
        """
        db_password = generate_secure_password(min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20)
        create_result = await postgres_conn.create_user(
            username=db_username,
            password=db_password,
            database_privileges=database_privileges,
            connection_limit=connection_limit,
        )

        if create_result["status"] == "exists":
            update_result = await postgres_conn.update_user_password(
                username=db_username,
                new_password=db_password,
            )
            if database_privileges:
                await postgres_conn.update_user_privileges(
                    username=db_username,
                    database_privileges=database_privileges,
                )
            return db_password, update_result
        else:
            return db_password, create_result

    async def _reconcile_connection_limit(self, username: str, connection_limit: int) -> None:
        """Bring the role's connection limit to ``connection_limit`` and report the outcome."""
        result = await self.postgres_connector.set_connection_limit(username, connection_limit)
        if result["status"] == "not_found":
            raise ValueError(f"Database role {username} does not exist, cannot set its connection limit")
        if result["status"] == "updated":
            outcome = f"{username}: van {result['previous']} naar {connection_limit}"
        else:
            outcome = f"{username}: {connection_limit}, ongewijzigd"
        logger.info(f"Connection limit {outcome}")
        progress_manager = self.project_manager.get_progress_manager()
        if progress_manager:
            progress_manager.complete_task(progress_manager.add_task(steps.CONNECTION_LIMIT, subject=outcome))

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

    async def _resolve_application_schemas(
        self, *, source_database: str, platform_sources: set[str], platform_targets: set[str]
    ) -> list[tuple[str, str]]:
        """Discover and validate app-created schemas from the clone source to preserve them (RC-360).

        Excludes: public, pg_*, extension namespaces (postInitSQL owns those in the target).
        """
        inventory = await self.postgres_connector.describe_schemas(source_database)
        discovered = [
            name
            for name in inventory.schemas
            if name != "public"
            and not name.startswith("pg_")
            and name not in platform_sources
            and name not in inventory.extension_namespaces
        ]

        for name in sorted(inventory.extension_namespaces - platform_sources - {"public"}):
            logger.info(f"Clone leaves schema {name} of {source_database} to postInitSQL: it holds an extension")

        # Before the target gets anything: a name the platform cannot handle, or one that
        # collides with a schema of this deployment, has to fail the whole clone. A half
        # clone is the problem this solves.
        for name in discovered:
            try:
                self.postgres_connector.validate_cloneable_schema_name(name)
            except PostgresValidationError as err:
                raise PostgresValidationError(
                    f"Cannot clone {source_database}: {err}. Rename the schema in the source deployment, "
                    "then clone again."
                ) from err
            if name in platform_targets:
                raise PostgresValidationError(
                    f"Cannot clone {source_database}: schema '{name}' has the same name as a schema this "
                    "deployment gets of its own. Rename it in the source deployment, then clone again."
                )

        self._report_cloned_application_schemas(source_database, discovered, inventory.public_objects)
        return [(name, name) for name in discovered]

    def _report_cloned_application_schemas(
        self, source_database: str, discovered: list[str], public_objects: list[str]
    ) -> None:
        """Say in the task progress which schemas ride along, and what public leaves behind."""
        progress_manager = self.project_manager.get_progress_manager()
        if discovered:
            logger.info(f"Clone takes along the application schemas of {source_database}: {', '.join(discovered)}")
            if progress_manager:
                progress_manager.complete_task(
                    progress_manager.add_task(steps.CLONE, subject=f"ook {', '.join(discovered)}")
                )
        if public_objects:
            objects = ", ".join(public_objects)
            logger.warning(
                f"Clone does not take the public schema of {source_database} along, so these objects stay "
                f"behind: {objects}"
            )
            if progress_manager:
                progress_manager.complete_task(
                    progress_manager.add_task(steps.CLONE, subject=f"public gaat niet mee: {objects}")
                )

    async def _ensure_readonly_user(
        self,
        deployment_name: str,
        deployment: dict[str, Any],
        main_username: str,
        database: str,
        schemas: list[str],
        connection_limit: int,
        db_host: str,
        rotate_password: bool = False,
    ) -> tuple[str, str]:
        """Ensure a persistent read-only role exists for the deployment's database.

        The role mirrors the main user's lifecycle: created alongside it, granted
        SELECT-only on every schema of the deployment (the default schema plus any
        extra schemas, RC-17 decision 10.4), with a default search_path over the same
        list and a statement_timeout of ``READONLY_STATEMENT_TIMEOUT``. The read-only
        database console (and read-only application use) connect as this role, so writes
        are impossible server-side and the console can browse every schema. Its password
        is kept stable across syncs by reusing the value already stored in the deployment's
        database secret, and it is only set again when that value no longer logs in, like
        the main user in ``_resolve_database_credentials``.

        Args:
            schemas: The deployment's schemas, primary first; must be non-empty.
            rotate_password: Skip reuse of the stored password and set a new one. This role
                reads every schema of the deployment, so a rotation that leaves it alone
                leaves a working old value behind.

        Returns:
            Tuple of (ro_username, ro_password).
        """
        ro_username = f"{main_username}_ro"

        # Reuse the stored read-only password to avoid churning the secret (and
        # any consumer of it) on every reprocess; generate one only if absent.
        ro_password = ""
        reused = False
        if not rotate_password:
            existing = await self._get_existing_database_credentials_from_k8s(deployment_name, deployment)
            ro_password = existing.ro_password if existing and existing.ro_password else ""
            reused = bool(ro_password)
        if not ro_password:
            ro_password = generate_secure_password(min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20)

        create_result = await self.postgres_connector.create_user(
            username=ro_username, password=ro_password, connection_limit=connection_limit
        )
        if create_result["status"] == "exists" and not (
            reused
            and await self._test_database_connection(
                username=ro_username, password=ro_password, database=database, schema=schemas[0], host=db_host
            )
        ):
            await self.postgres_connector.update_user_password(username=ro_username, new_password=ro_password)
        await self._reconcile_connection_limit(ro_username, connection_limit)

        for schema in schemas:
            await self.postgres_connector.grant_readonly_on_schema(database, schema, ro_username)
        await self.postgres_connector.set_role_search_path(username=ro_username, database=database, schemas=schemas)
        await self.postgres_connector.set_role_setting(ro_username, "statement_timeout", READONLY_STATEMENT_TIMEOUT)

        return ro_username, ro_password

    async def _reset_user_password(
        self,
        db_username: str,
        connection_limit: int,
        database_privileges: list[str] | None,
    ) -> str:
        """Set a fresh password on the database role and return it."""
        new_password, result = await self._create_or_update_user(
            db_username=db_username,
            postgres_conn=self.postgres_connector,
            connection_limit=connection_limit,
            database_privileges=database_privileges,
        )
        if result["status"] not in ["updated", "created", "success"]:
            raise Exception(
                f"Failed to update password for database user {db_username}: {result.get('message', 'Unknown error')}"
            )
        logger.info(f"Updated password for database user: {db_username}")
        return new_password

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
        connection_limit: int,
        database_privileges: list[str] | None = None,
        rotate_password: bool = False,
    ) -> str:
        """The working password of the database user: the stored one when it still logs in,
        otherwise a fresh one on a created or updated role.

        Args:
            connection_limit: CONNECTION LIMIT for a role this call creates.
            rotate_password: Skip reuse of the stored password and set a new one.
        """

        logger.info(f"Checking database credentials for {project_name}/{deployment_name} at host {db_host}")

        if rotate_password:
            logger.info(f"Replacing database password for {project_name}/{deployment_name} on request")
            return await self._reset_user_password(db_username, connection_limit, database_privileges)

        existing_credentials = await self._get_existing_database_credentials_from_k8s(deployment_name, deployment)

        if existing_credentials:
            logger.info(f"Found existing database secret in Kubernetes for {project_name}/{deployment_name}")
            db_password = existing_credentials.password

            if not db_password:
                logger.error(f"Database secret exists but has no password for {project_name}/{deployment_name}")
                raise ValueError(f"Invalid database secret for {project_name}/{deployment_name}: missing password")

            logger.info(f"Testing existing database credentials for {project_name}/{deployment_name}")
            credentials_valid = await self._test_database_connection(
                username=db_username, password=db_password, database=db_database, schema=db_schema, host=db_host
            )

            if credentials_valid:
                logger.info(f"Existing database credentials are valid for {project_name}/{deployment_name}")
                if database_privileges:
                    logger.info(f"Ensuring database privileges for {db_username}: {database_privileges}")
                    await self.postgres_connector.update_user_privileges(
                        username=db_username,
                        database_privileges=database_privileges,
                    )
                return db_password
            else:
                if settings.RECREATE_PASSWORD_ON_AUTHENTICATION_FAILURE:
                    logger.warning(
                        f"Database credentials are invalid for {project_name}/{deployment_name}, updating password"
                    )
                    return await self._reset_user_password(db_username, connection_limit, database_privileges)
                else:
                    raise Exception(
                        f"Database secret exists for {project_name}/{deployment_name} but credentials are invalid. "
                        f"Manual intervention required to fix database user or update secret."
                    )
        else:
            logger.info(f"No database secret found in Kubernetes for {project_name}/{deployment_name}")
            db_password, create_result = await self._create_or_update_user(
                db_username=db_username,
                postgres_conn=self.postgres_connector,
                connection_limit=connection_limit,
                database_privileges=database_privileges,
            )

            if create_result["status"] == "error":
                raise Exception(
                    f"Failed to create database user {db_username}: {create_result.get('message', 'Unknown error')}"
                )
            elif create_result["status"] == "exists":
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
        clone_interrupted: bool = False,
    ) -> DatabaseStateResult:
        """Bring the database into the right state, including its ``clone-from`` and force-clone.

        Runs whether or not credentials existed, covers both a local deployment clone and a
        remote-source clone over a Chisel tunnel, and updates ``clone-from.status`` in
        ``project_data`` after a successful clone. Mode ``once`` (the default) clones only
        while ``status.completed`` is not True; mode ``always`` clones every sync and implies
        force_clone.

        Args:
            project_data: Needed for a remote-source clone.
            force_clone_override: Runtime override of force_clone, from the API.
            clone_interrupted: A previous run started this clone and did not record it as
                completed.

        Returns the final database, schema and password, which differ from the input values
        when a generational failover happened.
        """
        clone_from = deployment.get("clone-from")
        force_clone = force_clone_override or deployment.get("force-clone", False)

        # clone-from must be a dict with type/reference/mode keys
        if clone_from and not isinstance(clone_from, dict):
            raise ValueError(
                f"clone-from for deployment '{deployment_name}' must be a dict with 'type', 'reference', "
                f"and 'mode' keys, got: {type(clone_from).__name__} = {clone_from!r}"
            )

        if clone_from:
            clone_mode = clone_from.get("mode", "once")
            status = clone_from.get("status", {})
            clone_status_completed = status.get("completed", False) if isinstance(status, dict) else False

            if clone_mode == "always":
                force_clone = True
                logger.info(f"Clone mode 'always' for {deployment_name}, forcing clone")

            if clone_mode == "once" and clone_status_completed and not force_clone:
                logger.info(
                    f"Clone mode 'once' for {deployment_name} and clone already completed "
                    f"(timestamp: {status.get('timestamp', 'unknown')}), skipping clone"
                )
                # None skips the clone logic and carries on with the normal flow.
                clone_from = None
            elif clone_mode == "once" and clone_status_completed and force_clone:
                logger.info(f"Clone mode 'once' for {deployment_name} but force_clone=True, proceeding with clone")

        clone_source_ref: str | None = None
        if clone_from:
            clone_type = clone_from.get("type")
            if clone_type == CloneFromType.REMOTE_SOURCE.value:
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
                    clone_interrupted=clone_interrupted,
                )

                if not result.get("success"):
                    raise RuntimeError(f"Remote source clone failed: {result.get('errors', ['Unknown error'])}")

                self.project_manager.report_clone_performed(
                    deployment_name, ServiceType.POSTGRESQL_DATABASE.value, generation
                )

                target = result.get("target", {})
                return DatabaseStateResult(
                    database=target.get("database", db_database),
                    schema=target.get("schema", db_schema),
                    password=result.get("resolved_password", db_password),
                )
            elif clone_type == CloneFromType.DEPLOYMENT.value:
                clone_source_ref = clone_from.get("reference")
            elif clone_type == CloneFromType.BACKUP.value:
                if project_data is None:
                    raise ValueError(f"project_data is required for backup clone: {deployment_name}")
                backup_items = await clone_backup_items(project_data, project_name, deployment, clone_from)
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
            if project_data is None:
                msg = (
                    f"project_data is required to clone '{deployment_name}': without it the source cannot be "
                    "checked against the project and the extra schemas cannot be resolved"
                )
                raise ValueError(msg)
            resolve_clone_source_deployment(project_data, clone_source_ref, deployment_name)
            source_database = generate_database_name(project_name, clone_source_ref, None)
            source_schema = source_database
            # Extra schemas (RC-17) live in the same database and must come along, or a
            # clone would lose their data. Their names embed the deployment, so the
            # source uses the source deployment and the target uses this deployment.
            extra_clone_pairs = [
                (
                    generate_extra_database_schema(project_name, clone_source_ref, postfix),
                    generate_extra_database_schema(project_name, deployment_name, postfix),
                )
                for postfix, _ in self._resolve_extra_schemas(project_data, project_name, deployment_name)
            ]
            logger.info(f"Clone requested from {source_database} to {db_database} (force={force_clone})")

            service_type = (
                ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value
                if project_data and self._project_uses_namespace_postgresql(project_data)
                else ServiceType.POSTGRESQL_DATABASE.value
            )

            await self._validate_clone_source(source_database, source_schema)

            # Schemas the application created itself (RC-360) ride along too, or the clone
            # yields a database whose migration table claims work that is not there.
            extra_clone_pairs += await self._resolve_application_schemas(
                source_database=source_database,
                platform_sources={source_schema, *(source for source, _ in extra_clone_pairs)},
                platform_targets={db_schema, *(target for _, target in extra_clone_pairs)},
            )

            database_result = await self.postgres_connector.create_database(
                database_name=db_database,
                owner=db_username,
            )
            target_db_exists = database_result["status"] == "exists"

            database_created_here = not target_db_exists

            final_generation = generation

            # A half state (part of the target schemas) keeps the failover: cloning on refuses
            # on the existing target schema (postgres.py:1842) and dropping is not safe.
            clone_finished = False
            resume_in_place = False
            leftover_source_schemas: set[str] = set()
            if target_db_exists and clone_interrupted and not force_clone:
                clone_pairs = [(source_schema, db_schema), *extra_clone_pairs]
                # Only the pairs that rename say how far the clone got: an app schema of the
                # same name (RC-360) already sits under its target name after CREATE SCHEMA.
                renamed_targets = {target for source, target in clone_pairs if source != target}
                schemas_present = await self._schema_names(db_database)
                clone_finished = renamed_targets <= schemas_present
                resume_in_place = renamed_targets.isdisjoint(schemas_present)
                if resume_in_place:
                    # A hard stop leaves source schemas behind, because pg_dump only renames at
                    # the end; dropping them is safe, since they belong to the source deployment
                    # and are not live data.
                    source_names = {source for source, _ in clone_pairs}
                    leftover_source_schemas = (source_names - renamed_targets) & schemas_present

            if target_db_exists and (
                force_clone or (generation is None and not clone_finished and not resume_in_place)
            ):
                # Generational failover: create a new versioned database instead of cloning
                # into an existing, possibly half-finished one. On force_clone, or when the
                # database exists while no generation was ever recorded, but not when an
                # interrupted attempt left a state this run can continue.
                new_generation = (generation or 0) + 1

                # Previous failed attempts may have left orphaned versioned databases, so look
                # for a generation that is free.
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

            # After postInitSQL, so a second attempt still updates the extensions.
            if clone_finished:
                logger.info(
                    f"Clone into {db_database} was finished by an interrupted run, recording it instead of cloning again"
                )
                self._record_and_report_clone(
                    project_data=project_data,
                    deployment_name=deployment_name,
                    service_type=service_type,
                    generation=final_generation,
                    resource_name=db_database,
                    source=f"deployment:{clone_source_ref}",
                )
                return DatabaseStateResult(database=db_database, schema=db_schema, password=db_password)

            for leftover in sorted(leftover_source_schemas):
                logger.info(f"Dropping schema {leftover} in {db_database}, left by an interrupted clone")
                await self.postgres_connector.delete_schema(leftover, db_database, cascade=True)

            schemas_before = set() if database_created_here else await self._schema_names(db_database)
            # The failover may have moved db_schema, so the target set is rebuilt here.
            target_schemas = {db_schema, *(target for _, target in extra_clone_pairs)}
            # pg_dump restores under the source name and renames at the end, so a failed
            # clone can leave either name behind.
            clone_schemas = target_schemas | {source_schema, *(source for source, _ in extra_clone_pairs)}
            try:
                # No flag on a new generation (which only reaches disk at the save at the end)
                # and none over a schema that was already there: in both cases a next run would
                # take it for a finished clone (`features/kloonpoging.md`).
                if final_generation == generation and target_schemas.isdisjoint(schemas_before):
                    await self.project_manager.mark_clone_started(deployment_name)
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
                    except Exception as cleanup_err:  # noqa: BLE001  # best-effort cleanup; it must not hide the failure it follows
                        logger.error(f"Failed to clean up database {db_database} after failed clone: {cleanup_err}")
                else:
                    await self._drop_schemas_created_since(db_database, schemas_before, clone_schemas)
                raise

            logger.info(f"Successfully cloned database from {source_database} to {db_database}")
            logger.info(f"Schema cloned with target name '{db_schema}' - no additional rename needed")

            self._record_and_report_clone(
                project_data=project_data,
                deployment_name=deployment_name,
                service_type=service_type,
                generation=final_generation,
                resource_name=db_database,
                source=f"deployment:{clone_source_ref}",
            )
        else:
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

    def _record_and_report_clone(
        self,
        *,
        project_data: dict[str, Any] | None,
        deployment_name: str,
        service_type: str,
        generation: int | None,
        resource_name: str,
        source: str,
    ) -> None:
        """A clone a previous run finished reports itself exactly like a fresh one."""
        if project_data:
            self.project_manager._revision_manager.record_clone(
                project_data=project_data,
                deployment_name=deployment_name,
                service_type=service_type,
                generation=generation,
                resource_name=resource_name,
                source=source,
            )
        self.project_manager.report_clone_performed(deployment_name, service_type, generation)

    async def _schema_names(self, database: str) -> set[str]:
        return {row["schema_name"] for row in await self.postgres_connector.list_schemas(database)}

    async def _drop_schemas_created_since(
        self, database: str, schemas_before: set[str], clone_schemas: set[str]
    ) -> None:
        """Drop what a failed clone left in a database that already existed.

        The database itself cannot be dropped here, so without this a half schema
        survives and a retry takes it for a finished clone. Only the clone's own schema
        names are candidates: this runs in a live tenant database, where another session
        creates schemas too.
        """
        try:
            leftovers = (await self._schema_names(database) - schemas_before) & clone_schemas
        except (PostgresExecutionError, PostgresValidationError) as list_err:
            logger.error(f"Failed to list schemas in {database} after failed clone: {list_err}")
            return
        for schema in sorted(leftovers):
            try:
                await self.postgres_connector.delete_schema(schema, database, cascade=True)
                logger.info(f"Dropped schema {schema} in {database} after failed clone")
            except (PostgresExecutionError, PostgresValidationError) as cleanup_err:
                logger.error(f"Failed to drop schema {schema} in {database} after failed clone: {cleanup_err}")

    async def _validate_clone_source(self, source_database: str, source_schema: str) -> None:
        """Raises:
        Exception: the source database or the source schema does not exist.
        """
        logger.info(f"Validating clone source: database={source_database}, schema={source_schema}")

        conn = await self.postgres_connector._get_or_create_connection("postgres")
        source_db_exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", source_database)

        if not source_db_exists:
            raise Exception(
                f"Cannot clone from {source_database}: source database does not exist. "
                f"Ensure the source deployment has been created successfully first."
            )

        conn = await self.postgres_connector._get_or_create_connection(source_database)

        schema_exists = await conn.fetchval(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = $1", source_schema
        )

        if not schema_exists:
            raise Exception(
                f"Cannot clone from {source_database}: source schema '{source_schema}' does not exist. "
                f"The source deployment appears to be empty or incomplete. "
                f"Ensure the source deployment has been properly initialized with data."
            )

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
        """The (db_host, admin_username, admin_password) of a deployment's placement: its own
        dedicated PostgreSQL, or the shared one.
        """
        project_name = await self.project_manager.get_name()
        cluster_name = deployment.get("cluster")

        if not cluster_name:
            raise ValueError(f"Deployment {deployment['name']} is missing required 'cluster' field")

        # Determine placement: a dedicated (project-scoped) cluster or the shared instance.
        uses_dedicated_postgresql = project_uses_dedicated_postgres(project_data)

        if uses_dedicated_postgresql:
            cluster_config = self._get_database_cluster_config(project_data, cluster_name)
            db_host = cluster_config["service_endpoint"]
            infrastructure_namespace = cluster_config["infrastructure_namespace"]

            logger.info(
                f"Using dedicated PostgreSQL cluster for {project_name} "
                f"(infrastructure: {infrastructure_namespace}, endpoint: {db_host})"
            )
            admin_username, admin_password = await self._get_infrastructure_superuser_credentials(
                project_name, infrastructure_namespace
            )
        else:
            db_host = get_database_server(cluster_name)
            admin_username = settings.DATABASE_ADMIN_NAME
            admin_password = settings.DATABASE_ADMIN_PASSWORD
            logger.info(f"Using shared PostgreSQL for {project_name} (host: {db_host})")

        return db_host, admin_username, admin_password

    async def delete_resources_for_deployment(
        self, project_data: dict[str, Any], deployment: dict[str, Any]
    ) -> dict[str, Any]:
        """Delete the database resources of a deployment, and report the result."""
        project_name = await self.project_manager.get_name()
        deployment_name = deployment["name"]

        deletion_results = {
            "service": "database",
            "deployment": deployment_name,
            "operations": [],
            "success": True,
            "errors": [],
        }

        # Deliberately NOT gated on "does this deployment use PostgreSQL": that asks the
        # CURRENT project file, while the database was created under an earlier version of
        # it, so a deployment rewired away from the service would outlive its database.
        # Existence is the evidence: delete_database is idempotent and returns not_found for
        # a name that was never provisioned, so probing costs nothing.
        declared = await self._deployment_uses_postgresql(project_data, deployment_name)
        logger.info(
            f"Deleting database resources for project: {project_name}, deployment: {deployment_name} "
            f"(declared in project file: {declared})"
        )

        try:
            self._ensure_connection()

            db_username = generate_database_name(project_name, deployment_name, None)

            # Build list of all databases to delete: base name + all generational versions.
            # Check both the YAML generation and discover versions from PostgreSQL
            # to handle cases where the YAML is out of sync (e.g. partial delete).
            base_db_name = generate_database_name(project_name, deployment_name, None)
            databases_to_delete = [base_db_name]
            current_generation = self._get_deployment_database_generation(project_data, deployment_name)
            max_gen = current_generation or 0
            for gen in range(1, max_gen + 10):
                versioned_name = generate_database_name(project_name, deployment_name, gen)
                if versioned_name not in databases_to_delete:
                    databases_to_delete.append(versioned_name)
            if len(databases_to_delete) > 1:
                logger.info(f"Will attempt to delete {len(databases_to_delete)} database(s): {databases_to_delete}")

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
        """Clean up when the PostgreSQL service is removed from a deployment.

        Args:
            deployment_data: The deployment dict from the PREVIOUS YAML.
            project_data: The PREVIOUS project YAML, so the internal service checks
                still pass.
            marked_for_deletion_service: Given, the database and user are marked for deferred
                deletion, which leaves room for recovery; left out, they are deleted at once.
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
        """Whether the project selected NAMESPACE_POSTGRESQL_DATABASE, which needs a dedicated
        database cluster in the project's infrastructure namespace.
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

        The config is merged with the model defaults, and ``image``, ``instances``, ``storage``
        and ``resources`` are required in the result. ``privileges``, ``postInitSQL`` and
        ``registry`` are optional.

        Raises:
            ValueError: the service configuration is invalid, or misses a required field.
        """
        # Defaults and validation live in the typed config model. The dedicated-cluster config
        # can come from either service (namespace-postgresql-database, or postgresql-database
        # with scope: project), and get_dedicated_postgres_config reads whichever is present
        # (RC-17). A shared database has no CNPG cluster, so the model defaults come back and
        # postInitSQL stays empty.
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
        """The service config plus the cluster settings, as the CNPG cluster template, the
        database secret and the infrastructure resources all need it.

        Returns ``database_config`` (image, instances, storage), ``infrastructure_namespace``,
        ``service_endpoint`` (the fully qualified database service DNS name),
        ``storage_class``, ``cluster_name`` and ``database_operator_namespace``.
        """
        from opi.core.cluster_config import get_database_operator_namespace, get_storage_class_name
        from opi.utils.naming import get_database_cluster_service_endpoint, get_infrastructure_namespace

        project_name = project_data.get("name")
        if not project_name:
            raise ValueError("Project name is required in project_data")

        database_config = self._get_database_service_config(project_data)

        infrastructure_namespace = get_infrastructure_namespace(cluster_name, project_name)
        service_endpoint = get_database_cluster_service_endpoint(cluster_name, project_name)
        storage_class = get_storage_class_name(cluster_name)

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
        """The (username, password) of the superuser the CloudNativePG operator bootstraps the
        cluster with, from the Secret in the infrastructure namespace.

        An application uses its own deployment credentials, not these.

        Raises:
            RuntimeError: the Secret is absent or invalid.
        """
        from opi.utils.naming import _sanitize_for_lowercase

        project_clean = _sanitize_for_lowercase(project_name)
        secret_name = f"{project_clean}-postgres-superuser"

        logger.debug(f"Retrieving superuser credentials from {infrastructure_namespace}/{secret_name}")

        try:
            kubectl_connector = self.project_manager._kubectl_connector
            secret_data = await kubectl_connector.get_secret(secret_name, infrastructure_namespace)

            if not secret_data:
                raise RuntimeError(
                    f"Superuser secret '{secret_name}' not found in namespace '{infrastructure_namespace}'. "
                    f"Infrastructure resources may not be deployed yet."
                )

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
        return self.project_manager._project_file_handler.deployment_uses_service(
            project_data,
            deployment_name,
            [ServiceType.POSTGRESQL_DATABASE.value, ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value],
        )

    def _get_remote_source_config(self, project_data: dict[str, Any], remote_source_name: str) -> dict[str, Any] | None:
        remote_sources = project_data.get("remote-sources", [])
        for source in remote_sources:
            if source.get("name") == remote_source_name:
                return source
        return None

    async def _get_existing_database_credentials_from_k8s(
        self, deployment_name: str, deployment: dict[str, Any]
    ) -> DatabaseSecret | None:
        """The deployment's stored DatabaseSecret, or None when it is absent.

        A retrieval error is re-raised rather than swallowed into a None result.
        """
        try:
            from opi.core.cluster_config import get_prefixed_namespace
            from opi.core.config import settings

            secret_name = DatabaseSecret.get_secret_name(deployment_name)
            kubectl_connector = self.project_manager._kubectl_connector

            namespace = get_prefixed_namespace(settings.CLUSTER_MANAGER, deployment["namespace"])

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
        """Whether the server accepts these credentials, on a connection of its own (the pool
        holds the admin credentials).

        False means the server rejected them, or the database does not exist yet, which a
        fresh generation reaches before phase 2. A missing CONNECT privilege counts as True:
        PostgreSQL checks it only after the password is accepted, and the read-only role gets
        its CONNECT later on a clone target (RC-285).

        Raises:
            Exception: any other connection failure. The caller reads False as "authentication
                failed" and sets the password again, so an unreachable host or a failed TLS
                handshake must not land there.
        """
        try:
            conn = await asyncpg.connect(
                host=host,
                port=5432,
                user=username,
                password=password,
                database=database,
                ssl=settings.DATABASE_SSLMODE,
            )
            await conn.close()
            logger.debug(f"Connection test successful for {username}@{host}/{database}")
            return True
        except asyncpg.InsufficientPrivilegeError:
            logger.debug(f"Credentials accepted for {username}@{host}, CONNECT on {database} not granted")
            return True
        except (asyncpg.InvalidAuthorizationSpecificationError, asyncpg.InvalidCatalogNameError) as e:
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
        """Connect to the external source and check that ``source_schema`` is there.

        Returns ``table_count``, the number of tables in that schema.

        Raises:
            Exception: the connection or the check itself failed.
        """
        logger.info(f"Validating external source: {source_host}:{source_port}/{source_database}.{source_schema}")

        source_conn = None
        try:
            source_conn = await asyncpg.connect(
                host=source_host,
                port=source_port,
                user=source_username,
                password=source_password,
                database=source_database,
            )

            schema_exists = await source_conn.fetchval(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = $1", source_schema
            )

            if not schema_exists:
                raise Exception(f"Source schema '{source_schema}' does not exist in database '{source_database}'")

            table_count = await source_conn.fetchval(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = $1", source_schema
            )

            logger.info(
                f"External source validated: {source_host}:{source_port}/{source_database}.{source_schema} "
                f"({table_count} tables)"
            )

            return {"table_count": table_count}

        except asyncpg.InvalidCatalogNameError as e:
            logger.warning(f"Source database '{source_database}' does not exist at {source_host}:{source_port}")
            raise Exception(UNREACHABLE_SOURCE_REASON) from e
        except asyncpg.InvalidPasswordError as e:
            logger.warning(f"Authentication failed for {source_username}@{source_host}:{source_port}")
            raise Exception(UNREACHABLE_SOURCE_REASON) from e
        except Exception as e:
            logger.warning(f"Failed to connect to external source {source_host}:{source_port}/{source_database}: {e!s}")
            raise Exception(UNREACHABLE_SOURCE_REASON) from e
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
        clone_interrupted: bool = False,
    ) -> dict[str, Any]:
        """Set up and tear down the tunnel; the clone itself runs on the direct path."""
        from opi.manager.chisel_helper import chisel_tunnel

        logger.info(
            f"Starting database clone via Chisel tunnel: {project_name}/{deployment_name} "
            f"<- {source_host}:{source_port}/{source_database}.{source_schema} "
            f"(via {chisel_config['server-url']})"
        )

        async with chisel_tunnel(chisel_config, source_host, source_port, project_data) as endpoint:
            logger.info(f"Tunnel established: {endpoint['host']}:{endpoint['port']} -> {source_host}:{source_port}")

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
                clone_interrupted=clone_interrupted,
            )

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
        clone_interrupted: bool = False,
    ) -> dict[str, Any]:
        """Clone a database from an external source into a target deployment.

        Validates, prepares and executes the cross-cluster clone in sequence.

        Args:
            force_clone: Drop the existing target database before cloning.
            chisel_config: Given, a Chisel tunnel is established to reach the source database;
                carries ``server-url``, ``username`` and ``password`` (possibly encrypted).
            project_data: Needed to decrypt that password.
            clone_interrupted: A previous run started this clone and did not record it as
                completed.
        """
        refusal = await clone_source_refusal(source_host, chisel_config.get("server-url") if chisel_config else None)
        if refusal:
            return {
                "project": project_name,
                "deployment": deployment_name,
                "source": {
                    "host": source_host,
                    "port": source_port,
                    "database": source_database,
                    "schema": source_schema,
                },
                "operations": [],
                "success": False,
                "errors": [refusal],
            }

        if chisel_config:
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
                clone_interrupted=clone_interrupted,
            )

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
            clone_interrupted=clone_interrupted,
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
        clone_interrupted: bool = False,
    ) -> dict[str, Any]:
        """The clone itself, without any tunnel handling."""
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
            except Exception as e:  # noqa: BLE001  # a step of this flow records its failure in the result
                result["errors"].append(f"Source validation failed: {e!s}")
                result["operations"].append({"type": "source_validation", "status": "failed", "error": str(e)})
                return result

            try:
                project_data = await self.project_manager.get_contents()
                if not project_data:
                    raise Exception(f"Project '{project_name}' not found")

                deployment = Project(project_data).find("deployments", name=deployment_name)
                if not deployment:
                    raise Exception(f"Deployment '{deployment_name}' not found")

                if not await self._deployment_uses_postgresql(project_data, deployment_name):
                    raise Exception(f"Deployment '{deployment_name}' does not use PostgreSQL service")

                result["operations"].append({"type": "target_validation", "status": "success"})
            except Exception as e:  # noqa: BLE001  # a step of this flow records its failure in the result
                result["errors"].append(f"Target validation failed: {e!s}")
                result["operations"].append({"type": "target_validation", "status": "failed", "error": str(e)})
                return result

            db_host, admin_username, admin_password = await self._get_database_config_for_deployment(
                project_data, deployment
            )

            # Placement (dedicated or shared) decides whether there is a CNPG config to read;
            # the revision service name below is the separate question of which service the
            # deployment entry is stored under, so both flags are kept.
            uses_dedicated_postgresql = project_uses_dedicated_postgres(project_data)
            uses_namespace_postgresql = self._project_uses_namespace_postgresql(project_data)
            service_config = self._get_database_service_config(project_data) if uses_dedicated_postgresql else {}
            database_privileges = service_config.get("privileges", [])

            generation = self._get_deployment_database_generation(project_data, deployment_name)

            target_username = generate_database_name(project_name, deployment_name, None)
            # force_clone may move the generation further down.
            target_database = generate_database_name(project_name, deployment_name, generation)
            target_schema = target_database
            connection_limit = deployment_connection_limit(project_data, deployment_name)

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
                    connection_limit=connection_limit,
                    database_privileges=database_privileges,
                )
                await self._reconcile_connection_limit(target_username, connection_limit)
                result["target"] = {"database": target_database, "schema": target_schema, "username": target_username}
                result["resolved_password"] = target_password
                result["operations"].append({"type": "credentials_resolved", "status": "success"})
            except Exception as e:  # noqa: BLE001  # a step of this flow records its failure in the result
                result["errors"].append(f"Credential resolution failed: {e!s}")
                result["operations"].append({"type": "credentials_resolved", "status": "failed", "error": str(e)})
                return result

            clone_already_finished = False
            new_generation_created = False
            try:
                create_result = await self.postgres_connector.create_database(
                    database_name=target_database,
                    owner=target_username,
                )
                target_db_exists = create_result["status"] == "exists"
                # The manual clone API comes past here too, outside a processing round, so the
                # database is closed off here already (RC-285). Owner only: the read-only role
                # gets its CONNECT from the round that puts this database to use.
                await self.postgres_connector.restrict_database_to_roles(target_database, [target_username])

                if force_clone and target_db_exists:
                    new_generation = (generation or 0) + 1
                    target_database = generate_database_name(project_name, deployment_name, new_generation)
                    target_schema = target_database

                    logger.info(
                        f"force_clone=True: Using generational approach. "
                        f"Creating new database {target_database} (generation {generation} -> {new_generation})"
                    )

                    create_result = await self.postgres_connector.create_database(
                        database_name=target_database,
                        owner=target_username,
                    )
                    if create_result["status"] != "created":
                        raise Exception(
                            f"Failed to create versioned database {target_database}: {create_result.get('message', 'Unknown error')}"
                        )
                    logger.info(f"Created new versioned database: {target_database}")
                    await self.postgres_connector.restrict_database_to_roles(target_database, [target_username])
                    new_generation_created = True

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
                    result["target"]["database"] = target_database
                    result["target"]["schema"] = target_schema
                elif create_result["status"] == "created":
                    logger.info(f"Created database for cloning: {target_database}")
                    result["operations"].append({"type": "database_prepared", "status": "created"})

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
                    if clone_interrupted and not force_clone:
                        # Unlike in _ensure_database_state a leftover source schema stays here:
                        # that name comes from remote-sources and can be a tenant's live data, so
                        # the refusal in postgres.py:1809 stands.
                        schemas_present = await self._schema_names(target_database)
                        clone_already_finished = target_schema in schemas_present
            except Exception as e:  # noqa: BLE001  # a step of this flow records its failure in the result
                result["errors"].append(f"Database preparation failed: {e!s}")
                result["operations"].append({"type": "database_prepared", "status": "failed", "error": str(e)})
                return result

            if clone_already_finished:
                logger.info(f"Clone into {target_database} was finished by an interrupted run, not cloning again")
                result["operations"].append({"type": "database_cloned", "status": "already_finished"})
            else:
                schemas_before = await self._schema_names(target_database)
                try:
                    # No flag over a target schema that was already there, see
                    # _ensure_database_state.
                    if not new_generation_created and target_schema not in schemas_before:
                        await self.project_manager.mark_clone_started(deployment_name)
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
                except Exception as e:  # noqa: BLE001  # a step of this flow records its failure in the result
                    await self._drop_schemas_created_since(
                        target_database, schemas_before, {target_schema, source_schema}
                    )
                    result["errors"].append(f"Database clone failed: {e!s}")
                    result["operations"].append({"type": "database_cloned", "status": "failed", "error": str(e)})
                    return result

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
                    connection_limit=connection_limit,
                    db_host=db_host,
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
            except Exception as e:  # noqa: BLE001  # a step of this flow records its failure in the result
                logger.warning(f"Failed to store credentials in memory (clone succeeded): {e}")
                result["operations"].append(
                    {"type": "credentials_stored_in_memory", "status": "warning", "error": str(e)}
                )

            # Before the manifests below, so they are built on the recorded generation.
            await self.project_manager.commit_clone_state(project_data, deployment_name)

            if deployment_name in self.project_manager._secrets_to_create:
                logger.info(f"External clone updated secrets for {deployment_name}, syncing to Git for ArgoCD")
                try:
                    # This commits and pushes the manifests to the deployments repo itself.
                    # Do not add a commit on the project-files connector here: that is the
                    # shared warm working copy, and its ``git add -A`` would sweep whatever
                    # is dirty there into this commit.
                    await self.project_manager._process_application_manifests(deployment_name)

                    result["operations"].append(
                        {
                            "type": "secrets_synced_to_git",
                            "status": "success",
                            "message": "Updated database credentials committed to Git for ArgoCD sync",
                        }
                    )
                    logger.info(f"Successfully synced updated secrets to Git for {deployment_name}")
                except Exception as e:  # noqa: BLE001  # a step of this flow records its failure in the result
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
