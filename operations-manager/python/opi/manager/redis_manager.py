"""Redis service manager for handling Redis cache resources."""

import logging
from typing import TYPE_CHECKING, Any

from jsonpath_ng.ext import parse as jsonpath_parse

if TYPE_CHECKING:
    from opi.manager.project_manager import ProjectManager

from opi.core.cluster_config import get_redis_server
from opi.core.config import settings
from opi.services import ServiceType
from opi.utils.secrets import RedisSecret

logger = logging.getLogger(__name__)


class RedisManager:
    """Manager for Redis cache operations and resources.

    RedisManager handles provisioning Redis credentials for deployments
    that use the Redis service. For shared Redis, it uses cluster-wide
    credentials. For namespace-specific Redis, it would provision
    dedicated instances (not yet implemented).
    """

    def __init__(self, project_manager: "ProjectManager") -> None:
        """
        Initialize the RedisManager with reference to ProjectManager.

        Args:
            project_manager: The main ProjectManager instance for accessing shared resources
        """
        self.project_manager = project_manager

    async def create_resources_for_deployment(
        self,
        project_data: dict[str, Any],
        deployment: dict[str, Any],
    ) -> None:
        """
        Create Redis resources for a deployment that has Redis service enabled.

        For shared Redis, this simply provisions credentials pointing to the
        shared Redis instance.

        Args:
            project_data: The project configuration data
            deployment: The specific deployment configuration
        """
        project_name = await self.project_manager.get_name()
        deployment_name = deployment["name"]
        cluster_name = deployment.get("cluster")

        if not cluster_name:
            raise ValueError(f"Deployment {deployment_name} is missing required 'cluster' field")

        # Check if this deployment has Redis service enabled
        if not await self._deployment_uses_redis(project_data, deployment_name):
            logger.debug(f"Deployment {deployment_name} does not use Redis service, skipping")
            return

        logger.info(f"Processing Redis resources for project: {project_name}, deployment: {deployment_name}")

        progress_manager = self.project_manager.get_progress_manager()
        redis_task = None
        if progress_manager:
            redis_task = progress_manager.add_task("Creating Redis cache resources")

        try:
            # Determine if using namespace-specific or shared Redis
            uses_namespace_redis = self._project_uses_namespace_redis(project_data)

            if uses_namespace_redis:
                # Namespace-specific Redis (not yet implemented)
                logger.warning(
                    f"Namespace-specific Redis requested for {project_name}/{deployment_name} "
                    "but not yet implemented. Using shared Redis instead."
                )
                # Fall through to shared Redis for now

            # Shared Redis configuration
            redis_host = get_redis_server(cluster_name)
            redis_port = settings.REDIS_PORT
            redis_password = settings.REDIS_PASSWORD

            logger.info(f"Using shared Redis for {project_name}/{deployment_name} (host: {redis_host})")

            # Check for existing Redis credentials in Kubernetes
            existing_redis_secret = await self._get_existing_redis_credentials_from_k8s(deployment_name, deployment)

            if existing_redis_secret:
                logger.info(f"Found existing Redis secret in Kubernetes for {project_name}/{deployment_name}")
                # Use existing credentials
                redis_secret = existing_redis_secret
            else:
                # Create new Redis secret with shared credentials
                logger.info(f"No Redis secret found, creating new one for {project_name}/{deployment_name}")
                redis_secret = RedisSecret(
                    host=redis_host,
                    port=redis_port,
                    password=redis_password,
                )

            # Store Redis credentials in private secrets map
            self.project_manager._add_secret_to_create(
                deployment_name,
                "redis",
                redis_secret,
            )

            logger.info(f"Redis resources ready for {deployment_name} (stored in secrets map)")

        finally:
            if progress_manager and redis_task:
                progress_manager.complete_task(redis_task)

    async def delete_resources_for_deployment(
        self, project_data: dict[str, Any], deployment: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Delete Redis resources for a deployment.

        For shared Redis, there are no resources to delete since we just
        provide access to the shared instance. The Kubernetes secret
        is managed separately.

        Args:
            project_data: The project configuration data
            deployment: The specific deployment configuration

        Returns:
            Dictionary containing deletion results and status
        """
        project_name = await self.project_manager.get_name()
        deployment_name = deployment["name"]

        deletion_results = {
            "service": "redis",
            "deployment": deployment_name,
            "operations": [],
            "success": True,
            "errors": [],
        }

        # Check if this deployment uses Redis service
        if not await self._deployment_uses_redis(project_data, deployment_name):
            deletion_results["operations"].append(
                {"type": "redis_cleanup", "status": "skipped", "reason": "Deployment does not use Redis service"}
            )
            logger.debug(f"Deployment {deployment_name} does not use Redis service, skipping Redis cleanup")
            return deletion_results

        logger.info(f"Redis cleanup for project: {project_name}, deployment: {deployment_name}")

        # For shared Redis, no actual resources to delete
        # The Kubernetes secret deletion is handled by the general deployment cleanup
        deletion_results["operations"].append(
            {
                "type": "redis_cleanup",
                "status": "success",
                "message": "Shared Redis - no dedicated resources to delete",
            }
        )

        return deletion_results

    async def _deployment_uses_redis(self, project_data: dict[str, Any], deployment_name: str) -> bool:
        """
        Check if a deployment uses Redis service.

        Args:
            project_data: The project configuration data
            deployment_name: Name of the deployment to check

        Returns:
            True if deployment uses Redis service, False otherwise
        """
        return self.project_manager._project_file_handler.deployment_uses_service(
            project_data,
            deployment_name,
            [ServiceType.REDIS.value, ServiceType.NAMESPACE_REDIS.value],
        )

    def _project_uses_namespace_redis(self, project_data: dict[str, Any]) -> bool:
        """
        Check if project uses namespace-specific Redis service.

        This checks if the project-level services configuration includes
        NAMESPACE_REDIS, which would require a dedicated Redis instance
        in the project's infrastructure namespace.

        Args:
            project_data: The project configuration data

        Returns:
            True if project uses namespace-specific Redis, False otherwise
        """
        # Check project-level services
        project_services = project_data.get("services", [])
        if not project_services:
            return False

        # Services can be strings or dicts with service name as key
        for service_item in project_services:
            if isinstance(service_item, str):
                if service_item == ServiceType.NAMESPACE_REDIS.value:
                    return True
            elif isinstance(service_item, dict):
                # Dict format: {"namespace-redis": {"config": {...}}}
                if ServiceType.NAMESPACE_REDIS.value in service_item:
                    return True

        return False

    async def _get_existing_redis_credentials_from_k8s(
        self, deployment_name: str, deployment: dict[str, Any]
    ) -> RedisSecret | None:
        """
        Get existing Redis credentials from Kubernetes secret.

        Args:
            deployment_name: Name of the deployment
            deployment: The deployment configuration containing namespace info

        Returns:
            RedisSecret if found, None otherwise
        """
        try:
            from opi.core.cluster_config import get_prefixed_namespace
            from opi.core.config import settings

            secret_name = RedisSecret.get_secret_name(deployment_name)
            kubectl_connector = self.project_manager._kubectl_connector

            # Calculate the namespace where the secret should be stored
            namespace = get_prefixed_namespace(settings.CLUSTER_MANAGER, deployment["namespace"])

            # Try to get the secret from Kubernetes
            secret_data = await kubectl_connector.get_secret(secret_name, namespace)
            if secret_data:
                return RedisSecret.from_k8s_secret_data(secret_data)
            return None
        except Exception as e:
            logger.debug(f"Could not retrieve Redis secret for {deployment_name}: {e}")
            return None
