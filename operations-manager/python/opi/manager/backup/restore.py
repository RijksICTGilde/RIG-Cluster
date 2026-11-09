"""Restoring with generations: a new PVC, database or bucket next to the previous one."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opi.connectors.kubectl import create_kubectl_connector
from opi.core.cluster_config import get_storage_access_modes, get_storage_class_name
from opi.core.config import settings
from opi.manager.backup.bucket_backup import create_bucket_backup_manager
from opi.manager.backup.database_backup import create_database_backup_manager
from opi.manager.backup.pvc_backup import create_backup_manager
from opi.services.catalog.postgresql_database.connection_limit import deployment_connection_limit
from opi.services.project_file_handler import extract_storage_from_component_services
from opi.services.secrets import DatabaseSecret
from opi.utils.naming import (
    generate_bucket_name,
    generate_database_name,
    generate_database_username,
    generate_pvc_name,
    generate_storage_name,
    generate_unique_name,
)

if TYPE_CHECKING:
    from opi.services.project_file_handler import ProjectFileHandler

logger = logging.getLogger(__name__)

TERUGZETTEN_MISLUKT = "Het terugzetten is niet gelukt. Probeer het over een minuut opnieuw."


async def _restore_pvc_with_versioning(
    project_name: str,
    deployment_name: str,
    component_name: str,
    storage_name: str,
    snapshot_id: str,
    deployment_cluster: str,
    namespace: str,
    project_data: dict[str, Any],
    project_file_handler: ProjectFileHandler,
) -> dict[str, Any]:
    """
    Restore a PVC with versioning support.

    Returns dict with: success, error, old_generation, new_generation, old_resource_name, new_resource_name
    """
    # Get current generation
    current_generation = project_file_handler.get_storage_generation(
        project_data, deployment_name, component_name, storage_name
    )
    if current_generation is None:
        current_generation = 0
    next_generation = current_generation + 1

    # Calculate PVC names
    unique_name = generate_unique_name(deployment_name, component_name)
    source_pvc_name = generate_pvc_name(unique_name, storage_name, current_generation)
    target_pvc_name = generate_pvc_name(unique_name, storage_name, next_generation)

    # Get storage info from base component
    base_components = {c.get("name"): c for c in project_data.get("components", [])}
    base_component = base_components.get(component_name, {})
    storage_list = extract_storage_from_component_services(base_component)

    target_storage = None
    for idx, storage in enumerate(storage_list):
        if storage.get("type") != "persistent":
            continue
        s_name = storage.get("name")
        if not s_name:
            mount_path = storage.get("mount-path", "") or storage.get("mount_path", "")
            s_name = generate_storage_name(mount_path, idx)
        if s_name == storage_name:
            target_storage = storage
            break

    storage_size = target_storage.get("size", "10Gi") if target_storage else "10Gi"
    storage_class = get_storage_class_name(deployment_cluster)
    access_modes = get_storage_access_modes(deployment_cluster)
    backup_enabled = target_storage.get("backup", True) if target_storage else True

    logger.info(
        f"Restoring PVC {source_pvc_name} -> {target_pvc_name} "
        f"(generation {current_generation} -> {next_generation}) in {namespace}"
    )

    # Perform the restore
    backup_manager = create_backup_manager()
    result = await backup_manager.restore_to_project_pvc(
        cluster=deployment_cluster,
        namespace=namespace,
        source_pvc_name=source_pvc_name,
        target_pvc_name=target_pvc_name,
        storage_size=storage_size,
        storage_class=storage_class,
        access_modes=access_modes,
        snapshot_id=snapshot_id,
        backup_enabled=backup_enabled,
        project_name=project_name,
    )

    if not result.success:
        # De melding van de terugzetlaag noemt de pod en de laatste 500 tekens van zijn
        # log; dat hoort in de log en niet in een antwoord.
        logger.error("Terugzetten van PVC %s mislukt: %s", target_pvc_name, result.error)
        return {
            "success": False,
            "error": TERUGZETTEN_MISLUKT,
            "old_generation": current_generation,
            "new_generation": next_generation,
            "old_resource_name": source_pvc_name,
            "new_resource_name": target_pvc_name,
        }

    return {
        "success": True,
        "error": None,
        "old_generation": current_generation,
        "new_generation": next_generation,
        "old_resource_name": source_pvc_name,
        "new_resource_name": target_pvc_name,
    }


async def _restore_database_with_versioning(
    project_name: str,
    deployment_name: str,
    component_name: str,
    reference_name: str,
    snapshot_id: str,
    deployment_cluster: str,
    namespace: str,
    project_data: dict[str, Any],
    project_file_handler: ProjectFileHandler,
) -> dict[str, Any]:
    """
    Restore a database with versioning support.

    Creates a new versioned database, restores data to it.

    Returns dict with: success, error, old_generation, new_generation, old_resource_name, new_resource_name
    """
    from opi.connectors.postgres import create_postgres_connector
    from opi.core.cluster_config import get_database_server
    from opi.utils.passwords import generate_secure_password

    # Get current generation
    current_generation = project_file_handler.get_database_generation(project_data, deployment_name)
    if current_generation is None:
        current_generation = 0
    next_generation = current_generation + 1

    # Generate database names
    old_database_name = generate_database_name(project_name, deployment_name, current_generation)
    new_database_name = generate_database_name(project_name, deployment_name, next_generation)

    logger.info(
        f"Restoring database {old_database_name} -> {new_database_name} "
        f"(generation {current_generation} -> {next_generation})"
    )

    # Get database connection info
    db_host = get_database_server(deployment_cluster)
    admin_username = settings.DATABASE_ADMIN_NAME
    admin_password = settings.DATABASE_ADMIN_PASSWORD

    try:
        # Create new database with versioned name
        postgres_connector = create_postgres_connector(
            host=db_host,
            admin_username=admin_username,
            admin_password=admin_password,
        )

        # The versioned database is owned by the SAME user as every other generation --
        # the username carries no generation suffix.
        db_username = generate_database_username(project_name, deployment_name)

        # Reuse the deployment's password: ArgoCD owns the secret (selfHeal policy)
        # so a new one cannot be patched in. Only generate one if no secret exists.
        existing_secret = await DatabaseSecret.get_data(
            kubectl_connector=create_kubectl_connector(), namespace=namespace, prefix=deployment_name
        )
        reuse_password = bool(existing_secret and existing_secret.password)
        db_password = (
            existing_secret.password  # type: ignore[union-attr]
            if reuse_password
            else generate_secure_password(min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20)
        )

        user_result = await postgres_connector.create_user(
            username=db_username,
            password=db_password,
            connection_limit=deployment_connection_limit(project_data, deployment_name),
        )
        if user_result["status"] == "exists" and not reuse_password:
            await postgres_connector.update_user_password(username=db_username, new_password=db_password)

        # Create new versioned database
        db_result = await postgres_connector.create_database(database_name=new_database_name, owner=db_username)
        if db_result["status"] not in ["created", "exists"]:
            await postgres_connector.close()
            return {
                "success": False,
                "error": f"Failed to create new database {new_database_name}: {db_result.get('message')}",
                "old_generation": current_generation,
                "new_generation": next_generation,
                "old_resource_name": old_database_name,
                "new_resource_name": new_database_name,
            }

        # pg_restore adds to existing rows; restore only to empty databases.
        # A database that exists but is empty is a half-finished earlier attempt.
        if db_result["status"] == "exists" and await postgres_connector.database_has_user_data(new_database_name):
            await postgres_connector.close()
            return {
                "success": False,
                "error": (
                    f"Target database {new_database_name} already exists and is not empty. "
                    f"Restoring into it would add the backup on top of the rows already there. "
                    f"Remove that database or raise the generation before retrying."
                ),
                "old_generation": current_generation,
                "new_generation": next_generation,
                "old_resource_name": old_database_name,
                "new_resource_name": new_database_name,
            }

        logger.info(f"Created new database: {new_database_name}")

        # Een nieuwe generatie is een nieuwe database, dus ook hier dicht voor PUBLIC
        # (RC-285). Alleen de eigenaar: de read-only rol krijgt zijn CONNECT bij de
        # verwerking die deze generatie in gebruik neemt, en heeft hem daarvoor niet nodig.
        await postgres_connector.restrict_database_to_roles(new_database_name, [db_username])

        # Create schema in new database
        schema_result = await postgres_connector.create_schema(
            schema_name=new_database_name,  # Schema name matches database name
            database=new_database_name,
            owner=db_username,
        )
        if schema_result["status"] not in ["created", "exists"]:
            logger.warning(f"Schema creation warning: {schema_result}")

        await postgres_connector.close()

        # Restore data to new database using backup manager
        database_backup_manager = create_database_backup_manager()
        restore_result = await database_backup_manager.restore_database(
            cluster=deployment_cluster,
            namespace=namespace,
            reference_name=reference_name,
            target_database_host=db_host,
            target_database_port=5432,
            target_database_name=new_database_name,
            target_database_user=db_username,
            target_database_password=db_password,
            snapshot_id=snapshot_id,
            project_name=project_name,
            # The dump comes from the previous generation, whose database name is also
            # its default schema name. Naming it here is what keeps the restore pod from
            # having to guess which of the database's schemas is the application's own.
            source_database_name=old_database_name,
        )

        if not restore_result.success:
            logger.error("Terugzetten van database %s mislukt: %s", new_database_name, restore_result.error)
            return {
                "success": False,
                "error": TERUGZETTEN_MISLUKT,
                "old_generation": current_generation,
                "new_generation": next_generation,
                "old_resource_name": old_database_name,
                "new_resource_name": new_database_name,
            }

        return {
            "success": True,
            "error": None,
            "old_generation": current_generation,
            "new_generation": next_generation,
            "old_resource_name": old_database_name,
            "new_resource_name": new_database_name,
        }

    except Exception:
        logger.exception("Error in database versioned restore")
        return {
            "success": False,
            "error": TERUGZETTEN_MISLUKT,
            "old_generation": current_generation,
            "new_generation": next_generation,
            "old_resource_name": old_database_name,
            "new_resource_name": new_database_name,
        }


async def _restore_bucket_with_versioning(
    project_name: str,
    deployment_name: str,
    component_name: str,
    reference_name: str,
    snapshot_id: str,
    deployment_cluster: str,
    namespace: str,
    project_data: dict[str, Any],
    project_file_handler: ProjectFileHandler,
) -> dict[str, Any]:
    """
    Restore a bucket with versioning support.

    Creates a new versioned bucket, restores data to it.

    Returns dict with: success, error, old_generation, new_generation, old_resource_name, new_resource_name
    """
    from opi.connectors.minio_mc import create_minio_connector
    from opi.utils.passwords import generate_secure_password

    # Get current generation
    current_generation = project_file_handler.get_bucket_generation(project_data, deployment_name)
    if current_generation is None:
        current_generation = 0
    next_generation = current_generation + 1

    # Generate bucket names
    old_bucket_name = generate_bucket_name(project_name, deployment_name, current_generation)
    new_bucket_name = generate_bucket_name(project_name, deployment_name, next_generation)

    logger.info(
        f"Restoring bucket {old_bucket_name} -> {new_bucket_name} "
        f"(generation {current_generation} -> {next_generation})"
    )

    try:
        # Create new bucket with versioned name
        minio_connector = create_minio_connector()
        alias_name = "default-minio"

        alias_configured = await minio_connector.configure_alias(
            alias=alias_name,
            host=settings.MINIO_HOST,
            access_key=settings.MINIO_ADMIN_ACCESS_KEY,
            secret_key=settings.MINIO_ADMIN_SECRET_KEY,
            secure=settings.MINIO_USE_TLS,
            region=settings.MINIO_REGION,
        )

        if not alias_configured:
            return {
                "success": False,
                "error": f"Failed to configure MinIO alias for {settings.MINIO_HOST}",
                "old_generation": current_generation,
                "new_generation": next_generation,
                "old_resource_name": old_bucket_name,
                "new_resource_name": new_bucket_name,
            }

        # Create new versioned bucket
        bucket_result = await minio_connector.create_bucket(alias_name, new_bucket_name)
        if bucket_result["status"] not in ["created", "exists"]:
            return {
                "success": False,
                "error": f"Failed to create new bucket {new_bucket_name}: {bucket_result.get('message')}",
                "old_generation": current_generation,
                "new_generation": next_generation,
                "old_resource_name": old_bucket_name,
                "new_resource_name": new_bucket_name,
            }

        # Same bottom as the database path: a restore into a bucket that already holds
        # objects merges the backup with what is there instead of replacing it, and
        # ``clear_target=False`` below assumes a fresh bucket.
        if bucket_result["status"] == "exists" and await minio_connector.bucket_has_objects(
            alias_name, new_bucket_name
        ):
            return {
                "success": False,
                "error": (
                    f"Target bucket {new_bucket_name} already exists and is not empty. "
                    f"Restoring into it would merge the backup with the objects already there. "
                    f"Remove that bucket or raise the generation before retrying."
                ),
                "old_generation": current_generation,
                "new_generation": next_generation,
                "old_resource_name": old_bucket_name,
                "new_resource_name": new_bucket_name,
            }

        logger.info(f"Created new bucket: {new_bucket_name}")

        # Grant access to user (same user pattern as original bucket)
        from opi.utils.naming import generate_minio_username

        minio_username = generate_minio_username(project_name, deployment_name)

        # Ensure user exists and has access to new bucket
        user_result = await minio_connector.create_user(
            alias_name,
            minio_username,
            generate_secure_password(min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20),
        )
        if user_result["status"] not in ["created", "exists"]:
            logger.warning(f"Could not ensure user exists: {user_result}")

        # Grant bucket access
        access_result = await minio_connector.grant_bucket_access(
            alias_name, minio_username, new_bucket_name, ["read", "write", "delete", "list"]
        )
        if access_result["status"] not in ["granted", "attached"]:
            logger.warning(f"Could not grant bucket access: {access_result}")

        # Restore data to new bucket using backup manager
        bucket_backup_manager = create_bucket_backup_manager()
        restore_result = await bucket_backup_manager.restore_bucket(
            cluster=deployment_cluster,
            namespace=namespace,
            reference_name=reference_name,
            target_minio_endpoint=f"http://{settings.MINIO_HOST}",
            target_bucket_name=new_bucket_name,
            target_access_key=settings.MINIO_ADMIN_ACCESS_KEY,
            target_secret_key=settings.MINIO_ADMIN_SECRET_KEY,
            snapshot_id=snapshot_id,
            clear_target=False,  # New bucket is empty
            project_name=project_name,
        )

        if not restore_result.success:
            logger.error("Terugzetten van bucket %s mislukt: %s", new_bucket_name, restore_result.error)
            return {
                "success": False,
                "error": TERUGZETTEN_MISLUKT,
                "old_generation": current_generation,
                "new_generation": next_generation,
                "old_resource_name": old_bucket_name,
                "new_resource_name": new_bucket_name,
            }

        return {
            "success": True,
            "error": None,
            "old_generation": current_generation,
            "new_generation": next_generation,
            "old_resource_name": old_bucket_name,
            "new_resource_name": new_bucket_name,
        }

    except Exception:
        logger.exception("Error in bucket versioned restore")
        return {
            "success": False,
            "error": TERUGZETTEN_MISLUKT,
            "old_generation": current_generation,
            "new_generation": next_generation,
            "old_resource_name": old_bucket_name,
            "new_resource_name": new_bucket_name,
        }
