"""Read and change Kubernetes resources through the kubectl binary."""

import asyncio
import base64
import json
import logging
import re
import subprocess
from datetime import UTC
from typing import Any, Self

from opi.connectors.vpa import VpaContainerRecommendation, parse_vpa_status
from opi.core.cluster_config import get_argo_namespace, is_platform_namespace
from opi.core.config import settings
from opi.core.subprocess_env import child_env
from opi.generation.manifests import render_template_source
from opi.services.catalog.base import APPLICATION_CONTAINER_NAME, deployment_pod_selector
from opi.utils.argocd_tracking import TrackedResource, tracked_resource_from_item
from opi.utils.connect_retry import backoff_delays, retry_on_connect_error

logger = logging.getLogger(__name__)


#: What kubectl answers when a pod has no previous attempt to read. Measured on
#: production, 28 August 2026:
#:
#:     Error from server (BadRequest): previous terminated container "app" in pod
#:     "<naam>" not found
#:
#: That is not an error the user did anything about, since a pod that never crashed simply
#: has no earlier log, so it belongs in the panel as a sentence and not as raw stderr.
_PREVIOUS_ATTEMPT_MISSING_MARKER = "previous terminated container"

#: Pause between two probes of the background connection recovery.
_CONNECTION_PROBE_SECONDS = 30.0

#: What a label failure says when trying again will not help.
_PERMANENT_LABEL_MARKERS = ("not found", "unable to recognize", "no matches for kind")

#: Budget for the Capsule webhook of a namespace that was just created: its admin
#: RoleBinding takes about a second to propagate and denies the apply until it has.
_LABEL_RETRY_SECONDS = 3.0

#: Types left out of the ArgoCD-ownership inventory. Events are never applied by ArgoCD,
#: so they carry no ownership mark, but a busy namespace holds thousands of them.
#: Endpoints and EndpointSlices are in ArgoCD's own resource.exclusions, so ArgoCD cannot
#: have created them; they copy their Service's labels, instance label included, and the
#: Endpoints carry no ownerReference to tell them apart.
_TRACKING_SWEEP_SKIPPED_TYPES = {
    "events",
    "events.events.k8s.io",
    "endpoints",
    "endpointslices.discovery.k8s.io",
}


def types_named_in_kubectl_error(stderr_text: str, types: list[str]) -> list[str]:
    """Which of the queried types kubectl blamed in its error output.

    A ``kubectl get`` over many types exits non-zero when ONE of them fails, still prints
    the rest, and names that type exactly as it was asked for ("deployments.apps is
    forbidden").

    Matched on a name boundary, not as a substring: ``secrets`` sits inside
    ``externalsecrets.external-secrets.io``, and blaming the wrong type sends the reader of
    the log elsewhere. A deprecation warning in the same stderr cannot blame a type by
    accident, because those name a group and a Kind, never the ``plural.group`` string a
    type is queried under (measured on the sandbox).
    """
    return [t for t in types if re.search(rf"(?<![\w.-]){re.escape(t)}(?![\w.-])", stderr_text)]


def types_forbidden_in_kubectl_error(stderr_text: str, types: list[str]) -> list[str]:
    """Which of these types kubectl refused with Forbidden (per line, as kubectl reports)."""
    forbidden: set[str] = set()
    for line in stderr_text.splitlines():
        if "forbidden" in line.lower():
            forbidden.update(types_named_in_kubectl_error(line, types))
    return sorted(forbidden)


def is_previous_attempt_missing(stderr_text: str) -> bool:
    """Whether kubectl's stderr says this pod has no previous attempt to read."""
    return _PREVIOUS_ATTEMPT_MISSING_MARKER in stderr_text


def _summarize_kubectl_command(args: list[str]) -> str:
    """Secret-free summary of a kubectl invocation for logging: the operation and the project.

    Only the subcommand, the resource kind (the next token when it is a plain positional, not
    a flag) and the target namespace (from ``-n``/``--namespace``). Every flag value, resource
    name and stdin is left out, so no secret can reach the log whatever the command:
    `kubectl apply (project rig-prd-regel-k4c)`, `kubectl get pods (project rig-prd-amt-odc)`.
    """
    if not args:
        return "kubectl"

    parts = [f"kubectl {args[0]}"]

    # The resource kind is the first token after the verb, and only when it is a plain
    # positional: a resource name or a flag value must stay out.
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
    """The connection to the cluster is not available."""


class KubectlExecutionError(Exception):
    """A kubectl command exited non-zero."""


def _label_failure_is_transient(error: BaseException) -> bool:
    """Whether a failed label apply is worth another attempt."""
    if not isinstance(error, KubectlExecutionError | KubectlConnectionError):
        return False
    message = str(error).lower()
    return not any(marker in message for marker in _PERMANENT_LABEL_MARKERS)


