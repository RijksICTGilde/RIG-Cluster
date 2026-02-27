"""Redis service manager for handling Redis cache resources."""

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opi.manager.project_manager import ProjectManager

from opi.core.cluster_config import get_redis_server
from opi.core.config import settings
from opi.services import ServiceType
from opi.utils.naming import generate_redis_key_prefix, generate_redis_username
from opi.utils.passwords import generate_secure_password
from opi.utils.secrets import RedisSecret

logger = logging.getLogger(__name__)


class RedisManager:
    """Manager for Redis cache operations and resources.

    RedisManager handles provisioning per-project Redis ACL users for deployments
    that use the Redis service. Each project/deployment gets a dedicated Redis user
    with a unique password, restricted to keys prefixed with {deployment}-{project}:.

    The key prefix restriction can be disabled via service config for applications
    that don't support key prefixing (e.g., Grist):
        - redis:
            config:
              acl-key-prefix: false
    """

    def __init__(self, project_manager: "ProjectManager") -> None:
        self.project_manager = project_manager

    async def create_resources_for_deployment(
        self,
        project_data: dict[str, Any],
        deployment: dict[str, Any],
    ) -> None:
        """
        Create Redis resources for a deployment that has Redis service enabled.

        Creates a per-project Redis ACL user with a unique password, restricted
        to keys with the deployment-project prefix.

        Args:
            project_data: The project configuration data
            deployment: The specific deployment configuration
        """
        project_name = await self.project_manager.get_name()
        deployment_name = deployment["name"]
        cluster_name = deployment.get("cluster")

        if not cluster_name:
            raise ValueError(f"Deployment {deployment_name} is missing required 'cluster' field")

        if not await self._deployment_uses_redis(project_data, deployment_name):
            logger.debug(f"Deployment {deployment_name} does not use Redis service, skipping")
            return

        logger.info(f"Processing Redis resources for project: {project_name}, deployment: {deployment_name}")

        progress_manager = self.project_manager.get_progress_manager()
        redis_task = None
        if progress_manager:
            redis_task = progress_manager.add_task("Creating Redis cache resources")

        try:
            uses_namespace_redis = self._project_uses_namespace_redis(project_data)

            if uses_namespace_redis:
                logger.warning(
                    f"Namespace-specific Redis requested for {project_name}/{deployment_name} "
                    "but not yet implemented. Using shared Redis instead."
                )

            # Shared Redis configuration
            redis_host = get_redis_server(cluster_name)
            redis_port = settings.REDIS_PORT
            admin_password = settings.REDIS_PASSWORD

            logger.info(f"Using shared Redis for {project_name}/{deployment_name} (host: {redis_host})")

            # Verify admin connectivity before proceeding
            connection_valid = await self._test_redis_connection(redis_host, redis_port, admin_password)
            if not connection_valid:
                raise RuntimeError(
                    f"Failed to connect to Redis at {redis_host}:{redis_port}. "
                    "Check that the Redis server is running and the REDIS_PASSWORD setting is correct."
                )

            # Generate per-project Redis username and key prefix
            redis_username = generate_redis_username(project_name, deployment_name)
            redis_config = self._get_redis_service_config(project_data)
            use_key_prefix = redis_config.get("acl-key-prefix", True) if redis_config else True
            key_prefix = generate_redis_key_prefix(project_name, deployment_name) if use_key_prefix else ""

            if not use_key_prefix:
                logger.info(
                    f"ACL key prefix disabled for {project_name}/{deployment_name}, user will have access to all keys"
                )

            # Check for existing Redis credentials in Kubernetes
            existing_redis_secret = await self._get_existing_redis_credentials_from_k8s(deployment_name, deployment)

            if existing_redis_secret:
                logger.info(f"Found existing Redis secret in Kubernetes for {project_name}/{deployment_name}")

                # Check if key prefix config has changed (ACL needs to be updated)
                acl_needs_update = existing_redis_secret.key_prefix != key_prefix
                if acl_needs_update:
                    logger.info(
                        f"ACL key prefix changed for {project_name}/{deployment_name} "
                        f"(was: '{existing_redis_secret.key_prefix}', now: '{key_prefix}'), updating ACL"
                    )

                # Verify existing credentials still work
                existing_valid = await self._test_redis_connection(
                    existing_redis_secret.host,
                    existing_redis_secret.port,
                    existing_redis_secret.password,
                    username=redis_username,
                )
                if existing_valid and not acl_needs_update:
                    logger.info(f"Existing Redis credentials are valid for {project_name}/{deployment_name}")
                    redis_secret = existing_redis_secret
                elif existing_valid and acl_needs_update:
                    # Credentials work but ACL permissions need updating, reuse password
                    await self._create_acl_user(
                        redis_host,
                        redis_port,
                        admin_password,
                        redis_username,
                        existing_redis_secret.password,
                        key_prefix,
                    )
                    redis_secret = RedisSecret(
                        host=redis_host,
                        port=redis_port,
                        username=redis_username,
                        password=existing_redis_secret.password,
                        key_prefix=key_prefix,
                    )
                else:
                    logger.warning(
                        f"Existing Redis credentials invalid for {project_name}/{deployment_name}, recreating ACL user"
                    )
                    user_password = generate_secure_password(
                        min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20
                    )
                    await self._create_acl_user(
                        redis_host,
                        redis_port,
                        admin_password,
                        redis_username,
                        user_password,
                        key_prefix,
                    )
                    redis_secret = RedisSecret(
                        host=redis_host,
                        port=redis_port,
                        username=redis_username,
                        password=user_password,
                        key_prefix=key_prefix,
                    )
            else:
                # Create new per-project ACL user
                logger.info(f"No Redis secret found, creating ACL user for {project_name}/{deployment_name}")
                user_password = generate_secure_password(
                    min_uppercase=3, min_lowercase=3, min_digits=3, total_length=20
                )

                # Check if user already exists in Redis (e.g., secret was deleted but user remains)
                user_exists = await self._check_acl_user_exists(redis_host, redis_port, admin_password, redis_username)
                if user_exists:
                    logger.info(f"Redis ACL user {redis_username} already exists, resetting password")

                await self._create_acl_user(
                    redis_host,
                    redis_port,
                    admin_password,
                    redis_username,
                    user_password,
                    key_prefix,
                )
                redis_secret = RedisSecret(
                    host=redis_host,
                    port=redis_port,
                    username=redis_username,
                    password=user_password,
                    key_prefix=key_prefix,
                )

            # Verify the new/existing credentials actually work
            final_valid = await self._test_redis_connection(
                redis_secret.host,
                redis_secret.port,
                redis_secret.password,
                username=redis_username,
            )
            if not final_valid:
                raise RuntimeError(
                    f"Redis ACL user {redis_username} was created but authentication failed. "
                    "Check Redis ACL configuration."
                )

            # Store Redis credentials in private secrets map
            self.project_manager._add_secret_to_create(
                deployment_name,
                "redis",
                redis_secret,
            )

            logger.info(f"Redis resources ready for {deployment_name} (user: {redis_username}, prefix: {key_prefix}*)")

        finally:
            if progress_manager and redis_task:
                progress_manager.complete_task(redis_task)

    async def delete_resources_for_deployment(
        self, project_data: dict[str, Any], deployment: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Delete Redis resources for a deployment.

        Removes the per-project Redis ACL user.

        Args:
            project_data: The project configuration data
            deployment: The specific deployment configuration

        Returns:
            Dictionary containing deletion results and status
        """
        project_name = await self.project_manager.get_name()
        deployment_name = deployment["name"]
        cluster_name = deployment.get("cluster")

        deletion_results: dict[str, Any] = {
            "service": "redis",
            "deployment": deployment_name,
            "operations": [],
            "success": True,
            "errors": [],
        }

        if not await self._deployment_uses_redis(project_data, deployment_name):
            deletion_results["operations"].append(
                {"type": "redis_cleanup", "status": "skipped", "reason": "Deployment does not use Redis service"}
            )
            logger.debug(f"Deployment {deployment_name} does not use Redis service, skipping Redis cleanup")
            return deletion_results

        logger.info(f"Redis cleanup for project: {project_name}, deployment: {deployment_name}")

        if cluster_name:
            redis_host = get_redis_server(cluster_name)
            redis_port = settings.REDIS_PORT
            admin_password = settings.REDIS_PASSWORD
            redis_username = generate_redis_username(project_name, deployment_name)

            try:
                await self._delete_acl_user(redis_host, redis_port, admin_password, redis_username)
                deletion_results["operations"].append(
                    {
                        "type": "redis_acl_user_delete",
                        "status": "success",
                        "message": f"Deleted Redis ACL user: {redis_username}",
                    }
                )
                logger.info(f"Deleted Redis ACL user: {redis_username}")
            except Exception as e:
                error_msg = f"Failed to delete Redis ACL user {redis_username}: {e}"
                deletion_results["operations"].append(
                    {"type": "redis_acl_user_delete", "status": "error", "message": error_msg}
                )
                deletion_results["errors"].append(error_msg)
                logger.warning(error_msg)
        else:
            deletion_results["operations"].append(
                {"type": "redis_cleanup", "status": "skipped", "reason": "No cluster configured"}
            )

        return deletion_results

    async def _deployment_uses_redis(self, project_data: dict[str, Any], deployment_name: str) -> bool:
        """Check if a deployment uses Redis service."""
        return self.project_manager._project_file_handler.deployment_uses_service(
            project_data,
            deployment_name,
            [ServiceType.REDIS.value, ServiceType.NAMESPACE_REDIS.value],
        )

    def _project_uses_namespace_redis(self, project_data: dict[str, Any]) -> bool:
        """Check if project uses namespace-specific Redis service."""
        project_services = project_data.get("services", [])
        if not project_services:
            return False

        for service_item in project_services:
            if isinstance(service_item, str):
                if service_item == ServiceType.NAMESPACE_REDIS.value:
                    return True
            elif isinstance(service_item, dict) and ServiceType.NAMESPACE_REDIS.value in service_item:
                return True

        return False

    @staticmethod
    def _get_redis_service_config(project_data: dict[str, Any]) -> dict[str, Any] | None:
        """
        Extract redis service configuration from the project services block.

        Supports both simple and configured service formats:
        - Simple: "- redis" -> returns None
        - Configured: "- redis:\\n    config:\\n      acl-key-prefix: false" -> returns config dict

        Args:
            project_data: The project configuration data

        Returns:
            Config dict or None if service not configured or no config block
        """
        services = project_data.get("services", [])

        for service in services:
            if isinstance(service, str):
                if service in (ServiceType.REDIS.value, ServiceType.NAMESPACE_REDIS.value):
                    return None
            elif isinstance(service, dict):
                service_name = next(iter(service.keys())) if service else None
                if service_name in (ServiceType.REDIS.value, ServiceType.NAMESPACE_REDIS.value):
                    config = service.get(service_name, {}).get("config")
                    if config:
                        logger.debug(f"Found redis config: {config}")
                        return config
                    return None

        return None

    async def _get_existing_redis_credentials_from_k8s(
        self, deployment_name: str, deployment: dict[str, Any]
    ) -> RedisSecret | None:
        """Get existing Redis credentials from Kubernetes secret."""
        try:
            from opi.core.cluster_config import get_prefixed_namespace

            secret_name = RedisSecret.get_secret_name(deployment_name)
            kubectl_connector = self.project_manager._kubectl_connector
            namespace = get_prefixed_namespace(settings.CLUSTER_MANAGER, deployment["namespace"])

            secret_data = await kubectl_connector.get_secret(secret_name, namespace)
            if secret_data:
                return RedisSecret.from_k8s_secret_data(secret_data)
            return None
        except Exception as e:
            logger.debug(f"Could not retrieve Redis secret for {deployment_name}: {e}")
            return None

    @staticmethod
    async def _send_redis_command(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *args: str) -> str:
        """
        Send a command to Redis using RESP protocol and return the response.

        Args:
            reader: Async stream reader
            writer: Async stream writer
            *args: Command arguments (e.g., "AUTH", "password")

        Returns:
            Raw response string from Redis
        """
        # Build RESP array command
        cmd = f"*{len(args)}\r\n"
        for arg in args:
            cmd += f"${len(arg)}\r\n{arg}\r\n"

        writer.write(cmd.encode())
        await writer.drain()

        response = await asyncio.wait_for(reader.readline(), timeout=5.0)
        return response.decode().strip()

    @staticmethod
    async def _test_redis_connection(
        host: str,
        port: int,
        password: str,
        username: str | None = None,
    ) -> bool:
        """
        Test if Redis credentials are valid by connecting and sending AUTH + PING.

        Args:
            host: Redis server hostname
            port: Redis server port
            password: Redis password
            username: Redis ACL username (None for default user)

        Returns:
            True if connection and authentication successful, False otherwise
        """
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5.0)

            try:
                # AUTH with username (ACL) or just password (default user)
                if username:
                    auth_cmd = (
                        f"*3\r\n$4\r\nAUTH\r\n${len(username)}\r\n{username}\r\n${len(password)}\r\n{password}\r\n"
                    )
                else:
                    auth_cmd = f"*2\r\n$4\r\nAUTH\r\n${len(password)}\r\n{password}\r\n"

                writer.write(auth_cmd.encode())
                await writer.drain()

                auth_response = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if not auth_response.startswith(b"+OK"):
                    logger.debug(
                        f"Redis AUTH failed at {host}:{port} (user: {username or 'default'}): {auth_response.decode().strip()}"
                    )
                    return False

                # Send PING
                ping_cmd = "*1\r\n$4\r\nPING\r\n"
                writer.write(ping_cmd.encode())
                await writer.drain()

                ping_response = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if not ping_response.startswith(b"+PONG"):
                    logger.debug(f"Redis PING failed at {host}:{port}: {ping_response.decode().strip()}")
                    return False

                logger.debug(f"Redis connection test successful at {host}:{port} (user: {username or 'default'})")
                return True
            finally:
                writer.close()
                await writer.wait_closed()

        except Exception as e:
            logger.debug(f"Redis connection test failed at {host}:{port}: {e}")
            return False

    @staticmethod
    async def _create_acl_user(
        host: str,
        port: int,
        admin_password: str,
        username: str,
        user_password: str,
        key_prefix: str,
    ) -> None:
        """
        Create or update a Redis ACL user with key prefix restrictions.

        Uses: ACL SETUSER <username> on ><password> resetpass ~<prefix>* +@all

        When key_prefix is empty, the user gets access to all keys and channels
        (~* &*). This is needed for applications that don't support key prefixing.

        Args:
            host: Redis server hostname
            port: Redis server port
            admin_password: Admin password for authentication
            username: ACL username to create
            user_password: Password for the new user
            key_prefix: Key prefix restriction (e.g., "main-myproject:"), or empty for all keys
        """
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5.0)

            try:
                # Authenticate as admin
                response = await RedisManager._send_redis_command(reader, writer, "AUTH", admin_password)
                if not response.startswith("+OK"):
                    raise RuntimeError(f"Redis admin AUTH failed: {response}")

                # Create/update ACL user:
                # - on: enable the user
                # - resetpass: clear any existing passwords
                # - ><password>: set the new password
                # - ~<prefix>* or ~*: restrict to keys matching prefix (or all keys)
                # - &<prefix>* or &*: restrict pub/sub channels to same prefix (or all channels)
                # - +@all: allow all commands
                if key_prefix:
                    key_pattern = f"~{key_prefix}*"
                    channel_pattern = f"&{key_prefix}*"
                else:
                    key_pattern = "~*"
                    channel_pattern = "&*"

                response = await RedisManager._send_redis_command(
                    reader,
                    writer,
                    "ACL",
                    "SETUSER",
                    username,
                    "on",
                    "resetpass",
                    f">{user_password}",
                    key_pattern,
                    channel_pattern,
                    "+@all",
                )
                if not response.startswith("+OK"):
                    raise RuntimeError(f"Redis ACL SETUSER failed for {username}: {response}")

                scope = f"{key_prefix}*" if key_prefix else "* (all keys)"
                logger.info(f"Created/updated Redis ACL user: {username} (keys+channels: {scope})")
            finally:
                writer.close()
                await writer.wait_closed()

        except Exception as e:
            raise RuntimeError(f"Failed to create Redis ACL user {username}: {e}") from e

    @staticmethod
    async def _delete_acl_user(host: str, port: int, admin_password: str, username: str) -> None:
        """
        Delete a Redis ACL user.

        Args:
            host: Redis server hostname
            port: Redis server port
            admin_password: Admin password for authentication
            username: ACL username to delete
        """
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5.0)

            try:
                response = await RedisManager._send_redis_command(reader, writer, "AUTH", admin_password)
                if not response.startswith("+OK"):
                    raise RuntimeError(f"Redis admin AUTH failed: {response}")

                response = await RedisManager._send_redis_command(reader, writer, "ACL", "DELUSER", username)
                # Response is :1 for deleted, :0 for not found — both are acceptable
                logger.info(f"Deleted Redis ACL user: {username} (response: {response})")
            finally:
                writer.close()
                await writer.wait_closed()

        except Exception as e:
            raise RuntimeError(f"Failed to delete Redis ACL user {username}: {e}") from e

    @staticmethod
    async def _check_acl_user_exists(host: str, port: int, admin_password: str, username: str) -> bool:
        """
        Check if a Redis ACL user exists.

        Args:
            host: Redis server hostname
            port: Redis server port
            admin_password: Admin password for authentication
            username: ACL username to check

        Returns:
            True if user exists, False otherwise
        """
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5.0)

            try:
                response = await RedisManager._send_redis_command(reader, writer, "AUTH", admin_password)
                if not response.startswith("+OK"):
                    return False

                # ACL GETUSER returns user info or error if not found
                cmd = f"*3\r\n$3\r\nACL\r\n$7\r\nGETUSER\r\n${len(username)}\r\n{username}\r\n"
                writer.write(cmd.encode())
                await writer.drain()

                response_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                decoded = response_line.decode().strip()

                # If user exists, response starts with * (array); if not, starts with - (error) or $-1 (nil)
                return decoded.startswith("*")
            finally:
                writer.close()
                await writer.wait_closed()

        except Exception as e:
            logger.debug(f"Failed to check Redis ACL user {username}: {e}")
            return False
