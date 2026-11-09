"""
The project manager handles project files. It can read, update, delete, or process them.
Processing means it can create, update, or delete any resources defined in a project file.
"""

import asyncio
import base64
import copy
import glob
import logging
import os
import posixpath
import shutil
import tempfile
import time
from collections.abc import (  # noqa: TC003 - used in an eagerly-evaluated annotation (no future-annotations)
    Callable,
    Iterator,
)
from contextlib import AbstractAsyncContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar, cast

import asyncpg
from sqlalchemy.exc import SQLAlchemyError

from opi.connectors import create_argo_connector
from opi.connectors.git import GitConnector, repo_lock_key
from opi.connectors.git_repo import (
    GitRepoConflictError,
    GitRepoWorkspace,
    argo_workspace,
    credentials_from_repo_config,
    get_git_repo,
    workspace_from_repo_config,
)
from opi.connectors.kubectl import KubectlConnector
from opi.core.actor import get_actor
from opi.core.cluster_config import (
    get_argo_namespace,
    get_backup_namespace,
    get_ca_certificate_config,
    get_cluster_cidrs,
    get_external_dns_target_for_hostname,
    get_infrastructure_namespace,
    get_ingress_cluster_issuer,
    get_ingress_controller_selector,
    get_ingress_ip_whitelist,
    get_ingress_postfix,
    get_ingress_tls_enabled,
    get_namespace,
    get_ops_egress,
    get_prefixed_namespace,
    is_reserved_project_name,
    supports_vpa,
)
from opi.core.config import settings
from opi.core.metrics import (
    PHASE_AFTER_SYNC,
    PHASE_GENERATE,
    PHASE_PROVISION,
    rollout_phase,
)
from opi.generation.manifests import (
    CONFIG_HASH_IGNORE_LABEL_KEY,
    CONFIG_HASH_IGNORE_LABEL_VALUE,
    MANIFEST_TEMPLATES_DIR,
    ManifestGenerator,
)
from opi.manager.argo_manager import (
    APPLICATIE_AANMAAK_TIMEOUT_SECONDEN,
    ApplicationGone,
)
from opi.manager.project.aliases import (
    DeploymentAliases,
    generate_storage_env_vars_from_services,
    generate_web_env_vars_from_services,
    resolve_aliases,
    scope_direct_aliases_to_component,
)
from opi.manager.project.argocd_sync import ArgoCDSync
from opi.manager.project.attachment_mutations import AttachmentMutations
from opi.manager.project.component_mutations import ComponentMutations
from opi.manager.project.database_schema_mutations import DatabaseSchemaMutations
from opi.manager.project.deployment_mutations import DeploymentMutations
from opi.manager.project.emitters import emit_letsencrypt_issuer, emit_namespace_network_policies
from opi.manager.project.helm_deployment import (
    HelmDeployment,
    deployment_uses_helm_charts,
    deployment_uses_helmfile,
)
from opi.manager.project.infrastructure import Infrastructure
from opi.manager.project.registry_mutations import RegistryMutations
from opi.manager.project.service_config_mutations import ServiceConfigMutations
from opi.manager.project_validation import validate_project_structure
from opi.manager.revision_manager import RevisionManager
from opi.manager.run_support import resolve_image
from opi.services import ServiceAdapter, ServiceType
from opi.services.argocd_operation import (
    describe_sync_waiting,
)
from opi.services.catalog.base import (
    DeploymentManifestContext,
    ManifestContext,
    ManifestContribution,
    ProjectManifestContext,
    ProvisionContext,
    SecretFileSpec,
)
from opi.services.catalog.image_registries.manifest_pass import apply_rules_to_directory
from opi.services.catalog.image_registries.resolution import (
    build_rules,
    resolve_deployment_component_image,
    set_deployment_component_registry,
)
from opi.services.catalog.publish_on_web.domain_config import (
    DomainSetting,
    get_domain_setting,
)
from opi.services.catalog.publish_on_web.issuer import effective_issuer
from opi.services.catalog.publish_on_web.subdomain import (
    get_supported_base_domains,
    is_deployment_domain_approved,
    validate_bare_domain_allowed,
)
from opi.services.catalog.publish_on_web.urls import public_url_map_for_deployment
from opi.services.deployment_order import order_deployments_by_clone_dependency
from opi.services.persistence.subdomain_registry import SubdomainConnector
from opi.services.postgres_scope import project_uses_dedicated_postgres
from opi.services.project import Project
from opi.services.project_file_handler import (
    ProjectFileHandler,
    extract_service_names_from_component,
    image_is_confirmed_absent,
)
from opi.services.project_key_secret import ProjectKeySecretHandler
from opi.services.project_reconciliation_service import record_reconciliation
from opi.services.project_schema import ProjectIntegrityError, validate_project_schema
from opi.services.project_store import get_project_store
from opi.services.project_utils import (
    RESERVED_PROJECT_NAME_MESSAGE,
    normalize_container_image,
    project_level_deployment,
    resolve_deployment_filter,
)
from opi.services.redeploy import run_redeploy_hooks
from opi.services.registry import (
    deployment_manifest_services,
    manifest_services,
    project_manifest_services,
    provisioning_services,
)
from opi.services.secrets import (
    BaseSecret,
    KeycloakSecret,
    PlatformSecret,
    UserSecret,
)
from opi.services.tasks import progress_steps as steps
from opi.services.tasks.async_task_service import AsyncTaskService
from opi.services.tasks.task_errors import TaskInputError
from opi.utils.age import (
    get_project_public_key,
    project_private_key_or_none,
)
from opi.utils.env_vars import (
    substitute_known_variables,
)

