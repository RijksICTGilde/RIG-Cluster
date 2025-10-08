"""
MinIO connector using mc CLI commands.

This module provides functionality to interact with MinIO for bucket management,
user operations, and access control by executing mc CLI commands directly,
following the same pattern as kubectl.py.
"""

import asyncio
import json
import logging
import os
import re
import tempfile
import threading
from typing import Any

logger = logging.getLogger(__name__)


class MinioConnectionError(Exception):
    """Exception raised when MinIO connection is not available."""


class MinioExecutionError(Exception):
    """Exception raised when mc command execution fails."""


class MinioValidationError(Exception):
    """Exception raised when input validation fails."""


class MinioConnector:
    """Connector for interacting with MinIO using mc CLI commands."""

    _instance = None
    _lock = threading.Lock()
    is_mc_available = False
    configured_aliases = set()
    _retry_task = None

    def __new__(cls) -> "MinioConnector":
        """Implement singleton pattern."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        """Initialize the MinIO connector."""
        if self._initialized:
            return

        logger.debug("Initializing MinioConnector")

        # Setup environment variables for mc
        self.env = os.environ.copy()

        # Test mc CLI availability synchronously during initialization
        try:
            import subprocess

            result = subprocess.run(["mc", "--version"], capture_output=True, text=True, env=self.env, timeout=10)
            if result.returncode == 0:
                logger.info("MinIO CLI (mc) is available")
                MinioConnector.is_mc_available = True
            else:
                logger.warning(f"MinIO CLI (mc) not available: {result.stderr}")
                MinioConnector.is_mc_available = False
        except Exception as e:
            logger.error(f"Error testing mc CLI availability: {e}")
            MinioConnector.is_mc_available = False

        self._initialized = True
        logger.debug("MinioConnector initialized successfully")

    # Security validation methods

    @staticmethod
    def _validate_bucket_name(bucket_name: str) -> str:
        """Validate and sanitize bucket names according to S3 specifications."""
        if not bucket_name:
            raise MinioValidationError("Bucket name cannot be empty")

        if len(bucket_name) < 3:
            raise MinioValidationError("Bucket name must be at least 3 characters long")
        if len(bucket_name) > 63:
            raise MinioValidationError("Bucket name cannot exceed 63 characters")

        if not re.match(r"^[a-z0-9][a-z0-9\-]*[a-z0-9]$", bucket_name):
            raise MinioValidationError(
                f"Bucket name '{bucket_name}' contains invalid characters. "
                "Must contain only lowercase letters, numbers, and hyphens. "
                "Must start and end with a letter or number."
            )

        if "--" in bucket_name:
            raise MinioValidationError("Bucket name cannot contain consecutive hyphens")

        if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", bucket_name):
            raise MinioValidationError("Bucket name cannot be formatted as an IP address")

        return bucket_name

    @staticmethod
    def _validate_username(username: str) -> str:
        """Validate and sanitize MinIO usernames."""
        if not username:
            raise MinioValidationError("Username cannot be empty")

        if len(username) < 3:
            raise MinioValidationError("Username must be at least 3 characters long")
        if len(username) > 128:
            raise MinioValidationError("Username cannot exceed 128 characters")

        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_\-\.]*$", username):
            raise MinioValidationError(
                f"Username '{username}' contains invalid characters. "
                "Must start with letter/underscore and contain only letters, digits, underscores, hyphens, and dots."
            )

        return username

    @staticmethod
    def _validate_policy_name(policy_name: str) -> str:
        """Validate policy name."""
        if not policy_name:
            raise MinioValidationError("Policy name cannot be empty")

        if not re.match(r"^[a-zA-Z0-9_\-\.]+$", policy_name):
            raise MinioValidationError(
                f"Policy name '{policy_name}' contains invalid characters. "
                "Must contain only letters, digits, underscores, hyphens, and dots."
            )

        return policy_name

    async def _test_mc_availability(self) -> bool:
        """
        Test if mc CLI is available.

        Returns:
            True if mc is available, False otherwise
        """
        try:
            logger.debug("Testing mc CLI availability")

            process = await asyncio.create_subprocess_exec(
                "mc", "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=self.env
            )

            stdout, stderr = await process.communicate()

            if process.returncode == 0:
                logger.info("MC CLI is available")
                MinioConnector.is_mc_available = True
                return True
            else:
                logger.warning(f"MC CLI not available: {stderr.decode()}")
                MinioConnector.is_mc_available = False
                return False

        except Exception as e:
            logger.error(f"Error testing mc CLI availability: {e}")
            MinioConnector.is_mc_available = False
            return False

    async def _run_mc_command(
        self, args: list[str], env: dict[str, str] | None = None, stdin_input: str | None = None
    ) -> tuple[str, str, int]:
        """
        Run an mc command directly with subprocess.

        Args:
            args: List of mc command arguments
            env: Optional environment variables
            stdin_input: Optional string to pass to stdin

        Returns:
            Tuple of (stdout, stderr, return_code)

        Raises:
            MinioConnectionError: If mc CLI is not available
            MinioExecutionError: If mc command fails
        """
        # Check MC CLI availability before running command
        if not MinioConnector.is_mc_available:
            raise MinioConnectionError("MC CLI is not available")
        # Set up environment
        cmd_env = self.env.copy()
        if env:
            cmd_env.update(env)

        # Create cmd_str for logging
        cmd_args_str = " ".join([f'"{arg}"' if " " in arg else arg for arg in args])
        cmd_str = f"mc {cmd_args_str}"

        if stdin_input:
            # Use shell execution with EOF markers for stdin input
            shell_cmd = f"{cmd_str} <<'EOF'\n{stdin_input}\nEOF"

            logger.debug("Running mc shell command with stdin")

            process = await asyncio.create_subprocess_shell(
                shell_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=cmd_env
            )

            stdout, stderr = await process.communicate()
        else:
            # Use regular exec for commands without stdin
            cmd = ["mc"]
            cmd.extend(args)

            logger.debug(f"Running mc command: {cmd_str}")

            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=cmd_env
            )

            stdout, stderr = await process.communicate()

        stdout_str = stdout.decode("utf-8").strip()
        stderr_str = stderr.decode("utf-8").strip()

        if process.returncode != 0:
            logger.warning(f"mc command failed with code {process.returncode}: {stderr_str}")

        return stdout_str, stderr_str, process.returncode

    async def configure_alias(
        self, alias: str, host: str, access_key: str, secret_key: str, secure: bool = False, region: str = "us-east-1"
    ) -> bool:
        """
        Configure an mc alias for the MinIO server.

        Note: The region parameter is kept for documentation but not passed to mc CLI,
        as mc alias doesn't support region configuration. Region is used when creating
        secrets for Python MinIO clients.

        Args:
            alias: Alias name for the MinIO server
            host: MinIO server host (e.g., "minio.example.com:9000")
            access_key: Access key for authentication
            secret_key: Secret key for authentication
            secure: Whether to use HTTPS
            region: AWS region for documentation (not used by mc alias command)

        Returns:
            True if alias was configured successfully, False otherwise
        """
        logger.debug(f"Configuring mc alias: {alias}")

        try:
            protocol = "https" if secure else "http"
            endpoint = f"{protocol}://{host}"

            args = ["alias", "set", alias, endpoint, access_key, secret_key]
            stdout, stderr, code = await self._run_mc_command(args)

            if code != 0:
                error_msg = f"Failed to configure alias {alias}: {stderr}"
                logger.error(error_msg)
                return False

            # Test the alias by listing buckets
            test_stdout, test_stderr, test_code = await self._run_mc_command(["ls", alias])
            if test_code != 0:
                logger.warning(f"Alias {alias} configured but connection test failed: {test_stderr}")
                return False

            # Add to configured aliases set
            MinioConnector.configured_aliases.add(alias)
            logger.info(f"Successfully configured and tested alias: {alias}")
            return True

        except Exception as e:
            logger.error(f"Error configuring alias {alias}: {e}")
            return False

    async def test_alias_connection(self, alias: str) -> bool:
        """
        Test connection to a specific MinIO alias.

        Args:
            alias: MinIO server alias to test

        Returns:
            True if connection is successful, False otherwise
        """
        try:
            logger.debug(f"Testing connection to alias: {alias}")

            # Try to list buckets as a connectivity test
            stdout, stderr, code = await self._run_mc_command(["ls", alias])

            if code == 0:
                logger.info(f"Connection test successful for alias: {alias}")
                MinioConnector.configured_aliases.add(alias)
                return True
            else:
                logger.warning(f"Connection test failed for alias {alias}: {stderr}")
                MinioConnector.configured_aliases.discard(alias)
                return False

        except Exception as e:
            logger.error(f"Error testing alias {alias} connection: {e}")
            MinioConnector.configured_aliases.discard(alias)
            return False

    def is_alias_configured(self, alias: str) -> bool:
        """
        Check if an alias is configured and working.

        Args:
            alias: MinIO server alias to check

        Returns:
            True if alias is configured and working, False otherwise
        """
        return alias in MinioConnector.configured_aliases

    def get_configured_aliases(self) -> set[str]:
        """
        Get all configured aliases.

        Returns:
            Set of configured alias names
        """
        return MinioConnector.configured_aliases.copy()

    # User Management Operations

    async def create_user(self, alias: str, username: str, secret_key: str) -> dict[str, Any]:
        """
        Create a new MinIO user.

        Args:
            alias: MinIO server alias
            username: Username (access key) for new user
            secret_key: Secret key for new user

        Returns:
            Dictionary with operation status and details
        """
        try:
            validated_username = self._validate_username(username)

            if len(secret_key) < 8:
                raise MinioValidationError("Secret key must be at least 8 characters long")

            args = ["admin", "user", "add", alias, validated_username, secret_key]
            stdout, stderr, code = await self._run_mc_command(args)

            if code != 0:
                if "already exists" in stderr.lower():
                    logger.info(f"User {validated_username} already exists")
                    return {"status": "exists", "message": f"User {validated_username} already exists"}

                error_msg = f"Failed to create user {validated_username}: {stderr}"
                logger.error(error_msg)
                raise MinioExecutionError(error_msg)

            logger.info(f"User {validated_username} created successfully")
            return {
                "status": "created",
                "message": f"User {validated_username} created successfully",
                "access_key": validated_username,
            }

        except MinioValidationError:
            logger.exception("Validation failed for user creation")
            raise
        except Exception as e:
            logger.exception(f"Failed to create user {username}")
            raise MinioExecutionError(f"User creation failed: {e}") from e

    async def delete_user(self, alias: str, username: str) -> dict[str, Any]:
        """
        Delete a MinIO user.

        Args:
            alias: MinIO server alias
            username: Username to delete

        Returns:
            Dictionary with operation status and details
        """
        try:
            validated_username = self._validate_username(username)

            args = ["admin", "user", "rm", alias, validated_username]
            stdout, stderr, code = await self._run_mc_command(args)

            if code != 0:
                if "does not exist" in stderr.lower() or "not found" in stderr.lower():
                    logger.warning(f"User {validated_username} does not exist")
                    return {"status": "not_found", "message": f"User {validated_username} does not exist"}

                error_msg = f"Failed to delete user {validated_username}: {stderr}"
                logger.error(error_msg)
                raise MinioExecutionError(error_msg)

            logger.info(f"User {validated_username} deleted successfully")
            return {"status": "deleted", "message": f"User {validated_username} deleted successfully"}

        except MinioValidationError:
            logger.exception("Validation failed for user deletion")
            raise
        except Exception as e:
            logger.exception(f"Failed to delete user {username}")
            raise MinioExecutionError(f"User deletion failed: {e}") from e

    async def list_users(self, alias: str) -> list[dict[str, Any]]:
        """
        List all MinIO users.

        Args:
            alias: MinIO server alias

        Returns:
            List of user dictionaries with details
        """
        try:
            args = ["admin", "user", "list", alias, "--json"]
            stdout, stderr, code = await self._run_mc_command(args)

            if code != 0:
                error_msg = f"Failed to list users: {stderr}"
                logger.error(error_msg)
                raise MinioExecutionError(error_msg)

            user_list = []
            if stdout.strip():
                # Parse JSON lines (mc outputs one JSON object per line)
                for line in stdout.strip().split("\n"):
                    if line.strip():
                        try:
                            user_data = json.loads(line)
                            user_list.append(
                                {
                                    "username": user_data.get("accessKey", ""),
                                    "access_key": user_data.get("accessKey", ""),
                                    "status": user_data.get("userStatus", ""),
                                }
                            )
                        except json.JSONDecodeError as e:
                            logger.warning(f"Failed to parse user data: {e}")

            logger.debug(f"Retrieved {len(user_list)} users from {alias}")
            return user_list

        except Exception as e:
            logger.exception("Failed to list users")
            raise MinioExecutionError(f"User listing failed: {e}") from e

    # Policy Management Operations

    async def create_policy(self, alias: str, policy_name: str, policy_document: dict[str, Any]) -> dict[str, Any]:
        """
        Create a new policy.

        Args:
            alias: MinIO server alias
            policy_name: Name of the policy
            policy_document: Policy document as dictionary

        Returns:
            Dictionary with operation status and details
        """
        try:
            validated_policy_name = self._validate_policy_name(policy_name)

            # Write policy to temporary file
            from opi.core.config import settings

            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, dir=settings.TEMP_DIR) as f:
                json.dump(policy_document, f, indent=2)
                temp_file = f.name

            try:
                args = ["admin", "policy", "create", alias, validated_policy_name, temp_file]
                stdout, stderr, code = await self._run_mc_command(args)

                if code != 0:
                    if "already exists" in stderr.lower():
                        logger.info(f"Policy {validated_policy_name} already exists")
                        return {"status": "exists", "message": f"Policy {validated_policy_name} already exists"}

                    error_msg = f"Failed to create policy {validated_policy_name}: {stderr}"
                    logger.error(error_msg)
                    raise MinioExecutionError(error_msg)

                logger.info(f"Policy {validated_policy_name} created successfully")
                return {
                    "status": "created",
                    "message": f"Policy {validated_policy_name} created successfully",
                    "policy_name": validated_policy_name,
                }
            finally:
                # Clean up temporary file
                try:
                    os.unlink(temp_file)
                except OSError:
                    pass

        except MinioValidationError:
            logger.exception("Validation failed for policy creation")
            raise
        except Exception as e:
            logger.exception(f"Failed to create policy {policy_name}")
            raise MinioExecutionError(f"Policy creation failed: {e}") from e

    async def attach_policy(self, alias: str, policy_name: str, username: str) -> dict[str, Any]:
        """
        Attach a policy to a user.

        Args:
            alias: MinIO server alias
            policy_name: Name of the policy to attach
            username: Username to attach policy to

        Returns:
            Dictionary with operation status and details
        """
        try:
            validated_policy_name = self._validate_policy_name(policy_name)
            validated_username = self._validate_username(username)

            args = ["admin", "policy", "attach", alias, validated_policy_name, "--user", validated_username]
            stdout, stderr, code = await self._run_mc_command(args)

            if code != 0:
                if "already attached" in stderr.lower() or "already exists" in stderr.lower():
                    logger.info(f"Policy {validated_policy_name} already attached to user {validated_username}")
                    return {
                        "status": "attached",
                        "message": f"Policy {validated_policy_name} already attached to user {validated_username}",
                        "policy_name": validated_policy_name,
                        "username": validated_username,
                    }

                error_msg = f"Failed to attach policy {validated_policy_name} to user {validated_username}: {stderr}"
                logger.error(error_msg)
                raise MinioExecutionError(error_msg)

            logger.info(f"Policy {validated_policy_name} attached to user {validated_username}")
            return {
                "status": "attached",
                "message": f"Policy {validated_policy_name} attached to user {validated_username}",
                "policy_name": validated_policy_name,
                "username": validated_username,
            }

        except MinioValidationError:
            logger.exception("Validation failed for policy attachment")
            raise
        except Exception as e:
            logger.exception(f"Failed to attach policy {policy_name} to user {username}")
            raise MinioExecutionError(f"Policy attachment failed: {e}") from e

    async def detach_policy(self, alias: str, policy_name: str, username: str) -> dict[str, Any]:
        """
        Detach a policy from a user.

        Args:
            alias: MinIO server alias
            policy_name: Name of the policy to detach
            username: Username to detach policy from

        Returns:
            Dictionary with operation status and details
        """
        try:
            validated_policy_name = self._validate_policy_name(policy_name)
            validated_username = self._validate_username(username)

            args = ["admin", "policy", "detach", alias, validated_policy_name, "--user", validated_username]
            stdout, stderr, code = await self._run_mc_command(args)

            if code != 0:
                error_msg = f"Failed to detach policy {validated_policy_name} from user {validated_username}: {stderr}"
                logger.error(error_msg)
                raise MinioExecutionError(error_msg)

            logger.info(f"Policy {validated_policy_name} detached from user {validated_username}")
            return {
                "status": "detached",
                "message": f"Policy {validated_policy_name} detached from user {validated_username}",
                "policy_name": validated_policy_name,
                "username": validated_username,
            }

        except MinioValidationError:
            logger.exception("Validation failed for policy detachment")
            raise
        except Exception as e:
            logger.exception(f"Failed to detach policy {policy_name} from user {username}")
            raise MinioExecutionError(f"Policy detachment failed: {e}") from e

    async def remove_policy(self, alias: str, policy_name: str) -> dict[str, Any]:
        """
        Remove/delete a policy.

        Args:
            alias: MinIO server alias
            policy_name: Name of the policy to remove

        Returns:
            Dictionary with operation status and details
        """
        try:
            validated_policy_name = self._validate_policy_name(policy_name)

            args = ["admin", "policy", "remove", alias, validated_policy_name]
            stdout, stderr, code = await self._run_mc_command(args)

            if code != 0:
                if "not found" in stderr.lower() or "does not exist" in stderr.lower():
                    logger.info(f"Policy {validated_policy_name} not found (may have already been removed)")
                    return {
                        "status": "not_found",
                        "message": f"Policy {validated_policy_name} not found",
                        "policy_name": validated_policy_name,
                    }

                error_msg = f"Failed to remove policy {validated_policy_name}: {stderr}"
                logger.error(error_msg)
                raise MinioExecutionError(error_msg)

            logger.info(f"Policy {validated_policy_name} removed successfully")
            return {
                "status": "success",
                "message": f"Policy {validated_policy_name} removed successfully",
                "policy_name": validated_policy_name,
            }

        except MinioValidationError:
            logger.exception("Validation failed for policy removal")
            raise
        except Exception as e:
            logger.exception(f"Failed to remove policy {policy_name}")
            raise MinioExecutionError(f"Policy removal failed: {e}") from e

    # Bucket Management Operations

    async def create_bucket(self, alias: str, bucket_name: str) -> dict[str, Any]:
        """
        Create a new bucket.

        Args:
            alias: MinIO server alias
            bucket_name: Name of bucket to create

        Returns:
            Dictionary with operation status and details
        """
        try:
            validated_bucket_name = self._validate_bucket_name(bucket_name)
            bucket_path = f"{alias}/{validated_bucket_name}"

            args = ["mb", bucket_path]
            stdout, stderr, code = await self._run_mc_command(args)

            if code != 0:
                if any(
                    phrase in stderr.lower()
                    for phrase in ["already exists", "bucket already exists", "you already own it"]
                ):
                    logger.info(f"Bucket {validated_bucket_name} already exists")
                    return {"status": "exists", "message": f"Bucket {validated_bucket_name} already exists"}

                error_msg = f"Failed to create bucket {validated_bucket_name}: {stderr}"
                logger.error(error_msg)
                raise MinioExecutionError(error_msg)

            logger.info(f"Bucket {validated_bucket_name} created successfully")
            return {"status": "created", "message": f"Bucket {validated_bucket_name} created successfully"}

        except MinioValidationError:
            logger.exception("Validation failed for bucket creation")
            raise
        except Exception as e:
            logger.exception(f"Failed to create bucket {bucket_name}")
            raise MinioExecutionError(f"Bucket creation failed: {e}") from e

    async def delete_bucket(self, alias: str, bucket_name: str, force: bool = False) -> dict[str, Any]:
        """
        Delete a bucket.

        Args:
            alias: MinIO server alias
            bucket_name: Name of bucket to delete
            force: Whether to force delete non-empty bucket

        Returns:
            Dictionary with operation status and details
        """
        try:
            validated_bucket_name = self._validate_bucket_name(bucket_name)
            bucket_path = f"{alias}/{validated_bucket_name}"

            args = ["rb", bucket_path]
            if force:
                args.append("--force")

            stdout, stderr, code = await self._run_mc_command(args)

            if code != 0:
                if "does not exist" in stderr.lower() or "not found" in stderr.lower():
                    logger.warning(f"Bucket {validated_bucket_name} does not exist")
                    return {"status": "not_found", "message": f"Bucket {validated_bucket_name} does not exist"}

                error_msg = f"Failed to delete bucket {validated_bucket_name}: {stderr}"
                logger.error(error_msg)
                raise MinioExecutionError(error_msg)

            logger.info(f"Bucket {validated_bucket_name} deleted successfully")
            return {"status": "deleted", "message": f"Bucket {validated_bucket_name} deleted successfully"}

        except MinioValidationError:
            logger.exception("Validation failed for bucket deletion")
            raise
        except Exception as e:
            logger.exception(f"Failed to delete bucket {bucket_name}")
            raise MinioExecutionError(f"Bucket deletion failed: {e}") from e

    async def list_buckets(self, alias: str) -> list[dict[str, Any]]:
        """
        List all buckets.

        Args:
            alias: MinIO server alias

        Returns:
            List of bucket dictionaries with details
        """
        try:
            args = ["ls", alias, "--json"]
            stdout, stderr, code = await self._run_mc_command(args)

            if code != 0:
                error_msg = f"Failed to list buckets: {stderr}"
                logger.error(error_msg)
                raise MinioExecutionError(error_msg)

            bucket_list = []
            if stdout.strip():
                # Parse JSON lines
                for line in stdout.strip().split("\n"):
                    if line.strip():
                        try:
                            bucket_data = json.loads(line)
                            if bucket_data.get("type") == "folder":  # Buckets appear as folders
                                bucket_list.append(
                                    {
                                        "name": bucket_data.get("key", "").rstrip("/"),
                                        "size": bucket_data.get("size", 0),
                                        "last_modified": bucket_data.get("lastModified", ""),
                                    }
                                )
                        except json.JSONDecodeError as e:
                            logger.warning(f"Failed to parse bucket data: {e}")

            logger.debug(f"Retrieved {len(bucket_list)} buckets from {alias}")
            return bucket_list

        except Exception as e:
            logger.exception("Failed to list buckets")
            raise MinioExecutionError(f"Bucket listing failed: {e}") from e

    # Convenience methods for common operations

    async def grant_bucket_access(
        self,
        alias: str,
        username: str,
        bucket_name: str,
        permissions: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Grant user access to a bucket by creating and attaching a policy.

        Args:
            alias: MinIO server alias
            username: Username to grant access to
            bucket_name: Bucket name
            permissions: List of permissions (defaults to ["read", "write"])

        Returns:
            Dictionary with operation status and details
        """
        if permissions is None:
            permissions = ["read", "write"]

        try:
            validated_username = self._validate_username(username)
            validated_bucket_name = self._validate_bucket_name(bucket_name)

            # Validate permissions
            valid_permissions = {"read", "write", "delete", "list"}
            for perm in permissions:
                if perm.lower() not in valid_permissions:
                    raise MinioValidationError(f"Invalid permission: {perm}. Valid: {valid_permissions}")

            # Create policy for bucket access
            policy_name = f"{validated_username}-{validated_bucket_name}-policy"

            # Build policy document
            statements = []

            if "list" in permissions or "read" in permissions:
                statements.append(
                    {
                        "Effect": "Allow",
                        "Action": ["s3:ListBucket"],
                        "Resource": [f"arn:aws:s3:::{validated_bucket_name}"],
                    }
                )

            actions = []
            if "read" in permissions:
                actions.extend(["s3:GetObject", "s3:GetObjectVersion"])
            if "write" in permissions:
                actions.extend(["s3:PutObject"])
            if "delete" in permissions:
                actions.append("s3:DeleteObject")

            if actions:
                statements.append(
                    {"Effect": "Allow", "Action": actions, "Resource": [f"arn:aws:s3:::{validated_bucket_name}/*"]}
                )

            policy_document = {"Version": "2012-10-17", "Statement": statements}

            # Create the policy
            policy_result = await self.create_policy(alias, policy_name, policy_document)
            logger.debug(f"Policy creation result: {policy_result}")

            # Attach policy to user
            attach_result = await self.attach_policy(alias, policy_name, validated_username)
            logger.debug(f"Policy attachment result: {attach_result}")

            logger.info(f"Granted {permissions} access on bucket {validated_bucket_name} to user {validated_username}")
            return {
                "status": "granted",
                "message": "Access granted successfully",
                "policy_name": policy_name,
                "permissions": permissions,
            }

        except (MinioValidationError, MinioExecutionError):
            logger.exception("Failed to grant bucket access")
            raise
        except Exception as e:
            logger.exception(f"Failed to grant access to user {username} for bucket {bucket_name}")
            raise MinioExecutionError(f"Granting access failed: {e}") from e


# Factory function for creating connector instances
def create_minio_connector() -> MinioConnector:
    """Factory function to create a MinioConnector instance."""
    return MinioConnector()