class KubectlConnector:
    _instance = None
    is_connected = False
    _retry_task = None

    def __new__(cls) -> Self:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        """Inside a cluster kubectl picks up the service account mounted into the pod."""
        if self._initialized:
            return

        logger.debug("Initializing KubectlConnector")

        self.env = child_env()

        # Synchronously, so is_connected is set before any command runs.
        try:
            result = subprocess.run(
                ["kubectl", "auth", "whoami"],  # noqa: S607  # a tool from the image's PATH, not a fixed path
                capture_output=True,
                text=True,
                env=self.env,
                timeout=10,
            )
            if result.returncode == 0:
                logger.info("Kubectl connection successful")
                KubectlConnector.is_connected = True
            else:
                logger.warning(f"Kubectl connection failed: {result.stderr}")
                KubectlConnector.is_connected = False
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as e:
            logger.error(f"Error testing kubectl connection: {e}")
            KubectlConnector.is_connected = False

        # Without a running event loop (a synchronous test fixture, import time) there is no
        # retry task to schedule; the first async call works without it.
        if not KubectlConnector.is_connected:
            try:
                self._retry_task = asyncio.create_task(self._connection_retry())
            except RuntimeError:
                logger.debug("No running event loop; skipping kubectl connection-retry task")
                self._retry_task = None

        self._initialized = True
        logger.debug("KubectlConnector initialized successfully")

    async def _test_connection(self) -> bool:
        """Whether ``kubectl auth whoami`` answers, and record that on the class."""
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
                KubectlConnector.is_connected = True
                return True
            else:
                logger.warning(f"Kubectl connection failed: {stderr_str}")
                KubectlConnector.is_connected = False
                self._handle_connection_failure(stderr_str)
                return False

        except (OSError, ValueError, KeyError, TypeError) as e:
            logger.error(f"Error testing kubectl connection: {e}")
            KubectlConnector.is_connected = False
            return False

    def _handle_connection_failure(self, stderr_str: str) -> None:
        """On a refused connection, mark it down and start the retry task."""
        if "connection refused" in stderr_str.lower():
            KubectlConnector.is_connected = False
            # Start retry task if not already running (non-blocking)
            if KubectlConnector._retry_task is None or KubectlConnector._retry_task.done():
                KubectlConnector._retry_task = asyncio.create_task(self._connection_retry())

    async def _connection_retry(self) -> None:
        """Probe until the connection answers again.

        Endless on purpose: a connector that stops probing refuses every kubectl call for
        the rest of the pod's life. First and ceiling are equal, so the pause is fixed.
        """
        delays = backoff_delays(_CONNECTION_PROBE_SECONDS, _CONNECTION_PROBE_SECONDS)
        while not await self._test_connection():
            logger.debug("Retrying kubectl connection...")
            await asyncio.sleep(next(delays))
        logger.info("Kubectl connection restored")

    async def _run_kubectl_command(
        self,
        args: list[str],
        env: dict[str, str] | None = None,
        stdin_input: str | None = None,
        timeout: int = 60,
    ) -> tuple[str, str, int]:
        """One kubectl command; returns (stdout, stderr, exit code). A timeout yields exit 1.

        Raises:
            KubectlConnectionError: the connection is unavailable, or kubectl was refused.
        """
        if not KubectlConnector.is_connected:
            raise KubectlConnectionError("kubectl connection is not available")

        cmd_env = self.env.copy()
        if env:
            cmd_env.update(env)

        safe_cmd = _summarize_kubectl_command(args)

        from opi.core.metrics import track_subprocess_memory

        logger.debug(f"Running: {safe_cmd}")

        # Input goes through a pipe, never through a shell: a manifest can hold a secret
        # (BIO2 8.28) and a line reading exactly "EOF" closed the old heredoc.
        process = await asyncio.create_subprocess_exec(
            "kubectl",
            *args,
            stdin=asyncio.subprocess.PIPE if stdin_input is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=cmd_env,
        )

        try:
            async with track_subprocess_memory("kubectl"):
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(input=stdin_input.encode("utf-8") if stdin_input is not None else None),
                    timeout=timeout,
                )
        except TimeoutError:
            logger.error(f"Timed out after {timeout}s: {safe_cmd}")
            process.kill()
            await process.wait()
            return "", f"Command timed out after {timeout}s", 1

        stdout_str = stdout.decode("utf-8").strip()
        stderr_str = stderr.decode("utf-8").strip()

        if process.returncode != 0:
            logger.warning(f"Failed (code {process.returncode}): {safe_cmd} :: {stderr_str}")

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
        """The public form of ``_run_kubectl_command``, for a caller that needs raw kubectl."""
        return await self._run_kubectl_command(args, env, stdin_input)

    def template_manifest(self, manifest_content: str, variables: dict[str, Any]) -> str:
        """Render the manifest as a Jinja2 template, ending in a newline."""
        logger.debug(f"Templating manifest with variables: {variables.keys()}")

        result = render_template_source(manifest_content, variables)
        if not result.endswith("\n"):
            result += "\n"
        return result

    async def apply_manifest(
        self, file_path: str, variables: dict[str, Any] | None = None, namespace: str | None = None
    ) -> None:
        """Apply a manifest file, rendering ``variables`` into it first.

        Without ``namespace`` the manifest's own namespace applies.

        Raises:
            KubectlExecutionError: the apply failed. The message front-loads the manifest and
                namespace and collapses the server error onto one line. The connector does
                NOT log the failure: the caller decides whether a failure is really an error,
                so a caller that retries on a transient Capsule RBAC race logs once, after it
                has given up.
        """
        logger.debug(f"Applying manifest: {file_path}{' in namespace ' + namespace if namespace else ''}")

        with open(file_path) as f:
            manifest_content = f.read()

        if variables:
            manifest_content = self.template_manifest(manifest_content, variables)

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
        """A Secret's data, base64-decoded, or None when it is absent or unreadable."""
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

        True when it exists, False on a definitive NotFound, and None when the check itself
        failed: the caller must not conclude the Secret is gone.
        """
        return await self.resource_exists("secret", secret_name, namespace)

    async def resource_exists(self, resource_type: str, resource_name: str, namespace: str) -> bool | None:
        """Ground-truth existence check for a namespaced resource.

        ``resource_type`` is the type as kubectl names it ('secret', 'role'). True when it
        exists, False on a definitive NotFound, and None when the check itself failed.
        """
        stdout, stderr, code = await self._run_kubectl_command(
            ["get", resource_type, resource_name, "-n", namespace, "-o", "name"]
        )
        if code == 0:
            return True
        if "notfound" in stderr.lower().replace(" ", ""):
            return False
        logger.warning(f"Could not determine existence of {resource_type} '{resource_name}' in '{namespace}': {stderr}")
        return None

    async def get_sops_secret_from_namespace(self, namespace: str, secret_name: str) -> str | None:
        """The SOPS AGE private key out of ``secret_name``, or None when it is not readable.

        The caller owns the Secret's name.
        """
        logger.debug(f"Retrieving SOPS secret from namespace: {namespace}")

        args = ["get", "secret", secret_name, "-n", namespace, "-o", "jsonpath={.data.key}"]

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

        encoded_key = stdout.strip()
        if not encoded_key:
            logger.error("Empty key data received")
            return None

        decoded_key = base64.b64decode(encoded_key).decode("utf-8")
        logger.debug("Successfully retrieved SOPS secret")
        return decoded_key

    async def namespace_exists(self, namespace: str) -> bool:
        """Whether the namespace exists. A failed read counts as absent; see
        :meth:`namespace_absent` for a caller that may not conclude that."""
        logger.debug(f"Checking if namespace exists: {namespace}")

        args = ["get", "namespace", namespace]

        stdout, stderr, code = await self._run_kubectl_command(args)

        if code == 0:
            logger.debug(f"Namespace {namespace} exists")
            return True
        else:
            logger.debug(f"Namespace {namespace} does not exist")
            return False

    async def namespace_absent(self, namespace: str) -> bool | None:
        """Is the namespace gone (True), present (False), or unknown (None)?"""
        stdout, stderr, code = await self._run_kubectl_command(["get", "namespace", namespace, "-o", "name"])
        if code == 0:
            return False
        if "notfound" in stderr.lower().replace(" ", ""):
            return True
        logger.warning(f"Could not determine existence of namespace '{namespace}': {stderr}")
        return None

    async def wait_for_namespace_deletion(self, namespace: str, max_retries: int = 40, retry_delay: int = 5) -> bool:
        """Wait until the namespace is gone (a NotFound from the API server)."""
        for attempt in range(1, max_retries + 1):
            absent = await self.namespace_absent(namespace)
            if absent:
                logger.info(f"Namespace '{namespace}' is gone (after {attempt} check(s))")
                return True
            if attempt < max_retries:
                await asyncio.sleep(retry_delay)
        logger.warning(f"Namespace '{namespace}' was not confirmed gone after {max_retries} checks")
        return False

    async def wait_for_resource_absence(
        self, resource_type: str, resource_name: str, namespace: str, max_retries: int = 20, retry_delay: int = 2
    ) -> bool:
        """Wait until a namespaced resource is gone (a NotFound from the API server).

        For a resource a controller cleans up asynchronously, such as an AppProject ArgoCD
        prunes once its manifest left git. An unmeasurable read (None from
        ``resource_exists``) counts as still present: only a read NotFound counts as gone.
        After a timeout it returns False and the caller decides as it would without this wait.
        """
        for attempt in range(1, max_retries + 1):
            exists = await self.resource_exists(resource_type, resource_name, namespace)
            if exists is False:
                logger.info(f"{resource_type} '{resource_name}' in '{namespace}' is gone (after {attempt} check(s))")
                return True
            if attempt < max_retries:
                await asyncio.sleep(retry_delay)
        logger.warning(
            f"{resource_type} '{resource_name}' in '{namespace}' was not confirmed gone after {max_retries} checks"
        )
        return False

    async def get_namespace_label_map(self, label_key: str) -> dict[str, str]:
        """Every namespace in the cluster, mapped to its value for ``label_key``.

        Namespaces without the label are present with an empty string, so a caller
        can tell "missing label" from "missing namespace" (absent from the dict).
        """
        all_labels = await self.get_namespace_labels_map()
        return {namespace: labels.get(label_key, "") for namespace, labels in all_labels.items()}

    async def get_namespace_labels_map(self) -> dict[str, dict[str, str]]:
        """Every namespace in the cluster, mapped to all of its labels.

        One call instead of one per namespace: with this map both questions, does it exist
        and does it carry the right labels, are answered locally.
        """
        args = ["get", "namespaces", "-o", "json"]
        stdout, stderr, code = await self._run_kubectl_command(args)

        if code != 0:
            # Raise rather than guess: an empty map would read as "no namespace exists" and
            # set off creation attempts.
            logger.warning(f"Could not list namespaces: {stderr}")
            raise KubectlExecutionError(f"Failed to list namespaces: {stderr}")

        data = json.loads(stdout)
        result: dict[str, dict[str, str]] = {}
        for item in data.get("items", []):
            meta = item.get("metadata", {})
            name = meta.get("name")
            if name:
                result[name] = meta.get("labels", {}) or {}
        logger.debug(f"Listed {len(result)} namespaces with their labels")
        return result

    async def delete_namespace(self, namespace: str) -> bool:
        """Delete a namespace; True when it is gone or was never there.

        Raises:
            KubectlExecutionError: the namespace belongs to the platform itself.
        """
        logger.debug(f"Deleting namespace: {namespace}")

        # A second layer next to the refusal of a reserved project name: deleting a project
        # may never take the platform's own namespace with it.
        if is_platform_namespace(namespace):
            raise KubectlExecutionError(
                f"Weigert namespace '{namespace}' te verwijderen: dat is een namespace van het platform zelf."
            )

        args = ["delete", "namespace", namespace, "--ignore-not-found=true"]

        stdout, stderr, code = await self._run_kubectl_command(args)

        if code == 0:
            logger.debug(f"Successfully deleted namespace: {namespace}")
            return True
        else:
            logger.error(f"Failed to delete namespace {namespace}: {stderr}")
            return False

    async def remove_argocd_application_finalizers(self, app_name: str, namespace: str | None = None) -> bool:
        """Strip the finalizers off an Application CR; True when it is gone or was never there.

        The way out when an Application is stuck because the resources it manages (its target
        namespace, say) were deleted out of band: without finalizers it can be collected.
        ``namespace`` defaults to this instance's cluster.
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
        """Delete an Application CR directly; True when it is gone or was never there.

        The fallback when deletion over GitOps does not work, such as a parent app that is
        not syncing. ``namespace`` defaults to this instance's cluster.
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
        """Ground-truth existence check for an Application CR, through the Kubernetes API.

        The honest fallback for the ArgoCD API, which answers an ambiguous 'permission
        denied' while its cache is stalled (see ``ArgoConnector.wait_for_application_deletion``).

        True when it exists, False on a definitive NotFound, and None when the check itself
        failed: the caller must not conclude the application is gone. ``namespace`` defaults
        to this instance's cluster.
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

    async def get_argocd_application(self, app_name: str, namespace: str | None = None) -> dict[str, Any] | None:
        """One ArgoCD Application CR, read through the Kubernetes API.

        The Kubernetes API and not the ArgoCD API, for the same reason as
        ``argocd_application_exists`` above, and because the terminate patch next to this one
        goes through that same API.

        ``None`` both when it is absent and when it could not be read: those two are one
        answer on purpose, and the log line below says which it was. ``namespace`` defaults to
        this instance's cluster.
        """
        namespace = namespace or get_argo_namespace(settings.CLUSTER_MANAGER)
        stdout, stderr, code = await self._run_kubectl_command(
            ["get", "application", app_name, "-n", namespace, "-o", "json"]
        )
        if code != 0:
            if "notfound" in stderr.lower().replace(" ", ""):
                logger.debug(f"ArgoCD Application '{app_name}' does not exist in namespace '{namespace}'")
            else:
                logger.warning(f"Could not read ArgoCD Application '{app_name}': {stderr}")
            return None
        try:
            application = json.loads(stdout)
        except json.JSONDecodeError as e:
            logger.error(f"Could not parse ArgoCD Application '{app_name}': {e}")
            return None
        return application if isinstance(application, dict) else None

    async def argocd_application_deletion_progress(
        self, app_name: str, namespace: str | None = None, resource_types: list[str] | None = None
    ) -> tuple[bool, int, bool] | None:
        """How far along the cascade of a deleting ArgoCD Application is.

        Slow and stuck look the same if you only ask whether the Application is gone yet.
        What tells them apart is whether resources are still DISAPPEARING, and that is
        counted in the destination namespace through kubectl.

        Not read from the Application's own ``status.resources``: the controller stops
        recomputing that once the object has a deletionTimestamp, and a frozen number reads
        as a stall.

        The count is every ArgoCD-owned resource in that namespace, not only this
        Application's. Filtering on the name would answer 0 for a long-named Application on a
        label cluster, where the mark is the name CUT to 63 characters, and a permanent 0 is a
        permanent stall. A namespace that keeps emptying is a cascade at work, and one that
        stops emptying while our Application stays is the stall we are looking for.

        ``resource_types`` are the kubectl type names to inventory, discovered when not given;
        a caller that polls passes the types it discovered once.

        Returns (deletion underway, ArgoCD-owned resources left in the destination namespace,
        inventory complete), or ``None`` when the Application or its namespace could not be
        read. None is not "no progress", but it is no proof of progress either.
        """
        application = await self.get_argocd_application(app_name, namespace)
        if application is None:
            return None

        metadata = application.get("metadata") or {}
        underway = bool(metadata.get("deletionTimestamp"))
        destination = ((application.get("spec") or {}).get("destination") or {}).get("namespace")
        if not destination:
            logger.debug(
                f"ArgoCD Application '{app_name}' names no destination namespace, "
                f"so there is no namespace to measure the cascade in"
            )
            return None

        inventory = await self.list_tracked_resources(destination, resource_types)
        if inventory is None:
            return None
        tracked, complete = inventory
        return underway, len(tracked), complete

    async def terminate_argocd_application_operation(self, app_name: str, namespace: str | None = None) -> bool:
        """Remove any running operation from an Application CR.

        The operation is read first: a json-patch remove on an absent path is rejected with a
        generic 422 by this apiserver, so reading first avoids false errors when no operation
        is running. See features/argocd-vastgelopen-verwijdering.md.

        True when no running operation remains (removed, none present, or the app is gone),
        False when the Application could not be read or the patch failed. ``namespace``
        defaults to this instance's cluster.
        """
        namespace = namespace or get_argo_namespace(settings.CLUSTER_MANAGER)

        operation, read = await self._read_argocd_application_operation(app_name, namespace)
        if not read:
            return False
        if not operation:
            logger.debug(f"ArgoCD Application '{app_name}' has no running operation")
            return True

        logger.info(f"Removing the running operation from ArgoCD Application '{app_name}' in namespace '{namespace}'")
        stdout, stderr, code = await self._run_kubectl_command(
            [
                "patch",
                "application",
                app_name,
                "-n",
                namespace,
                "--type",
                "json",
                "-p",
                '[{"op":"remove","path":"/operation"}]',
            ]
        )

        if code == 0:
            logger.info(f"Removed the running operation from ArgoCD Application '{app_name}'")
            return True

        # The operation can end, and the Application can go, between the read and the patch.
        # Reading again says whether it did, where the generic 422 of this apiserver does not.
        operation, read = await self._read_argocd_application_operation(app_name, namespace)
        if read and not operation:
            logger.info(f"The running operation on ArgoCD Application '{app_name}' was already gone")
            return True

        logger.error(f"Failed to remove the running operation from ArgoCD Application '{app_name}': {stderr}")
        return False

    async def _read_argocd_application_operation(self, app_name: str, namespace: str) -> tuple[str, bool]:
        """The running operation of an Application, and whether it could be read at all.

        An Application that does not exist has no operation, and that is a read that
        succeeded: there is nothing to clear.
        """
        stdout, stderr, code = await self._run_kubectl_command(
            ["get", "application", app_name, "-n", namespace, "-o", "jsonpath={.operation}"]
        )
        if code == 0:
            return stdout.strip(), True
        if "notfound" in stderr.lower().replace(" ", ""):
            logger.info(f"ArgoCD Application '{app_name}' does not exist")
            return "", True
        logger.error(f"Could not read the running operation of ArgoCD Application '{app_name}': {stderr}")
        return "", False

    async def get_argocd_application_destination_namespace(
        self, app_name: str, namespace: str | None = None
    ) -> str | None:
        """
        The namespace an ArgoCD Application deploys into (``spec.destination.namespace``).

        Read it while the Application still exists: once it is gone, nothing links its
        leftover resources back to a namespace to look in.

        Args:
            app_name: The name of the ArgoCD Application
            namespace: Namespace holding the Application CR; defaults to this instance's cluster

        Returns:
            The destination namespace, or None if it could not be read.
        """
        namespace = namespace or get_argo_namespace(settings.CLUSTER_MANAGER)
        stdout, stderr, code = await self._run_kubectl_command(
            ["get", "application", app_name, "-n", namespace, "-o", "jsonpath={.spec.destination.namespace}"]
        )
        if code != 0 or not stdout.strip():
            logger.warning(f"Could not read the destination namespace of ArgoCD Application '{app_name}': {stderr}")
            return None
        return stdout.strip()

    async def list_argocd_applications(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """Every Application CR, empty when the query failed.

        ``namespace`` defaults to this instance's cluster.
        """
        namespace = namespace or get_argo_namespace(settings.CLUSTER_MANAGER)
        stdout, stderr, code = await self._run_kubectl_command(["get", "applications", "-n", namespace, "-o", "json"])
        if code != 0:
            logger.error(f"Failed to list ArgoCD Applications in namespace '{namespace}': {stderr}")
            return []

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as e:
            logger.error(f"Could not parse the ArgoCD Application list: {e}")
            return []
        return data.get("items", []) if isinstance(data, dict) else []

    async def list_namespaced_resource_types(self) -> list[str] | None:
        """The namespaced resource types this cluster can list and delete.

        Discovered rather than hard-coded: a fixed list is a guaranteed blind spot for a
        sweep whose whole job is finding what was left behind.

        kubectl type names (``secrets``, ``deployments.apps``, ...) minus
        ``_TRACKING_SWEEP_SKIPPED_TYPES``, or ``None`` when the discovery itself failed. A
        failed discovery must not read as 'this cluster has no types': every namespace would
        then inventory as empty and a sweep would report SCHOON without having looked.
        """
        stdout, stderr, code = await self._run_kubectl_command(
            ["api-resources", "--namespaced=true", "--verbs=list,delete", "-o", "name"]
        )
        if code != 0:
            logger.error(f"Failed to list namespaced api-resources: {stderr}")
            return None

        types = []
        for line in stdout.splitlines():
            name = line.strip()
            if not name or name in _TRACKING_SWEEP_SKIPPED_TYPES:
                continue
            types.append(name)
        return types

    async def list_namespaced_kinds(self) -> dict[str, str] | None:
        """The namespaced kinds this cluster serves, keyed ``Kind.group`` (core: ``Kind``).

        A manifest names its type by ``apiVersion`` and ``kind``, kubectl by plural and group.
        This maps the one onto the other, so a set of kinds read from manifests can be queried
        as the same type names ``list_namespaced_resource_types`` returns:
        ``{"Deployment.apps": "deployments.apps", "Secret": "secrets", ...}``, or ``None``
        when the discovery failed.
        """
        stdout, stderr, code = await self._run_kubectl_command(
            ["api-resources", "--namespaced=true", "--verbs=list", "-o", "wide", "--no-headers"]
        )
        if code != 0:
            logger.error(f"Failed to list namespaced api-resources: {stderr}")
            return None

        kinds = {}
        for line in stdout.splitlines():
            columns = line.split()
            # SHORTNAMES may be empty, so columns are found from NAMESPACED, which a
            # namespaced listing always prints as "true": APIVERSION before it, KIND after.
            if "true" not in columns:
                continue
            at = columns.index("true")
            plural, api_version, kind = columns[0], columns[at - 1], columns[at + 1]
            group = api_version.rpartition("/")[0]
            kinds[f"{kind}.{group}" if group else kind] = f"{plural}.{group}" if group else plural
        return kinds

    async def argocd_application_resource_types(self, app_name: str, namespace: str | None = None) -> list[str] | None:
        """Kubectl type names of the kinds an Application owns, or None to fall back to full discovery."""
        application = await self.get_argocd_application(app_name, namespace)
        if application is None:
            return None

        resources = (application.get("status") or {}).get("resources") or []
        kinds = set()
        for resource in resources:
            kind = resource.get("kind")
            if not kind:
                continue
            group = resource.get("group") or ""
            kinds.add(f"{kind}.{group}" if group else kind)
        if not kinds:
            logger.info(f"ArgoCD Application '{app_name}' names no resources; querying every namespaced type")
            return None

        served = await self.list_namespaced_kinds()
        if served is None:
            return None
        unknown = sorted(kinds - served.keys())
        if unknown:
            logger.info(
                f"ArgoCD Application '{app_name}' owns {', '.join(unknown)}, which this cluster does not serve as a "
                f"namespaced kind; querying every namespaced type"
            )
            return None
        return sorted({served[kind] for kind in kinds})

    async def list_tracked_resources(
        self, namespace: str, resource_types: list[str] | None = None, own_types: list[str] | None = None
    ) -> tuple[list[TrackedResource], bool] | None:
        """Every resource in a namespace marked as ArgoCD's own, with whether that is complete.

        ``resource_types`` are the kubectl types to query, all namespaced types when not
        given. ``own_types`` are the types where silence means an incomplete inventory; a
        Forbidden on a type outside that set is fine.

        Returns (resources, inventory complete), or None when the read failed.
        """
        types = resource_types if resource_types is not None else await self.list_namespaced_resource_types()
        if not types:
            logger.error(f"Cannot inventory namespace '{namespace}': no resource types to query")
            return None

        stdout, stderr, code = await self._run_kubectl_command(
            ["get", ",".join(types), "-n", namespace, "--ignore-not-found=true", "-o", "json"],
            timeout=120,
        )
        if not stdout.strip():
            if code != 0:
                logger.error(f"Failed to inventory tracked resources in namespace '{namespace}': {stderr}")
                return None
            return [], True
        complete = True
        if code != 0:
            # kubectl exits non-zero when ONE of the queried types fails (an unavailable
            # aggregated API, or a type this service account may not list here) but still
            # prints what it did get. That part is kept, as long as the caller hears that it
            # IS a part: the types that stayed silent are where a leftover resource hides.
            failed_types = types_named_in_kubectl_error(stderr, types)
            judged = set(own_types) if own_types is not None else set(types)
            tolerated = [
                t for t in failed_types if t not in judged and t in types_forbidden_in_kubectl_error(stderr, types)
            ]
            unmeasured = [t for t in failed_types if t not in tolerated]
            complete = bool(failed_types) and not unmeasured
            if unmeasured:
                logger.warning(
                    f"Partial inventory of namespace '{namespace}': {len(unmeasured)} of {len(types)} "
                    f"types could not be listed ({', '.join(unmeasured)}). kubectl reported: {stderr}"
                )
            elif not failed_types:
                logger.warning(
                    f"Partial inventory of namespace '{namespace}', and kubectl named none of the "
                    f"{len(types)} queried types as the one that failed: {stderr}"
                )
            else:
                logger.info(
                    f"Namespace '{namespace}' refused {len(tolerated)} type(s) ZAD does not render "
                    f"({', '.join(tolerated)}); the inventory of our own kinds is complete"
                )

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as e:
            logger.error(f"Could not parse the resource inventory of namespace '{namespace}': {e}")
            return None

        items = data.get("items", []) if isinstance(data, dict) else []
        tracked = [resource for item in items if (resource := tracked_resource_from_item(item)) is not None]
        logger.debug(f"Namespace '{namespace}': {len(tracked)} of {len(items)} resources are ArgoCD-owned")
        return tracked, complete

    async def delete_tracked_resources(self, resources: list[TrackedResource]) -> list[TrackedResource]:
        """Delete tracked resources one by one; returns the ones that could NOT be deleted."""
        failed = []
        for resource in resources:
            deleted = await self.delete_resource(resource.kubectl_type, resource.name, resource.namespace)
            if not deleted:
                failed.append(resource)
        if failed:
            logger.error(f"Failed to delete {len(failed)} of {len(resources)} tracked resources")
        return failed

    async def wait_for_capsule_tenant_label(self, namespace: str, timeout: int = 30) -> bool:
        """Wait for Capsule to put its tenant label on a namespace; False on a timeout.

        Capsule's admission webhook assigns an ownerReference and a tenant label to a
        namespace created by a tenant user. Only once that label is there is it safe to put
        labels and annotations on the namespace.
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

    async def apply_labels_to_resource(
        self,
        resource_type: str,
        resource_name: str,
        labels: dict[str, str],
        namespace: str | None = None,
    ) -> bool:
        """
        Apply one or more labels to a specific Kubernetes resource in a single call.

        Retries the transient denial of an admission webhook (Capsule) that is still
        reconciling a fresh namespace. A failure that trying again will not fix (the
        resource does not exist, kubectl cannot resolve the kind) is reported at once.

        A cluster-scoped resource needs no ``namespace``.
        """
        if not labels:
            return True

        description = ",".join(f"{key}={value}" for key, value in labels.items())
        logger.debug(f"Applying labels {description} to {resource_type}/{resource_name}")

        args = ["label", resource_type, resource_name]
        args.extend(f"{key}={value}" for key, value in labels.items())
        args.append("--overwrite")

        if namespace and resource_type.lower() != "namespace":
            args.extend(["-n", namespace])

        async def _label() -> None:
            _stdout, stderr, code = await self._run_kubectl_command(args)
            if code != 0:
                raise KubectlExecutionError(stderr)

        try:
            await retry_on_connect_error(
                _label,
                _LABEL_RETRY_SECONDS,
                f"labels on {resource_type}/{resource_name}",
                retry_on=_label_failure_is_transient,
            )
        except (KubectlExecutionError, KubectlConnectionError) as e:
            logger.error(f"Failed to apply labels to {resource_type}/{resource_name}: {e}")
            return False

        logger.info(f"Successfully applied labels {description} to {resource_type}/{resource_name}")
        return True

    async def apply_annotation_to_resource(
        self,
        resource_type: str,
        resource_name: str,
        annotation_key: str,
        annotation_value: str,
        namespace: str | None = None,
    ) -> bool:
        """Put one annotation on a resource; False on any failure.

        A cluster-scoped resource needs no ``namespace``.
        """
        logger.debug(f"Applying annotation {annotation_key}={annotation_value} to {resource_type}/{resource_name}")

        try:
            args = ["annotate", resource_type, resource_name, f"{annotation_key}={annotation_value}"]

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

        except (OSError, ValueError, KeyError, TypeError, KubectlConnectionError, KubectlExecutionError) as e:
            logger.error(f"Error applying annotation to {resource_type}/{resource_name}: {e}")
            return False

    async def get_deployment_logs(self, deployment_name: str, namespace: str, lines: int = 100) -> list[str]:
        """The last ``lines`` log lines of every pod of a deployment, empty on any failure.

        Through a label selector and not ``deployment/``, so this needs only the pods and
        pods/log permissions and not get on the deployment.
        """
        logger.debug(f"Getting logs for deployment {deployment_name} in namespace {namespace}")

        try:
            args = ["logs", "-l", f"app={deployment_name}", "-n", namespace, f"--tail={lines}"]
            stdout, stderr, code = await self._run_kubectl_command(args)

            if code != 0:
                logger.warning(f"Failed to get deployment logs: {stderr}")

            log_lines = [line for line in stdout.split("\n") if line.strip()]

            # No lines at all means there is no running container (a CrashLoopBackOff), and
            # then the previous attempt is what the reader wants.
            if not log_lines:
                prev_args = ["logs", "-l", f"app={deployment_name}", "-n", namespace, f"--tail={lines}", "--previous"]
                prev_stdout, _, prev_code = await self._run_kubectl_command(prev_args)
                if prev_code == 0 and prev_stdout.strip():
                    log_lines = ["[previous container logs]"] + [
                        line for line in prev_stdout.split("\n") if line.strip()
                    ]

            return log_lines

        except (OSError, ValueError, KeyError, TypeError, KubectlConnectionError, KubectlExecutionError) as e:
            logger.error(f"Error getting deployment logs: {e}")
            return []

    async def stream_deployment_logs(
        self,
        deployment_name: str,
        namespace: str,
        lines: int = 100,
        pod_name: str | None = None,
        previous: bool = False,
    ) -> asyncio.subprocess.Process | None:
        """The subprocess of a ``kubectl logs -f``, or None when it could not be started.

        Without ``pod_name`` this uses a label selector and not ``deployment/``, so it needs
        only the pods and pods/log permissions. That is also what makes it show every matching
        pod at once: during a rolling update the lines of the serving pod and of the crashing
        one arrive interleaved and, without ``--prefix``, indistinguishable. Naming a pod is
        the way out of that.

        The caller owns the process: read ``stdout``, terminate it when done, and restart it
        when it exits, which it does for a pod in CrashLoopBackOff and after the matched pod
        terminates.

        Args:
            lines: How many historical lines to fetch first. Pass 0 on a reattach, so the
                follower only streams NEW output instead of re-dumping the same stored tail
                every backoff cycle.
            pod_name: One specific pod to follow. The caller MUST have checked that this pod
                belongs to the project asking for it: this method does not, and a namespace
                carries the deployments of a whole team.
            previous: Read the PREVIOUS attempt of the container instead of the current one.
                Only meaningful together with ``pod_name``. There is nothing to follow about a
                container that already stopped: the API hands over the stored log and the
                process ends at once, so a caller must not treat that exit as a stream that
                needs reattaching.
        """
        if not KubectlConnector.is_connected:
            logger.error("kubectl connection is not available for log streaming")
            return None

        target = pod_name or f"deployment {deployment_name}"
        logger.debug(f"Starting log stream for {target} in namespace {namespace} (tail={lines}, previous={previous})")

        try:
            if pod_name:
                cmd = [
                    "kubectl",
                    "logs",
                    "-f",
                    pod_name,
                    "-c",
                    APPLICATION_CONTAINER_NAME,
                    "-n",
                    namespace,
                    f"--tail={lines}",
                ]
                if previous:
                    cmd.append("--previous")
            else:
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

            logger.info(f"Started log stream for {target} in {namespace} (PID: {process.pid}, tail={lines})")
            return process

        except (OSError, ValueError, KeyError, TypeError) as e:
            logger.error(f"Error starting log stream for {target}: {e}")
            return None

    # Event object prefixes and reasons that are infrastructure noise: a project user can do
    # nothing about them.
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
        """The recent events of a namespace, as {type, reason, object, kind, message, time}.

        ``event_type`` None keeps every type; ``max_age_hours`` drops anything older.
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

        except (OSError, ValueError, KeyError, TypeError, KubectlConnectionError, KubectlExecutionError) as e:
            logger.error(f"Error getting namespace events: {e}")
            return []

    async def get_application_pods(self, namespace: str, deployment_name: str) -> list[dict[str, Any]]:
        """The application pods of one deployment, with what each of them is actually doing.

        This answers the question the ArgoCD status cannot: *which pod is serving traffic
        right now*. During a rolling update two pods sit behind one Service, the previous
        version still serving and the new one failing to come up, and ArgoCD reports the
        deployment Degraded for both. Only the pod list tells them apart.

        One ``kubectl get pods -l <selector> -o json``; the selector comes from
        ``deployment_pod_selector`` so the service-owned pods (sleep-mode's waker and
        friends) are excluded exactly as everywhere else.

        One dict per pod with ``name``, ``app`` (the component's unique name),
        ``pod_template_hash``, ``deleting``, and from the ``app`` container's status
        ``ready``, ``image``, ``restart_count``, ``started_at`` and ``has_previous_attempt``.
        Best-effort like the rest of this connector: a failing command or unparsable output
        logs and yields an empty list, never raises.
        """
        if not KubectlConnector.is_connected:
            logger.debug("kubectl not available; no application pods for %s", deployment_name)
            return []

        args = [
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            deployment_pod_selector(deployment_name),
            "-o",
            "json",
        ]
        try:
            stdout, stderr, code = await self._run_kubectl_command(args, timeout=15)
        except (OSError, ValueError, KeyError, TypeError, KubectlConnectionError, KubectlExecutionError) as exc:
            logger.warning(f"Could not list application pods for {namespace}/{deployment_name}: {exc}")
            return []

        if code != 0:
            logger.warning(f"Could not list application pods for {namespace}/{deployment_name}: {stderr}")
            return []

        try:
            items = json.loads(stdout).get("items", []) or []
        except (json.JSONDecodeError, AttributeError) as exc:
            logger.warning(f"Unparsable pod list for {namespace}/{deployment_name}: {exc}")
            return []

        pods: list[dict[str, Any]] = []
        for item in items:
            metadata = item.get("metadata", {}) or {}
            labels = metadata.get("labels", {}) or {}
            pod: dict[str, Any] = {
                "name": metadata.get("name", ""),
                "app": labels.get("app", ""),
                "pod_template_hash": labels.get("pod-template-hash", ""),
                "deleting": bool(metadata.get("deletionTimestamp")),
                "ready": False,
                "image": "",
                "restart_count": 0,
                "started_at": None,
                "has_previous_attempt": False,
            }
            for container in (item.get("status", {}) or {}).get("containerStatuses", []) or []:
                if container.get("name") != APPLICATION_CONTAINER_NAME:
                    continue
                pod["ready"] = bool(container.get("ready"))
                pod["image"] = container.get("image", "") or ""
                pod["restart_count"] = int(container.get("restartCount") or 0)
                # The container's own start, not ``status.startTime`` of the pod: after a
                # restart the first is the truth and the second is when the pod was created.
                pod["started_at"] = ((container.get("state", {}) or {}).get("running", {}) or {}).get("startedAt")
                pod["has_previous_attempt"] = bool((container.get("lastState", {}) or {}).get("terminated"))
                break
            pods.append(pod)

        logger.debug("Found %d application pod(s) for %s/%s", len(pods), namespace, deployment_name)
        return pods

    async def watch_pods(self, label_selector: str) -> asyncio.subprocess.Process | None:
        """Start a cluster-wide pod watch and return the streaming subprocess.

        ``kubectl get pods -A --watch --output-watch-events`` first prints the current
        state (each object as an ``ADDED`` event) and then streams every change. That
        initial burst seeds the caller's cache and comes back after every reconnect, so
        nothing here has to track a ``resourceVersion``.

        Subprocess rather than a direct call to the API server, like every other
        Kubernetes read here: kubectl resolves both the in-cluster service account and a
        local kubeconfig, and ``stream_deployment_logs`` established the shape.

        The caller owns the process: read ``stdout``, terminate it when done, and
        restart it when it exits (which it does on any API-server hiccup).

        ``label_selector`` limits the stream, e.g. ``component=application``. None when the
        process could not be started.
        """
        if not KubectlConnector.is_connected:
            logger.error("kubectl connection is not available for the pod watch")
            return None

        cmd = [
            "kubectl",
            "get",
            "pods",
            "--all-namespaces",
            "-l",
            label_selector,
            "--watch",
            "--output-watch-events",
            "-o",
            "json",
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
            )
        except (OSError, ValueError, KeyError, TypeError) as e:
            logger.error(f"Error starting the pod watch ({label_selector}): {e}")
            return None

        logger.info(f"Started pod watch for '{label_selector}' (PID: {process.pid})")
        return process

    async def get_pod_container_image(self, namespace: str, pod_name: str, container_name: str) -> str | None:
        """The image a running container was started from, or None when it cannot be read.

        Read from ``.spec.containers[]`` rather than from a deployment or an overlay: the pod
        is what is actually running, and during a rolling update the two pods behind one
        Service differ exactly here.
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
        """The deployments of a namespace, as {name, ready, replicas, available, updated}.

        ``deployment_name`` narrows it to one deployment.
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

        except (OSError, ValueError, KeyError, TypeError, KubectlConnectionError, KubectlExecutionError) as e:
            logger.error(f"Error getting deployment status: {e}")
            return []

    async def get_deployment_conditions(self, namespace: str, deployment_name: str) -> list[dict[str, str]] | None:
        """A deployment's conditions as {type, status, reason, message}, or None when absent."""
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

        except (OSError, ValueError, KeyError, TypeError, KubectlConnectionError, KubectlExecutionError) as e:
            logger.warning(f"Error getting deployment conditions for {deployment_name}: {e}")
            return None

    async def delete_resource(self, resource_type: str, resource_name: str, namespace: str | None = None) -> bool:
        """Delete one resource; False on any failure. A cluster-scoped one needs no namespace."""
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

        except (OSError, ValueError, KeyError, TypeError, KubectlConnectionError, KubectlExecutionError) as e:
            logger.error(f"Error deleting {resource_type} {resource_name}: {e}")
            return False

    async def get_vpa_recommendation(
        self, namespace: str, workload_name: str, container_name: str = "app"
    ) -> VpaContainerRecommendation | None:
        """A VerticalPodAutoscaler's recommendation for one container, normalized.

        ``workload_name`` is the VPA object's name, which matches the Deployment's. None when
        the VPA is missing, its status is not populated yet (freshly created), or the values
        cannot be parsed.
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

        except (OSError, ValueError, KeyError, TypeError, KubectlConnectionError, KubectlExecutionError) as e:
            logger.error(f"Error getting VPA recommendation for '{workload_name}': {e}")
            return None

    async def get_namespaced_resource(
        self, resource_type: str, resource_name: str, namespace: str
    ) -> dict[str, Any] | None:
        """One named namespaced resource, as the API returns it.

        Works for a custom resource too, so the caller passes the fully qualified type
        (``clusters.postgresql.cnpg.io``) when the short name could be ambiguous. ``None``
        both when it is absent and when it could not be read; the log line below says which.
        """
        stdout, stderr, code = await self._run_kubectl_command(
            ["get", resource_type, resource_name, "-n", namespace, "-o", "json"]
        )
        if code != 0:
            if "notfound" in stderr.lower().replace(" ", ""):
                logger.debug(f"{resource_type} '{resource_name}' does not exist in namespace '{namespace}'")
            else:
                logger.warning(f"Could not read {resource_type} '{resource_name}' in '{namespace}': {stderr}")
            return None
        try:
            resource = json.loads(stdout)
        except json.JSONDecodeError as e:
            logger.error(f"Could not parse {resource_type} '{resource_name}': {e}")
            return None
        return resource if isinstance(resource, dict) else None

    async def get_resources_by_label(
        self, resource_type: str, namespace: str, label_selector: str
    ) -> list[dict[str, Any]]:
        """The resources of one type in a namespace matching ``label_selector``."""
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

        except (OSError, ValueError, KeyError, TypeError, KubectlConnectionError, KubectlExecutionError) as e:
            logger.error(f"Error getting {resource_type} resources: {e}")
            return []


def create_kubectl_connector() -> KubectlConnector:
    """The KubectlConnector singleton.

    Deliberately not logged: this returns the one existing instance and creates nothing, and
    every periodic job calls it. ``__init__`` logs the real creation once.
    """
    return KubectlConnector()
