"""MinIO service manager for handling object storage resources."""

import logging
from typing import Any

from jsonpath_ng.ext import parse as jsonpath_parse

from opi.connectors.minio_mc import MinioConnector, create_minio_connector
from opi.core.cluster_config import get_minio_server
from opi.core.config import settings
from opi.services import ServiceType
from opi.utils.naming import generate_bucket_name, generate_minio_username
from opi.utils.passwords import generate_secure_password
from opi.utils.secrets import MinIOSecret

logger = logging.getLogger(__name__)


class MinioManager:
    """Manager for MinIO object storage operations and resources."""

    def __init__(self, project_manager: "ProjectManager") -> None:
        """
        Initialize the MinioManager with reference to ProjectManager.

        Args:
            project_manager: The main ProjectManager instance for accessing shared resources
        """
        self.project_manager = project_manager

    async def create_resources_for_deployment(self, project_data: dict[str, Any], deployment: dict[str, Any]) -> None:
        """
        Create MinIO object storage resources for a deployment that has MinIO storage enabled.

        If the deployment has a 'clone-from' field, it will clone resources from the specified
        source deployment instead of creating new ones.

        Args:
            project_data: The project configuration data
            deployment: The specific deployment configuration
        """
        project_name = project_data["name"]
        deployment_name = deployment["name"]

        # Check if this deployment has MinIO service enabled
        if not await self._deployment_uses_minio(project_data, deployment_name):
            logger.debug(f"Deployment {deployment_name} does not use MinIO service, skipping")
            return

        # Check if this deployment should be cloned from another deployment
        clone_from = deployment.get("clone-from")
        if clone_from:
            logger.info(f"Deployment {deployment_name} has clone-from: {clone_from}, using clone instead of create")
            await self.clone_minio_from_deployment(project_data, deployment, clone_from)
            return

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

            # Generate consistent MinIO identifiers
            minio_username = generate_minio_username(project_name, deployment_name)
            bucket_name = generate_bucket_name(project_name, deployment_name)

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
                    if settings.RECREATE_PASSWORD_ON_AUTHENTICATION_FAILURE:
                        # Remove old user and create new one
                        # (MinIO requires user removal before creating with new password)
                        logger.warning(
                            f"MinIO credentials are invalid for {project_name}/{deployment_name}, "
                            f"removing old user and creating new one"
                        )

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
                        logger.error(
                            f"MinIO secret exists for {project_name}/{deployment_name} but credentials are invalid. "
                            f"Manual intervention required to fix MinIO user or update secret."
                        )
                        return
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

            minio_server_host = get_minio_server(deployment["cluster"])

            # Store MinIO credentials in private secrets map instead of project data
            minio_secret = MinIOSecret(
                host=minio_server_host,
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
        project_name = project_data["name"]
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
            bucket_name = generate_bucket_name(project_name, deployment_name)

            # Remove bucket access from user first
            try:
                policy_name = self._generate_policy_name(project_name, deployment_name)
                revoke_result = await minio_connector.detach_policy(alias_name, policy_name, minio_username)

                if revoke_result["status"] == "success":
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
                policy_name = self._generate_policy_name(project_name, deployment_name)
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
                policy_name = self._generate_policy_name(project_name, deployment_name)  # Ensure policy_name is defined
                deletion_results["operations"].append(
                    {"type": "minio_policy_deletion", "target": policy_name, "status": "error", "error": str(e)}
                )
                deletion_results["errors"].append(f"Error deleting policy {policy_name}: {e}")
                logger.exception("Error deleting MinIO policy")

            # Delete bucket (force=True to delete even if not empty)
            try:
                bucket_result = await minio_connector.delete_bucket(alias_name, bucket_name, force=True)

                if bucket_result["status"] == "success":
                    deletion_results["operations"].append(
                        {"type": "minio_bucket_deletion", "target": bucket_name, "status": "success"}
                    )
                    logger.info(f"Successfully deleted MinIO bucket: {bucket_name}")
                else:
                    deletion_results["operations"].append(
                        {
                            "type": "minio_bucket_deletion",
                            "target": bucket_name,
                            "status": "not_found" if "does not exist" in bucket_result.get("message", "") else "failed",
                            "error": bucket_result.get("message", "Unknown error"),
                        }
                    )
                    if "does not exist" not in bucket_result.get("message", ""):
                        deletion_results["errors"].append(
                            f"Failed to delete bucket {bucket_name}: {bucket_result.get('message')}"
                        )

            except Exception as e:
                deletion_results["operations"].append(
                    {"type": "minio_bucket_deletion", "target": bucket_name, "status": "error", "error": str(e)}
                )
                deletion_results["errors"].append(f"Error deleting bucket {bucket_name}: {e}")
                logger.exception("Error deleting MinIO bucket")

            # Delete user
            try:
                user_result = await minio_connector.delete_user(alias_name, minio_username)

                if user_result["status"] == "success":
                    deletion_results["operations"].append(
                        {"type": "minio_user_deletion", "target": minio_username, "status": "success"}
                    )
                    logger.info(f"Successfully deleted MinIO user: {minio_username}")
                else:
                    deletion_results["operations"].append(
                        {
                            "type": "minio_user_deletion",
                            "target": minio_username,
                            "status": "not_found" if "does not exist" in user_result.get("message", "") else "failed",
                            "error": user_result.get("message", "Unknown error"),
                        }
                    )
                    if "does not exist" not in user_result.get("message", ""):
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

    async def _deployment_uses_minio(self, project_data: dict[str, Any], deployment_name: str) -> bool:
        """
        Check if a deployment uses MinIO service.

        Args:
            project_data: The project configuration data
            deployment_name: Name of the deployment to check

        Returns:
            True if deployment uses MinIO service, False otherwise
        """
        # First get component references for this deployment
        component_refs_query = jsonpath_parse(f"$.deployments[?@.name=='{deployment_name}'].components[*].reference")
        component_refs = [match.value for match in component_refs_query.find(project_data)]

        # Then check if any of these components use MinIO service
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

            if ServiceType.MINIO_STORAGE.value in all_services:
                return True

        return False

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

    def _generate_policy_name(self, project_name: str, deployment_name: str) -> str:
        """
        Generate a consistent policy name for MinIO bucket access.

        Args:
            project_name: Project name
            deployment_name: Deployment name

        Returns:
            Policy name in format: {project}-{deployment}-policy
        """
        # Use bucket naming format for consistency but add policy suffix
        base_name = generate_bucket_name(project_name, deployment_name)
        return f"{base_name}-policy"

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

        This method only clones on initial setup when target resources don't exist yet,
        unless force_clone=True is specified.

        Args:
            project_data: The project configuration data
            target_deployment: The target deployment configuration
            source_deployment_name: Name of the source deployment to clone from
            force_clone: If True, clone even if target resources already exist (default: False)
        """
        project_name = project_data["name"]
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

        # Use existing naming utilities
        source_bucket = generate_bucket_name(project_name, source_deployment_name)
        target_username = generate_minio_username(project_name, target_deployment_name)
        target_bucket = generate_bucket_name(project_name, target_deployment_name)

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

        # Handle bucket creation/deletion based on existence and force_clone flag
        if target_bucket_exists and not force_clone:
            logger.info(
                f"Target MinIO bucket '{target_bucket}' already exists for {target_deployment_name}, "
                "skipping clone (use force_clone=True to override)"
            )
            # Ensure secret is still created even though we skip cloning
        else:
            # For cloning operations, always delete existing resources first to ensure clean state
            if target_bucket_exists:
                logger.info(f"Target MinIO bucket '{target_bucket}' exists, deleting before clone")
                await self._delete_existing_target_resources(
                    minio_connector, alias_name, project_name, target_deployment_name
                )
            else:
                logger.info(
                    f"No existing target MinIO bucket found for {target_deployment_name}, proceeding with fresh clone"
                )

            # Create user and bucket fresh for cloning
            await self._create_user_and_bucket_for_clone(
                minio_connector, alias_name, target_username_final, target_password, target_bucket, existing_credentials
            )

            # Copy data from source bucket to target bucket only if source exists
            if source_bucket_exists:
                logger.info(f"Copying data from bucket {source_bucket} to {target_bucket}")
                await self._copy_bucket_data(minio_connector, alias_name, source_bucket, target_bucket)
            else:
                logger.info(f"Skipping data copy - source bucket {source_bucket} does not exist")

        minio_server_host = get_minio_server(target_deployment["cluster"])

        # Store credentials using existing secret logic
        minio_secret = MinIOSecret(
            host=minio_server_host,
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
        self, minio_connector: MinioConnector, alias_name: str, project_name: str, deployment_name: str
    ) -> None:
        """
        Delete existing target MinIO resources (bucket and user) for force clone.

        Args:
            minio_connector: MinIO connector instance
            alias_name: MinIO alias name
            project_name: Project name
            deployment_name: Deployment name
        """
        target_username = generate_minio_username(project_name, deployment_name)
        target_bucket = generate_bucket_name(project_name, deployment_name)
        logger.info(f"Deleting existing MinIO resources: bucket '{target_bucket}' and user '{target_username}'")

        try:
            # Delete bucket first (will fail if not empty, which is expected)
            bucket_result = await minio_connector.delete_bucket(alias_name, target_bucket, force=True)
            if bucket_result["status"] == "success":
                logger.info(f"Deleted existing bucket: {target_bucket}")
            else:
                logger.warning(
                    f"Could not delete bucket {target_bucket}: {bucket_result.get('message', 'Unknown error')}"
                )
        except Exception as e:
            logger.warning(f"Error deleting bucket {target_bucket}: {e}")

        try:
            # Delete user
            user_result = await minio_connector.delete_user(alias_name, target_username)
            if user_result["status"] == "success":
                logger.info(f"Deleted existing user: {target_username}")
            else:
                logger.warning(
                    f"Could not delete user {target_username}: {user_result.get('message', 'Unknown error')}"
                )
        except Exception as e:
            logger.warning(f"Error deleting user {target_username}: {e}")

        # Also try to delete associated policy
        try:
            policy_name = self._generate_policy_name(project_name, deployment_name)
            policy_result = await minio_connector.remove_policy(alias_name, policy_name)
            if policy_result["status"] == "success":
                logger.info(f"Deleted existing policy: {policy_name}")
        except Exception as e:
            logger.debug(f"Error deleting policy (may not exist): {e}")

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

        # Generate resource identifiers
        username = generate_minio_username(project_name, deployment_name)
        bucket_name = generate_bucket_name(project_name, deployment_name)

        # Test existing credentials
        credentials_valid = await self._test_minio_connection(
            minio_connector, existing_minio_secret.access_key, existing_minio_secret.secret_key, bucket_name
        )

        if credentials_valid:
            logger.info(f"Existing MinIO credentials are valid for {deployment_name}")
            return existing_minio_secret.access_key, existing_minio_secret.secret_key

        # Credentials are invalid, need to fix them
        if settings.RECREATE_PASSWORD_ON_AUTHENTICATION_FAILURE:
            logger.warning(f"MinIO credentials are invalid for {deployment_name}, recreating user")

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
        else:
            logger.error(
                f"MinIO credentials are invalid for {deployment_name} and "
                "RECREATE_PASSWORD_ON_AUTHENTICATION_FAILURE is disabled"
            )
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
