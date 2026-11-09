"""Namespaces and the project's own database infrastructure."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import socket
from typing import TYPE_CHECKING, Any, Protocol, cast

import asyncpg

from opi.connectors import create_argo_connector
from opi.connectors.kubectl import KubectlExecutionError
from opi.core.cluster_config import (
    get_argo_namespace,
    get_backup_namespace,
    get_cluster_cidrs,
    get_database_cluster_service_endpoint,
    get_infrastructure_namespace,
    get_ingress_controller_selector,
    get_namespace,
    get_ops_egress,
    get_pod_security_labels,
    get_prefixed_namespace,
    get_storage_class_name,
    namespace_labels,
    uses_capsule,
)
from opi.core.config import settings
from opi.generation.manifests import render_template
from opi.manager.argo_manager import APPLICATIE_AANMAAK_TIMEOUT_SECONDEN
from opi.services.catalog.image_registries.resolution import resolve_project_image
from opi.services.project_key_secret import CMP_SOPS_ROLE_NAME, SOPS_SECRET_NAME
from opi.services.project_utils import resolve_deployment_filter
from opi.services.secrets import RegistrySecret
from opi.services.tasks import progress_steps as steps
from opi.utils.age import (
    decrypt_password_smart,
    get_decoded_project_private_key,
    get_project_public_key,
    project_private_key_or_none,
)
from opi.utils.connect_retry import retry_on_connect_error
from opi.utils.naming import (
    _sanitize_for_lowercase,
    generate_infrastructure_manifest_path,
    generate_postgres_superuser_secret_name,
    generate_registry_secret_name,
)
from opi.utils.passwords import generate_secure_password
from opi.utils.sops import encrypt_to_sops_files_or_fail, sops_filenames

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from opi.connectors.argo import ArgoConnector
    from opi.connectors.git_repo import GitRepoWorkspace
    from opi.connectors.kubectl import KubectlConnector
    from opi.generation.manifests import ManifestGenerator
    from opi.manager.argo_manager import ArgoManager
    from opi.manager.database_manager import DatabaseManager
    from opi.runtime.tasks.persistent_task_progress import PersistentTaskProgressManager
    from opi.services.project_file_handler import ProjectFileHandler
    from opi.services.project_key_secret import ProjectKeySecretHandler

logger = logging.getLogger(__name__)

# ArgoCD reports a CNPG cluster healthy as soon as the pod is Ready, which is before the
# primary accepts connections. This is the budget for that gap.
DATABASE_CONNECT_TIMEOUT_SECONDS = 180

#: Superuser credentials already proven against a (host, user, password) in this process.
_validated_infrastructure_credentials: set[str] = set()


class InfrastructureAccess(Protocol):
    """What the namespace and infrastructure work reaches from its host.

    It writes no instance state of its own: everything it changes lives in the cluster or
    in the deployment repository.
    """

    _argo_manager: ArgoManager
    _kubectl_connector: KubectlConnector
    _manifest_generator: ManifestGenerator
    _project_file_handler: ProjectFileHandler
    _sops_handler: ProjectKeySecretHandler

    async def get_contents(self, *, record_base: bool = True) -> dict[str, Any]: ...

    async def get_deployments(
        self,
        cluster_filter: bool = True,
        deployment_name: str | None = None,
        deployment_names: list[str] | None = None,
    ) -> list[dict[str, Any]]: ...

    def get_progress_manager(self) -> PersistentTaskProgressManager | None: ...

    async def get_deployment_workspace(self, name: str, repo_config: dict[str, str]) -> GitRepoWorkspace: ...

    async def _ensure_database_manager(self, skip_credential_check: bool = False) -> DatabaseManager: ...

    def _umbrella_verversbewaker(self, argo_connector: ArgoConnector) -> AbstractAsyncContextManager[None]: ...


class Infrastructure:
    """The namespace and infrastructure work of ``ProjectManager``, built per call around its host."""

    def __init__(self, host: InfrastructureAccess) -> None:
        self._host = host

    async def check_and_create_namespaces(
        self,
        deployment_name: str | None = None,
        deployment_names: list[str] | None = None,
        known_namespace_labels: dict[str, dict[str, str]] | None = None,
    ) -> bool:
        """Create the namespaces of this project's deployments on this cluster, if absent.

        ``known_namespace_labels`` maps namespace to its labels, as read in one call by the
        caller. Given this, an existing namespace with the right labels costs no kubectl
        invocation at all. Startup passes it because it walks every project; per-request
        callers leave it None and each check talks to the cluster.
        """

        project_data: dict[str, str | list | dict[str, str]] = await self._host.get_contents()
        logger.info(f"Checking namespaces for project: {project_data['name']}")

        # Track namespace creation with progress manager if available
        progress_manager = self._host.get_progress_manager()
        namespace_subtask = None
        if progress_manager:
            namespace_subtask = progress_manager.add_task(steps.ENVIRONMENT_SETUP)

        # Get deployments for THIS cluster using helper method
        deployments = await self._host.get_deployments(
            cluster_filter=True, deployment_name=deployment_name, deployment_names=deployment_names
        )

        targets = resolve_deployment_filter(deployment_name, deployment_names)
        if targets is not None:
            logger.info(f"Checking namespaces only for deployment(s): {targets}")

        if not deployments:
            logger.info(f"No deployments found in project {project_data['name']}")
            return True

        all_successful = True

        # Deployments share a namespace (one per project, not per deployment), so the
        # same namespace was checked and labelled once per deployment: 127 rounds for
        # 44 namespaces at startup. Collapse to the distinct set first.
        namespaces: dict[str, list[str]] = {}
        for deployment in deployments:
            namespace = get_prefixed_namespace(settings.CLUSTER_MANAGER, cast("str", deployment["namespace"]))
            namespaces.setdefault(namespace, []).append(cast("str", deployment["name"]))

        wanted_labels = namespace_labels(settings.CLUSTER_MANAGER)

        for namespace, deployment_list in namespaces.items():
            logger.info(
                f"Checking namespace '{namespace}' for deployment(s) {deployment_list} for project '{project_data['name']}':"
            )

            if known_namespace_labels is not None:
                namespace_exists = namespace in known_namespace_labels
                present = known_namespace_labels.get(namespace, {})
                labels_are_correct = all(present.get(key) == value for key, value in wanted_labels.items())
            else:
                namespace_exists = await self._host._kubectl_connector.namespace_exists(namespace)
                labels_are_correct = False  # unknown, so apply them as before

            if namespace_exists:
                logger.info(f"Namespace '{namespace}' already exists for project '{project_data['name']}'")
            else:
                logger.info(f"Creating namespace '{namespace}' for project '{project_data['name']}':")
                # Create the namespace using shared function
                await self.create_namespace_with_argocd_label(namespace)
                labels_are_correct = True  # applied as part of creation

            # Idempotent, but not free: skip it when we already read the right values.
            if not labels_are_correct:
                await self.ensure_namespace_labels(namespace)

            if progress_manager:
                progress_manager.set_namespace(namespace)

        # Complete namespace subtask if progress manager is available
        if progress_manager and namespace_subtask:
            if all_successful:
                progress_manager.complete_task(namespace_subtask)
            else:
                progress_manager.fail_task(namespace_subtask, "Failed to create one or more namespaces")

        return all_successful

    async def create_namespace_with_argocd_label(self, namespace: str) -> None:
        """Create a namespace with the ArgoCD managed-by and Pod Security labels.

        ``namespace`` carries the cluster prefix already.

        Raises:
            KubectlExecutionError: If applying the namespace manifest fails.
            RuntimeError: If Capsule does not assign the tenant label in time.
        """
        logger.info(f"Creating namespace '{namespace}'")

        # Create the namespace using the manifest template
        manifest_path = os.path.join(settings.MANIFESTS_PATH, "namespace.yaml.jinja")

        # Template variables
        variables = {
            "namespace": namespace,
            "manager": get_argo_namespace(settings.CLUSTER_MANAGER),
            "pod_security_labels": get_pod_security_labels(settings.CLUSTER_MANAGER),
        }

        await self._host._kubectl_connector.apply_manifest(manifest_path, variables)

        # If Capsule is enabled, wait for Capsule to assign the tenant label
        # before attempting to modify the namespace with additional labels
        if uses_capsule(settings.CLUSTER_MANAGER):
            logger.info(f"Cluster uses Capsule, waiting for tenant label assignment on namespace '{namespace}'")
            capsule_ready = await self._host._kubectl_connector.wait_for_capsule_tenant_label(namespace, timeout=30)

            if not capsule_ready:
                raise RuntimeError(
                    f"Timeout waiting for Capsule to assign tenant label to namespace '{namespace}'. "
                    "Cannot proceed with namespace configuration."
                )

        # Labelled once more after creation: the manifest sets them already, but on a cluster
        # with Capsule this only runs after the wait for the tenant label is done.
        await self.ensure_namespace_labels(namespace)

        logger.info(f"Successfully created namespace '{namespace}' with the managed-by and Pod Security labels")

    async def ensure_namespace_labels(self, namespace: str) -> None:
        """Ensure the managed-by and Pod Security labels exist on this namespace (idempotent)."""
        label_result = await self._host._kubectl_connector.apply_labels_to_resource(
            resource_type="namespace",
            resource_name=namespace,
            labels=namespace_labels(settings.CLUSTER_MANAGER),
        )
        if label_result:
            logger.info(f"Ensured ArgoCD managed-by and Pod Security labels on namespace: {namespace}")
        else:
            raise RuntimeError(
                f"Failed to apply the managed-by and Pod Security labels to namespace '{namespace}'. "
                "ArgoCD will not be able to manage resources in this namespace."
            )

    async def ensure_sops_secret_in_namespace(self, namespace: str, project_data: dict[str, Any]) -> None:
        """Create or replace the SOPS secret of this project in ``namespace`` (idempotent).

        An existing secret that does not hold the project's current public key is deleted
        and written again, as the old one can no longer decrypt what this project renders.

        Raises:
            RuntimeError: If the project has no public key, or the secret cannot be written.
        """
        project_name = project_data.get("name", "unknown")
        logger.info(f"Checking SOPS secret for project {project_name} in namespace {namespace}")

        public_key = get_project_public_key(project_data)
        if not public_key:
            msg = f"Project {project_name} has no AGE public key; a SOPS secret cannot be written without it"
            raise RuntimeError(msg)
        private_key = await get_decoded_project_private_key(project_data)

        existing_secret = await self._host._kubectl_connector.get_sops_secret_from_namespace(
            namespace, SOPS_SECRET_NAME
        )

        create_sops_secret = False
        if existing_secret is None:
            logger.info(f"SOPS secret not found for project {project_name} in namespace {namespace}")
            create_sops_secret = True
        elif public_key not in existing_secret:
            create_sops_secret = True
            logger.warning(
                f"Found existing SOPS secret in namespace {namespace} for project {project_name}. "
                f"Project has new SOPS keys - the old secret is now obsolete and will be replaced."
            )
            try:
                await self._host._kubectl_connector.delete_resource("secret", "sops-age-key", namespace)
                logger.info(f"Deleted old SOPS secret from namespace {namespace}")
            except Exception as e:
                logger.warning(f"Failed to delete old SOPS secret (continuing anyway): {e}")

        if create_sops_secret:
            # The handler owns the retry on the RBAC race; a False is its final answer.
            if not await self._host._sops_handler.store_project_sops_key_in_namespace(
                namespace, private_key, public_key
            ):
                message = (
                    f"Failed to create SOPS secret in namespace {namespace}. "
                    "RBAC permissions may not have propagated for this namespace."
                )
                logger.error(message)
                raise RuntimeError(message)
            logger.info(f"Created new SOPS secret for project {project_name} in namespace {namespace}")
        else:
            logger.info(f"Found existing SOPS secret for project {project_name} in namespace {namespace}")
            # The secret alone is not enough: without this Role the CMP of ArgoCD cannot read
            # it, and then ArgoCD no longer renders this project.
            try:
                await self._host._sops_handler.ensure_cmp_key_reader(namespace)
            except KubectlExecutionError as e:
                raise RuntimeError(
                    f"Failed to apply the CMP key reader role ({CMP_SOPS_ROLE_NAME}) in namespace {namespace}: {e}"
                ) from e

    @staticmethod
    async def test_infrastructure_database_connection(username: str, password: str, host: str) -> tuple[bool, str]:
        """Whether these superuser credentials open a connection, and what failed if not.

        Connects directly instead of through a connector: it runs before the project's
        database infrastructure exists. The second element is ``"success"``, ``"auth_error"``
        (wrong password), ``"connection_error"`` (not reachable, so possibly not running
        yet) or ``"unknown_error"``, and the caller must tell those apart: only the first
        is a reason to refuse.
        """
        # A passed check on exactly this (host, user, password) holds as long as the process
        # lives: the secret has not changed, so setting up the connection again measures the
        # same. A password that changes SERVER-SIDE without the secret changing falls outside
        # this, and surfaces as an auth error on that round's first real database operation.
        memo = hashlib.sha256(f"{host}\n{username}\n{password}".encode()).hexdigest()
        if memo in _validated_infrastructure_credentials:
            logger.debug(
                "Infrastructure credentials for %s@%s have already been tested in this process", username, host
            )
            return True, "success"

        try:
            # Create a direct connection with the superuser credentials to the postgres database
            conn = await asyncpg.connect(
                host=host, port=5432, user=username, password=password, database="postgres", timeout=5
            )
            await conn.close()
            logger.debug(f"Infrastructure database connection test successful for {username}@{host}/postgres")
            _validated_infrastructure_credentials.add(memo)
            return True, "success"
        except asyncpg.InvalidPasswordError as e:
            # Authentication failed - wrong password
            logger.debug(f"Infrastructure database authentication failed for user {username}: {e}")
            return False, "auth_error"
        except (ConnectionRefusedError, socket.gaierror, OSError) as e:
            # Database not reachable - likely not running yet or DNS not ready
            logger.debug(f"Infrastructure database not reachable at {host}: {e}")
            return False, "connection_error"
        except Exception as e:
            # Other errors
            logger.debug(f"Infrastructure database connection test failed for user {username}: {e}")
            return False, "unknown_error"

    async def read_database_cluster_status(self, cluster_name: str, project_name: str) -> dict[str, Any]:
        """The CNPG Cluster status of the project's dedicated cluster.

        Empty when the resource is absent or unreadable, so a caller cannot tell those
        apart and must stay silent instead of reporting a cluster as unhealthy.
        """
        cluster = await self._host._kubectl_connector.get_namespaced_resource(
            "clusters.postgresql.cnpg.io",
            f"{_sanitize_for_lowercase(project_name)}-db",
            get_infrastructure_namespace(cluster_name, project_name),
        )
        if not cluster:
            return {}
        status = cluster.get("status", {})
        return status if isinstance(status, dict) else {}

    @staticmethod
    def database_cluster_phase_summary(status: dict[str, Any]) -> str:
        """What CNPG says about the cluster, as a suffix for a message, or an empty string."""
        parts = [f"{key}: {status[key]}" for key in ("phase", "phaseReason") if status.get(key)]
        return f" ({', '.join(parts)})" if parts else ""

    async def wait_until_database_accepts_connections(
        self, project_name: str, cluster_name: str, username: str, password: str
    ) -> None:
        """Wait until the project's PostgreSQL primary answers a superuser query.

        ArgoCD calls the infrastructure healthy before the primary accepts connections, so
        provisioning that starts right after that wait hits a ConnectionRefusedError.

        Raises:
            RuntimeError: If no connection was accepted within the timeout.
        """
        host = get_database_cluster_service_endpoint(cluster_name, project_name)

        async def _select_one() -> None:
            conn = await asyncpg.connect(
                host=host, port=5432, user=username, password=password, database="postgres", timeout=5
            )
            try:
                await conn.fetchval("SELECT 1")
            finally:
                await conn.close()

        try:
            await retry_on_connect_error(
                _select_one,
                DATABASE_CONNECT_TIMEOUT_SECONDS,
                f"PostgreSQL at {host}",
                retry_on=(OSError,),
            )
        except OSError as e:
            summary = self.database_cluster_phase_summary(
                await self.read_database_cluster_status(cluster_name, project_name)
            )
            raise RuntimeError(
                f"Database of project '{project_name}' accepts no connections at {host} after "
                f"{DATABASE_CONNECT_TIMEOUT_SECONDS}s{summary}: {e}"
            ) from e
        logger.info(f"Database of project '{project_name}' accepts connections at {host}")

    async def warn_when_database_cluster_not_ready(
        self,
        project_name: str,
        cluster_name: str,
        progress_manager: PersistentTaskProgressManager | None,
        infra_task: str | None,
    ) -> None:
        """Report a dedicated cluster that CNPG itself does not call Ready, without blocking.

        The database answering queries is what provisioning needs; the operator reaching
        the instances is what backup and failover need. A cluster whose Ready condition
        stays false therefore gets a warning instead of a failure.
        """
        status = await self.read_database_cluster_status(cluster_name, project_name)
        if not status:
            return

        conditions = status.get("conditions") or []
        ready = next((c for c in conditions if isinstance(c, dict) and c.get("type") == "Ready"), None)
        if ready and ready.get("status") == "True":
            return

        summary = self.database_cluster_phase_summary(status)
        logger.warning(
            f"CNPG does not report the database cluster of project '{project_name}' as Ready{summary}. "
            f"The database accepts connections, but the operator cannot reach the instances, so backups "
            f"and failover through the operator are not guaranteed."
        )
        if progress_manager and infra_task:
            warning_step = progress_manager.add_subtask(infra_task, steps.database_not_ready(summary))
            progress_manager.complete_task(warning_step)

    async def create_infrastructure_namespace(self, project_data: dict[str, Any], cluster_name: str) -> None:
        """Create the namespace that holds this project's own PostgreSQL cluster, if absent.

        Raises:
            RuntimeError: If the namespace or its SOPS secret cannot be created.
        """
        project_name = project_data.get("name")
        if not project_name:
            raise ValueError("Project name is required in project_data")

        # Get infrastructure namespace with cluster-specific prefix
        infrastructure_namespace = get_infrastructure_namespace(cluster_name, project_name)

        logger.info(f"Checking infrastructure namespace '{infrastructure_namespace}' for project '{project_name}'")

        # Track namespace creation with progress manager if available
        progress_manager = self._host.get_progress_manager()
        namespace_subtask = None
        if progress_manager:
            namespace_subtask = progress_manager.add_task(steps.ENVIRONMENT_SETUP, subject="projectdatabase")

        try:
            # Check if namespace exists, create if needed (using shared function)
            namespace_exists = await self._host._kubectl_connector.namespace_exists(infrastructure_namespace)
            if not namespace_exists:
                await self.create_namespace_with_argocd_label(infrastructure_namespace)
            else:
                logger.info(
                    f"Infrastructure namespace '{infrastructure_namespace}' already exists for project '{project_name}'"
                )

            # Always ensure the managed-by and Pod Security labels exist (idempotent)
            await self.ensure_namespace_labels(infrastructure_namespace)

            # Create SOPS secret in the infrastructure namespace (using shared function)
            await self.ensure_sops_secret_in_namespace(infrastructure_namespace, project_data)

            if progress_manager and namespace_subtask:
                progress_manager.complete_task(namespace_subtask)

        except Exception as e:
            logger.error(f"Failed to create infrastructure namespace '{infrastructure_namespace}': {e}")
            if progress_manager and namespace_subtask:
                progress_manager.fail_task(namespace_subtask, f"Failed to create infrastructure namespace: {e}")
            raise RuntimeError(f"Cannot create infrastructure namespace '{infrastructure_namespace}': {e}") from e

    async def create_infrastructure_resources(self, project_data: dict[str, Any], cluster_name: str) -> None:
        """Provision this project's own PostgreSQL cluster and wait until it answers.

        Writes the manifests to the deployment repository, has ArgoCD roll them out, and
        only returns once the primary accepts a connection: ArgoCD reports Healthy earlier
        than that, and the provisioning after this call needs a database that answers. The
        superuser secret is read before it is written, as a regenerated password no longer
        matches a cluster that already exists.

        Raises:
            RuntimeError: If provisioning fails, or the credentials do not match the cluster.
            TimeoutError: If the infrastructure does not become ready within the timeout.
        """
        project_name = project_data.get("name")
        if not project_name:
            raise ValueError("Project name is required in project_data")

        # Get configuration
        infrastructure_namespace = get_infrastructure_namespace(cluster_name, project_name)
        # Initialize database manager for configuration only (skip credential check during bootstrap)
        db_manager = await self._host._ensure_database_manager(skip_credential_check=True)
        database_cluster_config = db_manager._get_database_cluster_config(project_data, cluster_name)
        storage_class = get_storage_class_name(cluster_name)
        project_clean = _sanitize_for_lowercase(project_name)

        logger.info(f"Creating infrastructure resources for project '{project_name}' in cluster '{cluster_name}'")

        # Track infrastructure creation with progress manager
        progress_manager = self._host.get_progress_manager()
        infra_task = None
        if progress_manager:
            infra_task = progress_manager.add_task(steps.PROJECT_DATABASE_SETUP)

        try:
            # STEP 1: Check for existing superuser credentials and validate them
            superuser_secret_name = generate_postgres_superuser_secret_name(project_name)
            existing_secret = await self._host._kubectl_connector.get_secret(
                superuser_secret_name, infrastructure_namespace
            )

            if existing_secret:
                logger.info(f"Found existing superuser secret for project '{project_name}', validating credentials")
                superuser_username = existing_secret.get("username", "postgres")
                superuser_password = existing_secret.get("password")

                if not superuser_password:
                    raise RuntimeError(
                        f"Superuser secret '{superuser_secret_name}' exists but has no password. "
                        f"Manual intervention required to fix or delete the secret."
                    )

                # Test the credentials against the PostgreSQL cluster
                db_host = get_database_cluster_service_endpoint(cluster_name, project_name)
                credentials_valid, error_type = await self.test_infrastructure_database_connection(
                    username=superuser_username,
                    password=superuser_password,
                    host=db_host,
                )

                if not credentials_valid:
                    if error_type == "auth_error":
                        # Actual authentication failure - wrong password
                        raise RuntimeError(
                            f"Superuser credentials for project '{project_name}' are invalid. "
                            f"The password in secret '{superuser_secret_name}' does not match the PostgreSQL cluster. "
                            f"This usually happens when the secret was regenerated after the cluster was created. "
                            f"Manual intervention required: either restore the correct password in the secret, "
                            f"or delete both the secret and the PostgreSQL cluster to recreate from scratch."
                        )
                    elif error_type == "connection_error":
                        # Database not reachable yet - assume credentials are correct and continue
                        logger.info(
                            f"Cannot verify superuser credentials for project '{project_name}' because database is not reachable yet. "
                            f"Assuming existing credentials are correct and proceeding with infrastructure creation."
                        )
                    else:
                        # Unknown error - log warning but continue
                        logger.warning(
                            f"Could not verify superuser credentials for project '{project_name}' due to unknown error. "
                            f"Assuming existing credentials are correct and proceeding with infrastructure creation."
                        )
                else:
                    logger.info(f"Existing superuser credentials validated successfully for project '{project_name}'")
            else:
                # No existing secret - generate new credentials for first-time creation
                logger.info(
                    f"No existing superuser secret found, generating new credentials for project '{project_name}'"
                )
                superuser_username = "postgres"
                superuser_password = generate_secure_password(
                    min_uppercase=3, min_lowercase=3, min_digits=3, total_length=32
                )

            # STEP 2: Create secret manifest with validated or new credentials
            secret_manifest = render_template(
                "generic-secret.yaml.to-sops.jinja",
                {
                    "name": superuser_secret_name,
                    "namespace": infrastructure_namespace,
                    "secret_type": "postgres-credentials",
                    "secret_k8s_type": "kubernetes.io/basic-auth",
                    "secret_pairs": {"username": superuser_username, "password": superuser_password},
                },
            )

            # STEP 2: Generate PostgreSQL cluster manifest
            logger.info(f"Generating PostgreSQL cluster manifest for project '{project_name}'")

            # Handle registry configuration for PostgreSQL image
            database_config = database_cluster_config["database_config"]
            registry_name = database_config.get("registry")
            image_pull_secrets_map = {}
            registry_config = None

            if registry_name:
                logger.info(f"PostgreSQL database configured with registry: {registry_name}")

                # Reuse existing registry extraction logic
                registries = self._host._project_file_handler.extract_registries(project_data)
                for registry in registries:
                    if registry.get("name") == registry_name:
                        registry_config = registry
                        break

                if not registry_config:
                    raise ValueError(
                        f"Registry '{registry_name}' specified in namespace-postgresql-database service "
                        f"but not found in project registries. Available registries: "
                        f"{[r.get('name') for r in registries]}"
                    )

                database_image = database_config.get("image")
                secret_name_ref = registry_config.get("secretName")

                if secret_name_ref:
                    # Pre-existing secret: use directly, skip creation
                    registry_secret_name = secret_name_ref
                    logger.info(
                        f"PostgreSQL will use pre-existing imagePullSecret '{registry_secret_name}' "
                        f"for image '{database_image}'"
                    )
                else:
                    # Generate registry secret name for infrastructure (using "infrastructure" as deployment name)
                    registry_secret_name = generate_registry_secret_name("infrastructure", registry_name)
                    logger.info(
                        f"PostgreSQL will use imagePullSecret '{registry_secret_name}' for image '{database_image}'"
                    )

                # Map the database image to its registry secret
                image_pull_secrets_map[database_image] = registry_secret_name

            # The cluster rewrite applies to the database image as well. Without it the bare
            # ghcr path sits in git while the admission of ODCN rewrites the pod to
            # rcr.rijksapps.nl, and then the infrastructure application is permanently
            # OutOfSync. That blocks more than it looks: processing waits for a Synced
            # infrastructure before it continues, so a refresh of such a project hangs. Same
            # rules as any other image, so a project registry still wins over the shared proxy.
            resolved_db = resolve_project_image(
                database_config.get("image", ""), project_data, cluster_name, registry_name
            )
            if resolved_db.image != database_config.get("image"):
                logger.info(f"PostgreSQL-image '{database_config.get('image')}' opgelost naar '{resolved_db.image}'")
                database_config = {**database_config, "image": resolved_db.image}
            if resolved_db.secret and resolved_db.image not in image_pull_secrets_map:
                image_pull_secrets_map[resolved_db.image] = resolved_db.secret

            cluster_manifest = render_template(
                "postgresql-cluster.yaml.jinja",
                {
                    "project_name": project_clean,
                    "infrastructure_namespace": infrastructure_namespace,
                    "database_config": database_config,
                    "storage_class": storage_class,
                    "imagePullSecretsMap": image_pull_secrets_map,
                },
            )

            # STEP 3: Write infrastructure resource manifests to deployment Git repository
            logger.info(f"Committing infrastructure manifests to deployment repo for project '{project_name}'")

            # Get project's main repository (infrastructure uses same repo as deployments)
            repositories = project_data.get("repositories", [])
            if not repositories:
                raise ValueError("No repositories defined in project data")
            main_repo = repositories[0]

            # Create repo config for infrastructure (treated as a special deployment)
            infra_repo_config = {
                "name": "infrastructure",
                "url": main_repo.get("url", ""),
                "username": main_repo.get("username"),
                "password": main_repo.get("password"),
                "branch": main_repo.get("branch", "main"),
                "path": main_repo.get("path", ""),
            }

            infra_workspace = await self._host.get_deployment_workspace("infrastructure", infra_repo_config)

            async def _write_infrastructure_manifests() -> None:
                """Write the infrastructure directory into this round's working copy."""
                deployment_working_dir = await infra_workspace.get_working_dir()

                # Create infrastructure resources directory in deployment repo
                # Path: {cluster}/{project_name}/infrastructure/
                # This contains the actual Kubernetes resources (PostgreSQL cluster, secrets)
                infra_resources_dir = os.path.join(
                    deployment_working_dir,
                    generate_infrastructure_manifest_path(cluster_name, project_name, infra_repo_config["path"]),
                )
                os.makedirs(infra_resources_dir, exist_ok=True)

                # Write manifests - secret as .to-sops.yaml for encryption
                secret_path = os.path.join(
                    infra_resources_dir, sops_filenames(f"{project_clean}-postgres-superuser-secret").plaintext
                )
                cluster_path = os.path.join(infra_resources_dir, f"{project_clean}-db-cluster.yaml")

                with open(secret_path, "w") as f:
                    f.write(secret_manifest)
                with open(cluster_path, "w") as f:
                    f.write(cluster_manifest)

                # Create a least-privilege baseline network policy for the
                # infrastructure namespace. This namespace hosts the project's
                # PostgreSQL cluster, so it must accept ingress from the project's
                # own tenant app namespaces (where the application pods connect
                # from) while denying everything else. It is NOT allow-all.
                tenant_namespaces = sorted(
                    {
                        get_prefixed_namespace(cluster_name, deployment_def["namespace"])
                        for deployment_def in project_data.get("deployments", [])
                        if deployment_def.get("cluster") == cluster_name and deployment_def.get("namespace")
                    }
                )
                logger.info(
                    f"Generating baseline network policy for infrastructure namespace: {infrastructure_namespace} "
                    f"(ingress allowed from tenant namespaces: {tenant_namespaces})"
                )
                network_policy_manifest = render_template(
                    "tenant-baseline-network-policy.yaml.jinja",
                    {
                        "name": "tenant-baseline",
                        "namespace": infrastructure_namespace,
                        # No deployment_selector: this policy covers the whole
                        # infrastructure namespace (CNPG operator + cluster pods,
                        # none of which carry our `deployment:` label).
                        "deployment_selector": None,
                        "ops_namespace": get_namespace(cluster_name),
                        "backup_namespace": get_backup_namespace(cluster_name),
                        "ingress_controller_selector": get_ingress_controller_selector(cluster_name),
                        "ops_egress": get_ops_egress(cluster_name),
                        "cluster_cidrs": get_cluster_cidrs(cluster_name),
                        # The CNPG operator (in its own namespace) must reach the
                        # dedicated cluster's pods to extract instance status, or the
                        # Cluster never becomes Ready. The DB subsystem owns which
                        # namespace that is; only the infra namespace needs this rule.
                        "database_operator_namespace": database_cluster_config["database_operator_namespace"],
                        # The project's app namespaces must reach the PostgreSQL
                        # cluster that lives in this infrastructure namespace.
                        "allowed_ingress_namespaces": tenant_namespaces,
                        # Skip the "project infra" egress rule: this IS the infra
                        # namespace, so an explicit self-rule would be redundant.
                        "project_infra_namespace": None,
                    },
                )
                network_policy_path = os.path.join(infra_resources_dir, "tenant-baseline-network-policy.yaml")
                with open(network_policy_path, "w") as f:
                    f.write(network_policy_manifest)
                logger.info(f"Created baseline network policy for infrastructure namespace: {infrastructure_namespace}")

                # Create registry secret if PostgreSQL uses a private registry (skip for pre-existing secrets)
                if registry_name and registry_config and not registry_config.get("secretName"):
                    logger.info(
                        f"Creating registry secret for PostgreSQL infrastructure in namespace: {infrastructure_namespace}"
                    )

                    # Decrypt registry credentials (reuse existing logic)
                    registry_url = registry_config.get("upstream", "")
                    username = registry_config.get("username", "")
                    password_encrypted = registry_config.get("password", "")

                    private_key = await get_decoded_project_private_key(project_data)
                    password = await decrypt_password_smart(password_encrypted, private_key)

                    # Create RegistrySecret instance (same as deployments)
                    registry_secret = RegistrySecret(registry_url=registry_url, username=username, password=password)

                    # Use generic secret template with kubernetes.io/dockerconfigjson type (same as deployments)
                    registry_secret_manifest = render_template(
                        "generic-secret.yaml.to-sops.jinja",
                        {
                            "name": registry_secret_name,
                            "namespace": infrastructure_namespace,
                            "secret_type": "registry",
                            "secret_k8s_type": "kubernetes.io/dockerconfigjson",
                            "secret_pairs": registry_secret.to_k8s_secret_data(),  # Contains .dockerconfigjson
                        },
                    )

                    # Write registry secret to infrastructure directory
                    registry_secret_path = os.path.join(
                        infra_resources_dir, sops_filenames(registry_secret_name).plaintext
                    )
                    with open(registry_secret_path, "w") as f:
                        f.write(registry_secret_manifest)

                    logger.info(
                        f"Created registry secret '{registry_secret_name}' for registry '{registry_name}' ({registry_url})"
                    )

                # STEP 4: SOPS encrypt the secrets using project's SOPS key
                logger.info(f"Encrypting secret with SOPS for project '{project_name}'")
                project_public_key = get_project_public_key(project_data)
                if not project_public_key:
                    raise RuntimeError(f"Project '{project_name}' does not have a SOPS public key configured")
                await asyncio.to_thread(
                    encrypt_to_sops_files_or_fail,
                    infra_resources_dir,
                    project_public_key,
                    f"infrastructuur-secrets voor project '{project_name}' in cluster '{cluster_name}'",
                    private_key=project_private_key_or_none(project_data),
                )

                # STEP 5: Generate kustomization.yaml and decrypt-sops.yaml for infrastructure resources
                logger.info(f"Generating infrastructure kustomization for project '{project_name}'")
                kustomization_success = self._host._manifest_generator.create_kustomization_files(
                    output_dir=infra_resources_dir,
                    namespace=infrastructure_namespace,
                )
                if not kustomization_success:
                    raise RuntimeError(f"Failed to create kustomization files for project '{project_name}'")

            await infra_workspace.mutate_and_push(
                f"Add infrastructure resources for {project_name} in {cluster_name} cluster",
                _write_infrastructure_manifests,
            )

            logger.info(
                f"Successfully committed infrastructure manifests to deployment repo for project '{project_name}'"
            )

            # STEP 6: Create ArgoCD Application, repo secret, and AppProject in ArgoCD applications repo
            # This folder is detected by App-of-Apps pattern and creates the ArgoCD Application
            logger.info(
                f"Creating ArgoCD application resources for infrastructure in {cluster_name}/{project_name}-infrastructure"
            )

            success = await self._host._argo_manager.create_infrastructure_application(
                project_data=project_data, database_config=database_cluster_config, cluster_name=cluster_name
            )

            if not success:
                raise RuntimeError(f"Failed to create ArgoCD infrastructure application for project '{project_name}'")

            logger.info(f"Successfully created ArgoCD infrastructure application for project '{project_name}'")

            # STEP 7: the ArgoCD connection for the refresh watcher below and for STEP 9
            logger.info(
                f"Refreshing ArgoCD user-applications to create infrastructure application for '{project_name}'"
            )
            argo_connector = create_argo_connector()

            if not await argo_connector.login():
                raise RuntimeError("Failed to login to ArgoCD")

            # STEP 8: Wait for infrastructure application to be created by ArgoCD
            infra_app_name = f"{project_name}-infrastructure"
            if progress_manager and infra_task:
                progress_manager.update_task(infra_task, steps.PROJECT_DATABASE_ROLLOUT)

            async with self._host._umbrella_verversbewaker(argo_connector):
                await self._host._argo_manager.wait_for_application_created(
                    app_name=infra_app_name,
                    timeout=APPLICATIE_AANMAAK_TIMEOUT_SECONDEN,
                    poll_interval=1,  # goedkope exists-check; 5s-rooster kostte ~4s per wachtstap
                )

            logger.info(f"Infrastructure application '{infra_app_name}' has been created, refreshing it")

            # STEP 9: Refresh the infrastructure application to ensure it picks up latest changes
            infra_reconciled_at = await argo_connector.refresh_application(infra_app_name)
            if not infra_reconciled_at:
                raise RuntimeError(f"Failed to refresh ArgoCD infrastructure application '{infra_app_name}'")

            logger.info(f"Infrastructure application '{infra_app_name}' refreshed successfully")

            # STEP 10: Wait for infrastructure to be synced and healthy
            logger.info(
                f"Waiting for infrastructure to be ready for project '{project_name}' (this may take a few minutes)..."
            )

            if progress_manager and infra_task:
                progress_manager.update_task(infra_task, steps.PROJECT_DATABASE_WAIT_READY)

            await self._host._argo_manager.wait_for_infrastructure_ready(
                project_name=project_name,
                cluster_name=cluster_name,
                timeout=600,  # 10 minutes timeout
                refreshed_after=infra_reconciled_at,
            )

            logger.info(f"Infrastructure is ready for project '{project_name}'")

            if progress_manager and infra_task:
                progress_manager.update_task(infra_task, steps.PROJECT_DATABASE_WAIT_CONNECTIONS)

            await self.wait_until_database_accepts_connections(
                project_name=project_name,
                cluster_name=cluster_name,
                username=superuser_username,
                password=superuser_password,
            )

            await self.warn_when_database_cluster_not_ready(
                project_name=project_name,
                cluster_name=cluster_name,
                progress_manager=progress_manager,
                infra_task=infra_task,
            )

            if progress_manager and infra_task:
                progress_manager.complete_task(infra_task)

        except (TimeoutError, RuntimeError) as e:
            logger.error(f"Failed to create infrastructure resources for project '{project_name}': {e}")
            if progress_manager and infra_task:
                progress_manager.fail_task(infra_task, f"Infrastructure provisioning failed: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error creating infrastructure resources for project '{project_name}': {e}")
            if progress_manager and infra_task:
                progress_manager.fail_task(infra_task, f"Unexpected error: {e}")
            raise RuntimeError(f"Cannot create infrastructure resources for project '{project_name}': {e}") from e

    async def check_and_create_sops_secrets_in_namespaces(
        self, deployment_name: str | None = None, deployment_names: list[str] | None = None
    ) -> None:
        """Write the SOPS secret in every namespace this project's deployments use."""
        contents = await self._host.get_contents()
        project_name = contents.get("name")

        # Get deployments for THIS cluster using helper method
        deployments = await self._host.get_deployments(
            cluster_filter=True, deployment_name=deployment_name, deployment_names=deployment_names
        )

        targets = resolve_deployment_filter(deployment_name, deployment_names)
        if targets is not None:
            logger.info(f"Creating SOPS secrets only for deployment(s): {targets}")

        if not deployments:
            logger.warning(f"No deployments found in project: {project_name}")
            return

        # The SOPS secret lives per namespace, and deployments of a project share one,
        # so iterating deployments read the same secret several times: 128 `kubectl get
        # secret` calls for 44 namespaces at startup. Same fix as for the namespaces.
        namespaces = {
            get_prefixed_namespace(cast("str", deployment["cluster"]), cast("str", deployment["namespace"]))
            for deployment in deployments
        }

        for namespace in sorted(namespaces):
            # Use shared function for SOPS secret creation
            await self.ensure_sops_secret_in_namespace(namespace, contents)
