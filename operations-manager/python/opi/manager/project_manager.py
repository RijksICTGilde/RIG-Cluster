"""
The project manager handles project files. It can read, update, delete, or process them.
Processing means it can create, update, or delete any resources defined in a project file.
"""

import asyncio
import base64
import contextlib
import copy
import glob
import logging
import os
import secrets
import shutil
from collections.abc import Callable  # noqa: TC003 - used in an eagerly-evaluated annotation (no future-annotations)
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar, cast

from fastapi import HTTPException
from jsonpath_ng.ext import parse as jsonpath_parse
from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import LiteralScalarString

from opi.connectors import create_argo_connector
from opi.connectors.chisel_connector import ChiselConnector
from opi.connectors.git import (
    GitConnector,
    GitPushConflictError,
    create_git_connector_for_argocd,
    create_git_connector_for_project_files,
    create_git_connector_from_repo_config,
)
from opi.connectors.kubectl import KubectlConnector
from opi.connectors.subdomain import SubdomainConnector, ensure_domain_requests
from opi.core.cluster_config import (
    get_argo_namespace,
    get_backup_namespace,
    get_ca_certificate_config,
    get_external_dns_target_for_hostname,
    get_infrastructure_namespace,
    get_ingress_cluster_issuer,
    get_ingress_controller_selector,
    get_ingress_ip_whitelist,
    get_ingress_postfix,
    get_ingress_tls_enabled,
    get_keycloak_discovery_url,
    get_letsencrypt_contact_email,
    get_minio_host,
    get_minio_port,
    get_namespace,
    get_prefixed_namespace,
    supports_vpa,
    uses_capsule,
)
from opi.core.config import settings
from opi.core.project_schema import ProjectIntegrityError, ProjectSchemaError, validate_project_schema
from opi.extensions import load_extensions
from opi.forms.editables.enforcers import DomainConfigEnforcer, FieldWarning
from opi.generation.manifests import ManifestGenerator
from opi.handlers.project_file_handler import (
    ProjectFileHandler,
    attachment_is_referenced,
    extract_service_names_from_component,
    find_attachment_data_list,
    is_image_pull_disable_reason,
    save_project_file,
    validate_attachment_couplings,
    validate_attachment_references,
)
from opi.handlers.sops import SopsHandler
from opi.manager.revision_manager import RevisionManager
from opi.manager.run_support import resolve_image
from opi.services import ServiceAdapter, ServiceType, ServiceValidationError, VariableDefinition
from opi.services.project_service import ProjectUser, get_project_service
from opi.utils.age import (
    decrypt_age_content,
    decrypt_password_smart,
    decrypt_password_smart_auto,
    encrypt_age_content,
    get_decoded_project_private_key,
    get_project_public_key,
)
from opi.utils.env_vars import detect_circular_references, extract_variable_references, substitute_variables

