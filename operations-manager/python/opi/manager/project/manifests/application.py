"""Building the manifests of one deployment: the components, their secrets and ingresses."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from opi.core.cluster_config import (
    get_backup_namespace,
    get_ca_certificate_config,
    get_cluster_cidrs,
    get_external_dns_target_for_hostname,
    get_ingress_cluster_issuer,
    get_ingress_controller_selector,
    get_ingress_ip_whitelist,
    get_ingress_postfix,
    get_ingress_tls_enabled,
    get_namespace,
    get_ops_egress,
    get_prefixed_namespace,
    supports_vpa,
)
from opi.core.config import settings
from opi.generation.manifests import MANIFEST_TEMPLATES_DIR
from opi.manager.project.aliases import (
    generate_storage_env_vars_from_services,
    generate_web_env_vars_from_services,
    resolve_aliases,
    scope_direct_aliases_to_component,
)
from opi.manager.project.emitters import emit_letsencrypt_issuer
from opi.manager.project.manifests.contributions import apply_manifest_contributions, collect_manifest_contributions
from opi.manager.project.manifests.secrets import (
    attachment_secret_prefix,
    existing_secret_pairs,
    secret_labels_for,
)
from opi.manager.project.results import DeploymentResult
from opi.manager.run_support import resolve_image
from opi.services import ServiceAdapter, ServiceType
from opi.services.catalog.base import (
    DeploymentManifestContext,
    ManifestContext,
    ManifestContribution,
    SecretFileSpec,
)
from opi.services.catalog.image_registries.resolution import (
    resolve_deployment_component_image,
)
from opi.services.catalog.publish_on_web.domain_config import (
    ROOT_COMPONENT_FORMAT_IDS,
    DomainSetting,
    get_domain_setting,
)
from opi.services.catalog.publish_on_web.hostnames import get_component_ingress_map
from opi.services.catalog.publish_on_web.issuer import effective_issuer
from opi.services.catalog.publish_on_web.subdomain import (
    get_supported_base_domains,
    is_deployment_domain_approved,
    validate_bare_domain_allowed,
)
from opi.services.catalog.publish_on_web.urls import public_url_map_for_deployment
from opi.services.persistence.subdomain_registry import SubdomainConnector
from opi.services.project_file_handler import extract_service_names_from_component
from opi.services.registry import deployment_manifest_services
from opi.services.secrets import BaseSecret, KeycloakSecret, PlatformSecret, UserSecret
from opi.utils.age import project_private_key_or_none
from opi.utils.env_vars import substitute_known_variables
from opi.utils.naming import (
    generate_bare_domain_hostname,
    generate_ingress_name_from_path,
    generate_issuer_name,
    generate_manifest_name,
    generate_network_policy_manifest_name,
    generate_network_policy_name,
    generate_project_service_account_name,
    generate_public_url,
    generate_pvc_name,
    generate_root_hostname,
    generate_storage_name,
    generate_tls_secret_name,
    generate_unique_name,
    get_infrastructure_namespace,
)
from opi.utils.sops import sops_filenames

if TYPE_CHECKING:
    from opi.connectors.git_repo import GitRepoWorkspace
    from opi.generation.manifests import ManifestGenerator
    from opi.manager.pvc_manager import PVCManager
    from opi.runtime.tasks.persistent_task_progress import PersistentTaskProgressManager
    from opi.services.catalog.image_registries.rules import ResolvedImage
    from opi.services.project_file_handler import ProjectFileHandler

logger = logging.getLogger(__name__)

#: The template every deployment secret is rendered from, plaintext until the SOPS step.
GENERIC_SECRET_TEMPLATE = os.path.join(MANIFEST_TEMPLATES_DIR, "generic-secret.yaml.to-sops.jinja")


@dataclass
class ComponentContributions:
    """What the services a component uses add to its manifests."""

    manifest_contributions: list[ManifestContribution]
    env_from_secrets: list[str]
    pod_replacement_mode: str
    uses_authorization_wall: bool


@dataclass
class ComponentSettings:
    """What one component's manifests are built from, as read from the project file."""

    image_url: str
    inbound_ports: list[int]
    has_inbound_port: bool
    application_port: int | None
    probe_scheme: str
    component_paths: list[dict[str, Any]]
    component_path: str | None
    image_pull_policy: str
    storage_configs: list[dict[str, Any]]
    publish_on_web: bool
    component_security: dict[str, Any] | None
    command_override: list[str] | None
    component_resources: dict[str, Any]
    replicas: int
    user_env_vars: dict[str, Any]
    attachment_secret_mounts: list[dict[str, str]]
    attachment_file_secrets: dict[str, dict[str, str]]
    unique_name: str
    processed_storage_configs: list[dict[str, Any]]
    use_https: bool
    subdomain: str | None
    base_domain: str | None
    issuer_config: str | None
    domain_format: str | None
    expose_on_bare_domain: str | bool | None
    ingress_map: dict[str, str]
    hostname: str


def _issuer_for(settings: ComponentSettings, cluster: str, deployment_name: str) -> tuple[str | None, str | None]:
    """The (namespace Issuer, ClusterIssuer) pair for an ingress; at most one of them is set.

    An own base-domain with an issuer gets a namespace-scoped Issuer: the generated one for
    Let's Encrypt, the configured name verbatim otherwise. Without such a domain the cluster's
    own ClusterIssuer signs it.
    """
    if settings.base_domain and settings.issuer_config:
        if settings.issuer_config in ("letsencrypt", "letsencrypt-staging"):
            return generate_issuer_name(settings.base_domain, settings.issuer_config, deployment_name), None
        return settings.issuer_config, None
    return None, get_ingress_cluster_issuer(cluster)


def _resolve_component_images(
    project_data: dict[str, Any], components: list[dict[str, Any]], cluster: str
) -> tuple[dict[str, ResolvedImage], dict[str, str]]:
    """Where each component's image comes from, and which pull secret belongs to it.

    Returns the resolved image per component reference, and the map from RESOLVED image url
    to pull-secret name that the templates read. The secret itself is namespace-scoped and
    lives at the project level.
    """
    component_definitions = {c.get("name"): c for c in project_data.get("components", []) or [] if isinstance(c, dict)}
    resolved_images: dict[str, ResolvedImage] = {}
    image_pull_secrets_map: dict[str, str] = {}

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
                f"Component '{component_reference}' image '{component.get('image')}' resolved to "
                f"'{resolved.image}' with pull secret '{resolved.secret}'"
            )
    return resolved_images, image_pull_secrets_map


def deployment_output_dir(working_dir: str, target_dir: str | None, project_name: str, deployment_name: str) -> str:
    """Where one deployment's manifests go in the clone.

    ``target_dir`` already carries the whole ``<cluster>/<project>/<deployment>`` path; without
    one the caller gets the two-segment fallback.
    """
    if target_dir:
        return os.path.join(working_dir, target_dir)
    return os.path.join(working_dir, project_name, deployment_name)


class ApplicationManifestsAccess(Protocol):
    """What the per-deployment manifest building reaches from its host.

    ``_deployment_results`` and ``_pending_subdomain_rollback`` are written here and read
    back by the host afterwards, so they stay there: this group is built per call and would
    forget them.
    """

    _deployment_aliases: dict[str, dict[str, dict[str, dict[str, str]]]]
    _deployment_results: dict[str, DeploymentResult]
    _force_clone_override: bool
    _manifest_generator: ManifestGenerator
    _pending_subdomain_rollback: dict[str, Any] | None
    _project_file_handler: ProjectFileHandler
    _pvc_manager: PVCManager

    async def get_contents(self, *, record_base: bool = True) -> dict[str, Any]: ...

    async def get_name(self) -> str: ...

    def get_previous_deployment(self, deployment_name: str) -> dict[str, Any] | None: ...

    def get_progress_manager(self) -> PersistentTaskProgressManager | None: ...

    def _add_secret_to_create(self, deployment_name: str, secret_type: str, secret_data: BaseSecret) -> None: ...

    def _get_secret_from_map[T: BaseSecret](
        self, deployment_name: str, secret_type: str, secret_class: type[T] | None = None
    ) -> T | None: ...


