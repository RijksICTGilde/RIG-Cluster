"""Bootstrap manager for handling bootstrap API actions during deployment."""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opi.handlers.bootstrap_api_handler import BootstrapApiHandler
from opi.services import ServiceType
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, get_domain_setting
from opi.services.project import Project
from opi.services.services import service_entry_name

if TYPE_CHECKING:
    from opi.manager.project_manager import ProjectManager

logger = logging.getLogger(__name__)


class BootstrapManager:
    """Manager for bootstrap API operations."""

    def __init__(self, project_manager: ProjectManager) -> None:
        """
        Initialize the BootstrapManager with reference to ProjectManager.

        Args:
            project_manager: The main ProjectManager instance for accessing shared resources
        """
        self.project_manager = project_manager
        self.bootstrap_configs_dir = Path(__file__).parent.parent / "configs" / "bootstrap"

    async def execute_bootstrap_for_deployment(self, project_data: dict[str, Any], deployment: dict[str, Any]) -> None:
        """
        Execute bootstrap actions for a deployment.

        Args:
            project_data: The project configuration data
            deployment: The specific deployment configuration
        """
        project_name = await self.project_manager.get_name()
        deployment_name = deployment["name"]
        cluster = deployment["cluster"]

        # Check if deployment has bootstrap configuration
        bootstrap_config = deployment.get("bootstrap")
        if not bootstrap_config:
            logger.debug(f"Deployment {deployment_name} has no bootstrap configuration, skipping")
            return

        logger.info(f"Processing bootstrap actions for project: {project_name}, deployment: {deployment_name}")

        progress_manager = self.project_manager.get_progress_manager()
        bootstrap_task = None
        if progress_manager:
            bootstrap_task = progress_manager.add_task("Executing bootstrap API actions")

        try:
            # Build context for variable substitution
            context = await self._build_context(project_data, deployment, cluster)

            # Determine if inline or template-based configuration
            if isinstance(bootstrap_config, dict) and "template" in bootstrap_config:
                # Template-based configuration
                await self._execute_template_bootstrap(bootstrap_config, context)
            elif isinstance(bootstrap_config, list):
                # Inline configuration
                await self._execute_inline_bootstrap(bootstrap_config, context)
            else:
                logger.error(f"Invalid bootstrap configuration format for deployment {deployment_name}")
                return

            logger.info(f"Bootstrap actions completed for deployment {deployment_name}")

            if progress_manager and bootstrap_task:
                progress_manager.complete_task(bootstrap_task)

        except Exception as e:
            logger.error(f"Bootstrap actions failed for deployment {deployment_name}: {e}")
            if progress_manager and bootstrap_task:
                progress_manager.fail_task(bootstrap_task, str(e))
            # Don't raise - bootstrap failures should not block deployment

    async def _execute_template_bootstrap(self, bootstrap_config: dict[str, Any], context: dict[str, Any]) -> None:
        """Execute bootstrap from template file.

        Args:
            bootstrap_config: Bootstrap configuration with template reference
            context: Context variables for substitution
        """
        template_name = bootstrap_config.get("template")
        if not template_name:
            msg = "No template name specified in bootstrap configuration"
            raise ValueError(msg)

        # Merge template variables with context
        template_variables = bootstrap_config.get("variables", {})
        merged_context = {**context, **template_variables}

        # Load and execute template
        template_path = self.bootstrap_configs_dir / f"{template_name}.yaml"

        if not template_path.exists():
            msg = f"Bootstrap template not found: {template_path}"
            raise FileNotFoundError(msg)

        logger.info(f"Executing bootstrap template: {template_name}")

        handler = BootstrapApiHandler()
        await handler.execute_config(template_path, merged_context)

    async def _execute_inline_bootstrap(self, bootstrap_config: list[dict[str, Any]], context: dict[str, Any]) -> None:
        """Execute inline bootstrap configuration.

        Args:
            bootstrap_config: List of inline bootstrap action configurations
            context: Context variables for substitution
        """
        logger.info("Executing inline bootstrap configuration")

        handler = BootstrapApiHandler()
        await handler.execute_inline_config(bootstrap_config, context)

    async def _build_context(
        self, project_data: dict[str, Any], deployment: dict[str, Any], cluster: str
    ) -> dict[str, Any]:
        """Build context dictionary for variable substitution.

        Args:
            project_data: Project configuration data
            deployment: Deployment configuration
            cluster: Cluster name

        Returns:
            Context dictionary with variables
        """
        from opi.core.cluster_config import get_ingress_postfix, get_ingress_tls_enabled, get_keycloak_discovery_url
        from opi.utils.naming import generate_external_hostname, generate_project_realm_name, generate_public_url

        project_name = await self.project_manager.get_name()
        deployment_name = deployment["name"]
        subdomain = get_domain_setting(deployment, DomainSetting.SUBDOMAIN)
        base_domain = get_domain_setting(deployment, DomainSetting.BASE_DOMAIN)

        # Build context similar to deployment environment variables.
        # This used to call generate_public_url(deployment_name, project_name,
        # ingress_postfix, subdomain), a signature that function has not had in a long
        # time: it takes (hostname, use_https, path), so every bootstrap action raised
        # TypeError before it could run. Derive the hostname the same way the deployment
        # env vars do, then turn it into a URL.
        ingress_postfix = get_ingress_postfix(cluster)
        if base_domain and subdomain:
            hostname = generate_external_hostname(subdomain, base_domain)
        elif subdomain:
            hostname = f"{subdomain}.{ingress_postfix}"
        else:
            hostname = f"{deployment_name}.{ingress_postfix}"
        public_host = generate_public_url(hostname, get_ingress_tls_enabled(cluster))

        # Get Keycloak info if available
        oidc_url = None
        oidc_realm = None
        keycloak_service_client_secret = None

        # Check if keycloak service is used
        services = project_data.get("services", [])
        for service in services:
            # Format-agnostic: a bare string, the legacy single-key dict, or the uniform
            # record. The old `isinstance(dict) and "keycloak" in service` matched only
            # the legacy form, so a plain "keycloak" selection or a record with config
            # left oidc_url/oidc_realm unset even though the service was in use.
            if service_entry_name(service) == ServiceType.KEYCLOAK.value:
                oidc_url = get_keycloak_discovery_url(cluster)
                oidc_realm = generate_project_realm_name(project_name, cluster)

                # Try to get service client secret from the keycloak service config
                # (RC-5 B: relocated from the old project-level config.keycloak).
                keycloak_configs = Project(project_data).get("services/keycloak/config/realms") or []
                for kc_config in keycloak_configs:
                    if isinstance(kc_config, dict):
                        host = kc_config.get("host", "")
                        realm = kc_config.get("realm", "")
                        if oidc_url in host and oidc_realm == realm:
                            keycloak_service_client_secret = kc_config.get("service_client_secret")
                            break
                break

        context = {
            "PROJECT_NAME": project_name,
            "DEPLOYMENT_NAME": deployment_name,
            "CLUSTER": cluster,
            "PUBLIC_HOST": public_host,
            "INGRESS_POSTFIX": ingress_postfix,
        }

        if oidc_url:
            context["OIDC_URL"] = oidc_url
        if oidc_realm:
            context["OIDC_REALM"] = oidc_realm
        if keycloak_service_client_secret:
            context["KEYCLOAK_SERVICE_CLIENT_SECRET"] = keycloak_service_client_secret

        logger.debug(f"Built context with variables: {list(context.keys())}")

        return context