# Environment variables are now generated using service definitions
from opi.utils.naming import (
    ROOT_COMPONENT_FORMAT_IDS,
    RepositoryPathError,
    generate_argocd_application_name,
    generate_argocd_project_application_name,
    generate_bare_domain_hostname,
    generate_cluster_manifest_folder_path,
    generate_deployment_manifest_path,
    generate_ingress_name_from_path,
    generate_issuer_name,
    generate_manifest_name,
    generate_network_policy_manifest_name,
    generate_network_policy_name,
    generate_project_level_manifest_path,
    generate_project_manifest_folder_path,
    generate_project_service_account_name,
    generate_public_url,
    generate_pvc_name,
    generate_root_hostname,
    generate_storage_name,
    generate_tls_secret_name,
    generate_unique_name,
    get_component_ingress_map,
)
from opi.utils.sops import (
    SOPS_SUFFIX,
    TO_SOPS_SUFFIX,
    decrypt_sops_with_key,
    encrypt_to_sops_files_or_fail,
    sops_filenames,
)
from opi.utils.yaml_util import (
    YAMLError,
    load_yaml_safe,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from opi.connectors.argo import ArgoConnector
    from opi.manager.database_manager import DatabaseManager
    from opi.runtime.tasks.persistent_task_progress import PersistentTaskProgressManager
    from opi.services.catalog.image_registries.rules import ResolvedImage

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


def _remote_identity(repo_config: dict[str, str]) -> tuple[str, str, str]:
    """De remote waar een repo-config naar wijst: URL zonder credentials, branch en pad.

    Het pad hoort erbij omdat de connector het gebruikt om bestandspaden op te lossen; twee
    configs met een ander pad mogen dus geen werkboom delen.
    """
    return (
        repo_lock_key(repo_config.get("url", "")),
        repo_config.get("branch", "main"),
        (repo_config.get("path") or "").strip("/"),
    )


async def _deployments_being_deleted(project_name: str) -> dict[str, str]:
    """Deployments with an open delete task, mapped to that task's id (RC-254).

    Een mislukte opvraging levert niets op: dan regenereert de ronde zoals voor deze
    grendel, wat beter is dan een project niet verwerken omdat de takentabel stilstaat.
    """
    try:
        return await AsyncTaskService(cluster=settings.CLUSTER_MANAGER).find_open_delete_deployments(project_name)
    except (SQLAlchemyError, OSError, asyncpg.exceptions.PostgresError, asyncpg.exceptions.InterfaceError) as e:
        logger.warning("Kon de open delete-taken van project %s niet opvragen: %s", project_name, e)
        return {}


# Filename extensions used for generated per-component manifests. A file is only
# considered for pruning when it ends with one of these (covers plain manifests
# and both pre/post SOPS-encryption secret files).
_COMPONENT_MANIFEST_EXTENSIONS: tuple[str, ...] = (
    SOPS_SUFFIX,
    TO_SOPS_SUFFIX,
    ".yaml",
    ".yml",
)


def _is_generated(basename: str, generated_files: set[str]) -> bool:
    """Whether a file belongs to the desired state of this run.

    The ``.sops.yaml`` of a secret written this run as ``.to-sops.yaml`` is its previous
    ciphertext, which the skip-if-unchanged compares against after the prune.
    """
    if basename in generated_files:
        return True
    if basename.endswith(SOPS_SUFFIX):
        return sops_filenames(basename).plaintext in generated_files
    return False


def _existing_secret_pairs(sops_path: str, private_key: str | None) -> dict[str, str]:
    """The ``stringData`` of a previously written SOPS secret, empty on any doubt."""
    if private_key is None or not os.path.exists(sops_path):
        return {}
    decrypted = decrypt_sops_with_key(sops_path, private_key)
    if decrypted is None:
        return {}
    try:
        doc = load_yaml_safe(decrypted)
    except YAMLError:
        logger.info(f"Could not parse decrypted {os.path.basename(sops_path)}, writing fresh values")
        return {}
    string_data = doc.get("stringData") if isinstance(doc, dict) else None
    if not isinstance(string_data, dict):
        return {}
    return {key: value for key, value in string_data.items() if isinstance(value, str)}


def _attachment_secret_prefix(deployment_name: str) -> str:
    """Prefix of the Secrets that carry the file-mode attachments of a deployment."""
    return f"{deployment_name}-attch-"


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
        if _is_generated(basename, generated_files):
            continue
        # Only prune files that belong to a component; shared/deployment-level files
        # (no component prefix) are never selected.
        if _owning_component(basename) is not None:
            selected.append(basename)

    return sorted(selected)


def _select_obsolete_service_manifests(
    directory: str,
    service_prefixes: set[str],
    generated_files: set[str],
) -> list[str]:
    """Select deployment-wide, service-owned manifest files this run did not (re)generate.

    Symmetric with ``_select_obsolete_component_manifests`` but keyed on the service
    prefix ``f"{deployment_name}-{service_type.value}-"`` that every
    ``DeploymentManifestSpec`` filename must carry (RC-15). A service-owned deployment
    manifest has no component prefix, so the component prune never touches it; without
    this, a NetworkPolicy would linger after its service is switched off and keep the
    opening alive. ``*.marked-for-deletion.yaml`` files are left to the reconciliation
    lifecycle.

    Args:
        directory: Deployment manifest directory to scan (non-recursive).
        service_prefixes: One ``f"{deployment_name}-{svc.value}-"`` per ServiceType.
        generated_files: Basenames generated this run (the desired-state set).

    Returns:
        Sorted list of basenames to remove.
    """
    if not os.path.isdir(directory):
        return []
    selected: list[str] = []
    for path in glob.glob(os.path.join(directory, "*")):
        if not os.path.isfile(path):
            continue
        basename = os.path.basename(path)
        if not basename.endswith(_COMPONENT_MANIFEST_EXTENSIONS):
            continue
        if basename.endswith(".marked-for-deletion.yaml"):
            continue
        if _is_generated(basename, generated_files):
            continue
        if any(basename.startswith(prefix) for prefix in service_prefixes):
            selected.append(basename)
    return sorted(selected)


# Registry auth/permission failures (missing or expired pull secret, denied robot)
# are real, actionable errors worth an ERROR log + alert. A not-found / not-yet-built
# image is expected churn for CI/CD and PR builds and belongs at WARNING, not ERROR
# ("ERROR" should mean someone must act). We match only unambiguous auth markers;
# a bare "denied" is left out because some registries (e.g. ghcr) return it for a
# private-or-missing tag too, which would misclassify a not-built image as an error.
_IMAGE_PULL_AUTH_MARKERS = (
    "unauthorized",
    "authentication required",
    "authorization failed",
    "no basic auth credentials",
    "forbidden",
    "401",
    "403",
)


def _is_image_pull_auth_error(message: str | None) -> bool:
    """True when an image-pull failure is a registry auth/permission problem."""
    if not message:
        return False
    lowered = message.lower()
    return any(marker in lowered for marker in _IMAGE_PULL_AUTH_MARKERS)


# Markers that identify an ArgoCD manifest-generation failure in a manifests-endpoint body.
# These are the phrases ArgoCD's repo-server uses when a plugin (our SOPS/kustomize CMP)
# cannot render, so they distinguish a real render failure from a transport/auth error (a
# 401/404/network hiccup) that must not block the deploy.
_RENDER_FAILURE_MARKERS = (
    "failed to generate manifests",
    "error generating manifests",
    "manifest generation error",
    "rpc error",
)


def _looks_like_render_failure(detail: str | None) -> bool:
    """True when a failed manifests-endpoint body indicates a generation/render error."""
    if not detail:
        return False
    lowered = detail.lower()
    return any(marker in lowered for marker in _RENDER_FAILURE_MARKERS)


@dataclass
class DeploymentResult:
    """Result information for a processed deployment."""

    deployment_name: str
    cluster: str
    namespace: str
    urls: dict[str, str] = field(default_factory=dict)  # component_name -> public_url
    status: str = "success"
    errors: list[str] = field(default_factory=list)


def _secret_labels_for(spec: SecretFileSpec) -> dict[str, str]:
    """The Secret's metadata labels, including the config-hash opt-out when it applies.

    A service declares ``include_in_config_hash=False`` when only its own auxiliary pod
    reads the secret; translating that into the label happens here, once, for every
    secret the manager writes.
    """
    labels = dict(spec.secret_labels or {})
    if not spec.include_in_config_hash:
        labels[CONFIG_HASH_IGNORE_LABEL_KEY] = CONFIG_HASH_IGNORE_LABEL_VALUE
    return labels


def collect_manifest_contributions(
    ctx: ManifestContext, *, component_services: list[str], project_services: list[str]
) -> list[ManifestContribution]:
    """Every contributing service's ``ManifestContribution`` for one component.

    Which services are asked depends on where their selection lives. Most read the
    COMPONENT's own ``services`` list -- each component decides whether it sits behind
    login, gets database credentials, is scraped. A service that declares
    ``selectable_per_component=False`` has no per-component choice to make and reads the
    PROJECT's list instead; no component ever ticks such a service, so a
    component-scoped question would answer "no" for every component forever.

    Module-level so both halves of that rule can be measured without building a whole
    deployment first.
    """
    return [
        provider.contribute_manifest_context(ctx)
        for provider in manifest_services()
        if any(
            service_type.value
            in (component_services if provider.definition.selectable_per_component else project_services)
            for service_type in provider.manifest_activation_types()
        )
    ]


def apply_manifest_contributions(variables: dict[str, Any], contributions: list[ManifestContribution]) -> None:
    """Merge the contributions into a component's template context, in ``manifest_order``.

    Three merge semantics, and the difference is load-bearing:

    * ``template_vars`` OVERRIDE a base key (auth-wall moves ``service_port`` 8080 ->
      4180).
    * ``env_vars`` are ADDITIVE: they join the component's own variables instead of
      replacing them. That is why they are a field of their own rather than a
      ``template_vars`` entry -- as an override, one service handing out one variable
      would wipe every variable the component itself declared.
    * ``sidecars`` are additive too.
    """
    for contribution in contributions:
        variables.update(contribution.template_vars)
        if contribution.env_vars:
            variables["env_vars"] = {**variables.get("env_vars", {}), **contribution.env_vars}
        if contribution.sidecars:
            variables.setdefault("sidecars", []).extend(contribution.sidecars)


class ProjectManager:
    """Manager for project resources and deployments."""

    # Class-level default: the state get_contents() last returned, used as the
    # compare-and-swap base on save. Declared here so instances that bypass
    # __init__ (tests calling the unbound methods with a stand-in self) still
    # resolve it, and so the attribute has one documented home.
    __contents_as_read: dict[str, Any] | None = None

    #: Of het PROJECTNIVEAU deze ronde veranderde, en in welke repo het staat. True zolang
    #: het tegendeel niet gemeten is: de grendel in wait_for_project_level_application mag
    #: alleen op bewijs worden overgeslagen. Op klasseniveau, om dezelfde reden als
    #: hierboven: een aanroeper die __init__ overslaat moet ze toch kunnen lezen.
    _project_level_manifests_changed: bool = True
    _project_level_repo: str | None = None
    #: De map in die repo waar de project-level manifests staan, zodat te bewijzen is dat
    #: ArgoCD precies die bestanden al heeft toegepast.
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
        # Dezelfde werkmappen, geindexeerd op remote, zodat twee repo-configs met dezelfde
        # remote er een delen. Zie get_deployment_workspace.
        self.__deployment_workspaces_by_remote: dict[tuple[str, str, str], GitRepoWorkspace] = {}
        # Wegwerpmappen die niet bij een werkmap horen; zie materialize_cluster_tree.
        self.__scratch_dirs: list[str] = []
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
        # (project_data, compare-and-swap base) of the process_project run in progress
        self._process_run: tuple[dict[str, Any], dict[str, Any] | None] | None = None

        # Last processing error message (set when process_project fails)
        self._processing_error: str | None = None

        # Per-component failure details (set when DeploymentHealthError is caught)
        self._component_failures: list[dict[str, Any]] | None = None

        self._terminated_sync_operations: list[str] = []

        # Change context from YAML diff (populated by process_project_changes)
        # Structure: {"previous_yaml": {...}, "current_yaml": {...}, "changes": {"added": {}, "changed": {}, "deleted": {}}}
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
            if not skip_credential_check and self._database_manager._admin_password == "placeholder":  # noqa: S105  # placeholder tot de echte credentials er zijn, geen secret
                logger.info(
                    "DatabaseManager was initialized with placeholder credentials, reinitializing with real credentials"
                )
                await self._database_manager.close()
                self._database_manager = None
            else:
                return self._database_manager

        from opi.core.cluster_config import get_database_cluster_service_endpoint, get_infrastructure_namespace
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
                admin_password = "placeholder"  # noqa: S105  # placeholder tot de echte credentials er zijn, geen secret
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

        # Filter by CLUSTER_MANAGER if requested
        if cluster_filter:
            deployments = [d for d in deployments if d.get("cluster") == settings.CLUSTER_MANAGER]

        # Filter by deployment name(s) if requested. ``is not None`` (not truthiness)
        # so an empty target list correctly resolves to zero deployments.
        targets = resolve_deployment_filter(deployment_name, deployment_names)
        if targets is not None:
            target_set = set(targets)
            deployments = [d for d in deployments if d.get("name") in target_set]

        # A deployment that clones from another must be processed after its source, and
        # nothing enforced that: the loop walked file order, which happened to work only
        # because the files grew that way. Ordering here rather than at the call site
        # because this is the one door every caller comes through.
        #
        # Applied after the filters on purpose: ordering the ones you are going to use is
        # the point, and a source filtered away (another cluster, a single-deployment
        # request) cannot be waited for anyway.
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
        """Record the public addresses this deployment offers on its result.

        The addresses come from publish-on-web, the service that decides whether a component
        gets an ingress, so they are the same ones the deployment endpoint returns and the
        detail page links. Deriving them here from the naming alone is what made a refresh
        promise an address that nothing serves (RC-104, vraag 13): a component without an
        ingress has a name, not an address.
        """
        self._deployment_results[deployment["name"]].urls.update(
            public_url_map_for_deployment(project_data, deployment, project_name, self._project_file_handler)
        )

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
        """De deployments waarvan deze run een lopende sync-operation heeft beëindigd."""
        return self._terminated_sync_operations

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
            # Use the ProjectStore's warm, long-lived working copy instead of cloning
            # the projects repo per request. It is cloned once per process with full
            # history (blobless), so the file-scoped diff that detects removed
            # components/deployments still works, but an edit no longer pays for a
            # clone. The store owns this connector: we must never close it (close()
            # rmtree's the working directory), hence owns=False below.
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
        """Emit the sleep-mode waker for one component, or nothing.

        Renders the waker Deployment + ConfigMap and its SOPS token Secret behind the
        component's Service (same ``app`` label, ``zad-role: waker``) when the deployment
        is asleep/waking and this is the chosen waker component. The token comes from the
        deployment's own ``sleep.wake-token`` (minted on its first sleep). All files go into
        ``created_files`` so the obsolete-manifest prune removes them once awake.

        ``service_port`` is what this component's Service targets -- the value the caller
        already resolved for the Service itself, contributions included. The waker has to
        listen there or the Service selects a pod that answers nothing, so it is taken
        from the caller rather than assumed.
        """
        from opi.services.catalog.sleep_mode import config as sleep_config
        from opi.services.catalog.sleep_mode import manifests as sleep_manifests
        from opi.services.catalog.sleep_mode import state as sleep_state
        from opi.services.catalog.sleep_mode import token as sleep_token
        from opi.services.catalog.sleep_mode.secret import WakeTokenSecret
        from opi.services.catalog.sleep_mode.state import STATE_SLEEPING, STATE_WAKING

        deployment_name = deployment.get("name", "")
        config = sleep_config.load(project_data, cluster)
        if config is None or not config.waker:
            return
        current = sleep_state.read(project_data, deployment_name)
        if current.state not in (STATE_SLEEPING, STATE_WAKING):
            return
        selected = sleep_manifests.select_waker_component(project_data, deployment, config, self._project_file_handler)
        if selected != component_reference:
            return
        if not service_port:
            # No Service to sit behind, so nothing would reach the waker anyway. Same
            # honesty as the other no-waker branches: the deployment still sleeps and is
            # wakeable from the portal or the API.
            logger.warning(
                "sleep-mode: component '%s' on %s/%s has no service port; skipping waker manifests",
                component_reference,
                project_name,
                deployment_name,
            )
            return
        if not current.wake_token:
            logger.warning(
                "sleep-mode: no wake token stored for %s/%s; skipping waker manifests",
                project_name,
                deployment_name,
            )
            return

        generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Waker Deployment
        deployment_values = sleep_manifests.build_waker_deployment_values(
            app_name=unique_name,
            namespace=namespace,
            project_name=project_name,
            deployment_name=deployment_name,
            cluster=cluster,
            port=service_port,
            generated_at=generated_at,
        )
        deployment_template = os.path.join(MANIFEST_TEMPLATES_DIR, "deployment.yaml.jinja")
        deployment_manifest_name = generate_manifest_name(component_name, "waker-deployment")
        self._manifest_generator.create_manifest_file(
            template_path=deployment_template,
            values=deployment_values,
            output_dir=output_dir,
            output_filename=deployment_manifest_name,
            use_sops=False,
        )
        created_files.append(f"{deployment_manifest_name}.yaml")

        # Waker ConfigMap
        configmap_values = sleep_manifests.build_waker_configmap_values(
            app_name=unique_name,
            namespace=namespace,
            project_name=project_name,
            deployment_name=deployment_name,
            component_reference=component_reference,
            config=config,
            cluster=cluster,
            port=service_port,
        )
        configmap_template = os.path.join(MANIFEST_TEMPLATES_DIR, "configmap.yaml.jinja")
        configmap_manifest_name = generate_manifest_name(component_name, "waker-config")
        self._manifest_generator.create_manifest_file(
            template_path=configmap_template,
            values=configmap_values,
            output_dir=output_dir,
            output_filename=configmap_manifest_name,
            use_sops=False,
        )
        created_files.append(f"{configmap_manifest_name}.yaml")

        # Waker token Secret (SOPS), from the deployment's stored wake token
        plain_token = await sleep_token.decrypt(current.wake_token, project_data)
        secret_template = os.path.join(MANIFEST_TEMPLATES_DIR, "generic-secret.yaml.to-sops.jinja")
        await self._write_secret_file(
            SecretFileSpec(
                secret_name=sleep_manifests.waker_token_secret_name(unique_name),
                secret_pairs=WakeTokenSecret(token=plain_token).to_k8s_secret_data(),
                # Only the waker pod reads this, and it is pruned on wake. Counting it in
                # the config hash restarted the application again right after it came up.
                include_in_config_hash=False,
            ),
            deployment_name=deployment_name,
            namespace=namespace,
            output_dir=output_dir,
            template_path=secret_template,
            created_files=created_files,
            private_key=private_key,
        )
        logger.info(
            "sleep-mode: emitted waker for %s/%s (component %s)", project_name, deployment_name, component_reference
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
        """Write one deployment SOPS secret manifest (RC-5 Phase 6c shared writer).

        The single place that turns a service's ``SecretFileSpec`` into a file:
        optional secret registration, cross-component alias resolution,
        ``create_manifest_file`` and obsolete-prune bookkeeping. Services declare the
        spec (``Service.build_secret_files``); this stays service-agnostic.
        """
        if spec.register_secret is not None:
            if not spec.secret_type:
                msg = f"SecretFileSpec '{spec.secret_name}' registers a secret without a secret_type to file it under"
                raise ValueError(msg)
            self._add_secret_to_create(deployment_name, spec.secret_type, spec.register_secret)

        secret_data = dict(spec.secret_pairs)
        manifest_name = f"{spec.secret_name}-secret"
        if spec.keep_existing_values:
            # In een thread: dit start het sops-binary om de vorige ciphertext te lezen.
            existing = await asyncio.to_thread(
                _existing_secret_pairs, os.path.join(output_dir, sops_filenames(manifest_name).encrypted), private_key
            )
            secret_data.update({key: existing[key] for key in spec.secret_pairs if key in existing})
        if spec.resolve_aliases and spec.secret_type:
            aliases = self._deployment_aliases.get(deployment_name, {}).get("secret", {}).get(spec.secret_type, {})
            if aliases:
                logger.debug(f"Resolving {len(aliases)} {spec.secret_type} aliases for deployment {deployment_name}")
                resolved_aliases = resolve_aliases(aliases, secret_data)
                secret_data.update(resolved_aliases)
                logger.info(f"Added {len(resolved_aliases)} resolved {spec.secret_type} aliases to deployment secret")

        secret_path = self._manifest_generator.create_manifest_file(
            template_path=template_path,
            values={
                "name": spec.secret_name,
                "namespace": namespace,
                "secret_pairs": secret_data,
                # One place turns "this is not application config" into the label the
                # ArgoCD plugin filters on, so a service states what its secret IS and
                # never has to know a config-hash exists.
                "secret_labels": _secret_labels_for(spec),
            },
            output_dir=output_dir,
            output_filename=manifest_name,
            use_sops=True,
        )
        sops_filename = sops_filenames(manifest_name).plaintext
        created_files.append(sops_filename)
        logger.info(f"Secret '{spec.secret_name}' will be SOPS encrypted: {sops_filename}")
        logger.debug(f"Successfully created secret manifest: {secret_path}")

    async def get_argo_workspace(self) -> GitRepoWorkspace:
        """De werkmap op de argo-applications repo voor dit project, een per taak."""
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
        """De werkmap op de deployments-repo van deze config, een per remote binnen deze taak.

        Twee namen kunnen dezelfde remote zijn: "infrastructure" is dezelfde repo, branch
        en pad als de deployments-repo (``repositories[0]``). Dat is dan ook dezelfde scope
        en dezelfde tempmap, zodat de generatie van beide in een commit kan landen.
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
        """De ``{cluster}/``-boom van de deployments-repo in een wegwerpmap, alleen om te lezen.

        Voor de lezers over ALLE projecten (``rendered_kinds``). De map wordt met de rest
        van de werkmappen opgeruimd.
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
    ) -> None:
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
        await get_project_store().save(
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

    def _umbrella_verversbewaker(self, argo_connector: ArgoConnector) -> AbstractAsyncContextManager[None]:
        """Run the umbrella refresh watcher alongside a wait; see :class:`ArgoCDSync`."""
        return ArgoCDSync(self)._umbrella_verversbewaker(argo_connector)

    async def wait_for_project_level_application(self, app_name: str) -> None:
        """Wait until the project level of this project is really synchronised."""
        await ArgoCDSync(self).wait_for_project_level_application(app_name)

    async def _render_already_proven(
        self, argo_connector: ArgoConnector, app_name: str, reconciled_at: str | None
    ) -> bool:
        """Whether this application reported Synced after our refresh, so its render held."""
        return await ArgoCDSync(self)._render_already_proven(argo_connector, app_name, reconciled_at)

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
                Komt van een refresh; zie ``_clear_blocking_sync_operations``.
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
            rotate_credentials: Diensten waarvan het geheim deze ronde vervangen wordt in
                plaats van hergebruikt (zie ``opi.services.credential_rotation``)
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
            read_step = self._begin_step(steps.PROJECT_FILE_FETCH)
            try:
                git_connector_for_project_files = await self.get_git_connector_for_project_files()

                # Use the file handler to analyze changes
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
                # Security gate FIRST: validate against the project schema before
                # any side-effect (including the auto-migration save+commit+push
                # below). Without this ordering, a hostile project that happens to
                # migrate cleanly would be written to zad-projects with our ops-
                # manager identity *before* validation rejected it. Fails closed.
                validate_project_schema(current_yaml)

                # Een projectbestand dat hier voor het eerst opduikt is een create, ook
                # als het rechtstreeks naar zad-projects is gecommit.
                if analysis["previous_yaml"] is None and is_reserved_project_name(str(current_yaml.get("name", ""))):
                    raise ProjectIntegrityError(
                        RESERVED_PROJECT_NAME_MESSAGE.format(name=current_yaml.get("name", "(onbekend)"))
                    )
            except Exception as e:
                self._end_step(validate_step, str(e))
                raise
            self._end_step(validate_step)

            # read_project_file auto-migrates in memory; persist to disk if needed
            if self._project_file_handler.was_migrated:
                project_name = current_yaml.get("name", relative_project_file_path)
                logger.info(f"Schema migration applied for project '{project_name}', committing changes")
                # Only reported when it actually happens: on a normal run there is no
                # migration, and a step claiming one would be a lie.
                migrate_step = self._begin_step(steps.PROJECT_FILE_MIGRATE)
                # Central save: one locked write+commit. The previous write-then-commit
                # pair left the migrated file uncommitted in the shared warm copy in
                # between, where a concurrent reconcile could discard it -- after which
                # the commit found nothing to commit and still reported success.
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

                get_database_pool("main")  # guard: raises if the DB is unavailable -> immediate delete
                marked_service = MarkedForDeletionService()
            except KeyError, ValueError:
                logger.warning("Database pool not available - persistent resources will be deleted immediately")

            project_name = current_yaml.get("name", "unknown")

            # Step 1.6: Process deployment removals BEFORE creations
            if previous_yaml is not None and deployment_changes["deleted"]:
                logger.info("Processing %d deleted deployment(s)", len(deployment_changes["deleted"]))
                # Only when there is something to remove; nothing to do gets no step.
                delete_step = self._begin_step(steps.deleted_deployments_cleanup(len(deployment_changes["deleted"])))
                delete_errors_before = len(critical_failures)

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

                new_delete_errors = critical_failures[delete_errors_before:]
                self._end_step(delete_step, "; ".join(new_delete_errors) if new_delete_errors else None)

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

            logger.info(
                "Triggering ArgoCD sync for user-applications and project applications after project processing"
            )

            progress_manager = self.get_progress_manager()
            # The rollout phase is this step itself: its rows per deployment land under it.
            argo_task = self._begin_phase(steps.ROLLOUT)

            argo_connector = create_argo_connector()

            project_name = await self.get_name()
            deployments = await self.get_deployments(cluster_filter=True, deployment_names=targets)
            # process_project heeft deze deployments overgeslagen (RC-254), dus er is geen
            # Application om op te wachten; wachten zou aflopen op de aanmaaktimeout.
            being_deleted = await _deployments_being_deleted(project_name)
            if being_deleted:
                deployments = [dep for dep in deployments if dep.get("name") not in being_deleted]
            # The project as it stands, for asking the services what they know about a
            # deployment (RC-28). Same contents the deployments above came from.
            sync_project_data = await self.get_contents(record_base=False)
            sync_failures: list[str] = []
            # Runtime pod-health issues (e.g. CrashLoopBackOff) are the user app crashing,
            # not a deploy/sync failure: OPI synced the manifests fine. These are logged
            # as warnings and do NOT fail the task, so a persistently-crashing app doesn't
            # flood ERRORs (and pages) on every reprocess of the project.
            health_warnings: list[str] = []
            # Set when a warning came from the registry failing to serve an image. The
            # closing sentence of the user-facing notice blames the tenant's own app,
            # which is wrong for a platform-side registry outage.
            registry_outage_seen = False
            # health_warnings blijft de rauwe, volledige tekst dragen: die gaat naar de LOG,
            # en daar hoort alle detail te staan. Wat de GEBRUIKER krijgt wordt hieronder uit
            # deze twee opgebouwd, als telling. Het per-component-verhaal staat al in
            # component_failures, met een vertaalde titel en een suggestie.
            unhealthy_deployments: list[str] = []
            synced_deployment_count = 0

            if deployments and project_name:
                from opi.services.deployment_observation import run_after_sync_observation
                from opi.services.deployment_state import collect_deployment_state
                from opi.services.oom_watcher import (
                    MAIN_CONTAINER_NAME,
                    STALL_NOTICE_SECONDS,
                    DeploymentHealthError,
                    component_health_from_oom_failures,
                    create_health_check_callback,
                    describe_components_waiting,
                )
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

                # Ook de applicatie van het PROJECTNIVEAU: die staat niet in app_deployments.
                # `create_argocd_resources` heeft hem via `wait_for_project_level_application`
                # al aangemaakt en gesynchroniseerd, dus normaal voegt dit niets toe; het
                # blijft staan voor paden die hier zonder die grendel binnenkomen.
                project_app_name = generate_argocd_project_application_name(project_name)
                project_app_exists = await self._kubectl_connector.argocd_application_exists(
                    project_app_name, get_argo_namespace(settings.CLUSTER_MANAGER)
                )
                if project_app_exists is not True:
                    apps_to_create.append(project_app_name)

                # Wait for not-yet-present applications to be created (ArgoCD needs to
                # sync user-applications first). All waits run concurrently: read-only polls.
                if apps_to_create:
                    logger.info(
                        f"Refreshing user-applications: {len(apps_to_create)} application(s) not yet present "
                        f"({', '.join(apps_to_create)})"
                    )

                    async def _wait_created(app_name: str) -> str | None:
                        try:
                            await self._argo_manager.wait_for_application_created(
                                app_name=app_name, timeout=APPLICATIE_AANMAAK_TIMEOUT_SECONDEN, poll_interval=1
                            )
                            return None
                        except TimeoutError:
                            logger.error(f"Timed out waiting for ArgoCD application '{app_name}' to be created")
                            return f"{app_name}: timed out waiting for application to be created"

                    async with self._umbrella_verversbewaker(argo_connector):
                        created_results = await asyncio.gather(*(_wait_created(name) for name in apps_to_create))
                    sync_failures.extend(result for result in created_results if result)
                else:
                    logger.info(
                        f"All {len(app_names)} target application(s) already exist; skipping user-applications refresh"
                    )

                # Op dit punt heeft een wacht op het projectniveau geen zin: de
                # deployment-applicaties bestaan al en ArgoCD synchroniseert ze zelfstandig.
                # De ordening ligt in `create_argocd_resources`, voor het aanmaken ervan.

                # Refresh each application that was created, then wait for sync+healthy.
                # Refresh + wait run concurrently per application (read-only polls);
                # remediation below stays serial because it mutates the project file.
                async def _refresh_and_wait(app_name: str, deployment: dict) -> dict[str, Any]:
                    dep_name = deployment.get("name", "")
                    base_namespace = deployment.get("namespace", "")
                    cluster = deployment.get("cluster", "")
                    namespace = get_prefixed_namespace(cluster, base_namespace) if base_namespace and cluster else ""

                    # What the services report about this deployment (RC-28): sleep-mode
                    # says it scaled the application to zero, so "no pods" is the intended
                    # state and not something to report as a stalled rollout. Collected
                    # once here, from the project file, and handed to both readers below.
                    deployment_state = collect_deployment_state(sync_project_data, dep_name)

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
                                state=deployment_state,
                            )

                    # Live per-deployment progress subtask: surfaces "what are we
                    # waiting on" each poll (which component, why), so a stalled
                    # rollout is no longer silent. Falls back to None when there is
                    # no progress manager (e.g. non-task callers).
                    app_subtask = (
                        progress_manager.add_subtask(argo_task, dep_name, subject="uitrol voorbereiden…")
                        if progress_manager and argo_task
                        else None
                    )

                    # Track the last reported line per deployment to detect stalls.
                    stall: dict[str, Any] = {"line": None, "since": 0}
                    # De statuspoll draait elke 2s; de kubectl-inspectie hieronder houdt zijn
                    # eigen 5s-ritme zodat de snellere poll het cluster niet zwaarder belast.
                    # (De OOM-callback verderop throttlet zichzelf al, via last_check_at.)
                    describe_next: dict[str, int] = {"at": 0}

                    async def _on_poll(elapsed: int, status: dict[str, Any], fresh: bool) -> None:
                        if not (progress_manager and app_subtask):
                            return
                        # Wat ArgoCD zelf nog doet gaat voor de pods: zolang de sync niet rond
                        # is, zegt de stand van de pods niets over waar de uitrol op wacht.
                        argo_reason = describe_sync_waiting(status, fresh=fresh)
                        statuses: list[tuple[str, str]] = []
                        if argo_reason:
                            detail = argo_reason
                        else:
                            if elapsed < describe_next["at"]:
                                return
                            describe_next["at"] = elapsed + 5
                            if namespace and component_names:
                                try:
                                    statuses = await describe_components_waiting(
                                        namespace, component_names, component_refs, deployment_state
                                    )
                                except Exception:
                                    statuses = []
                            # Componenten met dezelfde reden op één regel. Twee componenten
                            # die op hetzelfde image wachten zijn één ding dat misgaat, geen
                            # twee, en zo is ook te zien welke reden bij welke componenten hoort.
                            by_reason: dict[str, list[str]] = {}
                            for ref, reason in statuses:
                                by_reason.setdefault(reason, []).append(ref)

                            if statuses:
                                detail = " · ".join(
                                    f"{', '.join(refs)}: {reason}" for reason, refs in by_reason.items()
                                )
                            else:
                                detail = "wachten tot componenten gereed zijn"

                        if detail != stall["line"]:
                            stall["line"] = detail
                            stall["since"] = elapsed
                        waited = elapsed - stall["since"]
                        if waited >= STALL_NOTICE_SECONDS and (argo_reason or statuses):
                            detail = f"{detail} (al {waited}s onveranderd, bekijk de logs)"
                        # De deploymentnaam is de titel van de regel, de reden staat
                        # eronder. Beide in de titel proppen gaf één onleesbare regel.
                        progress_manager.update_task(app_subtask, dep_name, subject=detail)

                    # Failure detection (OOM / ImagePull / CrashLoop) runs after on_poll,
                    # so the user still sees the live status before a DeploymentHealthError
                    # aborts the wait.
                    on_poll = _on_poll if app_subtask else None
                    on_progressing = oom_callback

                    try:
                        reconciled_at = await argo_connector.refresh_application(app_name)

                        # Actively fetch the render right after our push (and the refresh
                        # above, so the repo-server has re-checked the source). A broken
                        # kustomization returns the generation error here - including the
                        # plugin stderr - so the deploy fails fast with the real cause
                        # instead of a 300s time-out. This also covers helm/helmfile
                        # deployments, which only render inside the CMP. A non-render failure
                        # (network, 401) must not block the deploy: log and fall through to
                        # the wait loop.
                        # Een applicatie die NA onze refresh Synced meldt heeft zijn render
                        # al gedaan: ArgoCD kan niet vergelijken zonder manifests. Dan is het
                        # ophalen ervan (de duurste call van de lus: de hele render over de
                        # lijn) alleen nog werk. De status-GET die dat vaststelt doet de
                        # wachtlus hieronder toch al.
                        if await self._render_already_proven(argo_connector, app_name, reconciled_at):
                            logger.debug("Render van '%s' al bewezen door een verse Synced; niet ophalen", app_name)
                            render_ok, render_detail = True, ""
                        else:
                            render_ok, render_detail = await argo_connector.get_application_manifests(app_name)
                        if not render_ok and _looks_like_render_failure(render_detail):
                            from opi.services.event_interpreter import condense_render_error

                            logger.error("Manifest render failed for '%s': %s", app_name, render_detail)
                            raise RuntimeError(
                                f"Manifest render failed for '{app_name}': {condense_render_error(render_detail)}"
                            )
                        if not render_ok:
                            logger.warning(
                                "Could not verify render for '%s' (not a render error), proceeding to wait: %s",
                                app_name,
                                render_detail,
                            )

                        if not wait_for_healthy:
                            logger.info(
                                "Application '%s' refreshed and rendered; not waiting for Synced/Healthy", app_name
                            )
                            if app_subtask:
                                progress_manager.update_task(app_subtask, dep_name, subject="uitgerold")
                                progress_manager.complete_subtask(app_subtask)
                            return {"app_name": app_name, "dep_name": dep_name, "status": "ok"}

                        await self._argo_manager.wait_for_application_synced(
                            app_name=app_name,
                            timeout=300,
                            # 2s: de status-GET is een goedkope live read; het oude 5s-rooster
                            # betekende gemiddeld 2,5s extra wachten nadat de app al groen was.
                            # De zwaardere componenten-inspectie in on_poll zit op zijn
                            # eigen 5s-throttle en versnelt hierdoor niet mee.
                            poll_interval=2,
                            refreshed_after=reconciled_at,
                            on_progressing=on_progressing,
                            on_poll=on_poll,
                        )
                        logger.info(f"Application '{app_name}' is synced and healthy")
                        if app_subtask:
                            progress_manager.update_task(app_subtask, dep_name, subject="uitgerold en gezond")
                            progress_manager.complete_subtask(app_subtask)
                        return {"app_name": app_name, "dep_name": dep_name, "status": "ok"}
                    except DeploymentHealthError as e:
                        logger.warning("Pod health issues during sync of '%s': %s", app_name, e)
                        if app_subtask:
                            comps = ", ".join(
                                dict.fromkeys(f.component_reference or f.component_name for f in e.failures)
                            )
                            # Geen update_task: de naam IS de deploymentnaam en het onderwerp
                            # draagt de laatste waargenomen reden. fail_subtask zet alleen de
                            # uitkomst erbij; het volledige verhaal staat in component_failures.
                            logger.info("Deployment '%s' not healthy: %s", dep_name, comps)
                            progress_manager.fail_subtask(app_subtask, "niet gezond geworden")
                        return {"app_name": app_name, "dep_name": dep_name, "status": "health_error", "error": e}
                    except TimeoutError:
                        last_state = stall["line"] or "no progress reported"
                        logger.error(
                            f"Timed out after 300s waiting for '{app_name}' ({dep_name}) to sync; "
                            f"last observed state: {last_state}"
                        )
                        await self._terminate_operation_we_waited_on(app_name, cluster)
                        if app_subtask:
                            progress_manager.fail_subtask(app_subtask, "time-out na 300s")
                        return {"app_name": app_name, "dep_name": dep_name, "status": "timeout"}
                    except ApplicationGone as e:
                        # Geen synchronisatiefout: een andere taak (meestal een
                        # delete_deployment voor dezelfde deployment) heeft de Application
                        # verwijderd terwijl wij erop stonden te wachten. Er valt niets meer
                        # te melden over een deployment die er niet meer is.
                        logger.info("Application '%s' (%s) verdween tijdens de wacht: %s", app_name, dep_name, e)
                        if app_subtask:
                            progress_manager.update_task(app_subtask, dep_name, subject="verwijderd tijdens de uitrol")
                            progress_manager.complete_subtask(app_subtask)
                        return {"app_name": app_name, "dep_name": dep_name, "status": "removed"}
                    except RuntimeError as e:
                        logger.error(f"Application '{app_name}' failed to sync: {e}")
                        if app_subtask:
                            progress_manager.fail_subtask(app_subtask, str(e))
                        return {"app_name": app_name, "dep_name": dep_name, "status": "error", "error": e}

                pending_apps = [(a, d) for a, d in app_deployments if not any(a in f for f in sync_failures)]
                if progress_manager and argo_task and pending_apps:
                    progress_manager.update_task(
                        argo_task, steps.ROLLOUT, subject=steps.waiting_for_applications(len(pending_apps))
                    )
                outcomes = await asyncio.gather(*(_refresh_and_wait(a, d) for a, d in pending_apps))
                synced_deployment_count = len(outcomes)

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
                    # "removed" krijgt dezelfde behandeling als "ok": de deployment bestaat
                    # niet meer, dus geen sync_failures en geen health_warnings.
                    if outcome["status"] in ("ok", "removed"):
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
                        # De ernst kwam al uit de vertaler en werd hier weggegooid, waardoor
                        # een registry die even niet antwoordt net zo rood op het scherm kwam
                        # als een image dat echt niet bestaat. Dat is precies het verschil
                        # tussen "hier moet jij iets doen" en "hier hoef je niets te doen".
                        severity = translation[2].value if translation else "actionable"

                        self._component_failures.append(
                            {
                                "severity": severity,
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
                    crash_loop_failures = [f for f in e.failures if f.failure_type == "crash_loop"]

                    # One list, one destination: reporting (RC-243). There used to be a
                    # split here on what the registry said, and the "absent" half was
                    # scaled to 0 replicas. The wording below still distinguishes the two;
                    # nothing else does.
                    image_pull_failures = [f for f in e.failures if f.failure_type == "image_pull"]

                    task_service = (
                        progress_manager._task_service
                        if progress_manager and hasattr(progress_manager, "_task_service")
                        else None
                    )

                    # Handle OOM via the generic after-sync hook scan (no per-service
                    # branch here): build the observed health for the OOM'd components
                    # and let the registry decide. The resource-tuning system service
                    # tunes and asks for a refresh; the runner commits once.
                    if oom_failures:
                        oom_names = ", ".join(f.component_reference or f.component_name for f in oom_failures)
                        if progress_manager and argo_task:
                            progress_manager.update_task(
                                argo_task, steps.ROLLOUT, subject=steps.tuning_memory(oom_names, app_name)
                            )
                        component_health = component_health_from_oom_failures(oom_failures)
                        try:
                            with rollout_phase(PHASE_AFTER_SYNC):
                                observation = await run_after_sync_observation(project_name, dep_name, component_health)
                            if observation.requeue_refresh:
                                logger.info(
                                    "OOM auto-tune committed changes for %s/%s (components: %s), queuing refresh task",
                                    project_name,
                                    dep_name,
                                    oom_names,
                                )
                                await self._queue_refresh_task(task_service, project_name, dep_name)
                            sync_failures.extend(f"{app_name}: {msg}" for msg in observation.failures)
                        except Exception as tune_err:
                            logger.error(
                                "OOM after-sync observation failed for %s/%s (components: %s): %s",
                                project_name,
                                dep_name,
                                oom_names,
                                tune_err,
                            )
                            sync_failures.append(
                                f"{app_name}: OOM detected for {oom_names}, auto-tune failed: {tune_err}"
                            )

                    # A pod never becomes ready while any of its containers (main OR
                    # sidecar) is stuck pulling, so a sidecar failure is the component's
                    # problem too, and the message names the exact container and image: a
                    # bad platform sidecar once sent us down the wrong path for hours
                    # because it did not.
                    #
                    # Which sentence the reader gets: an answer about this image is the
                    # tenant's to act on, anything else is the platform's and must not
                    # send them looking for an image that is fine.
                    for f in image_pull_failures:
                        image = f" [image {f.image}]" if f.image else ""
                        reason = " ".join(f.message.split()) if f.message else ""
                        if not image_is_confirmed_absent(f.message):
                            registry_outage_seen = True
                            health_warnings.append(
                                f"{app_name}: de registry kon het image voor component "
                                f"'{f.component_name}'{image} niet leveren: {reason}. "
                                "Het component blijft aan staan en probeert het opnieuw."
                            )
                            if dep_name not in unhealthy_deployments:
                                unhealthy_deployments.append(dep_name)
                            continue
                        if f.container_name and f.container_name != MAIN_CONTAINER_NAME:
                            target = f"sidecar '{f.container_name}' in component '{f.component_name}'"
                        else:
                            target = f"component '{f.component_name}'"
                        detail = f"{target}{image}"
                        if reason:
                            detail = f"{detail}: {reason}"
                        msg = f"{app_name}: ImagePullBackOff for {detail}"
                        # Only a registry auth/permission failure is a real error worth
                        # failing the task + alerting. A not-found / not-yet-built image is
                        # expected churn (CI/CD, PR builds) -> warn. Deliberately asked
                        # only of a message the registry answered: the auth markers include
                        # bare "401"/"403", which an image tag can carry, and a transport
                        # outage must never fail a deploy on the spelling of a tag.
                        if _is_image_pull_auth_error(f.message):
                            sync_failures.append(msg)
                        else:
                            health_warnings.append(msg)
                            if dep_name not in unhealthy_deployments:
                                unhealthy_deployments.append(dep_name)

                    # CrashLoopBackOff is the user's app crashing at runtime, not a
                    # deploy/sync failure - report it as a warning, don't fail the task.
                    if crash_loop_failures:
                        names = ", ".join(f.component_name for f in crash_loop_failures)
                        health_warnings.append(f"{app_name}: CrashLoopBackOff for {names}")
                        if dep_name not in unhealthy_deployments:
                            unhealthy_deployments.append(dep_name)

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
                # The deploy itself succeeded: manifests generated, pushed, and synced.
                # The app's own pods are crashing (CrashLoopBackOff, a not-yet-built PR
                # image), which is the tenant's problem, not ours. This branch used to
                # fall through WITHOUT returning, so the function returned None, the
                # caller read that as failure, and a crash-looping PR environment
                # produced a failed task and a useless "Deployment processing failed"
                # alert on every push. Complete the task and report the reason instead.
                logger.info("ArgoCD applications synced (%d runtime health warning(s) above)", len(health_warnings))
                if progress_manager and argo_task:
                    # The wait is over: its subject ("wacht op ...") would otherwise stay.
                    progress_manager.update_task(argo_task, steps.ROLLOUT, subject="")
                    progress_manager.complete_task(argo_task)
                    # Een TELLING, geen aaneenschakeling. Hier stond "; ".join(health_warnings),
                    # en die waarschuwingen dragen elk de volledige kubelet-tekst: voor een
                    # project met vijftien deployments werd dat één melding van dertienduizend
                    # tekens in wat de titel van een lijstregel is. Wat er per component aan de
                    # hand is staat in component_failures, met een vertaalde titel en een
                    # suggestie, en dat paneel staat onder deze lijst.
                    if len(unhealthy_deployments) <= 5:
                        which = ", ".join(unhealthy_deployments)
                    else:
                        rest = len(unhealthy_deployments) - 5
                        which = f"{', '.join(unhealthy_deployments[:5])} en {rest} andere"
                    aantal = len(unhealthy_deployments)
                    hoeveel = (
                        f"{aantal} van de {synced_deployment_count} deployments"
                        if synced_deployment_count > aantal
                        else f"{aantal} deployment" + ("" if aantal == 1 else "s")
                    )
                    draait = "draait" if aantal == 1 else "draaien"
                    cause = (
                        "Een deel ligt aan het platform: de registry kon een image niet leveren. "
                        "Daar hoef je zelf niets voor te doen, het ophalen wordt vanzelf opnieuw geprobeerd."
                        if registry_outage_seen
                        else "Dat ligt aan de applicatie zelf (bijvoorbeeld een crashende pod of een image dat "
                        "nog niet gebouwd is), niet aan het uitrollen."
                    )
                    notice = progress_manager.add_subtask(
                        argo_task,
                        steps.deployed_but_unhealthy(hoeveel, draait),
                        subject=f"{which}. {cause}",
                    )
                    progress_manager.complete_task(notice)
                return True
            else:
                if wait_for_healthy:
                    logger.info("All ArgoCD applications are synced and healthy")
                else:
                    logger.info("All ArgoCD applications are refreshed and rendered; health was not awaited")
                if progress_manager and argo_task:
                    progress_manager.update_task(argo_task, steps.ROLLOUT, subject="")
                    progress_manager.complete_task(argo_task)
                return True
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
            reset_running_sync: Break off a sync that is still running, however
                young. Komt van een refresh; zie ``_clear_blocking_sync_operations``.

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

            repo_info = Project.locate(repositories, name=repo_name)
            if not repo_info:
                raise Exception(f"Repository configuration not found: {repo_name}")

            await self._process_repository_manifests(repo_info, repo_deployments, reset_running_sync=reset_running_sync)

        logger.info(f"Successfully processed all application manifests for {project_name}")

    async def _process_repository_manifests(
        self,
        repo_config: dict[str, Any],
        deployments: list[dict[str, Any]],
        reset_running_sync: bool = False,
    ) -> None:
        """
        Process manifests for a specific repository.

        Args:
            repo_config: Repository configuration
            deployments: List of deployments for this repository
            reset_running_sync: Break off a sync that is still running, however young.
                Komt van een refresh; zie ``_clear_blocking_sync_operations``.

        Returns:
            None
        """
        project_data = await self.get_contents()
        project_name = project_data.get("name")

        workspace = await self.get_deployment_workspace(repo_config["name"], repo_config)
        owner = project_level_deployment(await self.get_deployments(cluster_filter=True))

        async def _generate() -> None:
            """Zet de manifests van deze repo neer in de werkmap van deze ronde."""
            # Deployments are already filtered for current cluster by caller
            for deployment in deployments:
                with self._step(steps.DEPLOYMENT_CONFIG_BUILD, subject=str(deployment["name"])):
                    await self._process_deployment_manifests(deployment, workspace)

            # Het PROJECTniveau: namespace-breed, dus in precies EEN repository, ook als het
            # project deployments over twee repo's heeft. Welke, beslist
            # project_level_deployment, dezelfde functie die de applicatie gebruikt.
            if owner is not None and owner.get("repository") == repo_config.get("name"):
                with self._step(steps.PROJECT_CONFIG_BUILD):
                    await self._process_project_manifests(
                        repo_config,
                        str(owner["cluster"]),
                        get_prefixed_namespace(str(owner["cluster"]), str(owner["namespace"])),
                        workspace,
                    )
                self._project_level_repo = repo_config.get("name")

            # Vlak VOOR de write, en niet erna: ArgoCD probeert een revisie die al eens faalde
            # niet opnieuw, dus beeindigen na de push laat de blokkade staan voor precies de
            # commit die hem moet oplossen. Een storing hierin houdt de write niet tegen
            # (``terminate_stuck_operation`` vangt een omgevallen kubectl zelf af).
            await self._clear_blocking_sync_operations(deployments, reset_running_sync=reset_running_sync)

        names = ", ".join(str(deployment["name"]) for deployment in deployments)
        # Fetching and pushing each get their own step, so a hanging or failing remote shows
        # up on the progress page at that phase, not at the last service or manifest. The
        # fetch lives inside mutate_and_push because it belongs under the lock. A conflict
        # is retried there, so it does not fail the push step.
        await workspace.mutate_and_push(
            f"Update manifests for {project_name} ({names})",
            _generate,
            on_prepare=lambda: self._step(steps.CONFIG_FETCH, subject=str(repo_config["name"])),
            on_push=lambda: self._step(
                steps.CONFIG_PUBLISH, subject=str(repo_config["name"]), retried_on=GitRepoConflictError
            ),
        )

        # Of het PROJECTNIVEAU deze ronde echt veranderde. Zo niet, dan hoeft de grendel in
        # wait_for_project_level_application niet op een nieuwe sync te wachten. Alleen de
        # repo die het projectniveau bezit kan dat meten; voor een andere blijft het True,
        # want die weet er niets van. De paden van de write zijn repo-relatief en
        # genormaliseerd, en ``_project_level_path`` is dat niet (``./cluster/...`` bij een
        # repositories[].path van '.'), dus beide kanten gaan door normpath.
        if owner is not None and owner.get("repository") == repo_config.get("name") and self._project_level_path:
            prefix = f"{posixpath.normpath(self._project_level_path)}/"
            self._project_level_manifests_changed = bool(
                workspace.last_write
                and any(posixpath.normpath(path).startswith(prefix) for path in workspace.last_write.paths)
            )

        logger.info(f"Successfully processed repository: {repo_config['name']}")

    async def _terminate_operation_we_waited_on(self, app_name: str, cluster: str | None) -> None:
        """Terminate the operation that OPI's own wait has just run out on."""
        await ArgoCDSync(self)._terminate_operation_we_waited_on(app_name, cluster)

    async def _clear_blocking_sync_operations(
        self, deployments: list[dict[str, Any]], reset_running_sync: bool = False
    ) -> None:
        """Terminate the sync operations blocking a next sync on these deployments."""
        await ArgoCDSync(self)._clear_blocking_sync_operations(deployments, reset_running_sync=reset_running_sync)

    async def get_repository_path(self, repository_name: str) -> str | None:
        project_data = await self.get_contents()
        repositories = project_data.get("repositories") or []
        repositories: list[dict[str, str]] = repositories
        repo = Project.locate(repositories, name=repository_name)
        return repo.get("path", "") if repo else ""

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

    def _prune_obsolete_service_manifests(
        self, deployment: dict[str, Any], target_path: str, generated_files: list[str]
    ) -> None:
        """Delete deployment-wide, service-owned manifest files this run did not regenerate.

        The counterpart of ``_prune_obsolete_component_manifests`` for the RC-15
        deployment-manifest hook: a service (cross-domain-access today) writes files named
        ``<deployment>-<service>-...``. When the service is switched off, its last rule is
        removed, a component scope disappears, or a target project vanishes, the run no
        longer generates that file, and this removes it (ArgoCD then prunes the resource).
        The component prune leaves these alone because they carry no component prefix.
        """
        deployment_name = deployment["name"]
        service_prefixes = {f"{deployment_name}-{service.value}-" for service in ServiceType}
        # The attachment Secrets predate the RC-15 naming; renaming them would recreate every
        # mounted Secret, so their own prefix joins the set instead.
        service_prefixes.add(_attachment_secret_prefix(deployment_name))
        obsolete = _select_obsolete_service_manifests(target_path, service_prefixes, set(generated_files))
        if not obsolete:
            return
        logger.info(
            f"Pruning {len(obsolete)} obsolete service manifest(s) from {deployment_name}: {', '.join(obsolete)}"
        )
        for basename in obsolete:
            os.remove(os.path.join(target_path, basename))

    def _prune_obsolete_project_manifests(self, target_path: str, generated_files: list[str]) -> None:
        """Verwijder projectbrede dienstbestanden die deze run niet opnieuw heeft gemaakt.

        Dezelfde vorm als de prune op deploymentniveau, een laag hoger.
        """
        service_prefixes = {f"{service.value}-" for service in ServiceType}
        obsolete = _select_obsolete_service_manifests(target_path, service_prefixes, set(generated_files))
        if not obsolete:
            return
        logger.info(f"Pruning {len(obsolete)} obsolete project manifest(s): {', '.join(obsolete)}")
        for basename in obsolete:
            os.remove(os.path.join(target_path, basename))

    async def _process_project_manifests(
        self,
        repo_config: dict[str, Any],
        cluster_name: str,
        namespace: str,
        git_connector: GitRepoWorkspace,
    ) -> None:
        """Schrijf het PROJECTniveau van de deployments-repo: ``<cluster>/<project>/_project/``.

        De generieke emitter: wat er komt te staan bepalen de diensten met
        ``contribute_project_manifests``.
        """
        project_data = await self.get_contents()
        project_name = await self.get_name()

        # Dezelfde functie als de ArgoCD-applicatie die naar deze map wijst, zodat de map
        # en de verwijzing ernaar niet uit elkaar kunnen lopen.
        project_dir = generate_project_level_manifest_path(cluster_name, project_name, repo_config.get("path", ""))
        # Onthouden voor _project_level_already_applied: dat is de map waarvan bewezen moet
        # worden dat ArgoCD hem al heeft toegepast.
        self._project_level_path = project_dir
        target_path = os.path.join(await git_connector.get_working_dir(), project_dir)
        os.makedirs(target_path, exist_ok=True)

        ctx = ProjectManifestContext(
            project_name=project_name,
            project_data=project_data,
            cluster=cluster_name,
            namespace=namespace,
        )

        created_files: list[str] = []
        for service in project_manifest_services():
            for spec in service.contribute_project_manifests(ctx):
                self._manifest_generator.create_manifest_file(
                    template_path=os.path.join(MANIFEST_TEMPLATES_DIR, spec.template_path),
                    values=spec.values,
                    output_dir=target_path,
                    output_filename=spec.filename,
                    use_sops=spec.encrypt,
                )
                if spec.encrypt:
                    created_files.append(sops_filenames(spec.filename).plaintext)
                else:
                    created_files.append(f"{spec.filename}.yaml")
                logger.info(f"Created project manifest '{spec.filename}' for project '{project_name}'")

        self._prune_obsolete_project_manifests(target_path, created_files)

        sops_files, regular_files = self._manifest_generator.collect_manifest_files(
            target_path, include_subfolders=False
        )
        await self.create_kustomization_file(git_connector, namespace, sops_files, regular_files, project_dir)

        public_key = get_project_public_key(project_data)
        if not public_key:
            raise RuntimeError(f"Geen SOPS public key voor het projectniveau van '{project_name}'")
        await asyncio.to_thread(
            encrypt_to_sops_files_or_fail,
            target_path,
            public_key,
            f"projectbrede secrets van '{project_name}' (namespace '{namespace}')",
            private_key=project_private_key_or_none(project_data),
        )

    async def _process_deployment_manifests(
        self,
        deployment: dict[str, Any],
        git_connector: GitRepoWorkspace,
    ) -> None:
        """
        Process manifests for a specific deployment.

        Supports two deployment types:
        1. Component-based deployments: Traditional OPI components with deployment/service/ingress manifests
        2. Helm chart deployments: External Helm charts rendered via Kustomize helmCharts

        Args:
            deployment: Deployment configuration
            git_connector: de werkmap van de repository

        Returns:
            None
        """
        project_data = await self.get_contents()
        project_name = await self.get_name()
        deployment_name = deployment["name"]
        cluster_name = deployment["cluster"]

        repo_path = await self.get_repository_path(deployment["repository"]) or ""
        deployment_path = generate_deployment_manifest_path(cluster_name, project_name, deployment_name, repo_path)

        prefixed_namespace = get_prefixed_namespace(cluster_name, deployment["namespace"])
        target_path = os.path.join(await git_connector.get_working_dir(), deployment_path)

        logger.info(f"Processing deployment: {deployment_name} at path: {deployment_path}")

        # Voor alle drie de soorten deployments, want juist een helm- of helmfile-pod
        # draagt geen deployment-label en viel tot nu toe buiten elk beleid.
        namespace_policy_files = emit_namespace_network_policies(
            self._manifest_generator,
            project_data=project_data,
            project_name=project_name,
            deployment=deployment,
            cluster_name=cluster_name,
            namespace=prefixed_namespace,
            output_dir=target_path,
        )

        # Check if this is a helmfile deployment, helm chart deployment, or component deployment
        if deployment_uses_helmfile(deployment):
            # Process helmfile deployment (ArgoCD CMP runs helmfile template)
            logger.info(f"Deployment '{deployment_name}' uses helmfile - processing as helmfile deployment")
            await self._process_helmfile_deployment(deployment, git_connector, target_path, namespace_policy_files)
            # Note: SOPS encryption is handled inside _process_helmfile_deployment
            return

        if deployment_uses_helm_charts(deployment):
            # Process helm chart deployment (no component manifests, uses Kustomize helmCharts)
            logger.info(f"Deployment '{deployment_name}' uses helm-charts - processing as helm deployment")
            await self._process_helm_chart_deployment(deployment, git_connector, target_path, namespace_policy_files)
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

        # Hoort bij de gewenste staat van deze map, dus de prunes hieronder laten ze staan.
        created_files.extend(namespace_policy_files)

        # Note: SSO and user secrets are already created in create_application_manifests above

        # Prune obsolete per-component manifests: any file this run did not regenerate
        # (removed component, or a renamed/dropped file of a surviving component such as
        # an ingress whose path changed). The kustomization is rebuilt from the on-disk
        # glob, so leftovers would otherwise be re-adopted and keep dead workloads in
        # ArgoCD. Symmetric with the removed-service cleanup in DeleteProjectManager.
        self._prune_obsolete_component_manifests(deployment, target_path, created_files)

        # Prune obsolete deployment-wide, service-owned manifests (RC-15). A service (e.g.
        # cross-domain-access) writes <deployment>-<service>-* files; when the service is
        # switched off or stops contributing, this removes the leftover so ArgoCD prunes
        # the resource. Symmetric with the component prune above and non-overlapping (those
        # files carry no component prefix).
        self._prune_obsolete_service_manifests(deployment, target_path, created_files)

        # De registrypas voor de images die niet door de componentlus lopen, zoals een
        # sidecar met een vaste waarde in zijn sjabloon. Idempotent op de rest.
        apply_rules_to_directory(target_path, build_rules(project_data, cluster_name))

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

        to_sops_pattern = os.path.join(target_path, f"*{TO_SOPS_SUFFIX}")
        to_sops_files = glob.glob(to_sops_pattern)
        logger.info(f"Found {len(to_sops_files)} .to-sops.yaml files for final encryption:")
        for file_path in to_sops_files:
            logger.info(f"  - {os.path.basename(file_path)}")

        if not public_key:
            raise RuntimeError(
                f"Geen SOPS public key voor deployment '{deployment_name}'; "
                "dit zou secrets in platte tekst naar git committen."
            )

        await asyncio.to_thread(
            encrypt_to_sops_files_or_fail,
            target_path,
            public_key,
            f"secrets voor deployment '{deployment_name}' (namespace '{prefixed_namespace}')",
            private_key=project_private_key_or_none(project_data),
        )
        logger.info("All .to-sops.yaml files successfully encrypted")

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
                Komt van een refresh; zie ``_clear_blocking_sync_operations``.
            rotate_credentials: Diensten waarvan het geheim deze ronde vervangen wordt in
                plaats van hergebruikt (zie ``opi.services.credential_rotation``)

        Returns:
            True if all operations succeeded, False if any operation failed
        """
        logger.info(f"Processing project file: {self._project_file_relative_path}")

        # Store force_clone override for nested calls (e.g., PVC manager)
        self._force_clone_override = force_clone
        self._terminated_sync_operations = []

        targets = resolve_deployment_filter(deployment_name, deployment_names)
        # De scope zoals de AANROEPER hem gaf. De grendel hieronder mag een projectbrede
        # ronde versmallen, maar dat maakt hem niet tot een gerichte ronde.
        project_wide = targets is None

        try:
            # The snapshot this run works from; its moment is what the reconciliation rows store.
            read_started = time.monotonic()
            project_data = await self.get_contents()
            project_name = await self.get_name()

            # Slaa deployments met een open delete-taak over (RC-254).
            # Hun Application is minuten eerder uit git dan de deployment zelf.
            being_deleted = await _deployments_being_deleted(project_name)
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
                # Zonder rij blijft een uitgestelde wijziging aan zo'n project eeuwig wachten.
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
        private_key = project_private_key_or_none(project_data)

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

        self._track_public_urls(project_data, deployment, project_name)

        logger.info(f"Processing deployment: {deployment_name} in prefixed namespace: {namespace}")

        # Check if deployment has components
        components = deployment.get("components", [])
        if not components:
            logger.warning(f"No components found in deployment {deployment_name}, skipping")
            return []

        # Register subdomain for the dotted component-per-subdomain layouts (with rollback
        # on failure). Keyed on domain-format: the legacy ``domain-mode: nice-url`` files
        # were migrated to ``component.subdomain`` (schema v2.8).
        subdomain = get_domain_setting(deployment, DomainSetting.SUBDOMAIN)
        base_domain = get_domain_setting(deployment, DomainSetting.BASE_DOMAIN)
        domain_format = get_domain_setting(deployment, DomainSetting.DOMAIN_FORMAT)
        uses_dotted_subdomain_layout = domain_format in ROOT_COMPONENT_FORMAT_IDS
        subdomain_registered = False  # Track if we registered a new subdomain for rollback
        subdomain_connector = None  # Initialize for use in rollback

        # Validate the layout's requirements BEFORE subdomain registration
        # This prevents orphaned subdomain entries when validation fails
        root_component_name = get_domain_setting(deployment, DomainSetting.ROOT_COMPONENT)
        # Every component with publish-on-web gets an ingress, in every layout, and an ingress
        # points at the component's Service on its first inbound port. A component without
        # one gets no Service, so its ingress would carry `port.number: None` and ArgoCD
        # refuses the whole Application. This check used to run for the dotted layouts only.
        components_missing_ports = []
        for component in components:
            component_name = component.get("reference") or component.get("name")
            if not component_name:
                continue
            has_publish_on_web = self._project_file_handler.extract_component_publish_on_web(
                project_data, component_name
            )
            if has_publish_on_web and not self._project_file_handler.component_has_ports(project_data, component_name):
                components_missing_ports.append(component_name)

        if components_missing_ports:
            raise ValueError(
                f"Components with 'publish-on-web' must have at least one port configured. "
                f"Missing ports for: {', '.join(components_missing_ports)}. "
                f"Add 'ports.inbound' to the component definition."
            )

        # Validate that root component has publish-on-web (required for root ingress creation)
        if uses_dotted_subdomain_layout and subdomain and base_domain and root_component_name:
            root_has_publish_on_web = self._project_file_handler.extract_component_publish_on_web(
                project_data, root_component_name
            )
            if not root_has_publish_on_web:
                raise ValueError(
                    f"Component '{root_component_name}' is marked as root but does not have 'publish-on-web' enabled. "
                    f"Root components must have a service exposed for the root URL to work."
                )

        if uses_dotted_subdomain_layout and subdomain and base_domain:
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
        expose_on_bare_domain = get_domain_setting(deployment, DomainSetting.BARE_DOMAIN_COMPONENT)
        bare_domain_registered = False
        # Opslaan mag, toepassen niet: het formulier laat een kaal domein op een nog niet
        # goedgekeurd eigen domein door (RC-216), dus de goedkeuring hangt hier, aan
        # dezelfde voorwaarde als het rootadres.
        if (
            expose_on_bare_domain
            and base_domain
            and is_deployment_domain_approved(project_data, base_domain, subdomain, cluster)
        ):
            # The publication path enforces the bare-domain rule itself, not just the form
            # layer: the setting is also writable through the service config API, which the
            # form enforcer never sees. This is the point of no return -- past it a
            # registration and an apex certificate exist, on a domain that has to be this
            # project's own: the rule covers both "not a platform domain" and "approved
            # for this project".
            validate_bare_domain_allowed(base_domain, get_supported_base_domains(cluster), project_data)
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
        elif base_domain:
            # Bare domain deselected, or not (yet) approved: clean up any existing registration
            if expose_on_bare_domain:
                logger.warning(
                    "Bare domain '%s' not applied for deployment '%s': the domain is not approved for project '%s'",
                    base_domain,
                    deployment_name,
                    project_name,
                )
            if subdomain_connector is None:
                subdomain_connector = SubdomainConnector()
            # Scoped to this project: the base-domain is just a string in a project file,
            # so an unscoped delete lets one project remove another tenant's apex
            # registration by naming their domain.
            deleted = await subdomain_connector.delete_bare_domain(base_domain, project_name=project_name)
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

        # Waar komt de image van elk component vandaan, en welk pull-secret hoort erbij.
        # Het secret zelf is namespace-scoped en staat op het projectniveau.
        component_definitions = {
            c.get("name"): c for c in project_data.get("components", []) or [] if isinstance(c, dict)
        }
        resolved_images: dict[str, ResolvedImage] = {}
        image_pull_secrets_map: dict[str, str] = {}  # image_url (opgelost) -> secret_name

        for component in components:
            component_reference = component.get("reference")
            if not component_reference or not component.get("image"):
                continue
            resolved = resolve_deployment_component_image(
                project_data, component, component_definitions.get(component_reference), cluster
            )
            resolved_images[component_reference] = resolved
            if resolved.secret:
                image_pull_secrets_map[resolved.image] = resolved.secret
            if resolved.image != component.get("image"):
                logger.info(
                    f"Component '{component_reference}' image '{component.get('image')}' opgelost naar "
                    f"'{resolved.image}' met pull-secret '{resolved.secret}'"
                )

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

            # Vanaf hier de opgeloste verwijzing: die komt in het manifest en is de sleutel
            # van imagePullSecretsMap.
            image_url = resolved_images[component_reference].image

            component_name = component_reference

            # Update component deployment name if progress manager is available
            progress_manager = self.get_progress_manager()
            if progress_manager:
                deployment_resource_name = f"{project_name}-{component_name}"
                progress_manager.update_component_deployment(component_name, deployment_resource_name)
                logger.debug(f"Updated component {component_name} deployment name to {deployment_resource_name}")

            # Extract the application port from the component definition using the file handler
            # A component may declare no inbound port (background worker). Read the real
            # list with no default fallback: no ports -> no Service, no probe, no
            # containerPort. application_port is None in that case.
            inbound_ports = self._project_file_handler.extract_component_inbound_ports(
                project_data, component_reference
            )
            has_inbound_port = bool(inbound_ports)
            application_port = inbound_ports[0] if has_inbound_port else None

            # Base health-probe scheme: a plain TCP probe on the first inbound port.
            # A component without an inbound port cannot be probed by the kubelet, so
            # scheme=none disables all probes. The optional health-check service
            # overrides scheme/port/paths from here via its manifest contribution.
            probe_scheme = "tcp" if has_inbound_port else "none"

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
            # Sleep-mode: a sleeping deployment scales every component to zero (a broken
            # image `disabled` always wins). `waking` keeps the app at 1 so it cold-starts
            # while the waker still serves the loading page. Applies to every component,
            # endpoint or not.
            from opi.services.catalog.sleep_mode import state as sleep_mode_state

            sleep_state = sleep_mode_state.read(project_data, deployment_name).state
            is_sleeping = sleep_state == sleep_mode_state.STATE_SLEEPING
            replicas = 0 if (is_disabled or is_sleeping) else 1
            if is_disabled:
                logger.info(f"Component '{component_name}' is disabled (reason: {disabled_reason}), setting replicas=0")
            elif is_sleeping:
                logger.info(
                    f"Deployment '{deployment_name}' is sleeping, setting component '{component_name}' replicas=0"
                )

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
                    secret_name = f"{_attachment_secret_prefix(deployment_name)}{reference}"
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
            subdomain = get_domain_setting(deployment, DomainSetting.SUBDOMAIN)
            base_domain = get_domain_setting(deployment, DomainSetting.BASE_DOMAIN)
            issuer_config = effective_issuer(project_data, deployment, cluster)
            domain_format = get_domain_setting(deployment, DomainSetting.DOMAIN_FORMAT)
            expose_on_bare_domain = get_domain_setting(deployment, DomainSetting.BARE_DOMAIN_COMPONENT, False)
            logger.info(
                f"Extracted subdomain for {component_name}: {subdomain}, base-domain: {base_domain}, "
                f"issuer: {issuer_config}, domain-format: {domain_format}, "
                f"expose-component-on-bare-domain: {expose_on_bare_domain}"
            )

            # Get ingress map using centralized function
            ingress_map = get_component_ingress_map(
                component_name=component_name,
                deployment_name=deployment_name,
                project_name=project_name,
                ingress_postfix=ingress_postfix,
                subdomain=subdomain,
                base_domain=base_domain,
                domain_format=domain_format,
                project_data=project_data,
                cluster=settings.CLUSTER_MANAGER,
            )
            hostname = next(iter(ingress_map.values()))

            logger.info(f"Generated ingress_map for {component_name}: {ingress_map}")
            logger.info(f"Primary hostname for {component_name}: {hostname}")

            # Show the component's public address in the background-task UI, but only the one
            # that publish-on-web recorded for this deployment: a component without an ingress
            # has no address to show, and the hostname above is only what it would be called.
            web_address = self._deployment_results[deployment_name].urls.get(component_name)
            if web_address and progress_manager:
                progress_manager.update_component_web_address(component_name, web_address)

            # Generate environment variables using service-based registration pattern
            env_vars = {}

            # Register storage environment variables using service definitions
            if storage_configs:
                storage_env_vars = generate_storage_env_vars_from_services(processed_storage_configs)
                if storage_env_vars:
                    env_vars.update(storage_env_vars)

            # Register publish-on-web environment variables using service definitions
            if publish_on_web and hostname:
                web_env_vars = generate_web_env_vars_from_services(hostname, use_https)
                if web_env_vars:
                    env_vars.update(web_env_vars)

            # Resolve and add direct aliases (aliases that reference direct env vars).
            # Scoped to the declaring component: the context below holds only THIS
            # component's storage and publish-on-web variables, so a sibling without
            # those services would resolve another component's aliases against an
            # empty context and fail.
            direct_aliases = scope_direct_aliases_to_component(
                self._deployment_aliases.get(deployment_name, {}).get("direct", {}),
                project_data,
                component_name,
            )
            for service_category, service_aliases in direct_aliases.items():
                if service_aliases:
                    logger.debug(
                        f"Resolving {len(service_aliases)} direct {service_category} aliases for component {component_name}"
                    )
                    # Resolve aliases using current env_vars as context
                    resolved_direct_aliases = resolve_aliases(service_aliases, env_vars)
                    # Add resolved aliases to env_vars
                    env_vars.update(resolved_direct_aliases)
                    logger.info(
                        f"Added {len(resolved_direct_aliases)} resolved direct {service_category} aliases to component {component_name}"
                    )

            # Register user environment variables
            # NOTE: User env vars go into a secret and are referenced via envFrom, not as direct env vars
            if user_env_vars:
                # Resolve $VAR / ${VAR} in user-env-vars against everything this component
                # can see: the storage, publish-on-web and resolved-alias variables built
                # above. This used to special-case PUBLIC_HOST and PUBLIC_HOSTNAME by hand,
                # with a note to extend the alias system if more were ever needed -- so
                # this is that extension, using the same engine aliases already use.
                #
                # Deliberately the LENIENT substitution: an unknown $NAME is left as-is.
                # In an alias a missing reference is a typo and must fail, but a user-env-var
                # value is often just a password that happens to contain a dollar. Measured
                # on production: regel-k4c's ADMIN_PASSWORD and rijks-595's DJANGO_SECRET_KEY
                # both carry one, and strict substitution would fail their deploy outright.
                substituted_user_env_vars: dict[str, Any] = {}
                for key, value in user_env_vars.items():
                    if not isinstance(value, str) or "$" not in value:
                        substituted_user_env_vars[key] = value
                        continue
                    substituted_value = substitute_known_variables(
                        # No key name: a user-env-var's NAME is the user's business too, and
                        # the component is enough to locate the warning.
                        value,
                        env_vars,
                        where=f"a user-env-var of component {component_name}",
                    )
                    substituted_user_env_vars[key] = substituted_value
                    if substituted_value != value:
                        # Neither the value nor the name: user-env-vars are the user's.
                        logger.debug(f"Substituted variable references in a user-env-var of {component_name}")
                user_env_vars = substituted_user_env_vars

            # Service contributions (envFrom, sidecars, template vars, secret files) are
            # driven generically off the provider registry (RC-5 Phase 6). Only the
            # auth-wall observability log below still needs an explicit flag.
            component_uses_authorization_wall = False
            component_def = None
            all_services: list[str] = []

            if component_reference:
                component_def = self._project_file_handler._find_component(project_data, component_reference)
                all_services = extract_service_names_from_component(component_def) if component_def else []

                component_uses_authorization_wall = ServiceType.AUTHORIZATION_WALL.value in all_services

            # Collect each used service's manifest contribution once (RC-5 Phase 6).
            # A provider may add envFrom secrets + sidecars (additive) and override
            # template vars (e.g. auth-wall service_port 8080 -> 4180). envFrom is
            # applied here (it is a template var itself); template_vars/sidecars are
            # merged after the template context dict is built, below.
            manifest_ctx = ManifestContext(
                deployment_name=deployment_name,
                project_data=project_data,
                unique_name=unique_name,
                cluster=cluster,
                get_secret=self._get_secret_from_map,
                component_def=component_def,
            )
            manifest_contributions = collect_manifest_contributions(
                manifest_ctx,
                component_services=all_services,
                project_services=ServiceAdapter.extract_service_names_from_project_services(
                    project_data.get("services", []) or []
                ),
            )

            # Build envFrom secrets list based on services used and user env vars
            # This list determines which secrets are referenced in the deployment manifest
            # Note: Secret FILES are only generated if the secret is in _secrets_to_create map
            env_from_secrets = []

            # Add deployment-level secrets in manifest_order -- byte-identical to the old
            # fixed db -> minio -> keycloak -> redis -> metrics append sequence.
            for contribution in manifest_contributions:
                env_from_secrets.extend(contribution.env_from_secrets)

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
                "service_port": application_port,  # Service's primary port; overridden to 4180 when an auth wall fronts it
                # The full inbound-port list. Both the Deployment (containerPorts) and the
                # Service render every port; the first is the primary (ingress + probe).
                "inbound_ports": inbound_ports,
                # Base probe: tcp on the application port (or none when there is no
                # inbound port). The health-check service overrides these via its
                # manifest contribution; the template keeps its own path defaults
                # (| default('/')) and falls probe_port back to application_port.
                "probe_scheme": probe_scheme,  # none | tcp | http | https
                "probe_port": None,  # None -> template falls back to application_port
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
                # De eigen serviceaccount van het project; elke podspec draagt daarnaast zijn
                # eigen pull-secret, dus de erfenis van de default serviceaccount vervalt.
                "service_account_name": generate_project_service_account_name(project_name),
                # Timestamp to force pod restart when secrets are regenerated
                "generated_at": generated_at,
                # CA certificate configuration for SSL/TLS
                "ca_config": get_ca_certificate_config(cluster),
                # Prometheus metrics configuration. Base is None; the metrics-scraper
                # provider overrides this with the component's scrape {port, path} via
                # its manifest contribution (RC-5 Phase 6b) when the service is used.
                "metrics_config": None,
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

            # Merge each service's manifest contribution into the template context
            # (RC-5 Phase 6b). The contributions were collected once, above, before the
            # dict was built; the merge semantics per field live with the function.
            apply_manifest_contributions(variables, manifest_contributions)

            # Auth-wall observability: the provider contributes nothing when a component
            # asks for an auth wall but no keycloak secret is provisioned (old warn path).
            # The cookie secret itself is now part of the auth-wall provider's
            # contribution (secret_files), written in the shared loop below.
            if component_uses_authorization_wall:
                if "authorization_wall" in variables:
                    logger.info(f"Authorization wall enabled for component '{component_name}'")
                else:
                    logger.warning(
                        f"Component '{component_name}' uses authorization-wall but no keycloak service configured"
                    )

            # Generate extra manifests for sidecars (e.g. ConfigMaps)
            # Each sidecar template can define a 'configmap' section that produces a standalone manifest
            if "sidecars" in variables:
                if target_dir:
                    sidecar_output_dir = os.path.join(working_dir, target_dir)
                else:
                    sidecar_output_dir = os.path.join(working_dir, project_name, deployment_name)

                for sidecar_name in variables["sidecars"]:
                    sidecar_template_path = os.path.join(MANIFEST_TEMPLATES_DIR, f"sidecar-{sidecar_name}.yaml.jinja")
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
            manifests = ["deployment.yaml.jinja"]
            # A component with no inbound port gets no Service (nothing to expose).
            if has_inbound_port:
                manifests.append("service.yaml.jinja")

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
                manifest_path = os.path.join(MANIFEST_TEMPLATES_DIR, manifest_file)

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
                                            MANIFEST_TEMPLATES_DIR,
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
                                    provided_tls_sops_filename = sops_filenames(provided_tls_manifest_name).plaintext
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

                    # Create the root ingress (``subdomain.base-domain``) when this is the
                    # root component of a dotted component-per-subdomain layout. Keyed on
                    # domain-format: the legacy ``domain-mode: nice-url`` files were
                    # migrated to ``component.subdomain`` (schema v2.8), and only the
                    # dotted formats WITH a component segment carry a root address next to
                    # the per-component ones.
                    # The root ingress composes ``subdomain.base-domain`` itself instead of
                    # asking get_component_ingress_map, so the approval fallback that moves
                    # the components to the cluster address does not reach it. Without this
                    # check an unapproved domain still got an apex-style ingress plus a
                    # certificate request for a domain nobody granted this project.
                    is_root_component = component_name == root_component_name
                    if (
                        domain_format in ROOT_COMPONENT_FORMAT_IDS
                        and subdomain
                        and base_domain
                        and is_root_component
                        and is_deployment_domain_approved(project_data, base_domain, subdomain, cluster)
                    ):
                        root_hostname = generate_root_hostname(subdomain, base_domain)
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
                            old_root = get_domain_setting(previous_dep, DomainSetting.ROOT_COMPONENT)
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
                    if (
                        expose_on_bare_domain
                        and base_domain
                        and component_name == expose_on_bare_domain
                        and is_deployment_domain_approved(project_data, base_domain, subdomain, cluster)
                    ):
                        # Same rule as at registration: never an apex ingress plus
                        # certificate from a tenant namespace on a platform domain, nor on
                        # a domain that is not approved for this project.
                        validate_bare_domain_allowed(base_domain, get_supported_base_domains(cluster), project_data)
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
                            old_bare = get_domain_setting(previous_dep, DomainSetting.BARE_DOMAIN_COMPONENT)
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

            # Sleep-mode: emit the waker (Deployment + ConfigMap + token Secret) behind
            # this component's Service when the deployment sleeps and this is the chosen
            # waker component. No-op otherwise. Pruned by the obsolete-manifest sweep like
            # any other component file once the deployment is awake again.
            await self._emit_waker_manifests(
                project_data=project_data,
                deployment=deployment,
                component_name=component_name,
                component_reference=component_reference,
                unique_name=unique_name,
                namespace=namespace,
                cluster=cluster,
                project_name=project_name,
                # The Service's own targetPort, contributions applied (the authorization
                # wall moves it to the oauth2-proxy port). Taken from the built values so
                # the waker can never drift from the Service it sits behind.
                service_port=variables.get("service_port"),
                output_dir=full_output_dir,
                created_files=created_files,
                private_key=private_key,
            )

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
            secret_template_path = os.path.join(MANIFEST_TEMPLATES_DIR, "generic-secret.yaml.to-sops.jinja")

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
                    resolved_aliases = resolve_aliases(keycloak_aliases, keycloak_secret_data)
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
                sops_filename = sops_filenames(sso_manifest_name).plaintext
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
                resolved_platform_aliases = resolve_aliases(platform_aliases, platform_secret_data)
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
                sops_filename = sops_filenames(user_manifest_name).plaintext
                created_files.append(sops_filename)
                logger.info(f"User secret will be SOPS encrypted: {sops_filename}")
                logger.info(f"Successfully created user secret manifest: {user_secret_path}")

            # Create binary secrets for file-mode attachments (one per attachment id per deployment).
            # Keyed by secret name so duplicate references across components stay idempotent.
            if attachment_file_secrets:
                binary_secret_template_path = os.path.join(MANIFEST_TEMPLATES_DIR, "binary-secret.yaml.to-sops.jinja")
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
                    attachment_sops_filename = sops_filenames(attachment_manifest_name).plaintext
                    if attachment_sops_filename not in created_files:
                        created_files.append(attachment_sops_filename)
                    logger.info(f"Created attachment secret manifest: {secret_name}")

            # Create Let's Encrypt Issuer manifest if configured (once per unique base-domain/issuer combination)
            if base_domain and issuer_config and issuer_config.startswith("letsencrypt"):
                # Track created issuers to avoid duplicates within this deployment
                issuer_key = f"{base_domain}:{issuer_config}"
                if issuer_key not in created_issuers:
                    created_issuers.add(issuer_key)
                    created_files.extend(
                        emit_letsencrypt_issuer(
                            self._manifest_generator,
                            project_data=project_data,
                            deployment_name=deployment_name,
                            base_domain=base_domain,
                            issuer_config=issuer_config,
                            cluster_name=cluster,
                            namespace=namespace,
                            output_dir=full_output_dir,
                        )
                    )

            # Write each used service's declared secret files via the shared writer
            # (RC-5 Phase 6c). The typed secret plus alias/register flags are built by
            # the service provider (build_secret_files); this loop only drives the
            # shared write. Byte-identical to the old per-service blocks -- write order
            # is immaterial (kustomization is built from a disk scan and the
            # obsolete-manifest prune keeps a name set, both order-independent).
            for contribution in manifest_contributions:
                for spec in contribution.secret_files:
                    await self._write_secret_file(
                        spec,
                        deployment_name=deployment_name,
                        namespace=namespace,
                        output_dir=full_output_dir,
                        template_path=secret_template_path,
                        created_files=created_files,
                        private_key=private_key,
                    )

        # Emit one tenant-baseline NetworkPolicy per deployment after all
        # components have been processed. The policy selects pods by the
        # `deployment: <name>` label (added in deployment.yaml.jinja), so
        # multiple components of the same deployment share one policy, while
        # unlabeled pods (helm, helmfile, backup/restore) are covered by the
        # namespace-level baseline policy.
        if target_dir:
            deployment_output_dir = os.path.join(working_dir, target_dir)
        else:
            deployment_output_dir = os.path.join(working_dir, project_name, deployment_name)
        baseline_template_path = os.path.join(MANIFEST_TEMPLATES_DIR, "tenant-baseline-network-policy.yaml.jinja")
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
            "ops_egress": get_ops_egress(cluster),
            "cluster_cidrs": get_cluster_cidrs(cluster),
            # A regular app namespace hosts no CNPG cluster, so the operator does
            # not need ingress here (only the infrastructure namespace does).
            "database_operator_namespace": None,
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

        # Deployment-wide, service-owned manifests (RC-15). After the component loop and the
        # baseline, every service that overrides contribute_deployment_manifests may add
        # resources scoped to the whole deployment (cross-domain-access adds one NetworkPolicy
        # per own component with rules). The kustomization is rebuilt from the on-disk glob,
        # so writing the files is enough for them to be included; the prune in
        # _process_deployment_manifests removes any that this run no longer generates.
        deployment_manifest_ctx = DeploymentManifestContext(
            project_name=project_name,
            project_data=project_data,
            deployment=deployment,
            cluster=cluster,
            namespace=namespace,
        )
        for service in deployment_manifest_services():
            for spec in service.contribute_deployment_manifests(deployment_manifest_ctx):
                self._manifest_generator.create_manifest_file(
                    template_path=os.path.join(MANIFEST_TEMPLATES_DIR, spec.template_path),
                    values=spec.values,
                    output_dir=deployment_output_dir,
                    output_filename=spec.filename,
                    use_sops=False,
                )
                created_files.append(f"{spec.filename}.yaml")
                logger.info(
                    f"Created {service.service_type.value} deployment manifest '{spec.filename}.yaml' "
                    f"for deployment '{deployment_name}'"
                )

        # Clear rollback info on success
        self._pending_subdomain_rollback = None
        return created_files

    async def create_kustomization_file(
        self,
        git_connector: GitRepoWorkspace,
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

    async def get_contents(self, *, record_base: bool = True) -> dict[str, Any]:
        """
        Convenience method to get the contents of the project file.

        :param record_base: whether this read becomes the compare-and-swap base for a
            later save. True for a caller that is going to mutate and save what it
            gets back; False for an internal projection that reads the file, takes one
            value out of it and discards the rest.
        :return: Contents of the project file
        """
        # Read through the store, not off the working copy. Same committed state and
        # the same schema migration, but ProjectManager no longer needs a working
        # directory of its own -- which is what let callers read and write around the
        # store in the first place.
        self.__has_contents = True
        if self._project_file_relative_path is None:
            raise ValueError("Project file relative path is not set")
        data = await get_project_store().read_path(str(self._project_file_relative_path))
        if data is None:
            raise ValueError(f"Project file not found: {self._project_file_relative_path}")

        # Remember what was read so save_and_commit_project() can hand it to the store
        # as the compare-and-swap base. Nearly every write goes read-here/write-later
        # through this one class, so recording it here covers those call sites without
        # touching any of them. A copy, because callers mutate the dict they get back.
        #
        # record_base=False exists because this method is not called only by the caller
        # that saves. Projection helpers (get_name, get_deployments, ...) read the file
        # too, and they run BETWEEN a caller's read and its save -- in almost every
        # write method, on the very next line:
        #
        #     project_data = await self.get_contents()   # the dict we will save
        #     project_name = await self.get_name()       # reads the file again
        #
        # Were that second read to record as well, the base would end up NEWER than the
        # state project_data was built on. The store would then re-apply our change
        # against the newer base and *revert* the concurrent write instead of merging
        # with it -- and the result validates fine, so nothing downstream catches it.
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
            # TaskInputError en geen ValueError: dit is geen storing maar een verzoek dat
            # niet kan, en de worker maakt er daardoor een blijvende mislukking van MET een
            # reden die de aanroeper kan lezen. Hetzelfde woord dat de andere paden hier al
            # gebruiken (add_component_to_deployment geeft 'deployment_not_found' terug).
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

        This method is for manual API operations, not project-file-based cloning.
        De tunnel wordt door de gedeelde kloonfunctie opgezet, zodat de guard de
        chisel-server-url toetst in plaats van het lokale tunnel-endpoint.
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

        This method is for manual API operations, not project-file-based cloning.
        De tunnel wordt door de gedeelde kloonfunctie opgezet, zodat de guard de
        chisel-server-url toetst in plaats van het lokale tunnel-endpoint.
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
