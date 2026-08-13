"""
Kubectl connector for managing Kubernetes resources.

This module provides functionality to interact with Kubernetes clusters using kubectl.
"""

import asyncio
import base64
import json
import logging
import os
from datetime import UTC
from typing import Any

from jinja2 import Template
from tenacity import retry, stop_after_attempt, wait_fixed

from opi.connectors.vpa import VpaContainerRecommendation, parse_vpa_status
from opi.core.cluster_config import assigns_uid_via_scc, get_argo_namespace
from opi.core.config import settings

logger = logging.getLogger(__name__)


def _summarize_kubectl_command(args: list[str]) -> str:
    """Secret-free summary of a kubectl invocation for logging: the operation
    and the target project.

    Returns only the subcommand, the resource kind (the next token when it is a
    plain positional, not a flag) and the target namespace (from
    ``-n``/``--namespace``, which is ``rig-prd-<project>``). It deliberately
    omits every flag value, resource name and stdin, so no secret can ever reach
    the log regardless of the command -- e.g. ``kubectl apply (project
    rig-prd-regel-k4c)`` or ``kubectl get pods (project rig-prd-amt-odc)``.
    """
    if not args:
        return "kubectl"

    parts = [f"kubectl {args[0]}"]

    # Resource kind = first token after the verb, only when it's a plain
    # positional (not a flag/flag-value). Resource names and values are skipped.
    if len(args) > 1 and not args[1].startswith("-"):
        parts.append(args[1])

    namespace = ""
    for i, arg in enumerate(args):
        if arg in ("-n", "--namespace") and i + 1 < len(args):
            namespace = args[i + 1]
        elif arg.startswith("--namespace="):
            namespace = arg.split("=", 1)[1]
    if namespace:
        parts.append(f"(project {namespace})")

    return " ".join(parts)


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

        # Start async retry task if connection failed. If no event loop is
        # running yet (e.g. instantiated from a synchronous test fixture or
        # at import time) skip the retry-task scheduling; first async call
        # will keep working without it and tests no longer crash on init.
        if not KubectlConnector.isConnected:
            try:
                self._retry_task = asyncio.create_task(self._connection_retry())
            except RuntimeError:
                logger.debug("No running event loop; skipping kubectl connection-retry task")
                self._retry_task = None

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
        self,
        args: list[str],
        env: dict[str, str] | None = None,
        stdin_input: str | None = None,
        timeout: int = 60,
    ) -> tuple[str, str, int]:
        """
        Run a kubectl command directly with subprocess.

        Args:
            args: List of kubectl command arguments
            env: Optional environment variables
            stdin_input: Optional string to pass to stdin
            timeout: Maximum seconds to wait for the command to complete (default: 60)

        Returns:
            Tuple of (stdout, stderr, return_code)

        Raises:
            KubectlConnectionError: If kubectl connection is not available
            KubectlExecutionError: If kubectl command fails or times out
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
        # Values-free "operation X on project Y" summary for log lines.
        safe_cmd = _summarize_kubectl_command(args)

        from opi.core.metrics import track_subprocess_memory

        if stdin_input:
            # Use shell execution with EOF markers for stdin input to handle spaces/newlines properly
            shell_cmd = f"{cmd_str} <<'EOF'\n{stdin_input}\nEOF"

            logger.debug(f"Running (stdin): {safe_cmd}")

            # Create shell process
            process = await asyncio.create_subprocess_shell(
                shell_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=cmd_env
            )

            # Wait for command to complete with timeout
            try:
                async with track_subprocess_memory("kubectl"):
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except TimeoutError:
                logger.error(f"Timed out after {timeout}s: {safe_cmd}")
                process.kill()
                await process.wait()
                return "", f"Command timed out after {timeout}s", 1
        else:
            # Use regular exec for commands without stdin
            cmd = ["kubectl"]
            cmd.extend(args)

            logger.debug(f"Running: {safe_cmd}")

            # Create process
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=cmd_env
            )

            # Wait for command to complete with timeout
            try:
                async with track_subprocess_memory("kubectl"):
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except TimeoutError:
                logger.error(f"Timed out after {timeout}s: {safe_cmd}")
                process.kill()
                await process.wait()
                return "", f"Command timed out after {timeout}s", 1

        stdout_str = stdout.decode("utf-8").strip()
        stderr_str = stderr.decode("utf-8").strip()

        if process.returncode != 0:
            logger.warning(f"Failed (code {process.returncode}): {safe_cmd} :: {stderr_str}")

            # Check if this is a connection error and handle it
            if "connection refused" in stderr_str.lower():
                self._handle_connection_failure(stderr_str)
                error_msg = f"kubectl connection failed: {stderr_str}"
                logger.error(error_msg)
                raise KubectlConnectionError(error_msg)
        else:
            logger.debug(f"Succeeded: {safe_cmd}")

        return stdout_str, stderr_str, process.returncode or 0

    async def run_command(
        self, args: list[str], env: dict[str, str] | None = None, stdin_input: str | None = None
    ) -> tuple[str, str, int]:
        """
        Run a kubectl command directly.

        This is a public wrapper around _run_kubectl_command for use by other modules
        that need direct kubectl access (e.g., backup manager).

        Args:
            args: List of kubectl command arguments
            env: Optional environment variables
            stdin_input: Optional input to pass via stdin

        Returns:
            Tuple of (stdout, stderr, return_code)
        """
        return await self._run_kubectl_command(args, env, stdin_input)

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
        result = template.render(assigns_uid_via_scc=assigns_uid_via_scc, **variables)
        # convention: files should end with a newline
        if not result.endswith("\n"):
            result += "\n"
        return result

    async def apply_manifest(
        self, file_path: str, variables: dict[str, Any] | None = None, namespace: str | None = None
    ) -> None:
        """
        Apply a Kubernetes manifest file with variable substitution.

        Args:
            file_path: Path to the manifest file
            variables: Optional dictionary of variables to replace in the manifest
            namespace: Optional namespace to apply the manifest to. If not provided,
                      it will use the namespace specified in the manifest itself.

        Raises:
            KubectlExecutionError: If the apply fails. The message front-loads the
                manifest + namespace and collapses the (often multi-line) server
                error onto one line. The connector does NOT log the failure itself:
                the caller decides whether a failure is really an error (e.g. a
                caller that retries on a transient Capsule RBAC race logs only once
                it has genuinely given up).
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
            reason = " ".join(stderr.split()) or f"kubectl exited {code} with no stderr"
            where = f" in namespace {namespace}" if namespace else ""
            raise KubectlExecutionError(f"Failed to apply manifest {file_path}{where}: {reason}")

        logger.info(f"Successfully applied manifest: {stdout}")

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

    async def secret_exists(self, secret_name: str, namespace: str) -> bool | None:
        """
        Ground-truth existence check for a Secret.

        ``get_secret`` answers ``None`` both for a Secret that is not there and for a
        kubectl call that failed, which is fine for a reader that only wants the contents
        but not for a caller that would CREATE something on the strength of the absence.
        This one keeps the two apart.

        Args:
            secret_name: Name of the secret
            namespace: The namespace that would contain it

        Returns:
            True  - the Secret exists
            False - the Secret is confirmed absent (NotFound)
            None  - the check itself failed; existence is unknown, so the caller must not
                    conclude the Secret is gone
        """
        stdout, stderr, code = await self._run_kubectl_command(
            ["get", "secret", secret_name, "-n", namespace, "-o", "name"]
        )
        if code == 0:
            return True
        if "notfound" in stderr.lower().replace(" ", ""):
            return False
        logger.warning(f"Could not determine existence of secret '{secret_name}' in '{namespace}': {stderr}")
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
            stderr_lower = stderr.lower()
            if "notfound" in stderr_lower or "not found" in stderr_lower or "forbidden" in stderr_lower:
                # Expected during new-project bootstrap: the namespace, the secret, or
                # its Capsule RBAC may not exist yet. Callers treat None as "not present"
                # and retry, so this absence is a warning, not an error to escalate on.
                logger.warning(f"SOPS secret not available in namespace {namespace} yet: {stderr.strip()}")
            else:
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

    async def get_namespace_label_map(self, label_key: str) -> dict[str, str]:
        """Every namespace in the cluster, mapped to its value for ``label_key``.

        One call instead of one per namespace. Startup checks 45 projects across 44
        namespaces; asking per namespace meant 127 ``kubectl get`` invocations (a
        subprocess each) plus 127 unconditional ``kubectl label`` calls, together 70
        of the 83 seconds it took to boot. With this map both questions -- does it
        exist, does it carry the right label -- are answered locally.

        Namespaces without the label are present with an empty string, so a caller
        can tell "missing label" from "missing namespace" (absent from the dict).
        """
        args = ["get", "namespaces", "-o", "json"]
        stdout, stderr, code = await self._run_kubectl_command(args)

        if code != 0:
            # Fall back to per-namespace checks rather than guessing: an empty map
            # would read as "no namespace exists" and trigger creation attempts.
            logger.warning(f"Could not list namespaces: {stderr}")
            raise KubectlExecutionError(f"Failed to list namespaces: {stderr}")

        data = json.loads(stdout)
        result: dict[str, str] = {}
        for item in data.get("items", []):
            meta = item.get("metadata", {})
            name = meta.get("name")
            if name:
                result[name] = (meta.get("labels", {}) or {}).get(label_key, "")
        logger.debug(f"Listed {len(result)} namespaces with label '{label_key}'")
        return result

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

    async def remove_argocd_application_finalizers(self, app_name: str, namespace: str | None = None) -> bool:
        """
        Remove all finalizers from an ArgoCD Application resource.

        This is useful when an ArgoCD Application is stuck because its managed resources
        (e.g., the target namespace) have been deleted out-of-band. Removing the finalizers
        allows the Application to be garbage collected.

        Args:
            app_name: The name of the ArgoCD Application
            namespace: Namespace holding the Application CR; defaults to this instance's cluster

        Returns:
            True if finalizers were successfully removed or app doesn't exist, False on error
        """
        namespace = namespace or get_argo_namespace(settings.CLUSTER_MANAGER)
        logger.info(f"Removing finalizers from ArgoCD Application '{app_name}' in namespace '{namespace}'")

        # Use merge patch to set finalizers to empty array - this won't fail if finalizers don't exist
        patch_args = [
            "patch",
            "application",
            app_name,
            "-n",
            namespace,
            "--type",
            "merge",
            "-p",
            '{"metadata":{"finalizers":[]}}',
        ]

        stdout, stderr, code = await self._run_kubectl_command(patch_args)

        if code == 0:
            logger.info(f"Successfully removed finalizers from ArgoCD Application '{app_name}'")
            return True
        elif "not found" in stderr.lower():
            # Application doesn't exist - consider this success
            logger.info(f"ArgoCD Application '{app_name}' does not exist")
            return True
        else:
            logger.error(f"Failed to remove finalizers from ArgoCD Application '{app_name}': {stderr}")
            return False

    async def delete_argocd_application(self, app_name: str, namespace: str | None = None) -> bool:
        """
        Delete an ArgoCD Application directly using kubectl.

        This is a fallback when GitOps-based deletion doesn't work (e.g., parent app not syncing).

        Args:
            app_name: The name of the ArgoCD Application
            namespace: Namespace holding the Application CR; defaults to this instance's cluster

        Returns:
            True if successfully deleted or app doesn't exist, False on error
        """
        namespace = namespace or get_argo_namespace(settings.CLUSTER_MANAGER)
        logger.info(f"Deleting ArgoCD Application '{app_name}' in namespace '{namespace}'")

        delete_args = ["delete", "application", app_name, "-n", namespace, "--ignore-not-found=true"]

        stdout, stderr, code = await self._run_kubectl_command(delete_args)

        if code == 0:
            logger.info(f"Successfully deleted ArgoCD Application '{app_name}'")
            return True
        else:
            logger.error(f"Failed to delete ArgoCD Application '{app_name}': {stderr}")
            return False

    async def argocd_application_exists(self, app_name: str, namespace: str | None = None) -> bool | None:
        """
        Ground-truth existence check for an ArgoCD Application CR via the Kubernetes API.

        This is the honest fallback for the ArgoCD API, which returns an ambiguous
        'permission denied' when its cache is stalled (see
        ArgoConnector.wait_for_application_deletion). The Kubernetes API instead answers
        cleanly: the object exists, a definitive NotFound, or a transport error we can
        tell apart.

        Args:
            app_name: The name of the ArgoCD Application
            namespace: Namespace holding the Application CR; defaults to this instance's cluster

        Returns:
            True  - the Application CR exists
            False - the Application CR is confirmed absent (NotFound)
            None  - the check itself failed; existence is unknown, so the caller must
                    not conclude the application is gone
        """
        namespace = namespace or get_argo_namespace(settings.CLUSTER_MANAGER)
        stdout, stderr, code = await self._run_kubectl_command(
            ["get", "application", app_name, "-n", namespace, "-o", "name"]
        )
        if code == 0:
            return True
        if "notfound" in stderr.lower().replace(" ", ""):
            return False
        logger.warning(f"Could not determine existence of ArgoCD Application '{app_name}': {stderr}")
        return None

    async def wait_for_capsule_tenant_label(self, namespace: str, timeout: int = 30) -> bool:
        """
        Wait for Capsule to assign the tenant label to a namespace.

        Capsule's admission webhook assigns an ownerReference and tenant label to namespaces
        created by tenant users. Once this label is present, it's safe to modify the namespace
        with labels and annotations.

        Args:
            namespace: The namespace to check
            timeout: Maximum time to wait in seconds (default: 30)

        Returns:
            True if the tenant label was found within the timeout, False otherwise
        """
        logger.info(f"Waiting for Capsule to assign tenant label to namespace '{namespace}'")

        start_time = asyncio.get_event_loop().time()
        poll_interval = 1.0

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time

            if elapsed >= timeout:
                logger.error(
                    f"Timeout waiting for Capsule tenant label on namespace '{namespace}' after {timeout} seconds"
                )
                return False

            args = ["get", "namespace", namespace, "-o", "jsonpath={.metadata.labels.capsule\\.clastix\\.io/tenant}"]

            stdout, stderr, code = await self._run_kubectl_command(args)

            if code == 0 and stdout.strip():
                tenant_name = stdout.strip()
                logger.info(
                    f"Capsule tenant label found on namespace '{namespace}': tenant='{tenant_name}' "
                    f"(waited {elapsed:.1f}s)"
                )
                return True

            logger.debug(f"Capsule tenant label not yet present on namespace '{namespace}', waiting...")
            await asyncio.sleep(poll_interval)

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
        self,
        resource_type: str,
        resource_name: str,
        label_key: str,
        label_value: str,
        namespace: str | None = None,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ) -> bool:
        """
        Apply a label to a specific Kubernetes resource.

        Includes retry logic to handle transient failures from admission webhooks
        (e.g., Capsule) that may temporarily deny requests during reconciliation.

        Args:
            resource_type: The type of resource (e.g., 'namespace', 'pod', 'service')
            resource_name: The name of the resource
            label_key: The label key to apply
            label_value: The label value to apply
            namespace: The namespace of the resource (not needed for cluster-scoped resources like namespaces)
            max_retries: Maximum number of retry attempts (default: 3)
            retry_delay: Delay in seconds between retry attempts (default: 1.0)

        Returns:
            True if the label was applied successfully, False otherwise
        """
        logger.debug(f"Applying label {label_key}={label_value} to {resource_type}/{resource_name}")

        # Build the kubectl label command with --overwrite to handle existing labels
        args = ["label", resource_type, resource_name, f"{label_key}={label_value}", "--overwrite"]

        # Add namespace flag if provided and not a cluster-scoped resource
        if namespace and resource_type.lower() != "namespace":
            args.extend(["-n", namespace])

        last_error = ""
        for attempt in range(1, max_retries + 1):
            try:
                stdout, stderr, code = await self._run_kubectl_command(args)

                if code == 0:
                    logger.info(
                        f"Successfully applied label {label_key}={label_value} to {resource_type}/{resource_name}"
                    )
                    return True

                last_error = stderr
                if attempt < max_retries:
                    logger.warning(
                        f"Failed to apply label to {resource_type}/{resource_name} (attempt {attempt}/{max_retries}): "
                        f"{stderr}. Retrying in {retry_delay}s..."
                    )
                    await asyncio.sleep(retry_delay)

            except Exception as e:
                last_error = str(e)
                if attempt < max_retries:
                    logger.warning(
                        f"Error applying label to {resource_type}/{resource_name} (attempt {attempt}/{max_retries}): "
                        f"{e}. Retrying in {retry_delay}s..."
                    )
                    await asyncio.sleep(retry_delay)

        logger.error(
            f"Failed to apply label to {resource_type}/{resource_name} after {max_retries} attempts: {last_error}"
        )
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

        Uses label selector instead of deployment/ to avoid needing deployment
        get permissions - only requires pods and pods/log permissions.

        Args:
            deployment_name: Name of the deployment
            namespace: Namespace containing the deployment
            lines: Number of recent lines to retrieve (default: 100)

        Returns:
            List of log lines from all pods in the deployment
        """
        logger.debug(f"Getting logs for deployment {deployment_name} in namespace {namespace}")

        try:
            # Use label selector instead of deployment/ to only require pod permissions
            args = ["logs", "-l", f"app={deployment_name}", "-n", namespace, f"--tail={lines}"]
            stdout, stderr, code = await self._run_kubectl_command(args)

            if code != 0:
                logger.warning(f"Failed to get deployment logs: {stderr}")

            # Split logs into lines, filter out empty lines
            log_lines = [line for line in stdout.split("\n") if line.strip()]

            # If no logs found (e.g. CrashLoopBackOff with no running container),
            # try fetching previous container logs
            if not log_lines:
                prev_args = ["logs", "-l", f"app={deployment_name}", "-n", namespace, f"--tail={lines}", "--previous"]
                prev_stdout, _, prev_code = await self._run_kubectl_command(prev_args)
                if prev_code == 0 and prev_stdout.strip():
                    log_lines = ["[previous container logs]"] + [
                        line for line in prev_stdout.split("\n") if line.strip()
                    ]

            return log_lines

        except Exception as e:
            logger.error(f"Error getting deployment logs: {e}")
            return []

    async def stream_deployment_logs(
        self, deployment_name: str, namespace: str, lines: int = 100
    ) -> asyncio.subprocess.Process | None:
        """
        Start streaming logs from a deployment using kubectl logs -f.

        Uses label selector instead of deployment/ to avoid needing deployment
        get permissions - only requires pods and pods/log permissions.

        Returns a subprocess that streams logs in real-time. The caller is
        responsible for reading from process.stdout, terminating the process
        when done, and restarting it if it exits (which it does for pods in
        CrashLoopBackOff or after the matched pod terminates).

        Args:
            deployment_name: Name of the deployment
            namespace: Namespace containing the deployment
            lines: Number of historical lines to retrieve initially. Pass 0
                on reattach so the follower only streams NEW output and does
                not re-dump the same stored tail every backoff cycle.

        Returns:
            Subprocess with stdout stream, or None if failed to start
        """
        if not KubectlConnector.isConnected:
            logger.error("kubectl connection is not available for log streaming")
            return None

        logger.debug(f"Starting log stream for deployment {deployment_name} in namespace {namespace} (tail={lines})")

        try:
            # Use label selector instead of deployment/ to only require pod permissions
            cmd = [
                "kubectl",
                "logs",
                "-f",
                "-l",
                f"app={deployment_name}",
                "-n",
                namespace,
                f"--tail={lines}",
            ]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
            )

            logger.info(f"Started log stream for {deployment_name} in {namespace} (PID: {process.pid}, tail={lines})")
            return process

        except Exception as e:
            logger.error(f"Error starting log stream for {deployment_name}: {e}")
            return None

    # Event object prefixes and reasons that are infrastructure noise —
    # not actionable by project users.
    _IGNORED_EVENT_PREFIXES = ("cm-acme-",)
    _IGNORED_EVENT_REASONS = (
        "FailedToUpdateEndpoint",
        "FailedIngressToRouteConversion",
    )

    async def get_namespace_events(
        self,
        namespace: str,
        limit: int = 50,
        event_type: str | None = "Warning",
        max_age_hours: float = 2,
    ) -> list[dict[str, str]]:
        """
        Get recent events from a namespace.

        Args:
            namespace: Namespace to get events from
            limit: Maximum number of events to retrieve (default: 50)
            event_type: Filter by event type (default: "Warning"), None for all
            max_age_hours: Only return events younger than this (default: 2)

        Returns:
            List of event dictionaries with keys: type, reason, object, kind, message, time
        """
        from datetime import datetime

        logger.debug(f"Getting events for namespace {namespace}")

        try:
            args = ["get", "events", "-n", namespace, "--sort-by=.metadata.creationTimestamp", "-o", "json"]
            if event_type:
                args.extend(["--field-selector", f"type={event_type}"])
            stdout, stderr, code = await self._run_kubectl_command(args)

            if code != 0:
                logger.warning(f"Failed to get namespace events: {stderr}")
                return []

            import json

            events_data = json.loads(stdout)
            now = datetime.now(UTC)
            events: list[dict[str, str]] = []
            for event in reversed(events_data.get("items", [])):
                timestamp = event.get("metadata", {}).get("creationTimestamp", "")
                if timestamp and max_age_hours > 0:
                    try:
                        event_dt = datetime.fromisoformat(timestamp)
                        if (now - event_dt).total_seconds() / 3600 > max_age_hours:
                            continue
                    except ValueError, TypeError:
                        pass

                # Filter out infrastructure noise that users can't act on
                obj_name = event.get("involvedObject", {}).get("name", "")
                reason = event.get("reason", "")
                if any(obj_name.startswith(p) for p in self._IGNORED_EVENT_PREFIXES):
                    continue
                if reason in self._IGNORED_EVENT_REASONS:
                    continue

                events.append(
                    {
                        "type": event.get("type", ""),
                        "reason": reason,
                        "object": obj_name,
                        "kind": event.get("involvedObject", {}).get("kind", ""),
                        "message": event.get("message", ""),
                        "time": timestamp,
                    }
                )
                if len(events) >= limit:
                    break

            return events

        except Exception as e:
            logger.error(f"Error getting namespace events: {e}")
            return []

    async def get_pod_container_image(self, namespace: str, pod_name: str, container_name: str) -> str | None:
        """Get the image a running container was started from.

        Read from ``.spec.containers[]`` rather than from a deployment or an overlay:
        the pod is what is actually running, and during a rolling update the two pods
        behind one Service differ exactly here.

        Args:
            namespace: Namespace the pod runs in
            pod_name: Name of the pod
            container_name: Name of the container within the pod

        Returns:
            The image reference, or None when the pod or container cannot be read.
        """
        args = [
            "get",
            "pod",
            pod_name,
            "-n",
            namespace,
            "-o",
            f"jsonpath={{.spec.containers[?(@.name=='{container_name}')].image}}",
        ]
        stdout, stderr, code = await self._run_kubectl_command(args, timeout=15)
        if code != 0:
            logger.warning(f"Could not read image of pod {namespace}/{pod_name}: {stderr}")
            return None
        return stdout.strip() or None

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

    async def get_deployment_conditions(self, namespace: str, deployment_name: str) -> list[dict[str, str]] | None:
        """
        Get the status conditions of a specific deployment.

        Args:
            namespace: Namespace of the deployment
            deployment_name: Name of the deployment

        Returns:
            List of condition dicts (with keys: type, status, reason, message),
            or None if the deployment was not found.
        """
        try:
            args = [
                "get",
                "deployment",
                deployment_name,
                "-n",
                namespace,
                "-o",
                "json",
            ]
            stdout, stderr, code = await self._run_kubectl_command(args)

            if code != 0:
                logger.debug(f"Deployment '{deployment_name}' not found in {namespace}: {stderr}")
                return None

            data = json.loads(stdout)
            return data.get("status", {}).get("conditions", [])

        except Exception as e:
            logger.warning(f"Error getting deployment conditions for {deployment_name}: {e}")
            return None

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

    async def get_vpa_recommendation(
        self, namespace: str, workload_name: str, container_name: str = "app"
    ) -> VpaContainerRecommendation | None:
        """
        Read a VerticalPodAutoscaler's recommendation for one container.

        Returns the normalized recommendation (CPU in millicores, memory in
        MiB), or None when the VPA is missing, its status is not yet populated
        (freshly created), or the values cannot be parsed.

        Args:
            namespace: Namespace containing the VPA
            workload_name: VPA object name (matches the Deployment name)
            container_name: Container whose recommendation to read (default "app")
        """
        try:
            args = ["get", "verticalpodautoscaler", workload_name, "-n", namespace, "-o", "json"]
            stdout, stderr, code = await self._run_kubectl_command(args)

            if code != 0:
                if "NotFound" in stderr:
                    logger.debug(f"No VPA '{workload_name}' in namespace {namespace}")
                else:
                    logger.warning(f"Failed to get VPA '{workload_name}': {stderr}")
                return None

            return parse_vpa_status(json.loads(stdout), container_name)

        except Exception as e:
            logger.error(f"Error getting VPA recommendation for '{workload_name}': {e}")
            return None

    async def get_resources_by_label(
        self, resource_type: str, namespace: str, label_selector: str
    ) -> list[dict[str, Any]]:
        """
        Get Kubernetes resources matching a label selector.

        Args:
            resource_type: The type of resource to get (e.g., 'ingress', 'deployment', 'service')
            namespace: The namespace to search in
            label_selector: Label selector string (e.g., 'app.kubernetes.io/part-of=myapp')

        Returns:
            List of resource dictionaries with metadata
        """
        logger.debug(f"Getting {resource_type} resources in namespace {namespace} with selector: {label_selector}")

        try:
            args = [
                "get",
                resource_type,
                "-n",
                namespace,
                "-l",
                label_selector,
                "-o",
                "json",
            ]

            stdout, stderr, code = await self._run_kubectl_command(args)

            if code != 0:
                if "No resources found" in stderr or not stdout.strip():
                    logger.debug(f"No {resource_type} resources found with selector {label_selector}")
                    return []
                else:
                    logger.error(f"Failed to get {resource_type} resources: {stderr}")
                    return []

            import json

            result = json.loads(stdout)
            items = result.get("items", [])
            logger.debug(f"Found {len(items)} {resource_type} resources with selector {label_selector}")
            return items

        except Exception as e:
            logger.error(f"Error getting {resource_type} resources: {e}")
            return []


# TODO: remove this method and make direct calles to create KubectlConnector()
def create_kubectl_connector() -> KubectlConnector:
    """
    Create and return a KubectlConnector instance.

    When running inside a Kubernetes cluster, the connector will automatically use
    the service account mounted into the pod.

    Returns:
        KubectlConnector instance
    """
    # Deliberately not logged: KubectlConnector is a singleton, so this returns the
    # one existing instance and creates nothing. Logging "Creating ..." here put a
    # line in the log for every caller, with no work behind it -- the real creation
    # is logged once by __init__. Every periodic job calls this, so the noise was
    # continuous.
    return KubectlConnector()
