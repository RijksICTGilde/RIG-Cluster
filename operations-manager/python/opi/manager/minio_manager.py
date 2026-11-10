"""MinIO service manager for handling object storage resources."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opi.manager.project_manager import ProjectManager
    from opi.services.marked_for_deletion_service import MarkedForDeletionService

from opi.connectors.minio_mc import MinioConnector, MinioExecutionError, create_minio_connector
from opi.core.cluster_config import get_minio_host, get_minio_port
from opi.core.config import settings
from opi.manager.backup.runs import clone_backup_items
from opi.manager.clone_validation import (
    composite_name_collision,
    other_project_deployments,
    resolve_clone_source_deployment,
)
from opi.services import CloneFromType, ServiceType
from opi.services.catalog.shared.noncurrent_versions import deployment_noncurrent_rule
from opi.services.project import Project
from opi.services.secrets import MinIOSecret
from opi.services.tasks import progress_steps as steps
from opi.utils.egress_guard import UNREACHABLE_SOURCE_REASON, clone_source_refusal
from opi.utils.naming import generate_bucket_name, generate_minio_policy_name, generate_minio_username
from opi.utils.passwords import generate_secure_password

logger = logging.getLogger(__name__)


class MinioManager:
    def __init__(self, project_manager: ProjectManager) -> None:
        self.project_manager = project_manager

    def _get_deployment_bucket_generation(self, project_data: dict[str, Any], deployment_name: str) -> int | None:
        """From ``deployments[name].services.minio-storage.generation``, or None when unset."""
        project_file_handler = self.project_manager._project_file_handler
        return project_file_handler.get_deployment_service_generation(
            project_data, deployment_name, ServiceType.MINIO_STORAGE.value
        )

    def _get_minio_service_config(self, project_data: dict[str, Any], deployment_name: str) -> dict[str, Any] | None:
        """The minio-storage config of a deployment, or None when it carries no config block.

        A bare ``- minio-storage`` entry therefore gives None; one with a ``config:`` block
        gives that block, e.g. ``{"enable-versioning": True}``.
        """
        deployments = project_data.get("deployments", [])
        deployment = Project.locate(deployments, name=deployment_name)

        if not deployment:
            logger.debug(f"Deployment {deployment_name} not found in project data")
            return None

        from opi.services.services import service_entry_config, service_entry_name

        services = project_data.get("services", [])

        # The minio-storage entry may be a bare string, a legacy single-key dict, or a
        # uniform record ({name, config}), so the name is read rather than matched on a key.
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
        """Apply bucket versioning when the config asks for it (idempotent).

        A ``config`` of None, or one that says nothing about versioning, changes nothing.

        Raises:
            RuntimeError: the versioning operation failed.
        """
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

    async def _apply_noncurrent_version_limit(
        self,
        minio_connector: MinioConnector,
        alias: str,
        bucket_name: str,
        project_data: dict[str, Any],
        deployment_name: str,
    ) -> None:
        """Put the limit on noncurrent versions on a bucket that has versioning (RC-380).

        Runs on every bucket with versioning, also one where versioning was turned on
        outside OPI: without a rule such a bucket keeps every old version forever. A
        bucket without versioning has no noncurrent versions, so it gets no rule.

        Raises:
            RuntimeError: if the rule cannot be read or written.
        """
        status = await minio_connector.get_bucket_versioning_status(alias, bucket_name)
        if not status["enabled"]:
            logger.debug(f"Bucket {bucket_name} has no versioning, so no limit on old versions")
            return

        rule = deployment_noncurrent_rule(project_data, deployment_name, ServiceType.MINIO_STORAGE)
        try:
            result = await minio_connector.ensure_noncurrent_version_rule(
                alias, bucket_name, max_versions=rule.max_versions, expire_days=rule.expire_days
            )
        except MinioExecutionError as e:
            raise RuntimeError(f"Failed to limit old versions on bucket {bucket_name}: {e}") from e

        logger.info(
            f"Bucket {bucket_name} keeps at most {rule.max_versions} old versions per object"
            f"{'' if rule.expire_days is None else f', at most {rule.expire_days} days old'}"
            f" ({result['status']})"
        )

    async def create_resources_for_deployment(
        self,
        project_data: dict[str, Any],
        deployment: dict[str, Any],
        force_clone_override: bool = False,
        rotate_secret_key: bool = False,
    ) -> None:
        """Create the MinIO resources of a deployment that has the MinIO service.

        A deployment with a ``clone-from`` clones from the source it names instead.

        Args:
            force_clone_override: Runtime override of force_clone, from the API.
            rotate_secret_key: Replace the stored secret key instead of reusing it.
        """
        project_name = await self.project_manager.get_name()
        deployment_name = deployment["name"]

        if not await self._deployment_uses_minio(project_data, deployment_name):
            logger.debug(f"Deployment {deployment_name} does not use MinIO service, skipping")
            return

        # Second layer: an existing MinIO user with this name gets a new secret key below,
        # so first check that the user belongs to this project.
        refusal = composite_name_collision(project_name, deployment_name, other_project_deployments(project_name))
        if refusal:
            raise ValueError(refusal)

        clone_from = deployment.get("clone-from")
        force_clone = force_clone_override or deployment.get("force-clone", False)

        restore_from_backup = False

        if clone_from:
            if not isinstance(clone_from, dict):
                raise ValueError(
                    f"clone-from for deployment '{deployment_name}' must be a dict with 'type', 'reference', "
                    f"and 'mode' keys, got: {type(clone_from).__name__} = {clone_from!r}"
                )

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
                    if not remote_source_name:
                        msg = f"clone-from van type remote-source zonder reference voor '{deployment_name}'"
                        raise ValueError(msg)
                    await self._handle_remote_source_clone(
                        project_name, deployment_name, remote_source_name, project_data, force_clone
                    )
                    return
                elif clone_type == CloneFromType.DEPLOYMENT.value:
                    source_deployment = str(clone_from.get("reference") or "")
                    logger.info(f"Deployment {deployment_name} has clone-from deployment: {source_deployment}")
                    await self.clone_minio_from_deployment(project_data, deployment, source_deployment)
                    return
                elif clone_type == CloneFromType.BACKUP.value:
                    restore_from_backup = True
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
            minio_task = progress_manager.add_task(steps.STORAGE_CONFIG_APPLY, subject=deployment_name)

        try:
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
                raise RuntimeError(f"Failed to configure MinIO alias '{alias_name}' for {settings.MINIO_HOST}")

            generation = self._get_deployment_bucket_generation(project_data, deployment_name)
            if generation is not None and generation > 0:
                logger.info(f"Using bucket generation {generation} for {deployment_name}")

            minio_username = generate_minio_username(project_name, deployment_name)
            bucket_name = generate_bucket_name(project_name, deployment_name, generation)

            existing_minio_secret = await self._get_existing_minio_credentials_from_k8s(deployment_name, deployment)

            if existing_minio_secret:
                logger.info(f"Found existing MinIO secret in Kubernetes for {project_name}/{deployment_name}")

                if rotate_secret_key:
                    logger.info(f"Replacing MinIO secret key for {project_name}/{deployment_name} on request")
                    credentials_valid = False
                else:
                    logger.info(f"Testing existing MinIO credentials for {project_name}/{deployment_name}")
                    credentials_valid = await self._test_minio_connection(
                        minio_connector, existing_minio_secret.access_key, existing_minio_secret.secret_key, bucket_name
                    )

                if not credentials_valid:
                    if rotate_secret_key:
                        should_recreate = True
                    else:
                        user_exists = await self._check_minio_user_exists(
                            minio_connector, alias_name, existing_minio_secret.access_key
                        )

                        if not user_exists:
                            logger.warning(
                                f"MinIO user {minio_username} doesn't exist in MinIO (fresh deployment), "
                                f"creating new user with existing credentials from secret"
                            )
                            should_recreate = True
                        elif settings.RECREATE_PASSWORD_ON_AUTHENTICATION_FAILURE:
                            logger.warning(
                                f"MinIO credentials are invalid for {project_name}/{deployment_name}, "
                                f"removing old user and creating new one"
                            )
                            should_recreate = True
                        else:
                            logger.error(
                                f"MinIO secret exists for {project_name}/{deployment_name} but credentials are invalid. "
                                f"Manual intervention required to fix MinIO user or update secret."
                            )
                            return

                    if should_recreate:
                        minio_secret_key = generate_secure_password(
                            min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20
                        )

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

                        user_result = await minio_connector.create_user(alias_name, minio_username, minio_secret_key)
                        if user_result["status"] not in ["created", "exists"]:
                            logger.error(
                                f"Failed to recreate MinIO user {minio_username}: "
                                f"{user_result.get('message', 'Unknown error')}"
                            )
                            return
                        logger.info(f"Recreated MinIO user with new credentials: {minio_username}")

                        access_key = minio_username
                        secret_key = minio_secret_key

                        need_bucket_setup = True
                else:
                    logger.info(f"Existing MinIO credentials are valid for {project_name}/{deployment_name}")
                    access_key = existing_minio_secret.access_key
                    secret_key = existing_minio_secret.secret_key
                    need_bucket_setup = False
            else:
                logger.info(f"No MinIO secret found in Kubernetes for {project_name}/{deployment_name}")

                minio_username = generate_minio_username(project_name, deployment_name)
                minio_secret_key = generate_secure_password(
                    min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20
                )

                user_result = await minio_connector.create_user(alias_name, minio_username, minio_secret_key)

                if user_result["status"] == "error":
                    logger.error(
                        f"Failed to create MinIO user {minio_username}: {user_result.get('message', 'Unknown error')}"
                    )
                    return
                elif user_result["status"] == "exists":
                    logger.info(f"MinIO user {minio_username} already exists, updating secret key")

                    minio_secret_key = generate_secure_password(
                        min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20
                    )

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

                access_key = minio_username
                secret_key = minio_secret_key
                need_bucket_setup = True

            if need_bucket_setup:
                logger.info(f"Setting up bucket and permissions for {minio_username}")

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

            minio_config = self._get_minio_service_config(project_data, deployment_name)
            await self._apply_bucket_versioning(minio_connector, alias_name, bucket_name, minio_config)
            await self._apply_noncurrent_version_limit(
                minio_connector, alias_name, bucket_name, project_data, deployment_name
            )

            cluster = deployment["cluster"]

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

            if restore_from_backup and isinstance(clone_from, dict):
                backup_items = await clone_backup_items(project_data, project_name, deployment, clone_from)
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
        """Delete the MinIO resources of a deployment, and report the result."""
        project_name = await self.project_manager.get_name()
        deployment_name = deployment["name"]

        deletion_results = {
            "service": "minio",
            "deployment": deployment_name,
            "operations": [],
            "success": True,
            "errors": [],
        }

        # Cleanup asks the BROAD question: "does this deployment currently use MinIO" reads
        # the current project file, while the bucket was created under an earlier one, so a
        # deployment rewired away from the service would outlive its resources. Deleting is
        # idempotent, so err inclusive; only a project that never declared MinIO anywhere is
        # skipped, which keeps a MinIO connection out of every unrelated deployment delete.
        if not self.project_manager._project_file_handler.project_declares_service(
            project_data, [ServiceType.MINIO_STORAGE.value]
        ):
            deletion_results["operations"].append(
                {"type": "minio_cleanup", "status": "skipped", "reason": "Project does not declare the MinIO service"}
            )
            logger.debug(f"Project {project_name} does not declare MinIO anywhere, skipping MinIO cleanup")
            return deletion_results

        logger.info(f"Deleting MinIO resources for project: {project_name}, deployment: {deployment_name}")

        try:
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
                deletion_results["success"] = False
                deletion_results["errors"].append(f"Failed to configure MinIO alias '{alias_name}'")
                return deletion_results

            minio_username = generate_minio_username(project_name, deployment_name)

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

            bucket_name = buckets_to_delete[0]

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
                policy_name = generate_minio_policy_name(project_name, deployment_name)
                deletion_results["operations"].append(
                    {"type": "minio_policy_deletion", "target": policy_name, "status": "error", "error": str(e)}
                )
                deletion_results["errors"].append(f"Error deleting policy {policy_name}: {e}")
                logger.exception("Error deleting MinIO policy")

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
        """Clean up when the MinIO service is removed from a deployment.

        Args:
            deployment_data: The deployment dict from the PREVIOUS YAML.
            project_data: The PREVIOUS project YAML, so the internal service checks
                still pass.
            marked_for_deletion_service: Given, the bucket, user and policy are marked for
                deferred deletion; left out, they are deleted at once.
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
        return self.project_manager._project_file_handler.deployment_uses_service(
            project_data,
            deployment_name,
            [ServiceType.MINIO_STORAGE.value],
        )

    async def _get_existing_minio_credentials_from_k8s(
        self, deployment_name: str, deployment: dict[str, Any]
    ) -> MinIOSecret | None:
        try:
            from opi.core.cluster_config import get_prefixed_namespace
            from opi.core.config import settings

            secret_name = MinIOSecret.get_secret_name(deployment_name)
            kubectl_connector = self.project_manager._kubectl_connector

            namespace = get_prefixed_namespace(settings.CLUSTER_MANAGER, deployment["namespace"])

            secret_data = await kubectl_connector.get_secret(secret_name, namespace)
            if secret_data:
                return MinIOSecret.from_k8s_secret_data(secret_data)
            return None
        except Exception as e:
            logger.debug(f"Could not retrieve MinIO secret for {deployment_name}: {e}")
            return None

    async def _check_minio_user_exists(self, minio_connector: MinioConnector, alias: str, username: str) -> bool:
        try:
            users = await minio_connector.list_users(alias)
            user_exists = any(user.get("username") == username or user.get("access_key") == username for user in users)
            logger.debug(f"MinIO user {username} exists: {user_exists}")
            return user_exists
        except Exception as e:
            logger.debug(f"Error checking if MinIO user exists: {e}")
            return False

    async def _test_minio_connection(
        self, minio_connector: MinioConnector, access_key: str, secret_key: str, bucket_name: str
    ) -> bool:
        """Whether these credentials can still reach ``bucket_name``."""
        try:
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
        """Clone the MinIO resources of one deployment into another.

        An existing bucket is never destroyed: ``force_clone`` creates a new versioned
        bucket next to it, so the old data stays and a rollback remains possible.

        Raises:
            ValueError: if the source is not a deployment of this project.
        """
        project_name = await self.project_manager.get_name()
        target_deployment_name = target_deployment["name"]
        force_clone = target_deployment.get("force-clone", False)

        resolve_clone_source_deployment(project_data, source_deployment_name, target_deployment_name)

        logger.info(
            f"Cloning MinIO resources from {source_deployment_name} to {target_deployment_name} "
            f"for project {project_name}"
        )

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
            raise RuntimeError(f"Failed to configure MinIO alias '{alias_name}' for {settings.MINIO_HOST}")

        current_generation = self._get_deployment_bucket_generation(project_data, target_deployment_name)

        source_bucket = generate_bucket_name(project_name, source_deployment_name, None)

        target_username = generate_minio_username(project_name, target_deployment_name)
        target_bucket = generate_bucket_name(project_name, target_deployment_name, current_generation)

        logger.info(f"Target bucket will be: {target_bucket} (generation={current_generation})")

        source_bucket_exists = await self._check_bucket_exists(minio_connector, alias_name, source_bucket)
        if not source_bucket_exists:
            logger.warning(
                f"Source bucket '{source_bucket}' does not exist for deployment {source_deployment_name}. "
                "Creating target bucket without data migration."
            )

        target_bucket_exists = await self._check_bucket_exists(minio_connector, alias_name, target_bucket)

        existing_credentials = await self._validate_and_fix_credentials(
            minio_connector, alias_name, project_name, target_deployment_name, target_deployment
        )

        if existing_credentials:
            target_username_final, target_password = existing_credentials
            logger.info(f"Using existing validated credentials for {target_deployment_name}")
        else:
            target_password = generate_secure_password(min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20)
            target_username_final = target_username

        clone_performed = False
        final_generation = current_generation

        if target_bucket_exists and not force_clone:
            logger.info(
                f"Target MinIO bucket '{target_bucket}' already exists for {target_deployment_name}, "
                "skipping clone (use force_clone=True to override)"
            )
        elif target_bucket_exists and force_clone:
            new_generation = (current_generation or 0) + 1
            target_bucket = generate_bucket_name(project_name, target_deployment_name, new_generation)

            logger.info(
                f"force_clone=True: Using generational approach. "
                f"Creating new bucket {target_bucket} (generation {current_generation} -> {new_generation})"
            )

            await self._create_user_and_bucket_for_clone(
                minio_connector, alias_name, target_username_final, target_password, target_bucket, existing_credentials
            )

            if source_bucket_exists:
                logger.info(f"Copying data from bucket {source_bucket} to {target_bucket}")
                await self._copy_bucket_data(minio_connector, alias_name, source_bucket, target_bucket)
            else:
                logger.info(f"Skipping data copy - source bucket {source_bucket} does not exist")

            project_file_handler = self.project_manager._project_file_handler
            project_file_handler.set_deployment_service_generation(
                project_data, target_deployment_name, ServiceType.MINIO_STORAGE.value, new_generation
            )
            logger.info(f"Updated bucket generation in project file: {new_generation}")

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
            logger.info(
                f"No existing target MinIO bucket found for {target_deployment_name}, proceeding with fresh clone"
            )

            await self._create_user_and_bucket_for_clone(
                minio_connector, alias_name, target_username_final, target_password, target_bucket, existing_credentials
            )

            if source_bucket_exists:
                logger.info(f"Copying data from bucket {source_bucket} to {target_bucket}")
                await self._copy_bucket_data(minio_connector, alias_name, source_bucket, target_bucket)
            else:
                logger.info(f"Skipping data copy - source bucket {source_bucket} does not exist")

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

        minio_config = self._get_minio_service_config(project_data, target_deployment_name)
        await self._apply_bucket_versioning(minio_connector, alias_name, target_bucket, minio_config)
        await self._apply_noncurrent_version_limit(
            minio_connector, alias_name, target_bucket, project_data, target_deployment_name
        )

        cluster = target_deployment["cluster"]

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
        logger.info(f"Ensuring target MinIO user {username} exists for cloning")
        user_result = await minio_connector.create_user(alias_name, username, password)
        if user_result["status"] not in ["created", "exists"]:
            raise RuntimeError(f"Failed to create MinIO user {username}: {user_result.get('message', 'Unknown error')}")

        if user_result["status"] == "created":
            logger.info(f"Created new MinIO user: {username}")
        else:
            logger.info(f"MinIO user already exists: {username}")

        logger.info(f"Ensuring target bucket {bucket_name} exists for cloning")
        await self._ensure_bucket_and_permissions(minio_connector, alias_name, username, bucket_name)

    async def _copy_bucket_data(
        self, minio_connector: MinioConnector, alias_name: str, source_bucket: str, target_bucket: str
    ) -> None:
        """Copy every object from one bucket to another with mc mirror."""
        try:
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
        try:
            bucket_list = await minio_connector.list_buckets(alias_name)

            if isinstance(bucket_list, list):
                bucket_names = [bucket.get("name", "") for bucket in bucket_list]
                return bucket_name in bucket_names
            else:
                logger.debug(f"Error listing buckets: {bucket_list}")
                return False

        except Exception as e:
            logger.debug(f"Error checking bucket existence for {bucket_name}: {e}")
            return False

    async def _validate_and_fix_credentials(
        self,
        minio_connector: MinioConnector,
        alias_name: str,
        project_name: str,
        deployment_name: str,
        deployment: dict[str, Any],
    ) -> tuple[str, str] | None:
        """The stored credentials, recreated when they no longer authenticate.

        Returns (access_key, secret_key), or None when there are no stored credentials.
        """
        existing_minio_secret = await self._get_existing_minio_credentials_from_k8s(deployment_name, deployment)

        if not existing_minio_secret:
            return None

        logger.info(f"Found existing MinIO credentials for {deployment_name}, validating...")

        username = generate_minio_username(project_name, deployment_name)
        bucket_name = generate_bucket_name(project_name, deployment_name, None)

        credentials_valid = await self._test_minio_connection(
            minio_connector, existing_minio_secret.access_key, existing_minio_secret.secret_key, bucket_name
        )

        if credentials_valid:
            logger.info(f"Existing MinIO credentials are valid for {deployment_name}")
            return existing_minio_secret.access_key, existing_minio_secret.secret_key

        user_exists = await self._check_minio_user_exists(minio_connector, alias_name, existing_minio_secret.access_key)

        if not user_exists:
            logger.warning(f"MinIO user {username} doesn't exist in MinIO (fresh deployment), creating new user")
            should_recreate = True
        elif settings.RECREATE_PASSWORD_ON_AUTHENTICATION_FAILURE:
            logger.warning(f"MinIO credentials are invalid for {deployment_name}, recreating user")
            should_recreate = True
        else:
            logger.error(
                f"MinIO credentials are invalid for {deployment_name} and "
                "RECREATE_PASSWORD_ON_AUTHENTICATION_FAILURE is disabled"
            )
            return None

        if should_recreate:
            new_secret_key = generate_secure_password(min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20)

            delete_result = await minio_connector.delete_user(alias_name, username)
            if delete_result["status"] not in ["deleted", "not_found"]:
                logger.error(
                    f"Failed to delete existing MinIO user {username}: {delete_result.get('message', 'Unknown error')}"
                )
                return None
            elif delete_result["status"] == "deleted":
                logger.info(f"Deleted existing MinIO user: {username}")

            user_result = await minio_connector.create_user(alias_name, username, new_secret_key)
            if user_result["status"] not in ["created", "exists"]:
                logger.error(f"Failed to recreate MinIO user {username}: {user_result.get('message', 'Unknown error')}")
                return None

            logger.info(f"Recreated MinIO user with new credentials: {username}")

            await self._ensure_bucket_and_permissions(minio_connector, alias_name, username, bucket_name)

            return username, new_secret_key

        return None

    async def _ensure_bucket_and_permissions(
        self, minio_connector: MinioConnector, alias_name: str, username: str, bucket_name: str
    ) -> None:
        bucket_result = await minio_connector.create_bucket(alias_name, bucket_name)
        if bucket_result["status"] not in ["created", "exists"]:
            raise RuntimeError(
                f"Failed to create MinIO bucket {bucket_name}: {bucket_result.get('message', 'Unknown error')}"
            )

        if bucket_result["status"] == "created":
            logger.info(f"Created MinIO bucket: {bucket_name}")
        else:
            logger.info(f"MinIO bucket already exists: {bucket_name}")

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
        """Connect to the external source and check that ``source_bucket`` is there.

        Returns ``bucket_exists`` and ``object_count`` (None when it cannot be counted).

        Raises:
            Exception: the connection or the check itself failed.
        """
        logger.info(f"Validating external source: {source_host}:{source_port}/{source_bucket}")

        minio_connector = None
        temp_alias = None

        try:
            minio_connector = create_minio_connector()

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

            bucket_list = await minio_connector.list_buckets(temp_alias)

            bucket_exists = False
            if isinstance(bucket_list, list):
                bucket_names = [bucket.get("name", "") for bucket in bucket_list]
                bucket_exists = source_bucket in bucket_names

            if not bucket_exists:
                raise Exception(f"Source bucket '{source_bucket}' does not exist at {source_host}:{source_port}")

            logger.info(f"External source validated: {source_host}:{source_port}/{source_bucket}")

            return {"bucket_exists": bucket_exists, "object_count": None}

        except Exception as e:
            logger.warning(f"Failed to validate external source {source_host}:{source_port}/{source_bucket}: {e!s}")
            raise Exception(UNREACHABLE_SOURCE_REASON) from e
        finally:
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
        """Args:
        remote_source_name: The remote source as named in the project file.
        force_clone: Overwrite an existing bucket.
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

        generation = self._get_deployment_bucket_generation(project_data, deployment_name)
        self.project_manager.report_clone_performed(deployment_name, ServiceType.MINIO_STORAGE.value, generation)

    def _get_remote_source_config(self, project_data: dict[str, Any], remote_source_name: str) -> dict[str, Any] | None:
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
        """Set up and tear down the tunnel; the clone itself runs on the direct path."""
        from opi.manager.chisel_helper import chisel_tunnel

        logger.info(
            f"Starting MinIO clone via Chisel tunnel: {project_name}/{deployment_name} "
            f"<- {source_host}:{source_port}/{source_bucket} "
            f"(via {chisel_config['server-url']})"
        )

        async with chisel_tunnel(chisel_config, source_host, source_port, project_data) as endpoint:
            logger.info(f"Tunnel established: {endpoint['host']}:{endpoint['port']} -> {source_host}:{source_port}")

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
        """Clone a bucket from an external source into a target deployment.

        Validates, prepares and executes the cross-cluster clone in sequence.

        Args:
            force_clone: Overwrite an existing target bucket.
            chisel_config: Given, a Chisel tunnel is established to reach the source MinIO;
                carries ``server-url``, ``username`` and ``password`` (possibly encrypted).
            project_data: Needed to decrypt that password.
        """
        refusal = await clone_source_refusal(source_host, chisel_config.get("server-url") if chisel_config else None)
        if refusal:
            return {
                "project": project_name,
                "deployment": deployment_name,
                "source": {"host": source_host, "port": source_port, "bucket": source_bucket},
                "operations": [],
                "success": False,
                "errors": [refusal],
            }

        if chisel_config:
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
        """The clone itself, without any tunnel handling."""
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

            target_username = generate_minio_username(project_name, deployment_name)

            current_generation = self._get_deployment_bucket_generation(project_data, deployment_name)
            target_bucket = generate_bucket_name(project_name, deployment_name, current_generation)
            generation_was_incremented = False
            # 0, not None: this is only read once incremented (where it is reassigned),
            # and the project file wants a number there.
            new_generation = current_generation or 0

            try:
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
                    raise RuntimeError(f"Failed to configure MinIO alias '{alias_name}' for {settings.MINIO_HOST}")

                existing_credentials = await self._validate_and_fix_credentials(
                    minio_connector, alias_name, project_name, deployment_name, deployment
                )

                if existing_credentials:
                    target_username_final, target_password = existing_credentials
                    logger.info(f"Using existing validated credentials for {deployment_name}")
                else:
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

            target_bucket_exists = await self._check_bucket_exists(minio_connector, alias_name, target_bucket)

            if target_bucket_exists and force_clone:
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

                result["target"]["bucket"] = target_bucket

            try:
                logger.info(f"Preparing target bucket and user for {deployment_name}: {target_bucket}")

                await self._create_user_and_bucket_for_clone(
                    minio_connector,
                    alias_name,
                    target_username_final,
                    target_password,
                    target_bucket,
                    existing_credentials,
                )

                result["operations"].append({"type": "target_prepared", "status": "success"})

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

            temp_source_alias = None
            try:
                logger.info(f"Executing bucket clone from external source to {target_bucket}")

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

                mirror_result = await minio_connector.mirror_bucket_cross_alias(
                    source_alias=temp_source_alias,
                    source_bucket=source_bucket,
                    target_alias=alias_name,
                    target_bucket=target_bucket,
                )

                if mirror_result["status"] != "success":
                    raise Exception(f"Mirror failed: {mirror_result.get('message')}")

                result["operations"].append({"type": "bucket_cloned", "status": "success"})

                logger.info(f"Applying versioning configuration to cloned bucket {target_bucket}")
                minio_config = self._get_minio_service_config(project_data, deployment_name)
                await self._apply_bucket_versioning(minio_connector, alias_name, target_bucket, minio_config)
                await self._apply_noncurrent_version_limit(
                    minio_connector, alias_name, target_bucket, project_data, deployment_name
                )
                result["operations"].append({"type": "versioning_applied", "status": "success"})

            except Exception as e:
                logger.error(f"Bucket clone failed: {e}")
                result["errors"].append(f"Bucket clone failed: {e!s}")
                result["operations"].append({"type": "bucket_cloned", "status": "failed", "error": str(e)})
                return result
            finally:
                if temp_source_alias:
                    try:
                        logger.debug(f"Removing temporary source alias: {temp_source_alias}")
                        await minio_connector._run_mc_command(["alias", "rm", temp_source_alias])
                        logger.debug(f"Removed temporary source alias: {temp_source_alias}")
                    except Exception as cleanup_error:
                        logger.warning(f"Failed to cleanup temporary alias {temp_source_alias}: {cleanup_error}")

            if generation_was_incremented:
                try:
                    project_file_handler = self.project_manager._project_file_handler
                    project_file_handler.set_deployment_service_generation(
                        project_data, deployment_name, ServiceType.MINIO_STORAGE.value, new_generation
                    )
                    logger.info(f"Updated bucket generation in project file: {new_generation}")

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

            # Always stored: a new generation means a new bucket name in the secret.
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
