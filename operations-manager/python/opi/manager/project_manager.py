"""
The project manager handles project files. It can read, update, delete, or process them.
Processing means it can create, update, or delete any resources defined in a project file.
"""

import copy
import logging
import os
import shutil
import tempfile
import time
from collections.abc import (  # noqa: TC003 - used in an eagerly-evaluated annotation (no future-annotations)
    Callable,
    Iterator,
)
from contextlib import AbstractAsyncContextManager, contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar, cast

import asyncpg
from sqlalchemy.exc import SQLAlchemyError

from opi.connectors import create_argo_connector
from opi.connectors.git import GitConnector, repo_lock_key
from opi.connectors.git_repo import (
    GitRepoWorkspace,
    argo_workspace,
    credentials_from_repo_config,
    get_git_repo,
    workspace_from_repo_config,
)
from opi.connectors.kubectl import KubectlConnector
from opi.core.actor import get_actor
from opi.core.cluster_config import (
    get_infrastructure_namespace,
    is_reserved_project_name,
)
from opi.core.config import settings
from opi.core.metrics import (
    PHASE_GENERATE,
    PHASE_PROVISION,
    rollout_phase,
)
from opi.generation.manifests import (
    ManifestGenerator,
)
from opi.manager.project.aliases import (
    DeploymentAliases,
)
from opi.manager.project.argocd_sync import ArgoCDSync
from opi.manager.project.attachment_mutations import AttachmentMutations
from opi.manager.project.component_mutations import ComponentMutations
from opi.manager.project.database_schema_mutations import DatabaseSchemaMutations
from opi.manager.project.deployment_mutations import DeploymentMutations
from opi.manager.project.deployment_removal import (
    DeploymentRemoval,
    DeploymentRemovalResult,
    RemovalTrigger,
    marking_service,
    remove_deployment,
)
from opi.manager.project.helm_deployment import (
    HelmDeployment,
)
from opi.manager.project.infrastructure import Infrastructure
from opi.manager.project.manifests.application import ApplicationManifests
from opi.manager.project.manifests.repository import RepositoryManifests
from opi.manager.project.registry_mutations import RegistryMutations
from opi.manager.project.results import (  # noqa: TC001 - used in an eagerly-evaluated annotation (no future-annotations)
    DeploymentResult,
)
from opi.manager.project.rollout import DeploymentRollout, deployments_being_deleted
from opi.manager.project.service_config_mutations import ServiceConfigMutations
from opi.manager.project_validation import validate_project_structure
from opi.manager.revision_manager import RevisionManager
from opi.services.catalog.base import (
    ProvisionContext,
    SecretFileSpec,
)
from opi.services.catalog.image_registries.resolution import (
    set_deployment_component_registry,
)
from opi.services.deployment_order import order_deployments_by_clone_dependency
from opi.services.postgres_scope import project_uses_dedicated_postgres
from opi.services.project import Project
from opi.services.project_file_handler import (
    ProjectFileHandler,
)
from opi.services.project_key_secret import ProjectKeySecretHandler
from opi.services.project_reconciliation_service import record_reconciliation
from opi.services.project_schema import ProjectIntegrityError, validate_project_schema
from opi.services.project_store import MutationResult, get_project_store
from opi.services.project_utils import (
    RESERVED_PROJECT_NAME_MESSAGE,
    normalize_container_image,
    resolve_deployment_filter,
)
from opi.services.redeploy import run_redeploy_hooks
from opi.services.registry import (
    provisioning_services,
)
from opi.services.secrets import (
    BaseSecret,
)
from opi.services.tasks import progress_steps as steps
from opi.services.tasks.task_errors import TaskInputError

