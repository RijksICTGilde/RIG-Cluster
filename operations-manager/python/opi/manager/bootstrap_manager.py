"""Bootstrap manager for handling bootstrap API actions during deployment."""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opi.manager.bootstrap.bootstrap_api_handler import BootstrapApiHandler
from opi.services import ServiceType
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, get_domain_setting
from opi.services.project import Project
from opi.services.services import service_entry_name
from opi.services.tasks import progress_steps as steps

if TYPE_CHECKING:
    from opi.manager.project_manager import ProjectManager

logger = logging.getLogger(__name__)


class BootstrapManager:
    def __init__(self, project_manager: ProjectManager) -> None:
        self.project_manager = project_manager
        self.bootstrap_configs_dir = Path(__file__).parent.parent / "configs" / "bootstrap"

    async def execute_bootstrap_for_deployment(self, project_data: dict[str, Any], deployment: dict[str, Any]) -> None:
        project_name = await self.project_manager.get_name()
        deployment_name = deployment["name"]
        cluster = deployment["cluster"]

        bootstrap_config = deployment.get("bootstrap")
        if not bootstrap_config:
            logger.debug(f"Deployment {deployment_name} has no bootstrap configuration, skipping")
            return

        logger.info(f"Processing bootstrap actions for project: {project_name}, deployment: {deployment_name}")

        progress_manager = self.project_manager.get_progress_manager()
        bootstrap_task = None
        if progress_manager:
            bootstrap_task = progress_manager.add_task(steps.BOOTSTRAP_ACTIONS, subject=deployment_name)

        try:
            context = await self._build_context(project_data, deployment, cluster)

            if isinstance(bootstrap_config, dict) and "template" in bootstrap_config:
                await self._execute_template_bootstrap(bootstrap_config, context)
            elif isinstance(bootstrap_config, list):
                await self._execute_inline_bootstrap(bootstrap_config, context)
            else:
                logger.error(f"Invalid bootstrap configuration format for deployment {deployment_name}")
                return

            logger.info(f"Bootstrap actions completed for deployment {deployment_name}")

            if progress_manager and bootstrap_task:
                progress_manager.complete_task(bootstrap_task)

        except Exception as e:  # noqa: BLE001  # a failed bootstrap does not block the deployment
            logger.error(f"Bootstrap actions failed for deployment {deployment_name}: {e}")
            if progress_manager and bootstrap_task:
                progress_manager.fail_task(bootstrap_task, str(e))
            # A failed bootstrap does not block the deployment.

    async def _execute_template_bootstrap(self, bootstrap_config: dict[str, Any], context: dict[str, Any]) -> None:
        """Run the bootstrap from the template file ``bootstrap_config`` names."""
        template_name = bootstrap_config.get("template")
        if not template_name:
            msg = "No template name specified in bootstrap configuration"
            raise ValueError(msg)

        template_variables = bootstrap_config.get("variables", {})
        merged_context = {**context, **template_variables}

        template_path = self.bootstrap_configs_dir / f"{template_name}.yaml"

        if not template_path.exists():
            msg = f"Bootstrap template not found: {template_path}"
            raise FileNotFoundError(msg)

        logger.info(f"Executing bootstrap template: {template_name}")

        handler = BootstrapApiHandler()
        await handler.execute_config(template_path, merged_context)

    async def _execute_inline_bootstrap(self, bootstrap_config: list[dict[str, Any]], context: dict[str, Any]) -> None:
        logger.info("Executing inline bootstrap configuration")

        handler = BootstrapApiHandler()
        await handler.execute_inline_config(bootstrap_config, context)

    async def _build_context(
        self, project_data: dict[str, Any], deployment: dict[str, Any], cluster: str
    ) -> dict[str, Any]:
        from opi.core.cluster_config import get_ingress_postfix, get_ingress_tls_enabled, get_keycloak_discovery_url
        from opi.utils.naming import generate_external_hostname, generate_project_realm_name, generate_public_url

        project_name = await self.project_manager.get_name()
        deployment_name = deployment["name"]
        subdomain = get_domain_setting(deployment, DomainSetting.SUBDOMAIN)
        base_domain = get_domain_setting(deployment, DomainSetting.BASE_DOMAIN)

        # The context mirrors the deployment's environment variables: derive the hostname
        # the way those do, then turn it into a URL with generate_public_url, which takes
        # (hostname, use_https, path).
        ingress_postfix = get_ingress_postfix(cluster)
        if base_domain and subdomain:
            hostname = generate_external_hostname(subdomain, base_domain)
        elif subdomain:
            hostname = f"{subdomain}.{ingress_postfix}"
        else:
            hostname = f"{deployment_name}.{ingress_postfix}"
        public_host = generate_public_url(hostname, get_ingress_tls_enabled(cluster))

        oidc_url = None
        oidc_realm = None
        keycloak_service_client_secret = None

        services = project_data.get("services", [])
        for service in services:
            # The entry may be a bare string, a legacy single-key dict or a uniform record,
            # so the name is read rather than matched on a dict key.
            if service_entry_name(service) == ServiceType.KEYCLOAK.value:
                oidc_url = get_keycloak_discovery_url(cluster)
                oidc_realm = generate_project_realm_name(project_name, cluster)

                # The service client secret lives in the keycloak service config.
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
