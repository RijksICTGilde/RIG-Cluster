"""
Kubectl connector for managing Kubernetes resources.

This module provides functionality to interact with Kubernetes clusters using kubectl.
"""

import asyncio
import base64
import logging
import os
from typing import Any

from tenacity import retry, stop_after_attempt, wait_fixed

logger = logging.getLogger(__name__)
from jinja2 import Template


class KubectlConnectionError(Exception):
    """Exception raised when kubectl connection is not available."""


class KubectlExecutionError(Exception):
    """Exception raised when kubectl command execution fails."""


# TODO: consider using the kubernetes API instead of kubectl commands
class KubectlConnector:
    """Connector for interacting with Kubernetes clusters using kubectl."""

    _instance = None
    isConnected = False
    _retry_task = None

    def __new__(cls):
        """Implement singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        """
        Initialize the Kubectl connector.

        When running inside a Kubernetes cluster, the connector will automatically
        use the service account mounted into the pod.
        """
        if self._initialized:
            return

        logger.debug("Initializing KubectlConnector")

        # Setup env variables for kubectl
        self.env = os.environ.copy()

        # Test connection synchronously during initialization
        # This ensures isConnected is set before any commands are run
        try:
            import subprocess

            result = subprocess.run(
                ["kubectl", "auth", "whoami"], capture_output=True, text=True, env=self.env, timeout=10
            )
            if result.returncode == 0:
                logger.info("Kubectl connection successful")
                KubectlConnector.isConnected = True
            else:
                logger.warning(f"Kubectl connection failed: {result.stderr}")
                KubectlConnector.isConnected = False
        except Exception as e:
            logger.error(f"Error testing kubectl connection: {e}")
            KubectlConnector.isConnected = False

        # Start async retry task if connection failed
        if not KubectlConnector.isConnected:
            asyncio.create_task(self._connection_retry())

        self._initialized = True
        logger.debug("KubectlConnector initialized successfully")

    async def _test_connection(self) -> bool:
        """
        Test kubectl connection using 'kubectl cluster-info'.

        Returns:
            True if connection is successful, False otherwise
        """
        try:
            logger.debug("Testing kubectl connection")

            cmd = ["kubectl", "auth", "whoami"]

            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=self.env
            )

            stdout, stderr = await process.communicate()
            stderr_str = stderr.decode("utf-8").lower()

            if process.returncode == 0:
                logger.info("Kubectl connection successful")
                KubectlConnector.isConnected = True
                return True
            else:
                logger.warning(f"Kubectl connection failed: {stderr_str}")
                KubectlConnector.isConnected = False
                self._handle_connection_failure(stderr_str)
                return False

        except Exception as e:
            logger.error(f"Error testing kubectl connection: {e}")
            KubectlConnector.isConnected = False
            return False

    def _handle_connection_failure(self, stderr_str: str):
        """Handle connection failure by setting status and starting retry task."""
        if "connection refused" in stderr_str.lower():
            KubectlConnector.isConnected = False
            # Start retry task if not already running (non-blocking)
            if KubectlConnector._retry_task is None or KubectlConnector._retry_task.done():
                KubectlConnector._retry_task = asyncio.create_task(self._connection_retry())

    @retry(stop=stop_after_attempt(999999), wait=wait_fixed(30))  # Retry indefinitely every 30 seconds
    async def _connection_retry(self):
        """Background retry task using tenacity."""
        logger.debug("Retrying kubectl connection...")
        success = await self._test_connection()
        if success:
            logger.info("Kubectl connection restored")
            return  # Success - stop retrying
        else:
            # Raise exception to trigger tenacity retry
            raise KubectlConnectionError("Connection still failed")

    async def _run_kubectl_command(
        self, args: list[str], env: dict[str, str] | None = None, stdin_input: str | None = None
    ) -> tuple[str, str, int]:
        """
        Run a kubectl command directly with subprocess.

        Args:
            args: List of kubectl command arguments
            env: Optional environment variables
            stdin_input: Optional string to pass to stdin

        Returns:
            Tuple of (stdout, stderr, return_code)

        Raises:
            KubectlConnectionError: If kubectl connection is not available
            KubectlExecutionError: If kubectl command fails
        """
        # Check connection before running command
        if not KubectlConnector.isConnected:
            raise KubectlConnectionError("kubectl connection is not available")

        # Set up environment
        cmd_env = self.env.copy()
        if env:
            cmd_env.update(env)

        # Create cmd_str for logging regardless of execution path
        cmd_args_str = " ".join([f'"{arg}"' if " " in arg else arg for arg in args])
        cmd_str = f"kubectl {cmd_args_str}"

        if stdin_input:
            # Use shell execution with EOF markers for stdin input to handle spaces/newlines properly
            shell_cmd = f"{cmd_str} <<'EOF'\n{stdin_input}\nEOF"

            logger.debug("Running kubectl shell command with stdin")

            # Create shell process
            process = await asyncio.create_subprocess_shell(
                shell_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=cmd_env
            )

            # Wait for command to complete
            stdout, stderr = await process.communicate()
        else:
            # Use regular exec for commands without stdin
            cmd = ["kubectl"]
            cmd.extend(args)

            logger.debug(f"Running kubectl command: {cmd_str}")

            # Create process
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=cmd_env
            )

            # Wait for command to complete
            stdout, stderr = await process.communicate()
        stdout_str = stdout.decode("utf-8").strip()
        stderr_str = stderr.decode("utf-8").strip()

        if process.returncode != 0:
            logger.warning(f"kubectl command failed with code {process.returncode}: {stderr_str}")

            # Check if this is a connection error and handle it
            if "connection refused" in stderr_str.lower():
                self._handle_connection_failure(stderr_str)
                error_msg = f"kubectl connection failed: {stderr_str}"
                logger.error(error_msg)
                raise KubectlConnectionError(error_msg)
        else:
            logger.debug(f"kubectl command succeeded: {cmd_str}")

        return stdout_str, stderr_str, process.returncode

    def template_manifest(self, manifest_content: str, variables: dict[str, Any]) -> str:
        """
        Process Jinja2 template variables in a manifest.

        This implementation handles standard Jinja2 templating syntax.
        Variables in the manifest should be in the format {{ variable }} and support Jinja2 features.

        Args:
            manifest_content: The content of the manifest file
            variables: Dictionary of variables to replace

        Returns:
            The processed manifest content with variables replaced
        """
        logger.debug(f"Templating manifest with variables: {variables.keys()}")

        template = Template(manifest_content)
        result = template.render(**variables)
        # convention: files should end with a newline
        if not result.endswith("\n"):
            result += "\n"
        return result

    async def apply_manifest(
        self, file_path: str, variables: dict[str, Any] | None = None, namespace: str | None = None
    ) -> bool:
        """
        Apply a Kubernetes manifest file with variable substitution.

        Args:
            file_path: Path to the manifest file
            variables: Optional dictionary of variables to replace in the manifest
            namespace: Optional namespace to apply the manifest to. If not provided,
                      it will use the namespace specified in the manifest itself.

        Returns:
            True if the apply was successful, False otherwise
        """
        logger.debug(f"Applying manifest: {file_path}{' in namespace ' + namespace if namespace else ''}")

        # Read the manifest file
        with open(file_path) as f:
            manifest_content = f.read()

        if variables:
            manifest_content = self.template_manifest(manifest_content, variables)

        # Apply manifest using stdin instead of temp file
        args = ["apply", "-f", "-"]

        if namespace:
            args.extend(["-n", namespace])

        stdout, stderr, code = await self._run_kubectl_command(args, stdin_input=manifest_content)

        if code != 0:
            error_msg = f"Failed to apply manifest: {stderr}"
            logger.error(error_msg)
            return False

        logger.info(f"Successfully applied manifest: {stdout}")
        return True

    async def get_secret(self, secret_name: str, namespace: str) -> dict[str, str] | None:
        """
        Retrieve a secret from Kubernetes and return its data as a dictionary.

        Args:
            secret_name: Name of the secret to retrieve
            namespace: The namespace containing the secret

        Returns:
            Dictionary with secret data (decoded from base64) if found, None otherwise
        """
        logger.debug(f"Retrieving secret {secret_name} from namespace {namespace}")

        # Get the secret from Kubernetes in JSON format
        args = ["get", "secret", secret_name, "-n", namespace, "-o", "json"]
        stdout, stderr, code = await self._run_kubectl_command(args)

        if code != 0:
            if "NotFound" in stderr:
                logger.debug(f"Secret {secret_name} not found in namespace {namespace}")
            else:
                logger.error(f"Failed to retrieve secret {secret_name}: {stderr}")
            return None

        # Parse the JSON output
        import json

        try:
            secret_data = json.loads(stdout)
            data = secret_data.get("data", {})

            # Decode base64-encoded values
            decoded_data = {}
            for key, value in data.items():
                if value:
                    decoded_data[key] = base64.b64decode(value).decode("utf-8")
                else:
                    decoded_data[key] = ""

            return decoded_data

        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Failed to parse secret data: {e}")
            return None

    async def get_sops_secret_from_namespace(self, namespace: str) -> str | None:
        """
        Retrieve the SOPS AGE private key from the specified namespace.

        Args:
            namespace: The namespace to retrieve the secret from

        Returns:
            The private key if found, None otherwise
        """
        logger.debug(f"Retrieving SOPS secret from namespace: {namespace}")

        # Get the secret from Kubernetes
        args = ["get", "secret", "sops-age-key", "-n", namespace, "-o", "jsonpath={.data.key}"]

        stdout, stderr, code = await self._run_kubectl_command(args)

        if code != 0:
            logger.error(f"Failed to retrieve SOPS secret: {stderr}")
            return None

        # Decode the base64 encoded key
        encoded_key = stdout.strip()
        if not encoded_key:
            logger.error("Empty key data received")
            return None

        decoded_key = base64.b64decode(encoded_key).decode("utf-8")
        logger.debug("Successfully retrieved SOPS secret")
        return decoded_key

    async def namespace_exists(self, namespace: str) -> bool:
        """
        Check if a namespace exists in the cluster.

        Args:
            namespace: The namespace to check

        Returns:
            True if the namespace exists, False otherwise
        """
        logger.debug(f"Checking if namespace exists: {namespace}")

        # Check if the namespace exists
        args = ["get", "namespace", namespace]

        stdout, stderr, code = await self._run_kubectl_command(args)

        if code == 0:
            logger.debug(f"Namespace {namespace} exists")
            return True
        else:
            logger.debug(f"Namespace {namespace} does not exist")
            return False

    async def delete_namespace(self, namespace: str) -> bool:
        """
        Delete a namespace from the cluster.

        Args:
            namespace: The namespace to delete

        Returns:
            True if the namespace was successfully deleted or didn't exist, False otherwise
        """
        logger.debug(f"Deleting namespace: {namespace}")

        # Delete the namespace with ignore-not-found to handle cases where it doesn't exist
        args = ["delete", "namespace", namespace, "--ignore-not-found=true"]

        stdout, stderr, code = await self._run_kubectl_command(args)

        if code == 0:
            logger.debug(f"Successfully deleted namespace: {namespace}")
            return True
        else:
            logger.error(f"Failed to delete namespace {namespace}: {stderr}")
            return False

    async def encrypt_file_with_sops(self, file_path: str, public_key: str, output_path: str) -> bool:
        """
        Encrypt a file using SOPS with the specified AGE public key.

        Args:
            file_path: Path to the file to encrypt
            public_key: The AGE public key for encryption
            output_path: Path where the encrypted file should be saved

        Returns:
            True if the file was encrypted successfully, False otherwise
        """
        logger.debug(f"Encrypting file {file_path} with SOPS")

        try:
            # Run SOPS encrypt command
            args = ["sops", "--encrypt", "--age", public_key, file_path]

            process = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                logger.error(f"Failed to encrypt file with SOPS: {stderr.decode()}")
                return False

            # Write the encrypted content to the output file
            with open(output_path, "w") as f:
                f.write(stdout.decode())

            logger.info(f"Successfully encrypted file: {output_path}")
            return True

        except Exception as e:
            logger.error(f"Error encrypting file with SOPS: {e}")
            return False

    async def apply_label_to_resource(
        self, resource_type: str, resource_name: str, label_key: str, label_value: str, namespace: str | None = None
    ) -> bool:
        """
        Apply a label to a specific Kubernetes resource.

        Args:
            resource_type: The type of resource (e.g., 'namespace', 'pod', 'service')
            resource_name: The name of the resource
            label_key: The label key to apply
            label_value: The label value to apply
            namespace: The namespace of the resource (not needed for cluster-scoped resources like namespaces)

        Returns:
            True if the label was applied successfully, False otherwise
        """
        logger.debug(f"Applying label {label_key}={label_value} to {resource_type}/{resource_name}")

        try:
            # Build the kubectl label command
            args = ["label", resource_type, resource_name, f"{label_key}={label_value}"]

            # Add namespace flag if provided and not a cluster-scoped resource
            if namespace and resource_type.lower() != "namespace":
                args.extend(["-n", namespace])

            stdout, stderr, code = await self._run_kubectl_command(args)

            if code != 0:
                error_msg = f"Failed to apply label to {resource_type}/{resource_name}: {stderr}"
                logger.error(error_msg)
                return False

            logger.info(f"Successfully applied label {label_key}={label_value} to {resource_type}/{resource_name}")
            return True

        except Exception as e:
            logger.error(f"Error applying label to {resource_type}/{resource_name}: {e}")
            return False

    async def apply_annotation_to_resource(
        self,
        resource_type: str,
        resource_name: str,
        annotation_key: str,
        annotation_value: str,
        namespace: str | None = None,
    ) -> bool:
        """
        Apply an annotation to a specific Kubernetes resource.

        Args:
            resource_type: The type of resource (e.g., 'namespace', 'pod', 'service')
            resource_name: The name of the resource
            annotation_key: The annotation key to apply
            annotation_value: The annotation value to apply
            namespace: The namespace of the resource (not needed for cluster-scoped resources like namespaces)

        Returns:
            True if the annotation was applied successfully, False otherwise
        """
        logger.debug(f"Applying annotation {annotation_key}={annotation_value} to {resource_type}/{resource_name}")

        try:
            # Build the kubectl annotate command
            args = ["annotate", resource_type, resource_name, f"{annotation_key}={annotation_value}"]

            # Add namespace flag if provided and not a cluster-scoped resource
            if namespace and resource_type.lower() != "namespace":
                args.extend(["-n", namespace])

            stdout, stderr, code = await self._run_kubectl_command(args)

            if code != 0:
                error_msg = f"Failed to apply annotation to {resource_type}/{resource_name}: {stderr}"
                logger.error(error_msg)
                return False

            logger.info(
                f"Successfully applied annotation {annotation_key}={annotation_value} to {resource_type}/{resource_name}"
            )
            return True

        except Exception as e:
            logger.error(f"Error applying annotation to {resource_type}/{resource_name}: {e}")
            return False

    async def get_deployment_logs(self, deployment_name: str, namespace: str, lines: int = 100) -> list[str]:
        """
        Get logs from all pods belonging to a deployment.

        Args:
            deployment_name: Name of the deployment
            namespace: Namespace containing the deployment
            lines: Number of recent lines to retrieve (default: 100)

        Returns:
            List of log lines from all pods in the deployment
        """
        logger.debug(f"Getting logs for deployment {deployment_name} in namespace {namespace}")

        try:
            # Get logs from deployment (kubectl will aggregate from all pods)
            args = ["logs", f"deployment/{deployment_name}", "-n", namespace, f"--tail={lines}"]
            stdout, stderr, code = await self._run_kubectl_command(args)

            if code != 0:
                logger.warning(f"Failed to get deployment logs: {stderr}")
                return []

            # Split logs into lines, filter out empty lines
            log_lines = [line for line in stdout.split("\n") if line.strip()]
            return log_lines

        except Exception as e:
            logger.error(f"Error getting deployment logs: {e}")
            return []

    async def get_namespace_events(self, namespace: str, limit: int = 50) -> list[dict[str, str]]:
        """
        Get recent events from a namespace.

        Args:
            namespace: Namespace to get events from
            limit: Maximum number of events to retrieve (default: 50)

        Returns:
            List of event dictionaries with keys: type, reason, object, message, time
        """
        logger.debug(f"Getting events for namespace {namespace}")

        try:
            # Get events in JSON format for easier parsing
            args = ["get", "events", "-n", namespace, "--sort-by=.metadata.creationTimestamp", "-o", "json"]
            stdout, stderr, code = await self._run_kubectl_command(args)

            if code != 0:
                logger.warning(f"Failed to get namespace events: {stderr}")
                return []

            import json

            events_data = json.loads(stdout)
            events = []

            # Process events (newest first, limited by limit parameter)
            for event in list(reversed(events_data.get("items", [])))[:limit]:
                events.append(
                    {
                        "type": event.get("type", ""),
                        "reason": event.get("reason", ""),
                        "object": event.get("involvedObject", {}).get("name", ""),
                        "message": event.get("message", ""),
                        "time": event.get("metadata", {}).get("creationTimestamp", ""),
                    }
                )

            return events

        except Exception as e:
            logger.error(f"Error getting namespace events: {e}")
            return []

    async def get_deployment_status(self, namespace: str, deployment_name: str | None = None) -> list[dict[str, str]]:
        """
        Get status of deployments in a namespace, optionally filtered by deployment name.

        Args:
            namespace: Namespace to check
            deployment_name: Optional specific deployment name to check

        Returns:
            List of deployment status dictionaries with keys: name, ready, replicas, available, updated
        """
        logger.debug(f"Getting deployment status in namespace {namespace}")

        try:
            args = ["get", "deployments", "-n", namespace, "-o", "json"]

            stdout, stderr, code = await self._run_kubectl_command(args)

            if code != 0:
                logger.warning(f"Failed to get deployment status: {stderr}")
                return []

            import json

            deployments_data = json.loads(stdout)
            deployments = []

            for deployment in deployments_data.get("items", []):
                metadata = deployment.get("metadata", {})
                status = deployment.get("status", {})
                spec = deployment.get("spec", {})

                name = metadata.get("name", "")

                # Filter by deployment name if specified
                if deployment_name and name != deployment_name:
                    continue

                # Calculate deployment status
                desired_replicas = spec.get("replicas", 0)
                ready_replicas = status.get("readyReplicas", 0)
                available_replicas = status.get("availableReplicas", 0)
                updated_replicas = status.get("updatedReplicas", 0)

                deployments.append(
                    {
                        "name": name,
                        "ready": f"{ready_replicas}/{desired_replicas}",
                        "replicas": str(desired_replicas),
                        "available": str(available_replicas),
                        "updated": str(updated_replicas),
                    }
                )

            return deployments

        except Exception as e:
            logger.error(f"Error getting deployment status: {e}")
            return []

    async def delete_resource(self, resource_type: str, resource_name: str, namespace: str | None = None) -> bool:
        """
        Delete a Kubernetes resource.

        Args:
            resource_type: The type of resource to delete (e.g., 'secret', 'pod', 'deployment')
            resource_name: The name of the resource to delete
            namespace: The namespace containing the resource (not needed for cluster-scoped resources)

        Returns:
            True if the resource was deleted successfully, False otherwise
        """
        logger.debug(f"Deleting {resource_type} {resource_name}{' in namespace ' + namespace if namespace else ''}")

        try:
            args = ["delete", resource_type, resource_name]

            if namespace:
                args.extend(["-n", namespace])

            stdout, stderr, code = await self._run_kubectl_command(args)

            if code != 0:
                if "NotFound" in stderr:
                    logger.debug(f"{resource_type} {resource_name} not found - already deleted")
                    return True
                else:
                    error_msg = f"Failed to delete {resource_type} {resource_name}: {stderr}"
                    logger.error(error_msg)
                    return False

            logger.info(f"Successfully deleted {resource_type} {resource_name}")
            return True

        except Exception as e:
            logger.error(f"Error deleting {resource_type} {resource_name}: {e}")
            return False


# TODO: remove this method and make direct calles to create KubectlConnector()
def create_kubectl_connector() -> KubectlConnector:
    """
    Create and return a KubectlConnector instance.

    When running inside a Kubernetes cluster, the connector will automatically use
    the service account mounted into the pod.

    Returns:
        KubectlConnector instance
    """
    logger.debug("Creating KubectlConnector")
    return KubectlConnector()