# Environment variables are now generated using service definitions
from opi.utils.naming import (
    RepositoryPathError,
    generate_argocd_application_name,
    generate_cluster_manifest_folder_path,
    generate_project_manifest_folder_path,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from types import TracebackType

    from opi.connectors.argo import ArgoConnector
    from opi.manager.database_manager import DatabaseManager
    from opi.runtime.tasks.persistent_task_progress import PersistentTaskProgressManager
    from opi.services.tasks.async_task_service import AsyncTaskService

# TypeVar for generic secret types
T = TypeVar("T", bound=BaseSecret)

logger = logging.getLogger(__name__)


def enforce_namespace_pin(project_data: dict[str, Any]) -> None:
    """Pin every deployment namespace to the project name (mutates in place).

    Defaults a missing namespace to the project name; raises ValueError on
    mismatch. Must be called by every entry that turns a project file into
    namespace/ArgoCD actions, otherwise a tenant can label another
    project's namespace.
    """
    project_name = project_data.get("name")
    if not project_name:
        return
    for deployment in project_data.get("deployments", []):
        declared_namespace = deployment.get("namespace")
        if declared_namespace is None:
            deployment["namespace"] = project_name
        elif declared_namespace != project_name:
            deployment_label = deployment.get("name", "<naamloos>")
            raise ValueError(
                f"Deployment '{deployment_label}' in project '{project_name}' "
                f"gebruikt namespace '{declared_namespace}', maar de namespace "
                f"moet gelijk zijn aan de projectnaam '{project_name}'. "
                f"Een afwijkende namespace is niet toegestaan omdat dit toegang "
                f"tot de resources van een ander project zou geven."
            )


def _deployment_name_for_path(path: str, yaml_doc: dict[str, Any] | None) -> str | None:
    """Resolve a DeepDiff path like ``deployments.7`` to the deployment name in ``yaml_doc``.

    Returns None when the path does not point inside a resolvable deployment entry.
    """
    parts = path.split(".")
    if len(parts) < 2 or parts[0] != "deployments" or not parts[1].isdigit():
        return None
    deployments = (yaml_doc or {}).get("deployments", [])
    index = int(parts[1])
    if 0 <= index < len(deployments) and isinstance(deployments[index], dict):
        return deployments[index].get("name")
    return None


def _deployment_names_for_paths(paths: Iterable[str], yaml_doc: dict[str, Any] | None) -> list[str]:
    """Resolve DeepDiff paths to deployment names (falling back to the raw path), deduplicated."""
    names = [_deployment_name_for_path(path, yaml_doc) or path for path in paths]
    return list(dict.fromkeys(names))


def _count_deployment_changes(label: str, paths: dict[str, Any], yaml_doc: dict[str, Any] | None) -> str:
    """One section of the change summary: how many, and the deployment names behind them."""
    names = _deployment_names_for_paths(paths, yaml_doc)
    suffix = f" ({', '.join(names)})" if names else ""
    return f"{label}: {len(paths)}{suffix}"


def _remote_identity(repo_config: dict[str, str]) -> tuple[str, str, str]:
    """The remote a repo config points at: URL without credentials, branch and path.

    The path belongs to it because the connector uses it to resolve file paths; two configs
    with a different path may therefore not share a working tree.
    """
    return (
        repo_lock_key(repo_config.get("url", "")),
        repo_config.get("branch", "main"),
        (repo_config.get("path") or "").strip("/"),
    )


class ProjectManager:
    """Manager for project resources and deployments."""

    # Class-level default: the state get_contents() last returned, used as the
    # compare-and-swap base on save. Declared here so instances that bypass
    # __init__ (tests calling the unbound methods with a stand-in self) still
    # resolve it, and so the attribute has one documented home.
    __contents_as_read: dict[str, Any] | None = None

    #: Whether the PROJECT LEVEL changed this round, and which repo it sits in. True until
    #: the opposite is measured: the guard in wait_for_project_level_application may only be
    #: skipped on evidence. At class level for the same reason as above: a caller that
    #: bypasses __init__ must still be able to read them.
    _project_level_manifests_changed: bool = True
    _project_level_repo: str | None = None
    #: The directory in that repo where the project-level manifests sit, so that it can be
    #: proven that ArgoCD has applied exactly those files.
    _project_level_path: str | None = None

    def __init__(
        self,
        *,
        project_file_relative_path: str | None = None,
        git_connector_for_project_files: GitConnector | None = None,
    ) -> None:
        self.__has_contents = False
        self.__contents_as_read: dict[str, Any] | None = None
        logger.debug("Initializing ProjectManager")
        self._project_file_relative_path = project_file_relative_path
        self._kubectl_connector = KubectlConnector()
        self._sops_handler = ProjectKeySecretHandler(self._kubectl_connector)
        self._manifest_generator = ManifestGenerator()
        self._project_file_handler = ProjectFileHandler()
        self._revision_manager = RevisionManager(self._project_file_handler)
        self.__git_connector_for_project_files = git_connector_for_project_files
        # Track ownership: if connector was injected, we don't own it and shouldn't close it
        self.__owns_git_connector_for_project_files = git_connector_for_project_files is None
        self.__argo_workspace: GitRepoWorkspace | None = None
        # each deployment has a repository, referenced by name
        self.__deployment_workspaces: dict[str, GitRepoWorkspace] = {}
        # The same workspaces, indexed on remote, so that two repo configs with the same
        # remote share one. See get_deployment_workspace.
        self.__deployment_workspaces_by_remote: dict[tuple[str, str, str], GitRepoWorkspace] = {}
        # Scratch directories that belong to no workspace; see materialize_cluster_tree.
        self.__scratch_dirs: list[str] = []
        # Progress manager for tracking operation status
        self.__progress_manager = None
        # Secrets to create, keyed on deployment name and then secret type.
        self._secrets_to_create: dict[str, dict[str, BaseSecret]] = {}

        # Aliases collected from all components, keyed on deployment name, source type
        # ("secret" or "direct"), service category and alias name.
        self._deployment_aliases: dict[str, dict[str, dict[str, dict[str, str]]]] = {}

        # Per deployment name the URLs, status and errors of its processing.
        self._deployment_results: dict[str, DeploymentResult] = {}

        # The subdomain registration of the deployment being rendered, to be undone when its
        # manifests fail. Set by the manifest generation, read back by its rollback.
        self._pending_subdomain_rollback: dict[str, Any] | None = None

        # Clones performed, keyed on deployment name and service type, each with a generation
        # and a timestamp. Services report here; the clone-from status is updated at the end.
        self._clones_performed: dict[str, dict[str, dict[str, Any]]] = {}

        # Runtime force_clone override from API (used by PVC manager and other nested calls)
        self._force_clone_override: bool = False
        # (project_data, compare-and-swap base) of the process_project run in progress
        self._process_run: tuple[dict[str, Any], dict[str, Any] | None] | None = None

        # Last processing error message (set when process_project fails)
        self._processing_error: str | None = None

        # Per-component failure details (set when DeploymentHealthError is caught)
        self._component_failures: list[dict[str, Any]] | None = None

        self._terminated_sync_operations: list[str] = []

        # Change context from the YAML diff: previous_yaml, current_yaml and the added,
        # changed and deleted keys under changes. Populated by process_project_changes.
        self._project_changes: dict[str, Any] | None = None

        self._closed = False

        # Service managers for handling service-specific operations
        # Import here to avoid circular dependencies
        from opi.manager.argo_manager import ArgoManager
        from opi.manager.bootstrap_manager import BootstrapManager
        from opi.manager.delete_project_manager import DeleteProjectManager
        from opi.manager.keycloak_manager import KeycloakManager
        from opi.manager.mail_manager import MailManager
        from opi.manager.minio_manager import MinioManager
        from opi.manager.pvc_manager import PVCManager
        from opi.manager.redis_manager import RedisManager
        from opi.manager.seaweedfs_manager import SeaweedfsManager

        # DatabaseManager will be lazily initialized on first access
        # This allows us to determine the correct database host based on project services
        self._database_manager: DatabaseManager | None = None
        self._minio_manager = MinioManager(self)
        self._seaweedfs_manager = SeaweedfsManager(self)
        self._keycloak_manager = KeycloakManager(self)
        self._redis_manager = RedisManager(self)
        self._mail_manager = MailManager(self)
        self._argo_manager = ArgoManager(self)
        self._bootstrap_manager = BootstrapManager(self)
        self._delete_project_manager = DeleteProjectManager(self)
        self._pvc_manager = PVCManager(self)

    async def __aenter__(self) -> ProjectManager:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None
    ) -> None:
        await self.close()

    async def _ensure_database_manager(self, skip_credential_check: bool = False) -> DatabaseManager:
        """
        Lazily initialize DatabaseManager with correct database host based on project services.

        Args:
            skip_credential_check: If True, skip checking for superuser credentials.
                                  Used during infrastructure bootstrapping when credentials
                                  don't exist yet.
        """
        if self._database_manager is not None:
            # If we have a cached manager but it was created with placeholder credentials,
            # and now we need real credentials, reinitialize it
            if not skip_credential_check and self._database_manager._admin_password == "placeholder":  # noqa: S105  # a placeholder until the real credentials are there, not a secret
                logger.info(
                    "DatabaseManager was initialized with placeholder credentials, reinitializing with real credentials"
                )
                await self._database_manager.close()
                self._database_manager = None
            else:
                return self._database_manager

        from opi.core.cluster_config import get_database_cluster_service_endpoint
        from opi.manager.database_manager import DatabaseManager
        from opi.utils.naming import generate_postgres_superuser_secret_name

        project_data = await self.get_contents(record_base=False)
        project_name = await self.get_name()

        # A dedicated (project-scoped) cluster needs the infrastructure-namespace
        # superuser; the shared instance uses the cluster admin. Both
        # namespace-postgresql-database and postgresql-database with scope: project mean
        # "dedicated" (RC-17), so route the decision through the shared helper.
        uses_namespace_db = project_uses_dedicated_postgres(project_data)

        if uses_namespace_db:
            # Dedicated-cluster database
            db_host = get_database_cluster_service_endpoint(settings.CLUSTER_MANAGER, project_name)
            infrastructure_namespace = get_infrastructure_namespace(settings.CLUSTER_MANAGER, project_name)
            secret_name = generate_postgres_superuser_secret_name(project_name)

            if skip_credential_check:
                # During infrastructure creation, credentials don't exist yet
                # Use placeholder values - they'll be replaced after infrastructure is ready
                admin_username = "postgres"
                admin_password = "placeholder"  # noqa: S105  # a placeholder until the real credentials are there, not a secret
                logger.info(
                    f"Initializing DatabaseManager for configuration only (credentials not validated): {db_host}"
                )
            else:
                # Normal operation - get credentials from Kubernetes secret
                secret_data = await self._kubectl_connector.get_secret(secret_name, infrastructure_namespace)
                if not secret_data:
                    raise RuntimeError(
                        f"Superuser secret '{secret_name}' not found in '{infrastructure_namespace}'. "
                        f"Infrastructure may not be deployed yet."
                    )

                admin_username = secret_data.get("username")
                admin_password = secret_data.get("password")
                if not admin_username or not admin_password:
                    raise RuntimeError(
                        f"Superuser secret '{secret_name}' in '{infrastructure_namespace}' has no username "
                        f"or password. Without them every database operation fails one step later, with an "
                        f"error that points at the database instead of at the secret."
                    )
                logger.info(f"Initializing DatabaseManager with namespace-specific PostgreSQL: {db_host}")
        else:
            # Shared database
            db_host = settings.DATABASE_HOST
            admin_username = settings.DATABASE_ADMIN_NAME
            admin_password = settings.DATABASE_ADMIN_PASSWORD
            logger.info(f"Initializing DatabaseManager with shared PostgreSQL: {db_host}")

        self._database_manager = DatabaseManager(
            self, db_host=db_host, admin_username=admin_username, admin_password=admin_password
        )
        return self._database_manager

    async def get_name(self) -> str:
        contents = await self.get_contents(record_base=False)
        return contents["name"]

    async def get_deployments(
        self,
        cluster_filter: bool = True,
        deployment_name: str | None = None,
        deployment_names: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get deployments with optional cluster and name filtering.

        In a Distributed Operations Manager architecture, each operations-manager
        instance manages resources only for its configured CLUSTER_MANAGER cluster.

        Args:
            cluster_filter: If True, filter by CLUSTER_MANAGER setting (default: True)
            deployment_name: If provided, filter to this single deployment
            deployment_names: If provided, filter to exactly these deployments.
                Takes precedence over ``deployment_name``. An empty list yields
                zero deployments (not "all").

        Returns:
            List of deployment configurations matching the filters

        Raises:
            ValueError: If a deployment declares a namespace that does not
                match the project name. See ``enforce_namespace_pin``.
        """
        project_data = await self.get_contents(record_base=False)

        # Tenant-isolation guard: pin every deployment namespace to the
        # project name before anything reads it. Shared with the git-monitor
        # path so a project committed directly to git cannot bypass the pin.
        enforce_namespace_pin(project_data)

        deployments = project_data.get("deployments", [])

        if cluster_filter:
            deployments = [d for d in deployments if d.get("cluster") == settings.CLUSTER_MANAGER]

        # ``is not None`` and not truthiness, so an empty target list resolves to zero
        # deployments.
        targets = resolve_deployment_filter(deployment_name, deployment_names)
        if targets is not None:
            target_set = set(targets)
            deployments = [d for d in deployments if d.get("name") in target_set]

        # A deployment that clones from another is processed after its source. Ordered here
        # because this is the one door every caller comes through, and after the filters
        # because a source that is filtered away cannot be waited for anyway.
        return order_deployments_by_clone_dependency(deployments)

    async def get_deployment_by_name(self, deployment_name: str) -> dict[str, Any] | None:
        """
        Get a specific deployment by name (respects CLUSTER_MANAGER).

        Args:
            deployment_name: Name of the deployment to find

        Returns:
            Deployment configuration or None if not found
        """
        deployments = await self.get_deployments(cluster_filter=True, deployment_name=deployment_name)
        return deployments[0] if deployments else None

    def get_deployment_results(self, deployment_name: str | None = None) -> dict[str, DeploymentResult]:
        """
        Get deployment results collected during processing.

        Results include URLs, cluster info, and status for each processed deployment.
        Call this after process_project() or process_project_from_git() to get the results.

        Args:
            deployment_name: Optional specific deployment name to filter results

        Returns:
            Dictionary mapping deployment names to DeploymentResult objects
        """
        if deployment_name:
            if deployment_name in self._deployment_results:
                return {deployment_name: self._deployment_results[deployment_name]}
            return {}
        return self._deployment_results

    def _track_public_urls(self, project_data: dict[str, Any], deployment: dict[str, Any], project_name: str) -> None:
        """Record this deployment's public addresses; see :class:`ApplicationManifests`."""
        ApplicationManifests(self).track_public_urls(project_data, deployment, project_name)

    def get_processing_error(self) -> str | None:
        """
        Get the last processing error message.

        Returns the error message from the most recent process_project() failure,
        or None if processing succeeded.
        """
        return self._processing_error

    def get_component_failures(self) -> list[dict[str, Any]] | None:
        """Get per-component failure details from the most recent deployment sync."""
        return self._component_failures

    def get_terminated_sync_operations(self) -> list[str]:
        """The deployments whose running sync operation this run broke off."""
        return self._terminated_sync_operations

    async def _queue_refresh_task(
        self, task_service: AsyncTaskService | None, project_name: str, deployment_name: str
    ) -> None:
        """Queue a refresh_deployment task via the task queue."""
        if task_service:
            await task_service.create_task(
                task_type="refresh_deployment",
                project_name=project_name,
                deployment_name=deployment_name,
                cluster=settings.CLUSTER_MANAGER,
                payload={
                    "project_name": project_name,
                    "deployment_name": deployment_name,
                    "force_clone": False,
                    # Automated retry after a disable: must not re-enable moving-tag disables.
                    "automated_remediation": True,
                },
            )
        else:
            logger.warning(
                "Task service not available, cannot queue refresh for %s/%s",
                project_name,
                deployment_name,
            )

    def get_processing_exception(self) -> Exception | None:
        """Get the original exception from the most recent processing failure."""
        return getattr(self, "_processing_exception", None)

    async def get_repositories(self) -> list[dict[str, Any]]:
        """
        Get all repositories defined in project.

        Returns:
            List of repository configurations
        """
        project_data = await self.get_contents(record_base=False)
        return project_data.get("repositories", [])

    async def get_components(self) -> list[dict[str, Any]]:
        """
        Get all components defined in project.

        Returns:
            List of component configurations
        """
        project_data = await self.get_contents(record_base=False)
        return project_data.get("components", [])

    async def get_git_connector_for_project_files(self) -> GitConnector:
        if self.__git_connector_for_project_files is None:
            # The ProjectStore's warm, long-lived working copy, instead of cloning the
            # projects repo per request. The store owns this connector, so it must never be
            # closed here: close() rmtree's the working directory.
            self.__git_connector_for_project_files = await get_project_store().get_connector()
            self.__owns_git_connector_for_project_files = False
        return self.__git_connector_for_project_files

    async def close_git_connector_for_project_files(self) -> None:
        if self.__git_connector_for_project_files and self.__owns_git_connector_for_project_files:
            await self.__git_connector_for_project_files.close()
            self.__git_connector_for_project_files = None

    def _add_secret_to_create(self, deployment_name: str, secret_type: str, secret_data: BaseSecret) -> None:
        """
        Add a secret to the private secrets map for later creation.

        Args:
            deployment_name: Name of the deployment
            secret_type: Type of secret (e.g., "database", "keycloak", "vault")
            secret_data: Secret instance (BaseSecret subclass) to store
        """
        if deployment_name not in self._secrets_to_create:
            self._secrets_to_create[deployment_name] = {}
        self._secrets_to_create[deployment_name][secret_type] = secret_data
        logger.debug(f"Added {secret_type} secret for deployment {deployment_name} to secrets map")

    def report_clone_performed(
        self,
        deployment_name: str,
        service_type: str,
        generation: int | None = None,
    ) -> None:
        """
        Report that a clone operation was performed by a service.

        Services call this method after successfully completing a clone operation.
        Project manager will use this information to update clone-from status
        after all services complete.

        Args:
            deployment_name: Name of the deployment
            service_type: Type of service (e.g., "postgresql-database", "minio-storage")
            generation: Generation number if applicable (for versioned resources)
        """
        from datetime import UTC, datetime

        if deployment_name not in self._clones_performed:
            self._clones_performed[deployment_name] = {}

        self._clones_performed[deployment_name][service_type] = {
            "generation": generation,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        logger.info(f"Clone reported: {deployment_name}/{service_type} (generation={generation})")

    def has_clones_performed(self, deployment_name: str) -> bool:
        """Check if any clones were performed for a deployment."""
        return deployment_name in self._clones_performed and len(self._clones_performed[deployment_name]) > 0

    def clear_clones_performed(self) -> None:
        """Clear the clones tracking (called at start of processing)."""
        self._clones_performed = {}

    def _get_secret_from_map(
        self, deployment_name: str, secret_type: str, secret_class: type[T] | None = None
    ) -> T | None:
        """
        Get a secret from the private secrets map with type safety.

        Args:
            deployment_name: Name of the deployment
            secret_type: Type of secret (e.g., "database", "keycloak", "vault")
            secret_class: Expected secret class type for type safety (optional)

        Returns:
            Secret instance of the specified type if found, None otherwise
        """
        secret = self._secrets_to_create.get(deployment_name, {}).get(secret_type)
        if secret is None:
            return None

        # If secret_class is provided, verify the type for runtime safety
        if secret_class is not None and not isinstance(secret, secret_class):
            raise ValueError(
                f"Secret type mismatch for {deployment_name}.{secret_type}: "
                f"expected {secret_class.__name__}, got {type(secret).__name__}"
            )

        return cast("T", secret)

    async def _rollback_subdomain_if_needed(self) -> bool:
        """Undo a pending subdomain registration; see :class:`ApplicationManifests`."""
        return await ApplicationManifests(self).rollback_subdomain_if_needed()

    async def _collect_deployment_aliases(self, deployment_name: str) -> dict[str, dict[str, dict[str, str]]]:
        """Collect the aliases of every component in a deployment, categorised."""
        return await DeploymentAliases(self).collect_deployment_aliases(deployment_name)

    async def _emit_waker_manifests(
        self,
        *,
        project_data: dict[str, Any],
        deployment: dict[str, Any],
        component_name: str,
        component_reference: str,
        unique_name: str,
        namespace: str,
        cluster: str,
        project_name: str,
        service_port: int | None,
        output_dir: str,
        created_files: list[str],
        private_key: str | None,
    ) -> None:
        """Emit the sleep-mode waker of one component; see :class:`ApplicationManifests`."""
        await ApplicationManifests(self).emit_waker_manifests(
            project_data=project_data,
            deployment=deployment,
            component_name=component_name,
            component_reference=component_reference,
            unique_name=unique_name,
            namespace=namespace,
            cluster=cluster,
            project_name=project_name,
            service_port=service_port,
            output_dir=output_dir,
            created_files=created_files,
            private_key=private_key,
        )

    async def _write_secret_file(
        self,
        spec: SecretFileSpec,
        *,
        deployment_name: str,
        namespace: str,
        output_dir: str,
        template_path: str,
        created_files: list[str],
        private_key: str | None,
    ) -> None:
        """Write one deployment SOPS secret manifest; see :class:`ApplicationManifests`."""
        await ApplicationManifests(self).write_secret_file(
            spec,
            deployment_name=deployment_name,
            namespace=namespace,
            output_dir=output_dir,
            template_path=template_path,
            created_files=created_files,
            private_key=private_key,
        )

    async def get_argo_workspace(self) -> GitRepoWorkspace:
        """The workspace on the argo-applications repo for this project, one per task."""
        if self.__argo_workspace is None:
            self.__argo_workspace = argo_workspace(await self.get_name(), settings.CLUSTER_MANAGER)
        return self.__argo_workspace

    async def close_argo_workspace(self) -> None:
        if self.__argo_workspace:
            await self.__argo_workspace.close()
            self.__argo_workspace = None

    def set_progress_manager(self, task_progress_manager: PersistentTaskProgressManager) -> None:
        """Set the task progress manager for tracking operation status."""
        self.__progress_manager = task_progress_manager

    def get_project_changes(self) -> dict[str, Any] | None:
        """Get the change context from YAML diff analysis.

        Available after process_project_changes has run. Returns the structured
        changes dict with previous_yaml, current_yaml, and changes (added/changed/deleted).
        """
        return self._project_changes

    def get_previous_deployment(self, deployment_name: str) -> dict[str, Any] | None:
        """Get a deployment's data from the previous YAML version.

        Useful for detecting what changed (e.g. root-component was X, now is Y).
        """
        if not self._project_changes:
            return None
        previous_yaml = self._project_changes.get("previous_yaml")
        if not previous_yaml:
            return None
        return Project(previous_yaml).find("deployments", name=deployment_name)

    def get_progress_manager(self) -> PersistentTaskProgressManager | None:
        """Get the task progress manager for tracking operation status."""
        return self.__progress_manager

    # ------------------------------------------------------------------
    # Progress steps
    #
    # A step is only reported when it is actually performed, so a step that
    # shows up on the page always corresponds to work that ran. Skipped work
    # gets no step at all rather than a green check it did not earn.
    # ------------------------------------------------------------------

    def _begin_step(self, name: str, subject: str | None = None) -> str | None:
        """Start a named progress step for the running task.

        Returns the step id, or None when no progress manager is attached (the
        pipeline also runs from schedulers and CLI paths that report nothing).
        """
        progress_manager = self.get_progress_manager()
        if progress_manager is None:
            return None
        return progress_manager.add_task(name, subject=subject)

    def _begin_phase(self, name: str) -> str | None:
        """Open a phase: the steps that follow land under it. See ``begin_phase``."""
        progress_manager = self.get_progress_manager()
        if progress_manager is None:
            return None
        return progress_manager.begin_phase(name)

    def _end_step(self, step_id: str | None, error: str | None = None) -> None:
        """Close a step started with :meth:`_begin_step`: completed, or failed with a reason."""
        progress_manager = self.get_progress_manager()
        if progress_manager is None or step_id is None:
            return
        if error:
            progress_manager.fail_task(step_id, error)
        else:
            progress_manager.complete_task(step_id)

    @contextmanager
    def _step(self, name: str, subject: str | None = None, retried_on: type[Exception] | None = None) -> Iterator[None]:
        """Report one step around a block of work, failing it with the raised reason.

        ``retried_on`` is an error the caller retries itself: it closes the step as
        "retried" instead of failed, so a rollout that succeeds on the next attempt shows
        no red step. The error still propagates to the retry loop.
        """
        step_id = self._begin_step(name, subject)
        try:
            yield
        except Exception as e:
            if retried_on is not None and isinstance(e, retried_on):
                progress_manager = self.get_progress_manager()
                if progress_manager is not None and step_id is not None:
                    progress_manager.update_task(step_id, steps.retried_step(name))
                self._end_step(step_id)
            else:
                self._end_step(step_id, str(e))
            raise
        self._end_step(step_id)

    async def get_deployment_workspace(self, name: str, repo_config: dict[str, str]) -> GitRepoWorkspace:
        """The workspace on this config's deployments repo, one per remote within this task.

        Two names can be the same remote: "infrastructure" is the same repo, branch and path
        as the deployments repo (``repositories[0]``). That is then also the same scope and
        the same temp directory, so that the generation of both can land in one commit.
        """
        if name in self.__deployment_workspaces:
            return self.__deployment_workspaces[name]
        if "project_name" not in repo_config:
            repo_config["project_name"] = await self.get_name()
        remote = _remote_identity(repo_config)
        workspace = self.__deployment_workspaces_by_remote.get(remote)
        if workspace is None:
            workspace = workspace_from_repo_config(
                repo_config,
                [
                    generate_project_manifest_folder_path(
                        settings.CLUSTER_MANAGER, await self.get_name(), repo_config.get("path", "")
                    )
                ],
                project_name=await self.get_name(),
            )
            self.__deployment_workspaces_by_remote[remote] = workspace
        self.__deployment_workspaces[name] = workspace
        return workspace

    def get_existing_deployment_workspace(self, name: str) -> GitRepoWorkspace | None:
        """The workspace already opened under this name, or None; opens nothing itself."""
        return self.__deployment_workspaces.get(name)

    async def materialize_cluster_tree(self, repo_config: dict[str, str]) -> str | None:
        """The ``{cluster}/`` tree of the deployments repo in a scratch directory, to read only.

        For the readers across ALL projects (``rendered_kinds``). The directory is cleaned up
        with the rest of the workspaces.
        """
        repo_path = (repo_config.get("path") or "").strip("/")
        try:
            cluster_path = generate_cluster_manifest_folder_path(settings.CLUSTER_MANAGER, repo_path)
        except RepositoryPathError as e:
            logger.error("Repositorypad '%s' valt buiten de repository, dus niet gelezen: %s", repo_path, e)
            return None

        repo = get_git_repo(repo_config["url"], repo_config.get("branch") or "main")
        credentials = credentials_from_repo_config(repo_config)
        scratch = tempfile.mkdtemp(prefix="git-cluster-", dir=settings.TEMP_DIR)
        self.__scratch_dirs.append(scratch)
        materialized = await repo.materialize([cluster_path], await repo.tip(credentials), scratch, credentials)
        if not materialized:
            return None
        return os.path.join(scratch, repo_path) if repo_path else scratch

    async def close_deployment_workspaces(self) -> None:
        for workspace in self.__deployment_workspaces_by_remote.values():
            await workspace.close()
        for scratch in self.__scratch_dirs:
            shutil.rmtree(scratch, ignore_errors=True)
        self.__scratch_dirs = []
        self.__deployment_workspaces = {}
        self.__deployment_workspaces_by_remote = {}

    async def has_deployments_for_current_cluster(self) -> bool:
        """Check if project has any deployments for the current cluster."""
        current_cluster_deployments = await self.get_deployments(cluster_filter=True)
        return bool(current_cluster_deployments)

    # get_project_full_file_path() was removed with its last caller. It handed out a
    # path into the shared warm working copy, and reading that path raced
    # ProjectStore.reconcile(), which does `reset --hard` + `clean -fd` on the same
    # directory under a lock this code never held. Change detection now reads the
    # committed object via git show, which cannot tear.

    # save_project_data() was removed deliberately. It wrote the project file straight
    # into the shared warm working copy without committing, leaving the change to be
    # picked up by whichever unrelated operation ran `git add -A` next -- or discarded by
    # a concurrent reconcile before that happened, after which the eventual commit found
    # nothing to commit and reported success anyway. Use save_and_commit_project(), which
    # writes and commits as one locked, validated operation.

    async def _validate_structural_integrity(self, project_data: dict[str, Any]) -> None:
        """Validate cross-field structural integrity of a complete project dict.

        Delegates to opi.manager.project_validation, which holds the single
        implementation so ProjectManager and ProjectStore run exactly the same
        checks on the final state. Meant to run AFTER json-schema validation and
        BEFORE any write or commit. Raises ProjectIntegrityError; fails closed.
        """
        await validate_project_structure(project_data)

    async def mark_clone_started(self, deployment_name: str) -> None:
        """Commit the clone-attempt flag on the running process_project's data (features/kloonpoging.md).

        The end-of-run save drops the flag again, so its base moves along with this save.
        """
        if self._process_run is None:
            return
        project_data, base = self._process_run
        if not self._project_file_handler.mark_clone_in_progress(project_data, deployment_name):
            return
        await self.save_and_commit_project(
            project_data,
            f"Clone attempt started for {deployment_name}",
            enforce_validation=False,
            base=base,
        )
        self._process_run = (project_data, self.__contents_as_read)

    async def commit_clone_state(self, project_data: dict[str, Any], deployment_name: str) -> None:
        """Commit the generation and revision an external clone wrote into its own read.

        The clone tasks kept them in memory only: the deployment's secret pointed at the new
        database or bucket, the project file did not, and the next run of the project pointed
        the deployment back at the old one. Inside process_project the clone works on its own
        read too, so the end-of-run save would not carry them either.
        """
        await self.save_and_commit_project(project_data, f"Clone state for {deployment_name}", enforce_validation=False)

    async def save_and_commit_project(
        self,
        project_data: dict[str, Any],
        commit_message: str,
        *,
        refresh_cache: bool = True,
        enforce_validation: bool = True,
        base: dict[str, Any] | None = None,
        approval_decision: bool = False,
    ) -> MutationResult:
        """The ONLY validated way to persist a mutated project file.

        Order (fails closed -- steps 1-2 happen before any disk write or commit):
            1. validate_project_schema       -- JSON schema, raises ProjectSchemaError
            2. _validate_structural_integrity -- refs/uniqueness/paths/domain, raises ProjectIntegrityError
            3. store.save                     -- canonical dumper, commit built from git
                                                 objects (no working-tree write) + push
            5. load_project_from_data         -- refresh read-only cache (when refresh_cache)

        The input dict must already be migrated to the latest schema -- callers
        obtain it via get_contents(), which auto-migrates on read.

        enforce_validation: when True (the default, for user-driven edits/creates),
        a schema or structural violation aborts the save. Trusted, narrow
        programmatic mutators (auto-tune, oom_watcher, backup restore, keycloak
        config) pass False: validation still runs and logs any violation at
        WARNING, but the write proceeds, so unrelated pre-existing drift cannot
        block a recovery/optimization/credential write (which would otherwise
        leave on-disk and live state out of sync). It never introduces a
        less-validated write path: the same checks run either way.

        base: the version ``project_data`` was built from, for callers that did not
        read it through this instance. The task worker is that case: it receives a
        finished project dict from a form submission, so ``get_contents()`` never ran,
        ``__contents_as_read`` is None, and the store falls back to last-writer-wins --
        silently dropping whatever landed in between. Passing the version the form was
        rendered from restores the compare-and-swap, so the store merges the two
        changes instead of overwriting one.

        approval_decision: mark this save as an approver's decision; only the approval route
        sets True, and it is the one write that may set status to ``approved`` / ``denied``.
        """
        filename = (
            os.path.basename(self._project_file_relative_path)
            if self._project_file_relative_path
            else f"{project_data.get('name')}.yaml"
        )

        # Routed through the ProjectStore so this write is serialized against every
        # other project-file writer in the process, validated on the final state,
        # rolled back cleanly on push failure, and written through to the cache.
        #
        # base is what get_contents() last handed out. It turns this into a
        # compare-and-swap: if someone else wrote in between, the store merges and
        # re-validates rather than silently publishing over their change. None when
        # the caller built project_data without reading first (create), which is the
        # one case where there is nothing to lose.
        result = await get_project_store().save(
            str(project_data.get("name")),
            project_data,
            message=commit_message,
            actor=get_actor(),
            enforce_validation=enforce_validation,
            filename=filename,
            refresh_cache=refresh_cache,
            base=base if base is not None else self.__contents_as_read,
            approval_decision=approval_decision,
        )

        # This manager now knows the current state, so a second save from the same
        # instance compares against what it just wrote instead of against the older
        # read -- which would otherwise look like a concurrent change.
        self.__contents_as_read = copy.deepcopy(project_data)
        return result

    async def mutate_and_commit_project(
        self,
        mutator: Callable[[dict[str, Any]], dict[str, Any] | None],
        commit_message: str,
        *,
        max_attempts: int = 5,
        enforce_validation: bool = True,
    ) -> bool:
        """Reload the project, apply ``mutator``, and commit+push - retrying on a git
        push conflict by re-reading the CURRENT remote state and re-applying.

        This is the concurrency-safe way to make a semantic edit (e.g. remove a
        deployment). If a competing task pushed first, we do not fail with a fatal
        "manual intervention" error: we hard-reset to the remote (dropping our stale
        local commit) and re-apply the mutation on top of what actually landed.

        ``mutator`` receives fresh project data (from get_contents, already migrated)
        and returns the mutated dict, or ``None`` to signal the change is already
        applied - an idempotent no-op that succeeds without a commit (e.g. deleting a
        deployment that another task already removed).

        Returns True if a commit was pushed, False if the mutation was already applied.

        Delegates to ProjectStore.mutate, which is the mutation-as-function
        primitive: it holds the per-repo lock for the whole read-modify-write, so
        in-process writers serialize (each waiter re-reads the previous winner's
        result) instead of racing, and only falls back to the optimistic
        fetch/re-apply/retry loop for writes that landed from outside this process.
        ``max_attempts`` is kept for caller compatibility; the store applies its
        own bounded retry budget.
        """
        name = await self.get_name()
        filename = os.path.basename(self._project_file_relative_path) if self._project_file_relative_path else None

        result = await get_project_store().mutate(
            name,
            mutator,
            message=commit_message,
            actor=get_actor(),
            enforce_validation=enforce_validation,
            filename=filename,
        )
        return result.committed

    async def check_and_create_namespaces(
        self,
        deployment_name: str | None = None,
        deployment_names: list[str] | None = None,
        known_namespace_labels: dict[str, dict[str, str]] | None = None,
    ) -> bool:
        """Create the namespaces of this project's deployments; see :class:`Infrastructure`."""
        return await Infrastructure(self).check_and_create_namespaces(
            deployment_name, deployment_names, known_namespace_labels
        )

    async def check_and_create_sops_secrets_in_namespaces(
        self, deployment_name: str | None = None, deployment_names: list[str] | None = None
    ) -> None:
        """Write the SOPS secret in every namespace of this project; see :class:`Infrastructure`."""
        await Infrastructure(self).check_and_create_sops_secrets_in_namespaces(deployment_name, deployment_names)

    async def _create_infrastructure_namespace(self, project_data: dict[str, Any], cluster_name: str) -> None:
        """Create the namespace of this project's own database; see :class:`Infrastructure`."""
        await Infrastructure(self).create_infrastructure_namespace(project_data, cluster_name)

    async def _create_infrastructure_resources(self, project_data: dict[str, Any], cluster_name: str) -> None:
        """Provision this project's own database; see :class:`Infrastructure`."""
        await Infrastructure(self).create_infrastructure_resources(project_data, cluster_name)

    def _analyze_deployment_changes(
        self, changes: dict[str, Any], current_yaml: dict[str, Any], previous_yaml: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Analyze changes specifically in the deployments section.

        If no deployment changes are detected, return all current deployments as newly created.
        This ensures that the system processes all deployments when there are no specific changes.

        Args:
            changes: The structured changes from DeepDiff analysis
            current_yaml: Current YAML content
            previous_yaml: Previous YAML content (used to name deleted deployments)

        Returns:
            Dictionary with deployment-specific changes: added, changed, deleted
        """
        deployment_changes = {"added": {}, "changed": {}, "deleted": {}}

        # Check for deployment-related changes
        has_deployment_changes = False

        def _label(path: str, yaml_doc: dict[str, Any] | None) -> str:
            name = _deployment_name_for_path(path, yaml_doc)
            return f"{path} ('{name}')" if name else path

        # Look for changes in the deployments section
        for path, value in changes["added"].items():
            if path.startswith("deployments.") or path == "deployments":
                deployment_changes["added"][path] = value
                has_deployment_changes = True
                logger.debug(f"Added deployment change: {_label(path, current_yaml)}")

        for path, value in changes["changed"].items():
            if path.startswith("deployments."):
                deployment_changes["changed"][path] = value
                has_deployment_changes = True
                logger.debug(f"Changed deployment change: {_label(path, current_yaml)}")

        for path, value in changes["deleted"].items():
            if path.startswith("deployments.") or path == "deployments":
                deployment_changes["deleted"][path] = value
                has_deployment_changes = True
                logger.debug(f"Deleted deployment change: {_label(path, previous_yaml)}")

        # If no deployment changes detected, treat all current deployments as newly created
        if not has_deployment_changes:
            logger.info("No deployment-specific changes detected - treating all deployments as newly created")
            current_deployments = current_yaml.get("deployments", [])

            if current_deployments:
                # Create a path for each deployment treating them as added
                for i, deployment in enumerate(current_deployments):
                    deployment_name = deployment.get("name", f"deployment-{i}")
                    deployment_path = f"deployments.{i}"
                    deployment_changes["added"][deployment_path] = deployment
                    logger.debug(f"Treating deployment as newly created: {deployment_name}")

                logger.info(f"Treating {len(current_deployments)} existing deployment(s) as newly created")
            else:
                logger.info("No deployments found in current project configuration")

        return deployment_changes

    def _umbrella_refresh_watcher(self, argo_connector: ArgoConnector) -> AbstractAsyncContextManager[None]:
        """Run the umbrella refresh watcher alongside a wait; see :class:`ArgoCDSync`."""
        return ArgoCDSync(self)._umbrella_refresh_watcher(argo_connector)

    async def wait_for_project_level_application(self, app_name: str) -> None:
        """Wait until the project level of this project is really synchronised."""
        await ArgoCDSync(self).wait_for_project_level_application(app_name)

    async def _render_already_proven(
        self, argo_connector: ArgoConnector, app_name: str, reconciled_at: str | None
    ) -> bool:
        """Whether this application reported Synced after our refresh, so its render held."""
        return await ArgoCDSync(self)._render_already_proven(argo_connector, app_name, reconciled_at)

    async def _read_and_validate_project_file(self, relative_project_file_path: str) -> dict[str, Any]:
        """Read the project file, validate it, and persist a schema migration if there was one.

        The order is a security gate: validation runs before every side effect, the
        auto-migration save+commit+push included. Without it a hostile project that
        happens to migrate cleanly would be written to zad-projects under our
        ops-manager identity before validation rejected it. Fails closed.
        """
        read_step = self._begin_step(steps.PROJECT_FILE_FETCH)
        try:
            git_connector_for_project_files = await self.get_git_connector_for_project_files()
            analysis = await self._project_file_handler.analyze_project_changes(
                git_connector_for_project_files, relative_project_file_path
            )
            current_yaml = analysis["current_yaml"]
        except Exception as e:
            self._end_step(read_step, str(e))
            raise
        self._end_step(read_step)

        validate_step = self._begin_step(steps.PROJECT_FILE_VALIDATE)
        try:
            validate_project_schema(current_yaml)

            # A project file that turns up here for the first time is a create, even when
            # it was committed straight to zad-projects.
            if analysis["previous_yaml"] is None and is_reserved_project_name(str(current_yaml.get("name", ""))):
                raise ProjectIntegrityError(
                    RESERVED_PROJECT_NAME_MESSAGE.format(name=current_yaml.get("name", "(onbekend)"))
                )
        except Exception as e:
            self._end_step(validate_step, str(e))
            raise
        self._end_step(validate_step)

        if self._project_file_handler.was_migrated:
            await self._commit_schema_migration(current_yaml, relative_project_file_path)
        return analysis

    async def _commit_schema_migration(self, current_yaml: dict[str, Any], fallback_name: str) -> None:
        """Write the in-memory migration to disk, as one locked write+commit.

        The step is only reported when a migration really happened: on a normal run a
        step claiming one would be a lie.
        """
        project_name = current_yaml.get("name", fallback_name)
        logger.info(f"Schema migration applied for project '{project_name}', committing changes")
        migrate_step = self._begin_step(steps.PROJECT_FILE_MIGRATE)
        try:
            await self.save_and_commit_project(
                current_yaml,
                f"auto-migrate {project_name} to schema v{current_yaml.get('schema-version', '?')}",
                enforce_validation=False,
            )
        except Exception as e:
            self._end_step(migrate_step, str(e))
            raise
        self._end_step(migrate_step)

    def _log_detected_changes(self, analysis: dict[str, Any], deployment_changes: dict[str, Any]) -> None:
        """Log what this round found changed, per section and per deployment."""
        current_yaml = analysis["current_yaml"]
        previous_yaml = analysis["previous_yaml"]
        changes = analysis["changes"]
        if previous_yaml is None:
            logger.info("Processing new project file (no previous version found)")
        else:
            logger.info(
                f"Detected changes - Added: {len(changes['added'])}, "
                f"Changed: {len(changes['changed'])}, Deleted: {len(changes['deleted'])}"
            )

        migration_note = (
            " — schema auto-migration rewrote the project file this run, "
            "a one-time diff across all deployments is expected"
            if self._project_file_handler.was_migrated
            else ""
        )
        logger.info(
            "Deployment changes - "
            + ", ".join(
                (
                    _count_deployment_changes("Added", deployment_changes["added"], current_yaml),
                    _count_deployment_changes("Changed", deployment_changes["changed"], current_yaml),
                    _count_deployment_changes("Deleted", deployment_changes["deleted"], previous_yaml),
                )
            )
            + migration_note
        )

    async def _cleanup_removed(
        self, project_name: str, analysis: dict[str, Any], deployment_changes: dict[str, Any]
    ) -> list[str]:
        """Remove what this round's project file no longer declares, before anything is created.

        Returns the failures that must stop the rollout.
        """
        current_yaml = analysis["current_yaml"]
        previous_yaml = analysis["previous_yaml"]

        failures: list[str] = []
        if previous_yaml is not None and deployment_changes["deleted"]:
            logger.info("Processing %d deleted deployment(s)", len(deployment_changes["deleted"]))
            # Only when there is something to remove; nothing to do gets no step.
            delete_step = self._begin_step(steps.deleted_deployments_cleanup(len(deployment_changes["deleted"])))
            for value in deployment_changes["deleted"].values():
                if isinstance(value, dict) and "name" in value:
                    failures.extend(await self._remove_deployment_from_edit(project_name, value, analysis))
            self._end_step(delete_step, "; ".join(failures) if failures else None)

        if previous_yaml is None:
            return failures

        try:
            svc_removal_result = await self._delete_project_manager.cleanup_removed_services_from_yaml_change(
                project_name=project_name,
                previous_yaml=previous_yaml,
                current_yaml=current_yaml,
                marked_for_deletion_service=await marking_service(),
            )
            if svc_removal_result.get("services_removed", 0) > 0:
                logger.info(
                    "Service removal cleanup: %d service(s) cleaned up",
                    svc_removal_result["services_removed"],
                )
            for err in svc_removal_result.get("errors") or []:
                logger.error("Service removal error: %s", err)
                failures.append(err)
        except Exception as e:
            logger.exception("Failed to process service removal cleanup: %s", e)
            failures.append(f"Service removal cleanup failed: {e}")
        return failures

    async def _remove_deployment_from_edit(
        self, project_name: str, deployment: dict[str, Any], analysis: dict[str, Any]
    ) -> list[str]:
        """Remove one deployment that this round's project file no longer declares.

        The result is READ: a removal that left half its resources standing used to end up
        as a green round, because only an exception reached the caller and the removal
        catches nearly everything itself (RC-484, kaartje #402).
        """
        deployment_name = deployment["name"]
        logger.info("Deleting resources for removed deployment: %s", deployment_name)
        request = DeploymentRemoval(
            project_name=project_name,
            deployment_name=deployment_name,
            trigger=RemovalTrigger.PROJECT_FILE_EDIT,
            deployment=deployment,
            project_data=analysis["current_yaml"],
            previous_project_data=analysis["previous_yaml"],
            force=True,
        )
        try:
            result = await remove_deployment(request, self._delete_project_manager)
        except Exception as e:
            logger.exception("Failed to delete resources for deployment %s: %s", deployment_name, e)
            return [f"Failed to delete removed deployment '{deployment_name}': {e}"]
        if result.success:
            return []
        return [f"Failed to delete removed deployment '{deployment_name}': {result.error_detail}"]

    async def process_project_from_git(
        self,
        relative_project_file_path: str,
        task_progress_manager: PersistentTaskProgressManager | None = None,
        deployment_name: str | None = None,
        force_clone: bool = False,
        argocd_resources_changed: bool = True,
        deployment_names: list[str] | None = None,
        force_reenable: bool = True,
        wait_for_healthy: bool = True,
        reset_running_sync: bool = False,
        rotate_credentials: frozenset[str] = frozenset(),
    ) -> bool:
        """
        Process a project file from the Git repository.

        The process follows these steps:
        0. Fetch the project file from the Git repository
        1. Execute clones if configured (BEFORE deployment)
        2. Create a Git repository for infrastructure manifests
        3. Add a secret file to the repository and commit/push it
        4. Create a namespace in the Kubernetes cluster
        5. Create an ArgoCD application and push it to the ArgoCD config repository

        Args:
            relative_project_file_path: Path to the project file within the Git repository
            task_progress_manager: Optional progress manager for tracking operation status
            deployment_name: Optional single deployment to process
            force_clone: Force clone even if target resources exist (runtime parameter)
            reset_running_sync: Break off a sync that is still running, however young.
                Comes from a refresh; see ``_clear_blocking_sync_operations``.
            deployment_names: Optional explicit set of deployments to process (takes
                precedence over ``deployment_name``). Use this to scope a redeploy to
                the deployment(s) actually affected by a change (e.g. domain approval),
                instead of reprocessing the whole project. Empty list = no deployments.
            argocd_resources_changed: Retained for caller compatibility. The
                user-applications refresh decision is now driven by ground-truth
                Application existence (a refresh happens only when a target
                Application CR is not yet present), so this flag no longer controls
                it. Redeploys of existing apps skip the expensive umbrella refresh
                regardless of this value.
            rotate_credentials: Services whose secret is replaced this round instead of
                reused (see ``opi.services.credential_rotation``)
            wait_for_healthy: Wait for each application to become Synced + Healthy.
                Pass False when the deployment this run produces cannot become Healthy
                by design (sleep-mode's waking state), so the wait would only ever end
                on the 300s time-out. The manifest push, the ArgoCD refresh and the
                render check still run, so a broken kustomization still fails fast.

        Returns:
            True if all operations were successful, False otherwise

        Note:
            This method can only be called once per ProjectManager instance.
            The finally block calls close(), which destroys git connectors and
            their temp directories. The ProjectFileHandler caches file paths
            from the first call, so a second call would reference deleted paths.
            To process multiple deployments, call this method once without
            deployment_name - process_project() handles all cluster deployments
            internally.
        """

        if self._project_file_relative_path and relative_project_file_path != self._project_file_relative_path:
            raise Exception(f"Project file path already set: {self._project_file_relative_path}")

        # Set the task progress manager if provided
        if task_progress_manager:
            self.set_progress_manager(task_progress_manager)

        # Track critical failures
        critical_failures = []

        self._project_file_relative_path = relative_project_file_path

        # Resolve the single/plural deployment filter once; threaded into both
        # process_project() and the ArgoCD sync loop below so a scoped redeploy
        # only touches the affected deployment(s).
        targets = resolve_deployment_filter(deployment_name, deployment_names)

        logger.info(f"Processing project from Git: {relative_project_file_path}")

        try:
            analysis = await self._read_and_validate_project_file(relative_project_file_path)
            current_yaml = analysis["current_yaml"]
            previous_yaml = analysis["previous_yaml"]
            changes = analysis["changes"]

            # Store change context for use throughout the processing flow
            self._project_changes = {
                "previous_yaml": previous_yaml,
                "current_yaml": current_yaml,
                "changes": changes,
            }

            logger.info("Step 1.5: Analyzing deployment changes")
            deployment_changes = self._analyze_deployment_changes(changes, current_yaml, previous_yaml)
            self._log_detected_changes(analysis, deployment_changes)

            project_name = current_yaml.get("name", "unknown")
            critical_failures.extend(await self._cleanup_removed(project_name, analysis, deployment_changes))

            # Clone operations (both local deployment and remote-source) are handled by
            # DatabaseManager and MinioManager during create_resources_for_deployment, from
            # the clone-from configuration on the deployment.
            logger.info("Step 2: Processing project with change detection")

            process_success = await self.process_project(
                force_clone=force_clone,
                deployment_names=targets,
                force_reenable=force_reenable,
                reset_running_sync=reset_running_sync,
                rotate_credentials=rotate_credentials,
            )
            if not process_success:
                # The reason, not a pointer at logs the caller has no access to (RC-66,
                # bevinding 2). process_project records why it gave up in
                # _processing_error; only when it did not is there nothing better to say
                # than that this step failed. The steps inside process_project name their
                # own failure, so there is no container step left to fail here.
                critical_failures.append(self._processing_error or "Het verwerken van het project is mislukt")

            # Check for critical failures before triggering ArgoCD sync
            # Don't sync if there were failures during processing
            if critical_failures:
                logger.error(f"Project processing completed with {len(critical_failures)} critical failures:")
                for failure in critical_failures:
                    logger.error(f"  - {failure}")
                logger.warning("Skipping ArgoCD sync due to critical failures")
                # Surface the real reason(s) so the task shows them instead of a
                # generic "Project processing failed". Keep a more specific error a
                # callee may already have recorded.
                if not self._processing_error:
                    self._processing_error = "; ".join(critical_failures)
                return False

            return await DeploymentRollout(self).run(targets, wait_for_healthy)
        except Exception as e:
            logger.exception(f"Error processing project from Git: {e}")
            self._processing_error = f"Error processing project from Git: {e}"
            return False
        finally:
            await self.close()

    def _extract_added_changes(self, project_data: dict[str, Any]) -> dict[str, Any]:
        """
        Extract added changes from project data.
        Currently returns all data as "added" - future versions will support diffs.

        Args:
            project_data: The parsed project data

        Returns:
            Dictionary containing changes marked as "added"
        """
        logger.debug("Extracting added changes (all items marked as added)")
        return project_data  # For now, treat everything as "added"

    async def _process_application_manifests(
        self,
        deployment_name: str | None = None,
        deployment_names: list[str] | None = None,
        reset_running_sync: bool = False,
    ) -> None:
        """Write the manifests of the in-scope deployments; see :class:`RepositoryManifests`."""
        await RepositoryManifests(self).process_application_manifests(
            deployment_name, deployment_names, reset_running_sync
        )

    async def _process_repository_manifests(
        self,
        repo_config: dict[str, Any],
        deployments: list[dict[str, Any]],
        reset_running_sync: bool = False,
    ) -> None:
        """Write one repository's manifests; see :class:`RepositoryManifests`."""
        await RepositoryManifests(self).process_repository_manifests(repo_config, deployments, reset_running_sync)

    async def _terminate_operation_we_waited_on(self, app_name: str, cluster: str | None) -> None:
        """Terminate the operation that OPI's own wait has just run out on."""
        await ArgoCDSync(self)._terminate_operation_we_waited_on(app_name, cluster)

    async def _clear_blocking_sync_operations(
        self, deployments: list[dict[str, Any]], reset_running_sync: bool = False
    ) -> None:
        """Terminate the sync operations blocking a next sync on these deployments."""
        await ArgoCDSync(self)._clear_blocking_sync_operations(deployments, reset_running_sync=reset_running_sync)

    async def get_repository_path(self, repository_name: str) -> str | None:
        """The configured path of a repository; see :class:`RepositoryManifests`."""
        return await RepositoryManifests(self).get_repository_path(repository_name)

    def _prune_obsolete_component_manifests(
        self, deployment: dict[str, Any], target_path: str, generated_files: list[str]
    ) -> None:
        """Delete per-component manifests this run did not write; see :class:`RepositoryManifests`."""
        RepositoryManifests(self).prune_obsolete_component_manifests(deployment, target_path, generated_files)

    async def _process_project_manifests(
        self,
        repo_config: dict[str, Any],
        cluster_name: str,
        namespace: str,
        git_connector: GitRepoWorkspace,
    ) -> None:
        """Write the project level of a deployments repository; see :class:`RepositoryManifests`."""
        await RepositoryManifests(self).process_project_manifests(repo_config, cluster_name, namespace, git_connector)

    async def _process_deployment_manifests(
        self,
        deployment: dict[str, Any],
        git_connector: GitRepoWorkspace,
    ) -> None:
        """Write one deployment's manifest directory; see :class:`RepositoryManifests`."""
        await RepositoryManifests(self).process_deployment_manifests(deployment, git_connector)

    # ==========================================================================
    # Helm Chart Processing Methods
    # ==========================================================================

    async def _process_helm_chart_deployment(
        self,
        deployment: dict[str, Any],
        git_connector: GitRepoWorkspace,
        target_path: str,
        namespace_policy_files: list[str] | None = None,
    ) -> None:
        """Render a helm-charts deployment; see :class:`HelmDeployment`."""
        await HelmDeployment(self).process_helm_chart_deployment(
            deployment, git_connector, target_path, namespace_policy_files
        )

    async def _process_helmfile_deployment(
        self,
        deployment: dict[str, Any],
        git_connector: GitRepoWorkspace,
        target_path: str,
        namespace_policy_files: list[str] | None = None,
    ) -> None:
        """Render a helmfile deployment; see :class:`HelmDeployment`."""
        await HelmDeployment(self).process_helmfile_deployment(
            deployment, git_connector, target_path, namespace_policy_files
        )

    async def close(self) -> None:
        """Clean up resources. Safe to call multiple times.

        Each cleanup step is isolated: a failure to tear down one connector
        (e.g. a temp-dir removal race) is logged but never propagated, so it
        cannot mask the outcome of the operation that ran before ``close()``.
        Callers invoke this in a ``finally``; a raising cleanup would otherwise
        turn an already-committed change (e.g. an attachment delete that was
        pushed to Git) into a reported failure.
        """
        if self._closed:
            return
        self._closed = True
        for step in (
            self.close_git_connector_for_project_files,
            self.close_argo_workspace,
            self.close_deployment_workspaces,
        ):
            try:
                await step()
            except Exception:
                logger.exception("Error during ProjectManager cleanup step %s", step.__name__)
        if self._database_manager:
            try:
                await self._database_manager.close()
            except Exception:
                logger.exception("Error closing DatabaseManager during ProjectManager cleanup")

    async def process_project(
        self,
        deployment_name: str | None = None,
        force_clone: bool = False,
        deployment_names: list[str] | None = None,
        force_reenable: bool = True,
        reset_running_sync: bool = False,
        rotate_credentials: frozenset[str] = frozenset(),
    ) -> bool:
        """
        Process the project file and create all required resources.

        Args:
            deployment_name: Optional single deployment to process
            force_clone: Force clone even if target resources exist (runtime parameter)
            deployment_names: Optional explicit set of deployments to process
                (takes precedence over ``deployment_name``; empty list = none)
            reset_running_sync: Break off a sync that is still running, however young.
                Comes from a refresh; see ``_clear_blocking_sync_operations``.
            rotate_credentials: Services whose secret is replaced this round instead of
                reused (see ``opi.services.credential_rotation``)

        Returns:
            True if all operations succeeded, False if any operation failed
        """
        logger.info(f"Processing project file: {self._project_file_relative_path}")

        # Store force_clone override for nested calls (e.g., PVC manager)
        self._force_clone_override = force_clone
        self._terminated_sync_operations = []

        targets = resolve_deployment_filter(deployment_name, deployment_names)
        # The scope as the CALLER gave it. The guard below may narrow a project-wide round,
        # but that does not make it a targeted round.
        project_wide = targets is None

        try:
            # The snapshot this run works from; its moment is what the reconciliation rows store.
            read_started = time.monotonic()
            project_data = await self.get_contents()
            project_name = await self.get_name()

            # Skip deployments with an open delete task (RC-254): their Application is out
            # of git minutes before the deployment itself is.
            being_deleted = await deployments_being_deleted(project_name)
            if being_deleted:
                in_scope = (
                    [name for dep in project_data.get("deployments", []) or [] if (name := dep.get("name"))]
                    if targets is None
                    else targets
                )
                skipped = [name for name in in_scope if name in being_deleted]
                for name in skipped:
                    logger.info(
                        "Deployment '%s' van project '%s' wordt overgeslagen: delete-taak %s staat nog open",
                        name,
                        project_name,
                        being_deleted[name],
                    )
                if skipped:
                    targets = [name for name in in_scope if name not in being_deleted]

            logger.info(
                f"Processing project: {project_name} and deployment {targets if targets is not None else 'all'}"
            )

            if not await self.has_deployments_for_current_cluster():
                # Nothing to reconcile is not a failure (RC-66, bevinding 2). A project
                # created through POST /api/v2/projects has no deployments at all yet,
                # and a project whose deployments all live on another cluster is simply
                # not this operations manager's work. Reporting False made every refresh
                # of such a project fail with "Project processing failed - check logs
                # for details", pointing at logs the caller cannot read for a state that
                # is entirely correct.
                logger.info(
                    f"Project '{project_name}' has no deployments targeting cluster '{settings.CLUSTER_MANAGER}' - this operations manager only handles deployments for this cluster"
                )
                # Without a row, a deferred change to such a project waits forever.
                await self._record_reconciliation(project_name, [], project_wide, read_started)
                self._processing_error = None
                self._component_failures = None
                return True

            # Clear stale image-pull auto-disables so a component can deploy again:
            # when the image reference changed (any edit path), or -- on an explicit
            # user (re)process (force_reenable) -- unconditionally, so a component
            # disabled by a transient registry error recovers on a user refresh even
            # on an unchanged pinned tag. Done before generating manifests so the image
            # is actually deployed. See oom_watcher for the disable side; the automated
            # post-disable refresh passes force_reenable=False so it cannot flap.
            reenabled = self._project_file_handler.reenable_components_with_changed_image(
                project_data, targets, force_reenable=force_reenable
            )
            if reenabled:
                reenable_msg = "auto-reenable: image changed for " + ", ".join(f"{d}/{c}" for d, c in reenabled)
                await self.save_and_commit_project(project_data, reenable_msg, enforce_validation=False)

            interrupted_clones = {
                name
                for deployment in project_data.get("deployments", [])
                if (name := deployment.get("name"))
                and deployment.get("cluster") == settings.CLUSTER_MANAGER
                and (targets is None or name in targets)
                and self._project_file_handler.is_clone_in_progress(project_data, name)
            }

            # Snapshot the compare-and-swap base for the end-of-run save. Right now it
            # is exactly the state project_data was built on: the read above, plus the
            # reenable save when one happened. The provisioning steps below read and
            # even save through this same manager (Keycloak realm creation persists
            # its generated admin credentials mid-run), and each of those moves the
            # recorded base forward -- past project_data's lineage.
            process_base = self.__contents_as_read
            self._process_run = (project_data, process_base)

            # 2. Extract changes (dummy for now - all marked as "added")
            self._extract_added_changes(project_data)

            self._begin_phase(steps.PHASE_CONFIGURATION)

            # Create namespaces first (always first task)
            await self.check_and_create_namespaces(deployment_names=targets)
            await self.check_and_create_sops_secrets_in_namespaces(deployment_names=targets)

            # Check if project requires an infrastructure namespace (a dedicated,
            # project-scoped PostgreSQL cluster). Both namespace-postgresql-database and
            # postgresql-database with scope: project need it (RC-17). This check is
            # infrastructure-level, independent of any manager initialization.
            needs_infrastructure_namespace = project_uses_dedicated_postgres(project_data)

            # If infrastructure is needed, provision it BEFORE initializing managers
            if needs_infrastructure_namespace:
                logger.info(
                    f"Project '{project_name}' requires infrastructure namespace - provisioning infrastructure first"
                )

                # Create infrastructure namespace and wait for Capsule label if needed
                await self._create_infrastructure_namespace(project_data, settings.CLUSTER_MANAGER)

                # Create infrastructure resources (database cluster, secrets) and wait for ready
                await self._create_infrastructure_resources(project_data, settings.CLUSTER_MANAGER)

                logger.info(
                    f"Infrastructure provisioning complete for project '{project_name}' - proceeding with applications"
                )

            # Initialize database manager (infrastructure is ready if it was needed)
            db_manager = await self._ensure_database_manager()

            # Create service resources using service managers
            deployments = project_data.get("deployments", [])

            # Filter deployments to the requested target set if provided.
            # ``is not None`` so an empty target list yields zero deployments.
            if targets is not None:
                target_set = set(targets)
                deployments = [d for d in deployments if d.get("name") in target_set]
                logger.info(f"Processing only deployment(s): {targets}")

            for deployment in deployments:
                if deployment.get("cluster") == settings.CLUSTER_MANAGER:
                    current_deployment_name = deployment.get("name")
                    # RC-5 Phase 4: dispatch provisioning through the provider registry
                    # instead of a hand-maintained sequence. Byte-identical -- the same
                    # managers, in the same db -> minio -> keycloak -> redis order, with
                    # the same args (managers keep their own self-guards).
                    provision_ctx = ProvisionContext(
                        project_data=project_data,
                        deployment=deployment,
                        force_clone=force_clone,
                        database_manager=db_manager,
                        minio_manager=self._minio_manager,
                        seaweedfs_manager=self._seaweedfs_manager,
                        keycloak_manager=self._keycloak_manager,
                        redis_manager=self._redis_manager,
                        mail_manager=self._mail_manager,
                        clone_interrupted=current_deployment_name in interrupted_clones,
                        rotate_credentials=rotate_credentials,
                    )
                    with rollout_phase(PHASE_PROVISION):
                        for provider in provisioning_services():
                            await provider.provision(provision_ctx)

            # Generate application manifests (including PVC) BEFORE setting clone status.
            # PVC clone relies on clone-from.status.completed being false to include dataSource.
            with rollout_phase(PHASE_GENERATE):
                await self._process_application_manifests(
                    deployment_names=targets, reset_running_sync=reset_running_sync
                )

            # Now update clone-from status for all deployments that had clones performed
            # (covers DB, MinIO, and PVC clones reported during manifest generation)
            for deployment in deployments:
                if deployment.get("cluster") == settings.CLUSTER_MANAGER:
                    dep_name = deployment.get("name")
                    if dep_name and self.has_clones_performed(dep_name):
                        clone_info = self._clones_performed.get(dep_name, {})
                        timestamps: list[str] = [
                            ts for info in clone_info.values() if (ts := info.get("timestamp")) is not None
                        ]
                        timestamp = max(timestamps) if timestamps else datetime.now(UTC).isoformat()

                        self._project_file_handler.set_clone_status(
                            project_data, dep_name, completed=True, timestamp=timestamp
                        )
                        logger.info(
                            f"Updated clone-from status to completed for deployment {dep_name} "
                            f"(clones: {list(self._clones_performed.get(dep_name, {}).keys())})"
                        )

            # Clear clones tracking after all deployments processed
            self.clear_clones_performed()

            # Persist the in-memory project data. The loop above set
            # clone-from.status.completed=True, and the clone itself recorded the database
            # generation -- both mutate project_data in memory only. Without this save
            # those mutations never reach the file, so every reconcile re-reads
            # completed=false / generation=None and re-clones, creating a fresh full copy
            # each pass.
            #
            # This used to be save_project_data() (a bare write into the shared warm
            # working copy) followed by commit_and_push. That pair had a silent-loss
            # window: the write sat uncommitted on disk across the awaits above, and a
            # concurrent reconcile (30s TTL, triggered by any request) does
            # `reset --hard` + `git clean -fd`, which discarded it. The subsequent
            # commit_and_push then found nothing to commit and still reported success.
            # Routing through the central save makes the write and the commit one
            # atomic, locked operation.
            #
            # It must save project_data itself: a fresh get_contents() read is a
            # deepcopy of the committed state and does not carry the in-memory
            # mutations this save exists to persist. And it must compare against
            # the base project_data was built on, not the newer one a mid-run read
            # or save recorded: against a newer base the store sees everything
            # committed since (the Keycloak credentials, an external edit) as
            # deleted by us and publishes right over it. Against the true base it
            # three-way merges our mutations with whatever landed in between.
            self.__contents_as_read = self._process_run[1]
            scope = f" (deployment: {deployment_name})" if deployment_name else ""
            with self._step(steps.PROJECT_FILE_UPDATE):
                await self.save_and_commit_project(
                    project_data,
                    f"Process project {project_name}{scope}",
                    enforce_validation=False,
                )

            # The wait for the project level happens inside, so the line names ArgoCD for
            # as long as that wait runs.
            with self._step(steps.ROLLOUT_PREPARE):
                await self._argo_manager.create_argocd_resources(deployment_names=targets)

            await self._record_reconciliation(project_name, deployments, project_wide, read_started)

            # Execute bootstrap actions for deployments
            for deployment in deployments:
                if deployment.get("cluster") == settings.CLUSTER_MANAGER:
                    await self._bootstrap_manager.execute_bootstrap_for_deployment(project_data, deployment)

            # The read cache is not touched here. save_and_commit_project above already
            # wrote through to it, from what actually landed in git -- which is the only
            # version readers may see. Registering again from in-memory state would let
            # the cache drift ahead of the repository.
            #
            # If a step between the save and this point ever starts mutating project_data
            # (the bootstrap actions above do not today), that mutation must be persisted
            # with save_and_commit_project as well, not pushed into the cache directly.

            self._processing_error = None
            self._component_failures = None
            return True
        except Exception as e:
            logger.exception(f"Error processing project: {e}")
            self._processing_error = str(e)
            self._processing_exception = e
            return False
        finally:
            self._process_run = None

    async def _record_reconciliation(
        self,
        project_name: str,
        deployments: list[dict[str, Any]],
        project_wide: bool,
        read_started: float,
    ) -> None:
        """Write what this run reconciled (RC-188).

        A failing write is logged and not raised: the rollout itself succeeded, and a
        missing row only makes the drift count over-report.
        """
        processed = [d["name"] for d in deployments if d.get("cluster") == settings.CLUSTER_MANAGER]
        failed = {
            name
            for name in processed
            if (result := self._deployment_results.get(name)) is not None and result.status == "failed"
        }
        try:
            await record_reconciliation(
                project_name,
                [name for name in processed if name not in failed],
                project_wide=project_wide and not failed,
                read_seconds_ago=time.monotonic() - read_started,
            )
        except (SQLAlchemyError, OSError, asyncpg.exceptions.PostgresError, asyncpg.exceptions.InterfaceError) as e:
            logger.warning("Could not record the reconciliation of project %s: %s", project_name, e)

    async def create_application_manifests(
        self,
        deployment: dict[str, Any],
        git_connector: GitRepoWorkspace,
        target_dir: str | None = None,
    ) -> list[str]:
        """Build one deployment's manifests; see :class:`ApplicationManifests`."""
        return await ApplicationManifests(self).create_application_manifests(deployment, git_connector, target_dir)

    async def create_kustomization_file(
        self,
        git_connector: GitRepoWorkspace,
        namespace: str,
        sops_files: list[str],
        regular_files: list[str],
        target_dir: str | None = None,
        deployment: dict[str, Any] | None = None,
    ) -> bool:
        """Write a kustomization.yaml for a manifest directory; see :class:`ApplicationManifests`."""
        return await ApplicationManifests(self).create_kustomization_file(
            git_connector, namespace, sops_files, regular_files, target_dir, deployment
        )

    async def get_contents(self, *, record_base: bool = True) -> dict[str, Any]:
        """
        Convenience method to get the contents of the project file.

        :param record_base: whether this read becomes the compare-and-swap base for a
            later save. True for a caller that is going to mutate and save what it
            gets back; False for an internal projection that reads the file, takes one
            value out of it and discards the rest.
        :return: Contents of the project file
        """
        # Read through the store, not off the working copy: the same committed state and the
        # same schema migration, without ProjectManager needing a working directory of its
        # own.
        self.__has_contents = True
        if self._project_file_relative_path is None:
            raise ValueError("Project file relative path is not set")
        data = await get_project_store().read_path(str(self._project_file_relative_path))
        if data is None:
            raise ValueError(f"Project file not found: {self._project_file_relative_path}")

        # Remembered as the compare-and-swap base for save_and_commit_project, as a copy
        # because callers mutate what they get back. record_base=False is for the projection
        # helpers, which read BETWEEN a caller's read and its save: were those to record too,
        # the base would be NEWER than project_data and the store would revert the concurrent
        # write instead of merging, with a result that validates fine.
        if record_base:
            self.__contents_as_read = copy.deepcopy(data)
        return data

    async def upsert_deployment(
        self,
        deployment_name: str,
        components: list,  # ComponentReference objects from router
        clone_from: str | None = None,
        force_clone: bool = False,
        domain_format: str | None = None,
        subdomain: str | None = None,
        base_domain: str | None = None,
        environment: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a deployment, retrying a concurrent-edit conflict."""
        return await DeploymentMutations(self).upsert_deployment(
            deployment_name,
            components,
            clone_from=clone_from,
            force_clone=force_clone,
            domain_format=domain_format,
            subdomain=subdomain,
            base_domain=base_domain,
            environment=environment,
            actor=actor,
        )

    async def add_component(
        self,
        name: str,
        image: str | None,
        deployment_names: list[str],
        component_type: str = "single",
        description: str | None = None,
        port: int | None = None,
        ports: list[int] | None = None,
        path: str = "/",
        rewrite: str | None = None,
        services: list[str] | None = None,
        cpu_limit: str | None = None,
        memory_limit: str | None = None,
        env_vars: str | None = None,
        aliases: str | None = None,
        root: bool = False,
    ) -> dict[str, Any]:
        """Add a component definition and reference it in the named deployments."""
        return await ComponentMutations(self).add_component(
            name,
            image,
            deployment_names,
            component_type,
            description,
            port,
            ports,
            path,
            rewrite,
            services,
            cpu_limit,
            memory_limit,
            env_vars,
            aliases,
            root,
        )

    async def delete_component(self, component_name: str, *, confirm_in_use: bool = False) -> dict[str, Any]:
        """Remove a component definition from the project."""
        return await ComponentMutations(self).delete_component(component_name, confirm_in_use=confirm_in_use)

    async def update_component(
        self,
        name: str,
        description: str | None = None,
        image: str | None = None,
        port: int | None = None,
        ports: list[int] | None = None,
        path: str | None = None,
        rewrite: str | None = None,
        services: list[str] | None = None,
        add_services: list[str] | None = None,
        remove_services: list[str] | None = None,
        cpu_limit: str | None = None,
        memory_limit: str | None = None,
    ) -> dict[str, Any]:
        """Update fields of an existing component (partial update)."""
        return await ComponentMutations(self).update_component(
            name,
            description,
            image,
            port,
            ports,
            path,
            rewrite,
            services,
            add_services,
            remove_services,
            cpu_limit,
            memory_limit,
        )

    async def remove_attachment(self, attachment_id: str, *, confirm_in_use: bool = False) -> dict[str, Any]:
        """Remove an attachment from the project's catalog."""
        return await AttachmentMutations(self).remove_attachment(attachment_id, confirm_in_use=confirm_in_use)

    async def upsert_attachment(
        self,
        attachment_id: str,
        filename: str | None,
        content: bytes | None,
        *,
        on_existing: str,
        on_absent: str,
        component_name: str | None = None,
        binding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Put one attachment in the project's catalog, optionally bound to a component."""
        return await AttachmentMutations(self).upsert_attachment(
            attachment_id,
            filename,
            content,
            on_existing=on_existing,
            on_absent=on_absent,
            component_name=component_name,
            binding=binding,
        )

    async def add_service(self, service_name: str, component_names: list[str] | None = None) -> dict[str, Any]:
        """Select a service on the project, and optionally bind it to components."""
        return await ServiceConfigMutations(self).add_service(service_name, component_names)

    async def configure_service(
        self,
        service_name: str,
        target: str,
        config: dict[str, Any] | list[Any],
        *,
        component_name: str | None = None,
        deployment_name: str | None = None,
    ) -> dict[str, Any]:
        """Upsert one service's config block at a target layer, then persist."""
        return await ServiceConfigMutations(self).configure_service(
            service_name, target, config, component_name=component_name, deployment_name=deployment_name
        )

    async def clear_service_config(
        self,
        service_name: str,
        target: str,
        *,
        component_name: str | None = None,
        deployment_name: str | None = None,
    ) -> dict[str, Any]:
        """Remove one service's config at a target layer (demote to bare string)."""
        return await ServiceConfigMutations(self).clear_service_config(
            service_name, target, component_name=component_name, deployment_name=deployment_name
        )

    async def patch_service_config_list(
        self,
        service_name: str,
        target: str,
        *,
        add: list[Any],
        remove: list[str],
        list_field: str | None = None,
        component_name: str | None = None,
        deployment_name: str | None = None,
    ) -> dict[str, Any]:
        """Add/update/remove items in one list of a service's config, then persist."""
        return await ServiceConfigMutations(self).patch_service_config_list(
            service_name,
            target,
            add=add,
            remove=remove,
            list_field=list_field,
            component_name=component_name,
            deployment_name=deployment_name,
        )

    async def manage_database_schemas(
        self,
        operation: str,
        postfix: str,
        *,
        description: str = "",
        forget: bool = False,
    ) -> dict[str, Any]:
        """Add or remove one extra database schema without resending the rest of the config (RC-59)."""
        return await DatabaseSchemaMutations(self).manage_database_schemas(
            operation, postfix, description=description, forget=forget
        )

    async def set_component_values(
        self,
        service_name: str,
        layer: str,
        operation: str,
        *,
        component_name: str,
        deployment_name: str | None = None,
        values: dict[str, str] | None = None,
        keys: list[str] | None = None,
    ) -> dict[str, Any]:
        """Add, patch, delete or clear the values a service owns on one component (RC-55)."""
        return await ServiceConfigMutations(self).set_component_values(
            service_name,
            layer,
            operation,
            component_name=component_name,
            deployment_name=deployment_name,
            values=values,
            keys=keys,
        )

    async def add_component_to_deployment(
        self,
        deployment_name: str,
        component_name: str,
        image: str,
    ) -> dict[str, Any]:
        """Add an existing component to a deployment that does not yet include it."""
        return await ComponentMutations(self).add_component_to_deployment(deployment_name, component_name, image)

    async def upsert_registry_by_secret(self, name: str, url: str, secret_name: str) -> dict[str, Any]:
        """Add or update a registry that references a pre-existing Kubernetes secret."""
        return await RegistryMutations(self).upsert_registry_by_secret(name, url, secret_name)

    async def upsert_registry_by_credentials(
        self, name: str, url: str, username: str | None, password: str
    ) -> dict[str, Any]:
        """Add or update a registry with username/password credentials."""
        return await RegistryMutations(self).upsert_registry_by_credentials(name, url, username, password)

    async def update_image_and_regenerate(
        self,
        deployment_name: str,
        component_name: str,
        new_image_url: str,
        service_actions: dict[str, dict[str, dict[str, dict[str, str]]]] | None = None,
        registry: str | None = None,
        rollout: bool = True,
    ) -> dict[str, Any]:
        """
        Update component image and optionally perform service-specific actions.

        IMPORTANT: This method processes the entire deployment through process_project()
        to ensure all resources (databases, keycloak, secrets, ArgoCD) are created/updated,
        not just manifests.

        Args:
            deployment_name: Name of the deployment
            component_name: Name of the component
            new_image_url: New container image URL
            service_actions: Dict with service-specific actions
                            Example: {
                                "persistent-storage": {
                                    "reference": {
                                        "data": {"action": "recreate"},
                                        "logs": {"action": "recreate"}
                                    }
                                }
                            }
            registry: Optional registry name to link to this component for imagePullSecret creation
            rollout: When False, the new image is written and committed to the project file
                     but nothing is processed or synced. The project file then runs ahead of
                     the cluster until the project is refreshed.

        Returns:
            Result dict with status, updates, and actions performed

        Raises:
            Exception: If deployment/component not found or any operation fails
        """

        project_name = await self.get_name()

        # Normalize image to lowercase (OCI spec requirement)
        new_image_url, image_was_normalized = normalize_container_image(new_image_url)

        logger.info(f"Updating image for {project_name}/{deployment_name}/{component_name} to {new_image_url}")

        # 1. Load project data
        project_data = await self.get_contents()

        # 2. Validate registry exists if provided
        if registry:
            registries = self._project_file_handler.extract_registries(project_data)
            registry_names = [r.get("name") for r in registries]
            if registry not in registry_names:
                raise ValueError(
                    f"Registry '{registry}' not found in project '{project_name}'. "
                    f"Available registries: {registry_names or 'none'}"
                )

        # 3. Find the deployment WITHIN project_data -- the exact dict we save below.
        # Using get_deployment_by_name() here was the bug: it runs its own get_contents(),
        # so it returns a SEPARATE copy of the project, and setting comp["image"] on it
        # never reached the project_data that gets committed. The store then saw no diff
        # (current == data) and committed nothing, silently dropping the image update.
        # Match get_deployments' cluster filter so we pick the same deployment it would.
        deployment = Project(project_data).find("deployments", name=deployment_name, cluster=settings.CLUSTER_MANAGER)
        if not deployment:
            # TaskInputError and not ValueError: this is no outage but a request that cannot
            # be met, so the worker makes a lasting failure of it WITH a reason the caller can
            # read. The same word the other paths here already use.
            raise TaskInputError(
                f"Deployment '{deployment_name}' not found in project '{project_name}'",
                error_type="deployment_not_found",
            )

        # 3. Find component in deployment
        component_found = False
        old_image = None
        for comp in deployment.get("components", []):
            if comp.get("reference") == component_name:
                component_found = True
                old_image = comp.get("image")
                comp["image"] = new_image_url
                if registry:
                    set_deployment_component_registry(comp, registry)
                    logger.info(f"Set registry '{registry}' on component '{component_name}'")
                break

        if not component_found:
            raise TaskInputError(
                f"Component '{component_name}' not found in deployment '{deployment_name}' of project '{project_name}'",
                error_type="component_not_found",
            )

        logger.info(f"Updated image: {old_image} -> {new_image_url}")

        # A new image replaces what ran here, so every state a service recorded about the
        # old content is cleared -- by the services themselves, through the hook. This
        # used to be one hardcoded ``if`` for image-pull disables plus a call into
        # sleep-mode, which left an OOM-disabled component switched off after its image
        # was fixed (RC-37).
        state_notices = await run_redeploy_hooks(project_name, project_data, deployment, [component_name])

        # 4. Process service actions (e.g., increment PVC generations for persistent-storage)
        generation_changes = {}
        if service_actions:
            # Handle persistent-storage service actions
            persistent_storage_actions = service_actions.get("persistent-storage", {})
            storage_refs = persistent_storage_actions.get("reference", {})

            for storage_name, storage_config in storage_refs.items():
                action = storage_config.get("action")

                if action == "recreate":
                    # Get current generation
                    current_gen = self._project_file_handler.get_storage_generation(
                        project_data, deployment_name, component_name, storage_name
                    )
                    new_gen = current_gen + 1

                    # Set new generation
                    self._project_file_handler.set_storage_generation(
                        project_data, deployment_name, component_name, storage_name, new_gen
                    )

                    generation_changes[storage_name] = {"old": current_gen, "new": new_gen}
                    logger.info(
                        f"Incremented generation for {component_name}/{storage_name}: {current_gen} -> {new_gen}"
                    )

        # 5 + 6. Validate (schema + structural), save and commit through the single path
        commit_msg = f"Update {component_name} image to {new_image_url}"
        if generation_changes:
            storage_list = ", ".join(generation_changes.keys())
            commit_msg += f" and recreate PVCs: {storage_list}"
        commit_step = self._begin_step(steps.IMAGE_SAVE)
        try:
            await self.save_and_commit_project(project_data, commit_msg)
        except Exception as e:
            self._end_step(commit_step, str(e))
            raise
        self._end_step(commit_step)
        logger.info("Saved and committed project YAML changes")

        # The caller asked for the write only: stop before anything reaches the cluster.
        if not rollout:
            logger.info(
                "Skipping processing and sync for %s/%s: rollout was deferred",
                project_name,
                deployment_name,
            )
            actions_deferred = ["image_update"]
            if generation_changes:
                actions_deferred.append("pvc_recreation")
            deferred_result: dict[str, Any] = {
                "status": "success",
                "message": f"Successfully updated {component_name} in {deployment_name} (not rolled out)",
                "updates": {
                    "image": {"old": old_image, "new": new_image_url, "normalized": image_was_normalized},
                    "storage_generations": generation_changes,
                },
                "actions_performed": actions_deferred,
            }
            if state_notices:
                deferred_result["state_cleared"] = state_notices
            return deferred_result

        # 7. CRITICAL: Process entire project for this deployment
        # This ensures all resources are created/updated (not just manifests)
        # - Namespaces, SOPS secrets
        # - Database, MinIO, Keycloak resources
        # - Application manifests (including new PVC generations)
        # - ArgoCD resources
        logger.info(f"Processing deployment {deployment_name} to apply all changes")
        process_success = await self.process_project(deployment_name)
        if not process_success:
            # The steps inside process_project name their own failure; the reason has to
            # survive for the task, which reads _processing_error.
            reason = f"Failed to process deployment {deployment_name}"
            if not self._processing_error:
                self._processing_error = reason
            raise Exception(reason)

        # 8. Trigger ArgoCD sync
        logger.info("Triggering ArgoCD sync for updated deployment")
        sync_step = self._begin_phase(steps.ROLLOUT)
        try:
            argo_connector = create_argo_connector()

            # Refresh user-applications (contains project definitions)
            await argo_connector.refresh_application("user-applications")

            # Refresh the specific deployment application
            app_name = generate_argocd_application_name(project_name, deployment_name)
            if await argo_connector.application_exists(app_name):
                logger.info(f"Refreshing ArgoCD application: {app_name}")
                await argo_connector.refresh_application(app_name)
        except Exception as e:
            self._end_step(sync_step, str(e))
            raise
        self._end_step(sync_step)

        # Schedule fire-and-forget health watcher so an image bump to a missing
        # image (ImagePullBackOff) gets auto-disabled, same as the deploy path.
        from opi.services.oom_watcher import reset_oom_tune_attempts, schedule_oom_check

        # A new image is a new workload: its memory needs are unrelated to what the
        # previous image spent, so the OOM tune budget starts fresh.
        reset_oom_tune_attempts(project_name, deployment_name)

        if settings.OOM_WATCHER_ENABLED:
            schedule_oom_check(project_name, deployment_name)

        # 9. Build and return result
        actions_performed = ["image_update"]
        if generation_changes:
            actions_performed.append("pvc_recreation")
        actions_performed.extend(
            [
                "namespace_check",
                "secrets_update",
                "service_resources_update",
                "manifest_regeneration",
                "argocd_sync",
            ]
        )

        result = {
            "status": "success",
            "message": f"Successfully updated {component_name} in {deployment_name}",
            "updates": {
                "image": {"old": old_image, "new": new_image_url, "normalized": image_was_normalized},
                "storage_generations": generation_changes,
            },
            "actions_performed": actions_performed,
        }
        # What the services cleared, so a re-enabled component is visible to whoever
        # pushed the image instead of only in the log.
        if state_notices:
            result["state_cleared"] = state_notices

        logger.info(f"Image update completed successfully: {result}")
        return result

    # Manual external cloning methods (for direct API operations)
    async def clone_database_from_external_with_tunnel(
        self,
        project_name: str,
        deployment_name: str,
        source_database: str,
        source_schema: str,
        source_username: str,
        source_password: str,
        tunnel_server_url: str,
        tunnel_username: str,
        tunnel_password: str,
        tunnel_remote_host: str,
        tunnel_remote_port: int = 5432,
        force_clone: bool = False,
    ) -> dict[str, Any]:
        """
        Clone a database from a remote source via Chisel tunnel.

        This method is for manual API operations, not project-file-based cloning. The shared
        clone function sets up the tunnel, so that the guard checks the chisel server URL
        instead of the local tunnel endpoint.
        """

        logger.info(
            f"Manual database clone via tunnel: {project_name}/{deployment_name} "
            f"<- {tunnel_remote_host}:{tunnel_remote_port} (via {tunnel_server_url})"
        )

        db_manager = await self._ensure_database_manager()
        return await db_manager.clone_database_from_external_source(
            project_name=project_name,
            deployment_name=deployment_name,
            source_host=tunnel_remote_host,
            source_port=tunnel_remote_port,
            source_username=source_username,
            source_password=source_password,
            source_database=source_database,
            source_schema=source_schema,
            force_clone=force_clone,
            chisel_config={
                "server-url": tunnel_server_url,
                "username": tunnel_username,
                "password": tunnel_password,
            },
        )

    async def clone_database_from_external_direct(
        self,
        project_name: str,
        deployment_name: str,
        source_host: str,
        source_port: int,
        source_database: str,
        source_schema: str,
        source_username: str,
        source_password: str,
        force_clone: bool = False,
    ) -> dict[str, Any]:
        """
        Clone a database from a directly accessible source (no tunnel).

        This method is for manual API operations, not project-file-based cloning.
        """
        logger.info(f"Manual direct database clone: {project_name}/{deployment_name} <- {source_host}:{source_port}")

        db_manager = await self._ensure_database_manager()
        result = await db_manager.clone_database_from_external_source(
            project_name=project_name,
            deployment_name=deployment_name,
            source_host=source_host,
            source_port=source_port,
            source_username=source_username,
            source_password=source_password,
            source_database=source_database,
            source_schema=source_schema,
            force_clone=force_clone,
        )

        result["tunnel"] = {"used": False}
        return result

    async def clone_minio_bucket_from_external_with_tunnel(
        self,
        project_name: str,
        deployment_name: str,
        source_bucket: str,
        source_access_key: str,
        source_secret_key: str,
        tunnel_server_url: str,
        tunnel_username: str,
        tunnel_password: str,
        tunnel_remote_host: str,
        tunnel_remote_port: int = 9000,
        source_secure: bool = False,
        force_clone: bool = False,
    ) -> dict[str, Any]:
        """
        Clone a MinIO bucket from a remote source via Chisel tunnel.

        This method is for manual API operations, not project-file-based cloning. The shared
        clone function sets up the tunnel, so that the guard checks the chisel server URL
        instead of the local tunnel endpoint.
        """
        logger.info(
            f"Manual MinIO clone via tunnel: {project_name}/{deployment_name} "
            f"<- {tunnel_remote_host}:{tunnel_remote_port}/{source_bucket} (via {tunnel_server_url})"
        )

        return await self._minio_manager.clone_bucket_from_external_source(
            project_name=project_name,
            deployment_name=deployment_name,
            source_host=tunnel_remote_host,
            source_port=tunnel_remote_port,
            source_access_key=source_access_key,
            source_secret_key=source_secret_key,
            source_bucket=source_bucket,
            source_secure=source_secure,
            force_clone=force_clone,
            chisel_config={
                "server-url": tunnel_server_url,
                "username": tunnel_username,
                "password": tunnel_password,
            },
        )

    async def clone_minio_bucket_from_external_direct(
        self,
        project_name: str,
        deployment_name: str,
        source_host: str,
        source_port: int,
        source_bucket: str,
        source_access_key: str,
        source_secret_key: str,
        source_secure: bool = False,
        force_clone: bool = False,
    ) -> dict[str, Any]:
        """
        Clone a MinIO bucket from a directly accessible source (no tunnel).

        This method is for manual API operations, not project-file-based cloning.
        """
        logger.info(
            f"Manual direct MinIO clone: {project_name}/{deployment_name} "
            f"<- {source_host}:{source_port}/{source_bucket}"
        )

        result = await self._minio_manager.clone_bucket_from_external_source(
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

        result["tunnel"] = {"used": False}
        return result

    # Delegation methods for project deletion - delegated to DeleteProjectManager
    async def delete_deployment(
        self, project_name: str, deployment_name: str, force: bool = False
    ) -> DeploymentRemovalResult:
        """Remove a deployment and everything that belongs to it.

        Raises:
            HTTPException: 404 when the project or the deployment is not there, 500 when the
                removal broke off. Not under ``force``, which reports in the result.
        """
        details = await self._delete_project_manager.delete_deployment(project_name, deployment_name, force=force)
        return DeploymentRemovalResult(details)

    async def delete_project(self, project_name: str, force: bool = False) -> dict[str, Any]:
        """
        Delete a project by first deleting all deployments on the current cluster.

        Args:
            project_name: Name of the project to delete
            force: If True, continues on errors and cleans up stuck resources

        Returns:
            Dictionary containing deletion results
        """
        return await self._delete_project_manager.delete_project(project_name, force=force)


def create_project_manager() -> ProjectManager:
    """
    Create and return a ProjectManager instance.

    Returns:
        ProjectManager instance
    """
    logger.debug("Creating ProjectManager")
    return ProjectManager()
