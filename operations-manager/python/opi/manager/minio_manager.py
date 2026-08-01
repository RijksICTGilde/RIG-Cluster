"""MinIO service manager for handling object storage resources."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opi.manager.project_manager import ProjectManager
    from opi.services.marked_for_deletion_service import MarkedForDeletionService

from opi.connectors.minio_mc import MinioConnector, create_minio_connector
from opi.core.cluster_config import get_minio_host, get_minio_port
from opi.core.config import settings
from opi.services import CloneFromType, ServiceType
from opi.services.project import Project
from opi.utils.naming import generate_bucket_name, generate_minio_policy_name, generate_minio_username
from opi.utils.passwords import generate_secure_password
from opi.utils.secrets import MinIOSecret

logger = logging.getLogger(__name__)


class MinioManager:
    """Manager for MinIO object storage operations and resources."""

    def __init__(self, project_manager: ProjectManager) -> None:
        """
        Initialize the MinioManager with reference to ProjectManager.

        Args:
            project_manager: The main ProjectManager instance for accessing shared resources
        """
        self.project_manager = project_manager

    def _get_deployment_bucket_generation(self, project_data: dict[str, Any], deployment_name: str) -> int | None:
        """
        Get the bucket generation for a deployment from the project file.

        Reads from the deployment-level services block:
        deployments[name].services.minio-storage.generation

        Args:
            project_data: The project configuration data
            deployment_name: Name of the deployment

        Returns:
            Generation number if set, None otherwise
        """
        project_file_handler = self.project_manager._project_file_handler
        return project_file_handler.get_deployment_service_generation(
            project_data, deployment_name, ServiceType.MINIO_STORAGE.value
        )

    def _get_minio_service_config(self, project_data: dict[str, Any], deployment_name: str) -> dict[str, Any] | None:
        """
        Extract minio-storage service configuration for a deployment.

        Supports both simple and configured service formats:
        - Simple: "- minio-storage" -> returns None
        - Configured: "- minio-storage:\n    config:\n      enable-versioning: true" -> returns config dict

        Args:
            project_data: The project configuration data
            deployment_name: Name of the deployment to check

        Returns:
            Config dict or None if service not configured or no config block

        Example return:
            {"enable-versioning": True}
        """
        # Find the deployment in project data
        deployments = project_data.get("deployments", [])
        deployment = Project.locate(deployments, name=deployment_name)

        if not deployment:
            logger.debug(f"Deployment {deployment_name} not found in project data")
            return None

        from opi.services.services import service_entry_config, service_entry_name

        # Get services list for this deployment
        services = project_data.get("services", [])

        # Look for minio-storage service. Format-agnostic: the entry may be a bare
        # string, a legacy single-key dict, or a uniform record ({name, config}).
        # ``next(iter(keys))`` returned "name"/"reference" for a record, so a configured
        # minio-storage was silently read as unconfigured.
        for service in services:
            if service_entry_name(service) != ServiceType.MINIO_STORAGE.value:
                continue
            config = service_entry_config(service)
            if config:
                logger.debug(f"Found minio-storage config for {deployment_name} with keys: {sorted(config)}")
                return config
            logger.debug(f"Found minio-storage service for {deployment_name}, but no config block")
            return None

        logger.debug(f"No minio-storage service found for {deployment_name}")
        return None

    async def _apply_bucket_versioning(
        self,
        minio_connector: MinioConnector,
        alias: str,
        bucket_name: str,
        config: dict[str, Any] | None,
    ) -> None:
        """
        Apply bucket versioning configuration if specified.

        This method is idempotent and will only apply changes if needed.
        Supports backward compatibility - if config is None or doesn't specify versioning,
        no action is taken.

        Args:
            minio_connector: MinIO connector instance
            alias: MinIO alias name
            bucket_name: Bucket name
            config: MinIO service config (may be None for backward compatibility)

        Raises:
            RuntimeError: If versioning operation fails
        """
        # Backward compatibility: if no config or no versioning setting, skip
        if not config or "enable-versioning" not in config:
            logger.debug(f"No versioning configuration for bucket {bucket_name}, skipping")
            return

        enable_versioning = config.get("enable-versioning", False)
        logger.info(f"Applying versioning configuration to bucket {bucket_name}: enable={enable_versioning}")

        try:
            result = await minio_connector.ensure_bucket_versioning(alias, bucket_name, enable_versioning)

            if result["status"] == "unchanged":
                logger.info(f"Bucket {bucket_name} versioning already in desired state")
            elif result["status"] == "enabled":
                logger.info(f"Enabled versioning on bucket {bucket_name}")
            elif result["status"] == "suspended":
                logger.info(f"Suspended versioning on bucket {bucket_name}")
            else:
                logger.warning(f"Unexpected versioning operation result: {result}")

        except Exception as e:
            error_msg = f"Failed to apply versioning configuration to bucket {bucket_name}: {e}"
            logger.error(error_msg)
            raise RuntimeError(error_msg) from e

    async def create_resources_for_deployment(
        self,
        project_data: dict[str, Any],
        deployment: dict[str, Any],
        force_clone_override: bool = False,
    ) -> None:
        """
        Create MinIO object storage resources for a deployment that has MinIO storage enabled.

        If the deployment has a 'clone-from' field, it will clone resources from the specified
        source deployment instead of creating new ones.

        Args:
            project_data: The project configuration data
            deployment: The specific deployment configuration
            force_clone_override: Runtime override for force_clone (from API)
        """
        project_name = await self.project_manager.get_name()
        deployment_name = deployment["name"]

        # Check if this deployment has MinIO service enabled
        if not await self._deployment_uses_minio(project_data, deployment_name):
            logger.debug(f"Deployment {deployment_name} does not use MinIO service, skipping")
            return

        # Check if this deployment should be cloned from another deployment
        clone_from = deployment.get("clone-from")
        # Use runtime override if True, otherwise fall back to deployment config
        force_clone = force_clone_override or deployment.get("force-clone", False)

        if clone_from:
            # clone-from must be a dict with type/reference/mode keys
            if not isinstance(clone_from, dict):
                raise ValueError(
                    f"clone-from for deployment '{deployment_name}' must be a dict with 'type', 'reference', "
                    f"and 'mode' keys, got: {type(clone_from).__name__} = {clone_from!r}"
                )

            # Check if clone should be skipped (completed "once" mode clone)
            clone_mode = clone_from.get("mode", "once")
            clone_status = clone_from.get("status", {})
            clone_completed = clone_status.get("completed", False) if isinstance(clone_status, dict) else False

            if clone_mode == "once" and clone_completed and not force_clone:
                logger.info(
                    f"Skipping clone for {deployment_name}: mode is 'once' and already completed. "
                    "Proceeding with normal resource creation."
                )
            else:
                clone_type = clone_from.get("type")
                if clone_type == CloneFromType.REMOTE_SOURCE.value:
                    remote_source_name = clone_from.get("reference")
                    await self._handle_remote_source_clone(
                        project_name, deployment_name, remote_source_name, project_data, force_clone
                    )
                    return
                elif clone_type == CloneFromType.DEPLOYMENT.value:
                    source_deployment = clone_from.get("reference")
                    logger.info(f"Deployment {deployment_name} has clone-from deployment: {source_deployment}")
                    await self.clone_minio_from_deployment(project_data, deployment, source_deployment)
                    return
                elif clone_type == CloneFromType.BACKUP.value:
                    # Fall through to normal resource creation, then restore data
                    logger.info(
                        f"Clone-from type 'backup' for deployment '{deployment_name}': "
                        "creating bucket, then restoring data from backup"
                    )
                else:
                    raise ValueError(f"Unknown clone-from type '{clone_type}' for deployment '{deployment_name}'")

        logger.info(f"Processing MinIO resources for project: {project_name}, deployment: {deployment_name}")

        progress_manager = self.project_manager.get_progress_manager()
        minio_task = None
        if progress_manager:
            minio_task = progress_manager.add_task("Creating MinIO storage resources")

        try:
            minio_connector = create_minio_connector()

            # Configure MinIO alias for this connection
            alias_name = "default-minio"
            alias_configured = await minio_connector.configure_alias(
                alias=alias_name,
                host=settings.MINIO_HOST,
                access_key=settings.MINIO_ADMIN_ACCESS_KEY,
                secret_key=settings.MINIO_ADMIN_SECRET_KEY,
                secure=settings.MINIO_USE_TLS,
                region=settings.MINIO_REGION,  # Ensure bucket creation uses consistent region
            )

            if not alias_configured:
                raise RuntimeError(f"Failed to configure MinIO alias '{alias_name}' for {settings.MINIO_HOST}")

            # Get the generation from project file if set (for versioned buckets after restore)
            generation = self._get_deployment_bucket_generation(project_data, deployment_name)
            if generation is not None and generation > 0:
                logger.info(f"Using bucket generation {generation} for {deployment_name}")

            # Generate consistent MinIO identifiers
            # Username is never versioned (same user accesses all versions)
            # Bucket uses generation if set (for versioned restore)
            minio_username = generate_minio_username(project_name, deployment_name)
            bucket_name = generate_bucket_name(project_name, deployment_name, generation)

            # STEP 1: Check if MinIO secret already exists in Kubernetes
            existing_minio_secret = await self._get_existing_minio_credentials_from_k8s(deployment_name, deployment)

            if existing_minio_secret:
                # Test existing credentials
                logger.info(f"Found existing MinIO secret in Kubernetes for {project_name}/{deployment_name}")

                logger.info(f"Testing existing MinIO credentials for {project_name}/{deployment_name}")
                credentials_valid = await self._test_minio_connection(
                    minio_connector, existing_minio_secret.access_key, existing_minio_secret.secret_key, bucket_name
                )

                if not credentials_valid:
                    # Check if user exists in MinIO to distinguish between:
                    # 1. User doesn't exist (fresh MinIO) -> always recreate
                    # 2. User exists but wrong password -> respect setting
                    user_exists = await self._check_minio_user_exists(
                        minio_connector, alias_name, existing_minio_secret.access_key
                    )

                    if not user_exists:
                        # User doesn't exist in MinIO (fresh MinIO deployment)
                        # Always recreate user regardless of setting
                        logger.warning(
                            f"MinIO user {minio_username} doesn't exist in MinIO (fresh deployment), "
                            f"creating new user with existing credentials from secret"
                        )
                        should_recreate = True
                    elif settings.RECREATE_PASSWORD_ON_AUTHENTICATION_FAILURE:
                        # User exists but credentials are invalid
                        # Recreate because setting allows it
                        logger.warning(
                            f"MinIO credentials are invalid for {project_name}/{deployment_name}, "
                            f"removing old user and creating new one"
                        )
                        should_recreate = True
                    else:
                        # User exists, credentials invalid, but setting prevents recreation
                        logger.error(
                            f"MinIO secret exists for {project_name}/{deployment_name} but credentials are invalid. "
                            f"Manual intervention required to fix MinIO user or update secret."
                        )
                        return

                    if should_recreate:
                        minio_secret_key = generate_secure_password(
                            min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20
                        )

                        # Remove the existing user first (required for MinIO before creating with new password)
                        delete_result = await minio_connector.delete_user(alias_name, minio_username)
                        if delete_result["status"] not in ["deleted", "not_found"]:
                            logger.error(
                                f"Failed to delete existing MinIO user {minio_username}: "
                                f"{delete_result.get('message', 'Unknown error')}"
                            )
                            return
                        elif delete_result["status"] == "deleted":
                            logger.info(f"Deleted existing MinIO user: {minio_username}")
                        else:
                            logger.info(f"MinIO user {minio_username} was already deleted")

                        # Create new user with new credentials
                        user_result = await minio_connector.create_user(alias_name, minio_username, minio_secret_key)
                        if user_result["status"] not in ["created", "exists"]:
                            logger.error(
                                f"Failed to recreate MinIO user {minio_username}: "
                                f"{user_result.get('message', 'Unknown error')}"
                            )
                            return
                        logger.info(f"Recreated MinIO user with new credentials: {minio_username}")

                        # Use new credentials
                        access_key = minio_username
                        secret_key = minio_secret_key

                        # After user recreation, we need to ensure bucket exists and permissions are granted
                        need_bucket_setup = True
                else:
                    logger.info(f"Existing MinIO credentials are valid for {project_name}/{deployment_name}")
                    # Use existing credentials
                    access_key = existing_minio_secret.access_key
                    secret_key = existing_minio_secret.secret_key
                    need_bucket_setup = False  # Credentials work, assume setup is complete
            else:
                # No existing secret, create new MinIO resources
                logger.info(f"No MinIO secret found in Kubernetes for {project_name}/{deployment_name}")

                # Generate new secure MinIO credentials
                minio_username = generate_minio_username(project_name, deployment_name)
                minio_secret_key = generate_secure_password(
                    min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20
                )

                # Try to create MinIO user
                user_result = await minio_connector.create_user(alias_name, minio_username, minio_secret_key)

                if user_result["status"] == "error":
                    logger.error(
                        f"Failed to create MinIO user {minio_username}: {user_result.get('message', 'Unknown error')}"
                    )
                    return
                elif user_result["status"] == "exists":
                    # TODO: this flow part feels very weird.. maybe it should not exist

                    # User exists but we don't have the secret key, update it
                    logger.info(f"MinIO user {minio_username} already exists, updating secret key")

                    # Generate new secure MinIO credentials
                    minio_secret_key = generate_secure_password(
                        min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20
                    )

                    # Remove the existing user first (required for MinIO before creating with new password)
                    delete_result = await minio_connector.delete_user(alias_name, minio_username)
                    if delete_result["status"] not in ["deleted", "not_found"]:
                        logger.warning(
                            f"Failed to delete existing MinIO user {minio_username}: "
                            f"{delete_result.get('message', 'Unknown error')}"
                        )
                    else:
                        logger.info(f"Deleted existing MinIO user: {minio_username}")

                    update_result = await minio_connector.create_user(alias_name, minio_username, minio_secret_key)

                    if update_result["status"] not in ["created", "exists"]:
                        logger.error(
                            f"Failed to update secret key for existing MinIO user {minio_username}: "
                            f"{update_result.get('message', 'Unknown error')}"
                        )
                        return
                    logger.info(f"Secret key updated for existing MinIO user: {minio_username}")
                else:
                    logger.info(f"Created new MinIO user: {minio_username}")

                # Use new credentials
                access_key = minio_username
                secret_key = minio_secret_key
                need_bucket_setup = True  # New user created, need to ensure bucket and permissions

            # Ensure bucket exists and permissions are granted (for new users or recreated users)
            if need_bucket_setup:
                logger.info(f"Setting up bucket and permissions for {minio_username}")

                # Create or verify bucket exists
                bucket_result = await minio_connector.create_bucket(alias_name, bucket_name)

                if bucket_result["status"] not in ["created", "exists"]:
                    logger.error(
                        f"Failed to create MinIO bucket {bucket_name}: {bucket_result.get('message', 'Unknown error')}"
                    )
                    return

                if bucket_result["status"] == "created":
                    logger.info(f"Created MinIO bucket: {bucket_name}")
                else:
                    logger.info(f"MinIO bucket already exists: {bucket_name}")

                # Grant bucket access to user
                access_result = await minio_connector.grant_bucket_access(
                    alias_name, minio_username, bucket_name, ["read", "write", "delete", "list"]
                )

                if access_result["status"] not in ["granted", "attached"]:
                    logger.error(
                        f"Failed to grant bucket access to user {minio_username}: "
                        f"{access_result.get('message', 'Unknown error')}"
                    )
                    return

                logger.info(f"Granted full access on bucket {bucket_name} to user {minio_username}")

            # Apply versioning configuration regardless of setup path (works on create, refresh, and update)
            minio_config = self._get_minio_service_config(project_data, deployment_name)
            await self._apply_bucket_versioning(minio_connector, alias_name, bucket_name, minio_config)

            cluster = deployment["cluster"]

            # Store MinIO credentials in private secrets map instead of project data
            minio_secret = MinIOSecret(
                host=get_minio_host(cluster),
                port=get_minio_port(cluster),
                access_key=access_key,
                secret_key=secret_key,
                bucket_name=bucket_name,
                region=settings.MINIO_REGION,  # Required for newer MinIO server versions (2025+)
            )
            self.project_manager._add_secret_to_create(
                deployment_name,
                "minio",
                minio_secret,
            )

            logger.info(f"MinIO resources ready for {deployment_name} (stored in secrets map)")

            # Restore bucket data from backup if this is a backup clone
            if clone_from and isinstance(clone_from, dict) and clone_from.get("type") == CloneFromType.BACKUP.value:
                backup_items = clone_from.get("backup_items", [])
                bucket_item = next(
                    (item for item in backup_items if item.get("resource_type") == "bucket"),
                    None,
                )
                if bucket_item:
                    from opi.core.cluster_config import get_prefixed_namespace
                    from opi.manager.backup.bucket_backup import create_bucket_backup_manager
                    from opi.utils.naming import ensure_fqdn

                    bucket_backup = create_bucket_backup_manager()
                    dep_cluster = deployment.get("cluster", settings.CLUSTER_MANAGER)
                    raw_ns = deployment.get("namespace", project_name)
                    app_namespace = get_prefixed_namespace(dep_cluster, raw_ns)

                    # Ensure FQDN — restore pod runs in the project namespace
                    minio_host = ensure_fqdn(settings.MINIO_HOST)
                    minio_endpoint = f"https://{minio_host}" if settings.MINIO_USE_TLS else f"http://{minio_host}"

                    result = await bucket_backup.restore_bucket(
                        cluster=dep_cluster,
                        namespace=app_namespace,
                        reference_name=bucket_item.get("reference_name", ""),
                        target_minio_endpoint=minio_endpoint,
                        target_bucket_name=bucket_name,
                        target_access_key=settings.MINIO_ADMIN_ACCESS_KEY,
                        target_secret_key=settings.MINIO_ADMIN_SECRET_KEY,
                        snapshot_id=bucket_item.get("snapshot_id"),
                        project_name=project_name,
                    )
                    if result.success:
                        logger.info(f"Bucket restored from backup for {deployment_name}")
                        self.project_manager.report_clone_performed(deployment_name, "minio-storage", None)
                    else:
                        raise RuntimeError(f"Bucket restore from backup failed: {result.error}")

        finally:
            if progress_manager and minio_task:
                progress_manager.complete_task(minio_task)

    async def delete_resources_for_deployment(
        self, project_data: dict[str, Any], deployment: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Delete MinIO resources for a deployment.

        Args:
            project_data: The project configuration data
            deployment: The specific deployment configuration

        Returns:
            Dictionary containing deletion results and status
        """
        project_name = await self.project_manager.get_name()
        deployment_name = deployment["name"]

        deletion_results = {
            "service": "minio",
            "deployment": deployment_name,
            "operations": [],
            "success": True,
            "errors": [],
        }

        # Check if this deployment uses MinIO service
        if not await self._deployment_uses_minio(project_data, deployment_name):
            deletion_results["operations"].append(
                {"type": "minio_cleanup", "status": "skipped", "reason": "Deployment does not use MinIO service"}
            )
            logger.debug(f"Deployment {deployment_name} does not use MinIO service, skipping MinIO cleanup")
            return deletion_results

        logger.info(f"Deleting MinIO resources for project: {project_name}, deployment: {deployment_name}")

        try:
            minio_connector = create_minio_connector()
            alias_name = "default-minio"

            # Configure MinIO alias
            alias_configured = await minio_connector.configure_alias(
                alias=alias_name,
                host=settings.MINIO_HOST,
                access_key=settings.MINIO_ADMIN_ACCESS_KEY,
                secret_key=settings.MINIO_ADMIN_SECRET_KEY,
                secure=settings.MINIO_USE_TLS,
                region=settings.MINIO_REGION,
            )

            if not alias_configured:
                deletion_results["success"] = False
                deletion_results["errors"].append(f"Failed to configure MinIO alias '{alias_name}'")
                return deletion_results

            minio_username = generate_minio_username(project_name, deployment_name)

            # Build list of all buckets to delete: base name + all generational versions
            buckets_to_delete = [generate_bucket_name(project_name, deployment_name, None)]
            current_generation = self._get_deployment_bucket_generation(project_data, deployment_name)
            if current_generation is not None and current_generation > 0:
                buckets_to_delete.extend(
                    generate_bucket_name(project_name, deployment_name, gen) for gen in range(1, current_generation + 1)
                )
                logger.info(
                    f"Deployment has generation {current_generation}, "
                    f"will attempt to delete {len(buckets_to_delete)} bucket(s): {buckets_to_delete}"
                )

            bucket_name = buckets_to_delete[0]  # Base name for policy/access operations

            # Remove bucket access from user first
            try:
                policy_name = generate_minio_policy_name(project_name, deployment_name)
                revoke_result = await minio_connector.detach_policy(alias_name, policy_name, minio_username)

                if revoke_result["status"] in ["success", "detached"]:
                    deletion_results["operations"].append(
                        {
                            "type": "minio_bucket_access_revocation",
                            "target": f"{minio_username}@{bucket_name}",
                            "status": "success",
                        }
                    )
                    logger.info(f"Successfully revoked bucket access from user: {minio_username}")
                else:
                    deletion_results["operations"].append(
                        {
                            "type": "minio_bucket_access_revocation",
                            "target": f"{minio_username}@{bucket_name}",
                            "status": "not_found" if "not found" in revoke_result.get("message", "") else "failed",
                            "error": revoke_result.get("message", "Unknown error"),
                        }
                    )
                    if "not found" not in revoke_result.get("message", ""):
                        deletion_results["errors"].append(
                            f"Failed to revoke bucket access: {revoke_result.get('message')}"
                        )

            except Exception as e:
                deletion_results["operations"].append(
                    {
                        "type": "minio_bucket_access_revocation",
                        "target": f"{minio_username}@{bucket_name}",
                        "status": "error",
                        "error": str(e),
                    }
                )
                deletion_results["errors"].append(f"Error revoking bucket access: {e}")
                logger.exception("Error revoking MinIO bucket access")

            # Delete the policy itself (after detaching from user)
            try:
                policy_name = generate_minio_policy_name(project_name, deployment_name)
                policy_result = await minio_connector.remove_policy(alias_name, policy_name)

                if policy_result["status"] == "success":
                    deletion_results["operations"].append(
                        {"type": "minio_policy_deletion", "target": policy_name, "status": "success"}
                    )
                    logger.info(f"Successfully deleted MinIO policy: {policy_name}")
                else:
                    deletion_results["operations"].append(
                        {
                            "type": "minio_policy_deletion",
                            "target": policy_name,
                            "status": "not_found" if "not found" in policy_result.get("message", "") else "failed",
                            "error": policy_result.get("message", "Unknown error"),
                        }
                    )
                    if "not found" not in policy_result.get("message", ""):
                        deletion_results["errors"].append(
                            f"Failed to delete policy {policy_name}: {policy_result.get('message')}"
                        )

            except Exception as e:
                policy_name = generate_minio_policy_name(project_name, deployment_name)  # Ensure policy_name is defined
                deletion_results["operations"].append(
                    {"type": "minio_policy_deletion", "target": policy_name, "status": "error", "error": str(e)}
                )
                deletion_results["errors"].append(f"Error deleting policy {policy_name}: {e}")
                logger.exception("Error deleting MinIO policy")

            # Delete all buckets (base + generational versions)
            for bucket_to_delete in buckets_to_delete:
                try:
                    bucket_result = await minio_connector.delete_bucket(alias_name, bucket_to_delete, force=True)

                    if bucket_result["status"] in ["deleted", "success"]:
                        deletion_results["operations"].append(
                            {"type": "minio_bucket_deletion", "target": bucket_to_delete, "status": "success"}
                        )
                        logger.info(f"Successfully deleted MinIO bucket: {bucket_to_delete}")
                    elif bucket_result["status"] == "not_found" or "does not exist" in bucket_result.get("message", ""):
                        deletion_results["operations"].append(
                            {"type": "minio_bucket_deletion", "target": bucket_to_delete, "status": "not_found"}
                        )
                        logger.debug(f"MinIO bucket {bucket_to_delete} does not exist, skipping")
                    else:
                        deletion_results["operations"].append(
                            {
                                "type": "minio_bucket_deletion",
                                "target": bucket_to_delete,
                                "status": "failed",
                                "error": bucket_result.get("message", "Unknown error"),
                            }
                        )
                        deletion_results["errors"].append(
                            f"Failed to delete bucket {bucket_to_delete}: {bucket_result.get('message')}"
                        )

                except Exception as e:
                    deletion_results["operations"].append(
                        {
                            "type": "minio_bucket_deletion",
                            "target": bucket_to_delete,
                            "status": "error",
                            "error": str(e),
                        }
                    )
                    deletion_results["errors"].append(f"Error deleting bucket {bucket_to_delete}: {e}")
                    logger.exception(f"Error deleting MinIO bucket {bucket_to_delete}")

            # Delete user
            try:
                user_result = await minio_connector.delete_user(alias_name, minio_username)

                if user_result["status"] in ["deleted", "success"]:
                    deletion_results["operations"].append(
                        {"type": "minio_user_deletion", "target": minio_username, "status": "success"}
                    )
                    logger.info(f"Successfully deleted MinIO user: {minio_username}")
                elif user_result["status"] == "not_found" or "does not exist" in user_result.get("message", ""):
                    deletion_results["operations"].append(
                        {"type": "minio_user_deletion", "target": minio_username, "status": "not_found"}
                    )
                    logger.info(f"MinIO user {minio_username} does not exist or was already deleted")
                else:
                    deletion_results["operations"].append(
                        {
                            "type": "minio_user_deletion",
                            "target": minio_username,
                            "status": "failed",
                            "error": user_result.get("message", "Unknown error"),
                        }
                    )
                    deletion_results["errors"].append(
                        f"Failed to delete user {minio_username}: {user_result.get('message')}"
                    )

            except Exception as e:
                deletion_results["operations"].append(
                    {"type": "minio_user_deletion", "target": minio_username, "status": "error", "error": str(e)}
                )
                deletion_results["errors"].append(f"Error deleting user {minio_username}: {e}")
                logger.exception("Error deleting MinIO user")

        except Exception as e:
            deletion_results["success"] = False
            deletion_results["errors"].append(f"Failed to connect to MinIO: {e}")
            logger.exception("Error connecting to MinIO for cleanup")

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
        """Handle cleanup when MinIO service is removed from a deployment.

        If a ``MarkedForDeletionService`` is provided the bucket, user, and
        policy are marked for deferred deletion.  Otherwise they are deleted
        immediately via ``delete_resources_for_deployment``.

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
            "service": "minio",
            "deployment": deployment_name,
            "trigger": "service_removal",
            "operations": [],
            "success": True,
            "errors": [],
        }

        if marked_for_deletion_service is not None:
            bucket_name = generate_bucket_name(project_name, deployment_name)
            minio_user = generate_minio_username(project_name, deployment_name)
            minio_policy = generate_minio_policy_name(project_name, deployment_name)

            for rtype, rname in [
                ("minio_bucket", bucket_name),
                ("minio_user", minio_user),
                ("minio_policy", minio_policy),
            ]:
                await marked_for_deletion_service.mark_resource(
                    resource_type=rtype,
                    resource_name=rname,
                    project_name=project_name,
                    deployment_name=deployment_name,
                    cluster=cluster,
                    metadata={"server_alias": "minio"},
                )

            result["operations"].append(
                {
                    "type": "mark_for_deletion",
                    "resource_type": "minio_bucket",
                    "resource_name": bucket_name,
                    "status": "marked",
                }
            )
            logger.info(
                "Marked MinIO resources for deferred deletion: %s (project=%s, deployment=%s)",
                bucket_name,
                project_name,
                deployment_name,
            )
        else:
            result = await self.delete_resources_for_deployment(project_data, deployment_data)
            result["trigger"] = "service_removal"

        return result

    async def _deployment_uses_minio(self, project_data: dict[str, Any], deployment_name: str) -> bool:
        """
        Check if a deployment uses MinIO service.

        Args:
            project_data: The project configuration data
            deployment_name: Name of the deployment to check

        Returns:
            True if deployment uses MinIO service, False otherwise
        """
        return self.project_manager._project_file_handler.deployment_uses_service(
            project_data,
            deployment_name,
            [ServiceType.MINIO_STORAGE.value],
        )

    async def _get_existing_minio_credentials_from_k8s(
        self, deployment_name: str, deployment: dict[str, Any]
    ) -> MinIOSecret | None:
        """
        Get existing MinIO credentials from Kubernetes secret.

        Args:
            deployment_name: Name of the deployment
            deployment: The deployment configuration containing namespace info

        Returns:
            MinIOSecret if found, None otherwise
        """
        try:
            from opi.core.cluster_config import get_prefixed_namespace
            from opi.core.config import settings

            secret_name = MinIOSecret.get_secret_name(deployment_name)
            kubectl_connector = self.project_manager._kubectl_connector

            # Calculate the namespace where the secret should be stored
            namespace = get_prefixed_namespace(settings.CLUSTER_MANAGER, deployment["namespace"])

            # Try to get the secret from Kubernetes
            secret_data = await kubectl_connector.get_secret(secret_name, namespace)
            if secret_data:
                return MinIOSecret.from_k8s_secret_data(secret_data)
            return None
        except Exception as e:
            logger.debug(f"Could not retrieve MinIO secret for {deployment_name}: {e}")
            return None

    async def _check_minio_user_exists(self, minio_connector: MinioConnector, alias: str, username: str) -> bool:
        """
        Check if a MinIO user exists.

        Args:
            minio_connector: MinIO connector instance
            alias: MinIO alias name
            username: Username to check

        Returns:
            True if user exists, False otherwise
        """
        try:
            users = await minio_connector.list_users(alias)
            # Check if username matches either 'username' or 'access_key' field
            user_exists = any(user.get("username") == username or user.get("access_key") == username for user in users)
            logger.debug(f"MinIO user {username} exists: {user_exists}")
            return user_exists
        except Exception as e:
            logger.debug(f"Error checking if MinIO user exists: {e}")
            return False

    async def _test_minio_connection(
        self, minio_connector: MinioConnector, access_key: str, secret_key: str, bucket_name: str
    ) -> bool:
        """
        Test if MinIO credentials are valid by attempting operations.

        Args:
            minio_connector: MinIO connector instance
            access_key: MinIO access key
            secret_key: MinIO secret key
            bucket_name: Bucket name to test access to

        Returns:
            True if credentials are valid, False otherwise
        """
        try:
            # Configure a temporary alias for testing
            test_alias = "test-connection"
            alias_configured = await minio_connector.configure_alias(
                alias=test_alias,
                host=settings.MINIO_HOST,
                access_key=access_key,
                secret_key=secret_key,
                secure=settings.MINIO_USE_TLS,
                region=settings.MINIO_REGION,
            )

            if not alias_configured:
                logger.debug(f"Failed to configure test alias for access key {access_key}")
                return False

            # Test if we can list buckets (this tests authentication)
            list_result = await minio_connector.list_buckets(test_alias)
            success = isinstance(list_result, list) or (
                isinstance(list_result, dict) and list_result.get("status") != "error"
            )

            if not success:
                logger.debug(f"MinIO list buckets failed for access key {access_key}: {list_result}")

            return success
        except Exception as e:
            logger.debug(f"MinIO connection test failed for access key {access_key}: {e}")
            return False

    async def clone_minio_from_deployment(
        self,
        project_data: dict[str, Any],
        target_deployment: dict[str, Any],
        source_deployment_name: str,
    ) -> None:
        """
        Clone MinIO resources from source deployment to target deployment.

        Uses generational approach: instead of destroying existing buckets, creates
        new versioned buckets when force_clone is used. This preserves old data and
        allows for rollback.

        Args:
            project_data: The project configuration data
            target_deployment: The target deployment configuration
            source_deployment_name: Name of the source deployment to clone from
        """
        project_name = await self.project_manager.get_name()
        target_deployment_name = target_deployment["name"]
        force_clone = target_deployment.get("force-clone", False)

        logger.info(
            f"Cloning MinIO resources from {source_deployment_name} to {target_deployment_name} "
            f"for project {project_name}"
        )

        minio_connector = create_minio_connector()

        # Configure MinIO alias for this connection
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
            raise RuntimeError(f"Failed to configure MinIO alias '{alias_name}' for {settings.MINIO_HOST}")

        # Get current generation from project file (generational approach)
        current_generation = self._get_deployment_bucket_generation(project_data, target_deployment_name)

        # Source bucket - use base name (source might not be versioned)
        source_bucket = generate_bucket_name(project_name, source_deployment_name, None)

        # Target uses generation from project file
        target_username = generate_minio_username(project_name, target_deployment_name)
        target_bucket = generate_bucket_name(project_name, target_deployment_name, current_generation)

        logger.info(f"Target bucket will be: {target_bucket} (generation={current_generation})")

        # Check if source bucket exists before attempting clone
        source_bucket_exists = await self._check_bucket_exists(minio_connector, alias_name, source_bucket)
        if not source_bucket_exists:
            logger.warning(
                f"Source bucket '{source_bucket}' does not exist for deployment {source_deployment_name}. "
                "Creating target bucket without data migration."
            )

        # Check if target bucket already exists
        target_bucket_exists = await self._check_bucket_exists(minio_connector, alias_name, target_bucket)

        # Always check if target credentials already exist and validate/fix them
        existing_credentials = await self._validate_and_fix_credentials(
            minio_connector, alias_name, project_name, target_deployment_name, target_deployment
        )

        if existing_credentials:
            # Use existing valid credentials
            target_username_final, target_password = existing_credentials
            logger.info(f"Using existing validated credentials for {target_deployment_name}")
        else:
            # Generate new password for target (new deployment or credential fix failed)
            target_password = generate_secure_password(min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20)
            target_username_final = target_username

        # Handle bucket creation based on existence and force_clone flag
        # Generational approach: never delete, increment generation instead
        # Track if clone was actually performed and final generation used
        clone_performed = False
        final_generation = current_generation

        if target_bucket_exists and not force_clone:
            logger.info(
                f"Target MinIO bucket '{target_bucket}' already exists for {target_deployment_name}, "
                "skipping clone (use force_clone=True to override)"
            )
            # Ensure secret is still created even though we skip cloning
        elif target_bucket_exists and force_clone:
            # Generational approach: increment generation and create new versioned bucket
            new_generation = (current_generation or 0) + 1
            target_bucket = generate_bucket_name(project_name, target_deployment_name, new_generation)

            logger.info(
                f"force_clone=True: Using generational approach. "
                f"Creating new bucket {target_bucket} (generation {current_generation} -> {new_generation})"
            )

            # Create new versioned bucket (old bucket is preserved)
            await self._create_user_and_bucket_for_clone(
                minio_connector, alias_name, target_username_final, target_password, target_bucket, existing_credentials
            )

            # Copy data from source bucket to new versioned target bucket
            if source_bucket_exists:
                logger.info(f"Copying data from bucket {source_bucket} to {target_bucket}")
                await self._copy_bucket_data(minio_connector, alias_name, source_bucket, target_bucket)
            else:
                logger.info(f"Skipping data copy - source bucket {source_bucket} does not exist")

            # Update generation in project file
            project_file_handler = self.project_manager._project_file_handler
            project_file_handler.set_deployment_service_generation(
                project_data, target_deployment_name, ServiceType.MINIO_STORAGE.value, new_generation
            )
            logger.info(f"Updated bucket generation in project file: {new_generation}")

            # Record revision for the new generation
            self.project_manager._revision_manager.record_clone(
                project_data=project_data,
                deployment_name=target_deployment_name,
                service_type=ServiceType.MINIO_STORAGE.value,
                generation=new_generation,
                resource_name=target_bucket,
                source=f"deployment:{source_deployment_name}",
            )

            clone_performed = True
            final_generation = new_generation
        else:
            # Fresh clone - no existing bucket
            logger.info(
                f"No existing target MinIO bucket found for {target_deployment_name}, proceeding with fresh clone"
            )

            # Create user and bucket for cloning
            await self._create_user_and_bucket_for_clone(
                minio_connector, alias_name, target_username_final, target_password, target_bucket, existing_credentials
            )

            # Copy data from source bucket to target bucket only if source exists
            if source_bucket_exists:
                logger.info(f"Copying data from bucket {source_bucket} to {target_bucket}")
                await self._copy_bucket_data(minio_connector, alias_name, source_bucket, target_bucket)
            else:
                logger.info(f"Skipping data copy - source bucket {source_bucket} does not exist")

            # Record revision for initial creation
            initial_gen = current_generation if current_generation is not None else 0
            self.project_manager._revision_manager.record_clone(
                project_data=project_data,
                deployment_name=target_deployment_name,
                service_type=ServiceType.MINIO_STORAGE.value,
                generation=initial_gen,
                resource_name=target_bucket,
                source=f"deployment:{source_deployment_name}",
            )

            clone_performed = True
            final_generation = current_generation

        # Apply versioning configuration to cloned bucket (works on create, refresh, and update)
        minio_config = self._get_minio_service_config(project_data, target_deployment_name)
        await self._apply_bucket_versioning(minio_connector, alias_name, target_bucket, minio_config)

        cluster = target_deployment["cluster"]

        # Store credentials using existing secret logic
        minio_secret = MinIOSecret(
            host=get_minio_host(cluster),
            port=get_minio_port(cluster),
            access_key=target_username_final,
            secret_key=target_password,
            bucket_name=target_bucket,
            region=settings.MINIO_REGION,
        )
        self.project_manager._add_secret_to_create(
            target_deployment_name,
            "minio",
            minio_secret,
        )

        logger.info(f"MinIO clone credentials stored for deployment {target_deployment_name}")

        # Report clone to project_manager for status tracking (only if clone was actually performed)
        if clone_performed:
            self.project_manager.report_clone_performed(
                target_deployment_name, ServiceType.MINIO_STORAGE.value, final_generation
            )

    async def _create_user_and_bucket_for_clone(
        self,
        minio_connector: MinioConnector,
        alias_name: str,
        username: str,
        password: str,
        bucket_name: str,
        existing_credentials: tuple[str, str] | None,
    ) -> None:
        """
        Create MinIO user and bucket for clone operation if needed.

        Args:
            minio_connector: MinIO connector instance
            alias_name: MinIO alias name
            username: Username to create or use
            password: Password for the user
            bucket_name: Bucket name to create
            existing_credentials: Existing credentials if any
        """
        # Always ensure user exists for cloning operations
        # Even if we have existing credentials, the user might have been deleted during cleanup
        logger.info(f"Ensuring target MinIO user {username} exists for cloning")
        user_result = await minio_connector.create_user(alias_name, username, password)
        if user_result["status"] not in ["created", "exists"]:
            raise RuntimeError(f"Failed to create MinIO user {username}: {user_result.get('message', 'Unknown error')}")

        if user_result["status"] == "created":
            logger.info(f"Created new MinIO user: {username}")
        else:
            logger.info(f"MinIO user already exists: {username}")

        # Always ensure bucket exists and permissions are set up for cloning
        # This is crucial because mc mirror requires the target bucket to exist
        logger.info(f"Ensuring target bucket {bucket_name} exists for cloning")
        await self._ensure_bucket_and_permissions(minio_connector, alias_name, username, bucket_name)

    async def _copy_bucket_data(
        self, minio_connector: MinioConnector, alias_name: str, source_bucket: str, target_bucket: str
    ) -> None:
        """
        Copy all objects from source bucket to target bucket using mc mirror.

        Args:
            minio_connector: MinIO connector instance
            alias_name: MinIO alias name
            source_bucket: Source bucket name
            target_bucket: Target bucket name
        """
        try:
            # Use mc mirror command to copy all objects
            stdout, stderr, return_code = await minio_connector._run_mc_command(
                ["mirror", f"{alias_name}/{source_bucket}", f"{alias_name}/{target_bucket}"]
            )

            if return_code != 0:
                raise RuntimeError(f"MinIO mirror command failed: {stderr or 'Unknown error'}")

            logger.info(f"Successfully copied all objects from {source_bucket} to {target_bucket}")

        except Exception as e:
            logger.exception(f"Failed to copy bucket data from {source_bucket} to {target_bucket}")
            raise RuntimeError(f"Failed to copy bucket data: {e}") from e

    async def _check_bucket_exists(self, minio_connector: MinioConnector, alias_name: str, bucket_name: str) -> bool:
        """
        Check if a MinIO bucket exists.

        Args:
            minio_connector: MinIO connector instance
            alias_name: MinIO alias name
            bucket_name: Bucket name to check

        Returns:
            True if bucket exists, False otherwise
        """
        try:
            # List all buckets and check if our target bucket is in the list
            bucket_list = await minio_connector.list_buckets(alias_name)

            # bucket_list could be a list of dicts or an error dict
            if isinstance(bucket_list, list):
                bucket_names = [bucket.get("name", "") for bucket in bucket_list]
                return bucket_name in bucket_names
            else:
                # If we get an error dict, assume bucket doesn't exist
                logger.debug(f"Error listing buckets: {bucket_list}")
                return False

        except Exception as e:
            logger.debug(f"Error checking bucket existence for {bucket_name}: {e}")
            return False

    async def _delete_existing_target_resources(
        self,
        minio_connector: MinioConnector,
        alias_name: str,
        project_name: str,
        deployment_name: str,
        delete_user: bool = False,
    ) -> None:
        """
        Delete existing target MinIO resources (bucket and policy) for force clone.

        Optionally deletes the user if delete_user=True (when we don't have valid credentials).

        Args:
            minio_connector: MinIO connector instance
            alias_name: MinIO alias name
            project_name: Project name
            deployment_name: Deployment name
            delete_user: If True, also delete the user (only when credentials are invalid)
        """
        target_username = generate_minio_username(project_name, deployment_name)
        target_bucket = generate_bucket_name(project_name, deployment_name, None)

        if delete_user:
            logger.info(
                f"Deleting existing MinIO resources: bucket '{target_bucket}', policy, and user '{target_username}'"
            )
        else:
            logger.info(
                f"Deleting existing MinIO resources: bucket '{target_bucket}' and policy "
                f"(keeping user '{target_username}')"
            )

        # STEP 1: Detach policy from user (required before deleting policy)
        policy_name = generate_minio_policy_name(project_name, deployment_name)
        try:
            detach_result = await minio_connector.detach_policy(alias_name, policy_name, target_username)
            if detach_result["status"] == "success":
                logger.info(f"Detached policy {policy_name} from user {target_username}")
            else:
                logger.debug(f"Could not detach policy (may not be attached): {detach_result.get('message')}")
        except Exception as e:
            logger.debug(f"Error detaching policy (may not be attached): {e}")

        # STEP 2: Delete policy
        try:
            policy_result = await minio_connector.remove_policy(alias_name, policy_name)
            if policy_result["status"] == "success":
                logger.info(f"Deleted existing policy: {policy_name}")
            elif policy_result["status"] == "not_found":
                logger.info(f"Policy {policy_name} was already deleted or does not exist")
        except Exception as e:
            logger.debug(f"Error deleting policy (may not exist): {e}")

        # STEP 3: Delete bucket
        try:
            bucket_result = await minio_connector.delete_bucket(alias_name, target_bucket, force=True)
            if bucket_result["status"] in ["deleted", "success"]:
                logger.info(f"Deleted existing bucket: {target_bucket}")
            elif bucket_result["status"] == "not_found":
                logger.info(f"Bucket {target_bucket} was already deleted or does not exist")
            else:
                logger.warning(
                    f"Could not delete bucket {target_bucket}: {bucket_result.get('message', 'Unknown error')}"
                )
        except Exception as e:
            logger.warning(f"Error deleting bucket {target_bucket}: {e}")

        # STEP 4: Only delete user if explicitly requested (when credentials are invalid)
        if delete_user:
            try:
                user_result = await minio_connector.delete_user(alias_name, target_username)
                if user_result["status"] in ["deleted", "success"]:
                    logger.info(f"Deleted existing user: {target_username}")
                elif user_result["status"] == "not_found":
                    logger.info(f"User {target_username} was already deleted or does not exist")
                else:
                    logger.warning(
                        f"Could not delete user {target_username}: {user_result.get('message', 'Unknown error')}"
                    )
            except Exception as e:
                logger.warning(f"Error deleting user {target_username}: {e}")

    async def _validate_and_fix_credentials(
        self,
        minio_connector: MinioConnector,
        alias_name: str,
        project_name: str,
        deployment_name: str,
        deployment: dict[str, Any],
    ) -> tuple[str, str] | None:
        """
        Validate existing MinIO credentials and fix them if needed.

        This is a reusable method that checks if existing credentials work,
        and updates/recreates them if authentication fails.

        Args:
            minio_connector: MinIO connector instance
            alias_name: MinIO alias name
            project_name: Name of the project
            deployment_name: Name of the deployment
            deployment: The deployment configuration

        Returns:
            Tuple of (access_key, secret_key) if credentials are valid/fixed, None if no credentials exist
        """
        # Check if MinIO secret already exists in Kubernetes
        existing_minio_secret = await self._get_existing_minio_credentials_from_k8s(deployment_name, deployment)

        if not existing_minio_secret:
            return None

        logger.info(f"Found existing MinIO credentials for {deployment_name}, validating...")

        # Generate resource identifiers (base names without generation)
        username = generate_minio_username(project_name, deployment_name)
        bucket_name = generate_bucket_name(project_name, deployment_name, None)

        # Test existing credentials
        credentials_valid = await self._test_minio_connection(
            minio_connector, existing_minio_secret.access_key, existing_minio_secret.secret_key, bucket_name
        )

        if credentials_valid:
            logger.info(f"Existing MinIO credentials are valid for {deployment_name}")
            return existing_minio_secret.access_key, existing_minio_secret.secret_key

        # Credentials are invalid, check if user exists to determine action
        user_exists = await self._check_minio_user_exists(minio_connector, alias_name, existing_minio_secret.access_key)

        if not user_exists:
            # User doesn't exist (fresh MinIO) - always recreate
            logger.warning(f"MinIO user {username} doesn't exist in MinIO (fresh deployment), creating new user")
            should_recreate = True
        elif settings.RECREATE_PASSWORD_ON_AUTHENTICATION_FAILURE:
            # User exists but credentials invalid - recreate because setting allows it
            logger.warning(f"MinIO credentials are invalid for {deployment_name}, recreating user")
            should_recreate = True
        else:
            # User exists, credentials invalid, setting prevents recreation
            logger.error(
                f"MinIO credentials are invalid for {deployment_name} and "
                "RECREATE_PASSWORD_ON_AUTHENTICATION_FAILURE is disabled"
            )
            return None

        if should_recreate:
            # Generate new password
            new_secret_key = generate_secure_password(min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20)

            # Delete and recreate user (required for MinIO to change password)
            delete_result = await minio_connector.delete_user(alias_name, username)
            if delete_result["status"] not in ["deleted", "not_found"]:
                logger.error(
                    f"Failed to delete existing MinIO user {username}: {delete_result.get('message', 'Unknown error')}"
                )
                return None
            elif delete_result["status"] == "deleted":
                logger.info(f"Deleted existing MinIO user: {username}")

            # Create user with new credentials
            user_result = await minio_connector.create_user(alias_name, username, new_secret_key)
            if user_result["status"] not in ["created", "exists"]:
                logger.error(f"Failed to recreate MinIO user {username}: {user_result.get('message', 'Unknown error')}")
                return None

            logger.info(f"Recreated MinIO user with new credentials: {username}")

            # Ensure bucket exists and permissions are granted
            await self._ensure_bucket_and_permissions(minio_connector, alias_name, username, bucket_name)

            return username, new_secret_key

        return None

    async def _ensure_bucket_and_permissions(
        self, minio_connector: MinioConnector, alias_name: str, username: str, bucket_name: str
    ) -> None:
        """
        Ensure bucket exists and user has proper permissions.

        Args:
            minio_connector: MinIO connector instance
            alias_name: MinIO alias name
            username: MinIO username
            bucket_name: Bucket name
        """
        # Create or verify bucket exists
        bucket_result = await minio_connector.create_bucket(alias_name, bucket_name)
        if bucket_result["status"] not in ["created", "exists"]:
            raise RuntimeError(
                f"Failed to create MinIO bucket {bucket_name}: {bucket_result.get('message', 'Unknown error')}"
            )

        if bucket_result["status"] == "created":
            logger.info(f"Created MinIO bucket: {bucket_name}")
        else:
            logger.info(f"MinIO bucket already exists: {bucket_name}")

        # Grant bucket access to user
        access_result = await minio_connector.grant_bucket_access(
            alias_name, username, bucket_name, ["read", "write", "delete", "list"]
        )

        if access_result["status"] not in ["granted", "attached"]:
            raise RuntimeError(
                f"Failed to grant bucket access to user {username}: {access_result.get('message', 'Unknown error')}"
            )

        logger.info(f"Granted full access on bucket {bucket_name} to user {username}")

    @staticmethod
    async def _validate_external_source(
        source_host: str,
        source_port: int,
        source_access_key: str,
        source_secret_key: str,
        source_bucket: str,
        source_secure: bool,
    ) -> dict[str, Any]:
        """
        Validate external source MinIO connectivity and bucket existence.

        Single responsibility: Connect to external source and verify bucket exists.

        Args:
            source_host: External source MinIO host
            source_port: External source MinIO port
            source_access_key: Access key for external source connection
            source_secret_key: Secret key for external source connection
            source_bucket: Source bucket name
            source_secure: Whether source uses HTTPS

        Returns:
            Dictionary with validation results:
                - bucket_exists: bool - Whether the bucket exists
                - object_count: int | None - Number of objects in bucket (if accessible)

        Raises:
            Exception: If connection or validation fails
        """
        logger.info(f"Validating external source: {source_host}:{source_port}/{source_bucket}")

        minio_connector = None
        temp_alias = None

        try:
            # Create MinIO connector for validation
            minio_connector = create_minio_connector()

            # Configure temporary alias for external source
            temp_alias = f"validate-external-{source_host.replace('.', '-')}-{source_port}"
            alias_configured = await minio_connector.configure_alias(
                alias=temp_alias,
                host=f"{source_host}:{source_port}",
                access_key=source_access_key,
                secret_key=source_secret_key,
                secure=source_secure,
            )

            if not alias_configured:
                raise Exception(f"Failed to connect to external MinIO at {source_host}:{source_port}")

            # List buckets to verify connectivity and bucket existence
            bucket_list = await minio_connector.list_buckets(temp_alias)

            bucket_exists = False
            if isinstance(bucket_list, list):
                bucket_names = [bucket.get("name", "") for bucket in bucket_list]
                bucket_exists = source_bucket in bucket_names

            if not bucket_exists:
                raise Exception(f"Source bucket '{source_bucket}' does not exist at {source_host}:{source_port}")

            logger.info(f"External source validated: {source_host}:{source_port}/{source_bucket}")

            # Note: Getting object count would require additional mc commands
            # For now, we just validate connectivity and bucket existence
            return {"bucket_exists": bucket_exists, "object_count": None}

        except Exception as e:
            raise Exception(
                f"Failed to validate external source {source_host}:{source_port}/{source_bucket}: {e!s}"
            ) from e
        finally:
            # Cleanup temporary alias
            if minio_connector and temp_alias:
                try:
                    await minio_connector._run_mc_command(["alias", "rm", temp_alias])
                    logger.debug(f"Removed validation alias: {temp_alias}")
                except Exception as cleanup_error:
                    logger.warning(f"Failed to cleanup validation alias {temp_alias}: {cleanup_error}")

    async def _handle_remote_source_clone(
        self,
        project_name: str,
        deployment_name: str,
        remote_source_name: str,
        project_data: dict[str, Any],
        force_clone: bool,
    ) -> None:
        """
        Handle MinIO cloning from a remote source.

        Args:
            project_name: Name of the target project
            deployment_name: Name of the target deployment
            remote_source_name: Name of the remote source in project config
            project_data: Project configuration data
            force_clone: If True, overwrite existing bucket
        """
        from opi.utils.age import decrypt_password_smart, get_decoded_project_private_key

        remote_source = self._get_remote_source_config(project_data, remote_source_name)
        if not remote_source:
            raise ValueError(f"Remote source '{remote_source_name}' not found in project configuration")

        minio_config = remote_source.get("services", {}).get("minio-storage", {})
        if not minio_config:
            logger.debug(f"No minio-storage service in remote source '{remote_source_name}', skipping MinIO clone")
            return

        chisel_config = remote_source.get("chisel")

        # Decrypt credentials using project's AGE key
        project_private_key = await get_decoded_project_private_key(project_data)
        access_key = await decrypt_password_smart(minio_config.get("access-key", ""), project_private_key)
        secret_key = await decrypt_password_smart(minio_config.get("secret-key", ""), project_private_key)

        logger.info(f"Cloning MinIO from remote source '{remote_source_name}' for deployment {deployment_name}")

        result = await self.clone_bucket_from_external_source(
            project_name=project_name,
            deployment_name=deployment_name,
            source_host=minio_config["host"],
            source_port=minio_config.get("port", 9000),
            source_access_key=access_key,
            source_secret_key=secret_key,
            source_bucket=minio_config["bucket"],
            source_secure=minio_config.get("secure", False),
            force_clone=force_clone,
            chisel_config=chisel_config,
            project_data=project_data,
        )

        if not result.get("success"):
            raise RuntimeError(f"Remote source MinIO clone failed: {result.get('errors', ['Unknown error'])}")

        # Report clone to project_manager for status tracking
        # Get generation from project file (updated during clone if needed)
        generation = self._get_deployment_bucket_generation(project_data, deployment_name)
        self.project_manager.report_clone_performed(deployment_name, ServiceType.MINIO_STORAGE.value, generation)

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

    async def _clone_bucket_with_chisel_tunnel(
        self,
        project_name: str,
        deployment_name: str,
        source_host: str,
        source_port: int,
        source_access_key: str,
        source_secret_key: str,
        source_bucket: str,
        source_secure: bool,
        force_clone: bool,
        chisel_config: dict[str, Any],
        project_data: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """
        Clone MinIO bucket from external source via Chisel tunnel.

        Internal method that handles tunnel setup/teardown and delegates the actual
        clone operation to the direct clone implementation.
        """
        from opi.utils.chisel_helper import chisel_tunnel

        logger.info(
            f"Starting MinIO clone via Chisel tunnel: {project_name}/{deployment_name} "
            f"<- {source_host}:{source_port}/{source_bucket} "
            f"(via {chisel_config['server-url']})"
        )

        async with chisel_tunnel(chisel_config, source_host, source_port, project_data) as endpoint:
            logger.info(f"Tunnel established: {endpoint['host']}:{endpoint['port']} -> {source_host}:{source_port}")

            # Call the direct clone method with tunneled connection
            result = await self._execute_external_bucket_clone(
                project_name=project_name,
                deployment_name=deployment_name,
                source_host=endpoint["host"],
                source_port=endpoint["port"],
                source_access_key=source_access_key,
                source_secret_key=source_secret_key,
                source_bucket=source_bucket,
                source_secure=source_secure,
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

    async def clone_bucket_from_external_source(
        self,
        project_name: str,
        deployment_name: str,
        source_host: str,
        source_port: int,
        source_access_key: str,
        source_secret_key: str,
        source_bucket: str,
        source_secure: bool = False,
        force_clone: bool = False,
        chisel_config: dict[str, Any] | None = None,
        project_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Orchestrate cloning a MinIO bucket from an external source into a target deployment.

        This orchestrator method validates, prepares, and executes cross-cluster bucket cloning
        by calling existing single-responsibility methods in sequence.

        If chisel_config is provided, a Chisel tunnel is automatically established to reach
        the source MinIO through the tunnel.

        Args:
            project_name: Name of the target project
            deployment_name: Name of the target deployment
            source_host: External source MinIO host (e.g., localhost for port-forward)
            source_port: External source MinIO port (e.g., 19000)
            source_access_key: Access key for external source connection
            source_secret_key: Secret key for external source connection
            source_bucket: Source bucket name
            source_secure: Whether source uses HTTPS (default: False)
            force_clone: If True, overwrite existing target bucket (default: False)
            chisel_config: Optional Chisel tunnel configuration dict with keys:
                - server-url: Chisel server URL
                - username: Chisel auth username
                - password: Chisel auth password (may be encrypted)
            project_data: Optional project data for password decryption context

        Returns:
            Dictionary containing operation results with status and operations list

        Example:
            >>> result = await minio_manager.clone_bucket_from_external_source(
            ...     project_name="amt",
            ...     deployment_name="production",
            ...     source_host="localhost",
            ...     source_port=19000,
            ...     source_access_key="minioadmin",
            ...     source_secret_key="minioadmin",
            ...     source_bucket="amt-staging",
            ...     source_secure=False,
            ...     force_clone=True
            ... )
        """
        if chisel_config:
            # Use Chisel tunnel for the connection
            return await self._clone_bucket_with_chisel_tunnel(
                project_name=project_name,
                deployment_name=deployment_name,
                source_host=source_host,
                source_port=source_port,
                source_access_key=source_access_key,
                source_secret_key=source_secret_key,
                source_bucket=source_bucket,
                source_secure=source_secure,
                force_clone=force_clone,
                chisel_config=chisel_config,
                project_data=project_data,
            )

        # Direct connection (no tunnel)
        return await self._execute_external_bucket_clone(
            project_name=project_name,
            deployment_name=deployment_name,
            source_host=source_host,
            source_port=source_port,
            source_access_key=source_access_key,
            source_secret_key=source_secret_key,
            source_bucket=source_bucket,
            source_secure=source_secure,
            force_clone=force_clone,
        )

    async def _execute_external_bucket_clone(
        self,
        project_name: str,
        deployment_name: str,
        source_host: str,
        source_port: int,
        source_access_key: str,
        source_secret_key: str,
        source_bucket: str,
        source_secure: bool,
        force_clone: bool,
    ) -> dict[str, Any]:
        """
        Execute the actual bucket clone operation.

        Internal method that performs the clone without tunnel handling.
        Called by clone_bucket_from_external_source for direct connections
        and by _clone_bucket_with_chisel_tunnel for tunneled connections.
        """
        logger.info(
            f"Starting external bucket clone: {project_name}/{deployment_name} "
            f"<- {source_host}:{source_port}/{source_bucket}"
        )

        result = {
            "project": project_name,
            "deployment": deployment_name,
            "source": {"host": source_host, "port": source_port, "bucket": source_bucket},
            "operations": [],
            "success": False,
            "errors": [],
        }

        try:
            # STEP 1: Validate external source
            try:
                validation = await self._validate_external_source(
                    source_host, source_port, source_access_key, source_secret_key, source_bucket, source_secure
                )
                result["operations"].append(
                    {
                        "type": "source_validation",
                        "status": "success",
                        "bucket_exists": validation["bucket_exists"],
                    }
                )
            except Exception as e:
                logger.error(f"Source validation failed: {e}")
                result["errors"].append(f"Source validation failed: {e!s}")
                result["operations"].append({"type": "source_validation", "status": "failed", "error": str(e)})
                return result

            # STEP 2: Validate target deployment
            try:
                project_data = await self.project_manager.get_contents()
                if not project_data:
                    raise Exception(f"Project '{project_name}' not found")

                deployment = Project(project_data).find("deployments", name=deployment_name)
                if not deployment:
                    raise Exception(f"Deployment '{deployment_name}' not found")

                if not await self._deployment_uses_minio(project_data, deployment_name):
                    raise Exception(f"Deployment '{deployment_name}' does not use MinIO service")

                result["operations"].append({"type": "target_validation", "status": "success"})
            except Exception as e:
                logger.error(f"Target validation failed: {e}")
                result["errors"].append(f"Target validation failed: {e!s}")
                result["operations"].append({"type": "target_validation", "status": "failed", "error": str(e)})
                return result

            # STEP 3: Resolve target credentials and generation (generational approach)
            target_username = generate_minio_username(project_name, deployment_name)

            # Get current generation from project file
            current_generation = self._get_deployment_bucket_generation(project_data, deployment_name)
            target_bucket = generate_bucket_name(project_name, deployment_name, current_generation)
            generation_was_incremented = False
            new_generation = current_generation  # Will be updated if incremented

            try:
                minio_connector = create_minio_connector()
                alias_name = "default-minio"

                # Configure MinIO alias
                alias_configured = await minio_connector.configure_alias(
                    alias=alias_name,
                    host=settings.MINIO_HOST,
                    access_key=settings.MINIO_ADMIN_ACCESS_KEY,
                    secret_key=settings.MINIO_ADMIN_SECRET_KEY,
                    secure=settings.MINIO_USE_TLS,
                    region=settings.MINIO_REGION,
                )

                if not alias_configured:
                    raise RuntimeError(f"Failed to configure MinIO alias '{alias_name}' for {settings.MINIO_HOST}")

                # Validate and fix credentials
                existing_credentials = await self._validate_and_fix_credentials(
                    minio_connector, alias_name, project_name, deployment_name, deployment
                )

                if existing_credentials:
                    target_username_final, target_password = existing_credentials
                    logger.info(f"Using existing validated credentials for {deployment_name}")
                else:
                    # Generate new credentials
                    target_password = generate_secure_password(
                        min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20
                    )
                    target_username_final = target_username

                result["target"] = {"bucket": target_bucket, "username": target_username_final}
                result["operations"].append({"type": "credentials_resolved", "status": "success"})
            except Exception as e:
                logger.error(f"Credential resolution failed: {e}")
                result["errors"].append(f"Credential resolution failed: {e!s}")
                result["operations"].append({"type": "credentials_resolved", "status": "failed", "error": str(e)})
                return result

            # STEP 4: Handle bucket creation with generational approach
            # Check if target bucket already exists
            target_bucket_exists = await self._check_bucket_exists(minio_connector, alias_name, target_bucket)

            if target_bucket_exists and force_clone:
                # Generational approach: increment generation and create new versioned bucket
                new_generation = (current_generation or 0) + 1
                target_bucket = generate_bucket_name(project_name, deployment_name, new_generation)
                generation_was_incremented = True

                logger.info(
                    f"force_clone=True: Using generational approach. "
                    f"Creating new bucket {target_bucket} (generation {current_generation} -> {new_generation})"
                )
                result["operations"].append(
                    {
                        "type": "generation_incremented",
                        "status": "success",
                        "old_generation": current_generation,
                        "new_generation": new_generation,
                    }
                )

                # Update result target with new bucket name
                result["target"]["bucket"] = target_bucket

            # STEP 5: Prepare target (reuse existing clone method)
            try:
                logger.info(f"Preparing target bucket and user for {deployment_name}: {target_bucket}")

                # Reuse the existing method that properly creates user, bucket, and permissions
                await self._create_user_and_bucket_for_clone(
                    minio_connector,
                    alias_name,
                    target_username_final,
                    target_password,
                    target_bucket,
                    existing_credentials,
                )

                result["operations"].append({"type": "target_prepared", "status": "success"})

                # Record revision for initial creation (if not already incremented)
                if not generation_was_incremented:
                    initial_gen = current_generation if current_generation is not None else 0
                    source_desc = f"external:{source_host}:{source_port}/{source_bucket}"
                    self.project_manager._revision_manager.record_clone(
                        project_data=project_data,
                        deployment_name=deployment_name,
                        service_type=ServiceType.MINIO_STORAGE.value,
                        generation=initial_gen,
                        resource_name=target_bucket,
                        source=source_desc,
                    )
            except Exception as e:
                logger.error(f"Target preparation failed: {e}")
                result["errors"].append(f"Target preparation failed: {e!s}")
                result["operations"].append({"type": "target_prepared", "status": "failed", "error": str(e)})
                return result

            # STEP 6: Mirror data from external source to target bucket
            temp_source_alias = None
            try:
                logger.info(f"Executing bucket clone from external source to {target_bucket}")

                # Configure temporary alias for external source
                temp_source_alias = f"external-source-{source_host.replace('.', '-')}-{source_port}"
                logger.info(f"Configuring temporary source alias: {temp_source_alias}")

                source_configured = await minio_connector.configure_alias(
                    alias=temp_source_alias,
                    host=f"{source_host}:{source_port}",
                    access_key=source_access_key,
                    secret_key=source_secret_key,
                    secure=source_secure,
                )

                if not source_configured:
                    raise Exception(f"Failed to configure temporary source alias for {source_host}:{source_port}")

                # Use simple mirror helper to copy data between aliases
                mirror_result = await minio_connector.mirror_bucket_cross_alias(
                    source_alias=temp_source_alias,
                    source_bucket=source_bucket,
                    target_alias=alias_name,
                    target_bucket=target_bucket,
                )

                if mirror_result["status"] != "success":
                    raise Exception(f"Mirror failed: {mirror_result.get('message')}")

                result["operations"].append({"type": "bucket_cloned", "status": "success"})

                # Apply versioning configuration after successful clone
                logger.info(f"Applying versioning configuration to cloned bucket {target_bucket}")
                minio_config = self._get_minio_service_config(project_data, deployment_name)
                await self._apply_bucket_versioning(minio_connector, alias_name, target_bucket, minio_config)
                result["operations"].append({"type": "versioning_applied", "status": "success"})

            except Exception as e:
                logger.error(f"Bucket clone failed: {e}")
                result["errors"].append(f"Bucket clone failed: {e!s}")
                result["operations"].append({"type": "bucket_cloned", "status": "failed", "error": str(e)})
                return result
            finally:
                # Cleanup temporary source alias (always run)
                if temp_source_alias:
                    try:
                        logger.debug(f"Removing temporary source alias: {temp_source_alias}")
                        await minio_connector._run_mc_command(["alias", "rm", temp_source_alias])
                        logger.debug(f"Removed temporary source alias: {temp_source_alias}")
                    except Exception as cleanup_error:
                        logger.warning(f"Failed to cleanup temporary alias {temp_source_alias}: {cleanup_error}")

            # STEP 7: Update project file if generation was incremented
            if generation_was_incremented:
                try:
                    project_file_handler = self.project_manager._project_file_handler
                    project_file_handler.set_deployment_service_generation(
                        project_data, deployment_name, ServiceType.MINIO_STORAGE.value, new_generation
                    )
                    logger.info(f"Updated bucket generation in project file: {new_generation}")

                    # Record revision for the new generation
                    source_desc = f"external:{source_host}:{source_port}/{source_bucket}"
                    self.project_manager._revision_manager.record_clone(
                        project_data=project_data,
                        deployment_name=deployment_name,
                        service_type=ServiceType.MINIO_STORAGE.value,
                        generation=new_generation,
                        resource_name=target_bucket,
                        source=source_desc,
                    )

                    result["operations"].append(
                        {
                            "type": "generation_updated_in_project",
                            "status": "success",
                            "generation": new_generation,
                        }
                    )
                except Exception as e:
                    logger.warning(f"Failed to update generation in project file: {e}")
                    result["operations"].append(
                        {
                            "type": "generation_updated_in_project",
                            "status": "warning",
                            "error": str(e),
                        }
                    )

            # STEP 8: Always store secret (bucket name might have changed due to generation)
            # With generational approach, we always need fresh secrets with correct bucket name
            try:
                logger.info(f"Storing credentials for {deployment_name} with bucket {target_bucket}")
                cluster = deployment["cluster"]
                minio_secret = MinIOSecret(
                    host=get_minio_host(cluster),
                    port=get_minio_port(cluster),
                    access_key=target_username_final,
                    secret_key=target_password,
                    bucket_name=target_bucket,
                    region=settings.MINIO_REGION,
                )
                self.project_manager._add_secret_to_create(deployment_name, "minio", minio_secret)
                result["operations"].append({"type": "credentials_stored_in_memory", "status": "success"})
            except Exception as e:
                logger.warning(f"Failed to store credentials in memory (clone succeeded): {e}")
                result["operations"].append(
                    {"type": "credentials_stored_in_memory", "status": "warning", "error": str(e)}
                )

            # STEP 9: Sync secrets to Git
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
                    # commit labelled "Update MinIO credentials".
                    await self.project_manager._process_application_manifests(deployment_name)

                    result["operations"].append(
                        {
                            "type": "secrets_synced_to_git",
                            "status": "success",
                            "message": "Updated MinIO credentials committed to Git for ArgoCD sync",
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
                            "message": (
                                "Bucket clone succeeded but failed to sync credentials to Git. "
                                "Manual sync may be required."
                            ),
                        }
                    )

            result["success"] = True
            logger.info(f"External bucket clone completed successfully: {project_name}/{deployment_name}")
            return result

        except Exception as e:
            logger.exception(f"Unexpected error during external clone: {e}")
            result["errors"].append(f"Unexpected error: {e!s}")
            return result