class ApplicationManifests:
    """The per-deployment manifest building of ``ProjectManager``, built per call around its host."""

    def __init__(self, host: ApplicationManifestsAccess) -> None:
        self._host = host

    def track_public_urls(self, project_data: dict[str, Any], deployment: dict[str, Any], project_name: str) -> None:
        """Record the public addresses this deployment offers on its result.

        The addresses come from publish-on-web, the service that decides whether a component
        gets an ingress, so they are the same ones the deployment endpoint returns and the
        detail page links. Deriving them here from the naming alone is what made a refresh
        promise an address that nothing serves (RC-104, question 13): a component without an
        ingress has a name, not an address.
        """
        self._host._deployment_results[deployment["name"]].urls.update(
            public_url_map_for_deployment(project_data, deployment, project_name, self._host._project_file_handler)
        )

    async def rollback_subdomain_if_needed(self) -> bool:
        """Undo the subdomain registration the host recorded, when the manifests failed after it.

        Returns True when a rollback was performed.
        """
        rollback_info = getattr(self._host, "_pending_subdomain_rollback", None)
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
            self._host._pending_subdomain_rollback = None
            return True
        except Exception as e:  # noqa: BLE001  # best-effort cleanup; it must not hide the failure it follows
            logger.error(f"Failed to rollback subdomain '{subdomain}.{base_domain}': {e}")
            return False

    async def emit_waker_manifests(
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
        """Emit the sleep-mode waker Deployment, ConfigMap and token Secret, or nothing."""
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
        selected = sleep_manifests.select_waker_component(
            project_data, deployment, config, self._host._project_file_handler
        )
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
        self._host._manifest_generator.create_manifest_file(
            template_path=deployment_template,
            values=deployment_values,
            output_dir=output_dir,
            output_filename=deployment_manifest_name,
            use_sops=False,
        )
        created_files.append(f"{deployment_manifest_name}.yaml")

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
        self._host._manifest_generator.create_manifest_file(
            template_path=configmap_template,
            values=configmap_values,
            output_dir=output_dir,
            output_filename=configmap_manifest_name,
            use_sops=False,
        )
        created_files.append(f"{configmap_manifest_name}.yaml")

        plain_token = await sleep_token.decrypt(current.wake_token, project_data)
        secret_template = os.path.join(MANIFEST_TEMPLATES_DIR, "generic-secret.yaml.to-sops.jinja")
        await self.write_secret_file(
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

    async def write_secret_file(
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
        """Write one deployment SOPS secret manifest, handling registration, aliases and pruning."""
        if spec.register_secret is not None:
            if not spec.secret_type:
                msg = f"SecretFileSpec '{spec.secret_name}' registers a secret without a secret_type to file it under"
                raise ValueError(msg)
            self._host._add_secret_to_create(deployment_name, spec.secret_type, spec.register_secret)

        secret_data = dict(spec.secret_pairs)
        manifest_name = f"{spec.secret_name}-secret"
        if spec.keep_existing_values:
            # In a thread: this starts the sops binary to read the previous ciphertext.
            existing = await asyncio.to_thread(
                existing_secret_pairs, os.path.join(output_dir, sops_filenames(manifest_name).encrypted), private_key
            )
            secret_data.update({key: existing[key] for key in spec.secret_pairs if key in existing})
        if spec.resolve_aliases and spec.secret_type:
            aliases = (
                self._host._deployment_aliases.get(deployment_name, {}).get("secret", {}).get(spec.secret_type, {})
            )
            if aliases:
                logger.debug(f"Resolving {len(aliases)} {spec.secret_type} aliases for deployment {deployment_name}")
                resolved_aliases = resolve_aliases(aliases, secret_data)
                secret_data.update(resolved_aliases)
                logger.info(f"Added {len(resolved_aliases)} resolved {spec.secret_type} aliases to deployment secret")

        secret_path = self._host._manifest_generator.create_manifest_file(
            template_path=template_path,
            values={
                "name": spec.secret_name,
                "namespace": namespace,
                "secret_pairs": secret_data,
                # One place turns "this is not application config" into the label the
                # ArgoCD plugin filters on, so a service states what its secret IS and
                # never has to know a config-hash exists.
                "secret_labels": secret_labels_for(spec),
            },
            output_dir=output_dir,
            output_filename=manifest_name,
            use_sops=True,
        )
        sops_filename = sops_filenames(manifest_name).plaintext
        created_files.append(sops_filename)
        logger.info(f"Secret '{spec.secret_name}' will be SOPS encrypted: {sops_filename}")
        logger.debug(f"Successfully created secret manifest: {secret_path}")

    async def create_application_manifests(
        self,
        deployment: dict[str, Any],
        git_connector: GitRepoWorkspace,
        target_dir: str | None = None,
    ) -> list[str]:
        """Create application manifests for each component in the deployment."""
        project_data = await self._host.get_contents()
        working_dir = await git_connector.get_working_dir()
        private_key = project_private_key_or_none(project_data)

        project_name = await self._host.get_name()
        logger.info(f"Creating application manifests for project: {project_name}")

        created_files = []

        deployment_name = deployment["name"]
        cluster = deployment["cluster"]
        namespace = get_prefixed_namespace(cluster, deployment["namespace"])

        if deployment_name not in self._host._deployment_results:
            self._host._deployment_results[deployment_name] = DeploymentResult(
                deployment_name=deployment_name,
                cluster=cluster,
                namespace=namespace,
            )

        self.track_public_urls(project_data, deployment, project_name)

        logger.info(f"Processing deployment: {deployment_name} in prefixed namespace: {namespace}")

        components = deployment.get("components", [])
        if not components:
            logger.warning(f"No components found in deployment {deployment_name}, skipping")
            return []

        root_component_name = get_domain_setting(deployment, DomainSetting.ROOT_COMPONENT)
        self._validate_publication_requirements(project_data, components, deployment)
        self._host._pending_subdomain_rollback = await self._register_deployment_domains(
            project_data, deployment, project_name=project_name, deployment_name=deployment_name, cluster=cluster
        )

        resolved_images, image_pull_secrets_map = _resolve_component_images(project_data, components, cluster)

        created_issuers: set[str] = set()

        for component in components:
            await self._emit_component(
                project_data,
                deployment,
                component,
                deployment_name=deployment_name,
                namespace=namespace,
                cluster=cluster,
                project_name=project_name,
                working_dir=working_dir,
                target_dir=target_dir,
                private_key=private_key,
                resolved_images=resolved_images,
                image_pull_secrets_map=image_pull_secrets_map,
                root_component_name=root_component_name,
                created_issuers=created_issuers,
                created_files=created_files,
            )

        output_dir = deployment_output_dir(working_dir, target_dir, project_name, deployment_name)
        self._emit_tenant_baseline_policy(
            deployment_name=deployment_name,
            namespace=namespace,
            cluster=cluster,
            project_name=project_name,
            output_dir=output_dir,
            created_files=created_files,
        )
        self._emit_service_deployment_manifests(
            project_data=project_data,
            deployment=deployment,
            deployment_name=deployment_name,
            namespace=namespace,
            cluster=cluster,
            project_name=project_name,
            output_dir=output_dir,
            created_files=created_files,
        )

        self._host._pending_subdomain_rollback = None
        return created_files

    async def _emit_component(
        self,
        project_data: dict[str, Any],
        deployment: dict[str, Any],
        component: dict[str, Any],
        *,
        deployment_name: str,
        namespace: str,
        cluster: str,
        project_name: str,
        working_dir: str,
        target_dir: str | None,
        private_key: str | None,
        resolved_images: dict[str, ResolvedImage],
        image_pull_secrets_map: dict[str, str],
        root_component_name: str | None,
        created_issuers: set[str],
        created_files: list[str],
    ) -> None:
        """One component of a deployment, from its settings to its secrets.

        The order is the pipeline: settings, env vars, service contributions, template
        context, manifests, waker, PVCs, secrets. ``created_files``, ``created_issuers``
        and the host's deployment result are what it leaves behind.
        """
        component_reference = component.get("reference")
        image_url = component.get("image", "")

        if not component_reference:
            logger.warning(f"Component missing reference in deployment {deployment_name}, skipping")
            return

        if not image_url:
            logger.info(f"Component '{component_reference}' has no image in deployment {deployment_name}, skipping")
            return

        # From here on the resolved reference: that is what the manifest carries and the
        # key of imagePullSecretsMap.
        image_url = resolved_images[component_reference].image

        component_name = component_reference

        progress_manager = self._host.get_progress_manager()
        if progress_manager:
            deployment_resource_name = f"{project_name}-{component_name}"
            progress_manager.update_component_deployment(component_name, deployment_resource_name)
            logger.debug(f"Updated component {component_name} deployment name to {deployment_resource_name}")

        settings = await self._component_settings(
            project_data,
            deployment,
            component,
            component_reference=component_reference,
            component_name=component_name,
            deployment_name=deployment_name,
            project_name=project_name,
            cluster=cluster,
            image_url=image_url,
        )
        web_address = self._host._deployment_results[deployment_name].urls.get(component_name)
        if web_address and progress_manager:
            progress_manager.update_component_web_address(component_name, web_address)

        env_vars, user_env_vars = self._component_env_vars(
            project_data, settings, component_name=component_name, deployment_name=deployment_name
        )

        contributions = self._component_contributions(
            project_data,
            settings,
            component_reference=component_reference,
            component_name=component_name,
            deployment_name=deployment_name,
            cluster=cluster,
            user_env_vars=user_env_vars,
        )

        variables = self._template_variables(
            settings,
            contributions,
            env_vars,
            component_name=component_name,
            deployment_name=deployment_name,
            project_name=project_name,
            namespace=namespace,
            cluster=cluster,
            image_pull_secrets_map=image_pull_secrets_map,
        )

        logger.info(f"Creating manifests for component: {component_name} with image: {settings.image_url}")

        apply_manifest_contributions(variables, contributions.manifest_contributions)

        if contributions.uses_authorization_wall:
            if "authorization_wall" in variables:
                logger.info(f"Authorization wall enabled for component '{component_name}'")
            else:
                logger.warning(
                    f"Component '{component_name}' uses authorization-wall but no keycloak service configured"
                )

        full_output_dir = deployment_output_dir(working_dir, target_dir, project_name, deployment_name)
        self._emit_sidecar_configmaps(
            variables, component_name=component_name, output_dir=full_output_dir, created_files=created_files
        )

        await self._emit_component_manifests(
            project_data,
            settings,
            variables,
            component_name=component_name,
            deployment_name=deployment_name,
            namespace=namespace,
            cluster=cluster,
            root_component_name=root_component_name,
            output_dir=full_output_dir,
            created_files=created_files,
        )

        await self.emit_waker_manifests(
            project_data=project_data,
            deployment=deployment,
            component_name=component_name,
            component_reference=component_reference,
            unique_name=settings.unique_name,
            namespace=namespace,
            cluster=cluster,
            project_name=project_name,
            service_port=variables.get("service_port"),
            output_dir=full_output_dir,
            created_files=created_files,
            private_key=private_key,
        )

        await self._emit_component_pvcs(
            project_data,
            deployment,
            settings,
            component_name=component_name,
            namespace=namespace,
            cluster=cluster,
            output_dir=full_output_dir,
            created_files=created_files,
        )

        self._emit_component_secrets(
            settings,
            user_env_vars,
            component_name=component_name,
            deployment_name=deployment_name,
            namespace=namespace,
            output_dir=full_output_dir,
            created_files=created_files,
        )

        if settings.base_domain and settings.issuer_config and settings.issuer_config.startswith("letsencrypt"):
            issuer_key = f"{settings.base_domain}:{settings.issuer_config}"
            if issuer_key not in created_issuers:
                created_issuers.add(issuer_key)
                created_files.extend(
                    emit_letsencrypt_issuer(
                        self._host._manifest_generator,
                        project_data=project_data,
                        deployment_name=deployment_name,
                        base_domain=settings.base_domain,
                        issuer_config=settings.issuer_config,
                        cluster_name=cluster,
                        namespace=namespace,
                        output_dir=full_output_dir,
                    )
                )

        for contribution in contributions.manifest_contributions:
            for spec in contribution.secret_files:
                await self.write_secret_file(
                    spec,
                    deployment_name=deployment_name,
                    namespace=namespace,
                    output_dir=full_output_dir,
                    template_path=GENERIC_SECRET_TEMPLATE,
                    created_files=created_files,
                    private_key=private_key,
                )

    async def _component_settings(
        self,
        project_data: dict[str, Any],
        deployment: dict[str, Any],
        component: dict[str, Any],
        *,
        component_reference: str,
        component_name: str,
        deployment_name: str,
        project_name: str,
        cluster: str,
        image_url: str,
    ) -> ComponentSettings:
        """Everything one component's manifests need, read from the project file.

        Deployment-level settings win over the component definition throughout: ports,
        paths, pull policy, resources, disabled state, user env vars and attachments.
        """
        # A component may declare no inbound port (background worker). Read the real
        # list with no default fallback: no ports -> no Service, no probe, no
        # containerPort. application_port is None in that case.
        inbound_ports = self._host._project_file_handler.extract_component_inbound_ports(
            project_data, component_reference
        )
        has_inbound_port = bool(inbound_ports)
        application_port = inbound_ports[0] if has_inbound_port else None

        # Base health-probe scheme: a plain TCP probe on the first inbound port.
        # A component without an inbound port cannot be probed by the kubelet, so
        # scheme=none disables all probes. The optional health-check service
        # overrides scheme/port/paths from here via its manifest contribution.
        probe_scheme = "tcp" if has_inbound_port else "none"

        component_paths = self._host._project_file_handler.extract_deployment_component_paths(
            project_data, deployment, component_reference
        )
        component_path = component_paths[0]["match"] if component_paths else "/"

        # Resolve image + pull policy via the shared helper (handles the
        # local/ Kind-image special); one implementation across deployments,
        # the database console and jobs.
        image_url, image_pull_policy = resolve_image(image_url, component.get("imagePullPolicy", "Always"))

        storage_configs = self._host._project_file_handler.extract_component_storage(project_data, component_reference)

        publish_on_web = self._host._project_file_handler.extract_component_publish_on_web(
            project_data, component_reference
        )

        # Extract optional per-component security context override (sandbox-only).
        # Hidden YAML-only feature — defaults to None (template falls back to 1001/1001/1001).
        component_security = self._host._project_file_handler.extract_component_security(
            project_data, component_reference
        )

        # Extract optional container `command` override (K8s containers[].command).
        # Hidden YAML-only feature: deployment-level override wins over the
        # component-level default; both default to None so the image's own
        # ENTRYPOINT/CMD remains in effect when neither is set.
        command_override = self._host._project_file_handler.extract_deployment_component_command(
            project_data, deployment_name, component_reference
        )
        if command_override is None:
            command_override = self._host._project_file_handler.extract_component_command(
                project_data, component_reference
            )

        component_resources = self._host._project_file_handler.extract_component_resources(
            project_data, component_reference
        )
        deployment_resources = self._host._project_file_handler.extract_deployment_component_resources(
            project_data, deployment_name, component_reference
        )
        if deployment_resources:
            logger.info(f"Found deployment-level resource overrides for component: {component_name}")
            component_resources.update(deployment_resources)

        # Extract disabled state - disabled components get replicas: 0
        # Check deployment-level first, falls back to component-definition level
        is_disabled, disabled_reason = self._host._project_file_handler.extract_deployment_component_disabled(
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
            logger.info(f"Deployment '{deployment_name}' is sleeping, setting component '{component_name}' replicas=0")

        user_env_vars = await self._host._project_file_handler.extract_component_user_env_vars(
            project_data, component_reference
        )

        deployment_user_env_vars = await self._host._project_file_handler.extract_deployment_component_user_env_vars(
            project_data, deployment_name, component_reference
        )
        if deployment_user_env_vars:
            logger.info(
                f"Found {len(deployment_user_env_vars)} deployment-level user-env-vars for component: {component_name}"
            )
            user_env_vars.update(deployment_user_env_vars)

        deployment_env_vars = component.get("env-vars", {})
        if deployment_env_vars:
            logger.info(f"Found {len(deployment_env_vars)} deployment-level env-vars for component: {component_name}")
            user_env_vars.update(deployment_env_vars)

        # Resolve attachment uses for this component: file mounts and env-var content.
        # File-mode attachments become a binary Secret mounted read-only at a path; env-var
        # mode (text only) is injected via the existing user secret / envFrom path.
        attachment_secret_mounts: list[dict[str, str]] = []
        attachment_file_secrets: dict[str, dict[str, str]] = {}
        resolved_attachments = await self._host._project_file_handler.resolve_attachments_for_component(
            project_data, component_reference, deployment_name=deployment_name
        )
        for att in resolved_attachments:
            reference = att["reference"]
            if att["provide-as"] == "env-var":
                # Content was verified UTF-8 decodable in resolve(); expose as an env-var value.
                user_env_vars[att["env-name"]] = att["content_bytes"].decode("utf-8")
            else:
                secret_name = f"{attachment_secret_prefix(deployment_name)}{reference}"
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

        unique_name = generate_unique_name(deployment_name, component_name)

        processed_storage_configs = []
        for i, storage in enumerate(storage_configs):
            storage_copy = storage.copy()
            mount_path = storage.get("mount-path", f"/storage-{i}")
            storage_name = generate_storage_name(mount_path, i)
            storage_copy["name"] = storage_name

            if storage.get("type") == "persistent":
                generation = self._host._project_file_handler.get_storage_generation(
                    project_data, deployment_name, component_name, storage_name
                )
                pvc_name = generate_pvc_name(unique_name, storage_name, generation)
                storage_copy["pvc_name"] = pvc_name

            processed_storage_configs.append(storage_copy)

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

        return ComponentSettings(
            image_url=image_url,
            inbound_ports=inbound_ports,
            has_inbound_port=has_inbound_port,
            application_port=application_port,
            probe_scheme=probe_scheme,
            component_paths=component_paths,
            component_path=component_path,
            image_pull_policy=image_pull_policy,
            storage_configs=storage_configs,
            publish_on_web=publish_on_web,
            component_security=component_security,
            command_override=command_override,
            component_resources=component_resources,
            replicas=replicas,
            user_env_vars=user_env_vars,
            attachment_secret_mounts=attachment_secret_mounts,
            attachment_file_secrets=attachment_file_secrets,
            unique_name=unique_name,
            processed_storage_configs=processed_storage_configs,
            use_https=use_https,
            subdomain=subdomain,
            base_domain=base_domain,
            issuer_config=issuer_config,
            domain_format=domain_format,
            expose_on_bare_domain=expose_on_bare_domain,
            ingress_map=ingress_map,
            hostname=hostname,
        )

    def _component_env_vars(
        self,
        project_data: dict[str, Any],
        settings: ComponentSettings,
        *,
        component_name: str,
        deployment_name: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """This component's direct env vars, and its user env vars with references resolved.

        The direct ones come from the services that hand out variables (storage,
        publish-on-web) plus the component's own direct aliases. The user env vars are
        resolved against exactly those, and go into a secret instead of the podspec.
        """
        user_env_vars = settings.user_env_vars
        env_vars = {}

        if settings.storage_configs:
            storage_env_vars = generate_storage_env_vars_from_services(settings.processed_storage_configs)
            if storage_env_vars:
                env_vars.update(storage_env_vars)

        if settings.publish_on_web and settings.hostname:
            web_env_vars = generate_web_env_vars_from_services(settings.hostname, settings.use_https)
            if web_env_vars:
                env_vars.update(web_env_vars)

        # Resolve and add direct aliases (aliases that reference direct env vars).
        # Scoped to the declaring component: the context below holds only THIS
        # component's storage and publish-on-web variables, so a sibling without
        # those services would resolve another component's aliases against an
        # empty context and fail.
        direct_aliases = scope_direct_aliases_to_component(
            self._host._deployment_aliases.get(deployment_name, {}).get("direct", {}),
            project_data,
            component_name,
        )
        for service_category, service_aliases in direct_aliases.items():
            if service_aliases:
                logger.debug(
                    f"Resolving {len(service_aliases)} direct {service_category} aliases for component {component_name}"
                )
                resolved_direct_aliases = resolve_aliases(service_aliases, env_vars)
                env_vars.update(resolved_direct_aliases)
                logger.info(
                    f"Added {len(resolved_direct_aliases)} resolved direct {service_category} aliases to component {component_name}"
                )

        if user_env_vars:
            # Resolve $VAR / ${VAR} in user-env-vars against everything this component can
            # see: the storage, publish-on-web and resolved-alias variables built above,
            # through the same engine the aliases use.
            #
            # Deliberately the LENIENT substitution: an unknown $NAME is left as it is. In an
            # alias a missing reference is a typo and must fail, but a user-env-var value is
            # often a password that happens to carry a dollar, and strict substitution would
            # fail such a deploy outright.
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
        return env_vars, user_env_vars

    def _component_contributions(
        self,
        project_data: dict[str, Any],
        settings: ComponentSettings,
        *,
        component_reference: str,
        component_name: str,
        deployment_name: str,
        cluster: str,
        user_env_vars: dict[str, Any],
    ) -> ComponentContributions:
        """What the services this component uses add to its manifests.

        Driven off the provider registry: a provider may add envFrom secrets and sidecars
        (additive) and override template vars (auth-wall moves ``service_port`` 8080 to
        4180). envFrom is assembled here because it is a template var itself; the
        template_vars and sidecars are merged once the context dict exists.
        """
        # The service contributions (envFrom, sidecars, template vars, secret files) are driven
        # off the provider registry; only the auth-wall log below still needs a flag of its own.
        component_uses_authorization_wall = False
        component_def = None
        all_services: list[str] = []

        if component_reference:
            component_def = self._host._project_file_handler._find_component(project_data, component_reference)
            all_services = extract_service_names_from_component(component_def) if component_def else []

            component_uses_authorization_wall = ServiceType.AUTHORIZATION_WALL.value in all_services

        # Each used service's manifest contribution, collected once. A provider may add envFrom
        # secrets and sidecars (additive) and override template vars (the auth wall moves
        # service_port from 8080 to 4180). envFrom is applied here, because it is a template var
        # itself; template_vars and sidecars are merged after the context dict below is built.
        manifest_ctx = ManifestContext(
            deployment_name=deployment_name,
            project_data=project_data,
            unique_name=settings.unique_name,
            cluster=cluster,
            get_secret=self._host._get_secret_from_map,
            component_def=component_def,
        )
        manifest_contributions = collect_manifest_contributions(
            manifest_ctx,
            component_services=all_services,
            project_services=ServiceAdapter.extract_service_names_from_project_services(
                project_data.get("services", []) or []
            ),
        )

        env_from_secrets = []

        for contribution in manifest_contributions:
            env_from_secrets.extend(contribution.env_from_secrets)

        platform_secret_name = PlatformSecret.get_secret_name(settings.unique_name)
        env_from_secrets.append(platform_secret_name)
        logger.debug(f"Platform secret added to envFrom: {platform_secret_name}")

        if user_env_vars:
            logger.info(f"Processing {len(user_env_vars)} user environment variables for component: {component_name}")
            user_secret_name = UserSecret.get_secret_name(settings.unique_name)
            env_from_secrets.append(user_secret_name)
            logger.debug(f"User secret added to envFrom: {user_secret_name}")

        pod_replacement_mode = (
            "Recreate"
            if any(item.get("type") == "persistent" for item in settings.storage_configs)
            else "RollingUpdate"
        )

        return ComponentContributions(
            manifest_contributions=manifest_contributions,
            env_from_secrets=env_from_secrets,
            pod_replacement_mode=pod_replacement_mode,
            uses_authorization_wall=component_uses_authorization_wall,
        )

    def _template_variables(
        self,
        settings: ComponentSettings,
        contributions: ComponentContributions,
        env_vars: dict[str, Any],
        *,
        component_name: str,
        deployment_name: str,
        project_name: str,
        namespace: str,
        cluster: str,
        image_pull_secrets_map: dict[str, str],
    ) -> dict[str, Any]:
        """The template context of one component, before the service contributions merge in."""
        generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        variables = {
            "name": settings.unique_name,
            "deployment_name": deployment_name,
            "component_name": component_name,
            "namespace": namespace,
            "hostname": settings.hostname,
            "project": {"name": project_name},
            "cluster": cluster,
            "pod_replacement_mode": contributions.pod_replacement_mode,
            "imageURL": settings.image_url,
            "imagePullPolicy": settings.image_pull_policy,
            "application_port": settings.application_port,
            # The Service's primary port; an auth wall in front overrides it to 4180.
            "service_port": settings.application_port,
            # The full inbound-port list. Both the Deployment (containerPorts) and the
            # Service render every port; the first is the primary (ingress + probe).
            "inbound_ports": settings.inbound_ports,
            # Base probe: tcp on the application port (or none when there is no
            # inbound port). The health-check service overrides these via its
            # manifest contribution; the template keeps its own path defaults
            # (| default('/')) and falls probe_port back to application_port.
            # none | tcp | http | https; a probe_port of None lets the template fall back to
            # application_port.
            "probe_scheme": settings.probe_scheme,
            "probe_port": None,
            "path": settings.component_path,
            "storage_configs": settings.processed_storage_configs,
            "env_vars": env_vars,
            "env_from_secrets": contributions.env_from_secrets,
            "attachment_secret_mounts": settings.attachment_secret_mounts,
            # Cluster-specific ingress configuration
            "enable_tls": get_ingress_tls_enabled(cluster),
            "ip_whitelist": get_ingress_ip_whitelist(cluster),
            # Registry authentication
            # Per image URL the name of its registry secret.
            "imagePullSecretsMap": image_pull_secrets_map,
            # The project's own service account; every podspec also carries its own pull
            # secret, so inheritance from the default service account falls away.
            "service_account_name": generate_project_service_account_name(project_name),
            # Timestamp to force pod restart when secrets are regenerated
            "generated_at": generated_at,
            "ca_config": get_ca_certificate_config(cluster),
            # None, until the metrics-scraper provider overrides it with the component's
            # scrape {port, path} through its manifest contribution.
            "metrics_config": None,
            "resources_requests_memory": settings.component_resources["requests_memory"],
            "resources_requests_cpu": settings.component_resources["requests_cpu"],
            "resources_limits_memory": settings.component_resources["limits_memory"],
            "resources_limits_cpu": settings.component_resources["limits_cpu"],
            # Replicas (0 when component is disabled)
            "replicas": settings.replicas,
            # Optional per-component pod securityContext override (sandbox-only).
            # Template reads K8s-native field names (runAsUser/runAsGroup/fsGroup).
            "security": (
                {
                    "runAsUser": settings.component_security.get("run-as-user"),
                    "runAsGroup": settings.component_security.get("run-as-group"),
                    "fsGroup": settings.component_security.get("fs-group"),
                }
                if settings.component_security
                else None
            ),
            # Optional container command override (K8s containers[].command).
            # None => template omits the field => image's ENTRYPOINT/CMD stays.
            "command": settings.command_override,
        }
        return variables

    def _emit_sidecar_configmaps(
        self, variables: dict[str, Any], *, component_name: str, output_dir: str, created_files: list[str]
    ) -> None:
        """Each sidecar template's own ConfigMap, rendered from its ``configmap`` section."""
        for sidecar_name in variables.get("sidecars", []):
            template_path = os.path.join(MANIFEST_TEMPLATES_DIR, f"sidecar-{sidecar_name}.yaml.jinja")
            configmap_variables = variables.copy()
            configmap_variables["section"] = "configmap"
            manifest_name = generate_manifest_name(component_name, f"{sidecar_name}-configmap")
            self._host._manifest_generator.create_manifest_file(
                template_path=template_path,
                values=configmap_variables,
                output_dir=output_dir,
                output_filename=manifest_name,
                use_sops=False,
            )
            created_files.append(f"{manifest_name}.yaml")
            logger.info(f"Created sidecar configmap for '{sidecar_name}': {manifest_name}")

    async def _emit_component_manifests(
        self,
        project_data: dict[str, Any],
        settings: ComponentSettings,
        variables: dict[str, Any],
        *,
        component_name: str,
        deployment_name: str,
        namespace: str,
        cluster: str,
        root_component_name: str | None,
        output_dir: str,
        created_files: list[str],
    ) -> None:
        """Render this component's Deployment, Service, ingresses and VerticalPodAutoscaler.

        Every template but the ingress renders once; the ingress renders per publication
        path, plus a root and a bare-domain ingress where the layout asks for them.
        """
        # tenant-baseline-network-policy.yaml.jinja is deliberately absent here: it is
        # rendered once per deployment, in _emit_tenant_baseline_policy.
        manifests = ["deployment.yaml.jinja"]
        # A component with no inbound port gets no Service (nothing to expose).
        if settings.has_inbound_port:
            manifests.append("service.yaml.jinja")

        # Add ingress manifest only if publish-on-web is enabled for this component
        if settings.publish_on_web:
            manifests.append("ingress.yaml.jinja")
            logger.info(f"Including ingress manifest for component '{component_name}' (publish-on-web: true)")
        else:
            logger.debug(f"Skipping ingress manifest for component '{component_name}' (publish-on-web: false)")

        # Add an Off-mode VerticalPodAutoscaler only on clusters that run a VPA
        # recommender. It mutates nothing (advice only); the auto-tuner reads its
        # .status.recommendation to size memory and CPU via GitOps.
        if supports_vpa(cluster):
            manifests.append("vpa.yaml.jinja")
            logger.debug(f"Including VPA manifest for component '{component_name}' (cluster '{cluster}' supports VPA)")

        for manifest_file in manifests:
            manifest_path = os.path.join(MANIFEST_TEMPLATES_DIR, manifest_file)

            if not os.path.exists(manifest_path):
                logger.warning(f"Manifest file not found: {manifest_path}")
                continue

            manifest_name = manifest_file.replace(".yaml.jinja", "")

            if manifest_name == "ingress":
                await self._emit_path_ingresses(
                    project_data,
                    settings,
                    variables,
                    component_name=component_name,
                    deployment_name=deployment_name,
                    namespace=namespace,
                    cluster=cluster,
                    manifest_path=manifest_path,
                    output_dir=output_dir,
                    created_files=created_files,
                )
                self._emit_root_ingress(
                    project_data,
                    settings,
                    variables,
                    component_name=component_name,
                    deployment_name=deployment_name,
                    cluster=cluster,
                    manifest_path=manifest_path,
                    output_dir=output_dir,
                    created_files=created_files,
                    root_component_name=root_component_name,
                )
                self._emit_bare_domain_ingress(
                    project_data,
                    settings,
                    variables,
                    component_name=component_name,
                    deployment_name=deployment_name,
                    cluster=cluster,
                    manifest_path=manifest_path,
                    output_dir=output_dir,
                    created_files=created_files,
                )

            else:
                unique_manifest_name = generate_manifest_name(component_name, manifest_name)

                use_sops_for_manifest = manifest_name == "generic-secret"

                manifest_file_path = self._host._manifest_generator.create_manifest_file(
                    template_path=manifest_path,
                    values=variables,
                    output_dir=output_dir,
                    output_filename=unique_manifest_name,
                    use_sops=use_sops_for_manifest,
                )

                created_files.append(f"{unique_manifest_name}.yaml")
                logger.info(f"Successfully created {manifest_file} manifest: {manifest_file_path}")

    async def _emit_path_ingresses(
        self,
        project_data: dict[str, Any],
        settings: ComponentSettings,
        variables: dict[str, Any],
        *,
        component_name: str,
        deployment_name: str,
        namespace: str,
        cluster: str,
        manifest_path: str,
        output_dir: str,
        created_files: list[str],
    ) -> None:
        """One ingress per publication path, for every hostname this component answers on.

        The TLS mode decides who issues the certificate: ``passthrough`` leaves it to the
        pod, ``provided`` renders the customer's own certificate as a Secret, and otherwise
        cert-manager issues it.
        """
        manifest_name = "ingress"
        manifest_file = "ingress.yaml.jinja"
        for ingress_base_name, ingress_hostname in settings.ingress_map.items():
            for path_config in settings.component_paths:
                path_value = path_config["match"] or "/"
                rewrite_value = path_config.get("rewrite")

                ingress_name = generate_ingress_name_from_path(ingress_base_name, path_value)

                if path_value == "/" or not path_value:
                    unique_manifest_name = generate_manifest_name(component_name, manifest_name)
                else:
                    path_suffix = path_value.lstrip("/").replace("/", "").lower()
                    unique_manifest_name = generate_manifest_name(component_name, f"{manifest_name}-{path_suffix}")

                ingress_variables = variables.copy()

                # Resolve the publish-on-web TLS mode for this component.
                # passthrough: the pod presents its own certificate; provided: the
                # customer's own certificate rendered as a kubernetes.io/tls Secret
                # on the ingress. Both leave the issuers None so no cert-manager
                # annotation is emitted and the platform issues nothing.
                tls_mode = self._host._project_file_handler.extract_component_publish_on_web_tls(
                    project_data, component_name, deployment_name
                )
                ingress_passthrough = tls_mode == "passthrough"

                # provided: build a SOPS-encrypted tls Secret from the referenced
                # PEM attachment and point the ingress at it (idempotent per path).
                provided_tls_secret_name: str | None = None
                if tls_mode == "provided":
                    try:
                        tls_data = await self._host._project_file_handler.resolve_publish_on_web_certificate(
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
                        result = self._host._deployment_results.get(deployment_name)
                        if result is not None:
                            result.status = "failed"
                            result.errors.append(str(cert_error))
                        continue
                    if tls_data:
                        provided_tls_secret_name = f"{deployment_name}-{component_name}-provided-tls"
                        provided_tls_manifest_name = generate_manifest_name(provided_tls_secret_name, "tls-secret")
                        self._host._manifest_generator.create_manifest_file(
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
                            output_dir=output_dir,
                            output_filename=provided_tls_manifest_name,
                            use_sops=True,
                        )
                        provided_tls_sops_filename = sops_filenames(provided_tls_manifest_name).plaintext
                        if provided_tls_sops_filename not in created_files:
                            created_files.append(provided_tls_sops_filename)

                # passthrough and provided leave both issuers None: the pod or the customer
                # brings the certificate, so no cert-manager annotation is emitted.
                ingress_issuer_name, ingress_cluster_issuer = (
                    (None, None)
                    if ingress_passthrough or provided_tls_secret_name
                    else _issuer_for(settings, cluster, deployment_name)
                )

                ingress_variables.update(
                    {
                        # The ingress name carries the path, the Service name does not.
                        "name": ingress_name,
                        "service_name": settings.unique_name,
                        "hostname": ingress_hostname,
                        "path": path_value,
                        # The rewrite target, e.g. "/" to strip the prefix.
                        "rewrite": rewrite_value,
                        # A namespace-scoped Issuer for an external domain, a ClusterIssuer for
                        # a cluster domain.
                        "issuer_name": ingress_issuer_name,
                        "cluster_issuer": ingress_cluster_issuer,
                        "tls_secret_name": generate_tls_secret_name(ingress_name),
                        "external_dns_target": get_external_dns_target_for_hostname(cluster, ingress_hostname),
                        "passthrough": ingress_passthrough,
                        "provided_tls_secret": provided_tls_secret_name,
                    }
                )

                manifest_file_path = self._host._manifest_generator.create_manifest_file(
                    template_path=manifest_path,
                    values=ingress_variables,
                    output_dir=output_dir,
                    output_filename=unique_manifest_name,
                    use_sops=False,
                )
                created_files.append(f"{unique_manifest_name}.yaml")
                logger.info(
                    f"Successfully created {manifest_file} manifest for {ingress_hostname}{path_value}: {manifest_file_path}"
                )

    def _emit_root_ingress(
        self,
        project_data: dict[str, Any],
        settings: ComponentSettings,
        variables: dict[str, Any],
        *,
        component_name: str,
        deployment_name: str,
        cluster: str,
        manifest_path: str,
        output_dir: str,
        created_files: list[str],
        root_component_name: str | None,
    ) -> None:
        """The root address ``subdomain.base-domain`` of a dotted component-per-subdomain layout.

        It composes that hostname itself instead of asking ``get_component_ingress_map``, so
        the approval fallback that moves the components to the cluster address does not reach
        it: hence the approval check here. Without it an unapproved domain still got an
        apex-style ingress plus a certificate request for a domain nobody granted this project.
        """
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
            settings.domain_format in ROOT_COMPONENT_FORMAT_IDS
            and settings.subdomain
            and settings.base_domain
            and is_root_component
            and is_deployment_domain_approved(project_data, settings.base_domain, settings.subdomain, cluster)
        ):
            root_hostname = generate_root_hostname(settings.subdomain, settings.base_domain)
            root_ingress_name = f"{deployment_name}-root"
            root_manifest_name = generate_manifest_name(component_name, "ingress-root")

            root_ingress_variables = variables.copy()

            root_issuer_name, root_cluster_issuer = _issuer_for(settings, cluster, deployment_name)

            root_ingress_variables.update(
                {
                    "name": root_ingress_name,
                    "service_name": settings.unique_name,
                    "hostname": root_hostname,
                    "path": "/",
                    "issuer_name": root_issuer_name,
                    "cluster_issuer": root_cluster_issuer,
                    "tls_secret_name": generate_tls_secret_name(root_ingress_name),
                    "external_dns_target": get_external_dns_target_for_hostname(cluster, root_hostname),
                }
            )

            root_manifest_file_path = self._host._manifest_generator.create_manifest_file(
                template_path=manifest_path,
                values=root_ingress_variables,
                output_dir=output_dir,
                output_filename=root_manifest_name,
                use_sops=False,
            )
            created_files.append(f"{root_manifest_name}.yaml")
            logger.info(f"Successfully created root ingress manifest for {root_hostname}: {root_manifest_file_path}")

            root_web_address = generate_public_url(root_hostname, settings.use_https)
            self._host._deployment_results[deployment_name].urls["root"] = root_web_address
            logger.info(f"Tracked root URL: {root_web_address}")

            previous_dep = self._host.get_previous_deployment(deployment_name)
            if previous_dep:
                old_root = get_domain_setting(previous_dep, DomainSetting.ROOT_COMPONENT)
                if old_root and old_root != root_component_name:
                    old_manifest = f"{generate_manifest_name(old_root, 'ingress-root')}.yaml"
                    old_manifest_path = os.path.join(output_dir, old_manifest)
                    if os.path.exists(old_manifest_path):
                        os.remove(old_manifest_path)
                        logger.info(
                            f"Removed stale root ingress manifest '{old_manifest}' "
                            f"(root-component changed from '{old_root}' to '{root_component_name}')"
                        )

    def _emit_bare_domain_ingress(
        self,
        project_data: dict[str, Any],
        settings: ComponentSettings,
        variables: dict[str, Any],
        *,
        component_name: str,
        deployment_name: str,
        cluster: str,
        manifest_path: str,
        output_dir: str,
        created_files: list[str],
    ) -> None:
        """The apex address of the project's own domain, for the component that serves it."""
        # Create bare domain ingress for expose-component-on-bare-domain mode.
        # expose_on_bare_domain holds the component name that should serve the bare domain.
        if (
            settings.expose_on_bare_domain
            and settings.base_domain
            and component_name == settings.expose_on_bare_domain
            and is_deployment_domain_approved(project_data, settings.base_domain, settings.subdomain, cluster)
        ):
            # Same rule as at registration: never an apex ingress plus
            # certificate from a tenant namespace on a platform domain, nor on
            # a domain that is not approved for this project.
            validate_bare_domain_allowed(settings.base_domain, get_supported_base_domains(cluster), project_data)
            bare_hostname = generate_bare_domain_hostname(settings.base_domain)
            bare_ingress_name = f"{deployment_name}-bare-domain"
            bare_manifest_name = generate_manifest_name(component_name, "ingress-bare-domain")

            bare_ingress_variables = variables.copy()

            bare_issuer_name, bare_cluster_issuer = _issuer_for(settings, cluster, deployment_name)

            bare_ingress_variables.update(
                {
                    "name": bare_ingress_name,
                    "service_name": settings.unique_name,
                    "hostname": bare_hostname,
                    "path": "/",
                    "issuer_name": bare_issuer_name,
                    "cluster_issuer": bare_cluster_issuer,
                    "tls_secret_name": generate_tls_secret_name(bare_ingress_name),
                    "external_dns_target": get_external_dns_target_for_hostname(cluster, bare_hostname),
                }
            )

            bare_manifest_file_path = self._host._manifest_generator.create_manifest_file(
                template_path=manifest_path,
                values=bare_ingress_variables,
                output_dir=output_dir,
                output_filename=bare_manifest_name,
                use_sops=False,
            )
            created_files.append(f"{bare_manifest_name}.yaml")
            logger.info(
                f"Successfully created bare domain ingress manifest for {bare_hostname}: {bare_manifest_file_path}"
            )

            bare_web_address = generate_public_url(bare_hostname, settings.use_https)
            self._host._deployment_results[deployment_name].urls["bare-domain"] = bare_web_address
            logger.info(f"Tracked bare domain URL: {bare_web_address}")

            previous_dep = self._host.get_previous_deployment(deployment_name)
            if previous_dep:
                old_bare = get_domain_setting(previous_dep, DomainSetting.BARE_DOMAIN_COMPONENT)
                if old_bare and old_bare != settings.expose_on_bare_domain:
                    old_manifest = f"{generate_manifest_name(old_bare, 'ingress-bare-domain')}.yaml"
                    old_manifest_path = os.path.join(output_dir, old_manifest)
                    if os.path.exists(old_manifest_path):
                        os.remove(old_manifest_path)
                        logger.info(
                            f"Removed stale bare domain ingress manifest '{old_manifest}' "
                            f"(expose-component-on-bare-domain changed from '{old_bare}' to '{settings.expose_on_bare_domain}')"
                        )

    async def _emit_component_pvcs(
        self,
        project_data: dict[str, Any],
        deployment: dict[str, Any],
        settings: ComponentSettings,
        *,
        component_name: str,
        namespace: str,
        cluster: str,
        output_dir: str,
        created_files: list[str],
    ) -> None:
        """The PersistentVolumeClaims of this component, through ``PVCManager``.

        That manager owns the generation in the PVC name and the cleanup of an older one.
        """
        persistent_storage = self._host._project_file_handler.get_persistent_storage(settings.processed_storage_configs)

        if persistent_storage:
            logger.info(f"Creating {len(persistent_storage)} PVC manifests for component: {component_name}")

            created_pvc_files = await self._host._pvc_manager.create_pvc_manifests_for_component(
                project_data=project_data,
                deployment=deployment,
                component_name=component_name,
                unique_name=settings.unique_name,
                persistent_storage=persistent_storage,
                namespace=namespace,
                cluster=cluster,
                full_output_dir=output_dir,
                manifest_generator=self._host._manifest_generator,
                force_clone_override=self._host._force_clone_override,
            )
            created_files.extend(created_pvc_files)

    def _emit_component_secrets(
        self,
        settings: ComponentSettings,
        user_env_vars: dict[str, Any],
        *,
        component_name: str,
        deployment_name: str,
        namespace: str,
        output_dir: str,
        created_files: list[str],
    ) -> None:
        """The secrets of one component: its SSO pair, its platform secret, its own env
        vars and the file-mode attachments it mounts.

        The platform secret is always written; the other three only when the component has
        the matching content. Every one of them is written as plaintext ``.to-sops.yaml``
        and encrypted one level up.
        """
        keycloak_credentials = self._host._get_secret_from_map(deployment_name, "keycloak", KeycloakSecret)
        if keycloak_credentials:
            use_sops_for_sso = True

            keycloak_secret = keycloak_credentials

            keycloak_secret_data = keycloak_secret.to_k8s_secret_data()

            keycloak_aliases = (
                self._host._deployment_aliases.get(deployment_name, {}).get("secret", {}).get("keycloak", {})
            )
            if keycloak_aliases:
                logger.debug(f"Resolving {len(keycloak_aliases)} keycloak aliases for deployment {deployment_name}")
                resolved_aliases = resolve_aliases(keycloak_aliases, keycloak_secret_data)
                keycloak_secret_data.update(resolved_aliases)
                logger.info(f"Added {len(resolved_aliases)} resolved keycloak aliases to deployment secret")

            sso_secret_vars = {
                "name": KeycloakSecret.get_secret_name(deployment_name),
                "namespace": namespace,
                "secret_type": "keycloak",
                # The base secret plus the resolved aliases.
                "secret_pairs": keycloak_secret_data,
            }

            sso_manifest_name = generate_manifest_name(deployment_name, "keycloak-secret")

            sso_secret_path = self._host._manifest_generator.create_manifest_file(
                template_path=GENERIC_SECRET_TEMPLATE,
                values=sso_secret_vars,
                output_dir=output_dir,
                output_filename=sso_manifest_name,
                use_sops=use_sops_for_sso,
            )

            sops_filename = sops_filenames(sso_manifest_name).plaintext
            created_files.append(sops_filename)
            logger.info(f"SSO secret will be SOPS encrypted: {sops_filename}")
            logger.info(f"Successfully created SSO secret manifest: {sso_secret_path}")

        platform_secret = PlatformSecret(
            deployment_name=deployment_name,
            component_name=component_name,
        )
        platform_secret_data = platform_secret.to_k8s_secret_data()

        platform_aliases = self._host._deployment_aliases.get(deployment_name, {}).get("secret", {}).get("platform", {})
        if platform_aliases:
            logger.debug(f"Resolving {len(platform_aliases)} platform aliases for component {component_name}")
            resolved_platform_aliases = resolve_aliases(platform_aliases, platform_secret_data)
            platform_secret_data.update(resolved_platform_aliases)
            logger.info(
                f"Added {len(resolved_platform_aliases)} resolved platform aliases to component {component_name}"
            )

        platform_secret_vars = {
            "name": PlatformSecret.get_secret_name(settings.unique_name),
            "namespace": namespace,
            "secret_type": "platform",
            "secret_pairs": platform_secret_data,
        }

        platform_manifest_name = generate_manifest_name(component_name, "platform-secret")
        self._host._manifest_generator.create_manifest_file(
            template_path=GENERIC_SECRET_TEMPLATE,
            values=platform_secret_vars,
            output_dir=output_dir,
            output_filename=platform_manifest_name,
            use_sops=False,
        )
        created_files.append(f"{platform_manifest_name}.yaml")
        logger.info(f"Created platform secret manifest: {platform_manifest_name}")

        if user_env_vars:
            logger.debug(f"Processing {len(user_env_vars)} user environment variables for {component_name}")

            user_secret = UserSecret(env_vars=user_env_vars)

            user_secret_vars = {
                "name": UserSecret.get_secret_name(settings.unique_name),
                "namespace": namespace,
                "secret_type": "user",
                "secret_pairs": user_secret.to_k8s_secret_data(),
            }

            user_manifest_name = generate_manifest_name(component_name, "user-secret")
            use_sops_for_user = True

            user_secret_path = self._host._manifest_generator.create_manifest_file(
                template_path=GENERIC_SECRET_TEMPLATE,
                values=user_secret_vars,
                output_dir=output_dir,
                output_filename=user_manifest_name,
                use_sops=use_sops_for_user,
            )

            sops_filename = sops_filenames(user_manifest_name).plaintext
            created_files.append(sops_filename)
            logger.info(f"User secret will be SOPS encrypted: {sops_filename}")
            logger.info(f"Successfully created user secret manifest: {user_secret_path}")

        # Create binary secrets for file-mode attachments (one per attachment id per deployment).
        # Keyed by secret name so duplicate references across components stay idempotent.
        if settings.attachment_file_secrets:
            binary_secret_template_path = os.path.join(MANIFEST_TEMPLATES_DIR, "binary-secret.yaml.to-sops.jinja")
            for secret_name, data_pairs in settings.attachment_file_secrets.items():
                attachment_secret_vars = {
                    "name": secret_name,
                    "namespace": namespace,
                    "secret_type": "attachment",
                    "data_pairs": data_pairs,
                }
                attachment_manifest_name = generate_manifest_name(secret_name, "attachment-secret")
                self._host._manifest_generator.create_manifest_file(
                    template_path=binary_secret_template_path,
                    values=attachment_secret_vars,
                    output_dir=output_dir,
                    output_filename=attachment_manifest_name,
                    use_sops=True,
                )
                attachment_sops_filename = sops_filenames(attachment_manifest_name).plaintext
                if attachment_sops_filename not in created_files:
                    created_files.append(attachment_sops_filename)
                logger.info(f"Created attachment secret manifest: {secret_name}")

    def _validate_publication_requirements(
        self, project_data: dict[str, Any], components: list[dict[str, Any]], deployment: dict[str, Any]
    ) -> None:
        """Refuse a deployment whose publication cannot work, before anything is registered.

        Every component with publish-on-web gets an ingress, in every layout, and an ingress
        points at the component's Service on its first inbound port. A component without one
        gets no Service, so its ingress would carry ``port.number: None`` and ArgoCD refuses
        the whole Application. A root component without publish-on-web leaves the root address
        unserved the same way. Running before the registrations keeps a failure from leaving an
        orphaned subdomain entry behind.
        """
        components_missing_ports = []
        for component in components:
            component_name = component.get("reference") or component.get("name")
            if not component_name:
                continue
            has_publish_on_web = self._host._project_file_handler.extract_component_publish_on_web(
                project_data, component_name
            )
            if has_publish_on_web and not self._host._project_file_handler.component_has_ports(
                project_data, component_name
            ):
                components_missing_ports.append(component_name)

        if components_missing_ports:
            raise ValueError(
                f"Components with 'publish-on-web' must have at least one port configured. "
                f"Missing ports for: {', '.join(components_missing_ports)}. "
                f"Add 'ports.inbound' to the component definition."
            )

        subdomain = get_domain_setting(deployment, DomainSetting.SUBDOMAIN)
        base_domain = get_domain_setting(deployment, DomainSetting.BASE_DOMAIN)
        domain_format = get_domain_setting(deployment, DomainSetting.DOMAIN_FORMAT)
        root_component_name = get_domain_setting(deployment, DomainSetting.ROOT_COMPONENT)
        if domain_format in ROOT_COMPONENT_FORMAT_IDS and subdomain and base_domain and root_component_name:
            root_has_publish_on_web = self._host._project_file_handler.extract_component_publish_on_web(
                project_data, root_component_name
            )
            if not root_has_publish_on_web:
                raise ValueError(
                    f"Component '{root_component_name}' is marked as root but does not have 'publish-on-web' enabled. "
                    f"Root components must have a service exposed for the root URL to work."
                )

    async def _register_deployment_domains(
        self,
        project_data: dict[str, Any],
        deployment: dict[str, Any],
        *,
        project_name: str,
        deployment_name: str,
        cluster: str,
    ) -> dict[str, Any] | None:
        """Bring this deployment's subdomain and bare-domain registrations up to date.

        The only step here that touches the subdomain database, so it is also the only step
        with something to undo. Returns what ``rollback_subdomain_if_needed`` needs when a
        registration was ADDED this round, and None when there is nothing to undo.
        """
        subdomain = get_domain_setting(deployment, DomainSetting.SUBDOMAIN)
        base_domain = get_domain_setting(deployment, DomainSetting.BASE_DOMAIN)
        domain_format = get_domain_setting(deployment, DomainSetting.DOMAIN_FORMAT)
        subdomain_registered = False
        subdomain_connector = None

        # Keyed on domain-format: the legacy ``domain-mode: nice-url`` files were migrated to
        # ``component.subdomain`` (schema v2.8).
        if domain_format in ROOT_COMPONENT_FORMAT_IDS and subdomain and base_domain:
            subdomain_connector = SubdomainConnector()

            existing_registration = await subdomain_connector.get_by_deployment(project_name, deployment_name)
            is_new_registration = existing_registration is None

            # register_or_update keeps an unchanged subdomain, replaces a changed one, and
            # raises on a validation or availability problem.
            await subdomain_connector.register_or_update_for_deployment(
                subdomain=subdomain,
                base_domain=base_domain,
                project_name=project_name,
                deployment_name=deployment_name,
                cluster=cluster,
                created_by=None,
            )
            subdomain_registered = is_new_registration
            logger.info(f"Subdomain '{subdomain}.{base_domain}' registered/updated for project '{project_name}'")

        expose_on_bare_domain = get_domain_setting(deployment, DomainSetting.BARE_DOMAIN_COMPONENT)
        bare_domain_registered = False
        # Saving is allowed, applying is not: the form lets a bare domain through on a custom
        # domain that is not approved yet (RC-216), so the approval hangs here, on the same
        # condition as the root address.
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

        if not (subdomain_registered or bare_domain_registered):
            return None
        return {
            "should_rollback": True,
            "connector": subdomain_connector,
            "project_name": project_name,
            "deployment_name": deployment_name,
            "subdomain": subdomain,
            "base_domain": base_domain,
        }

    def _emit_tenant_baseline_policy(
        self,
        *,
        deployment_name: str,
        namespace: str,
        cluster: str,
        project_name: str,
        output_dir: str,
        created_files: list[str],
    ) -> None:
        """One tenant-baseline NetworkPolicy per deployment, after the component loop.

        The policy selects pods on the ``deployment: <name>`` label that deployment.yaml.jinja
        adds, so every component of one deployment shares a single policy. Unlabeled pods
        (helm, helmfile, backup and restore) fall to the namespace-level baseline instead.
        Per component this collided on one filename and the last write won.
        """
        template_path = os.path.join(MANIFEST_TEMPLATES_DIR, "tenant-baseline-network-policy.yaml.jinja")
        manifest_name = generate_network_policy_manifest_name(f"{deployment_name}-tenant-baseline").replace(".yaml", "")
        variables = {
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
        self._host._manifest_generator.create_manifest_file(
            template_path=template_path,
            values=variables,
            output_dir=output_dir,
            output_filename=manifest_name,
            use_sops=False,
        )
        created_files.append(f"{manifest_name}.yaml")
        logger.info(
            f"Created tenant-baseline NetworkPolicy for deployment '{deployment_name}' "
            f"(selector deployment={deployment_name}, ops={variables['ops_namespace']}, "
            f"backup={variables['backup_namespace']}, "
            f"ingress={variables['ingress_controller_selector']['namespace']})"
        )

    def _emit_service_deployment_manifests(
        self,
        *,
        project_data: dict[str, Any],
        deployment: dict[str, Any],
        deployment_name: str,
        namespace: str,
        cluster: str,
        project_name: str,
        output_dir: str,
        created_files: list[str],
    ) -> None:
        """Deployment-wide, service-owned manifests (RC-15).

        Every service that overrides ``contribute_deployment_manifests`` may add resources
        scoped to the whole deployment; cross-domain-access adds one NetworkPolicy per own
        component with rules. The kustomization is rebuilt from the on-disk glob, so writing
        the file is enough to include it, and the prune in ``process_deployment_manifests``
        removes whatever this run no longer generates.
        """
        ctx = DeploymentManifestContext(
            project_name=project_name,
            project_data=project_data,
            deployment=deployment,
            cluster=cluster,
            namespace=namespace,
        )
        for service in deployment_manifest_services():
            for spec in service.contribute_deployment_manifests(ctx):
                self._host._manifest_generator.create_manifest_file(
                    template_path=os.path.join(MANIFEST_TEMPLATES_DIR, spec.template_path),
                    values=spec.values,
                    output_dir=output_dir,
                    output_filename=spec.filename,
                    use_sops=False,
                )
                created_files.append(f"{spec.filename}.yaml")
                logger.info(
                    f"Created {service.service_type.value} deployment manifest '{spec.filename}.yaml' "
                    f"for deployment '{deployment_name}'"
                )

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

        result = self._host._manifest_generator.create_kustomization_files(
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
