"""Keycloak service manager for handling SSO resources."""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonpath_ng.ext import parse as jsonpath_parse
from ruamel.yaml.scalarstring import LiteralScalarString

from opi.connectors.keycloak import create_keycloak_connector
from opi.core.cluster_config import get_ingress_postfix, get_keycloak_discovery_url, get_keycloak_support_http
from opi.core.config import settings
from opi.core.startup import keycloak_operation_with_retry
from opi.handlers.keycloak_yaml_handler import KeycloakYamlHandler
from opi.services import ServiceAdapter, ServiceType
from opi.utils.age import encrypt_age_content, get_project_public_key
from opi.utils.naming import (
    generate_external_hostname,
    generate_ingress_map,
    generate_project_admin_username,
    generate_project_platform_client_id,
    generate_project_realm_name,
    resolve_effective_base_domain,
)
from opi.utils.passwords import generate_secure_password
from opi.utils.secrets import KeycloakSecret

if TYPE_CHECKING:
    from opi.manager.project_manager import ProjectManager

logger = logging.getLogger(__name__)


class KeycloakManager:
    """Manager for Keycloak SSO operations and resources."""

    def __init__(self, project_manager: "ProjectManager") -> None:
        """
        Initialize the KeycloakManager with reference to ProjectManager.

        Args:
            project_manager: The main ProjectManager instance for accessing shared resources
        """
        self.project_manager = project_manager

    async def create_resources_for_deployment(self, project_data: dict[str, Any], deployment: dict[str, Any]) -> None:
        """
        Create Keycloak SSO resources for a deployment that has SSO service enabled.

        Supports both component-based and helm-chart-based deployments.

        Args:
            project_data: The project configuration data
            deployment: The specific deployment configuration
        """
        project_name = await self.project_manager.get_name()
        deployment_name = deployment["name"]
        cluster = deployment["cluster"]

        # Check if any components in this deployment use SSO service
        sso_components = await self._get_sso_components_for_deployment(project_data, deployment_name)

        # Check if any helm-charts in this deployment use keycloak service
        uses_keycloak_via_helm = self._deployment_uses_keycloak_via_helm_charts(project_data, deployment_name)

        if not sso_components and not uses_keycloak_via_helm:
            logger.debug(
                f"Deployment {deployment_name} has no components or helm-charts using Keycloak service, skipping"
            )
            return

        logger.info(f"Processing Keycloak SSO resources for project: {project_name}, deployment: {deployment_name}")
        if sso_components:
            logger.info(f"Found {len(sso_components)} components using SSO: {', '.join(sso_components)}")
        if uses_keycloak_via_helm:
            logger.info(f"Deployment {deployment_name} uses Keycloak via helm-charts")

        # Extract and validate Keycloak configuration
        # This will raise ValueError/FileNotFoundError on invalid config
        keycloak_config = self._get_keycloak_service_config(project_data)

        progress_manager = self.project_manager.get_progress_manager()
        keycloak_task = None
        if progress_manager:
            keycloak_task = progress_manager.add_task("Creating Keycloak SSO resources")

        try:
            # Collect all hostnames from all SSO components/helm-charts in this deployment
            all_ingress_hosts = []
            ingress_postfix = get_ingress_postfix(cluster)
            subdomain = deployment.get("subdomain")
            base_domain = deployment.get("base-domain")

            if subdomain:
                logger.info(f"Using subdomain mode for deployment {deployment_name}: subdomain={subdomain}")
            else:
                logger.info(f"Using component-specific mode for deployment {deployment_name}")

            # Process component-based SSO (existing logic)
            for component_name in sso_components:
                # Check if we should process SSO for this component
                should_process = await self._should_process_sso_rijk(project_data, component_name)
                if not should_process:
                    logger.info(f"Skipping SSO setup for component {component_name} (not configured for SSO-Rijk)")
                    continue

                # Get hostname for this component using ingress map (supports subdomain)
                ingress_map = generate_ingress_map(
                    component_name, deployment_name, project_name, ingress_postfix, subdomain
                )
                hostname = next(iter(ingress_map.values()))
                all_ingress_hosts.append(hostname)
                logger.debug(f"Added hostname for component {component_name}: {hostname}")

            # Process helm-chart-based SSO (new logic)
            if uses_keycloak_via_helm:
                if not subdomain:
                    raise ValueError(
                        f"Helm-chart deployment {deployment_name} uses Keycloak but is missing required 'subdomain' field"
                    )
                # For helm-chart deployments, construct hostname from subdomain + base-domain
                effective_base_domain = resolve_effective_base_domain(base_domain, ingress_postfix)
                helm_hostname = generate_external_hostname(subdomain, effective_base_domain)
                if helm_hostname not in all_ingress_hosts:
                    all_ingress_hosts.append(helm_hostname)
                    logger.info(f"Added hostname for helm-chart deployment: {helm_hostname}")

            if not all_ingress_hosts:
                logger.info(f"No SSO-enabled components or helm-charts found in deployment {deployment_name}, skipping")
                return

            logger.info(
                f"Creating Keycloak client for deployment {deployment_name} with {len(all_ingress_hosts)} redirect URIs"
            )

            # Create ONE Keycloak client for the entire deployment with all redirect URIs
            keycloak_credentials = await self._setup_sso_rijk_integration(
                project_name=project_name,
                deployment_name=deployment_name,
                ingress_hosts=all_ingress_hosts,  # All hostnames from all components
                cluster=cluster,
                config=keycloak_config,  # Pass extracted Keycloak configuration
            )

            if keycloak_credentials:
                # Convert dictionary to KeycloakSecret instance for type safety
                keycloak_secret = KeycloakSecret(
                    client_id=keycloak_credentials["client_id"],
                    client_secret=keycloak_credentials["client_secret"],
                    discovery_url=keycloak_credentials.get("discovery_url", ""),
                    base_url=keycloak_credentials["base_url"],
                    realm=keycloak_credentials["realm"],
                )

                # Store ONE Keycloak secret for the deployment (not per component)
                self.project_manager._add_secret_to_create(deployment_name, "keycloak", keycloak_secret)
                logger.info(
                    f"Keycloak credentials stored for deployment {deployment_name} "
                    f"with {len(all_ingress_hosts)} redirect URIs"
                )
            else:
                logger.error(f"Failed to create Keycloak client for deployment {deployment_name}")

        finally:
            if progress_manager and keycloak_task:
                progress_manager.complete_task(keycloak_task)

    async def delete_resources_for_deployment(
        self, project_data: dict[str, Any], deployment: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Delete Keycloak resources for a deployment.

        Args:
            project_data: The project configuration data
            deployment: The specific deployment configuration

        Returns:
            Dictionary containing deletion results and status
        """
        project_name = await self.project_manager.get_name()
        deployment_name = deployment["name"]

        deletion_results = {
            "service": "keycloak",
            "deployment": deployment_name,
            "operations": [],
            "success": True,
            "errors": [],
        }

        # Check if any components in this deployment use SSO service
        sso_components = await self._get_sso_components_for_deployment(project_data, deployment_name)
        if not sso_components:
            deletion_results["operations"].append(
                {
                    "type": "keycloak_cleanup",
                    "status": "skipped",
                    "reason": "Deployment has no components using SSO service",
                }
            )
            logger.debug(f"Deployment {deployment_name} has no SSO components, skipping Keycloak cleanup")
            return deletion_results

        logger.info(f"Deleting Keycloak resources for project: {project_name}, deployment: {deployment_name}")
        logger.info(f"Found {len(sso_components)} SSO components: {', '.join(sso_components)}")

        # Get deployment cluster to determine which realm to use
        cluster = deployment.get("cluster")
        if not cluster:
            deletion_results["errors"].append("Deployment has no cluster specified")
            deletion_results["success"] = False
            return deletion_results

        # Try to get project realm for this cluster
        kc_config = await self.project_manager._get_project_keycloak_config_for_cluster(cluster)

        # Determine which realm to use (project realm or default for backwards compatibility)
        if kc_config:
            realm_name = kc_config["realm"]
            keycloak_host = kc_config["host"]
            logger.info(f"Using project realm {realm_name} for deletion")
        else:
            realm_name = settings.KEYCLOAK_DEFAULT_REALM
            keycloak_host = None
            logger.info(f"No project realm found, using default realm {realm_name}")

        try:
            if keycloak_host:
                keycloak = await create_keycloak_connector(
                    keycloak_url=keycloak_host,
                    admin_username=settings.KEYCLOAK_ADMIN_USERNAME,
                    admin_password=settings.KEYCLOAK_ADMIN_PASSWORD,
                )
            else:
                keycloak = await create_keycloak_connector()

            # Delete clients for each SSO component
            for component_name in sso_components:
                try:
                    logger.info(f"Attempting to delete Keycloak client for component: {component_name}")

                    # Try to delete the Keycloak client (with retry for robustness)
                    async def delete_client_operation():
                        return await keycloak.delete_deployment_client(
                            deployment_name=deployment_name, project_name=project_name, realm_name=realm_name
                        )

                    delete_success = await keycloak_operation_with_retry(delete_client_operation)

                    if delete_success:
                        deletion_results["operations"].append(
                            {
                                "type": "keycloak_client_deletion",
                                "target": f"{project_name}-{deployment_name}-{component_name}",
                                "component": component_name,
                                "deployment": deployment_name,
                                "status": "success",
                            }
                        )
                        logger.info(f"Successfully deleted Keycloak client for component: {component_name}")
                    else:
                        deletion_results["operations"].append(
                            {
                                "type": "keycloak_client_deletion",
                                "target": f"{project_name}-{deployment_name}-{component_name}",
                                "component": component_name,
                                "deployment": deployment_name,
                                "status": "not_found",
                            }
                        )
                        logger.info(
                            f"Keycloak client for component {component_name} was not found (may not have used SSO)"
                        )

                except Exception as e:
                    deletion_results["operations"].append(
                        {
                            "type": "keycloak_client_deletion",
                            "target": f"{project_name}-{deployment_name}-{component_name}",
                            "component": component_name,
                            "deployment": deployment_name,
                            "status": "error",
                            "error": str(e),
                        }
                    )
                    deletion_results["errors"].append(
                        f"Error deleting Keycloak client for component {component_name}: {e}"
                    )
                    logger.exception(f"Error deleting Keycloak client for component {component_name}: {e}")

        except Exception as e:
            # If we can't connect to Keycloak, log it but don't fail the entire deletion
            logger.warning(f"Could not connect to Keycloak for client cleanup: {e}")
            deletion_results["operations"].append(
                {"type": "keycloak_connection", "status": "error", "error": f"Could not connect to Keycloak: {e}"}
            )
            deletion_results["errors"].append(f"Keycloak client cleanup skipped: {e}")

        # Update success status based on errors
        deletion_results["success"] = len(deletion_results["errors"]) == 0

        return deletion_results

    def _get_keycloak_service_config(self, project_data: dict[str, Any]) -> dict[str, Any]:
        """
        Extract and validate Keycloak configuration from project-level services.

        This method extracts Keycloak template and variable configuration from the
        project's services section, following the same pattern as database service config.

        Expected format:
            services:
              - keycloak:
                  config:
                    template: "sso-only"  # or "algoritmeregister", etc.
                    variables:  # Optional template-specific variables
                      frontend_redirect_uris: "https://..."
                      realm_display_name: "Custom Name"
                    additional_redirect_uris:  # Optional additional redirect URIs for development
                      - "http://localhost:8080/*"
                      - "http://127.0.0.1:8080/*"

        Args:
            project_data: The project configuration data

        Returns:
            Dictionary with merged configuration:
            {
                "template": "sso-only",  # Template filename (without .yaml)
                "variables": {...},       # Template-specific variables
                "additional_redirect_uris": [...]  # Optional additional redirect URIs
                "restrict_access": {       # Optional access restriction config
                    "enabled": True,
                    "role": "allowed-user",
                    "error_message": "${accessDeniedNoPermission}"
                }
            }

        Raises:
            ValueError: If configuration format is invalid or contains path traversal
            FileNotFoundError: If specified template file doesn't exist
        """
        # Default configuration
        DEFAULT_CONFIG: dict[str, Any] = {
            "template": "sso-only",
            "variables": {},
            "additional_redirect_uris": [],
            "restrict_access": None,
        }

        project_services = project_data.get("services", [])
        if not project_services:
            logger.debug("No services defined, using default Keycloak config")
            return DEFAULT_CONFIG.copy()

        # Find keycloak service config
        user_config = None
        for service_item in project_services:
            if isinstance(service_item, dict):
                # Dict format: {"keycloak": {"config": {...}}}
                if "keycloak" in service_item:
                    service_data = service_item["keycloak"]
                    if not isinstance(service_data, dict):
                        raise ValueError(
                            f"Invalid keycloak service format. Expected dict with 'config' key, "
                            f"got {type(service_data).__name__}"
                        )
                    if "config" in service_data:
                        user_config = service_data["config"]
                        break

        # If no config specified, use defaults
        if user_config is None:
            logger.debug("No Keycloak config specified, using default template 'sso-only'")
            return DEFAULT_CONFIG.copy()

        # Validate config is a dict
        if not isinstance(user_config, dict):
            raise ValueError(f"Keycloak config must be a dict, got {type(user_config).__name__}")

        # Merge with defaults
        merged_config = DEFAULT_CONFIG.copy()

        # Extract and validate template
        if "template" in user_config:
            template = user_config["template"]
            if not isinstance(template, str):
                raise ValueError(f"Template must be a string, got {type(template).__name__}")

            # Security: prevent path traversal attacks
            if "/" in template or "\\" in template or ".." in template:
                raise ValueError(
                    f"Invalid template name: '{template}'. "
                    f"Template name must be a simple filename without path separators."
                )

            merged_config["template"] = template

        # Extract and validate variables
        if "variables" in user_config:
            variables = user_config["variables"]
            if not isinstance(variables, dict):
                raise ValueError(f"Template variables must be a dict, got {type(variables).__name__}")
            merged_config["variables"] = variables

        # Extract and validate additional_redirect_uris
        if "additional_redirect_uris" in user_config:
            additional_uris = user_config["additional_redirect_uris"]
            if not isinstance(additional_uris, list):
                raise ValueError(f"additional_redirect_uris must be a list, got {type(additional_uris).__name__}")
            # Validate all entries are strings
            for uri in additional_uris:
                if not isinstance(uri, str):
                    raise ValueError(f"All additional_redirect_uris must be strings, got {type(uri).__name__}: {uri}")
            merged_config["additional_redirect_uris"] = additional_uris
            logger.info(f"Found {len(additional_uris)} additional redirect URIs in config")

        # Extract and validate restrict_access
        if "restrict_access" in user_config:
            restrict_access = user_config["restrict_access"]
            if not isinstance(restrict_access, dict):
                raise ValueError(f"restrict_access must be a dict, got {type(restrict_access).__name__}")

            # Validate required fields if enabled
            if restrict_access.get("enabled", False):
                if "role" not in restrict_access:
                    raise ValueError("restrict_access.role is required when restrict_access.enabled is True")
                if not isinstance(restrict_access["role"], str):
                    raise ValueError(
                        f"restrict_access.role must be a string, got {type(restrict_access['role']).__name__}"
                    )

            merged_config["restrict_access"] = {
                "enabled": restrict_access.get("enabled", False),
                "role": restrict_access.get("role", "allowed-user"),
                "error_message": restrict_access.get("error_message", "${accessDeniedNoPermission}"),
            }
            logger.info(
                f"Access restriction configured: enabled={merged_config['restrict_access']['enabled']}, "
                f"role={merged_config['restrict_access']['role']}"
            )

        # CRITICAL: Validate template file exists
        template_path = Path(__file__).parent.parent / "configs" / "keycloak" / f"{merged_config['template']}.yaml"
        if not template_path.exists():
            # List available templates for helpful error message
            configs_dir = Path(__file__).parent.parent / "configs" / "keycloak"
            if configs_dir.exists():
                available_templates = sorted([f.stem for f in configs_dir.glob("*.yaml")])
                raise FileNotFoundError(
                    f"Keycloak template '{merged_config['template']}' not found at {template_path}. "
                    f"Available templates: {', '.join(available_templates)}"
                )
            else:
                raise FileNotFoundError(
                    f"Keycloak template '{merged_config['template']}' not found. "
                    f"Keycloak configs directory does not exist: {configs_dir}"
                )

        logger.info(f"Using Keycloak template: {merged_config['template']}")
        if merged_config["variables"]:
            logger.debug(f"Template variables provided: {list(merged_config['variables'].keys())}")

        return merged_config

    async def _get_sso_components_for_deployment(self, project_data: dict[str, Any], deployment_name: str) -> list[str]:
        """
        Get list of components in a deployment that use Keycloak service.

        BREAKING CHANGE: Now checks for 'keycloak' service (was 'sso-rijk').
        Components using 'sso-rijk' will be ignored.

        Args:
            project_data: The project configuration data
            deployment_name: Name of the deployment to check

        Returns:
            List of component names that use Keycloak service
        """
        sso_components = []

        # First get component references for this deployment
        component_refs_query = jsonpath_parse(f"$.deployments[?@.name=='{deployment_name}'].components[*].reference")
        component_refs = [match.value for match in component_refs_query.find(project_data)]

        # Then check if any of these components use Keycloak service
        for component_ref in component_refs:
            component_query = jsonpath_parse(f"$.components[?@.name=='{component_ref}']['uses-services']")
            component_services = [match.value for match in component_query.find(project_data)]
            # Flatten the services list (in case it's nested)
            all_services = []
            for services in component_services:
                if isinstance(services, list):
                    all_services.extend(services)
                else:
                    all_services.append(services)

            if ServiceType.KEYCLOAK.value in all_services:
                sso_components.append(component_ref)

        return sso_components

    async def _should_process_sso_rijk(self, project_data: dict[str, Any], component_reference: str) -> bool:
        """
        Check if a component has the Keycloak service enabled.

        BREAKING CHANGE: Now checks for 'keycloak' service (was 'sso-rijk').

        Args:
            project_data: The project configuration data
            component_reference: The component reference name

        Returns:
            True if Keycloak should be processed, False otherwise
        """
        try:
            components = project_data.get("components", [])
            for component in components:
                if component.get("name") == component_reference:
                    # Check uses-services array for Keycloak
                    uses_services = component.get("uses-services", [])
                    component_services = ServiceAdapter.parse_services_from_strings(uses_services)
                    has_keycloak_service = ServiceType.KEYCLOAK in component_services

                    if has_keycloak_service:
                        return True

            return False

        except Exception as e:
            logger.exception(f"Error checking Keycloak service for component {component_reference}: {e}")
            return False

    def _deployment_uses_keycloak_via_helm_charts(
        self, project_data: dict[str, Any], deployment_name: str
    ) -> bool:
        """
        Check if a deployment uses Keycloak service via helm-charts.

        Uses the shared utility method in project_file_handler.

        Args:
            project_data: The project configuration data
            deployment_name: Name of the deployment to check

        Returns:
            True if deployment uses Keycloak via helm-charts, False otherwise
        """
        # Get helm-chart references from the deployment
        helm_chart_refs = self.project_manager._project_file_handler.extract_deployment_helm_charts(
            project_data, deployment_name
        )

        if not helm_chart_refs:
            return False

        # Check each helm-chart reference for keycloak service
        for helm_chart_ref in helm_chart_refs:
            chart_reference = helm_chart_ref.get("reference")
            if not chart_reference:
                continue

            # Find the helm-chart definition
            helm_chart_def = self.project_manager._project_file_handler.get_helm_chart_by_name(
                project_data, chart_reference
            )
            if not helm_chart_def:
                continue

            # Check uses-services in the helm-chart definition
            uses_services = helm_chart_def.get("uses-services", [])
            chart_services = ServiceAdapter.parse_services_from_strings(uses_services)
            if ServiceType.KEYCLOAK in chart_services:
                return True

        return False

    # Hostname calculation moved to centralized naming.py

    async def _setup_sso_rijk_integration(
        self,
        project_name: str,
        deployment_name: str,
        ingress_hosts: list[str],
        cluster: str,
        config: dict[str, Any],
    ) -> dict[str, Any] | None:
        """
        Set up SSO-Rijk integration by adding a client to the project realm.
        Checks for existing credentials in secrets map first.

        Creates ONE client per deployment with ALL redirect URIs from all components.

        Args:
            project_name: Name of the project
            deployment_name: Name of the deployment
            ingress_hosts: List of all ingress hostnames for this deployment (from all components)
            cluster: Cluster name to determine which project realm to use
            config: Keycloak configuration with template and variables

        Returns:
            Dictionary with Keycloak credentials, or None if failed
        """
        try:
            # Get project realm config for this cluster
            kc_config = await self.project_manager._get_project_keycloak_config_for_cluster(cluster)

            # Determine if we need to create/recreate the realm
            need_to_create_realm = False
            keycloak_url = self.project_manager._get_keycloak_url_for_cluster(cluster)

            if not kc_config:
                # No config exists - definitely need to create
                logger.info(f"Project realm config not found for cluster {cluster}, will create realm")
                need_to_create_realm = True
            else:
                # Config exists - verify realm actually exists in Keycloak
                realm_name = kc_config["realm"]
                keycloak_host = kc_config["host"]

                # Check if realm exists in Keycloak
                verify_keycloak = await create_keycloak_connector(
                    keycloak_url=keycloak_host,
                    admin_username=settings.KEYCLOAK_ADMIN_USERNAME,
                    admin_password=settings.KEYCLOAK_ADMIN_PASSWORD,
                )

                if await verify_keycloak.realm_exists(realm_name):
                    logger.info(f"Verified project realm {realm_name} exists in Keycloak")
                else:
                    logger.warning(
                        f"Project realm config exists but realm {realm_name} not found in Keycloak - will recreate"
                    )
                    need_to_create_realm = True

            if need_to_create_realm:
                logger.info(f"Creating project realm infrastructure for cluster {cluster}...")
                await self._setup_project_keycloak_realm(project_name, cluster, keycloak_url, config, ingress_hosts)
                # Reload keycloak config after realm creation
                kc_config = await self.project_manager._get_project_keycloak_config_for_cluster(cluster)

                if not kc_config:
                    raise RuntimeError(f"Failed to create project realm for cluster {cluster}")

            realm_name = kc_config["realm"]
            keycloak_host = kc_config["host"]

            logger.info(f"Using project realm {realm_name} for deployment {deployment_name}")

            # Check for existing credentials in secrets map (not config)
            existing_credentials = self.project_manager._get_secret_from_map(
                deployment_name, "keycloak", KeycloakSecret
            )

            if existing_credentials:
                logger.info(f"Using existing Keycloak credentials for {project_name}/{deployment_name}")

                # Always check and apply access restriction even for existing clients
                restrict_access = config.get("restrict_access")
                if restrict_access and restrict_access.get("enabled", False):
                    keycloak = await create_keycloak_connector(
                        keycloak_url=keycloak_host,
                        admin_username=settings.KEYCLOAK_ADMIN_USERNAME,
                        admin_password=settings.KEYCLOAK_ADMIN_PASSWORD,
                    )
                    await self._apply_access_restriction(
                        keycloak=keycloak,
                        realm_name=realm_name,
                        client_id=existing_credentials.client_id,
                        restrict_access=restrict_access,
                    )

                return {
                    "client_id": existing_credentials.client_id,
                    "client_secret": existing_credentials.client_secret,
                    "discovery_url": existing_credentials.discovery_url,
                    "base_url": existing_credentials.base_url,
                    "realm": existing_credentials.realm,
                }

            # No existing credentials, create new client
            logger.info(
                f"Creating new Keycloak client for deployment {project_name}/{deployment_name} "
                f"with {len(ingress_hosts)} redirect URIs"
            )

            keycloak = await create_keycloak_connector(
                keycloak_url=keycloak_host,
                admin_username=settings.KEYCLOAK_ADMIN_USERNAME,
                admin_password=settings.KEYCLOAK_ADMIN_PASSWORD,
            )

            # Get cluster-specific HTTP support setting
            support_http = get_keycloak_support_http(cluster)

            # Get additional redirect URIs from config
            additional_redirect_uris = config.get("additional_redirect_uris", [])

            # Create client in project realm
            client_info = await keycloak.create_deployment_client(
                project_name=project_name,
                deployment_name=deployment_name,
                ingress_hosts=ingress_hosts,
                realm_name=realm_name,
                support_http=support_http,
                additional_redirect_uris=additional_redirect_uris if additional_redirect_uris else None,
            )

            # Apply access restriction if configured
            restrict_access = config.get("restrict_access")
            if restrict_access and restrict_access.get("enabled", False):
                await self._apply_access_restriction(
                    keycloak=keycloak,
                    realm_name=realm_name,
                    client_id=client_info["client_id"],
                    restrict_access=restrict_access,
                )

            # Get cluster-specific discovery URL for the project realm
            cluster_discovery_url = get_keycloak_discovery_url(cluster)
            realm_discovery_url = f"{cluster_discovery_url}/realms/{realm_name}/.well-known/openid-configuration"

            credentials = {
                "client_id": client_info["client_id"],
                "client_secret": client_info["client_secret"],
                "discovery_url": realm_discovery_url,
                "base_url": keycloak_host,
                "realm": realm_name,
            }

            # Store credentials in secrets map
            keycloak_secret = KeycloakSecret(
                client_id=client_info["client_id"],
                client_secret=client_info["client_secret"],
                discovery_url=realm_discovery_url,
                base_url=keycloak_host,
                realm=realm_name,
            )
            self.project_manager._add_secret_to_create(deployment_name, "keycloak", keycloak_secret)

            logger.info(f"Successfully created Keycloak client: {client_info['client_id']}")
            return credentials

        except Exception:
            logger.exception(f"Error setting up SSO-Rijk integration for {deployment_name}")
            raise

    async def _apply_access_restriction(
        self,
        keycloak: Any,
        realm_name: str,
        client_id: str,
        restrict_access: dict[str, Any],
    ) -> None:
        """
        Apply access restriction to a client using client roles and conditional authentication flow.

        This creates:
        1. A client role that users need to access the application
        2. A restricted browser flow that checks for the role (for direct logins)
        3. Sets the flow as an authentication override on the client
        4. A post-broker login flow for SSO/IdP authentication (if IdPs are configured)
        5. Sets the post-broker login flow on all identity providers in the realm

        Args:
            keycloak: KeycloakConnector instance
            realm_name: Name of the realm
            client_id: Client ID (not UUID)
            restrict_access: Access restriction configuration with:
                - enabled: bool
                - role: str (role name)
                - error_message: str (theme message key)
        """
        role_name = restrict_access.get("role", "allowed-user")
        error_message = restrict_access.get("error_message", "${accessDeniedNoPermission}")
        browser_flow_alias = f"browser-restricted-{client_id}"
        post_broker_flow_alias = f"post-broker-restricted-{client_id}"

        logger.info(f"Applying access restriction to client '{client_id}' in realm '{realm_name}'")
        logger.info(f"  - Role: {role_name}")
        logger.info(f"  - Error message: {error_message}")
        logger.info(f"  - Browser flow alias: {browser_flow_alias}")
        logger.info(f"  - Post-broker flow alias: {post_broker_flow_alias}")

        try:
            # Step 1: Create the client role
            logger.info(f"Creating client role '{role_name}' for client '{client_id}'")
            await keycloak.create_client_role(
                realm_name=realm_name,
                client_id=client_id,
                role_name=role_name,
                description=f"Users with this role can access {client_id}",
            )

            # Step 2: Create the restricted browser flow (for direct logins)
            logger.info(f"Creating restricted browser flow '{browser_flow_alias}'")
            await keycloak.create_restricted_browser_flow(
                realm_name=realm_name,
                flow_alias=browser_flow_alias,
                client_id=client_id,
                role_name=role_name,
                error_message=error_message,
            )

            # Step 3: Set the browser flow as an authentication override on the client
            logger.info(f"Setting authentication flow override on client '{client_id}'")
            await keycloak.set_client_authentication_flow_override(
                realm_name=realm_name,
                client_id=client_id,
                browser_flow_alias=browser_flow_alias,
            )

            # Step 4: Check for identity providers and set up post-broker login flow
            # This ensures SSO users are also checked for the required role
            await self._apply_post_broker_login_restriction(
                keycloak=keycloak,
                realm_name=realm_name,
                client_id=client_id,
                role_name=role_name,
                error_message=error_message,
                post_broker_flow_alias=post_broker_flow_alias,
            )

            logger.info(f"Access restriction successfully applied to client '{client_id}'")

        except Exception as e:
            logger.exception(f"Error applying access restriction to client '{client_id}': {e}")
            raise

    async def _apply_post_broker_login_restriction(
        self,
        keycloak: Any,
        realm_name: str,
        client_id: str,
        role_name: str,
        error_message: str,
        post_broker_flow_alias: str,
    ) -> None:
        """
        Apply post-broker login restriction to all identity providers in the realm.

        This ensures that users authenticating via SSO/IdP are also checked for the required role.

        Args:
            keycloak: KeycloakConnector instance
            realm_name: Name of the realm
            client_id: Client ID for the role check
            role_name: Client role name that grants access
            error_message: Error message key from theme
            post_broker_flow_alias: Alias for the post-broker login flow
        """
        # Get all identity providers in the realm
        identity_providers = await keycloak.get_identity_providers(realm_name)

        if not identity_providers:
            logger.debug(f"No identity providers found in realm '{realm_name}', skipping post-broker flow setup")
            return

        logger.info(
            f"Found {len(identity_providers)} identity provider(s) in realm '{realm_name}', "
            f"setting up post-broker login flow"
        )

        # Create the post-broker login flow
        await keycloak.create_post_broker_login_flow(
            realm_name=realm_name,
            flow_alias=post_broker_flow_alias,
            client_id=client_id,
            role_name=role_name,
            error_message=error_message,
        )

        # Set the post-broker login flow on each identity provider
        for idp in identity_providers:
            idp_alias = idp.get("alias")
            if idp_alias:
                logger.info(f"Setting post-broker login flow on IdP '{idp_alias}'")
                await keycloak.set_identity_provider_post_broker_login_flow(
                    realm_name=realm_name,
                    provider_alias=idp_alias,
                    flow_alias=post_broker_flow_alias,
                )

    async def _get_keycloak_credentials_from_config(
        self, project_data: dict[str, Any], deployment_name: str, project_name: str
    ) -> dict[str, Any] | None:
        """
        Retrieve existing Keycloak credentials from project config.

        Args:
            project_data: The project configuration data
            deployment_name: Name of the deployment
            project_name: Name of the project

        Returns:
            Dictionary with Keycloak credentials, or None if not found
        """
        try:
            # Look for credentials in project data under deployments
            deployments = project_data.get("deployments", [])
            for deployment in deployments:
                if deployment.get("name") == deployment_name:
                    keycloak_config = deployment.get("keycloak", {})
                    if keycloak_config.get("client_id") and keycloak_config.get("client_secret"):
                        logger.debug(f"Found existing Keycloak credentials in config for {deployment_name}")
                        return {
                            "client_id": keycloak_config["client_id"],
                            "client_secret": keycloak_config["client_secret"],
                            "discovery_url": keycloak_config.get("discovery_url", ""),
                            "issuer_url": keycloak_config.get("issuer_url", ""),
                        }

            return None

        except Exception as e:
            logger.exception(f"Error retrieving Keycloak credentials from config: {e}")
            return None

    async def _store_keycloak_credentials_in_config(
        self, deployment_name: str, project_name: str, credentials: dict[str, Any]
    ) -> None:
        """
        Store Keycloak credentials in the project configuration.

        NOTE: This method is deprecated and no longer used. In the new architecture,
        deployment client credentials are stored in K8s secrets via secrets map only.

        Args:
            deployment_name: Name of the deployment
            project_name: Name of the project
            credentials: Keycloak credentials to store
        """
        # Deployment credentials are stored in K8s secrets via secrets map, not in project config
        logger.debug("Deployment credentials are stored in K8s secrets, not storing in project config")

    async def _setup_project_keycloak_realm(
        self,
        project_name: str,
        cluster: str,
        keycloak_url: str,
        config: dict[str, Any],
        ingress_hosts: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Set up project-level Keycloak infrastructure for a cluster using YAML configuration.

        This creates the project realm, admin user, and federation with RIG Platform.

        Steps:
        1. Generate admin username/password
        2. Encrypt password with project's AGE public key
        3. Execute YAML configuration (realm, federation, IDP, SSO flow, client scope)
        4. Create project admin user in master realm
        5. Assign realm-admin role to admin for project realm
        6. Store config in project.yaml
        7. Save project file

        Args:
            project_name: Name of the project
            cluster: Name of the cluster
            keycloak_url: Base URL of the Keycloak server
            config: Keycloak configuration dict with template and variables
            ingress_hosts: Optional list of ingress hostnames for redirect URIs

        Returns:
            Dictionary with host, realm, username, password (plain text for immediate use)

        Raises:
            FileNotFoundError: If template file doesn't exist
            ValueError: If config is malformed
        """
        logger.info(f"Setting up project Keycloak realm for {project_name} in cluster {cluster} using YAML")

        # Generate names
        admin_username = generate_project_admin_username(project_name, cluster)
        realm_name = generate_project_realm_name(project_name, cluster)
        platform_client_id = generate_project_platform_client_id(project_name, cluster)

        # Generate and encrypt password
        admin_password = generate_secure_password()
        project_data = await self.project_manager.get_contents()
        project_public_key = get_project_public_key(project_data)

        if not project_public_key:
            raise Exception(f"Project public key not found for {project_name}")

        encrypted_password = await encrypt_age_content(admin_password, project_public_key)
        encrypted_password_str = LiteralScalarString(encrypted_password)

        # Create Keycloak connector
        keycloak = await create_keycloak_connector(
            keycloak_url=keycloak_url,
            admin_username=settings.KEYCLOAK_ADMIN_USERNAME,
            admin_password=settings.KEYCLOAK_ADMIN_PASSWORD,
        )

        # Extract template from config (already validated in _get_keycloak_service_config)
        template_name = config["template"]  # Will KeyError if config malformed
        yaml_path = Path(__file__).parent.parent / "configs" / "keycloak" / f"{template_name}.yaml"

        # Double-check template exists (defensive programming)
        if not yaml_path.exists():
            raise FileNotFoundError(f"Keycloak template not found: {yaml_path}")

        logger.info(f"Loading Keycloak template: {template_name}.yaml for realm {realm_name}")

        # Build base context for YAML template
        context = {
            # Infrastructure variables
            "project_name": project_name,
            "cluster": cluster,
            "keycloak_url": keycloak_url,
            "platform_realm_name": settings.KEYCLOAK_DEFAULT_REALM,
            "project_realm_name": realm_name,
            "project_display_name": f"{project_name} ({cluster})",
            "platform_client_id": platform_client_id,
            # Unified variable names (works with all templates)
            "realm_name": realm_name,
            "realm_display_name": f"{project_name} ({cluster})",
        }

        # Add redirect URIs from component ingress hosts if provided
        if ingress_hosts:
            # Get cluster-specific HTTP support setting
            support_http = get_keycloak_support_http(cluster)

            # Build redirect URIs from ingress hosts based on cluster protocol support
            # Use first host as frontend_redirect_uris (templates expect single value)
            # TODO: Support multiple redirect URIs using forEach in templates
            if support_http:
                # Local cluster: support both HTTP and HTTPS
                first_redirect_uri = f"http://{ingress_hosts[0]}/*"
                logger.info("Cluster supports HTTP - using HTTP redirect URI for template")
            else:
                # Production cluster: HTTPS only
                first_redirect_uri = f"https://{ingress_hosts[0]}/*"
                logger.info("Cluster HTTPS only - using HTTPS redirect URI for template")

            context["frontend_redirect_uris"] = first_redirect_uri
            logger.info(f"Added frontend_redirect_uris to context: {first_redirect_uri}")
            if len(ingress_hosts) > 1:
                logger.warning(
                    f"Multiple ingress hosts provided ({len(ingress_hosts)}), "
                    f"but only using first one for frontend_redirect_uris. "
                    f"Additional hosts: {', '.join(ingress_hosts[1:])}"
                )

        # Merge user-provided variables (overrides defaults)
        user_variables = config.get("variables", {})
        if not isinstance(user_variables, dict):
            raise ValueError(f"Template variables must be a dict, got {type(user_variables).__name__}")

        context.update(user_variables)

        logger.debug(f"Template context variables: {list(context.keys())}")

        # Execute YAML configuration for project realm
        handler = KeycloakYamlHandler(keycloak)
        await handler.execute_config(yaml_path, context)
        logger.info(f"Executed YAML configuration ({template_name}) for realm {realm_name}")

        # Create admin user in master realm with delegated access to the project realm
        # This allows the user to login at /admin/ and manage only the project realm
        admin_email = f"{admin_username}@local.invalid"
        logger.info(f"Creating realm admin user {admin_username} in master realm for {realm_name}")
        user_info = await keycloak.create_user(
            realm_name="master",
            username=admin_username,
            password=admin_password,
            email=admin_email,
            first_name="Realm",
            last_name="Administrator",
            enabled=True,
        )
        logger.info(f"Created admin user {admin_username} in master realm")

        # Assign realm management permissions for the project realm
        # This grants full admin access to the specific realm via the {realm}-realm client in master
        await keycloak.assign_realm_admin_from_master(
            target_realm_name=realm_name, user_id=user_info["id"]
        )
        logger.info(f"Assigned realm management permissions for {realm_name} to {admin_username}")

        # Store in project config
        if "config" not in project_data:
            project_data["config"] = {}
        if "keycloak" not in project_data["config"]:
            project_data["config"]["keycloak"] = []

        # Check if this realm config already exists
        existing_config = None
        for idx, kc_entry in enumerate(project_data["config"]["keycloak"]):
            if kc_entry.get("realm") == realm_name:
                existing_config = idx
                break

        config_entry = {
            "host": keycloak_url,
            "realm": realm_name,
            "username": admin_username,
            "password": encrypted_password_str,
        }

        if existing_config is not None:
            # Update existing entry
            project_data["config"]["keycloak"][existing_config] = config_entry
            logger.info(f"Updated existing Keycloak config for realm {realm_name}")
        else:
            # Add new entry
            project_data["config"]["keycloak"].append(config_entry)
            logger.info(f"Added new Keycloak config for realm {realm_name}")

        await self.project_manager.save_project_data()
        logger.info(f"Stored Keycloak config in project file for cluster {cluster}")

        return {
            "host": keycloak_url,
            "realm": realm_name,
            "username": admin_username,
            "password": admin_password,  # Return plain password for immediate use
        }