# Environment variables are now generated using service definitions
from opi.utils.naming import (
    DOMAIN_FORMAT_TEMPLATES,
    ROOT_COMPONENT_FORMAT_IDS,
    HostnameFormat,
    generate_argocd_application_name,
    generate_bare_domain_hostname,
    generate_external_hostname,
    generate_helm_values_filename,
    generate_ingress_name_from_path,
    generate_issuer_manifest_name,
    generate_issuer_name,
    generate_issuer_secret_name,
    generate_manifest_name,
    generate_network_policy_manifest_name,
    generate_network_policy_name,
    generate_nice_url_root_hostname,
    generate_project_realm_name,
    generate_public_url,
    generate_pvc_name,
    generate_registry_secret_name,
    generate_storage_name,
    generate_tls_secret_name,
    generate_unique_name,
    get_component_ingress_map,
)
from opi.utils.project_utils import (
    ComponentValidationError,
    build_component_config,
    normalize_container_image,
    validate_component_paths,
    validate_root_component,
)
from opi.utils.secrets import (
    BaseSecret,
    DatabaseSecret,
    KeycloakSecret,
    MetricsAuthSecret,
    MinIOSecret,
    PlatformSecret,
    RedisSecret,
    RegistrySecret,
    UserSecret,
)
from opi.utils.sops import encrypt_to_sops_files_or_fail
from opi.utils.yaml_util import (
    find_value_by_jsonpath,
    save_yaml_to_path,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from opi.core.task_manager import TaskProgressManager
    from opi.manager.database_manager import DatabaseManager

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


def _resolve_deployment_filter(deployment_name: str | None, deployment_names: list[str] | None) -> list[str] | None:
    """Normalize the single/plural deployment-filter arguments into one list.

    Returns None when no filter is requested (process all deployments). When a
    list is returned it is the exhaustive set of deployment names to target -
    an empty list therefore means *zero* deployments, not "all". ``deployment_names``
    takes precedence over the singular ``deployment_name`` convenience argument.
    """
    if deployment_names is not None:
        return deployment_names
    if deployment_name is not None:
        return [deployment_name]
    return None


# Filename extensions used for generated per-component manifests. A file is only
# considered for pruning when it ends with one of these (covers plain manifests
# and both pre/post SOPS-encryption secret files).
_COMPONENT_MANIFEST_EXTENSIONS: tuple[str, ...] = (
    ".sops.yaml",
    ".to-sops.yaml",
    ".yaml",
    ".yml",
)


def _select_obsolete_component_manifests(
    directory: str,
    component_names: set[str],
    generated_files: set[str],
    deployment_name: str,
) -> list[str]:
    """Select per-component manifest files that this run did not (re)generate.

    Per-component manifests are named with the component reference as the filename
    prefix (e.g. ``<component>-deployment.yaml``, ``<component>-service.yaml``,
    ``<component>-platform-secret.yaml``, ``<component>-user-secret.sops.yaml``,
    ingress/pvc/configmap variants), and the authorization-wall cookie secret uses
    the deployment-scoped unique name (``<deployment>-<component>-oauth2-cookie-...``).

    A per-component file is obsolete when the current run did not generate it
    (``generated_files`` is the complete set written this run). That covers both a
    removed component (none of its files are regenerated) and a renamed/dropped file of
    a surviving component (e.g. an ingress whose path changed, so its filename changed).
    Shared/deployment-level files (kustomization, decrypt-sops, issuer-*,
    *-network-policy, keycloak/registry secrets, etc.) never start with a component
    prefix and are left alone. ``*.marked-for-deletion.yaml`` PVCs are skipped so their
    data lifecycle stays governed by the reconciliation grace period.

    Args:
        directory: Deployment manifest directory to scan (non-recursive).
        component_names: All component references owning files here (current + previous).
        generated_files: Basenames generated this run (the desired-state set).
        deployment_name: Deployment name (for the unique-name oauth2 cookie prefix).

    Returns:
        Sorted list of basenames to remove.
    """
    if not os.path.isdir(directory):
        return []

    def _owning_component(basename: str) -> str | None:
        """Return the component that a basename belongs to, longest prefix wins.

        Considers both the bare ``<component>-`` prefix and the deployment-scoped
        ``<deployment>-<component>-`` prefix used by the oauth2 cookie secret.
        """
        best: str | None = None
        best_len = -1
        for comp in component_names:
            for prefix in (f"{comp}-", f"{deployment_name}-{comp}-"):
                if basename.startswith(prefix) and len(prefix) > best_len:
                    best = comp
                    best_len = len(prefix)
        return best

    selected: list[str] = []
    for path in glob.glob(os.path.join(directory, "*")):
        if not os.path.isfile(path):
            continue
        basename = os.path.basename(path)
        if not basename.endswith(_COMPONENT_MANIFEST_EXTENSIONS):
            continue
        # PVC manifests for a removed service are renamed to *.marked-for-deletion.yaml
        # by pvc_manager.handle_service_removal so ArgoCD keeps the volume alive until
        # the reconciliation grace period (recoverable if the removal was a mistake).
        # Hard-deleting them here would prune the PVC and its data immediately.
        if basename.endswith(".marked-for-deletion.yaml"):
            continue
        # Generated this run -> part of the desired state, keep it.
        if basename in generated_files:
            continue
        # Only prune files that belong to a component; shared/deployment-level files
        # (no component prefix) are never selected.
        if _owning_component(basename) is not None:
            selected.append(basename)

    return sorted(selected)


@dataclass
class DeploymentResult:
    """Result information for a processed deployment."""

    deployment_name: str
    cluster: str
    namespace: str
    urls: dict[str, str] = field(default_factory=dict)  # component_name -> public_url
    status: str = "success"
    errors: list[str] = field(default_factory=list)


class ProjectManager:
    """Manager for project resources and deployments."""

    def __init__(
        self,
        *,
        project_file_relative_path: str | None = None,
        git_connector_for_project_files: GitConnector | None = None,
    ) -> None:
        self.__has_contents = False
        logger.debug("Initializing ProjectManager")
        self._project_file_relative_path = project_file_relative_path
        self._kubectl_connector = KubectlConnector()
        self._sops_handler = SopsHandler(self._kubectl_connector)
        self._manifest_generator = ManifestGenerator()
        self._project_file_handler = ProjectFileHandler()
        self._revision_manager = RevisionManager(self._project_file_handler)
        self.__git_connector_for_project_files = git_connector_for_project_files
        # Track ownership: if connector was injected, we don't own it and shouldn't close it
        self.__owns_git_connector_for_project_files = git_connector_for_project_files is None
        self.__git_connector_for_argocd = None
        # each deployment has a repository, referenced by name
        self.__git_connectors_for_deployments: dict[str, GitConnector] = {}
        # Progress manager for tracking operation status
        self.__progress_manager = None
        # Private map for storing secrets that need to be created
        # Structure: {deployment_name: {secret_type: secret_instance}}
        # Example: {"dev": {"database": DatabaseSecret(...), "keycloak": KeycloakSecret(...)}}
        self._secrets_to_create: dict[str, dict[str, BaseSecret]] = {}

        # Private map for storing aliases collected from all components in a deployment
        # Structure: {deployment_name: {source_type: {service_category: {alias_name: alias_template}}}}
        # Example: {"dev": {"secret": {"database": {"DATABASE_URL": "$HOST:$PORT"}}, "direct": {"web": {"PREVIEW_URL": "https://$PUBLIC_HOST"}}}}
        self._deployment_aliases: dict[str, dict[str, dict[str, dict[str, str]]]] = {}

        # Deployment results collected during processing
        # Structure: {deployment_name: DeploymentResult}
        # Contains URLs, status, and errors for each processed deployment
        self._deployment_results: dict[str, DeploymentResult] = {}

        # Track clones performed during processing
        # Structure: {deployment_name: {service_type: {"generation": int | None, "timestamp": str}}}
        # Example: {"productie": {"postgresql-database": {"generation": None, "timestamp": "..."}}}
        # Services report clones here; project_manager updates clone-from status at the end
        self._clones_performed: dict[str, dict[str, dict[str, Any]]] = {}

        # Runtime force_clone override from API (used by PVC manager and other nested calls)
        self._force_clone_override: bool = False

        # Last processing error message (set when process_project fails)
        self._processing_error: str | None = None

        # Per-component failure details (set when DeploymentHealthError is caught)
        self._component_failures: list[dict[str, Any]] | None = None

        # Change context from YAML diff (populated by process_project_changes)
        # Structure: {"previous_yaml": {...}, "current_yaml": {...}, "changes": {"added": {}, "changed": {}, "deleted": {}}}
        self._project_changes: dict[str, Any] | None = None

        self._closed = False

        # Service managers for handling service-specific operations
        # Import here to avoid circular dependencies
        # TODO: fix me, we don't want this
        from opi.manager.argo_manager import ArgoManager
        from opi.manager.bootstrap_manager import BootstrapManager
        from opi.manager.delete_project_manager import DeleteProjectManager
        from opi.manager.keycloak_manager import KeycloakManager
        from opi.manager.minio_manager import MinioManager
        from opi.manager.pvc_manager import PVCManager
        from opi.manager.redis_manager import RedisManager

        # DatabaseManager will be lazily initialized on first access
        # This allows us to determine the correct database host based on project services
        self._database_manager: DatabaseManager | None = None
        self._minio_manager = MinioManager(self)
        self._keycloak_manager = KeycloakManager(self)
        self._redis_manager = RedisManager(self)
        self._argo_manager = ArgoManager(self)
        self._bootstrap_manager = BootstrapManager(self)
        self._delete_project_manager = DeleteProjectManager(self)
        self._pvc_manager = PVCManager(self)

    async def __aenter__(self) -> ProjectManager:
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: Any) -> None:
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
            if not skip_credential_check and self._database_manager._admin_password == "placeholder":
                logger.info(
                    "DatabaseManager was initialized with placeholder credentials, reinitializing with real credentials"
                )
                await self._database_manager.close()
                self._database_manager = None
            else:
                return self._database_manager

        from opi.core.cluster_config import get_database_cluster_service_endpoint, get_infrastructure_namespace
        from opi.manager.database_manager import DatabaseManager
        from opi.services import ServiceType
        from opi.utils.naming import generate_postgres_superuser_secret_name

        project_data = await self.get_contents()
        project_name = project_data.get("name")
        project_services = project_data.get("services", [])

        # Check if project uses namespace-specific PostgreSQL
        uses_namespace_db = any(
            ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value in service
            if isinstance(service, dict)
            else service == ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value
            for service in project_services
        )

        if uses_namespace_db:
            # Namespace-specific database
            db_host = get_database_cluster_service_endpoint(settings.CLUSTER_MANAGER, project_name)
            infrastructure_namespace = get_infrastructure_namespace(settings.CLUSTER_MANAGER, project_name)
            secret_name = generate_postgres_superuser_secret_name(project_name)

            if skip_credential_check:
                # During infrastructure creation, credentials don't exist yet
                # Use placeholder values - they'll be replaced after infrastructure is ready
                admin_username = "postgres"
                admin_password = "placeholder"
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
        contents = await self.get_contents()
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
        project_data = await self.get_contents()

        # Tenant-isolation guard: pin every deployment namespace to the
        # project name before anything reads it. Shared with the git-monitor
        # path so a project committed directly to git cannot bypass the pin.
        enforce_namespace_pin(project_data)

        deployments = project_data.get("deployments", [])

        # Filter by CLUSTER_MANAGER if requested
        if cluster_filter:
            deployments = [d for d in deployments if d.get("cluster") == settings.CLUSTER_MANAGER]

        # Filter by deployment name(s) if requested. ``is not None`` (not truthiness)
        # so an empty target list correctly resolves to zero deployments.
        targets = _resolve_deployment_filter(deployment_name, deployment_names)
        if targets is not None:
            target_set = set(targets)
            deployments = [d for d in deployments if d.get("name") in target_set]

        return deployments

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

    async def _queue_refresh_task(self, task_service: Any, project_name: str, deployment_name: str) -> None:
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
        project_data = await self.get_contents()
        return project_data.get("repositories", [])

    async def get_components(self) -> list[dict[str, Any]]:
        """
        Get all components defined in project.

        Returns:
            List of component configurations
        """
        project_data = await self.get_contents()
        return project_data.get("components", [])

    async def get_git_connector_for_project_files(self) -> GitConnector:
        if self.__git_connector_for_project_files is None:
            self.__git_connector_for_project_files = await create_git_connector_for_project_files("")
            await self.__git_connector_for_project_files.ensure_repo_cloned()
        return self.__git_connector_for_project_files

    async def set_git_connector_for_project_files(self, git_connector: GitConnector) -> None:
        if self.__git_connector_for_project_files:
            raise Exception("git_connector_for_projectfiles already set")
        self.__git_connector_for_project_files = git_connector
        # Injected connector is not owned by this instance
        self.__owns_git_connector_for_project_files = False

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

    def _get_expected_secrets(
        self, deployment_name: str, deployment: dict[str, Any], project_data: dict[str, Any]
    ) -> dict[str, str]:
        """
        Determine which secrets should be referenced in deployment based on:
        - Services used (database, keycloak, minio)
        - User environment variables from components

        This is used to build the envFrom list in deployment manifests, ensuring
        all required secrets are referenced even if they're not in the _secrets_to_create map.

        Args:
            deployment_name: Name of the deployment
            deployment: Deployment configuration
            project_data: Full project configuration

        Returns:
            Dictionary mapping secret_type to secret_name
            Example: {"database": "deployment1-database", "keycloak": "deployment1-keycloak"}
        """
        expected_secrets = {}

        # Check all components in this deployment to determine which services are used
        components = deployment.get("components", [])

        # Track which services are used across all components
        uses_postgresql = False
        uses_minio = False
        uses_sso = False

        for component in components:
            component_reference = component.get("reference")
            if not component_reference:
                continue

            # Check services used by this component

            component = self._project_file_handler._find_component(project_data, component_reference)
            all_services = extract_service_names_from_component(component) if component else []

            # Check for each service type
            # Check for both postgresql-database (shared) and namespace-postgresql-database (dedicated)
            if (
                ServiceType.POSTGRESQL_DATABASE.value in all_services
                or ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value in all_services
            ):
                uses_postgresql = True

            if ServiceType.MINIO_STORAGE.value in all_services:
                uses_minio = True

            if ServiceType.KEYCLOAK.value in all_services:
                # TODO: fix this, using keycloak only is sso if the configuration is provided for it
                uses_sso = True

        # Build expected secrets based on services used
        if uses_postgresql:
            expected_secrets["database"] = DatabaseSecret.get_secret_name(deployment_name)
            logger.debug(f"Deployment {deployment_name} expects database secret")

        if uses_minio:
            expected_secrets["minio"] = MinIOSecret.get_secret_name(deployment_name)
            logger.debug(f"Deployment {deployment_name} expects MinIO secret")

        if uses_sso:
            expected_secrets["keycloak"] = KeycloakSecret.get_secret_name(deployment_name)
            logger.debug(f"Deployment {deployment_name} expects Keycloak secret")

        # Note: User secrets are per-component, not per-deployment
        # They will be added during component processing

        logger.info(f"Expected secrets for deployment {deployment_name}: {list(expected_secrets.keys())}")

        return expected_secrets

    def _get_service_category_name(self, service_type: ServiceType) -> str:
        """
        Get a consistent category name for a service type.

        This maps service types to their category names used in alias resolution.

        Args:
            service_type: The service type enum

        Returns:
            Category name string (e.g., "database", "minio", "keycloak", "web", "storage")
        """
        # Map service types to their category names. Both the shared and
        # namespace-dedicated variants of postgresql/redis collapse to the
        # same category so an alias referencing e.g. $REDIS_URL routes to the
        # same secret bucket regardless of which variant the project picked.
        # Without this, the var_to_service lookup map ends up keyed by the
        # namespace variant (later overwrites earlier in dict iteration), and
        # aliases categorise as "namespace-redis" -- a bucket no project
        # consumes, silently dropping the alias.
        category_map = {
            ServiceType.POSTGRESQL_DATABASE: "database",
            ServiceType.NAMESPACE_POSTGRESQL_DATABASE: "database",
            ServiceType.REDIS: "redis",
            ServiceType.NAMESPACE_REDIS: "redis",
            ServiceType.MINIO_STORAGE: "minio",
            ServiceType.KEYCLOAK: "keycloak",
            ServiceType.PUBLISH_ON_WEB: "web",
            ServiceType.PERSISTENT_STORAGE: "storage",
            ServiceType.TEMP_STORAGE: "storage",
            ServiceType.PLATFORM: "platform",
        }
        return category_map.get(service_type, service_type.value)

    def _categorize_alias(self, alias_name: str, alias_template: str) -> tuple[str, str]:
        """
        Determine which service and source type an alias belongs to based on the variables it references.

        Args:
            alias_name: Name of the alias
            alias_template: Template string with variable references

        Returns:
            Tuple of (service_category, source_type) where:
            - service_category: 'database', 'minio', 'keycloak', 'web', 'storage'
            - source_type: 'secret' or 'direct'

        Raises:
            ValueError: If alias references variables from multiple services or unknown variables

        Logic:
            - Dynamically checks all services defined in ServiceAdapter.SERVICE_DEFINITIONS
            - Categorizes based on which service's variables are referenced
            - Determines if variables come from secrets or are direct env vars
            - Ensures all variables in an alias come from the same service
        """
        # Extract all referenced variables
        referenced_vars = extract_variable_references(alias_template)

        if not referenced_vars:
            raise ValueError(
                f"Alias '{alias_name}' has no variable references. "
                f"Aliases must reference at least one service variable."
            )

        # Build a mapping of variable_name -> (service_type, variable_definition)
        # by checking all services dynamically
        var_to_service: dict[str, tuple[ServiceType, VariableDefinition]] = {}
        all_known_vars = set()

        for service_type in ServiceAdapter.SERVICE_DEFINITIONS:
            service_def = ServiceAdapter.get_service_definition(service_type)
            for var_def in service_def.variables:
                # Add primary variable name
                var_to_service[var_def.name] = (service_type, var_def)
                all_known_vars.add(var_def.name)
                # Add all aliases for this variable
                for alias in var_def.aliases:
                    var_to_service[alias] = (service_type, var_def)
                    all_known_vars.add(alias)

        # Check for unknown variables
        unknown_vars = [var for var in referenced_vars if var not in all_known_vars]
        if unknown_vars:
            known_vars_list = ", ".join(sorted(all_known_vars))
            raise ValueError(
                f"Alias '{alias_name}' references unknown variables: {', '.join(unknown_vars)}. "
                f"Available variables: {known_vars_list}"
            )

        # Determine which service(s) and source type(s) are referenced
        services_referenced: dict[str, ServiceType] = {}
        source_types: set[str] = set()

        for var in referenced_vars:
            service_type, var_def = var_to_service[var]
            service_category = self._get_service_category_name(service_type)
            services_referenced[service_category] = service_type
            source_types.add(var_def.source)

        # Error if multiple services referenced
        if len(services_referenced) > 1:
            raise ValueError(
                f"Alias '{alias_name}' references variables from multiple services: {', '.join(services_referenced.keys())}. "
                f"Each alias must reference variables from only one service."
            )

        # Error if multiple source types referenced (mixing secret and direct variables)
        if len(source_types) > 1:
            raise ValueError(
                f"Alias '{alias_name}' mixes variables from different sources: {', '.join(source_types)}. "
                f"Each alias must use variables from only one source type (either 'secret' or 'direct')."
            )

        # Get the single service category and source type
        service_category = next(iter(services_referenced.keys()))
        source_type = next(iter(source_types))

        logger.debug(f"Alias '{alias_name}' categorized as service='{service_category}', source='{source_type}'")
        return service_category, source_type

    async def _rollback_subdomain_if_needed(self) -> bool:
        """
        Rollback subdomain registration if one is pending.

        This is called when manifest creation fails after subdomain was registered.
        Uses the rollback info stored in self._pending_subdomain_rollback.

        Returns:
            True if rollback was performed, False otherwise
        """
        rollback_info = getattr(self, "_pending_subdomain_rollback", None)
        if not rollback_info or not rollback_info.get("should_rollback"):
            return False

        connector = rollback_info.get("connector")
        project_name = rollback_info.get("project_name")
        deployment_name = rollback_info.get("deployment_name")
        subdomain = rollback_info.get("subdomain")
        base_domain = rollback_info.get("base_domain")

        if not connector:
            logger.warning("Cannot rollback subdomain: no connector available")
            return False

        try:
            await connector.delete_by_deployment(project_name, deployment_name)
            logger.info(
                f"Rolled back subdomain '{subdomain}.{base_domain}' for {project_name}/{deployment_name} "
                f"due to deployment failure"
            )
            self._pending_subdomain_rollback = None
            return True
        except Exception as e:
            logger.error(f"Failed to rollback subdomain '{subdomain}.{base_domain}': {e}")
            return False

    async def _collect_deployment_aliases(self, deployment_name: str) -> dict[str, dict[str, dict[str, str]]]:
        """
        Scan all components in a deployment and collect aliases, categorized by source type and service.

        Args:
            deployment_name: Name of the deployment

        Returns:
            Dictionary mapping source type -> service category -> aliases:
            {
                'direct': {
                    'web': {alias_name: template, ...},
                    'storage': {alias_name: template, ...}
                },
                'secret': {
                    'database': {alias_name: template, ...},
                    'minio': {alias_name: template, ...},
                    'keycloak': {alias_name: template, ...}
                }
            }

        Raises:
            ValueError: If any alias has invalid references (multiple services, unknown variables, etc.)
        """
        logger.debug(f"Collecting aliases for deployment: {deployment_name}")

        # Get project data
        project_data = await self.get_contents()

        # Find the deployment in project data
        deployments = project_data.get("deployments", [])
        deployment = next((d for d in deployments if d.get("name") == deployment_name), None)

        if not deployment:
            logger.warning(f"Deployment '{deployment_name}' not found in project data")
            return {"direct": {}, "secret": {}}

        # Initialize categorized aliases with two-level structure: source_type -> service_category -> aliases
        categorized_aliases: dict[str, dict[str, dict[str, str]]] = {
            "direct": {},
            "secret": {},
        }

        # Scan all components
        components = deployment.get("components", [])
        for component in components:
            component_name = component["reference"]
            component_definition = await self._get_by_json_path(f"$.components[?@.name=='{component_name}']")
            if not component_definition:
                logger.warning("Component '%s' referenced in deployment but not found in project", component_name)
                continue
            component_aliases = component_definition.get("aliases", {})

            if not component_aliases:
                continue

            logger.debug(f"Found {len(component_aliases)} aliases in component '{component_name}'")

            # Categorize each alias
            for alias_name, alias_template in component_aliases.items():
                if not isinstance(alias_template, str):
                    logger.warning(
                        f"Alias '{alias_name}' in component '{component_name}' has non-string value, skipping"
                    )
                    continue

                try:
                    # Determine which service and source type this alias belongs to
                    service_category, source_type = self._categorize_alias(alias_name, alias_template)
                except ValueError as e:
                    # Add component context to the error
                    raise ValueError(
                        f"Error in component '{component_name}', deployment '{deployment_name}': {e}"
                    ) from e

                # Initialize service category dict if needed
                if service_category not in categorized_aliases[source_type]:
                    categorized_aliases[source_type][service_category] = {}

                # Add to categorized collection. Aliases are declared per component but
                # merged into one shared secret per service for the deployment, so two
                # components declaring the same alias is fine ONLY if the value matches.
                existing = categorized_aliases[source_type][service_category].get(alias_name)
                if existing is not None and existing != alias_template:
                    # Unresolvable: a single shared secret cannot hold two values.
                    raise ValueError(
                        f"Conflicting alias '{alias_name}' in deployment '{deployment_name}': "
                        f"component '{component_name}' defines a different value than another "
                        f"component. Aliases for a service are merged into one shared secret, "
                        f"so they must match."
                    )
                if existing is not None:
                    logger.debug(
                        f"Alias '{alias_name}' re-declared identically by component "
                        f"'{component_name}' in deployment '{deployment_name}' (expected for shared services)"
                    )

                categorized_aliases[source_type][service_category][alias_name] = alias_template

        # Log summary
        total_aliases = 0
        summary_parts = []
        for source_type, service_dict in categorized_aliases.items():
            for service_category, aliases in service_dict.items():
                count = len(aliases)
                if count > 0:
                    total_aliases += count
                    summary_parts.append(f"{source_type}.{service_category}: {count}")

        if total_aliases > 0:
            summary = ", ".join(summary_parts)
            logger.info(f"Collected {total_aliases} aliases for deployment '{deployment_name}' ({summary})")

        return categorized_aliases

    def _resolve_aliases(self, aliases: dict[str, str], context: dict[str, str]) -> dict[str, str]:
        """
        Resolve variable references in aliases using the provided context.

        Args:
            aliases: Dictionary of alias_name -> template
            context: Dictionary of variable_name -> value for substitution

        Returns:
            Dictionary of alias_name -> resolved_value

        Raises:
            ValueError: If circular references detected or unknown variables referenced

        Security:
            - Never logs resolved values (may contain sensitive data)
            - Only logs alias names and template patterns for debugging
        """
        if not aliases:
            return {}

        logger.debug(f"Resolving {len(aliases)} aliases with context containing {len(context)} variables")

        # Detect circular references first
        try:
            detect_circular_references(aliases)
        except ValueError as e:
            logger.error(f"Circular reference detected in aliases: {e}")
            raise

        resolved: dict[str, str] = {}

        # Resolve each alias
        for alias_name, alias_template in aliases.items():
            try:
                # Perform substitution (this validates that all referenced vars exist)
                resolved_value = substitute_variables(alias_template, context)
                resolved[alias_name] = resolved_value

                # Log without exposing the actual value
                logger.debug(f"Successfully resolved alias: {alias_name}")

            except ValueError as e:
                logger.error(f"Failed to resolve alias '{alias_name}': {e}")
                raise ValueError(f"Failed to resolve alias '{alias_name}': {e}") from e

        logger.info(f"Successfully resolved {len(resolved)} aliases")
        return resolved

    async def _get_project_keycloak_config_for_cluster(self, cluster: str) -> dict[str, Any] | None:
        """
        Find Keycloak config entry for a specific cluster.

        Args:
            cluster: Name of the cluster

        Returns:
            Keycloak config entry with host/realm/username/password or None if not found
        """

        project_data = await self.get_contents()
        keycloak_list = project_data.get("config", {}).get("keycloak", [])
        if not keycloak_list:
            return None

        project_name = await self.get_name()
        expected_realm = generate_project_realm_name(project_name, cluster)

        for entry in keycloak_list:
            if entry.get("realm") == expected_realm:
                return entry

        return None

    def _get_keycloak_url_for_cluster(self, cluster: str) -> str:
        """
        Get Keycloak URL for cluster from cluster configuration.

        Args:
            cluster: Name of the cluster

        Returns:
            Base Keycloak URL (e.g., "https://keycloak.apps.digilab.network")
        """

        discovery_url = get_keycloak_discovery_url(cluster)

        # Extract base URL from discovery URL
        if "/.well-known" in discovery_url:
            base_url = discovery_url.split("/.well-known")[0]
            # Remove /realms/xxx part to get just the host
            if "/realms/" in base_url:
                base_url = base_url.split("/realms/")[0]
            return base_url

        return discovery_url

    def _generate_storage_env_vars_from_services(self, storage_configs: list[dict[str, Any]]) -> dict[str, str]:
        """
        Generate storage environment variables using service definitions.

        Args:
            storage_configs: List of processed storage configurations

        Returns:
            Dictionary of environment variables based on service definitions
        """
        env_vars = {}

        for storage in storage_configs:
            mount_path = storage.get("mount-path")
            storage_type = storage.get("type", "persistent")

            if not mount_path:
                continue

            # Determine service type based on storage type
            if storage_type == "persistent":
                service_type = ServiceType.PERSISTENT_STORAGE
            elif storage_type == "ephemeral":
                service_type = ServiceType.TEMP_STORAGE
            else:
                raise ValueError(f"Unknown storage type: {storage_type}")

            # Generate env vars using service variable definitions
            for var_def in ServiceAdapter.get_service_definition(service_type).variables:
                if var_def.source == "direct":
                    # For storage services, the value is the mount path
                    env_vars[var_def.name] = mount_path
                    logger.debug(f"Generated storage env var: {var_def.name}={mount_path}")

        return env_vars

    def _generate_web_env_vars_from_services(self, hostname: str, use_https: bool = True) -> dict[str, str]:
        """
        Generate web environment variables using service definitions.

        Args:
            hostname: The hostname for the component
            use_https: Whether to use HTTPS protocol (based on cluster TLS config)

        Returns:
            Dictionary of environment variables based on service definitions
        """
        env_vars = {}

        # Generate env vars using service variable definitions
        for var_def in ServiceAdapter.get_service_definition(ServiceType.PUBLISH_ON_WEB).variables:
            if var_def.source == "direct" and var_def.name == "PUBLIC_HOST":
                public_url = generate_public_url(hostname, use_https)
                env_vars[var_def.name] = public_url
                logger.debug(f"Generated web env var: {var_def.name}={public_url}")
            elif var_def.source == "direct" and var_def.name == "PUBLIC_HOSTNAME":
                # Hostname only (no scheme) for apps that reject full URLs in host-name fields
                env_vars[var_def.name] = hostname
                logger.debug(f"Generated web env var: {var_def.name}={hostname}")

        return env_vars

    async def get_git_connector_for_argocd(self) -> GitConnector:
        if self.__git_connector_for_argocd is None:
            self.__git_connector_for_argocd = await create_git_connector_for_argocd(await self.get_name())
            await self.__git_connector_for_argocd.ensure_repo_cloned()
        return self.__git_connector_for_argocd

    async def set_git_connector_for_argocd(self, git_connector: GitConnector) -> None:
        if self.__git_connector_for_argocd:
            raise Exception("git_connector_for_argocd already set")
        self.__git_connector_for_argocd = git_connector

    async def close_git_connector_for_argocd(self) -> None:
        if self.__git_connector_for_argocd:
            await self.__git_connector_for_argocd.close()
            self.__git_connector_for_argocd = None

    def set_progress_manager(self, task_progress_manager: TaskProgressManager) -> None:
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
        for dep in previous_yaml.get("deployments", []):
            if isinstance(dep, dict) and dep.get("name") == deployment_name:
                return dep
        return None

    def get_progress_manager(self) -> TaskProgressManager | None:
        """Get the task progress manager for tracking operation status."""
        return self.__progress_manager

    async def set_git_connector_for_deployment(self, name: str, git_connector: GitConnector) -> None:
        if name in self.__git_connectors_for_deployments:
            raise Exception(f"git_connector_for_deployments already set for {name}")
        self.__git_connectors_for_deployments[name] = git_connector

    async def get_git_connector_for_deployment(self, name: str, repo_config: dict[str, str]) -> GitConnector:
        if name not in self.__git_connectors_for_deployments:
            if "project_name" not in repo_config:
                repo_config["project_name"] = await self.get_name()
            self.__git_connectors_for_deployments[name] = await create_git_connector_from_repo_config(repo_config)
        return self.__git_connectors_for_deployments[name]

    async def close_git_connectors_for_deployments(self) -> None:
        for name in self.__git_connectors_for_deployments:
            await self.__git_connectors_for_deployments[name].close()
        self.__git_connectors_for_deployments = {}

    # TODO: we may want to process a file anyway
    async def has_deployments_for_current_cluster(self) -> bool:
        """Check if project has any deployments for the current cluster."""
        current_cluster_deployments = await self.get_deployments(cluster_filter=True)
        return bool(current_cluster_deployments)

    async def get_project_full_file_path(self):
        if self._project_file_relative_path is None:
            raise ValueError("Project file relative path is not set")
        git_connector_for_project_files = await self.get_git_connector_for_project_files()
        git_working_dir = await git_connector_for_project_files.get_working_dir()
        return os.path.join(git_working_dir, str(self._project_file_relative_path))

    async def save_project_data(self) -> None:
        project_full_file_path = await self.get_project_full_file_path()
        save_yaml_to_path(project_full_file_path, await self.get_contents())

    async def _validate_structural_integrity(self, project_data: dict[str, Any]) -> None:
        """Validate cross-field structural integrity of a complete project dict.

        Runs the reference/uniqueness/path/root/domain checks that were previously
        scattered across add_component, add_component_to_deployment and
        upsert_deployment, against the final merged dict so they hold no matter
        which caller produced it. Meant to run AFTER json-schema validation and
        BEFORE any write or commit. Raises ProjectIntegrityError on the first
        violation; fails closed.
        """
        project_name = project_data.get("name", "(onbekend)")
        components = project_data.get("components", []) or []
        deployments = project_data.get("deployments", []) or []

        # Component names unique
        seen_components: set[str] = set()
        for comp in components:
            if not isinstance(comp, dict):
                continue
            cname = comp.get("name")
            if cname in seen_components:
                raise ProjectIntegrityError(f"Project '{project_name}': component '{cname}' is meervoudig gedefinieerd")
            seen_components.add(cname)

        project_service_names = set(
            ServiceAdapter.extract_service_names_from_project_services(project_data.get("services", []))
        )
        component_by_name = {c.get("name"): c for c in components if isinstance(c, dict)}

        # Component service references resolve to a project-level service
        for comp in components:
            if not isinstance(comp, dict):
                continue
            comp_service_names = ServiceAdapter.extract_service_names_from_project_services(comp.get("services", []))
            invalid_services = [s for s in comp_service_names if s not in project_service_names]
            if invalid_services:
                raise ProjectIntegrityError(
                    f"Project '{project_name}': component '{comp.get('name')}' verwijst naar services die niet op "
                    f"projectniveau bestaan: {invalid_services}"
                )

        # Deployment names unique
        seen_deployments: set[str] = set()
        for index, dep in enumerate(deployments):
            if not isinstance(dep, dict):
                continue
            dep_name = dep.get("name")
            if dep_name in seen_deployments:
                raise ProjectIntegrityError(
                    f"Project '{project_name}': deployment '{dep_name}' is meervoudig gedefinieerd"
                )
            seen_deployments.add(dep_name)

            refs = dep.get("components", []) or []
            domain_mode = dep.get("domain-mode", "component-specific")

            # All component references resolve to a defined component
            reference_result = self._validate_component_references(project_data, refs, "deployment")
            if not reference_result["success"]:
                raise ProjectIntegrityError(reference_result["error"])

            # Ingress path uniqueness within the deployment
            paths = []
            for ref in refs:
                ref_name = ref.get("reference") if isinstance(ref, dict) else None
                comp = component_by_name.get(ref_name)
                if comp:
                    paths.append(comp.get("path", "/"))
            try:
                validate_component_paths(paths, domain_mode)
            except ComponentValidationError as e:
                raise ProjectIntegrityError(str(e)) from e

            # Root component constraints
            root_ref = dep.get("root-component")
            if root_ref:
                ref_names = [r.get("reference") for r in refs if isinstance(r, dict) and r.get("reference")]
                try:
                    validate_root_component(root_ref, ref_names, domain_mode, dep.get("domain-format"))
                except ComponentValidationError as e:
                    raise ProjectIntegrityError(str(e)) from e

            # Hard domain-config violations. A FieldWarning (e.g. an unapproved
            # custom domain) is non-fatal: the UI handles it via domain-request
            # entries, so only a ValueError/FieldError is a structural rejection.
            try:
                await DomainConfigEnforcer(deployment_index=index).enforce(project_data, {"project_name": project_name})
            except FieldWarning:
                pass
            except ValueError as e:
                raise ProjectIntegrityError(str(e)) from e

        # Attachment references must resolve to a catalog entry, so an unknown id
        # is rejected at save time instead of failing later at deploy/resolve time.
        attachment_errors = validate_attachment_references(project_data)
        if attachment_errors:
            raise ProjectIntegrityError(f"Project '{project_name}': {'; '.join(attachment_errors)}")

        # Attachment couplings must be structurally valid: no reference coupled
        # twice, no empty delivery target, no colliding path/env-var. The base
        # component 'services' list is not covered by the JSON schema, so this is
        # the only place a duplicate reference with an empty path is rejected
        # before it can be committed.
        coupling_errors = validate_attachment_couplings(project_data)
        if coupling_errors:
            raise ProjectIntegrityError(f"Project '{project_name}': {'; '.join(coupling_errors)}")

    async def save_and_commit_project(
        self,
        project_data: dict[str, Any],
        commit_message: str,
        *,
        refresh_cache: bool = True,
        enforce_validation: bool = True,
    ) -> None:
        """The ONLY validated way to persist a mutated project file.

        Order (fails closed -- steps 1-2 happen before any disk write or commit):
            1. validate_project_schema       -- JSON schema, raises ProjectSchemaError
            2. _validate_structural_integrity -- refs/uniqueness/paths/domain, raises ProjectIntegrityError
            3. save_yaml_to_path              -- canonical dumper only
            4. commit_and_push                -- single commit + push
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
        """
        try:
            validate_project_schema(project_data)
            await self._validate_structural_integrity(project_data)
        except (ProjectSchemaError, ProjectIntegrityError) as e:
            if enforce_validation:
                raise
            logger.warning(
                "Persisting project '%s' despite a validation failure (enforce_validation=False); "
                "the project file has pre-existing drift that should be repaired: %s",
                project_data.get("name", "(onbekend)"),
                e,
            )

        project_full_file_path = await self.get_project_full_file_path()
        save_yaml_to_path(project_full_file_path, project_data)

        git_connector = await self.get_git_connector_for_project_files()
        await git_connector.commit_and_push(commit_message)

        if refresh_cache:
            filename = (
                os.path.basename(self._project_file_relative_path)
                if self._project_file_relative_path
                else f"{project_data.get('name')}.yaml"
            )
            get_project_service().load_project_from_data(project_data, filename)

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
        """
        for attempt in range(max_attempts):
            if attempt > 0:
                # Drop our failed local commit and re-sync to the winner's state.
                git_connector = await self.get_git_connector_for_project_files()
                await git_connector.reset_to_remote()

            current = await self.get_contents()
            mutated = mutator(current)
            if mutated is None:
                logger.info("Project mutation already applied, nothing to commit: %s", commit_message)
                return False

            try:
                await self.save_and_commit_project(mutated, commit_message, enforce_validation=enforce_validation)
                return True
            except GitPushConflictError:
                if attempt < max_attempts - 1:
                    logger.warning(
                        "Push conflict on '%s' (attempt %d/%d); re-reading remote and re-applying",
                        commit_message,
                        attempt + 1,
                        max_attempts,
                    )
                    continue
                raise

        return False

    async def check_and_create_namespaces(
        self, deployment_name: str | None = None, deployment_names: list[str] | None = None
    ) -> bool:
        """
        Check and create namespaces for all deployments in the project for this cluster.

        Args:
            deployment_name: Optional single deployment to process
            deployment_names: Optional explicit set of deployments to process
                (takes precedence over ``deployment_name``)

        Returns:
            True if all namespaces were checked/created successfully
        """

        await self.get_project_full_file_path()

        project_data: dict[str, str | list | dict[str, str]] = await self.get_contents()
        logger.info(f"Checking namespaces for project: {project_data['name']}")

        # Track namespace creation with progress manager if available
        progress_manager = self.get_progress_manager()
        namespace_subtask = None
        if progress_manager:
            namespace_subtask = progress_manager.add_task("Kubernetes namespace(s) aanmaken")

        # Get deployments for THIS cluster using helper method
        deployments = await self.get_deployments(
            cluster_filter=True, deployment_name=deployment_name, deployment_names=deployment_names
        )

        targets = _resolve_deployment_filter(deployment_name, deployment_names)
        if targets is not None:
            logger.info(f"Checking namespaces only for deployment(s): {targets}")

        if not deployments:
            logger.info(f"No deployments found in project {project_data['name']}")
            return True

        all_successful = True

        for deployment in deployments:
            namespace = get_prefixed_namespace(settings.CLUSTER_MANAGER, cast("str", deployment["namespace"]))

            logger.info(
                f"Checking namespace '{namespace}' for deployment '{deployment['name']}' for project '{project_data['name']}':"
            )
            # Check if namespace exists
            namespace_exists = await self._kubectl_connector.namespace_exists(namespace)
            if namespace_exists:
                logger.info(
                    f"Namespace '{namespace}' already exists for deployment '{deployment['name']}' for project '{project_data['name']}'"
                )
            else:
                logger.info(
                    f"Creating namespace '{namespace}' for deployment '{deployment['name']}' for project '{project_data['name']}':"
                )
                # Create the namespace using shared function
                await self._create_namespace_with_argocd_label(namespace)

            # Always ensure ArgoCD managed-by label exists (idempotent)
            await self._ensure_argocd_managed_by_label(namespace)

            if progress_manager:
                progress_manager.set_namespace(namespace)

        # Complete namespace subtask if progress manager is available
        if progress_manager and namespace_subtask:
            if all_successful:
                progress_manager.complete_task(namespace_subtask)
            else:
                progress_manager.fail_task(namespace_subtask, "Failed to create one or more namespaces")

        return all_successful

    async def _create_namespace_with_argocd_label(self, namespace: str) -> None:
        """
        Create a Kubernetes namespace with ArgoCD managed-by label.

        This is the shared implementation used by both deployment namespaces
        and infrastructure namespaces.

        Args:
            namespace: Full namespace name (with cluster prefix already applied)

        Raises:
            RuntimeError: If namespace creation fails
        """
        logger.info(f"Creating namespace '{namespace}'")

        # Create the namespace using the manifest template
        manifest_path = os.path.join(settings.MANIFESTS_PATH, "namespace.yaml.jinja")

        # Template variables
        variables = {"namespace": namespace, "manager": get_argo_namespace(settings.CLUSTER_MANAGER)}

        await self._kubectl_connector.apply_manifest(manifest_path, variables)

        # If Capsule is enabled, wait for Capsule to assign the tenant label
        # before attempting to modify the namespace with additional labels
        if uses_capsule(settings.CLUSTER_MANAGER):
            logger.info(f"Cluster uses Capsule, waiting for tenant label assignment on namespace '{namespace}'")
            capsule_ready = await self._kubectl_connector.wait_for_capsule_tenant_label(namespace, timeout=30)

            if not capsule_ready:
                raise RuntimeError(
                    f"Timeout waiting for Capsule to assign tenant label to namespace '{namespace}'. "
                    "Cannot proceed with namespace configuration."
                )

        # Apply the argocd.argoproj.io/managed-by label after creating the namespace
        manager_value = get_argo_namespace(settings.CLUSTER_MANAGER)
        label_result = await self._kubectl_connector.apply_label_to_resource(
            resource_type="namespace",
            resource_name=namespace,
            label_key="argocd.argoproj.io/managed-by",
            label_value=manager_value,
        )

        if not label_result:
            raise RuntimeError(
                f"Failed to apply ArgoCD managed-by label to namespace '{namespace}'. "
                "ArgoCD will not be able to manage resources in this namespace."
            )

        logger.info(f"Successfully created namespace '{namespace}' with ArgoCD managed-by label")

    async def _ensure_argocd_managed_by_label(self, namespace: str) -> None:
        """
        Ensure the ArgoCD managed-by label exists on a namespace (idempotent).

        This applies the label regardless of whether it already exists,
        ensuring namespaces are always properly configured for ArgoCD management.

        Args:
            namespace: Full namespace name (with cluster prefix already applied)

        Raises:
            RuntimeError: If the label cannot be applied
        """
        manager_value = get_argo_namespace(settings.CLUSTER_MANAGER)
        label_result = await self._kubectl_connector.apply_label_to_resource(
            resource_type="namespace",
            resource_name=namespace,
            label_key="argocd.argoproj.io/managed-by",
            label_value=manager_value,
        )
        if label_result:
            logger.info(f"Ensured ArgoCD managed-by label on namespace: {namespace}")
        else:
            raise RuntimeError(
                f"Failed to apply ArgoCD managed-by label to namespace '{namespace}'. "
                "ArgoCD will not be able to manage resources in this namespace."
            )

    async def _ensure_sops_secret_in_namespace(self, namespace: str, project_data: dict[str, Any]) -> None:
        """
        Ensure SOPS secret exists in the given namespace.

        Creates or updates the SOPS secret as needed. This is the shared implementation
        used by both deployment namespaces and infrastructure namespaces.

        Args:
            namespace: Full namespace name (with cluster prefix already applied)
            project_data: Project configuration containing SOPS keys

        Raises:
            RuntimeError: If SOPS secret creation fails
        """
        project_name = project_data.get("name", "unknown")
        logger.info(f"Checking SOPS secret for project {project_name} in namespace {namespace}")

        public_key = get_project_public_key(project_data)
        private_key = await get_decoded_project_private_key(project_data)

        existing_secret = await self._kubectl_connector.get_sops_secret_from_namespace(namespace)

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
                await self._kubectl_connector.delete_resource("secret", "sops-age-key", namespace)
                logger.info(f"Deleted old SOPS secret from namespace {namespace}")
            except Exception as e:
                logger.warning(f"Failed to delete old SOPS secret (continuing anyway): {e}")

        if create_sops_secret:
            max_retries = 10
            for attempt in range(1, max_retries + 1):
                result = await self._sops_handler.store_project_sops_key_in_namespace(
                    namespace, private_key, public_key
                )
                if result:
                    logger.info(f"Created new SOPS secret for project {project_name} in namespace {namespace}")
                    break
                if attempt < max_retries:
                    logger.warning(
                        f"Failed to create SOPS secret in namespace {namespace} "
                        f"(attempt {attempt}/{max_retries}), retrying in 1s (RBAC may still be propagating)"
                    )
                    await asyncio.sleep(1)
                else:
                    raise RuntimeError(
                        f"Failed to create SOPS secret in namespace {namespace} after {max_retries} attempts. "
                        "RBAC permissions may not have propagated for this namespace."
                    )
        else:
            logger.info(f"Found existing SOPS secret for project {project_name} in namespace {namespace}")

    @staticmethod
    async def _test_infrastructure_database_connection(username: str, password: str, host: str) -> tuple[bool, str]:
        """
        Test if infrastructure database credentials are valid by attempting a direct connection.

        This method creates its own connection since it needs to test with the superuser credentials
        before the infrastructure is fully set up.

        Args:
            username: Database username to test (typically 'postgres')
            password: Database password to test
            host: Database host (namespace-specific service endpoint)

        Returns:
            Tuple of (success: bool, error_type: str) where error_type is:
            - "success" if connection successful
            - "auth_error" if authentication failed (wrong password)
            - "connection_error" if database not reachable (not running yet)
            - "unknown_error" for other errors
        """
        import socket

        import asyncpg

        try:
            # Create a direct connection with the superuser credentials to the postgres database
            conn = await asyncpg.connect(
                host=host, port=5432, user=username, password=password, database="postgres", timeout=5
            )
            await conn.close()
            logger.debug(f"Infrastructure database connection test successful for {username}@{host}/postgres")
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

    async def _create_infrastructure_namespace(self, project_data: dict[str, Any], cluster_name: str) -> None:
        """
        Create infrastructure namespace for namespace-specific PostgreSQL databases.

        This namespace hosts:
        - CloudNativePG database cluster
        - Superuser credentials secret
        - Database-related infrastructure resources

        Following the same pattern as regular deployment namespaces:
        - Creates namespace with ArgoCD managed-by label
        - Creates SOPS secret for secret decryption

        Args:
            project_data: The project configuration data
            cluster_name: Name of the cluster

        Raises:
            RuntimeError: If namespace creation fails
        """
        from opi.core.cluster_config import get_infrastructure_namespace

        project_name = project_data.get("name")
        if not project_name:
            raise ValueError("Project name is required in project_data")

        # Get infrastructure namespace with cluster-specific prefix
        infrastructure_namespace = get_infrastructure_namespace(cluster_name, project_name)

        logger.info(f"Checking infrastructure namespace '{infrastructure_namespace}' for project '{project_name}'")

        # Track namespace creation with progress manager if available
        progress_manager = self.get_progress_manager()
        namespace_subtask = None
        if progress_manager:
            namespace_subtask = progress_manager.add_task(
                f"Creating infrastructure namespace {infrastructure_namespace}"
            )

        try:
            # Check if namespace exists, create if needed (using shared function)
            namespace_exists = await self._kubectl_connector.namespace_exists(infrastructure_namespace)
            if not namespace_exists:
                await self._create_namespace_with_argocd_label(infrastructure_namespace)
            else:
                logger.info(
                    f"Infrastructure namespace '{infrastructure_namespace}' already exists for project '{project_name}'"
                )

            # Always ensure ArgoCD managed-by label exists (idempotent)
            await self._ensure_argocd_managed_by_label(infrastructure_namespace)

            # Create SOPS secret in the infrastructure namespace (using shared function)
            await self._ensure_sops_secret_in_namespace(infrastructure_namespace, project_data)

            if progress_manager and namespace_subtask:
                progress_manager.complete_task(namespace_subtask)

        except Exception as e:
            logger.error(f"Failed to create infrastructure namespace '{infrastructure_namespace}': {e}")
            if progress_manager and namespace_subtask:
                progress_manager.fail_task(namespace_subtask, f"Failed to create infrastructure namespace: {e}")
            raise RuntimeError(f"Cannot create infrastructure namespace '{infrastructure_namespace}': {e}") from e

    async def _create_infrastructure_resources(self, project_data: dict[str, Any], cluster_name: str) -> None:
        """
        Create and deploy infrastructure resources for namespace-specific PostgreSQL databases.

        This method orchestrates the complete infrastructure provisioning workflow:
        1. Generate database superuser credentials secret
        2. Generate CloudNativePG cluster manifest
        3. Generate infrastructure kustomization.yaml
        4. Commit manifests to deployment Git repository
        5. Create ArgoCD infrastructure application (with sync-wave 0)
        6. Refresh user-applications to detect new infrastructure application
        7. Wait for infrastructure application to be created by ArgoCD
        8. Refresh the infrastructure application to pick up latest changes
        9. Wait for ArgoCD to report infrastructure as Synced + Healthy

        Args:
            project_data: The project configuration data
            cluster_name: Name of the cluster

        Raises:
            RuntimeError: If infrastructure provisioning fails
            TimeoutError: If infrastructure doesn't become ready within timeout
        """
        from opi.core.cluster_config import get_infrastructure_namespace, get_storage_class_name
        from opi.generation.manifests import render_template
        from opi.utils.naming import _sanitize_for_lowercase
        from opi.utils.passwords import generate_secure_password
        from opi.utils.sops import encrypt_to_sops_files_or_fail

        project_name = project_data.get("name")
        if not project_name:
            raise ValueError("Project name is required in project_data")

        # Get configuration
        infrastructure_namespace = get_infrastructure_namespace(cluster_name, project_name)
        # Initialize database manager for configuration only (skip credential check during bootstrap)
        db_manager = await self._ensure_database_manager(skip_credential_check=True)
        database_cluster_config = db_manager._get_database_cluster_config(project_data, cluster_name)
        storage_class = get_storage_class_name(cluster_name)
        project_clean = _sanitize_for_lowercase(project_name)

        logger.info(f"Creating infrastructure resources for project '{project_name}' in cluster '{cluster_name}'")

        # Track infrastructure creation with progress manager
        progress_manager = self.get_progress_manager()
        infra_task = None
        if progress_manager:
            infra_task = progress_manager.add_task("Creating infrastructure resources (database cluster)")

        try:
            # STEP 1: Check for existing superuser credentials and validate them
            from opi.core.cluster_config import get_database_cluster_service_endpoint
            from opi.utils.naming import generate_postgres_superuser_secret_name

            superuser_secret_name = generate_postgres_superuser_secret_name(project_name)
            existing_secret = await self._kubectl_connector.get_secret(superuser_secret_name, infrastructure_namespace)

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
                credentials_valid, error_type = await self._test_infrastructure_database_connection(
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
                registries = self._project_file_handler.extract_registries(project_data)
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
                    from opi.utils.naming import generate_registry_secret_name

                    registry_secret_name = generate_registry_secret_name("infrastructure", registry_name)
                    logger.info(
                        f"PostgreSQL will use imagePullSecret '{registry_secret_name}' for image '{database_image}'"
                    )

                # Map the database image to its registry secret
                image_pull_secrets_map[database_image] = registry_secret_name

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

            # Get git connector for deployment repo
            deployment_git_connector = await self.get_git_connector_for_deployment("infrastructure", infra_repo_config)
            deployment_working_dir = await deployment_git_connector.get_working_dir()

            # Create infrastructure resources directory in deployment repo
            # Path: {cluster}/{project_name}/infrastructure/
            # This contains the actual Kubernetes resources (PostgreSQL cluster, secrets)
            repo_path = infra_repo_config.get("path", "")
            if repo_path:
                infra_resources_dir = os.path.join(
                    deployment_working_dir, repo_path, cluster_name, project_name, "infrastructure"
                )
            else:
                infra_resources_dir = os.path.join(deployment_working_dir, cluster_name, project_name, "infrastructure")
            os.makedirs(infra_resources_dir, exist_ok=True)

            # Write manifests - secret as .to-sops.yaml for encryption
            secret_path = os.path.join(infra_resources_dir, f"{project_clean}-postgres-superuser-secret.to-sops.yaml")
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
                from opi.utils.secrets import RegistrySecret

                registry_url = registry_config.get("url", "")
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
                registry_secret_path = os.path.join(infra_resources_dir, f"{registry_secret_name}.to-sops.yaml")
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
            encrypt_to_sops_files_or_fail(
                infra_resources_dir,
                project_public_key,
                f"infrastructuur-secrets voor project '{project_name}' in cluster '{cluster_name}'",
                private_key=await self._sops_private_key_for(project_data),
            )

            # STEP 5: Generate kustomization.yaml and decrypt-sops.yaml for infrastructure resources
            logger.info(f"Generating infrastructure kustomization for project '{project_name}'")
            kustomization_success = self._manifest_generator.create_kustomization_files(
                output_dir=infra_resources_dir,
                namespace=infrastructure_namespace,
            )
            if not kustomization_success:
                raise RuntimeError(f"Failed to create kustomization files for project '{project_name}'")

            # Commit and push infrastructure resources to deployment repo
            await deployment_git_connector.commit_and_push(
                f"Add infrastructure resources for {project_name} in {cluster_name} cluster"
            )

            logger.info(
                f"Successfully committed infrastructure manifests to deployment repo for project '{project_name}'"
            )

            # STEP 6: Create ArgoCD Application, repo secret, and AppProject in ArgoCD applications repo
            # This folder is detected by App-of-Apps pattern and creates the ArgoCD Application
            logger.info(
                f"Creating ArgoCD application resources for infrastructure in {cluster_name}/{project_name}-infrastructure"
            )

            success = await self._argo_manager.create_infrastructure_application(
                project_data=project_data, database_config=database_cluster_config, cluster_name=cluster_name
            )

            if not success:
                raise RuntimeError(f"Failed to create ArgoCD infrastructure application for project '{project_name}'")

            logger.info(f"Successfully created ArgoCD infrastructure application for project '{project_name}'")

            # STEP 7: Refresh ArgoCD user-applications to detect new infrastructure folder
            logger.info(
                f"Refreshing ArgoCD user-applications to create infrastructure application for '{project_name}'"
            )
            from opi.connectors.argo import ArgoConnector

            argo_connector = ArgoConnector(
                server_host=settings.ARGOCD_HOST,
                server_port=settings.ARGOCD_PORT,
                username=settings.ARGOCD_USERNAME,
                password=settings.ARGOCD_PASSWORD,
                use_tls=settings.ARGOCD_USE_TLS,
                verify_ssl=settings.ARGOCD_VERIFY_SSL,
            )

            if not await argo_connector.login():
                raise RuntimeError("Failed to login to ArgoCD")

            # Refresh user-applications app to pick up the new {project_name}-infrastructure folder
            if not await argo_connector.refresh_application("user-applications"):
                raise RuntimeError("Failed to refresh ArgoCD user-applications")

            logger.info("ArgoCD user-applications refreshed, waiting for infrastructure application to be created")

            # STEP 8: Wait for infrastructure application to be created by ArgoCD
            infra_app_name = f"{project_name}-infrastructure"
            if progress_manager and infra_task:
                progress_manager.update_task(infra_task, "Waiting for ArgoCD to create infrastructure application")

            await self._argo_manager.wait_for_application_created(
                app_name=infra_app_name,
                timeout=360,  # 6 min: umbrella app-of-apps refresh can take minutes under load
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
                progress_manager.update_task(infra_task, "Waiting for database cluster to be ready")

            await self._argo_manager.wait_for_infrastructure_ready(
                project_name=project_name,
                cluster_name=cluster_name,
                timeout=600,  # 10 minutes timeout
                refreshed_after=infra_reconciled_at,
            )

            logger.info(f"Infrastructure is ready for project '{project_name}'")

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
        """
        Creates SOPS secrets in the specified namespaces. If no SOPS information is in the project file,
        a new sops pair is created.

        Args:
            deployment_name: Optional single deployment to process
            deployment_names: Optional explicit set of deployments to process
                (takes precedence over ``deployment_name``)
        """
        contents = await self.get_contents()
        project_name = contents.get("name")

        # Get deployments for THIS cluster using helper method
        deployments = await self.get_deployments(
            cluster_filter=True, deployment_name=deployment_name, deployment_names=deployment_names
        )

        targets = _resolve_deployment_filter(deployment_name, deployment_names)
        if targets is not None:
            logger.info(f"Creating SOPS secrets only for deployment(s): {targets}")

        if not deployments:
            logger.warning(f"No deployments found in project: {project_name}")
            return

        for deployment in deployments:
            cluster_name = deployment["cluster"]
            base_namespace = deployment["namespace"]
            namespace = get_prefixed_namespace(cluster_name, base_namespace)

            # Use shared function for SOPS secret creation
            await self._ensure_sops_secret_in_namespace(namespace, contents)

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

    async def process_project_from_git(
        self,
        relative_project_file_path: str,
        task_progress_manager: TaskProgressManager | None = None,
        deployment_name: str | None = None,
        force_clone: bool = False,
        argocd_resources_changed: bool = True,
        deployment_names: list[str] | None = None,
        allow_mutable_tag_retry: bool = True,
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
        targets = _resolve_deployment_filter(deployment_name, deployment_names)

        logger.info(f"Processing project from Git: {relative_project_file_path}")

        try:
            project_full_file_path = await self.get_project_full_file_path()
            git_connector_for_project_files = await self.get_git_connector_for_project_files()

            # Use the file handler to analyze changes
            # TODO: change detection may turn out too difficult or unpredictable, so perhaps we should use API calls instead for partial changes
            analysis = await self._project_file_handler.analyze_project_changes(
                git_connector_for_project_files, project_full_file_path, relative_project_file_path
            )

            current_yaml = analysis["current_yaml"]

            # Security gate FIRST: validate against the project schema before
            # any side-effect (including the auto-migration save+commit+push
            # below). Without this ordering, a hostile project that happens to
            # migrate cleanly would be written to zad-projects with our ops-
            # manager identity *before* validation rejected it. Fails closed.
            validate_project_schema(current_yaml)

            # read_project_file auto-migrates in memory; persist to disk if needed
            if self._project_file_handler.was_migrated:
                project_name = current_yaml.get("name", relative_project_file_path)
                logger.info(f"Schema migration applied for project '{project_name}', committing changes")
                save_project_file(project_full_file_path, current_yaml)
                await git_connector_for_project_files.commit_and_push(
                    f"auto-migrate {project_name} to schema v{current_yaml.get('schema-version', '?')}"
                )

            previous_yaml = analysis["previous_yaml"]
            changes = analysis["changes"]

            # Store change context for use throughout the processing flow
            self._project_changes = {
                "previous_yaml": previous_yaml,
                "current_yaml": current_yaml,
                "changes": changes,
            }

            # Log the changes summary
            if previous_yaml is None:
                logger.info("Processing new project file (no previous version found)")
            else:
                logger.info(
                    f"Detected changes - Added: {len(changes['added'])}, "
                    f"Changed: {len(changes['changed'])}, Deleted: {len(changes['deleted'])}"
                )

            # Step 1.5: Analyze deployment-specific changes
            logger.info("Step 1.5: Analyzing deployment changes")
            deployment_changes = self._analyze_deployment_changes(changes, current_yaml, previous_yaml)

            def _summarize(label: str, section: dict[str, Any], source_yaml: dict[str, Any] | None) -> str:
                names = _deployment_names_for_paths(section, source_yaml)
                suffix = f" ({', '.join(names)})" if names else ""
                return f"{label}: {len(section)}{suffix}"

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
                        _summarize("Added", deployment_changes["added"], current_yaml),
                        _summarize("Changed", deployment_changes["changed"], current_yaml),
                        _summarize("Deleted", deployment_changes["deleted"], previous_yaml),
                    )
                )
                + migration_note
            )

            # Note: Clone operations (both local deployment and remote-source) are now handled
            # directly by DatabaseManager and MinioManager during their create_resources_for_deployment
            # methods. The clone-from configuration is read from the deployment and processed inline.

            # Build a marked-for-deletion service if database pool is available
            # (shared by deployment deletion and service-removal cleanup)
            marked_service = None
            try:
                from opi.core.database_pools import get_database_pool
                from opi.services.marked_for_deletion_service import MarkedForDeletionService

                pool = get_database_pool("main")
                marked_service = MarkedForDeletionService(pool)
            except KeyError, ValueError:
                logger.warning("Database pool not available - persistent resources will be deleted immediately")

            project_name = current_yaml.get("name", "unknown")

            # Step 1.6: Process deployment removals BEFORE creations
            if previous_yaml is not None and deployment_changes["deleted"]:
                logger.info("Processing %d deleted deployment(s)", len(deployment_changes["deleted"]))

                for value in deployment_changes["deleted"].values():
                    if isinstance(value, dict) and "name" in value:
                        dep_name = value["name"]
                        logger.info("Deleting resources for removed deployment: %s", dep_name)
                        try:
                            await self._delete_project_manager.delete_deployment_from_yaml_change(
                                project_name=project_name,
                                deployment_data=value,
                                project_data=current_yaml,
                                marked_for_deletion_service=marked_service,
                                previous_yaml=previous_yaml,
                            )
                        except Exception as e:
                            logger.exception("Failed to delete resources for deployment %s: %s", dep_name, e)
                            critical_failures.append(f"Failed to delete removed deployment '{dep_name}': {e}")

            # Step 1.7: Detect and clean up services removed from surviving deployments
            if previous_yaml is not None:
                try:
                    svc_removal_result = await self._delete_project_manager.cleanup_removed_services_from_yaml_change(
                        project_name=project_name,
                        previous_yaml=previous_yaml,
                        current_yaml=current_yaml,
                        marked_for_deletion_service=marked_service,
                    )
                    if svc_removal_result.get("services_removed", 0) > 0:
                        logger.info(
                            "Service removal cleanup: %d service(s) cleaned up",
                            svc_removal_result["services_removed"],
                        )
                    if svc_removal_result.get("errors"):
                        for err in svc_removal_result["errors"]:
                            logger.error("Service removal error: %s", err)
                            critical_failures.append(err)
                except Exception as e:
                    logger.exception("Failed to process service removal cleanup: %s", e)
                    critical_failures.append(f"Service removal cleanup failed: {e}")

            # Step 2: Process the project with change context
            logger.info("Step 2: Processing project with change detection")

            process_success = await self.process_project(
                force_clone=force_clone, deployment_names=targets, allow_mutable_tag_retry=allow_mutable_tag_retry
            )
            if not process_success:
                critical_failures.append("Project processing failed - check logs for details")

            # Check for critical failures before triggering ArgoCD sync
            # Don't sync if there were failures during processing
            if critical_failures:
                logger.error(f"Project processing completed with {len(critical_failures)} critical failures:")
                for failure in critical_failures:
                    logger.error(f"  - {failure}")
                logger.warning("Skipping ArgoCD sync due to critical failures")
                return False

            logger.info(
                "Triggering ArgoCD sync for user-applications and project applications after project processing"
            )

            progress_manager = self.get_progress_manager()
            argo_task = None
            if progress_manager:
                argo_task = progress_manager.add_task("Waiting for ArgoCD deployment sync")

            argo_connector = create_argo_connector()

            project_name = await self.get_name()
            deployments = await self.get_deployments(cluster_filter=True, deployment_names=targets)
            sync_failures: list[str] = []
            # Runtime pod-health issues (e.g. CrashLoopBackOff) are the user app crashing,
            # not a deploy/sync failure: OPI synced the manifests fine. These are logged
            # as warnings and do NOT fail the task, so a persistently-crashing app doesn't
            # flood ERRORs (and pages) on every reprocess of the project.
            health_warnings: list[str] = []

            if deployments and project_name:
                from opi.services.oom_watcher import (
                    MAIN_CONTAINER_NAME,
                    STALL_NOTICE_SECONDS,
                    DeploymentHealthError,
                    create_health_check_callback,
                    describe_components_waiting,
                    disable_components_for_image_pull,
                )
                from opi.services.resource_tuning_service import tune_deployment_resources
                from opi.utils.naming import generate_unique_name

                # Build (app_name, deployment) pairs
                app_deployments: list[tuple[str, dict]] = []
                for deployment in deployments:
                    dep_name = deployment.get("name")
                    if dep_name:
                        app_name = generate_argocd_application_name(project_name, dep_name)
                        app_deployments.append((app_name, deployment))

                app_names = [ad[0] for ad in app_deployments]
                logger.info(f"Waiting for {len(app_names)} ArgoCD applications to sync for {project_name}")

                # Decide whether the user-applications app-of-apps needs a refresh.
                # A refresh re-renders all ~90 child apps (the umbrella lacks the
                # manifest-generate-paths annotation, see issue #130), so it is slow
                # and only worth it when a target Application CR does not yet exist
                # (new deployment). For redeploys of existing apps it is wasted time
                # in the critical path. Existence is checked against the Kubernetes
                # API (ground truth); the ArgoCD API lies when its cache is stalled.
                # Fail safe: anything not confirmed present (False or unknown None)
                # triggers the refresh. The argocd_resources_changed flag is retained
                # on the signature for caller compatibility but no longer drives this.
                existence = await asyncio.gather(
                    *(
                        self._kubectl_connector.argocd_application_exists(
                            app_name, get_argo_namespace(deployment.get("cluster", "local"))
                        )
                        for app_name, deployment in app_deployments
                    )
                )
                apps_to_create = [app_names[i] for i, exists in enumerate(existence) if exists is not True]

                # Wait for not-yet-present applications to be created (ArgoCD needs to
                # sync user-applications first). All waits run concurrently: read-only polls.
                if apps_to_create:
                    logger.info(
                        f"Refreshing user-applications: {len(apps_to_create)} application(s) not yet present "
                        f"({', '.join(apps_to_create)})"
                    )
                    await argo_connector.refresh_application("user-applications")

                    async def _wait_created(app_name: str) -> str | None:
                        try:
                            await self._argo_manager.wait_for_application_created(
                                app_name=app_name, timeout=360, poll_interval=5
                            )
                            return None
                        except TimeoutError:
                            logger.error(f"Timed out waiting for ArgoCD application '{app_name}' to be created")
                            return f"{app_name}: timed out waiting for application to be created"

                    created_results = await asyncio.gather(*(_wait_created(name) for name in apps_to_create))
                    sync_failures.extend(result for result in created_results if result)
                else:
                    logger.info(
                        f"All {len(app_names)} target application(s) already exist; skipping user-applications refresh"
                    )

                # Refresh each application that was created, then wait for sync+healthy.
                # Refresh + wait run concurrently per application (read-only polls);
                # remediation below stays serial because it mutates the project file.
                async def _refresh_and_wait(app_name: str, deployment: dict) -> dict[str, Any]:
                    dep_name = deployment.get("name", "")
                    base_namespace = deployment.get("namespace", "")
                    cluster = deployment.get("cluster", "")
                    namespace = get_prefixed_namespace(cluster, base_namespace) if base_namespace and cluster else ""

                    # Build health check callback for this deployment
                    oom_callback = None
                    component_names: list[str] = []
                    component_refs: dict[str, str] = {}
                    if namespace:
                        components = deployment.get("components", [])
                        component_names = [
                            generate_unique_name(dep_name, comp.get("reference", ""))
                            for comp in components
                            if comp.get("reference")
                        ]
                        component_refs = {
                            generate_unique_name(dep_name, comp.get("reference", "")): comp.get("reference", "")
                            for comp in components
                            if comp.get("reference")
                        }
                        if component_names:
                            oom_callback = create_health_check_callback(
                                project_name,
                                dep_name,
                                namespace,
                                component_names,
                                component_refs=component_refs,
                            )

                    # Live per-deployment progress subtask: surfaces "what are we
                    # waiting on" each poll (which component, why), so a stalled
                    # rollout is no longer silent. Falls back to None when there is
                    # no progress manager (e.g. non-task callers).
                    app_subtask = (
                        progress_manager.add_subtask(argo_task, f"{dep_name}: uitrol voorbereiden…")
                        if progress_manager and argo_task
                        else None
                    )

                    # Track the last reported line per deployment to detect stalls.
                    stall: dict[str, Any] = {"line": None, "since": 0}

                    async def _on_progressing(elapsed: int) -> None:
                        if app_subtask and namespace and component_names:
                            try:
                                statuses = await describe_components_waiting(namespace, component_names, component_refs)
                            except Exception:
                                statuses = []
                            if statuses:
                                detail = " · ".join(f"{ref}: {reason}" for ref, reason in statuses)
                                line = f"{dep_name} — {detail}"
                            else:
                                line = f"{dep_name} — wachten tot componenten gereed zijn"

                            if line != stall["line"]:
                                stall["line"] = line
                                stall["since"] = elapsed
                            waited = elapsed - stall["since"]
                            if waited >= STALL_NOTICE_SECONDS and statuses:
                                comps = ", ".join(ref for ref, _ in statuses)
                                line = f"{line}  (al {waited}s onveranderd — controleer eventueel de logs van: {comps})"
                            progress_manager.update_task(app_subtask, line)

                        # Failure detection (OOM / ImagePull / CrashLoop) runs after
                        # reporting so the user still sees the live status before a
                        # DeploymentHealthError aborts the wait.
                        if oom_callback:
                            await oom_callback(elapsed)

                    on_progressing = _on_progressing if (app_subtask or oom_callback) else None

                    try:
                        reconciled_at = await argo_connector.refresh_application(app_name)
                        await self._argo_manager.wait_for_application_synced(
                            app_name=app_name,
                            timeout=300,
                            poll_interval=5,
                            refreshed_after=reconciled_at,
                            on_progressing=on_progressing,
                        )
                        logger.info(f"Application '{app_name}' is synced and healthy")
                        if app_subtask:
                            progress_manager.update_task(app_subtask, f"{dep_name}: uitgerold en gezond")
                            progress_manager.complete_subtask(app_subtask)
                        return {"app_name": app_name, "dep_name": dep_name, "status": "ok"}
                    except DeploymentHealthError as e:
                        logger.warning("Pod health issues during sync of '%s': %s", app_name, e)
                        if app_subtask:
                            comps = ", ".join(f.component_reference or f.component_name for f in e.failures)
                            progress_manager.fail_subtask(
                                app_subtask,
                                f"{dep_name}: probleem bij {comps} — controleer de logs van dit/deze component(en)",
                            )
                        return {"app_name": app_name, "dep_name": dep_name, "status": "health_error", "error": e}
                    except TimeoutError:
                        last_state = stall["line"] or "no progress reported"
                        logger.error(
                            f"Timed out after 300s waiting for '{app_name}' ({dep_name}) to sync; "
                            f"last observed state: {last_state}"
                        )
                        if app_subtask:
                            last = stall["line"] or f"{dep_name}: nog niet gezond"
                            progress_manager.fail_subtask(
                                app_subtask,
                                f"Time-out na 300s — {last}. Controleer de logs van het component.",
                            )
                        return {"app_name": app_name, "dep_name": dep_name, "status": "timeout"}
                    except RuntimeError as e:
                        logger.error(f"Application '{app_name}' failed to sync: {e}")
                        if app_subtask:
                            progress_manager.fail_subtask(app_subtask, f"{dep_name}: {e}")
                        return {"app_name": app_name, "dep_name": dep_name, "status": "error", "error": e}

                pending_apps = [(a, d) for a, d in app_deployments if not any(a in f for f in sync_failures)]
                if progress_manager and argo_task and pending_apps:
                    progress_manager.update_task(argo_task, f"Waiting for {len(pending_apps)} application(s) to sync")
                outcomes = await asyncio.gather(*(_refresh_and_wait(a, d) for a, d in pending_apps))

                # Serial remediation pass over the outcomes. OOM tuning and
                # image-pull disabling write to the project file in git, so
                # they must not run concurrently.
                from opi.services.event_interpreter import _interpret_by_reason

                _TYPE_TO_REASON = {
                    "crash_loop": "CrashLoopBackOff",
                    "image_pull": "ImagePullBackOff",
                    "oom": "OOMKilled",
                }
                self._component_failures = []
                for outcome in outcomes:
                    app_name = outcome["app_name"]
                    dep_name = outcome["dep_name"]
                    if outcome["status"] == "ok":
                        continue
                    if outcome["status"] == "timeout":
                        sync_failures.append(f"{app_name} ({dep_name}): timed out after 300s waiting for sync")
                        continue
                    if outcome["status"] == "error":
                        sync_failures.append(f"{app_name}: {outcome['error']}")
                        continue

                    # health_error: store per-component failure details for the
                    # task result, enriched with user-friendly title/suggestion
                    # from the event interpreter.
                    e = outcome["error"]
                    for f in e.failures:
                        reason = _TYPE_TO_REASON.get(f.failure_type, "")
                        translation = _interpret_by_reason(reason, f.message)
                        title = (
                            translation[0]
                            if translation
                            else f"Component '{f.component_reference or f.component_name}'"
                        )
                        suggestion = translation[1] if translation else f.message

                        self._component_failures.append(
                            {
                                "component": f.component_reference or f.component_name,
                                "deployment": f.deployment_name or dep_name,
                                "failure_type": f.failure_type,
                                "message": f.message,
                                "title": title,
                                "suggestion": suggestion,
                                **({"logs": f.logs} if f.logs else {}),
                                **({"container": f.container_name} if f.container_name else {}),
                                **({"image": f.image} if f.image else {}),
                            }
                        )

                    # Categorize failures by type
                    oom_failures = [f for f in e.failures if f.failure_type == "oom"]
                    image_pull_failures = [f for f in e.failures if f.failure_type == "image_pull"]
                    crash_loop_failures = [f for f in e.failures if f.failure_type == "crash_loop"]

                    task_service = (
                        progress_manager._task_service
                        if progress_manager and hasattr(progress_manager, "_task_service")
                        else None
                    )

                    # Handle OOM: tune resources, queue refresh (existing behavior)
                    if oom_failures:
                        if progress_manager and argo_task:
                            progress_manager.update_task(
                                argo_task,
                                f"OOM detected for {app_name}, tuning resources...",
                            )
                        try:
                            result = await tune_deployment_resources(project_name, dep_name, skip_reprocessing=True)
                            if result.changes:
                                logger.info(
                                    "OOM auto-tune applied %d change(s) for %s/%s, queuing refresh task",
                                    len(result.changes),
                                    project_name,
                                    dep_name,
                                )
                                await self._queue_refresh_task(task_service, project_name, dep_name)
                            else:
                                logger.warning(
                                    "OOM auto-tune found no actionable changes for %s/%s",
                                    project_name,
                                    dep_name,
                                )
                                sync_failures.append(
                                    f"{app_name}: OOM detected but auto-tune could not determine new limits"
                                )
                        except Exception as tune_err:
                            logger.error("OOM auto-tune failed for %s/%s: %s", project_name, dep_name, tune_err)
                            sync_failures.append(f"{app_name}: OOM detected, auto-tune failed: {tune_err}")

                    # Handle ImagePullBackOff: disable component, queue refresh. A pod
                    # never becomes ready while any of its containers (main OR sidecar)
                    # is stuck pulling, so a sidecar failure disables the component too.
                    #
                    # Use component_reference (the user-facing YAML reference), not
                    # component_name (the unique deployment-scoped name): disabling looks
                    # the component up by `reference`, so passing the unique name silently
                    # fails to match and the component is never scaled to 0.
                    if image_pull_failures:
                        disabled_components = [(f.component_reference, f.message) for f in image_pull_failures]
                        try:
                            await disable_components_for_image_pull(project_name, dep_name, disabled_components)
                            await self._queue_refresh_task(task_service, project_name, dep_name)
                        except Exception as img_err:
                            logger.error(
                                "ImagePull remediation failed for %s/%s: %s",
                                project_name,
                                dep_name,
                                img_err,
                            )
                        # Name the exact container and image, so a sidecar's pull error is
                        # never mistaken for the component's own image (the reason a bad
                        # platform sidecar sent us down the wrong path for hours).
                        for f in image_pull_failures:
                            if f.container_name and f.container_name != MAIN_CONTAINER_NAME:
                                target = f"sidecar '{f.container_name}' in component '{f.component_name}'"
                            else:
                                target = f"component '{f.component_name}'"
                            image = f" [image {f.image}]" if f.image else ""
                            reason = " ".join(f.message.split()) if f.message else ""
                            detail = f"{target}{image}"
                            if reason:
                                detail = f"{detail}: {reason}"
                            sync_failures.append(f"{app_name}: ImagePullBackOff for {detail}")

                    # CrashLoopBackOff is the user's app crashing at runtime, not a
                    # deploy/sync failure - report it as a warning, don't fail the task.
                    if crash_loop_failures:
                        names = ", ".join(f.component_name for f in crash_loop_failures)
                        health_warnings.append(f"{app_name}: CrashLoopBackOff for {names}")

            if health_warnings:
                logger.warning(
                    "Runtime pod-health issue(s) after sync (app crashing, not a deploy failure): %s",
                    "; ".join(health_warnings),
                )

            if sync_failures:
                failure_summary = "; ".join(sync_failures)
                logger.error(f"ArgoCD sync completed with {len(sync_failures)} failure(s): {failure_summary}")
                if progress_manager and argo_task:
                    progress_manager.fail_task(argo_task, f"Sync failures: {failure_summary}")
                critical_failures.append(f"ArgoCD sync failures: {failure_summary}")
                self._processing_error = failure_summary
                return False
            elif health_warnings:
                logger.info("ArgoCD applications synced (%d runtime health warning(s) above)", len(health_warnings))
            else:
                logger.info("All ArgoCD applications are synced and healthy")
                if progress_manager and argo_task:
                    progress_manager.complete_task(argo_task)
                return True
        except Exception as e:
            logger.exception(f"Error processing project from Git: {e}")
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
        self, deployment_name: str | None = None, deployment_names: list[str] | None = None
    ) -> None:
        """
        Process application manifests for the in-scope deployments.

        Args:
            deployment_name: Optional single deployment to process only.
            deployment_names: Optional explicit set of deployments to process
                (takes precedence over ``deployment_name``). Use this to keep a
                scoped op (e.g. an upsert of one deployment) from regenerating
                every deployment's manifest folder -- which would otherwise
                rewrite unrelated deployments and collide with a concurrent
                delete.

        Returns: None
        """
        project_name = await self.get_name()
        logger.info(f"Processing application manifests for {project_name}")

        repositories = await self.get_repositories()
        if not repositories:
            logger.warning("No repositories defined in project data")
            return

        # Group deployments by repository (only for current cluster), already
        # filtered to the requested scope -- get_deployments honours both
        # deployment_name and deployment_names.
        deployments = await self.get_deployments(
            cluster_filter=True, deployment_name=deployment_name, deployment_names=deployment_names
        )
        if deployment_names is not None or deployment_name:
            logger.info(f"Processing application manifests only for: {deployment_names or [deployment_name]}")

        deployments_by_repo = {}
        for deployment in deployments:
            repo_name = deployment.get("repository")
            if repo_name not in deployments_by_repo:
                deployments_by_repo[repo_name] = []
            deployments_by_repo[repo_name].append(deployment)

        # Process each repository
        for repo_name, repo_deployments in deployments_by_repo.items():
            logger.info(f"Processing repository: {repo_name} with {len(repo_deployments)} deployments")

            repo_info = next((r for r in repositories if r.get("name") == repo_name), None)
            if not repo_info:
                raise Exception(f"Repository configuration not found: {repo_name}")

            await self._process_repository_manifests(repo_info, repo_deployments)

        logger.info(f"Successfully processed all application manifests for {project_name}")

    async def _process_repository_manifests(
        self,
        repo_config: dict[str, Any],
        deployments: list[dict[str, Any]],
    ) -> None:
        """
        Process manifests for a specific repository.

        Args:
            repo_config: Repository configuration
            deployments: List of deployments for this repository

        Returns:
            None
        """
        project_data = await self.get_contents()
        project_name = project_data.get("name")

        project_repo_connector = await self.get_git_connector_for_deployment(repo_config["name"], repo_config)
        # Deployments are already filtered for current cluster by caller
        for deployment in deployments:
            await self._process_deployment_manifests(deployment, project_repo_connector)
            await project_repo_connector.commit_changes(f"Update manifests for {project_name}/{deployment['name']}")

        await project_repo_connector.push_changes()

        logger.info(f"Successfully processed repository: {repo_config['name']}")

    # TODO: maybe this should be moved to project_file manager
    async def get_repository_path(self, repository_name: str) -> str | None:
        project_data = await self.get_contents()
        repositories = project_data.get("repositories") or []
        repositories: list[dict[str, str]] = repositories
        for repo in repositories:
            if repo.get("name") == repository_name:
                return repo.get("path", "")
        return ""

    @staticmethod
    async def _sops_private_key_for(project_data: dict[str, Any]) -> str | None:
        """Decode the project's AGE private key for SOPS skip-if-unchanged.

        Returns None when the key can't be decoded (e.g. legacy project without
        ``age-private-key``), so the caller falls back to always re-encrypting.
        """
        try:
            return await get_decoded_project_private_key(project_data)
        except Exception as e:
            logger.info(f"No project private key for SOPS unchanged-check, re-encrypting: {e}")
            return None

    def _prune_obsolete_component_manifests(
        self, deployment: dict[str, Any], target_path: str, generated_files: list[str]
    ) -> None:
        """Delete per-component manifest files this run did not (re)generate.

        ``create_application_manifests`` only writes/overwrites files for the CURRENT
        components, and the kustomization is rebuilt from the on-disk glob, so any
        leftover per-component file would otherwise be re-adopted and keep its dead
        workload running in ArgoCD. This prunes those obsolete files, covering both a
        removed component (none of its files regenerated) and a renamed/dropped file of
        a surviving component (e.g. an ingress whose path changed). Shared files and
        ``*.marked-for-deletion.yaml`` PVCs are preserved.

        Args:
            deployment: Current deployment configuration.
            target_path: Absolute deployment manifest directory in the cloned repo.
            generated_files: Basenames generated this run (the desired-state set).
        """
        deployment_name = deployment["name"]

        def _component_refs(dep: dict[str, Any]) -> set[str]:
            return {
                ref for comp in dep.get("components", []) if isinstance(comp, dict) and (ref := comp.get("reference"))
            }

        # Components that own files here: current ones (stale-file pruning) plus any
        # from the previous version (removed-component pruning).
        component_names = _component_refs(deployment) | _component_refs(
            self.get_previous_deployment(deployment_name) or {}
        )
        if not component_names:
            return

        obsolete = _select_obsolete_component_manifests(
            target_path, component_names, set(generated_files), deployment_name
        )
        if not obsolete:
            return

        logger.info(
            f"Pruning {len(obsolete)} obsolete component manifest(s) from {deployment_name}: {', '.join(obsolete)}"
        )
        for basename in obsolete:
            os.remove(os.path.join(target_path, basename))

    async def _process_deployment_manifests(
        self,
        deployment: dict[str, Any],
        git_connector: GitConnector,
    ) -> None:
        """
        Process manifests for a specific deployment.

        Supports two deployment types:
        1. Component-based deployments: Traditional OPI components with deployment/service/ingress manifests
        2. Helm chart deployments: External Helm charts rendered via Kustomize helmCharts

        Args:
            deployment: Deployment configuration
            git_connector: GitConnector for the repository

        Returns:
            None
        """
        project_data = await self.get_contents()
        project_name = await self.get_name()
        deployment_name = deployment.get("name")
        cluster_name = deployment["cluster"]

        repo_path = await self.get_repository_path(deployment["repository"])
        if repo_path:
            deployment_path = f"{repo_path}/{cluster_name}/{project_name}/{deployment_name}"
        else:
            deployment_path = f"{cluster_name}/{project_name}/{deployment_name}"

        prefixed_namespace = get_prefixed_namespace(cluster_name, deployment["namespace"])
        target_path = os.path.join(await git_connector.get_working_dir(), deployment_path)

        logger.info(f"Processing deployment: {deployment_name} at path: {deployment_path}")

        # Check if this is a helmfile deployment, helm chart deployment, or component deployment
        if self._deployment_uses_helmfile(deployment):
            # Process helmfile deployment (ArgoCD CMP runs helmfile template)
            logger.info(f"Deployment '{deployment_name}' uses helmfile - processing as helmfile deployment")
            await self._process_helmfile_deployment(deployment, git_connector, target_path)
            # Note: SOPS encryption is handled inside _process_helmfile_deployment
            return

        if self._deployment_uses_helm_charts(deployment):
            # Process helm chart deployment (no component manifests, uses Kustomize helmCharts)
            logger.info(f"Deployment '{deployment_name}' uses helm-charts - processing as helm deployment")
            await self._process_helm_chart_deployment(deployment, git_connector, target_path)
            # Note: SOPS encryption is handled inside _process_helm_chart_deployment
            return

        # Standard component-based deployment processing
        logger.info(f"Deployment '{deployment_name}' uses components - processing as standard deployment")

        # Pre-scan: Collect all aliases from components before creating any manifests
        # This allows deployment-level secrets to include aliases from all components
        self._deployment_aliases[deployment_name] = await self._collect_deployment_aliases(deployment_name)

        try:
            created_files = await self.create_application_manifests(deployment, git_connector, deployment_path)
        except Exception as e:
            # Rollback subdomain registration if manifest creation fails
            await self._rollback_subdomain_if_needed()
            raise

        # Note: SSO and user secrets are already created in create_application_manifests above

        # Prune obsolete per-component manifests: any file this run did not regenerate
        # (removed component, or a renamed/dropped file of a surviving component such as
        # an ingress whose path changed). The kustomization is rebuilt from the on-disk
        # glob, so leftovers would otherwise be re-adopted and keep dead workloads in
        # ArgoCD. Symmetric with the removed-service cleanup in DeleteProjectManager.
        self._prune_obsolete_component_manifests(deployment, target_path, created_files)

        # Run manifest extensions (e.g. registry rewrite for ODCN)
        extension_pipeline = load_extensions(cluster_name)
        if extension_pipeline.has_extensions:
            logger.info(f"Running manifest extensions for deployment: {deployment_name}")
            extension_pipeline.process_directory(target_path)

        # Create a kustomization file BEFORE encrypting .to-sops.yaml files
        # This ensures kustomization and decrypt-sops.yaml can see all .to-sops.yaml files
        sops_files, regular_files = self._manifest_generator.collect_manifest_files(
            target_path, include_subfolders=False
        )
        logger.info(f"Found {len(sops_files)} SOPS files and {len(regular_files)} regular files for kustomization")
        await self.create_kustomization_file(
            git_connector, prefixed_namespace, sops_files, regular_files, deployment_path, deployment
        )

        # FINAL STEP: Convert .to-sops.yaml files to .sops.yaml files
        # This must be done AFTER kustomization creation so that decrypt-sops.yaml
        # can reference the original .to-sops.yaml filenames
        public_key = get_project_public_key(project_data)
        logger.info(f"Encrypting .to-sops.yaml files for deployment: {deployment_name}")
        logger.info(f"Using SOPS public key for namespace: {prefixed_namespace}")
        logger.info(f"SOPS encryption target path: {target_path}")

        # List .to-sops.yaml files before encryption for debugging

        to_sops_pattern = os.path.join(target_path, "*.to-sops.yaml")
        to_sops_files = glob.glob(to_sops_pattern)
        logger.info(f"Found {len(to_sops_files)} .to-sops.yaml files for final encryption:")
        for file_path in to_sops_files:
            logger.info(f"  - {os.path.basename(file_path)}")

        if not public_key:
            raise RuntimeError(
                f"Geen SOPS public key voor deployment '{deployment_name}'; "
                "dit zou secrets in platte tekst naar git committen."
            )

        encrypt_to_sops_files_or_fail(
            target_path,
            public_key,
            f"secrets voor deployment '{deployment_name}' (namespace '{prefixed_namespace}')",
            private_key=await self._sops_private_key_for(project_data),
        )
        logger.info("All .to-sops.yaml files successfully encrypted")

    # ==========================================================================
    # Helm Chart Processing Methods
    # ==========================================================================

    def _deployment_uses_helm_charts(self, deployment: dict[str, Any]) -> bool:
        """
        Check if a deployment uses helm-charts instead of components.

        Args:
            deployment: Deployment configuration

        Returns:
            True if deployment has helm-charts defined
        """
        helm_charts = deployment.get("helm-charts", [])
        return len(helm_charts) > 0

    def _deployment_uses_helmfile(self, deployment: dict[str, Any]) -> bool:
        """
        Check if a deployment uses helmfile instead of components or helm-charts.

        Args:
            deployment: Deployment configuration

        Returns:
            True if deployment has helmfile defined
        """
        helmfiles = deployment.get("helmfile", [])
        return len(helmfiles) > 0

    async def _get_helm_values_context(self, deployment_name: str) -> dict[str, Any]:
        """
        Build a context dictionary with all service credentials for alias resolution.

        This collects credentials from all service managers (database, minio, keycloak, redis)
        and deployment-level variables (hostname, subdomain, base-domain, issuer).

        Args:
            deployment_name: Name of the deployment

        Returns:
            Dictionary mapping alias names to resolved values (preserving types for integers)
        """
        context: dict[str, Any] = {}

        # Get deployment for cluster info
        deployment = await self.get_deployment_by_name(deployment_name)
        if not deployment:
            logger.warning(f"Deployment '{deployment_name}' not found")
            return context

        # Add deployment-level variables for hostname, subdomain, base-domain, issuer
        cluster_name = deployment.get("cluster", settings.CLUSTER_MANAGER)
        subdomain = deployment.get("subdomain")
        base_domain = deployment.get("base-domain")
        issuer_config = deployment.get("issuer")
        use_https = get_ingress_tls_enabled(cluster_name)

        # Calculate hostname based on configuration
        if base_domain and subdomain:
            # External domain mode: subdomain.base-domain
            hostname = generate_external_hostname(subdomain, base_domain)
        elif subdomain:
            # Subdomain with cluster domain
            ingress_postfix = get_ingress_postfix(cluster_name)
            hostname = f"{subdomain}.{ingress_postfix}"
        else:
            # Fallback: use deployment name with cluster domain
            ingress_postfix = get_ingress_postfix(cluster_name)
            hostname = f"{deployment_name}.{ingress_postfix}"

        # Determine issuer name
        if issuer_config:
            if issuer_config in ("letsencrypt", "letsencrypt-staging") and base_domain:
                # Generate full issuer name to match the Issuer resource we create
                context["ISSUER"] = generate_issuer_name(base_domain, issuer_config, deployment_name)
            else:
                context["ISSUER"] = issuer_config
        else:
            # Use cluster's default ClusterIssuer
            cluster_issuer = get_ingress_cluster_issuer(cluster_name)
            if cluster_issuer:
                context["ISSUER"] = cluster_issuer

        # Add hostname-related variables
        public_url = generate_public_url(hostname, use_https)
        context["PUBLIC_HOST"] = public_url
        context["PUBLIC_HOSTNAME"] = hostname
        context["HOSTNAME"] = hostname
        if subdomain:
            context["SUBDOMAIN"] = subdomain
        if base_domain:
            context["BASE_DOMAIN"] = base_domain

        logger.debug(f"Added deployment variables: PUBLIC_HOST={public_url}, HOSTNAME={hostname}")

        # Get database credentials if available
        db_secret = self._get_secret_from_map(deployment_name, "database", DatabaseSecret)
        if db_secret:
            context["DATABASE_SERVER_HOST"] = db_secret.host
            context["DATABASE_SERVER_PORT"] = db_secret.port  # Keep as int for YAML type preservation
            context["DATABASE_SERVER_USER"] = db_secret.username
            context["DATABASE_PASSWORD"] = db_secret.password
            context["DATABASE_DB"] = db_secret.database
            context["DATABASE_SCHEMA"] = db_secret.schema

        # Get MinIO credentials if available
        minio_secret = self._get_secret_from_map(deployment_name, "minio", MinIOSecret)
        if minio_secret:
            context["OBJECT_STORE_HOST"] = minio_secret.host
            context["OBJECT_STORE_PORT"] = minio_secret.port  # Keep as int for YAML type preservation
            context["OBJECT_STORE_URL"] = minio_secret.url
            context["OBJECT_STORE_ENDPOINT_URL"] = minio_secret.endpoint_url
            context["OBJECT_STORE_USER"] = minio_secret.access_key
            context["OBJECT_STORE_PASSWORD"] = minio_secret.secret_key
            context["OBJECT_STORE_BUCKET_NAME"] = minio_secret.bucket_name
            context["OBJECT_STORE_REGION"] = minio_secret.region

        # Get Keycloak credentials if available
        keycloak_secret = self._get_secret_from_map(deployment_name, "keycloak", KeycloakSecret)
        if keycloak_secret:
            context["OIDC_DISCOVERY_URL"] = keycloak_secret.discovery_url
            context["OIDC_CLIENT_ID"] = keycloak_secret.client_id
            context["OIDC_CLIENT_SECRET"] = keycloak_secret.client_secret
            context["KEYCLOAK_BASE_URL"] = keycloak_secret.base_url
            context["KEYCLOAK_REALM"] = keycloak_secret.realm

        # Get Redis credentials if available
        redis_secret = self._get_secret_from_map(deployment_name, "redis", RedisSecret)
        if redis_secret:
            context["REDIS_HOST"] = redis_secret.host
            context["REDIS_PORT"] = redis_secret.port  # Keep as int for YAML type preservation
            context["REDIS_PASSWORD"] = redis_secret.password
            context["REDIS_USERNAME"] = redis_secret.username
            context["REDIS_URL"] = redis_secret.url

        logger.debug(f"Built helm values context with {len(context)} variables for deployment '{deployment_name}'")
        return context

    def _resolve_nested_aliases(self, obj: Any, context: dict[str, Any]) -> Any:
        """
        Recursively resolve $ALIAS references in a nested YAML structure.

        Supports both $VAR and ${VAR} syntax for variable substitution.
        Preserves types (int, bool) when the entire value is a single variable reference.

        Args:
            obj: The object to process (can be dict, list, or scalar)
            context: Dictionary of alias_name -> value for substitution

        Returns:
            The object with all aliases resolved
        """
        if isinstance(obj, str):
            # Check if entire string is exactly a single variable reference
            # This preserves types (e.g., integers) instead of converting to string
            for key, value in context.items():
                if obj == f"${key}" or obj == f"${{{key}}}":
                    return value  # Return raw value, preserving original type
            # Otherwise do string replacement (for partial matches or multiple variables)
            result = obj
            for key, value in context.items():
                result = result.replace(f"${key}", str(value))
                result = result.replace(f"${{{key}}}", str(value))
            return result
        elif isinstance(obj, dict):
            return {k: self._resolve_nested_aliases(v, context) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._resolve_nested_aliases(item, context) for item in obj]
        return obj

    def _deep_merge_dicts(self, base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        """
        Deep merge two dictionaries, with override values taking precedence.

        Args:
            base: Base dictionary
            override: Override dictionary (values take precedence)

        Returns:
            Merged dictionary
        """
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge_dicts(result[key], value)
            else:
                result[key] = value
        return result

    async def _clone_helm_chart(
        self,
        helm_chart: dict[str, Any],
        target_dir: str,
    ) -> str:
        """
        Clone a Helm chart from git and return the path to the chart.

        Args:
            helm_chart: Helm chart configuration from project
            target_dir: Directory where chart should be cloned

        Returns:
            Path to the chart directory
        """
        source_type = helm_chart.get("source-type", "git-clone")

        if source_type != "git-clone":
            raise ValueError(f"Unsupported helm chart source-type: {source_type}")

        git_url = helm_chart.get("git-url")
        git_ref = helm_chart.get("git-ref", "main")
        chart_path = helm_chart.get("chart-path", ".")
        chart_name = helm_chart.get("name")

        if not git_url:
            raise ValueError(f"Helm chart '{chart_name}' missing git-url")
        if not chart_name:
            raise ValueError("Helm chart missing required 'name' field")

        logger.info(f"Cloning helm chart '{chart_name}' from {git_url}#{git_ref}")

        # Create a temporary git connector to clone the chart repo
        import shutil
        import tempfile

        dest_chart_path = ""  # Will be set inside the temp directory context

        with tempfile.TemporaryDirectory() as temp_dir:
            # Clone the repository
            from opi.connectors.git import GitConnector

            git_connector = GitConnector(
                repo_url=git_url,
                branch=git_ref,
                working_dir=temp_dir,
            )

            await git_connector.clone()

            # Get the chart source path
            source_chart_path = os.path.join(temp_dir, chart_path)

            if not os.path.exists(source_chart_path):
                raise FileNotFoundError(f"Chart path not found: {chart_path} in {git_url}")

            # Create charts directory in target
            charts_dir = os.path.join(target_dir, "charts")
            os.makedirs(charts_dir, exist_ok=True)

            # Copy chart to target directory
            dest_chart_path = os.path.join(charts_dir, chart_name)
            if os.path.exists(dest_chart_path):
                shutil.rmtree(dest_chart_path)
            shutil.copytree(source_chart_path, dest_chart_path)

            logger.info(f"Copied helm chart '{chart_name}' to {dest_chart_path}")

            # Check for chart dependencies (common chart)
            chart_yaml_path = os.path.join(dest_chart_path, "Chart.yaml")
            if os.path.exists(chart_yaml_path):
                yaml = YAML()
                with open(chart_yaml_path) as f:
                    chart_data = yaml.load(f)

                dependencies = chart_data.get("dependencies", [])
                for dep in dependencies:
                    dep_name = dep.get("name")
                    dep_repo = dep.get("repository", "")

                    # Handle local file:// dependencies
                    if dep_repo.startswith("file://"):
                        rel_path = dep_repo.replace("file://", "")
                        dep_source = os.path.join(temp_dir, chart_path, rel_path)

                        if os.path.exists(dep_source):
                            dep_dest = os.path.join(charts_dir, dep_name)
                            if os.path.exists(dep_dest):
                                shutil.rmtree(dep_dest)
                            shutil.copytree(dep_source, dep_dest)

                            # Update the dependency path in Chart.yaml
                            dep["repository"] = f"file://../{dep_name}"
                            logger.info(f"Copied dependency chart '{dep_name}' to {dep_dest}")

                # Write updated Chart.yaml if dependencies were modified
                with open(chart_yaml_path, "w") as f:
                    yaml.dump(chart_data, f)

            await git_connector.close()

        return dest_chart_path

    async def _process_helm_chart_deployment(
        self,
        deployment: dict[str, Any],
        git_connector: GitConnector,
        target_path: str,
    ) -> None:
        """
        Process a deployment that uses helm-charts instead of components.

        This method:
        1. Clones the helm chart(s) from git
        2. Extracts and merges helm-values (base + deployment-specific)
        3. Resolves $ALIAS references using service credentials
        4. Creates values.yaml with resolved values
        5. Generates kustomization.yaml with helmCharts section

        Args:
            deployment: Deployment configuration
            git_connector: Git connector for the target repository
            target_path: Full path to the deployment directory
        """
        project_data = await self.get_contents()
        deployment_name = deployment.get("name")
        if not deployment_name:
            raise ValueError("Deployment missing required 'name' field")

        cluster_name = deployment.get("cluster", settings.CLUSTER_MANAGER)
        prefixed_namespace = get_prefixed_namespace(cluster_name, deployment["namespace"])

        logger.info(f"Processing helm chart deployment: {deployment_name}")

        # Ensure target directory exists
        os.makedirs(target_path, exist_ok=True)

        # Get helm charts from deployment
        helm_chart_refs = deployment.get("helm-charts", [])

        # Build the credentials context for alias resolution
        context = await self._get_helm_values_context(deployment_name)

        # Process each helm chart reference
        helm_charts_config: list[dict[str, Any]] = []  # For kustomization.yaml helmCharts section

        for helm_chart_ref in helm_chart_refs:
            chart_reference = helm_chart_ref.get("reference")
            if not chart_reference:
                raise ValueError("Helm chart reference missing required 'reference' field")

            release_name = helm_chart_ref.get("release-name", chart_reference)

            logger.info(f"Processing helm chart reference: {chart_reference}")

            # Find the helm-chart definition in project
            helm_chart_def = self._project_file_handler.get_helm_chart_by_name(project_data, chart_reference)
            if not helm_chart_def:
                raise ValueError(f"Helm chart '{chart_reference}' not found in project definition")

            # Clone the chart to target directory
            chart_path = await self._clone_helm_chart(helm_chart_def, target_path)

            # Extract base helm-values from helm-chart definition
            base_values = await self._project_file_handler.extract_helm_chart_values(project_data, chart_reference)

            # Extract deployment-level helm-values
            deployment_values = await self._project_file_handler.extract_deployment_helm_chart_values(
                project_data, deployment_name, chart_reference
            )

            # Deep merge values (deployment overrides base)
            merged_values = self._deep_merge_dicts(base_values, deployment_values)

            # Resolve $ALIAS references in the merged values
            resolved_values = self._resolve_nested_aliases(merged_values, context)

            # Write values as .to-sops.yaml (will be encrypted later)
            # Use naming convention that matches CMP plugin pattern: *-helm-values.sops.yaml
            values_file_sops = generate_helm_values_filename(deployment_name, chart_reference, encrypted=True)
            values_file_to_sops = values_file_sops.replace(".sops.yaml", ".to-sops.yaml")
            values_path = os.path.join(target_path, values_file_to_sops)

            yaml = YAML()
            yaml.default_flow_style = False
            with open(values_path, "w") as f:
                yaml.dump(resolved_values, f)

            logger.info(f"Created helm values file (to be encrypted): {values_path}")

            # Add to helmCharts config for kustomization.yaml
            # Reference the final .sops.yaml name (after encryption)
            helm_charts_config.append(
                {
                    "name": chart_reference,
                    "releaseName": release_name,
                    "namespace": prefixed_namespace,
                    "valuesFile": values_file_sops,
                    "repo": f"file://./charts/{chart_reference}",
                }
            )

        # Create service secret manifests (database, minio, redis, keycloak)
        secret_files = await self._create_deployment_secrets(
            deployment_name,
            target_path,
            prefixed_namespace,
            cluster_name,
        )
        logger.info(f"Created {len(secret_files)} service secret manifests for helm deployment")

        # Create Let's Encrypt Issuer manifest if configured
        regular_files: list[str] = []
        issuer_config = deployment.get("issuer")
        base_domain = deployment.get("base-domain")

        # Only auto-generate issuer if issuer_config is exactly "letsencrypt" or "letsencrypt-staging"
        # If issuer_config already contains a domain suffix, use it as-is (no generation needed)
        if issuer_config and issuer_config in ("letsencrypt", "letsencrypt-staging") and base_domain:
            # Determine contact email: project override or cluster default
            project_contact_email = project_data.get("config", {}).get("contact-email")
            cluster_contact_email = get_letsencrypt_contact_email(cluster_name)
            contact_email = project_contact_email or cluster_contact_email

            if contact_email:
                issuer_template_path = os.path.join(
                    os.path.dirname(__file__), "..", "..", "manifests", "issuer-letsencrypt.yaml.jinja"
                )

                issuer_name_generated = generate_issuer_name(base_domain, issuer_config, deployment_name)
                issuer_secret_name = generate_issuer_secret_name(base_domain, issuer_config, deployment_name)
                issuer_manifest_filename = generate_issuer_manifest_name(
                    base_domain, issuer_config, deployment_name
                ).replace(".yaml", "")

                issuer_variables = {
                    "issuer_name": issuer_name_generated,
                    "issuer_secret_name": issuer_secret_name,
                    "contact_email": contact_email,
                    "staging": issuer_config == "letsencrypt-staging",
                    "namespace": prefixed_namespace,
                }

                issuer_manifest_path = self._manifest_generator.create_manifest_file(
                    template_path=issuer_template_path,
                    values=issuer_variables,
                    output_dir=target_path,
                    output_filename=issuer_manifest_filename,
                    use_sops=False,
                )

                regular_files.append(f"{issuer_manifest_filename}.yaml")
                logger.info(f"Created Let's Encrypt Issuer manifest for {base_domain}: {issuer_manifest_path}")

                # Create network policy for ACME HTTP-01 challenge.
                # cert-manager's HTTP-01 solver pod listens on 8089 (the router
                # forwards the challenge to the pod on 8089, not 80), so the
                # solver port must be allowed or the self-check times out.
                network_policy_template_path = os.path.join(
                    os.path.dirname(__file__), "..", "..", "manifests", "network-policy.yaml.jinja"
                )
                network_policy_filename = generate_network_policy_manifest_name("acme-http", deployment_name)
                network_policy_variables = {
                    "name": generate_network_policy_name("acme-http", deployment_name),
                    "namespace": prefixed_namespace,
                    "pod_selector": None,  # Match all pods
                    "ports": [80, 8089],  # 80 for ingress, 8089 for ACME solver pod
                }
                network_policy_path = self._manifest_generator.create_manifest_file(
                    template_path=network_policy_template_path,
                    values=network_policy_variables,
                    output_dir=target_path,
                    output_filename=network_policy_filename,
                    use_sops=False,
                )
                regular_files.append(f"{network_policy_filename}.yaml")
                logger.info(f"Created HTTP ingress network policy for ACME challenge: {network_policy_path}")
            else:
                logger.warning(
                    f"Cannot create Let's Encrypt Issuer for {base_domain}: no contact email configured "
                    f"(set contact-email in project config or letsencrypt.contact_email in cluster config)"
                )

        # Create kustomization.yaml with helmCharts section and secret resources
        await self._create_helm_kustomization(
            target_path,
            prefixed_namespace,
            helm_charts_config,
            secret_files,
            regular_files,
        )

        # Encrypt .to-sops.yaml files to .sops.yaml
        public_key = get_project_public_key(project_data)
        if not public_key:
            raise ValueError(
                f"No public key found for project, cannot encrypt helm values for deployment: {deployment_name}. "
                "This would commit secrets in plain text to git!"
            )

        logger.info(f"Encrypting helm values files for deployment: {deployment_name}")

        # List .to-sops.yaml files before encryption for debugging
        to_sops_pattern = os.path.join(target_path, "*.to-sops.yaml")
        to_sops_files = glob.glob(to_sops_pattern)
        logger.info(f"Found {len(to_sops_files)} .to-sops.yaml files to encrypt:")
        for file_path in to_sops_files:
            logger.info(f"  - {os.path.basename(file_path)}")

        encrypt_to_sops_files_or_fail(
            target_path,
            public_key,
            f"helm values-secrets voor deployment '{deployment_name}' (namespace '{prefixed_namespace}')",
            private_key=await self._sops_private_key_for(project_data),
        )

        logger.info("All helm values files successfully encrypted")
        logger.info(f"Helm chart deployment processing complete for: {deployment_name}")

    # ==========================================================================
    # Helmfile Processing Methods
    # ==========================================================================

    def _write_helmfile_custom_files(
        self,
        helmfile_def: dict[str, Any],
        helmfile_ref: dict[str, Any],
        target_path: str,
    ) -> list[str]:
        """
        Write custom files defined in helmfile definition and deployment reference.

        Files can be defined at two levels:
        1. helmfile.files - base files that apply to all deployments
        2. deployment.helmfile[].files - deployment-specific files (override base)

        Args:
            helmfile_def: Helmfile definition from project (contains base files)
            helmfile_ref: Helmfile reference from deployment (contains override files)
            target_path: Directory where files should be written

        Returns:
            List of filenames that were written
        """
        written_files: list[str] = []

        # Merge files: base files first, then deployment files override
        base_files = helmfile_def.get("files", {}) or {}
        deployment_files = helmfile_ref.get("files", {}) or {}

        # Combine with deployment files taking precedence
        all_files = {**base_files, **deployment_files}

        if not all_files:
            return written_files

        logger.info(f"Writing {len(all_files)} custom file(s) to {target_path}")

        for filename, content in all_files.items():
            if not isinstance(content, str):
                logger.warning(f"Skipping file '{filename}': content must be a string")
                continue

            # Security: prevent path traversal
            if ".." in filename or filename.startswith("/"):
                logger.warning(f"Skipping file '{filename}': path traversal not allowed")
                continue

            file_path = os.path.join(target_path, filename)

            # Create parent directories if needed
            file_dir = os.path.dirname(file_path)
            if file_dir and file_dir != target_path:
                os.makedirs(file_dir, exist_ok=True)

            with open(file_path, "w") as f:
                f.write(content)

            written_files.append(filename)
            logger.info(f"  Created custom file: {filename}")

        return written_files

    async def _clone_helmfile_source(
        self,
        helmfile_def: dict[str, Any],
        target_path: str,
    ) -> tuple[str, str]:
        """
        Clone a helmfile source repository to the target directory.

        Args:
            helmfile_def: Helmfile definition containing url, ref, path, and entry
            target_path: Directory where the helmfile should be placed

        Returns:
            Tuple of (path to cloned directory, entry point relative path)
            The entry point is the subdirectory containing the helmfile to execute.
        """
        import tempfile

        helmfile_name = helmfile_def.get("name", "unknown")
        source_url = helmfile_def.get("url")
        source_ref = helmfile_def.get("ref", "main")
        source_path = helmfile_def.get("path", "")
        entry_path = helmfile_def.get("entry", "")  # Subdirectory containing the helmfile

        if not source_url:
            raise ValueError(f"Helmfile '{helmfile_name}' missing required 'url' field")

        # Create a temporary directory for cloning the source
        with tempfile.TemporaryDirectory() as temp_dir:
            git_connector = GitConnector(
                repo_url=source_url,
                branch=source_ref,
                working_dir=temp_dir,
            )
            await git_connector.clone()

            # Determine source path within the cloned repo
            full_source_path = os.path.join(temp_dir, source_path) if source_path else temp_dir

            if not os.path.exists(full_source_path):
                await git_connector.close()
                raise ValueError(f"Helmfile path '{source_path}' not found in repository for '{helmfile_name}'")

            # If entry is specified, verify it exists within the source path
            if entry_path:
                full_entry_path = os.path.join(full_source_path, entry_path)
                if not os.path.exists(full_entry_path):
                    await git_connector.close()
                    raise ValueError(
                        f"Helmfile entry '{entry_path}' not found within path '{source_path}' for '{helmfile_name}'"
                    )

            # Copy helmfile content to target directory
            # This includes the helmfile.yaml and all related files (charts, values, etc.)
            dest_helmfile_path = target_path
            os.makedirs(dest_helmfile_path, exist_ok=True)

            # Copy all files from source to destination
            # Skip .git directory to prevent Git from treating the target as a submodule
            for item in os.listdir(full_source_path):
                if item == ".git":
                    continue
                src_item = os.path.join(full_source_path, item)
                dst_item = os.path.join(dest_helmfile_path, item)
                if os.path.isdir(src_item):
                    shutil.copytree(src_item, dst_item, dirs_exist_ok=True)
                else:
                    shutil.copy2(src_item, dst_item)

            logger.info(f"Copied helmfile source from {full_source_path} to {dest_helmfile_path}")
            if entry_path:
                logger.info(f"Helmfile entry point: {entry_path}")

            await git_connector.close()

        return dest_helmfile_path, entry_path

    async def _process_helmfile_deployment(
        self,
        deployment: dict[str, Any],
        git_connector: GitConnector,
        target_path: str,
    ) -> None:
        """
        Process a deployment that uses helmfile instead of components or helm-charts.

        This method:
        1. Clones the helmfile source from git
        2. Extracts and merges helm-values (base + deployment-specific)
        3. Resolves $ALIAS references using service credentials
        4. Creates values.sops.yaml with resolved values (CMP decrypts at runtime)
        5. Creates service secret manifests (database, minio, redis, keycloak)

        The CMP plugin will:
        - Detect helmfile.yaml in the directory
        - Decrypt values.sops.yaml -> values.yaml
        - Run: helmfile template --values values.yaml
        - Output rendered manifests to ArgoCD

        Args:
            deployment: Deployment configuration
            git_connector: Git connector for the target repository
            target_path: Full path to the deployment directory
        """
        project_data = await self.get_contents()
        deployment_name = deployment.get("name")
        if not deployment_name:
            raise ValueError("Deployment missing required 'name' field")

        cluster_name = deployment.get("cluster", settings.CLUSTER_MANAGER)
        prefixed_namespace = get_prefixed_namespace(cluster_name, deployment["namespace"])

        logger.info(f"Processing helmfile deployment: {deployment_name}")

        # Ensure target directory exists
        os.makedirs(target_path, exist_ok=True)

        # Get helmfile references from deployment
        helmfile_refs = deployment.get("helmfile", [])

        # Build the credentials context for alias resolution
        context = await self._get_helm_values_context(deployment_name)

        # Add namespace to context for alias resolution
        context["NAMESPACE"] = prefixed_namespace

        # Process each helmfile reference
        for helmfile_ref in helmfile_refs:
            helmfile_reference = helmfile_ref.get("reference")
            if not helmfile_reference:
                raise ValueError("Helmfile reference missing required 'reference' field")

            logger.info(f"Processing helmfile reference: {helmfile_reference}")

            # Find the helmfile definition in project
            helmfile_def = self._project_file_handler.get_helmfile_by_name(project_data, helmfile_reference)
            if not helmfile_def:
                raise ValueError(f"Helmfile '{helmfile_reference}' not found in project definition")

            # Clone the helmfile source to target directory
            _, entry_path = await self._clone_helmfile_source(helmfile_def, target_path)

            # Write helmfile entry config for CMP to use
            if entry_path:
                helmfile_config_path = os.path.join(target_path, ".helmfile-entry")
                with open(helmfile_config_path, "w") as f:
                    f.write(entry_path)
                logger.info(f"Created helmfile entry config: {helmfile_config_path} -> {entry_path}")

            # Write .cmp-env file with environment variables for the CMP
            cmp_env_path = os.path.join(target_path, ".cmp-env")
            cmp_env_vars: list[str] = []

            # Add env-vars from the deployment's helmfile reference
            # These can be plain text or AGE-encrypted values
            env_vars = helmfile_ref.get("env-vars", {})
            if env_vars and isinstance(env_vars, dict):
                for key, value in env_vars.items():
                    # Decrypt if value is AGE-encrypted
                    if isinstance(value, str) and "-----BEGIN AGE ENCRYPTED FILE-----" in value:
                        decrypted_value = await decrypt_age_content(value, private_key)
                        cmp_env_vars.append(f"{key}={decrypted_value}")
                    else:
                        cmp_env_vars.append(f"{key}={value}")

            if cmp_env_vars:
                with open(cmp_env_path, "w") as f:
                    f.write("\n".join(cmp_env_vars) + "\n")
                logger.info(f"Created CMP environment file: {cmp_env_path}")

            # Extract base helm-values from helmfile definition
            base_values = await self._project_file_handler.extract_helmfile_values(project_data, helmfile_reference)

            # Extract deployment-level helm-values
            deployment_values = await self._project_file_handler.extract_deployment_helmfile_values(
                project_data, deployment_name, helmfile_reference
            )

            # Deep merge values (deployment overrides base)
            merged_values = self._deep_merge_dicts(base_values, deployment_values)

            # Resolve $ALIAS references in the merged values
            resolved_values = self._resolve_nested_aliases(merged_values, context)

            # Write values as .to-sops.yaml (will be encrypted later)
            # CMP plugin looks for values.sops.yaml in helmfile directories
            values_file_to_sops = "values.to-sops.yaml"
            values_path = os.path.join(target_path, values_file_to_sops)

            yaml = YAML()
            yaml.default_flow_style = False
            with open(values_path, "w") as f:
                yaml.dump(resolved_values, f)

            logger.info(f"Created helmfile values file (to be encrypted): {values_path}")

            # Write custom files defined in project (base) and deployment (override)
            # This allows users to override helmfile.yaml.gotmpl or add other files
            custom_files = self._write_helmfile_custom_files(helmfile_def, helmfile_ref, target_path)
            if custom_files:
                logger.info(f"Wrote {len(custom_files)} custom file(s) for helmfile deployment")

        # Create service secret manifests (database, minio, redis, keycloak)
        secret_files = await self._create_deployment_secrets(
            deployment_name,
            target_path,
            prefixed_namespace,
            cluster_name,
        )
        logger.info(f"Created {len(secret_files)} service secret manifests for helmfile deployment")

        # Create Let's Encrypt Issuer manifest if configured
        regular_files: list[str] = []
        issuer_config = deployment.get("issuer")
        base_domain = deployment.get("base-domain")

        if issuer_config and issuer_config in ("letsencrypt", "letsencrypt-staging") and base_domain:
            project_contact_email = project_data.get("config", {}).get("contact-email")
            cluster_contact_email = get_letsencrypt_contact_email(cluster_name)
            contact_email = project_contact_email or cluster_contact_email

            if contact_email:
                issuer_template_path = os.path.join(
                    os.path.dirname(__file__), "..", "..", "manifests", "issuer-letsencrypt.yaml.jinja"
                )

                issuer_name_generated = generate_issuer_name(base_domain, issuer_config, deployment_name)
                issuer_secret_name = generate_issuer_secret_name(base_domain, issuer_config, deployment_name)
                issuer_manifest_filename = generate_issuer_manifest_name(
                    base_domain, issuer_config, deployment_name
                ).replace(".yaml", "")

                issuer_variables = {
                    "issuer_name": issuer_name_generated,
                    "issuer_secret_name": issuer_secret_name,
                    "contact_email": contact_email,
                    "staging": issuer_config == "letsencrypt-staging",
                    "namespace": prefixed_namespace,
                }

                issuer_manifest_path = self._manifest_generator.create_manifest_file(
                    template_path=issuer_template_path,
                    values=issuer_variables,
                    output_dir=target_path,
                    output_filename=issuer_manifest_filename,
                    use_sops=False,
                )

                regular_files.append(f"{issuer_manifest_filename}.yaml")
                logger.info(f"Created Let's Encrypt Issuer manifest for {base_domain}: {issuer_manifest_path}")

                # Create network policy for ACME HTTP-01 challenge
                network_policy_template_path = os.path.join(
                    os.path.dirname(__file__), "..", "..", "manifests", "network-policy.yaml.jinja"
                )
                network_policy_filename = generate_network_policy_manifest_name("acme-http", deployment_name)
                network_policy_variables = {
                    "name": generate_network_policy_name("acme-http", deployment_name),
                    "namespace": prefixed_namespace,
                    "pod_selector": None,
                    "ports": [80, 8089],  # 80 for ingress, 8089 for ACME solver pod
                }
                network_policy_path = self._manifest_generator.create_manifest_file(
                    template_path=network_policy_template_path,
                    values=network_policy_variables,
                    output_dir=target_path,
                    output_filename=network_policy_filename,
                    use_sops=False,
                )
                regular_files.append(f"{network_policy_filename}.yaml")
                logger.info(f"Created HTTP ingress network policy for ACME challenge: {network_policy_path}")
            else:
                logger.warning(f"Cannot create Let's Encrypt Issuer for {base_domain}: no contact email configured")

        # Create kustomization.yaml for additional resources (Issuer, NetworkPolicy, Secrets)
        # The CMP plugin will run BOTH kustomize build AND helmfile template
        # This ensures Let's Encrypt Issuer, secrets, and other resources are applied alongside helmfile output
        # Convert .to-sops.yaml filenames to .sops.yaml (they get encrypted below)
        sops_files = [f.replace(".to-sops.yaml", ".sops.yaml") for f in secret_files]

        if regular_files or sops_files:
            logger.info(
                f"Creating kustomization.yaml for helmfile deployment with "
                f"{len(regular_files)} resources and {len(sops_files)} SOPS files"
            )
            self._manifest_generator.create_kustomization_files(
                output_dir=target_path,
                namespace=prefixed_namespace,
                sops_files=sops_files,  # Include secret SOPS files
                regular_files=regular_files,
                helm_charts=[],  # No helm charts - helmfile handles this
            )
            logger.info(f"Created kustomization.yaml with resources: {regular_files}, sops: {sops_files}")
        else:
            logger.debug("No additional resources for kustomization.yaml, skipping creation")

        # Encrypt .to-sops.yaml files to .sops.yaml
        public_key = get_project_public_key(project_data)
        if not public_key:
            raise ValueError(
                f"No public key found for project, cannot encrypt helm values for deployment: {deployment_name}. "
                "This would commit secrets in plain text to git!"
            )

        logger.info(f"Encrypting helmfile values files for deployment: {deployment_name}")

        # List .to-sops.yaml files before encryption for debugging
        to_sops_pattern = os.path.join(target_path, "*.to-sops.yaml")
        to_sops_files = glob.glob(to_sops_pattern)
        logger.info(f"Found {len(to_sops_files)} .to-sops.yaml files to encrypt:")
        for file_path in to_sops_files:
            logger.info(f"  - {os.path.basename(file_path)}")

        encrypt_to_sops_files_or_fail(
            target_path,
            public_key,
            f"helmfile values-secrets voor deployment '{deployment_name}' (namespace '{prefixed_namespace}')",
            private_key=await self._sops_private_key_for(project_data),
        )

        logger.info("All helmfile values files successfully encrypted")
        logger.info(f"Helmfile deployment processing complete for: {deployment_name}")

    async def _create_deployment_secrets(
        self,
        deployment_name: str,
        target_path: str,
        namespace: str,
        cluster: str,
    ) -> list[str]:
        """
        Create Kubernetes Secret manifests for all services used by a deployment.

        This method creates secret manifests based on what's stored in self._secrets_to_create.
        It's used by both component-based and helm-chart-based deployments.

        Args:
            deployment_name: Name of the deployment
            target_path: Directory where secret manifests should be created
            namespace: Target namespace for the secrets
            cluster: Cluster name for cluster-specific configurations

        Returns:
            List of created secret filenames (*.to-sops.yaml)
        """
        created_files: list[str] = []

        secret_template_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "manifests", "generic-secret.yaml.to-sops.jinja"
        )

        # Create Keycloak/SSO secret if available
        keycloak_secret = self._get_secret_from_map(deployment_name, "keycloak", KeycloakSecret)
        if keycloak_secret:
            keycloak_secret_data = keycloak_secret.to_k8s_secret_data()
            secret_vars = {
                "name": KeycloakSecret.get_secret_name(deployment_name),
                "namespace": namespace,
                "secret_pairs": keycloak_secret_data,
            }
            manifest_name = f"{KeycloakSecret.get_secret_name(deployment_name)}-secret"
            self._manifest_generator.create_manifest_file(
                template_path=secret_template_path,
                values=secret_vars,
                output_dir=target_path,
                output_filename=manifest_name,
                use_sops=True,
            )
            created_files.append(f"{manifest_name}.to-sops.yaml")
            logger.info(f"Created Keycloak secret manifest: {manifest_name}")

        # Create Database secret if available
        db_secret = self._get_secret_from_map(deployment_name, "database", DatabaseSecret)
        if db_secret:
            db_secret_data = db_secret.to_k8s_secret_data()
            secret_vars = {
                "name": DatabaseSecret.get_secret_name(deployment_name),
                "namespace": namespace,
                "secret_pairs": db_secret_data,
            }
            manifest_name = f"{DatabaseSecret.get_secret_name(deployment_name)}-secret"
            self._manifest_generator.create_manifest_file(
                template_path=secret_template_path,
                values=secret_vars,
                output_dir=target_path,
                output_filename=manifest_name,
                use_sops=True,
            )
            created_files.append(f"{manifest_name}.to-sops.yaml")
            logger.info(f"Created Database secret manifest: {manifest_name}")

        # Create MinIO secret if available
        minio_secret = self._get_secret_from_map(deployment_name, "minio", MinIOSecret)
        if minio_secret:
            minio_secret_data = minio_secret.to_k8s_secret_data()
            secret_vars = {
                "name": MinIOSecret.get_secret_name(deployment_name),
                "namespace": namespace,
                "secret_pairs": minio_secret_data,
            }
            manifest_name = f"{MinIOSecret.get_secret_name(deployment_name)}-secret"
            self._manifest_generator.create_manifest_file(
                template_path=secret_template_path,
                values=secret_vars,
                output_dir=target_path,
                output_filename=manifest_name,
                use_sops=True,
            )
            created_files.append(f"{manifest_name}.to-sops.yaml")
            logger.info(f"Created MinIO secret manifest: {manifest_name}")

        # Create Redis secret if available
        redis_secret = self._get_secret_from_map(deployment_name, "redis", RedisSecret)
        if redis_secret:
            redis_secret_data = redis_secret.to_k8s_secret_data()
            secret_vars = {
                "name": RedisSecret.get_secret_name(deployment_name),
                "namespace": namespace,
                "secret_pairs": redis_secret_data,
            }
            manifest_name = f"{RedisSecret.get_secret_name(deployment_name)}-secret"
            self._manifest_generator.create_manifest_file(
                template_path=secret_template_path,
                values=secret_vars,
                output_dir=target_path,
                output_filename=manifest_name,
                use_sops=True,
            )
            created_files.append(f"{manifest_name}.to-sops.yaml")
            logger.info(f"Created Redis secret manifest: {manifest_name}")

        return created_files

    async def _create_helm_kustomization(
        self,
        target_path: str,
        namespace: str,
        helm_charts: list[dict[str, Any]],
        secret_files: list[str] | None = None,
        regular_files: list[str] | None = None,
    ) -> None:
        """
        Create kustomization.yaml and decrypt-sops.yaml files for helm chart deployment.

        Uses the shared create_kustomization_files method from ManifestGenerator.

        Args:
            target_path: Directory where kustomization files should be created
            namespace: Target namespace
            helm_charts: List of helm chart configurations
            secret_files: Optional list of secret manifest files (.to-sops.yaml) to include
            regular_files: Optional list of regular manifest files (e.g., Issuer) to include
        """
        # Collect SOPS files for decryption (Secret manifests only)
        # NOTE: Helm values files are NOT included here - they are decrypted by the
        # CMP plugin's decrypt_helm_values function, not by KSOPS. KSOPS only handles
        # Kubernetes Secret manifests that need to be decrypted and applied as resources.
        all_sops_files: list[str] = []

        if secret_files:
            all_sops_files.extend(secret_files)

        # Use the shared manifest generator method
        self._manifest_generator.create_kustomization_files(
            output_dir=target_path,
            namespace=namespace,
            sops_files=all_sops_files or None,
            regular_files=regular_files or [],
            helm_charts=helm_charts,
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
            self.close_git_connector_for_argocd,
            self.close_git_connectors_for_deployments,
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
        allow_mutable_tag_retry: bool = True,
    ) -> bool:
        """
        Process the project file and create all required resources.

        Args:
            deployment_name: Optional single deployment to process
            force_clone: Force clone even if target resources exist (runtime parameter)
            deployment_names: Optional explicit set of deployments to process
                (takes precedence over ``deployment_name``; empty list = none)

        Returns:
            True if all operations succeeded, False if any operation failed
        """
        logger.info(f"Processing project file: {self._project_file_relative_path}")

        # Store force_clone override for nested calls (e.g., PVC manager)
        self._force_clone_override = force_clone

        targets = _resolve_deployment_filter(deployment_name, deployment_names)

        try:
            project_data = await self.get_contents()
            project_name = await self.get_name()
            logger.info(
                f"Processing project: {project_name} and deployment {targets if targets is not None else 'all'}"
            )

            if not await self.has_deployments_for_current_cluster():
                logger.info(
                    f"Project '{project_name}' has no deployments targeting cluster '{settings.CLUSTER_MANAGER}' - this operations manager only handles deployments for this cluster"
                )
                return False

            # Clear stale image-pull auto-disables so a fixed image can deploy again:
            # when the image reference changed (any edit path), or -- on an explicit
            # user redeploy (allow_mutable_tag_retry) -- when it uses a moving tag like
            # :latest whose string cannot reveal a re-push. Done before generating
            # manifests so the new image is actually deployed. See oom_watcher for the
            # disable side; the automated post-disable refresh passes the flag as False.
            reenabled = self._project_file_handler.reenable_components_with_changed_image(
                project_data, targets, allow_mutable_retry=allow_mutable_tag_retry
            )
            if reenabled:
                reenable_msg = "auto-reenable: image changed for " + ", ".join(f"{d}/{c}" for d, c in reenabled)
                await self.save_and_commit_project(project_data, reenable_msg, enforce_validation=False)

            # # 1.5. Create configuration handler to collect deployment info
            # config_handler = create_configuration_handler(project_name, self.project_data)

            # 2. Extract changes (dummy for now - all marked as "added")
            self._extract_added_changes(project_data)

            # TODO: most likely remove creating repositories
            # 3. Process repositories
            # repositories = self._get_project_repositories(added_changes)
            # missing_repos = await self._get_missing_repositories(repositories)
            # if missing_repos and not await self._create_repositories(missing_repos, project_data):
            #     logger.error("Failed to create repositories, aborting")
            #     return False

            progress_manager = self.get_progress_manager()
            creation_task = None

            if progress_manager:
                creation_task = progress_manager.add_task("Project creation")

            # TODO: consider checking if a deployment needs to be done for this cluster instead of checking per method call

            # Create namespaces first (always first task)
            # TODO: move methods to a kubernetes manager?
            await self.check_and_create_namespaces(deployment_names=targets)
            await self.check_and_create_sops_secrets_in_namespaces(deployment_names=targets)

            # Check if project requires infrastructure namespace (namespace-specific PostgreSQL)
            # This check is infrastructure-level, independent of any manager initialization
            project_services = project_data.get("services", [])
            needs_infrastructure_namespace = any(
                service_item == ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value
                if isinstance(service_item, str)
                else ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value in service_item
                for service_item in (project_services or [])
            )

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
                    await db_manager.create_resources_for_deployment(project_data, deployment, force_clone)
                    await self._minio_manager.create_resources_for_deployment(project_data, deployment, force_clone)
                    await self._keycloak_manager.create_resources_for_deployment(project_data, deployment)
                    await self._redis_manager.create_resources_for_deployment(project_data, deployment)

            # Generate application manifests (including PVC) BEFORE setting clone status.
            # PVC clone relies on clone-from.status.completed being false to include dataSource.
            await self._process_application_manifests(deployment_names=targets)

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

            # Persist the in-memory project data before committing. The loop above
            # set clone-from.status.completed=True, and the clone itself recorded
            # the database generation -- both mutate project_data in memory only.
            # Without this save those mutations never reach the file, so every
            # reconcile re-reads completed=false / generation=None and re-clones,
            # creating a fresh full copy each pass. Mirrors every other write flow.
            await self.save_project_data()

            scope = f" (deployment: {deployment_name})" if deployment_name else ""
            await (await self.get_git_connector_for_project_files()).commit_and_push(
                f"Process project {project_name}{scope}"
            )

            await self._argo_manager.create_argocd_resources(deployment_names=targets)

            # Execute bootstrap actions for deployments
            for deployment in deployments:
                if deployment.get("cluster") == settings.CLUSTER_MANAGER:
                    await self._bootstrap_manager.execute_bootstrap_for_deployment(project_data, deployment)

            # Register the project with the original (encrypted) data.
            # The web UI does its own decryption for display via deepcopy.
            api_key = await self.get_api_key()
            project_name = await self.get_name()
            project_service = get_project_service()
            filename = (
                os.path.basename(self._project_file_relative_path)
                if self._project_file_relative_path
                else f"{project_name}.yaml"
            )

            project_data_with_configs = await self.get_contents()

            # Extract users from project data
            users_data = project_data_with_configs.get("users", [])
            users = []
            if users_data and isinstance(users_data, list):
                users.extend(
                    ProjectUser(email=user_data["email"], role=user_data["role"])
                    for user_data in users_data
                    if isinstance(user_data, dict) and "email" in user_data and "role" in user_data
                )

            project_service.register(
                project_name,
                api_key,
                filename,
                users=users or None,
                data=project_data_with_configs,
            )

            if progress_manager and creation_task:
                self.get_progress_manager().complete_task(creation_task)

            self._processing_error = None
            self._component_failures = None
            return True
        except Exception as e:
            logger.exception(f"Error processing project: {e}")
            self._processing_error = str(e)
            self._processing_exception = e
            return False
        finally:
            pass
            # TODO: we may need to close it here, but the project manager is still used in a flow which should change
            # await self.close()

    async def create_application_manifests(
        self,
        deployment: dict[str, Any],
        git_connector: GitConnector,
        target_dir: str | None = None,
    ) -> list[str]:
        """
        Create application manifests (deployment, service, ingress) in the git repository.
        This creates the application manifest files for each component in each deployment.

        Args:
            deployment: current deployment
            git_connector: The git connector with an already cloned repository
            target_dir: Optional subdirectory within the git repository

        Returns:
            List of created manifest filenames, empty list if failed
        """
        project_data = await self.get_contents()
        working_dir = await git_connector.get_working_dir()

        project_name = await self.get_name()
        logger.info(f"Creating application manifests for project: {project_name}")

        created_files = []

        deployment_name = deployment["name"]
        cluster = deployment["cluster"]
        namespace = get_prefixed_namespace(cluster, deployment["namespace"])

        # Initialize deployment result tracking
        if deployment_name not in self._deployment_results:
            self._deployment_results[deployment_name] = DeploymentResult(
                deployment_name=deployment_name,
                cluster=cluster,
                namespace=namespace,
            )

        logger.info(f"Processing deployment: {deployment_name} in prefixed namespace: {namespace}")

        # Check if deployment has components
        components = deployment.get("components", [])
        if not components:
            logger.warning(f"No components found in deployment {deployment_name}, skipping")
            return []

        # Register subdomain for nice-url mode (with rollback on failure)
        domain_mode = deployment.get("domain-mode")
        subdomain = deployment.get("subdomain")
        base_domain = deployment.get("base-domain")
        subdomain_registered = False  # Track if we registered a new subdomain for rollback
        subdomain_connector = None  # Initialize for use in rollback

        # Validate nice-url mode requirements BEFORE subdomain registration
        # This prevents orphaned subdomain entries when validation fails
        root_component_name = deployment.get("root-component")
        if domain_mode == "nice-url" and subdomain and base_domain:
            # Validate that all components with publish-on-web have ports configured
            # In nice-url mode, each component gets its own ingress at component.subdomain.base_domain
            components_missing_ports = []
            for component in components:
                component_name = component.get("reference") or component.get("name")
                if not component_name:
                    continue

                # Check if component has publish-on-web enabled
                has_publish_on_web = self._project_file_handler.extract_component_publish_on_web(
                    project_data, component_name
                )
                if has_publish_on_web:
                    # Component needs a port for its ingress
                    has_ports = self._project_file_handler.component_has_ports(project_data, component_name)
                    if not has_ports:
                        components_missing_ports.append(component_name)

            if components_missing_ports:
                raise ValueError(
                    f"Components with 'publish-on-web' in nice-url mode must have at least one port configured. "
                    f"Missing ports for: {', '.join(components_missing_ports)}. "
                    f"Add 'ports.inbound' to the component definition."
                )

            # Validate that root component has publish-on-web (required for root ingress creation)
            if root_component_name:
                root_has_publish_on_web = self._project_file_handler.extract_component_publish_on_web(
                    project_data, root_component_name
                )
                if not root_has_publish_on_web:
                    raise ValueError(
                        f"Component '{root_component_name}' is marked as root but does not have 'publish-on-web' enabled. "
                        f"Root components must have a service exposed for the root URL to work."
                    )

        if domain_mode == "nice-url" and subdomain and base_domain:
            subdomain_connector = SubdomainConnector()

            # Check if this is a new registration (for rollback purposes)
            existing_registration = await subdomain_connector.get_by_deployment(project_name, deployment_name)
            is_new_registration = existing_registration is None

            # Use register_or_update to handle both new registrations and subdomain changes
            # This method:
            # 1. Checks if deployment already has a registration
            # 2. If subdomain unchanged, returns existing
            # 3. If subdomain changed, deletes old and registers new
            # 4. Raises appropriate errors on validation/availability issues
            await subdomain_connector.register_or_update_for_deployment(
                subdomain=subdomain,
                base_domain=base_domain,
                project_name=project_name,
                deployment_name=deployment_name,
                cluster=cluster,
                created_by=None,  # Could be enhanced to track user in future
            )
            subdomain_registered = is_new_registration  # Mark for rollback only if new
            logger.info(f"Subdomain '{subdomain}.{base_domain}' registered/updated for project '{project_name}'")

        # Register or clean up bare domain in subdomain registry
        expose_on_bare_domain = deployment.get("expose-component-on-bare-domain")
        bare_domain_registered = False
        if expose_on_bare_domain and base_domain:
            if subdomain_connector is None:
                subdomain_connector = SubdomainConnector()
            await subdomain_connector.register_bare_domain(
                base_domain=base_domain,
                project_name=project_name,
                deployment_name=deployment_name,
                cluster=cluster,
            )
            bare_domain_registered = True
            logger.info(f"Bare domain '{base_domain}' registered for project '{project_name}'")
        elif base_domain and not expose_on_bare_domain:
            # Bare domain deselected — clean up any existing registration
            if subdomain_connector is None:
                subdomain_connector = SubdomainConnector()
            deleted = await subdomain_connector.delete_bare_domain(base_domain)
            if deleted:
                logger.info(f"Bare domain '{base_domain}' deregistered for project '{project_name}'")

        # Store rollback info for use in exception handler
        # These variables are used by _rollback_subdomain_on_failure if needed
        should_rollback = subdomain_registered or bare_domain_registered
        self._pending_subdomain_rollback = (
            {
                "should_rollback": should_rollback,
                "connector": subdomain_connector,
                "project_name": project_name,
                "deployment_name": deployment_name,
                "subdomain": subdomain,
                "base_domain": base_domain,
            }
            if should_rollback
            else None
        )

        # Collect registry configurations for all components in this deployment
        registry_configs_map: dict[str, dict[str, Any]] = {}  # registry_name -> registry_config
        image_to_registry_map: dict[str, str] = {}  # image_url -> registry_name

        for component in components:
            component_reference = component.get("reference")
            image_url = component.get("image")

            if not component_reference or not image_url:
                continue

            # Check if component has a registry configured at deployment level
            # Registry reference is specified in deployments[].components[].registry
            registry_ref = component.get("registry")

            if registry_ref:
                # Find registry by name in registries list
                registries = self._project_file_handler.extract_registries(project_data)
                registry_config = None

                for registry in registries:
                    if registry.get("name") == registry_ref:
                        registry_config = registry
                        logger.info(f"Deployment component '{component_reference}' uses registry '{registry_ref}'")
                        break

                if not registry_config:
                    logger.warning(
                        f"Deployment component '{component_reference}' references registry '{registry_ref}' which does not exist"
                    )
                    continue

                registry_name = registry_config.get("name")
                if registry_name:
                    # Store unique registry configs
                    if registry_name not in registry_configs_map:
                        registry_configs_map[registry_name] = registry_config

                    # Map this image to its registry
                    image_to_registry_map[image_url] = registry_name

        # Create registry secrets and build imagePullSecretsMap
        image_pull_secrets_map: dict[str, str] = {}  # image_url -> secret_name

        for registry_name, registry_config in registry_configs_map.items():
            registry_url = registry_config.get("url", "")
            secret_name_ref = registry_config.get("secretName")

            if secret_name_ref:
                # Pre-existing secret: use directly, skip creation
                secret_name = secret_name_ref
                logger.info(f"Registry '{registry_name}' uses pre-existing secret '{secret_name}' ({registry_url})")
            else:
                # Credential-based: decrypt and create RegistrySecret
                username = registry_config.get("username", "")
                password_encrypted = registry_config.get("password", "")

                # Decrypt password (should be AGE-encrypted)
                private_key = await get_decoded_project_private_key(project_data)
                password = await decrypt_password_smart(password_encrypted, private_key)

                # Generate secret name using naming utility
                secret_name = generate_registry_secret_name(deployment_name, registry_name)

                # Create RegistrySecret instance
                registry_secret = RegistrySecret(registry_url=registry_url, username=username, password=password)

                # Add to secrets to be created (using generic secret template with dockerconfigjson type)
                self._add_secret_to_create(deployment_name, registry_name, registry_secret)

                logger.info(f"Created registry secret '{secret_name}' for registry '{registry_name}' ({registry_url})")

            # Map all images using this registry to the secret name
            for image_url, img_registry_name in image_to_registry_map.items():
                if img_registry_name == registry_name:
                    image_pull_secrets_map[image_url] = secret_name

        # Track created issuers to avoid duplicates (per base-domain/issuer combination)
        created_issuers: set[str] = set()

        # Process each component within the deployment
        for component in components:
            # Get component reference and image from deployment
            component_reference = component.get("reference")
            image_url = component.get("image", "")

            if not component_reference:
                logger.warning(f"Component missing reference in deployment {deployment_name}, skipping")
                continue

            if not image_url:
                logger.info(f"Component '{component_reference}' has no image in deployment {deployment_name}, skipping")
                continue

            component_name = component_reference

            # Update component deployment name if progress manager is available
            progress_manager = self.get_progress_manager()
            if progress_manager:
                deployment_resource_name = f"{project_name}-{component_name}"
                progress_manager.update_component_deployment(component_name, deployment_resource_name)
                logger.debug(f"Updated component {component_name} deployment name to {deployment_resource_name}")

            # Extract the application port from the component definition using the file handler
            application_port = self._project_file_handler.extract_component_port(
                project_data, component_reference, default_port=80
            )

            # Extract health-probe configuration (scheme + readiness/liveness paths).
            # scheme=tcp keeps the plain tcpSocket probe; http/https switch to httpGet
            # so a TLS-serving app completes the handshake instead of logging a failed one.
            probe_config = self._project_file_handler.extract_component_probe(project_data, component_reference)

            # Extract publication paths: deployment-level overrides component-level
            component_paths = self._project_file_handler.extract_deployment_component_paths(
                project_data, deployment, component_reference
            )
            # For backward compatibility, use first path as the primary path
            component_path = component_paths[0]["match"] if component_paths else "/"

            # Extract imagePullPolicy from deployment-level component configuration (not component definition)
            # This allows overriding the pull policy per deployment
            # Resolve image + pull policy via the shared helper (handles the
            # local/ Kind-image special); one implementation across deployments,
            # the database console and jobs.
            image_url, image_pull_policy = resolve_image(image_url, component.get("imagePullPolicy", "Always"))

            # Extract storage configuration from component
            storage_configs = self._project_file_handler.extract_component_storage(project_data, component_reference)

            # Extract publish-on-web flag from component
            publish_on_web = self._project_file_handler.extract_component_publish_on_web(
                project_data, component_reference
            )

            # Extract optional per-component security context override (sandbox-only).
            # Hidden YAML-only feature — defaults to None (template falls back to 1001/1001/1001).
            component_security = self._project_file_handler.extract_component_security(
                project_data, component_reference
            )

            # Extract optional container `command` override (K8s containers[].command).
            # Hidden YAML-only feature: deployment-level override wins over the
            # component-level default; both default to None so the image's own
            # ENTRYPOINT/CMD remains in effect when neither is set.
            command_override = self._project_file_handler.extract_deployment_component_command(
                project_data, deployment_name, component_reference
            )
            if command_override is None:
                command_override = self._project_file_handler.extract_component_command(
                    project_data, component_reference
                )

            # Extract resource configuration (component-level, then deployment-level overrides)
            component_resources = self._project_file_handler.extract_component_resources(
                project_data, component_reference
            )
            deployment_resources = self._project_file_handler.extract_deployment_component_resources(
                project_data, deployment_name, component_reference
            )
            if deployment_resources:
                logger.info(f"Found deployment-level resource overrides for component: {component_name}")
                component_resources.update(deployment_resources)

            # Extract disabled state - disabled components get replicas: 0
            # Check deployment-level first, falls back to component-definition level
            is_disabled, disabled_reason = self._project_file_handler.extract_deployment_component_disabled(
                project_data, deployment_name, component_reference
            )
            replicas = 0 if is_disabled else 1
            if is_disabled:
                logger.info(f"Component '{component_name}' is disabled (reason: {disabled_reason}), setting replicas=0")

            # Extract user environment variables from component definition
            user_env_vars = await self._project_file_handler.extract_component_user_env_vars(
                project_data, component_reference
            )

            # Extract deployment-level user-env-vars and merge (deployment takes precedence)
            deployment_user_env_vars = await self._project_file_handler.extract_deployment_component_user_env_vars(
                project_data, deployment_name, component_reference
            )
            if deployment_user_env_vars:
                logger.info(
                    f"Found {len(deployment_user_env_vars)} deployment-level user-env-vars for component: {component_name}"
                )
                # Deployment-level user-env-vars override component-level user-env-vars
                user_env_vars.update(deployment_user_env_vars)

            # Extract deployment-level env-vars (plaintext) and merge with user-env-vars
            deployment_env_vars = component.get("env-vars", {})
            if deployment_env_vars:
                logger.info(
                    f"Found {len(deployment_env_vars)} deployment-level env-vars for component: {component_name}"
                )
                # Deployment-level env-vars override all other env-vars
                user_env_vars.update(deployment_env_vars)

            # Resolve attachment uses for this component: file mounts and env-var content.
            # File-mode attachments become a binary Secret mounted read-only at a path; env-var
            # mode (text only) is injected via the existing user secret / envFrom path.
            attachment_secret_mounts: list[dict[str, str]] = []
            attachment_file_secrets: dict[str, dict[str, str]] = {}
            resolved_attachments = await self._project_file_handler.resolve_attachments_for_component(
                project_data, component_reference, deployment_name=deployment_name
            )
            for att in resolved_attachments:
                reference = att["reference"]
                if att["provide-as"] == "env-var":
                    # Content was verified UTF-8 decodable in resolve(); expose as an env-var value.
                    user_env_vars[att["env-name"]] = att["content_bytes"].decode("utf-8")
                else:
                    secret_name = f"{deployment_name}-attch-{reference}"
                    attachment_file_secrets[secret_name] = {
                        reference: base64.b64encode(att["content_bytes"]).decode("ascii")
                    }
                    attachment_secret_mounts.append(
                        {
                            "name": f"attch-{reference}",
                            "secret_name": secret_name,
                            "mount_path": att["path"],
                            "sub_path": reference,
                        }
                    )

            # Create unique name combining deployment name and component name using centralized utility
            # Project name is not included since resources are deployed within project-specific namespaces
            unique_name = generate_unique_name(deployment_name, component_name)

            # Add unique names to storage configs for templating
            processed_storage_configs = []
            for i, storage in enumerate(storage_configs):
                storage_copy = storage.copy()
                # Generate unique storage name based on mount path or index using centralized utility
                mount_path = storage.get("mount-path", f"/storage-{i}")
                storage_name = generate_storage_name(mount_path, i)
                storage_copy["name"] = storage_name

                # For persistent storage, add the versioned PVC name
                if storage.get("type") == "persistent":
                    # Get generation for this storage from project data
                    generation = self._project_file_handler.get_storage_generation(
                        project_data, deployment_name, component_name, storage_name
                    )
                    # Generate versioned PVC name
                    pvc_name = generate_pvc_name(unique_name, storage_name, generation)
                    storage_copy["pvc_name"] = pvc_name

                processed_storage_configs.append(storage_copy)

            # Generate ingress map based on cluster configuration and optional subdomain using centralized utility
            ingress_postfix = get_ingress_postfix(cluster)
            use_https = get_ingress_tls_enabled(cluster)
            subdomain = deployment.get("subdomain")
            base_domain = deployment.get("base-domain")
            issuer_config = deployment.get("issuer")
            domain_mode = deployment.get("domain-mode")
            domain_format = deployment.get("domain-format")
            expose_on_bare_domain = deployment.get("expose-component-on-bare-domain", False)
            logger.info(
                f"Extracted subdomain for {component_name}: {subdomain}, base-domain: {base_domain}, "
                f"issuer: {issuer_config}, domain-mode: {domain_mode}, domain-format: {domain_format}, "
                f"expose-component-on-bare-domain: {expose_on_bare_domain}"
            )

            # Get ingress map using centralized function
            hostname_format = HostnameFormat.from_domain_mode(domain_mode)
            ingress_map = get_component_ingress_map(
                component_name=component_name,
                deployment_name=deployment_name,
                project_name=project_name,
                ingress_postfix=ingress_postfix,
                subdomain=subdomain,
                base_domain=base_domain,
                hostname_format=hostname_format,
                domain_format=domain_format,
                project_data=project_data,
                cluster=settings.CLUSTER_MANAGER,
            )
            hostname = next(iter(ingress_map.values()))

            logger.info(f"Generated ingress_map for {component_name}: {ingress_map}")
            logger.info(f"Primary hostname for {component_name}: {hostname}")

            # Track component URL in deployment results
            if hostname:
                web_address = generate_public_url(hostname, use_https)
                self._deployment_results[deployment_name].urls[component_name] = web_address
                logger.debug(f"Tracked component {component_name} URL: {web_address}")

                # Also update progress manager if available (for background task UI)
                if progress_manager:
                    progress_manager.update_component_web_address(component_name, web_address)

            # Generate environment variables using service-based registration pattern
            env_vars = {}

            # Register storage environment variables using service definitions
            if storage_configs:
                storage_env_vars = self._generate_storage_env_vars_from_services(processed_storage_configs)
                if storage_env_vars:
                    env_vars.update(storage_env_vars)

            # Register publish-on-web environment variables using service definitions
            if publish_on_web and hostname:
                web_env_vars = self._generate_web_env_vars_from_services(hostname, use_https)
                if web_env_vars:
                    env_vars.update(web_env_vars)

            # Resolve and add direct aliases (aliases that reference direct env vars)
            # These are resolved per-component using the env_vars available to this component
            direct_aliases = self._deployment_aliases.get(deployment_name, {}).get("direct", {})
            for service_category, service_aliases in direct_aliases.items():
                if service_aliases:
                    logger.debug(
                        f"Resolving {len(service_aliases)} direct {service_category} aliases for component {component_name}"
                    )
                    # Resolve aliases using current env_vars as context
                    resolved_direct_aliases = self._resolve_aliases(service_aliases, env_vars)
                    # Add resolved aliases to env_vars
                    env_vars.update(resolved_direct_aliases)
                    logger.info(
                        f"Added {len(resolved_direct_aliases)} resolved direct {service_category} aliases to component {component_name}"
                    )

            # Register user environment variables
            # NOTE: User env vars go into a secret and are referenced via envFrom, not as direct env vars
            if user_env_vars:
                # Substitute PUBLIC_HOST / PUBLIC_HOSTNAME in user-env-vars if referenced
                # NOTE: This is a simple substitution for these two direct vars only. If we need
                # to support more direct variables in user-env-vars in the future, consider
                # extending the alias system to support "direct" source variables.
                # IMPORTANT: substitute PUBLIC_HOSTNAME before PUBLIC_HOST so the longer name
                # is matched first and isn't partially consumed by the PUBLIC_HOST replacement.
                public_host: str | None = env_vars.get("PUBLIC_HOST")
                public_hostname: str | None = env_vars.get("PUBLIC_HOSTNAME")
                if public_host or public_hostname:
                    substituted_user_env_vars: dict[str, Any] = {}
                    for key, value in user_env_vars.items():
                        if isinstance(value, str) and (
                            "$PUBLIC_HOST" in value or "${PUBLIC_HOST}" in value or "${PUBLIC_HOSTNAME}" in value
                        ):
                            substituted_value = value
                            if public_hostname:
                                substituted_value = substituted_value.replace("${PUBLIC_HOSTNAME}", public_hostname)
                                substituted_value = substituted_value.replace("$PUBLIC_HOSTNAME", public_hostname)
                            if public_host:
                                substituted_value = substituted_value.replace("${PUBLIC_HOST}", public_host)
                                substituted_value = substituted_value.replace("$PUBLIC_HOST", public_host)
                            substituted_user_env_vars[key] = substituted_value
                            if substituted_value != value:
                                logger.debug(
                                    f"Substituted PUBLIC_HOST/HOSTNAME in user-env-var {key}: "
                                    f"{value} -> {substituted_value}"
                                )
                        else:
                            substituted_user_env_vars[key] = value
                    user_env_vars = substituted_user_env_vars

            # # IMPORTANT: Add component FIRST to prevent fallback creation with namespace=None
            # if config_handler:
            #     logger.debug(f"Config DEBUG: Adding component {component_name} with namespace: {namespace}")
            #     config_handler.add_component(component_name, "component", namespace)

            # Determine which services this component uses (check once, use multiple times)
            component_uses_postgresql = False
            component_uses_minio = False
            component_uses_sso = False
            component_uses_redis = False
            component_uses_authorization_wall = False
            component_uses_metrics_scraper = False
            component_def = None
            metrics_config = {"port": None, "path": None}

            if component_reference:
                component_def = self._project_file_handler._find_component(project_data, component_reference)
                all_services: list[str] = extract_service_names_from_component(component_def) if component_def else []

                # Check for both postgresql-database (shared) and namespace-postgresql-database (dedicated)
                component_uses_postgresql = (
                    ServiceType.POSTGRESQL_DATABASE.value in all_services
                    or ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value in all_services
                )
                component_uses_minio = ServiceType.MINIO_STORAGE.value in all_services
                component_uses_sso = ServiceType.KEYCLOAK.value in all_services
                component_uses_redis = (
                    ServiceType.REDIS.value in all_services or ServiceType.NAMESPACE_REDIS.value in all_services
                )
                component_uses_authorization_wall = ServiceType.AUTHORIZATION_WALL.value in all_services
                component_uses_metrics_scraper = ServiceType.METRICS_SCRAPER.value in all_services

            # Build envFrom secrets list based on services used and user env vars
            # This list determines which secrets are referenced in the deployment manifest
            # Note: Secret FILES are only generated if the secret is in _secrets_to_create map
            env_from_secrets = []

            # Add deployment-level secrets based on services used
            if component_uses_postgresql:
                database_secret_name = DatabaseSecret.get_secret_name(deployment_name)
                env_from_secrets.append(database_secret_name)
                logger.debug(f"Database secret added to envFrom: {database_secret_name}")

            if component_uses_minio:
                minio_secret_name = MinIOSecret.get_secret_name(deployment_name)
                env_from_secrets.append(minio_secret_name)
                logger.debug(f"MinIO secret added to envFrom: {minio_secret_name}")

            if component_uses_sso:
                keycloak_secret_name = KeycloakSecret.get_secret_name(deployment_name)
                env_from_secrets.append(keycloak_secret_name)
                logger.debug(f"Keycloak secret added to envFrom: {keycloak_secret_name}")

            if component_uses_redis:
                redis_secret_name = RedisSecret.get_secret_name(deployment_name)
                env_from_secrets.append(redis_secret_name)
                logger.debug(f"Redis secret added to envFrom: {redis_secret_name}")

            if component_uses_metrics_scraper:
                metrics_auth_secret_name = MetricsAuthSecret.get_secret_name(deployment_name)
                env_from_secrets.append(metrics_auth_secret_name)
                logger.debug(f"Metrics auth secret added to envFrom: {metrics_auth_secret_name}")

            # Platform secret (always present, per-component)
            platform_secret_name = PlatformSecret.get_secret_name(unique_name)
            env_from_secrets.append(platform_secret_name)
            logger.debug(f"Platform secret added to envFrom: {platform_secret_name}")

            # Add component-level user secret if user env vars exist
            if user_env_vars:
                logger.info(
                    f"Processing {len(user_env_vars)} user environment variables for component: {component_name}"
                )
                user_secret_name = UserSecret.get_secret_name(unique_name)
                env_from_secrets.append(user_secret_name)
                logger.debug(f"User secret added to envFrom: {user_secret_name}")

            # NOTE: SSO integration is now handled at deployment level by keycloak_manager
            # in create_resources_for_deployment(), not per-component here

            pod_replacement_mode = (
                "Recreate" if any(item.get("type") == "persistent" for item in storage_configs) else "RollingUpdate"
            )

            # Prepare variables for templating
            # Generate timestamp for pod annotation to force restart when secrets change
            generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

            variables = {
                "name": unique_name,
                "deployment_name": deployment_name,
                "component_name": component_name,
                "namespace": namespace,
                "hostname": hostname,
                "project": {"name": project_name},
                "cluster": cluster,  # Add cluster information for template conditionals
                "pod_replacement_mode": pod_replacement_mode,
                "imageURL": image_url,
                "imagePullPolicy": image_pull_policy,  # Image pull policy (Always, IfNotPresent, Never)
                "application_port": application_port,
                "service_port": application_port,  # Use same port for service by default
                "probe_scheme": probe_config["scheme"],  # tcp | http | https
                "probe_readiness_path": probe_config["readiness_path"],
                "probe_liveness_path": probe_config["liveness_path"],
                "path": component_path,  # Publication path for ingress routing
                "storage_configs": processed_storage_configs,
                "env_vars": env_vars,
                "env_from_secrets": env_from_secrets,  # List of secrets for envFrom
                "attachment_secret_mounts": attachment_secret_mounts,  # File-mode attachment volume mounts
                # Cluster-specific ingress configuration
                "enable_tls": get_ingress_tls_enabled(cluster),
                "ip_whitelist": get_ingress_ip_whitelist(cluster),
                # Registry authentication
                "imagePullSecretsMap": image_pull_secrets_map,  # Map of image URLs to registry secret names
                # Timestamp to force pod restart when secrets are regenerated
                "generated_at": generated_at,
                # CA certificate configuration for SSL/TLS
                "ca_config": get_ca_certificate_config(cluster),
                # Prometheus metrics configuration (passed to template if metrics-scraper service enabled)
                "metrics_config": metrics_config if component_uses_metrics_scraper else None,
                # Resource configuration (requests and limits)
                "resources_requests_memory": component_resources["requests_memory"],
                "resources_requests_cpu": component_resources["requests_cpu"],
                "resources_limits_memory": component_resources["limits_memory"],
                "resources_limits_cpu": component_resources["limits_cpu"],
                # Replicas (0 when component is disabled)
                "replicas": replicas,
                # Optional per-component pod securityContext override (sandbox-only).
                # Template reads K8s-native field names (runAsUser/runAsGroup/fsGroup).
                "security": (
                    {
                        "runAsUser": component_security.get("run-as-user"),
                        "runAsGroup": component_security.get("run-as-group"),
                        "fsGroup": component_security.get("fs-group"),
                    }
                    if component_security
                    else None
                ),
                # Optional container command override (K8s containers[].command).
                # None => template omits the field => image's ENTRYPOINT/CMD stays.
                "command": command_override,
            }

            logger.info(f"Creating manifests for component: {component_name} with image: {image_url}")

            # Collect additional configuration information if handler is provided
            # if config_handler:
            # Component was already added above before SSO processing

            # Add environment variables
            # for env_key, env_value in env_vars.items():
            #     config_handler.add_env_var(component_name, env_key, str(env_value))
            #
            # # Add web address (hostname) only if publish-on-web is enabled
            # if publish_on_web:
            #     web_address = f"https://{hostname}"
            #     logger.info(f"Adding web address to config: {component_name} -> {web_address}")
            #     config_handler.add_web_address(component_name, web_address)
            # else:
            #     logger.debug(f"Skipping web address for {deployment_name}-{component_name} (publish-on-web: false)")

            # Add storage configuration
            # for storage_config in processed_storage_configs:
            #     config_handler.add_storage_config(
            #         component_name,
            #         "pvc",
            #         {
            #             "name": storage_config.get("name"),
            #             "mount_path": storage_config.get("mount-path"),
            #             "size": storage_config.get("size"),
            #             "access_mode": storage_config.get("access-mode"),
            #         },
            #     )

            # Add custom configuration
            # config_handler.add_custom_config(component_name, "image", image_url)
            # config_handler.add_custom_config(component_name, "port", application_port)
            # config_handler.add_custom_config(component_name, "unique_name", unique_name)

            # Add authorization-wall sidecar if enabled
            if component_uses_authorization_wall:
                keycloak_secret = self._get_secret_from_map(deployment_name, "keycloak", KeycloakSecret)
                if keycloak_secret:
                    # Extract optional config (banner) from project-level services
                    banner_text = None
                    project_services = project_data.get("services", [])
                    for service_item in project_services:
                        if isinstance(service_item, dict) and ServiceType.AUTHORIZATION_WALL.value in service_item:
                            auth_wall_data = service_item[ServiceType.AUTHORIZATION_WALL.value]
                            if isinstance(auth_wall_data, dict):
                                auth_wall_config = auth_wall_data.get("config", {})
                                if isinstance(auth_wall_config, dict):
                                    banner_text = auth_wall_config.get("banner")
                            break

                    variables["authorization_wall"] = {
                        "issuer_url": keycloak_secret.discovery_url.replace("/.well-known/openid-configuration", ""),
                        "client_id": keycloak_secret.client_id,
                        "keycloak_secret_name": KeycloakSecret.get_secret_name(deployment_name),
                        "cookie_secret_name": f"{unique_name}-oauth2-cookie",
                        "banner": banner_text,
                    }
                    variables["sidecars"] = ["authorization-wall"]
                    variables["service_port"] = 4180
                    logger.info(f"Authorization wall enabled for component '{component_name}'")
                else:
                    logger.warning(
                        f"Component '{component_name}' uses authorization-wall but no keycloak service configured"
                    )

            # Create authorization-wall cookie secret if needed
            if component_uses_authorization_wall and "authorization_wall" in variables:
                import secrets as secrets_module

                cookie_secret_value = secrets_module.token_urlsafe(32)
                cookie_secret_name = variables["authorization_wall"]["cookie_secret_name"]
                secret_template_path = os.path.join(
                    os.path.dirname(__file__), "..", "..", "manifests", "generic-secret.yaml.to-sops.jinja"
                )
                cookie_secret_vars = {
                    "name": cookie_secret_name,
                    "namespace": namespace,
                    "secret_pairs": {"cookie-secret": cookie_secret_value},
                }
                # Determine output dir for the cookie secret manifest
                if target_dir:
                    cookie_output_dir = os.path.join(working_dir, target_dir)
                else:
                    cookie_output_dir = os.path.join(working_dir, project_name, deployment_name)
                self._manifest_generator.create_manifest_file(
                    template_path=secret_template_path,
                    values=cookie_secret_vars,
                    output_dir=cookie_output_dir,
                    output_filename=f"{cookie_secret_name}-secret",
                    use_sops=True,
                )
                logger.info(f"Created authorization-wall cookie secret: {cookie_secret_name}")

            # Configure metrics scraper if enabled
            if component_uses_metrics_scraper and component_def:
                # Extract metrics config from component's service definition.
                # Config is stored as: services: [{metrics-scraper: {config: [{port, path}]}}]
                services = component_def.get("services", [])
                for service_item in services:
                    if isinstance(service_item, dict) and ServiceType.METRICS_SCRAPER.value in service_item:
                        metrics_data = service_item[ServiceType.METRICS_SCRAPER.value]
                        if isinstance(metrics_data, dict):
                            metrics_config = {
                                "port": metrics_data.get("port"),
                                "path": metrics_data.get("path"),
                            }
                        break
                logger.info(
                    f"Metrics scraper enabled for component '{component_name}': port={metrics_config.get('port')}, path={metrics_config.get('path')}"
                )

            # Generate extra manifests for sidecars (e.g. ConfigMaps)
            # Each sidecar template can define a 'configmap' section that produces a standalone manifest
            if "sidecars" in variables:
                if target_dir:
                    sidecar_output_dir = os.path.join(working_dir, target_dir)
                else:
                    sidecar_output_dir = os.path.join(working_dir, project_name, deployment_name)

                for sidecar_name in variables["sidecars"]:
                    sidecar_template_path = os.path.join(
                        os.path.dirname(__file__), "..", "..", "manifests", f"sidecar-{sidecar_name}.yaml.jinja"
                    )
                    configmap_variables = variables.copy()
                    configmap_variables["section"] = "configmap"
                    configmap_manifest_name = generate_manifest_name(component_name, f"{sidecar_name}-configmap")
                    self._manifest_generator.create_manifest_file(
                        template_path=sidecar_template_path,
                        values=configmap_variables,
                        output_dir=sidecar_output_dir,
                        output_filename=configmap_manifest_name,
                        use_sops=False,
                    )
                    created_files.append(f"{configmap_manifest_name}.yaml")
                    logger.info(f"Created sidecar configmap for '{sidecar_name}': {configmap_manifest_name}")

            # Create each manifest type in the git repository.
            # NOTE: tenant-baseline-network-policy.yaml.jinja is intentionally
            # NOT emitted here. It is rendered ONCE per deployment after the
            # component loop completes (see _emit_tenant_baseline_policy
            # call below). Emitting per-component caused file collisions and
            # last-write-wins behaviour for multi-component deployments.
            manifests = [
                "deployment.yaml.jinja",
                "service.yaml.jinja",
            ]

            # Add ingress manifest only if publish-on-web is enabled for this component
            if publish_on_web:
                manifests.append("ingress.yaml.jinja")
                logger.info(f"Including ingress manifest for component '{component_name}' (publish-on-web: true)")
            else:
                logger.debug(f"Skipping ingress manifest for component '{component_name}' (publish-on-web: false)")

            # Add an Off-mode VerticalPodAutoscaler only on clusters that run a VPA
            # recommender. It mutates nothing (advice only); the auto-tuner reads its
            # .status.recommendation to size memory and CPU via GitOps.
            if supports_vpa(cluster):
                manifests.append("vpa.yaml.jinja")
                logger.debug(
                    f"Including VPA manifest for component '{component_name}' (cluster '{cluster}' supports VPA)"
                )

            # Construct the full output directory path once for reuse
            if target_dir:
                # target_dir already contains the complete path structure (cluster/project/deployment)
                full_output_dir = os.path.join(working_dir, target_dir)
            else:
                # Only add project_name/deployment_name when no target_dir is provided
                full_output_dir = os.path.join(working_dir, project_name, deployment_name)

            for manifest_file in manifests:
                manifest_path = os.path.join(os.path.dirname(__file__), "..", "..", "manifests", manifest_file)

                if not os.path.exists(manifest_path):
                    logger.warning(f"Manifest file not found: {manifest_path}")
                    continue

                # Use enhanced manifest generator for proper directory structure
                # Extract just the manifest name (without .yaml.jinja extension)
                manifest_name = manifest_file.replace(".yaml.jinja", "")

                # Handle ingress manifests - iterate through paths and ingress_map
                if manifest_name == "ingress":
                    for ingress_base_name, ingress_hostname in ingress_map.items():
                        # Iterate over each path to create separate ingress for each
                        for path_config in component_paths:
                            path_value = path_config["match"] or "/"
                            rewrite_value = path_config.get("rewrite")

                            # Generate unique ingress name that includes the path
                            ingress_name = generate_ingress_name_from_path(ingress_base_name, path_value)

                            # Create unique manifest filename that includes the path suffix
                            if path_value == "/" or not path_value:
                                unique_manifest_name = generate_manifest_name(component_name, manifest_name)
                            else:
                                # Sanitize path for filename: /api -> api, /v1/users -> v1users
                                path_suffix = path_value.lstrip("/").replace("/", "").lower()
                                unique_manifest_name = generate_manifest_name(
                                    component_name, f"{manifest_name}-{path_suffix}"
                                )

                            # Create ingress-specific variables
                            ingress_variables = variables.copy()

                            # Determine which issuer to use
                            ingress_issuer_name = None
                            ingress_cluster_issuer = None

                            # Resolve the publish-on-web TLS mode for this component.
                            # passthrough: the pod presents its own certificate; provided: the
                            # customer's own certificate rendered as a kubernetes.io/tls Secret
                            # on the ingress. Both leave the issuers None so no cert-manager
                            # annotation is emitted and the platform issues nothing.
                            tls_mode = self._project_file_handler.extract_component_publish_on_web_tls(
                                project_data, component_name, deployment_name
                            )
                            ingress_passthrough = tls_mode == "passthrough"

                            # provided: build a SOPS-encrypted tls Secret from the referenced
                            # PEM attachment and point the ingress at it (idempotent per path).
                            provided_tls_secret_name: str | None = None
                            if tls_mode == "provided":
                                try:
                                    tls_data = await self._project_file_handler.resolve_publish_on_web_certificate(
                                        project_data, component_name, deployment_name
                                    )
                                except ValueError as cert_error:
                                    # A bad/missing customer certificate is a per-component config
                                    # error. Don't abort the whole deployment: log it, record it on
                                    # the deployment result, and skip just this ingress so the rest
                                    # of the project still deploys.
                                    logger.error(
                                        f"publish-on-web 'provided' certificate error for "
                                        f"{deployment_name}/{component_name}, skipping its ingress: {cert_error}"
                                    )
                                    result = self._deployment_results.get(deployment_name)
                                    if result is not None:
                                        result.status = "failed"
                                        result.errors.append(str(cert_error))
                                    continue
                                if tls_data:
                                    provided_tls_secret_name = f"{deployment_name}-{component_name}-provided-tls"
                                    provided_tls_manifest_name = generate_manifest_name(
                                        provided_tls_secret_name, "tls-secret"
                                    )
                                    self._manifest_generator.create_manifest_file(
                                        template_path=os.path.join(
                                            os.path.dirname(__file__),
                                            "..",
                                            "..",
                                            "manifests",
                                            "binary-secret.yaml.to-sops.jinja",
                                        ),
                                        values={
                                            "name": provided_tls_secret_name,
                                            "namespace": namespace,
                                            "secret_type": "tls",
                                            "secret_k8s_type": "kubernetes.io/tls",
                                            "data_pairs": tls_data,
                                        },
                                        output_dir=full_output_dir,
                                        output_filename=provided_tls_manifest_name,
                                        use_sops=True,
                                    )
                                    provided_tls_sops_filename = f"{provided_tls_manifest_name}.to-sops.yaml"
                                    if provided_tls_sops_filename not in created_files:
                                        created_files.append(provided_tls_sops_filename)

                            if ingress_passthrough or provided_tls_secret_name:
                                # No platform issuance: pod cert (passthrough) or customer cert (provided).
                                pass
                            elif base_domain and issuer_config:
                                # External domain with specified issuer
                                if issuer_config in ("letsencrypt", "letsencrypt-staging"):
                                    # Auto-generated namespace Issuer for Let's Encrypt
                                    ingress_issuer_name = generate_issuer_name(
                                        base_domain, issuer_config, deployment_name
                                    )
                                else:
                                    # Custom issuer name - use as namespace-scoped Issuer reference
                                    ingress_issuer_name = issuer_config
                            else:
                                # Standard mode: use cluster's ClusterIssuer
                                ingress_cluster_issuer = get_ingress_cluster_issuer(cluster)

                            ingress_variables.update(
                                {
                                    "name": ingress_name,  # Unique ingress resource name (includes path)
                                    "service_name": unique_name,  # Service name stays the same
                                    "hostname": ingress_hostname,
                                    "path": path_value,  # Path for this specific ingress
                                    "rewrite": rewrite_value,  # Path rewrite target (e.g. "/" to strip prefix)
                                    "issuer_name": ingress_issuer_name,  # Namespace-scoped Issuer (for external domains)
                                    "cluster_issuer": ingress_cluster_issuer,  # ClusterIssuer (for cluster domains)
                                    "tls_secret_name": generate_tls_secret_name(ingress_name),
                                    "external_dns_target": get_external_dns_target_for_hostname(
                                        cluster, ingress_hostname
                                    ),
                                    "passthrough": ingress_passthrough,
                                    "provided_tls_secret": provided_tls_secret_name,
                                }
                            )

                            # Create the ingress manifest file
                            manifest_file_path = self._manifest_generator.create_manifest_file(
                                template_path=manifest_path,
                                values=ingress_variables,
                                output_dir=full_output_dir,
                                output_filename=unique_manifest_name,
                                use_sops=False,
                            )
                            created_files.append(f"{unique_manifest_name}.yaml")
                            logger.info(
                                f"Successfully created {manifest_file} manifest for {ingress_hostname}{path_value}: {manifest_file_path}"
                            )

                    # Create root ingress for nice-url mode if this is the root component.
                    # When domain-format is set, skip root ingress if the template does not
                    # include {component} (all components already share the same hostname).
                    is_root_component = component_name == root_component_name
                    template_has_component = (
                        "{component}" in DOMAIN_FORMAT_TEMPLATES.get(domain_format, "") if domain_format else True
                    )
                    if (
                        domain_mode == "nice-url"
                        and subdomain
                        and base_domain
                        and is_root_component
                        and template_has_component
                    ):
                        root_hostname = generate_nice_url_root_hostname(subdomain, base_domain)
                        root_ingress_name = f"{deployment_name}-root"
                        root_manifest_name = generate_manifest_name(component_name, "ingress-root")

                        # Create root ingress variables
                        root_ingress_variables = variables.copy()

                        # Determine issuer for root ingress (same logic as component ingress)
                        root_issuer_name = None
                        root_cluster_issuer = None

                        if base_domain and issuer_config:
                            if issuer_config in ("letsencrypt", "letsencrypt-staging"):
                                root_issuer_name = generate_issuer_name(base_domain, issuer_config, deployment_name)
                            else:
                                root_issuer_name = issuer_config
                        else:
                            root_cluster_issuer = get_ingress_cluster_issuer(cluster)

                        root_ingress_variables.update(
                            {
                                "name": root_ingress_name,
                                "service_name": unique_name,  # Points to same service as component
                                "hostname": root_hostname,
                                "path": "/",  # Root always uses /
                                "issuer_name": root_issuer_name,
                                "cluster_issuer": root_cluster_issuer,
                                "tls_secret_name": generate_tls_secret_name(root_ingress_name),
                                "external_dns_target": get_external_dns_target_for_hostname(cluster, root_hostname),
                            }
                        )

                        # Create the root ingress manifest
                        root_manifest_file_path = self._manifest_generator.create_manifest_file(
                            template_path=manifest_path,
                            values=root_ingress_variables,
                            output_dir=full_output_dir,
                            output_filename=root_manifest_name,
                            use_sops=False,
                        )
                        created_files.append(f"{root_manifest_name}.yaml")
                        logger.info(
                            f"Successfully created root ingress manifest for {root_hostname}: {root_manifest_file_path}"
                        )

                        # Track root URL in deployment results
                        root_web_address = generate_public_url(root_hostname, use_https)
                        self._deployment_results[deployment_name].urls["root"] = root_web_address
                        logger.info(f"Tracked root URL: {root_web_address}")

                        # Clean up stale root ingress from previous root component
                        previous_dep = self.get_previous_deployment(deployment_name)
                        if previous_dep:
                            old_root = previous_dep.get("root-component")
                            if old_root and old_root != root_component_name:
                                old_manifest = f"{generate_manifest_name(old_root, 'ingress-root')}.yaml"
                                old_manifest_path = os.path.join(full_output_dir, old_manifest)
                                if os.path.exists(old_manifest_path):
                                    os.remove(old_manifest_path)
                                    logger.info(
                                        f"Removed stale root ingress manifest '{old_manifest}' "
                                        f"(root-component changed from '{old_root}' to '{root_component_name}')"
                                    )

                    # Create bare domain ingress for expose-component-on-bare-domain mode.
                    # expose_on_bare_domain holds the component name that should serve the bare domain.
                    if expose_on_bare_domain and base_domain and component_name == expose_on_bare_domain:
                        bare_hostname = generate_bare_domain_hostname(base_domain)
                        bare_ingress_name = f"{deployment_name}-bare-domain"
                        bare_manifest_name = generate_manifest_name(component_name, "ingress-bare-domain")

                        bare_ingress_variables = variables.copy()

                        # Determine issuer for bare domain ingress (same logic as component ingress)
                        bare_issuer_name = None
                        bare_cluster_issuer = None

                        if base_domain and issuer_config:
                            if issuer_config in ("letsencrypt", "letsencrypt-staging"):
                                bare_issuer_name = generate_issuer_name(base_domain, issuer_config, deployment_name)
                            else:
                                bare_issuer_name = issuer_config
                        else:
                            bare_cluster_issuer = get_ingress_cluster_issuer(cluster)

                        bare_ingress_variables.update(
                            {
                                "name": bare_ingress_name,
                                "service_name": unique_name,  # Points to same service as component
                                "hostname": bare_hostname,
                                "path": "/",
                                "issuer_name": bare_issuer_name,
                                "cluster_issuer": bare_cluster_issuer,
                                "tls_secret_name": generate_tls_secret_name(bare_ingress_name),
                                "external_dns_target": get_external_dns_target_for_hostname(cluster, bare_hostname),
                            }
                        )

                        # Create the bare domain ingress manifest
                        bare_manifest_file_path = self._manifest_generator.create_manifest_file(
                            template_path=manifest_path,
                            values=bare_ingress_variables,
                            output_dir=full_output_dir,
                            output_filename=bare_manifest_name,
                            use_sops=False,
                        )
                        created_files.append(f"{bare_manifest_name}.yaml")
                        logger.info(
                            f"Successfully created bare domain ingress manifest for {bare_hostname}: {bare_manifest_file_path}"
                        )

                        # Track bare domain URL in deployment results
                        bare_web_address = generate_public_url(bare_hostname, use_https)
                        self._deployment_results[deployment_name].urls["bare-domain"] = bare_web_address
                        logger.info(f"Tracked bare domain URL: {bare_web_address}")

                        # Clean up stale bare domain ingress from previous component
                        previous_dep = self.get_previous_deployment(deployment_name)
                        if previous_dep:
                            old_bare = previous_dep.get("expose-component-on-bare-domain")
                            if old_bare and old_bare != expose_on_bare_domain:
                                old_manifest = f"{generate_manifest_name(old_bare, 'ingress-bare-domain')}.yaml"
                                old_manifest_path = os.path.join(full_output_dir, old_manifest)
                                if os.path.exists(old_manifest_path):
                                    os.remove(old_manifest_path)
                                    logger.info(
                                        f"Removed stale bare domain ingress manifest '{old_manifest}' "
                                        f"(expose-component-on-bare-domain changed from '{old_bare}' to '{expose_on_bare_domain}')"
                                    )

                else:
                    # Standard single manifest creation
                    unique_manifest_name = generate_manifest_name(component_name, manifest_name)

                    # Use SOPS encryption for generic secrets (SSO/OIDC), regular processing for others
                    use_sops_for_manifest = manifest_name == "generic-secret"

                    # Create manifest file in the specific directory structure
                    manifest_file_path = self._manifest_generator.create_manifest_file(
                        template_path=manifest_path,
                        values=variables,
                        output_dir=full_output_dir,
                        output_filename=unique_manifest_name,
                        use_sops=use_sops_for_manifest,
                    )

                    # Add to the list of created files with component name for uniqueness
                    created_files.append(f"{unique_manifest_name}.yaml")
                    logger.info(f"Successfully created {manifest_file} manifest: {manifest_file_path}")

            # Create PVC manifests for persistent storage using PVCManager
            persistent_storage = self._project_file_handler.get_persistent_storage(processed_storage_configs)

            if persistent_storage:
                logger.info(f"Creating {len(persistent_storage)} PVC manifests for component: {component_name}")

                # Delegate PVC creation to PVCManager which handles generation and cleanup
                created_pvc_files = await self._pvc_manager.create_pvc_manifests_for_component(
                    project_data=project_data,
                    deployment=deployment,
                    component_name=component_name,
                    unique_name=unique_name,
                    persistent_storage=persistent_storage,
                    namespace=namespace,
                    cluster=cluster,
                    full_output_dir=full_output_dir,
                    manifest_generator=self._manifest_generator,
                    force_clone_override=self._force_clone_override,
                )
                created_files.extend(created_pvc_files)

            # Create separate secret manifests for SSO and user secrets
            secret_template_path = os.path.join(
                os.path.dirname(__file__), "..", "..", "manifests", "generic-secret.yaml.to-sops.jinja"
            )

            # Create SSO secret if Keycloak credentials are available in secrets map
            keycloak_credentials = self._get_secret_from_map(deployment_name, "keycloak", KeycloakSecret)
            if keycloak_credentials:
                # Always use SOPS encryption for secrets (security requirement)
                use_sops_for_sso = True

                # Use the existing Keycloak secret instance directly (no need to recreate)
                keycloak_secret = keycloak_credentials

                # Get base Keycloak secret data
                keycloak_secret_data = keycloak_secret.to_k8s_secret_data()

                # Add resolved Keycloak aliases from all components
                keycloak_aliases = (
                    self._deployment_aliases.get(deployment_name, {}).get("secret", {}).get("keycloak", {})
                )
                if keycloak_aliases:
                    logger.debug(f"Resolving {len(keycloak_aliases)} keycloak aliases for deployment {deployment_name}")
                    resolved_aliases = self._resolve_aliases(keycloak_aliases, keycloak_secret_data)
                    keycloak_secret_data.update(resolved_aliases)
                    logger.info(f"Added {len(resolved_aliases)} resolved keycloak aliases to deployment secret")

                # Only include fields needed for the generic-secret template
                # NOTE: Keycloak secret is per-deployment, not per-component
                sso_secret_vars = {
                    "name": KeycloakSecret.get_secret_name(deployment_name),
                    "namespace": namespace,
                    "secret_type": "keycloak",  # For proper labeling
                    "secret_pairs": keycloak_secret_data,  # Now includes base + aliases
                }

                # Create SSO secret with keycloak naming convention (deployment-level)
                sso_manifest_name = generate_manifest_name(deployment_name, "keycloak-secret")

                sso_secret_path = self._manifest_generator.create_manifest_file(
                    template_path=secret_template_path,
                    values=sso_secret_vars,
                    output_dir=full_output_dir,
                    output_filename=sso_manifest_name,
                    use_sops=use_sops_for_sso,
                )

                # All secrets are SOPS encrypted for security
                sops_filename = f"{sso_manifest_name}.to-sops.yaml"
                created_files.append(sops_filename)
                logger.info(f"SSO secret will be SOPS encrypted: {sops_filename}")
                logger.info(f"Successfully created SSO secret manifest: {sso_secret_path}")

            # Create platform secret (always present, per-component)
            platform_secret = PlatformSecret(
                deployment_name=deployment_name,
                component_name=component_name,
            )
            platform_secret_data = platform_secret.to_k8s_secret_data()

            # Resolve platform aliases if any
            platform_aliases = self._deployment_aliases.get(deployment_name, {}).get("secret", {}).get("platform", {})
            if platform_aliases:
                logger.debug(f"Resolving {len(platform_aliases)} platform aliases for component {component_name}")
                resolved_platform_aliases = self._resolve_aliases(platform_aliases, platform_secret_data)
                platform_secret_data.update(resolved_platform_aliases)
                logger.info(
                    f"Added {len(resolved_platform_aliases)} resolved platform aliases to component {component_name}"
                )

            platform_secret_vars = {
                "name": PlatformSecret.get_secret_name(unique_name),
                "namespace": namespace,
                "secret_type": "platform",
                "secret_pairs": platform_secret_data,
            }

            platform_manifest_name = generate_manifest_name(component_name, "platform-secret")
            self._manifest_generator.create_manifest_file(
                template_path=secret_template_path,
                values=platform_secret_vars,
                output_dir=full_output_dir,
                output_filename=platform_manifest_name,
                use_sops=False,
            )
            created_files.append(f"{platform_manifest_name}.yaml")
            logger.info(f"Created platform secret manifest: {platform_manifest_name}")

            # Create user secret if configured
            if user_env_vars:
                logger.debug(f"Processing {len(user_env_vars)} user environment variables for {component_name}")

                # Create typed User secret
                user_secret = UserSecret(env_vars=user_env_vars)

                # Only include fields needed for the generic-secret template
                user_secret_vars = {
                    "name": UserSecret.get_secret_name(unique_name),
                    "namespace": namespace,
                    "secret_type": "user",  # For proper labeling
                    "secret_pairs": user_secret.to_k8s_secret_data(),
                }

                # Create user secret with user naming convention
                user_manifest_name = generate_manifest_name(component_name, "user-secret")
                # Always use SOPS encryption for secrets (security requirement)
                use_sops_for_user = True

                user_secret_path = self._manifest_generator.create_manifest_file(
                    template_path=secret_template_path,
                    values=user_secret_vars,
                    output_dir=full_output_dir,
                    output_filename=user_manifest_name,
                    use_sops=use_sops_for_user,
                )

                # All secrets are SOPS encrypted for security
                sops_filename = f"{user_manifest_name}.to-sops.yaml"
                created_files.append(sops_filename)
                logger.info(f"User secret will be SOPS encrypted: {sops_filename}")
                logger.info(f"Successfully created user secret manifest: {user_secret_path}")

            # Create binary secrets for file-mode attachments (one per attachment id per deployment).
            # Keyed by secret name so duplicate references across components stay idempotent.
            if attachment_file_secrets:
                binary_secret_template_path = os.path.join(
                    os.path.dirname(__file__), "..", "..", "manifests", "binary-secret.yaml.to-sops.jinja"
                )
                for secret_name, data_pairs in attachment_file_secrets.items():
                    attachment_secret_vars = {
                        "name": secret_name,
                        "namespace": namespace,
                        "secret_type": "attachment",
                        "data_pairs": data_pairs,
                    }
                    attachment_manifest_name = generate_manifest_name(secret_name, "attachment-secret")
                    self._manifest_generator.create_manifest_file(
                        template_path=binary_secret_template_path,
                        values=attachment_secret_vars,
                        output_dir=full_output_dir,
                        output_filename=attachment_manifest_name,
                        use_sops=True,
                    )
                    attachment_sops_filename = f"{attachment_manifest_name}.to-sops.yaml"
                    if attachment_sops_filename not in created_files:
                        created_files.append(attachment_sops_filename)
                    logger.info(f"Created attachment secret manifest: {secret_name}")

            # Create registry secrets for private container registries (deployment-level, created once)
            if component == components[0]:  # Only create registry secrets once per deployment
                for registry_name in registry_configs_map:
                    registry_secret = self._get_secret_from_map(deployment_name, registry_name, RegistrySecret)
                    if registry_secret:
                        logger.debug(f"Creating registry secret for registry '{registry_name}'")

                        # Registry secrets use kubernetes.io/dockerconfigjson type
                        registry_secret_vars = {
                            "name": generate_registry_secret_name(deployment_name, registry_name),
                            "namespace": namespace,
                            "secret_type": "registry",
                            "secret_k8s_type": "kubernetes.io/dockerconfigjson",
                            "secret_pairs": registry_secret.to_k8s_secret_data(),  # Contains .dockerconfigjson
                        }

                        # Create registry secret manifest
                        registry_manifest_name = generate_manifest_name(
                            deployment_name, f"{registry_name}-registry-secret"
                        )
                        use_sops_for_registry = True  # Always use SOPS encryption for registry credentials

                        registry_secret_path = self._manifest_generator.create_manifest_file(
                            template_path=secret_template_path,
                            values=registry_secret_vars,
                            output_dir=full_output_dir,
                            output_filename=registry_manifest_name,
                            use_sops=use_sops_for_registry,
                        )

                        # All secrets are SOPS encrypted for security
                        sops_filename = f"{registry_manifest_name}.to-sops.yaml"
                        created_files.append(sops_filename)
                        logger.info(f"Registry secret will be SOPS encrypted: {sops_filename}")
                        logger.info(f"Successfully created registry secret manifest: {registry_secret_path}")

            # Create Let's Encrypt Issuer manifest if configured (once per unique base-domain/issuer combination)
            if base_domain and issuer_config and issuer_config.startswith("letsencrypt"):
                # Track created issuers to avoid duplicates within this deployment
                issuer_key = f"{base_domain}:{issuer_config}"
                if issuer_key not in created_issuers:
                    created_issuers.add(issuer_key)

                    # Determine contact email: project override or cluster default
                    project_contact_email = project_data.get("config", {}).get("contact-email")
                    cluster_contact_email = get_letsencrypt_contact_email(cluster)
                    contact_email = project_contact_email or cluster_contact_email

                    if contact_email:
                        issuer_template_path = os.path.join(
                            os.path.dirname(__file__), "..", "..", "manifests", "issuer-letsencrypt.yaml.jinja"
                        )

                        issuer_name_generated = generate_issuer_name(base_domain, issuer_config, deployment_name)
                        issuer_secret_name = generate_issuer_secret_name(base_domain, issuer_config, deployment_name)
                        issuer_manifest_filename = generate_issuer_manifest_name(
                            base_domain, issuer_config, deployment_name
                        ).replace(".yaml", "")

                        issuer_variables = {
                            "issuer_name": issuer_name_generated,
                            "issuer_secret_name": issuer_secret_name,
                            "contact_email": contact_email,
                            "staging": issuer_config == "letsencrypt-staging",
                            "namespace": namespace,
                        }

                        issuer_manifest_path = self._manifest_generator.create_manifest_file(
                            template_path=issuer_template_path,
                            values=issuer_variables,
                            output_dir=full_output_dir,
                            output_filename=issuer_manifest_filename,
                            use_sops=False,
                        )

                        created_files.append(f"{issuer_manifest_filename}.yaml")
                        logger.info(
                            f"Successfully created Let's Encrypt Issuer manifest for {base_domain}: {issuer_manifest_path}"
                        )

                        # Create network policy for ACME HTTP-01 challenge.
                        # cert-manager's HTTP-01 solver pod listens on 8089 (the router
                        # forwards the challenge to the pod on 8089, not 80), so the
                        # solver port must be allowed or the self-check times out.
                        network_policy_template_path = os.path.join(
                            os.path.dirname(__file__), "..", "..", "manifests", "network-policy.yaml.jinja"
                        )
                        network_policy_filename = generate_network_policy_manifest_name("acme-http", deployment_name)
                        network_policy_variables = {
                            "name": generate_network_policy_name("acme-http", deployment_name),
                            "namespace": namespace,
                            "pod_selector": None,  # Match all pods
                            "ports": [80, 8089],  # 80 for ingress, 8089 for ACME solver pod
                        }
                        network_policy_path = self._manifest_generator.create_manifest_file(
                            template_path=network_policy_template_path,
                            values=network_policy_variables,
                            output_dir=full_output_dir,
                            output_filename=network_policy_filename,
                            use_sops=False,
                        )
                        created_files.append(f"{network_policy_filename}.yaml")
                        logger.info(f"Created HTTP ingress network policy for ACME challenge: {network_policy_path}")
                    else:
                        logger.warning(
                            f"Cannot create Let's Encrypt Issuer for {base_domain}: no contact email configured "
                            f"(set contact-email in project config or letsencrypt.contact_email in cluster config)"
                        )

            # Create database secret if component uses PostgreSQL service
            if component_uses_postgresql:
                db_credentials = self._get_secret_from_map(deployment_name, "database", DatabaseSecret)

                if db_credentials:
                    logger.debug(f"Creating database secret for {component_name} with PostgreSQL credentials")

                    # Use the host from db_credentials - database_manager already determined
                    # the correct host (namespace-specific or shared) based on service type
                    database_secret = DatabaseSecret(
                        host=db_credentials.host,  # Already set by database_manager
                        port=db_credentials.port,
                        username=db_credentials.username,
                        password=db_credentials.password,
                        database=db_credentials.database,
                        schema=db_credentials.schema,
                    )

                    # Store the updated secret instance for configuration tracking
                    self._add_secret_to_create(deployment_name, "database", database_secret)

                    # Get base database secret data
                    database_secret_data = database_secret.to_k8s_secret_data()

                    # Add resolved database aliases from all components
                    database_aliases = (
                        self._deployment_aliases.get(deployment_name, {}).get("secret", {}).get("database", {})
                    )
                    if database_aliases:
                        logger.debug(
                            f"Resolving {len(database_aliases)} database aliases for deployment {deployment_name}"
                        )
                        resolved_aliases = self._resolve_aliases(database_aliases, database_secret_data)
                        database_secret_data.update(resolved_aliases)
                        logger.info(f"Added {len(resolved_aliases)} resolved database aliases to deployment secret")

                    # Create database secret vars with all required environment variables + aliases
                    # Use deployment-level naming for the secret name
                    database_secret_vars = {
                        "name": DatabaseSecret.get_secret_name(deployment_name),
                        "namespace": namespace,
                        "secret_pairs": database_secret_data,  # Now includes base + aliases
                    }

                    # Create database secret with deployment-level naming (not component-level)
                    # This matches what we look for in _get_existing_database_credentials_from_k8s
                    database_manifest_name = f"{DatabaseSecret.get_secret_name(deployment_name)}-secret"

                    database_secret_path = self._manifest_generator.create_manifest_file(
                        template_path=secret_template_path,
                        values=database_secret_vars,
                        output_dir=full_output_dir,
                        output_filename=database_manifest_name,
                        use_sops=True,
                    )

                    # All secrets are SOPS encrypted for security
                    sops_filename = f"{database_manifest_name}.to-sops.yaml"
                    created_files.append(sops_filename)
                    logger.info(f"Database secret will be SOPS encrypted: {sops_filename}")
                    logger.info(f"Successfully created database secret manifest: {database_secret_path}")
                else:
                    logger.warning(
                        f"Component {component_name} uses PostgreSQL but no database credentials found in deployment {deployment_name}"
                    )

            # Create MinIO secret if component uses object storage service
            if component_uses_minio:
                # Get MinIO credentials from the private secrets map (not from deployment data)
                minio_credentials = self._get_secret_from_map(deployment_name, "minio", MinIOSecret)

                if minio_credentials:
                    logger.debug(f"Creating MinIO secret for {component_name} with object storage credentials")

                    # Create typed MinIO secret with cluster-specific host and port
                    minio_secret = MinIOSecret(
                        host=get_minio_host(cluster),
                        port=get_minio_port(cluster),
                        access_key=minio_credentials.access_key,
                        secret_key=minio_credentials.secret_key,
                        bucket_name=minio_credentials.bucket_name,
                        region=minio_credentials.region,
                    )

                    # Get base MinIO secret data
                    minio_secret_data = minio_secret.to_k8s_secret_data()

                    # Add resolved MinIO aliases from all components
                    minio_aliases = self._deployment_aliases.get(deployment_name, {}).get("secret", {}).get("minio", {})
                    if minio_aliases:
                        logger.debug(f"Resolving {len(minio_aliases)} minio aliases for deployment {deployment_name}")
                        resolved_aliases = self._resolve_aliases(minio_aliases, minio_secret_data)
                        minio_secret_data.update(resolved_aliases)
                        logger.info(f"Added {len(resolved_aliases)} resolved minio aliases to deployment secret")

                    # Create MinIO secret vars with all required environment variables + aliases
                    # Use deployment-level naming for the secret name
                    minio_secret_vars = {
                        "name": MinIOSecret.get_secret_name(deployment_name),
                        "namespace": namespace,
                        "secret_pairs": minio_secret_data,  # Now includes base + aliases
                    }

                    # Create MinIO secret with deployment-level naming (not component-level)
                    # This matches what we look for in _get_existing_minio_credentials_from_k8s
                    minio_manifest_name = f"{MinIOSecret.get_secret_name(deployment_name)}-secret"

                    minio_secret_path = self._manifest_generator.create_manifest_file(
                        template_path=secret_template_path,
                        values=minio_secret_vars,
                        output_dir=full_output_dir,
                        output_filename=minio_manifest_name,
                        use_sops=True,
                    )

                    # All secrets are SOPS encrypted for security
                    sops_filename = f"{minio_manifest_name}.to-sops.yaml"
                    created_files.append(sops_filename)
                    logger.info(f"MinIO secret will be SOPS encrypted: {sops_filename}")
                    logger.info(f"Successfully created MinIO secret manifest: {minio_secret_path}")
                else:
                    logger.warning(
                        f"Component {component_name} uses MinIO but no object storage credentials found in deployment {deployment_name}"
                    )

            # Create Redis secret if component uses Redis cache service
            if component_uses_redis:
                # Get Redis credentials from the private secrets map
                redis_credentials = self._get_secret_from_map(deployment_name, "redis", RedisSecret)

                if redis_credentials:
                    logger.debug(f"Creating Redis secret for {component_name} with cache credentials")

                    redis_secret = RedisSecret(
                        host=redis_credentials.host,
                        port=redis_credentials.port,
                        username=redis_credentials.username,
                        password=redis_credentials.password,
                        key_prefix=redis_credentials.key_prefix,
                    )

                    # Get base Redis secret data
                    redis_secret_data = redis_secret.to_k8s_secret_data()

                    # Add resolved Redis aliases from all components
                    redis_aliases = self._deployment_aliases.get(deployment_name, {}).get("secret", {}).get("redis", {})
                    if redis_aliases:
                        logger.debug(f"Resolving {len(redis_aliases)} redis aliases for deployment {deployment_name}")
                        resolved_aliases = self._resolve_aliases(redis_aliases, redis_secret_data)
                        redis_secret_data.update(resolved_aliases)
                        logger.info(f"Added {len(resolved_aliases)} resolved redis aliases to deployment secret")

                    redis_secret_vars = {
                        "name": RedisSecret.get_secret_name(deployment_name),
                        "namespace": namespace,
                        "secret_pairs": redis_secret_data,
                    }

                    redis_manifest_name = f"{RedisSecret.get_secret_name(deployment_name)}-secret"

                    redis_secret_path = self._manifest_generator.create_manifest_file(
                        template_path=secret_template_path,
                        values=redis_secret_vars,
                        output_dir=full_output_dir,
                        output_filename=redis_manifest_name,
                        use_sops=True,
                    )

                    sops_filename = f"{redis_manifest_name}.to-sops.yaml"
                    created_files.append(sops_filename)
                    logger.info(f"Redis secret will be SOPS encrypted: {sops_filename}")
                    logger.info(f"Successfully created Redis secret manifest: {redis_secret_path}")
                else:
                    logger.warning(
                        f"Component {component_name} uses Redis but no cache credentials found in deployment {deployment_name}"
                    )

            # Create metrics auth secret if component uses metrics-scraper
            if component_uses_metrics_scraper:
                metrics_auth_token = settings.PROMETHEUS_METRICS_AUTH_TOKEN
                if metrics_auth_token:
                    metrics_secret = MetricsAuthSecret(token=metrics_auth_token)
                    metrics_secret_data = metrics_secret.to_k8s_secret_data()

                    metrics_secret_vars = {
                        "name": MetricsAuthSecret.get_secret_name(deployment_name),
                        "namespace": namespace,
                        "secret_pairs": metrics_secret_data,
                    }

                    metrics_manifest_name = f"{MetricsAuthSecret.get_secret_name(deployment_name)}-secret"

                    self._manifest_generator.create_manifest_file(
                        template_path=secret_template_path,
                        values=metrics_secret_vars,
                        output_dir=full_output_dir,
                        output_filename=metrics_manifest_name,
                        use_sops=True,
                    )

                    sops_filename = f"{metrics_manifest_name}.to-sops.yaml"
                    created_files.append(sops_filename)
                    logger.info(f"Metrics auth secret will be SOPS encrypted: {sops_filename}")
                else:
                    logger.warning(
                        f"Component {component_name} uses metrics-scraper but PROMETHEUS_METRICS_AUTH_TOKEN is not configured"
                    )

        # Emit one tenant-baseline NetworkPolicy per deployment after all
        # components have been processed. The policy selects pods by the
        # `deployment: <name>` label (added in deployment.yaml.jinja), so
        # multiple components of the same deployment share one policy and
        # helm/helmfile pods are left unselected (default-allow remains the
        # status quo for those until a separate baseline is added).
        if target_dir:
            deployment_output_dir = os.path.join(working_dir, target_dir)
        else:
            deployment_output_dir = os.path.join(working_dir, project_name, deployment_name)
        baseline_template_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "manifests", "tenant-baseline-network-policy.yaml.jinja"
        )
        baseline_manifest_name = generate_network_policy_manifest_name(f"{deployment_name}-tenant-baseline").replace(
            ".yaml", ""
        )
        baseline_variables = {
            "name": generate_network_policy_name(f"{deployment_name}-tenant-baseline"),
            "namespace": namespace,
            "deployment_selector": deployment_name,
            "ops_namespace": get_namespace(cluster),
            "backup_namespace": get_backup_namespace(cluster),
            "ingress_controller_selector": get_ingress_controller_selector(cluster),
            "project_infra_namespace": get_infrastructure_namespace(cluster, project_name),
        }
        self._manifest_generator.create_manifest_file(
            template_path=baseline_template_path,
            values=baseline_variables,
            output_dir=deployment_output_dir,
            output_filename=baseline_manifest_name,
            use_sops=False,
        )
        created_files.append(f"{baseline_manifest_name}.yaml")
        logger.info(
            f"Created tenant-baseline NetworkPolicy for deployment '{deployment_name}' "
            f"(selector deployment={deployment_name}, ops={baseline_variables['ops_namespace']}, "
            f"backup={baseline_variables['backup_namespace']}, "
            f"ingress={baseline_variables['ingress_controller_selector']['namespace']})"
        )

        # Clear rollback info on success
        self._pending_subdomain_rollback = None
        return created_files

    # TODO: this should be moved to manifests.py
    async def create_kustomization_file(
        self,
        git_connector: GitConnector,
        namespace: str,
        sops_files: list[str],
        regular_files: list[str],
        target_dir: str | None = None,
        deployment: dict[str, Any] | None = None,
    ) -> bool:
        """
        Create a kustomization.yaml file that includes both SOPS encrypted files and regular files.
        Uses the new manifest generator with YAML templates.

        Args:
            git_connector: The git connector with an already cloned repository
            namespace: Target namespace for the kustomization
            sops_files: List of SOPS encrypted file names
            regular_files: List of regular manifest file names
            target_dir: Optional target directory within the git repository
            deployment: Optional deployment data containing cluster information for namespace prefixing

        Returns:
            True if kustomization file was created successfully, False otherwise
        """
        working_dir = await git_connector.get_working_dir()
        if target_dir:
            target_path = os.path.join(working_dir, target_dir)
            os.makedirs(target_path, exist_ok=True)
        else:
            target_path = working_dir

        # Use the manifest generator to create kustomization files
        result = self._manifest_generator.create_kustomization_files(
            output_dir=target_path,
            namespace=namespace,
            sops_files=sops_files,
            regular_files=regular_files,
            deployment=deployment,
        )

        if result:
            logger.info(
                f"Successfully created kustomization.yaml with {len(sops_files)} SOPS files and {len(regular_files)} regular files"
            )

        return result

    async def get_contents(self) -> dict[str, Any]:
        """
        Convenience method to get the contents of the project file.
        :return: Contents of the project file
        """
        full_path = await self.get_project_full_file_path()
        self.__has_contents = True
        return await self._project_file_handler.read_project_file(full_path)

    async def get_decrypted_view(self) -> dict[str, Any]:
        """
        Return a throwaway deep copy of the project data with all AGE-encrypted
        values decrypted. This is for display purposes only -- the returned dict
        must never be stored or written back to git.
        """
        import copy

        project_data = await self.get_contents()
        view = copy.deepcopy(project_data)

        try:
            private_key = await get_decoded_project_private_key(project_data)
        except Exception as e:
            logger.warning(f"Could not get project private key for decrypted view: {e}")
            return view

        if not private_key:
            return view

        await self._decrypt_tree(view, private_key)
        return view

    async def _decrypt_tree(self, data: Any, private_key: str) -> None:
        """Recursively walk data and decrypt any AGE-encrypted string values in place."""
        age_header = "-----BEGIN AGE ENCRYPTED FILE-----"

        if isinstance(data, dict):
            for key in list(data.keys()):
                value = data[key]
                if isinstance(value, str) and age_header in value:
                    try:
                        data[key] = await decrypt_age_content(value, private_key)
                    except Exception:
                        logger.debug(f"Failed to decrypt field '{key}', leaving as-is")
                elif isinstance(value, dict | list):
                    await self._decrypt_tree(value, private_key)
        elif isinstance(data, list):
            for i, item in enumerate(data):
                if isinstance(item, str) and age_header in item:
                    with contextlib.suppress(Exception):
                        data[i] = await decrypt_age_content(item, private_key)
                elif isinstance(item, dict | list):
                    await self._decrypt_tree(item, private_key)

    async def _get_by_json_path(self, json_path: str) -> Any:
        """
        Get a value from the project file using JSONPath.

        Args:
            json_path: JSONPath expression to query the project data

        Returns:
            The value found at the JSONPath, or None if not found
        """
        project_data = await self.get_contents()

        try:
            jsonpath_expr = jsonpath_parse(json_path)
            matches = jsonpath_expr.find(project_data)
            return matches[0].value if matches else None
        except Exception as e:
            raise Exception(f"Error querying JSONPath '{json_path}'") from e

    async def get_api_key(self) -> str:
        """
        Get and decrypt the project's API key.

        Returns:
            Decrypted API key
        """
        project_name = await self.get_name()
        encrypted_api_key = await self._get_by_json_path("$.config.api-key")
        if not encrypted_api_key:
            raise ValueError(f"No api key found in project config for {project_name}")
        private_key = await get_decoded_project_private_key(await self.get_contents())
        decrypted_api_key = await decrypt_age_content(str(encrypted_api_key), private_key)
        logger.debug(f"Successfully decrypted API key for project: {project_name}")
        return decrypted_api_key

    async def _enforce_domain_config(self, project_data: dict[str, Any], deployment_name: str) -> str | None:
        """Run the shared domain enforcer against an upserted deployment.

        Reuses the wizard's ``DomainConfigEnforcer`` so the API enforces the
        same cross-field domain rules — notably that a dot-separated URL format
        requires a domain that supports dots (a dash-only ODCN domain with a
        dot format produces an unreachable multi-label hostname).

        API callers cannot tick a "request domain" checkbox, so an unapproved
        custom domain (a non-blocking warning in the wizard) is treated as a
        hard rejection here.

        Returns an error message on violation, or None when the config is valid.
        """
        deployments = project_data.get("deployments", [])
        index = next(
            (i for i, d in enumerate(deployments) if isinstance(d, dict) and d.get("name") == deployment_name),
            None,
        )
        if index is None:
            return None
        try:
            await DomainConfigEnforcer(deployment_index=index).enforce(
                project_data, {"project_name": await self.get_name()}
            )
        except (ValueError, FieldWarning) as e:  # FieldError is a ValueError subclass
            return str(e)
        return None

    async def upsert_deployment(
        self,
        deployment_name: str,
        components: list,  # ComponentReference objects from router
        clone_from: str | None = None,
        force_clone: bool = False,
        domain_format: str | None = None,
        subdomain: str | None = None,
        base_domain: str | None = None,
    ) -> dict[str, Any]:
        """
        Create or update a deployment in the project YAML file.

        If the deployment doesn't exist, it will be created. If it exists, the component
        images will be updated. The clone_from parameter is only used when creating a new
        deployment, or when updating with force_clone set to true.

        Args:
            deployment_name: Name of the deployment
            components: List of ComponentReference objects with reference and image
            clone_from: Optional deployment name to clone configuration from (only on create or if force_clone)
            force_clone: If true, use clone_from even when updating an existing deployment

        Returns:
            Dict with success status, created flag, and error details if applicable:
            {"success": bool, "created": bool, "error": str | None, "error_type": str | None}
        """
        try:
            # Get current project data
            project_data = await self.get_contents()
            project_name = await self.get_name()

            # Validate that all component references exist in the project
            validation_result = self._validate_component_references(project_data, components, "deployment")
            if not validation_result["success"]:
                return {
                    "success": False,
                    "created": False,
                    "error": validation_result["error"],
                    "error_type": "invalid_component_references",
                }

            # Check if deployment already exists
            existing_deployments = await self.get_deployments(cluster_filter=False)
            existing_deployment = None
            existing_deployment_index = None
            for idx, deployment in enumerate(existing_deployments):
                if deployment.get("name") == deployment_name:
                    existing_deployment = deployment
                    existing_deployment_index = idx
                    break

            if existing_deployment:
                # UPDATE existing deployment - only update component images
                logger.info(f"Updating existing deployment '{deployment_name}' in project '{project_name}'")

                # Collect commit-message parts while mutating: after the update the
                # old image is gone from project_data, so comparing afterwards
                # always sees "no change" and the commit message degrades to the
                # generic "configuration".
                change_parts: list[str] = []

                # Find the deployment in project_data["deployments"] to update
                for deployment in project_data["deployments"]:
                    if deployment.get("name") == deployment_name:
                        # Update images for existing components, add new ones
                        existing_components = {c["reference"]: c for c in deployment.get("components", [])}

                        normalized_warnings: list[str] = []
                        for component in components:
                            # Normalize image to lowercase (OCI spec requirement)
                            normalized_image, was_normalized = normalize_container_image(component.image)
                            if was_normalized:
                                normalized_warnings.append(
                                    f"Image for component '{component.reference}' was normalized to lowercase: "
                                    f"'{component.image}' -> '{normalized_image}'"
                                )

                            if component.reference in existing_components:
                                # Update existing component's image
                                old_image = existing_components[component.reference].get("image", "")
                                existing_components[component.reference]["image"] = normalized_image
                                if old_image != normalized_image:
                                    change_parts.append(f"{component.reference} image to {normalized_image}")
                                logger.info(
                                    f"Updated image for component '{component.reference}' to '{normalized_image}'"
                                )
                            else:
                                # Add new component
                                deployment["components"].append(
                                    {"reference": component.reference, "image": normalized_image}
                                )
                                change_parts.append(f"add component {component.reference}")
                                logger.info(
                                    f"Added new component '{component.reference}' with image '{normalized_image}'"
                                )

                        # Update domain settings if provided
                        if domain_format is not None:
                            deployment["domain-format"] = domain_format
                        if subdomain is not None:
                            deployment["subdomain"] = subdomain
                        if base_domain is not None:
                            deployment["base-domain"] = base_domain

                        # Handle clone_from only if force_clone is true
                        if clone_from and force_clone:
                            deployment["clone-from"] = {
                                "type": "deployment",
                                "reference": clone_from,
                                "mode": "once",
                            }
                            logger.info(f"Setting clone-from to deployment '{clone_from}' (force_clone=true)")

                        break

                # Validate domain config when this call changes domain settings.
                # Gated so image-only updates never fail on a deployment whose
                # domain config predates this check.
                if domain_format is not None or base_domain is not None or subdomain is not None:
                    domain_error = await self._enforce_domain_config(project_data, deployment_name)
                    if domain_error:
                        return {
                            "success": False,
                            "created": False,
                            "error": domain_error,
                            "error_type": "domain_validation",
                        }

                # Ensure unapproved domains/subdomains get request entries
                ensure_domain_requests(project_data, settings.CLUSTER_MANAGER)

                # Build descriptive commit message (component parts were collected
                # during the update loop, before the old values were overwritten)
                if domain_format is not None:
                    change_parts.append(f"domain-format to {domain_format}")
                if subdomain is not None:
                    change_parts.append(f"subdomain to {subdomain}")
                if base_domain is not None:
                    change_parts.append(f"base-domain to {base_domain}")
                if clone_from and force_clone:
                    change_parts.append(f"clone-from {clone_from}")

                details = ", ".join(change_parts) if change_parts else "configuration"
                commit_message = f"Update deployment '{deployment_name}' in project '{project_name}': {details}"

                # Validate (schema + structural) then save and commit through the single path
                await self.save_and_commit_project(project_data, commit_message)

                logger.info(f"Successfully updated deployment '{deployment_name}' in project '{project_name}'")
                result: dict[str, Any] = {"success": True, "created": False, "error": None, "error_type": None}
                if normalized_warnings:
                    result["warnings"] = normalized_warnings
                return result

            else:
                # CREATE new deployment
                logger.info(f"Creating new deployment '{deployment_name}' in project '{project_name}'")

                # Create new deployment object
                new_deployment: dict[str, Any] = {"name": deployment_name, "components": []}

                # Apply domain settings if provided
                if domain_format:
                    new_deployment["domain-format"] = domain_format
                if subdomain:
                    new_deployment["subdomain"] = subdomain
                if base_domain:
                    new_deployment["base-domain"] = base_domain

                # Convert components from router objects to dict format
                normalized_warnings_create: list[str] = []
                for component in components:
                    # Normalize image to lowercase (OCI spec requirement)
                    normalized_image, was_normalized = normalize_container_image(component.image)
                    if was_normalized:
                        normalized_warnings_create.append(
                            f"Image for component '{component.reference}' was normalized to lowercase: "
                            f"'{component.image}' -> '{normalized_image}'"
                        )
                    new_deployment["components"].append({"reference": component.reference, "image": normalized_image})

                # Handle clone-from logic for new deployments
                if clone_from:
                    # Find source deployment to clone from
                    source_deployment = find_value_by_jsonpath(
                        project_data, f"$.deployments[?(@.name=='{clone_from}')]"
                    )

                    if source_deployment:
                        logger.info(f"Cloning deployment configuration from '{clone_from}'")

                        # Clone properties from source, excluding fields that must be
                        # unique or that tie the deployment to a custom domain setup.
                        # Custom domains (base-domain, domain-mode, domain-format, issuer)
                        # are not copied because cloned deployments should use the default
                        # cluster domain rather than inheriting the source's DNS config.
                        # domain-format in particular must be dropped: a dot-based format
                        # (e.g. component.subdomain) inherited without the source's
                        # base-domain resolves onto the cluster wildcard, producing a
                        # multi-label host the single-label wildcard cert cannot cover.
                        # The backup block is not copied either: backups are an explicit
                        # per-deployment choice, and inheriting the source's schedule made
                        # every PR preview accumulate nightly snapshots.
                        clone_exclude_keys = [
                            "name",
                            "components",
                            "subdomain",
                            "base-domain",
                            "domain-mode",
                            "domain-format",
                            "issuer",
                            "backup",
                        ]
                        new_deployment.update(
                            {
                                key: copy.deepcopy(value)
                                for key, value in source_deployment.items()
                                if key not in clone_exclude_keys
                            }
                        )

                        # A clone uses its own (target) domain setup, not the source's.
                        # Drop an inherited root-component when the target format does not
                        # expose a root host, so it isn't carried as inert config. Same
                        # applicability rule as validate_root_component.
                        if new_deployment.get("root-component"):
                            target_mode = new_deployment.get("domain-mode")
                            target_format = new_deployment.get("domain-format")
                            if target_mode != "nice-url" and target_format not in ROOT_COMPONENT_FORMAT_IDS:
                                new_deployment.pop("root-component", None)

                        # When the source's subdomain matches its deployment name, it means
                        # components share a single hostname and use paths to differentiate.
                        # Preserve this pattern for the clone by setting subdomain to the
                        # new deployment name.
                        source_subdomain = source_deployment.get("subdomain")
                        source_name = source_deployment.get("name")
                        if source_subdomain and source_subdomain == source_name:
                            new_deployment["subdomain"] = deployment_name
                            logger.info(
                                f"Source subdomain '{source_subdomain}' matches source deployment name, "
                                f"setting cloned subdomain to '{deployment_name}'"
                            )

                        # Set clone-from in dict format for proper status tracking
                        new_deployment["clone-from"] = {
                            "type": "deployment",
                            "reference": clone_from,
                            "mode": "once",
                        }
                    else:
                        raise ValueError(f"Source deployment '{clone_from}' not found in project '{project_name}'")

                # Assume missing parameters from project configuration
                if not new_deployment.get("cluster"):
                    # Use clusters from project root configuration
                    project_clusters = project_data.get("clusters", [])
                    if len(project_clusters) == 1:
                        new_deployment["cluster"] = project_clusters[0]
                    elif len(project_clusters) > 1:
                        logger.error(
                            f"Multiple clusters defined in project '{project_name}': {project_clusters}. Cluster must be specified explicitly for new deployment."
                        )
                        return {
                            "success": False,
                            "created": False,
                            "error": "Multiple clusters defined, cluster must be specified explicitly",
                            "error_type": "ambiguous_cluster",
                        }

                if not new_deployment.get("namespace"):
                    # Use project name as namespace (common pattern)
                    new_deployment["namespace"] = project_name

                if not new_deployment.get("repository"):
                    # Use repositories from project configuration
                    repositories = project_data.get("repositories", [])
                    if len(repositories) == 1:
                        new_deployment["repository"] = repositories[0]["name"]
                    elif len(repositories) > 1:
                        repo_names = [repo["name"] for repo in repositories]
                        error_msg = f"Multiple repositories defined in project '{project_name}': {repo_names}. Repository must be specified explicitly for new deployment."
                        logger.error(error_msg)
                        return {
                            "success": False,
                            "created": False,
                            "error": error_msg,
                            "error_type": "ambiguous_repository",
                        }
                    else:
                        error_msg = "No repositories found in project configuration"
                        logger.error(error_msg)
                        return {"success": False, "created": False, "error": error_msg, "error_type": "no_repositories"}

                # Add the new deployment to the project data
                project_data["deployments"].append(new_deployment)

                # Validate domain config when this call sets domain settings.
                if domain_format is not None or base_domain is not None or subdomain is not None:
                    domain_error = await self._enforce_domain_config(project_data, deployment_name)
                    if domain_error:
                        return {
                            "success": False,
                            "created": False,
                            "error": domain_error,
                            "error_type": "domain_validation",
                        }

                # Ensure unapproved domains/subdomains get request entries
                ensure_domain_requests(project_data, settings.CLUSTER_MANAGER)

                commit_message = f"Add deployment '{deployment_name}' to project '{project_name}'"
                if clone_from:
                    commit_message += f" (cloned from '{clone_from}')"

                # Validate (schema + structural) then save and commit through the single path
                await self.save_and_commit_project(project_data, commit_message)

                logger.info(f"Successfully created deployment '{deployment_name}' in project '{project_name}'")
                result_create: dict[str, Any] = {"success": True, "created": True, "error": None, "error_type": None}
                if normalized_warnings_create:
                    result_create["warnings"] = normalized_warnings_create
                return result_create

        except Exception as e:
            error_msg = f"Error upserting deployment '{deployment_name}': {e}"
            logger.exception(error_msg)
            return {"success": False, "created": False, "error": error_msg, "error_type": "internal_error"}

    async def add_component(
        self,
        name: str,
        image: str,
        deployment_names: list[str],
        component_type: str = "single",
        port: int | None = None,
        path: str = "/",
        services: list[str] | None = None,
        cpu_limit: str | None = None,
        memory_limit: str | None = None,
        env_vars: str | None = None,
        aliases: str | None = None,
        root: bool = False,
    ) -> dict[str, Any]:
        """
        Add a new component definition to the project and reference it in specified deployments.

        Args:
            name: Component name (must be K8s-compliant and unique within the project)
            image: Container image URL
            deployment_names: Deployments to add this component to (must already exist)
            component_type: Component type (e.g. "single", "frontend", "backend")
            port: Inbound port (None for background workers that don't serve HTTP)
            path: Ingress path (only relevant if publish-on-web is in services)
            services: Component's services list (e.g. ["postgresql-database"])
            cpu_limit: CPU limit (e.g. "500m")
            memory_limit: Memory limit (e.g. "512Mi")
            env_vars: User environment variables in KEY=value format (will be AGE-encrypted)
            aliases: YAML string of alias definitions (e.g. "DATABASE_URL: $HOST:$PORT/$DB_NAME")
            root: Whether this is the root component (nice-url mode)

        Returns:
            Result dict with success status, component config, and deployments updated
        """
        try:
            project_data = await self.get_contents()
            project_name = await self.get_name()

            # Validate component name is unique
            existing_components = project_data.get("components", [])
            for comp in existing_components:
                if comp.get("name") == name:
                    return {
                        "success": False,
                        "error": f"Component '{name}' already exists in project '{project_name}'",
                        "error_type": "duplicate_component",
                    }

            # Validate all deployment_names exist
            existing_deployments = project_data.get("deployments", [])
            existing_deployment_names = {d.get("name") for d in existing_deployments}
            invalid_deployments = [d for d in deployment_names if d not in existing_deployment_names]
            if invalid_deployments:
                return {
                    "success": False,
                    "error": f"Deployments not found: {invalid_deployments}",
                    "error_type": "invalid_deployments",
                }

            # Validate path uniqueness per target deployment
            for deployment in existing_deployments:
                dep_name = deployment.get("name")
                if dep_name not in deployment_names:
                    continue
                domain_mode = deployment.get("domain-mode", "component-specific")

                # Collect existing component paths in this deployment
                existing_paths = []
                for comp_ref in deployment.get("components", []):
                    comp_ref_name = comp_ref.get("reference")
                    if comp_ref_name:
                        for comp_def in existing_components:
                            if comp_def.get("name") == comp_ref_name:
                                existing_paths.append(comp_def.get("path", "/"))
                                break

                # Validate path uniqueness (including the new component)
                try:
                    validate_component_paths([*existing_paths, path], domain_mode)
                except ComponentValidationError as e:
                    return {
                        "success": False,
                        "error": str(e),
                        "error_type": "validation_error",
                    }

                # Validate root component constraints
                if root:
                    dep_component_names = [
                        c.get("reference") for c in deployment.get("components", []) if c.get("reference")
                    ]
                    try:
                        validate_root_component(
                            name, [*dep_component_names, name], domain_mode, deployment.get("domain-format")
                        )
                    except ComponentValidationError as e:
                        return {
                            "success": False,
                            "error": str(e),
                            "error_type": "validation_error",
                        }

            # Validate requested services against project-level services
            if services:
                project_service_names = set(
                    ServiceAdapter.extract_service_names_from_project_services(project_data.get("services", []))
                )
                invalid_services = [s for s in services if s not in project_service_names]
                if invalid_services:
                    return {
                        "success": False,
                        "error": f"Services not defined on project: {invalid_services}. Available services: {sorted(project_service_names) if project_service_names else 'none'}",
                        "error_type": "invalid_services",
                    }

            # Normalize image
            normalized_image, was_normalized = normalize_container_image(image)
            warnings: list[str] = []
            if was_normalized:
                warnings.append(f"Image was normalized to lowercase: '{image}' -> '{normalized_image}'")

            # Build component config using shared function
            public_key = get_project_public_key(project_data)
            try:
                component_config = await build_component_config(
                    name=name,
                    component_type=component_type,
                    port=port,
                    path=path,
                    services=services or [],
                    cpu_limit=cpu_limit,
                    memory_limit=memory_limit,
                    env_vars=env_vars,
                    aliases=aliases,
                    public_key=public_key,
                )
            except (ComponentValidationError, ValueError) as e:
                return {
                    "success": False,
                    "error": str(e),
                    "error_type": "validation_error",
                }

            # Add component to project
            if "components" not in project_data:
                project_data["components"] = []
            project_data["components"].append(component_config)

            # Add component reference to specified deployments
            deployments_updated = []
            for deployment in existing_deployments:
                dep_name = deployment.get("name")
                if dep_name in deployment_names:
                    existing_refs = {c.get("reference") for c in deployment.get("components", [])}
                    if name not in existing_refs:
                        ref: dict[str, Any] = {"reference": name, "image": normalized_image}
                        deployment.setdefault("components", []).append(ref)
                        if root:
                            deployment["root-component"] = name
                        deployments_updated.append(dep_name)

            # Validate (schema + structural) then save and commit through the single path
            commit_message = f"Add component '{name}' to project '{project_name}'"
            if deployments_updated:
                commit_message += f" (deployments: {', '.join(deployments_updated)})"
            await self.save_and_commit_project(project_data, commit_message)

            logger.info(f"Successfully added component '{name}' to project '{project_name}'")
            result: dict[str, Any] = {
                "success": True,
                "component": component_config,
                "deployments_updated": deployments_updated,
            }
            if warnings:
                result["warnings"] = warnings
            return result

        except Exception as e:
            error_msg = f"Error adding component '{name}': {e}"
            logger.exception(error_msg)
            return {"success": False, "error": "An internal error occurred", "error_type": "internal_error"}

    async def delete_component(self, component_name: str) -> dict[str, Any]:
        """Remove a component definition from the project.

        Mirrors add_component's read-mutate-save-commit lifecycle so the deletion goes through
        the single ProjectManager path (fresh contents from Git, save, commit+push). It never
        builds the commit from the in-memory cache, which could lag behind Git and silently
        overwrite components added since the cache was last refreshed.
        """
        try:
            project_data = await self.get_contents()
            project_name = await self.get_name()

            components = project_data.get("components", []) or []
            if not any(isinstance(c, dict) and c.get("name") == component_name for c in components):
                return {
                    "success": False,
                    "error": f"Component '{component_name}' niet gevonden",
                    "error_type": "not_found",
                }

            project_data["components"] = [
                c for c in components if not (isinstance(c, dict) and c.get("name") == component_name)
            ]

            await self.save_and_commit_project(
                project_data, f"Remove component '{component_name}' from project '{project_name}'"
            )

            logger.info(f"Successfully removed component '{component_name}' from project '{project_name}'")
            return {"success": True}

        except Exception as e:
            error_msg = f"Error removing component '{component_name}': {e}"
            logger.exception(error_msg)
            return {"success": False, "error": "An internal error occurred", "error_type": "internal_error"}

    async def remove_attachment(self, attachment_id: str) -> dict[str, Any]:
        """Remove an attachment from the project's catalog.

        Idempotent (already-absent is a no-op success) and refuses removal while the attachment
        is still referenced. Uses the single ProjectManager path: fresh contents from Git, save,
        commit and push, with no cache in the mutate-commit path.
        """
        try:
            project_data = await self.get_contents()
            project_name = await self.get_name()

            data = find_attachment_data_list(project_data.get("services"))
            if data is None or not any(isinstance(e, dict) and e.get("id") == attachment_id for e in data):
                return {"success": True, "changed": False}

            if attachment_is_referenced(project_data, attachment_id):
                return {
                    "success": False,
                    "error": f"Bijlage '{attachment_id}' is in gebruik en kan niet worden verwijderd",
                    "error_type": "in_use",
                }

            data[:] = [e for e in data if not (isinstance(e, dict) and e.get("id") == attachment_id)]

            await self.save_and_commit_project(
                project_data, f"Remove attachment '{attachment_id}' from project '{project_name}'"
            )

            logger.info(f"Removed attachment '{attachment_id}' from project '{project_name}'")
            return {"success": True, "changed": True}

        except Exception as e:
            error_msg = f"Error removing attachment '{attachment_id}': {e}"
            logger.exception(error_msg)
            return {"success": False, "error": "An internal error occurred", "error_type": "internal_error"}

    async def add_service(
        self,
        service_name: str,
        component_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Add a service to the project's services list.

        The service (and any auto-resolved dependencies) is added at the
        project level.  Optionally, the listed components' ``services``
        lists are updated as well.

        Args:
            service_name: Service to add (e.g. ``"postgresql-database"``).
            component_names: Optional component names whose ``services``
                lists should also be updated.

        Returns:
            Result dict with ``success``, ``services_added``,
            ``services_skipped``, ``components_updated``, and ``warnings``.
        """
        try:
            project_data = await self.get_contents()
            project_name = await self.get_name()

            try:
                result = ServiceAdapter.add_services_to_project(project_data, [service_name], component_names)
            except ServiceValidationError as e:
                error_type = "invalid_components" if "Components not found" in str(e) else "invalid_service"
                return {"success": False, "error": str(e), "error_type": error_type}

            # Persist changes only when something was actually added
            if result["services_added"]:
                added = ", ".join(result["services_added"])
                commit_message = f"Add service(s) {added} to project '{project_name}'"
                await self.save_and_commit_project(project_data, commit_message)

            logger.info(
                f"Add service '{service_name}' to project '{project_name}': "
                f"added={result['services_added']}, skipped={result['services_skipped']}"
            )

            return {"success": True, **result}

        except Exception as e:
            error_msg = f"Error adding service '{service_name}': {e}"
            logger.exception(error_msg)
            return {"success": False, "error": "An internal error occurred", "error_type": "internal_error"}

    async def add_component_to_deployment(
        self,
        deployment_name: str,
        component_name: str,
        image: str,
    ) -> dict[str, Any]:
        """
        Add an existing component to a deployment that doesn't yet include it.

        Args:
            deployment_name: Name of the target deployment
            component_name: Name of an existing component in the project
            image: Container image URL for this deployment

        Returns:
            Result dict with success status and component reference added
        """
        try:
            project_data = await self.get_contents()
            project_name = await self.get_name()

            # Validate deployment exists
            existing_deployments = project_data.get("deployments", [])
            target_deployment = None
            for deployment in existing_deployments:
                if deployment.get("name") == deployment_name:
                    target_deployment = deployment
                    break

            if target_deployment is None:
                return {
                    "success": False,
                    "error": f"Deployment '{deployment_name}' not found in project '{project_name}'",
                    "error_type": "deployment_not_found",
                }

            # Validate component exists in project
            existing_components = project_data.get("components", [])
            component_def = None
            for comp in existing_components:
                if comp.get("name") == component_name:
                    component_def = comp
                    break

            if component_def is None:
                return {
                    "success": False,
                    "error": f"Component '{component_name}' not found in project '{project_name}'",
                    "error_type": "component_not_found",
                }

            # Validate component is not already in this deployment
            existing_refs = {c.get("reference") for c in target_deployment.get("components", [])}
            if component_name in existing_refs:
                return {
                    "success": False,
                    "error": f"Component '{component_name}' is already in deployment '{deployment_name}'",
                    "error_type": "duplicate_component_in_deployment",
                }

            # Validate path uniqueness
            domain_mode = target_deployment.get("domain-mode", "component-specific")
            new_path = component_def.get("path", "/")

            existing_paths = []
            for comp_ref in target_deployment.get("components", []):
                comp_ref_name = comp_ref.get("reference")
                if comp_ref_name:
                    for existing_comp in existing_components:
                        if existing_comp.get("name") == comp_ref_name:
                            existing_paths.append(existing_comp.get("path", "/"))
                            break

            try:
                validate_component_paths([*existing_paths, new_path], domain_mode)
            except ComponentValidationError as e:
                return {
                    "success": False,
                    "error": str(e),
                    "error_type": "validation_error",
                }

            # Normalize image
            normalized_image, was_normalized = normalize_container_image(image)
            warnings: list[str] = []
            if was_normalized:
                warnings.append(f"Image was normalized to lowercase: '{image}' -> '{normalized_image}'")

            # Add component reference to the deployment
            component_ref: dict[str, Any] = {"reference": component_name, "image": normalized_image}
            target_deployment.setdefault("components", []).append(component_ref)

            # Validate (schema + structural) then save and commit through the single path
            commit_message = (
                f"Add component '{component_name}' to deployment '{deployment_name}' in project '{project_name}'"
            )
            await self.save_and_commit_project(project_data, commit_message)

            logger.info(
                f"Successfully added component '{component_name}' to deployment '{deployment_name}' "
                f"in project '{project_name}'"
            )
            result: dict[str, Any] = {
                "success": True,
                "component_reference": component_ref,
            }
            if warnings:
                result["warnings"] = warnings
            return result

        except Exception as e:
            error_msg = f"Error adding component '{component_name}' to deployment '{deployment_name}': {e}"
            logger.exception(error_msg)
            return {"success": False, "error": "An internal error occurred", "error_type": "internal_error"}

    async def upsert_registry_by_secret(
        self,
        name: str,
        url: str,
        secret_name: str,
    ) -> dict[str, Any]:
        """
        Add or update a registry that references a pre-existing Kubernetes secret.

        Args:
            name: Unique registry identifier
            url: Registry URL (without protocol, may include path)
            secret_name: Name of an existing kubernetes.io/dockerconfigjson secret

        Returns:
            Result dict with success status and whether it was created or updated
        """
        project_data = await self.get_contents()
        project_name = await self.get_name()

        registries = project_data.get("registries", [])
        registry_entry = {"name": name, "url": url, "secretName": secret_name}

        created = True
        for i, reg in enumerate(registries):
            if reg.get("name") == name:
                registries[i] = registry_entry
                created = False
                break
        else:
            registries.append(registry_entry)

        project_data["registries"] = registries

        action = "Add" if created else "Update"
        await self.save_and_commit_project(
            project_data, f"{action} registry '{name}' (secretName) in project '{project_name}'"
        )

        logger.info(f"Successfully {'added' if created else 'updated'} registry '{name}' in project '{project_name}'")
        return {"success": True, "created": created, "registry": registry_entry}

    async def upsert_registry_by_credentials(
        self,
        name: str,
        url: str,
        username: str,
        password: str,
    ) -> dict[str, Any]:
        """
        Add or update a registry with username/password credentials.

        The password is AGE-encrypted with the project's public key before storing.

        Args:
            name: Unique registry identifier
            url: Registry URL (without protocol, may include path)
            username: Registry username or token name
            password: Registry password or token (will be AGE-encrypted)

        Returns:
            Result dict with success status and whether it was created or updated
        """
        project_data = await self.get_contents()
        project_name = await self.get_name()

        public_key = get_project_public_key(project_data)
        if not public_key:
            return {
                "success": False,
                "error": "Project has no AGE public key configured",
                "error_type": "missing_public_key",
            }

        encrypted_password = LiteralScalarString(await encrypt_age_content(password, public_key))

        registries = project_data.get("registries", [])
        registry_entry = {"name": name, "url": url, "username": username, "password": encrypted_password}

        created = True
        for i, reg in enumerate(registries):
            if reg.get("name") == name:
                registries[i] = registry_entry
                created = False
                break
        else:
            registries.append(registry_entry)

        project_data["registries"] = registries

        action = "Add" if created else "Update"
        await self.save_and_commit_project(
            project_data, f"{action} registry '{name}' (credentials) in project '{project_name}'"
        )

        logger.info(f"Successfully {'added' if created else 'updated'} registry '{name}' in project '{project_name}'")
        return {"success": True, "created": created, "registry": {"name": name, "url": url, "username": username}}

    async def update_image_and_regenerate(
        self,
        deployment_name: str,
        component_name: str,
        new_image_url: str,
        service_actions: dict[str, dict[str, dict[str, dict[str, str]]]] | None = None,
        registry: str | None = None,
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

        # 3. Find deployment (raise ValueError if not found)
        deployment = await self.get_deployment_by_name(deployment_name)
        if not deployment:
            raise ValueError(f"Deployment '{deployment_name}' not found in project '{project_name}'")

        # 3. Find component in deployment
        component_found = False
        old_image = None
        for comp in deployment.get("components", []):
            if comp.get("reference") == component_name:
                component_found = True
                old_image = comp.get("image")
                comp["image"] = new_image_url
                if registry:
                    comp["registry"] = registry
                    logger.info(f"Set registry '{registry}' on component '{component_name}'")
                break

        if not component_found:
            raise ValueError(
                f"Component '{component_name}' not found in deployment '{deployment_name}' of project '{project_name}'"
            )

        logger.info(f"Updated image: {old_image} -> {new_image_url}")

        # Re-enable component if it was disabled due to an image pull error
        is_disabled, disabled_reason = self._project_file_handler.extract_deployment_component_disabled(
            project_data, deployment_name, component_name
        )
        if is_disabled and is_image_pull_disable_reason(disabled_reason):
            self._project_file_handler.set_deployment_component_disabled(
                project_data, deployment_name, component_name, False, ""
            )
            logger.info(f"Re-enabled component '{component_name}' (was disabled: {disabled_reason})")

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
        await self.save_and_commit_project(project_data, commit_msg)
        logger.info("Saved and committed project YAML changes")

        # 7. CRITICAL: Process entire project for this deployment
        # This ensures all resources are created/updated (not just manifests)
        # - Namespaces, SOPS secrets
        # - Database, MinIO, Keycloak resources
        # - Application manifests (including new PVC generations)
        # - ArgoCD resources
        logger.info(f"Processing deployment {deployment_name} to apply all changes")
        process_success = await self.process_project(deployment_name)

        if not process_success:
            raise Exception(f"Failed to process deployment {deployment_name}")

        # 8. Trigger ArgoCD sync
        logger.info("Triggering ArgoCD sync for updated deployment")
        argo_connector = create_argo_connector()

        # Refresh user-applications (contains project definitions)
        await argo_connector.refresh_application("user-applications")

        # Refresh the specific deployment application
        app_name = generate_argocd_application_name(project_name, deployment_name)
        if await argo_connector.application_exists(app_name):
            logger.info(f"Refreshing ArgoCD application: {app_name}")
            await argo_connector.refresh_application(app_name)

        # Schedule fire-and-forget health watcher so an image bump to a missing
        # image (ImagePullBackOff) gets auto-disabled, same as the deploy path.
        from opi.services.oom_watcher import schedule_oom_check

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

        logger.info(f"Image update completed successfully: {result}")
        return result

    def _validate_component_references(
        self, project_data: dict, components: list, context: str = "deployment"
    ) -> dict[str, Any]:
        """
        Validate that all component references exist in the project.

        Args:
            project_data: The project data containing component definitions
            components: List of ComponentReference objects or dicts with 'reference' key
            context: Context for error messages (e.g. "deployment", "update")

        Returns:
            Dict with validation result: {"success": bool, "error": str | None, "invalid_references": list | None}
        """
        project_components = project_data.get("components", [])
        component_names = {comp.get("name") for comp in project_components}
        invalid_references = []

        for component in components:
            # Handle both ComponentReference objects and dict format
            reference = getattr(component, "reference", None) or component.get("reference")

            if reference not in component_names:
                invalid_references.append(reference)

        if invalid_references:
            available_components = list(component_names) if component_names else ["none"]
            project_name = project_data.get("name", "unknown")
            error_msg = f"Invalid component references in {context} for project '{project_name}': {invalid_references}. Available components: {available_components}"
            logger.error(error_msg)
            return {"success": False, "error": error_msg, "invalid_references": invalid_references}

        return {"success": True, "error": None, "invalid_references": None}

    async def update_project_field_by_path(
        self, project_name: str, json_path: str, new_value: Any, commit_message: str
    ) -> bool:
        """
        Update a specific field in a project file using JSON path and commit the change.

        Args:
            project_name: Name of the project
            json_path: JSON path to the field (e.g., "deployments[?(@.name=='api')].components[?(@.reference=='backend')].image")
            new_value: New value to set
            commit_message: Commit message for the change

        Returns:
            True if update was successful, False otherwise
        """
        try:
            project_data = await self.get_contents()
            jsonpath_expr = jsonpath_parse(json_path)
            matches = jsonpath_expr.find(project_data)

            if not matches:
                logger.error(f"JSON path '{json_path}' not found in project {project_name}")
                return False

            # Update the first match (there should typically be only one)
            matches[0].full_path.update(project_data, new_value)

            logger.info(f"Successfully updated {json_path} in project {project_name}")
            return True

        except Exception as e:
            logger.exception(f"Error updating project field for {project_name}: {e}")
            return False

    async def find_value_by_jsonpath(self, json_path: str, default: Any = None) -> Any:
        """
        Retrieve a specific value from project configuration using JSONPath.

        This method provides a reusable way to extract any value from the project YAML
        using JSONPath expressions like 'config.api-key' or 'deployments[0].cluster'.

        Args:
            json_path: JSONPath expression to extract the value
            default: Default value to return if path not found

        Returns:
            The extracted value or default if not found

        Raises:
            HTTPException: If project not found or parsing fails
        """
        try:
            project_data = await self.get_contents()
            return find_value_by_jsonpath(project_data, json_path, default)
        except Exception as e:
            logger.exception(f"Error extracting value from project at path '{json_path}': {e}")
            raise HTTPException(status_code=500, detail=f"Error extracting project value: {e!s}")

    async def clone_deployment(
        self, project_name: str, target_deployment_name: str, source_deployment_name: str, force_clone: bool = False
    ) -> dict[str, Any]:
        """
        Clone resources from source deployment to target deployment.

        This method orchestrates cloning of:
        1. Database resources (schema, user, data)
        2. MinIO resources (bucket, user, objects)

        By default, cloning only happens on initial setup when target resources don't exist yet.
        Use force_clone=True to clone even if target resources already exist.

        Args:
            project_name: Name of the project
            target_deployment_name: Name of the target deployment
            source_deployment_name: Name of the source deployment to clone from
            force_clone: If True, clone even if target resources already exist (default: False)

        Returns:
            Dictionary containing clone results and status

        Raises:
            HTTPException: If critical operations fail
        """
        clone_results = {
            "project": project_name,
            "source_deployment": source_deployment_name,
            "target_deployment": target_deployment_name,
            "operations": [],
            "success": True,
            "errors": [],
        }

        logger.info(
            f"Starting clone operation: {source_deployment_name} -> {target_deployment_name} for project {project_name}"
        )

        try:
            # Load project data
            project_data = await self.config_handler.get_project_file_content(project_name)
            if not project_data:
                raise HTTPException(status_code=404, detail=f"Project {project_name} not found")

            # Find target deployment using helper method
            target_deployment = await self.get_deployment_by_name(target_deployment_name)

            if not target_deployment:
                raise HTTPException(status_code=404, detail=f"Target deployment {target_deployment_name} not found")

            # Verify source deployment exists using helper method
            source_deployment = await self.get_deployment_by_name(source_deployment_name)

            if not source_deployment:
                raise HTTPException(status_code=404, detail=f"Source deployment {source_deployment_name} not found")

            # Clone database resources if target deployment uses PostgreSQL
            if await self.database_manager._deployment_uses_postgresql(project_data, target_deployment_name):
                try:
                    logger.info(f"Cloning database resources from {source_deployment_name} to {target_deployment_name}")
                    await self.database_manager.clone_database_from_deployment(
                        project_data, target_deployment, source_deployment_name, force_clone
                    )
                    clone_results["operations"].append(
                        {
                            "type": "database_clone",
                            "status": "success",
                            "message": f"Successfully cloned database from {source_deployment_name}",
                        }
                    )
                except Exception as e:
                    logger.exception(f"Failed to clone database resources: {e}")
                    clone_results["errors"].append(f"Database clone failed: {e}")
                    clone_results["operations"].append({"type": "database_clone", "status": "failed", "error": str(e)})

            # Clone MinIO resources if target deployment uses MinIO
            if await self._minio_manager._deployment_uses_minio(project_data, target_deployment_name):
                try:
                    logger.info(f"Cloning MinIO resources from {source_deployment_name} to {target_deployment_name}")
                    await self._minio_manager.clone_minio_from_deployment(
                        project_data, target_deployment, source_deployment_name
                    )
                    clone_results["operations"].append(
                        {
                            "type": "minio_clone",
                            "status": "success",
                            "message": f"Successfully cloned MinIO from {source_deployment_name}",
                        }
                    )
                except Exception as e:
                    logger.exception(f"Failed to clone MinIO resources: {e}")
                    clone_results["errors"].append(f"MinIO clone failed: {e}")
                    clone_results["operations"].append({"type": "minio_clone", "status": "failed", "error": str(e)})

            # Update success status based on errors
            clone_results["success"] = len(clone_results["errors"]) == 0

            if clone_results["success"]:
                logger.info(
                    f"Clone operation completed successfully: {source_deployment_name} -> {target_deployment_name}"
                )
            else:
                logger.warning(f"Clone operation completed with errors: {clone_results['errors']}")

            return clone_results

        except HTTPException:
            raise
        except Exception as e:
            logger.exception(f"Error during clone operation: {e}")
            raise HTTPException(status_code=500, detail=f"Clone operation failed: {e!s}") from e

    async def validate_project_api_key(self, project_name: str, provided_api_key: str) -> bool:
        """
        Validate that the provided API key matches the project's API key.

        This method uses the reusable encrypted value retrieval system to get and decrypt
        the project's API key, then compares it with the provided key.

        Args:
            project_name: Name of the project
            provided_api_key: API key provided in the request header

        Returns:
            True if the API key is valid

        Raises:
            HTTPException: If project not found or API key is invalid
        """
        try:
            # Get the raw API key value
            raw_api_key = await self.find_value_by_jsonpath(project_name, "config.api-key")
            if raw_api_key is None:
                raise HTTPException(status_code=404, detail=f"No API key found for project '{project_name}'")

            # Use the smart decryption logic from age.py
            decrypted_api_key = await decrypt_password_smart_auto(str(raw_api_key))

            # Compare API keys
            if not secrets.compare_digest(decrypted_api_key, provided_api_key):
                raise HTTPException(status_code=401, detail="Invalid project API key")

            logger.debug(f"Project API key validated successfully for project: {project_name}")
            return True

        except HTTPException:
            raise
        except Exception as e:
            logger.exception(f"Error validating project API key for {project_name}: {e}")
            raise HTTPException(status_code=500, detail=f"Error validating project API key: {e!s}") from e

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

        This method is for manual API operations, not project-file-based cloning.
        """

        logger.info(
            f"Manual database clone via tunnel: {project_name}/{deployment_name} "
            f"<- {tunnel_remote_host}:{tunnel_remote_port} (via {tunnel_server_url})"
        )

        connector = ChiselConnector(
            server_url=tunnel_server_url,
            username=tunnel_username,
            password=tunnel_password,
        )

        try:
            _ = connector.start_tunnel(
                remote_host=tunnel_remote_host,
                remote_port=tunnel_remote_port,
            )

            endpoint = connector.get_local_endpoint()
            logger.info(f"Tunnel established: {endpoint['host']}:{endpoint['port']}")

            db_manager = await self._ensure_database_manager()
            result = await db_manager.clone_database_from_external_source(
                project_name=project_name,
                deployment_name=deployment_name,
                source_host=endpoint["host"],
                source_port=endpoint["port"],
                source_username=source_username,
                source_password=source_password,
                source_database=source_database,
                source_schema=source_schema,
                force_clone=force_clone,
            )

            result["tunnel"] = {
                "used": True,
                "server": tunnel_server_url,
                "local_endpoint": f"{endpoint['host']}:{endpoint['port']}",
                "remote_endpoint": f"{tunnel_remote_host}:{tunnel_remote_port}",
            }

            return result

        finally:
            connector.stop_tunnel()
            logger.info("Tunnel cleaned up")

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

        This method is for manual API operations, not project-file-based cloning.
        """
        logger.info(
            f"Manual MinIO clone via tunnel: {project_name}/{deployment_name} "
            f"<- {tunnel_remote_host}:{tunnel_remote_port}/{source_bucket} (via {tunnel_server_url})"
        )

        connector = ChiselConnector(
            server_url=tunnel_server_url,
            username=tunnel_username,
            password=tunnel_password,
        )

        try:
            _ = connector.start_tunnel(
                remote_host=tunnel_remote_host,
                remote_port=tunnel_remote_port,
            )

            endpoint = connector.get_local_endpoint()
            logger.info(f"Tunnel established: {endpoint['host']}:{endpoint['port']}")

            result = await self._minio_manager.clone_bucket_from_external_source(
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
                "server": tunnel_server_url,
                "local_endpoint": f"{endpoint['host']}:{endpoint['port']}",
                "remote_endpoint": f"{tunnel_remote_host}:{tunnel_remote_port}",
            }

            return result

        finally:
            connector.stop_tunnel()
            logger.info("Tunnel cleaned up")

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
    async def delete_deployment(self, project_name: str, deployment_name: str, force: bool = False) -> dict[str, Any]:
        """
        Delete all resources associated with a specific deployment.

        Args:
            project_name: Name of the project
            deployment_name: Name of the deployment to delete
            force: If True, continues on errors and cleans up stuck resources

        Returns:
            Dictionary containing deletion results
        """
        return await self._delete_project_manager.delete_deployment(project_name, deployment_name, force=force)

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
