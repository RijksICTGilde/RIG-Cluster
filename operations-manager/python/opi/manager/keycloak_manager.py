"""Keycloak service manager for handling SSO resources."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonpath_ng.ext import parse as jsonpath_parse
from ruamel.yaml.scalarstring import LiteralScalarString

from opi.connectors.keycloak import create_keycloak_connector
from opi.core.cluster_config import (
    get_ingress_postfix,
    get_keycloak_discovery_url,
    get_keycloak_support_http,
    get_namespace,
)
from opi.core.config import settings
from opi.core.startup import keycloak_operation_with_retry
from opi.handlers.keycloak_yaml_handler import KeycloakYamlHandler
from opi.services import ServiceAdapter, ServiceType
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, get_domain_setting
from opi.services.project import Project
from opi.services.services import service_entry_config, service_entry_name, service_entry_type
from opi.utils.age import (
    decrypt_password_smart,
    encrypt_age_content,
    get_decoded_project_private_key,
    get_project_public_key,
)
from opi.utils.naming import (
    HostnameFormat,
    extract_domain_from_url,
    generate_external_hostname,
    generate_project_admin_username,
    generate_project_platform_client_id,
    generate_project_realm_name,
    get_deployment_hostnames,
    resolve_effective_base_domain,
)
from opi.utils.passwords import generate_secure_password
from opi.utils.secrets import KeycloakSecret
from opi.utils.totp import build_otpauth_uri, generate_totp_secret

if TYPE_CHECKING:
    from opi.manager.project_manager import ProjectManager

logger = logging.getLogger(__name__)


def build_project_realm_context(
    *,
    project_name: str,
    cluster: str,
    keycloak_url: str,
    realm_name: str,
    platform_client_id: str,
    operations_manager_domain: str,
    account_link: str | None,
) -> dict[str, Any]:
    """Base variables every project-realm template is rendered with.

    A project-realm template may only reference names this returns, plus the user's own
    ``variables:`` block. ``tests/test_keycloak_template_variables.py`` holds the two sides
    together.
    """
    return {
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
        # Operations manager domain and client ID for invite flow
        "operations_manager_domain": operations_manager_domain,
        "invite_client_id": settings.INVITE_CLIENT_ID,
        # Per-realm SSO account-linking mode (automatic | confirm | verify; None/verify -> stock)
        "account_link": account_link,
    }


class KeycloakManager:
    """Manager for Keycloak SSO operations and resources."""

    def __init__(self, project_manager: ProjectManager) -> None:
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

        # Check if any helmfiles in this deployment use keycloak service
        uses_keycloak_via_helmfile = self._deployment_uses_keycloak_via_helmfile(project_data, deployment_name)

        if not sso_components and not uses_keycloak_via_helm and not uses_keycloak_via_helmfile:
            logger.debug(
                f"Deployment {deployment_name} has no components, helm-charts, or helmfiles using Keycloak service, skipping"
            )
            return

        logger.info(f"Processing Keycloak SSO resources for project: {project_name}, deployment: {deployment_name}")
        if sso_components:
            logger.info(f"Found {len(sso_components)} components using SSO: {', '.join(sso_components)}")
        if uses_keycloak_via_helm:
            logger.info(f"Deployment {deployment_name} uses Keycloak via helm-charts")
        if uses_keycloak_via_helmfile:
            logger.info(f"Deployment {deployment_name} uses Keycloak via helmfile")

        # Extract and validate Keycloak configuration
        # This will raise ValueError/FileNotFoundError on invalid config
        keycloak_config = self._get_keycloak_service_config(project_data)

        progress_manager = self.project_manager.get_progress_manager()
        keycloak_task = None
        if progress_manager:
            keycloak_task = progress_manager.add_task("Creating Keycloak SSO resources")

        try:
            # Handle external keycloak (credentials from Kubernetes secret)
            if keycloak_config.get("type") == "external":
                logger.info(f"Using external keycloak for deployment {deployment_name}")
                await self._handle_external_keycloak(
                    project_name=project_name,
                    deployment_name=deployment_name,
                    cluster=cluster,
                    config=keycloak_config,
                )
                return

            # Collect all hostnames from all SSO components/helm-charts in this deployment
            ingress_postfix = get_ingress_postfix(cluster)
            subdomain = get_domain_setting(deployment, DomainSetting.SUBDOMAIN)
            base_domain = get_domain_setting(deployment, DomainSetting.BASE_DOMAIN)
            domain_mode = get_domain_setting(deployment, DomainSetting.DOMAIN_MODE)
            domain_format = get_domain_setting(deployment, DomainSetting.DOMAIN_FORMAT)
            expose_on_bare_domain = get_domain_setting(deployment, DomainSetting.BARE_DOMAIN_COMPONENT, False)

            if domain_mode == "nice-url":
                logger.info(
                    f"Using nice-url mode for deployment {deployment_name}: subdomain={subdomain}, base-domain={base_domain}"
                )
            elif subdomain:
                logger.info(f"Using subdomain mode for deployment {deployment_name}: subdomain={subdomain}")
            else:
                logger.info(f"Using component-specific mode for deployment {deployment_name}")

            # Filter components that should process SSO
            filtered_sso_components = []
            for component_name in sso_components:
                should_process = await self._should_process_sso_rijk(project_data, component_name)
                if should_process:
                    filtered_sso_components.append(component_name)
                else:
                    logger.info(f"Skipping SSO setup for component {component_name} (not configured for SSO-Rijk)")

            # Collect all hostnames using centralized function
            all_ingress_hosts = get_deployment_hostnames(
                component_names=filtered_sso_components,
                deployment_name=deployment_name,
                project_name=project_name,
                ingress_postfix=ingress_postfix,
                subdomain=subdomain,
                base_domain=base_domain,
                hostname_format=HostnameFormat.from_domain_mode(domain_mode),
                domain_format=domain_format,
                expose_on_bare_domain=expose_on_bare_domain,
                project_data=project_data,
                cluster=settings.CLUSTER_MANAGER,
            )
            if all_ingress_hosts:
                logger.info(f"Generated hostnames for components: {all_ingress_hosts}")

            # Process helm-chart-based SSO
            if uses_keycloak_via_helm:
                if not subdomain:
                    raise ValueError(
                        f"Helm-chart deployment {deployment_name} uses Keycloak but is missing required 'subdomain' field"
                    )
                effective_base_domain = resolve_effective_base_domain(base_domain, ingress_postfix)
                helm_hostname = generate_external_hostname(subdomain, effective_base_domain)
                if helm_hostname not in all_ingress_hosts:
                    all_ingress_hosts.append(helm_hostname)
                    logger.info(f"Added hostname for helm-chart deployment: {helm_hostname}")

            # Process helmfile-based SSO
            if uses_keycloak_via_helmfile:
                if not subdomain:
                    raise ValueError(
                        f"Helmfile deployment {deployment_name} uses Keycloak but is missing required 'subdomain' field"
                    )
                effective_base_domain = resolve_effective_base_domain(base_domain, ingress_postfix)
                helmfile_hostname = generate_external_hostname(subdomain, effective_base_domain)
                if helmfile_hostname not in all_ingress_hosts:
                    all_ingress_hosts.append(helmfile_hostname)
                    logger.info(f"Added hostname for helmfile deployment: {helmfile_hostname}")

            if not all_ingress_hosts:
                logger.info(
                    f"No SSO-enabled components, helm-charts, or helmfiles found in deployment {deployment_name}, skipping"
                )
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
                    public_client_id=keycloak_credentials.get("public_client_id", ""),
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
                  type: external  # Optional: "external" to use credentials from another project
                  config:
                    template: "sso-only"  # or "algoritmeregister", etc.
                    variables:  # Optional template-specific variables
                      frontend_redirect_uris: "https://..."
                      realm_display_name: "Custom Name"
                    additional_redirect_uris:  # Optional additional redirect URIs for development
                      - "http://localhost:8080/*"
                      - "http://127.0.0.1:8080/*"
                    additional-clients:  # Optional: create clients for other projects
                      - name: other-project-client
                        redirect-uris:
                          - https://other.example.com/*
                    realm-roles:  # Optional: create realm-level roles
                      - name: mijnbureau-user
                        description: Access to MijnBureau applications

        Args:
            project_data: The project configuration data

        Returns:
            Dictionary with merged configuration:
            {
                "type": None,  # None for normal, "external" for external keycloak
                "template": "sso-only",  # Template filename (without .yaml)
                "variables": {...},       # Template-specific variables
                "additional_redirect_uris": [...]  # Optional additional redirect URIs
                "additional_clients": [...]  # Optional additional clients to create
                "realm_roles": [...]  # Optional realm roles to create
                "restrict_access": {       # Optional access restriction config (YAML: restrict-access)
                    "enabled": True,
                    "role": "allowed-user",  # Client role
                    "realm_role": "allowed-user",  # Or realm role (takes precedence, YAML: realm-role)
                    "error_message": "${accessDeniedNoPermission}"  # YAML: error-message
                }
            }

        Raises:
            ValueError: If configuration format is invalid or contains path traversal
            FileNotFoundError: If specified template file doesn't exist
        """
        # Default configuration
        DEFAULT_CONFIG: dict[str, Any] = {
            "type": None,
            "template": "sso-only",
            "variables": {},
            "additional_redirect_uris": [],
            "additional_clients": [],
            "realm_roles": [],
            "restrict_access": None,
            "account_link": None,  # None = keep Keycloak's stock first-broker-login flow (opt-in)
        }

        project_services = project_data.get("services", [])
        if not project_services:
            logger.debug("No services defined, using default Keycloak config")
            return DEFAULT_CONFIG.copy()

        # Find keycloak service config. Read format-agnostically: an entry is a bare
        # string, the legacy single-key dict ({keycloak: {...}}), or the uniform record
        # ({name: keycloak, config: {...}}) that the wizard writes today. The previous
        # `"keycloak" in service_item` test only matched the legacy form -- a record has
        # the keys `name` and `config` -- so every project in the current format fell
        # back to DEFAULT_CONFIG. restrict-access therefore did nothing at all: no realm
        # role created, no restriction applied, and no error to show for it.
        user_config = None
        keycloak_type = None
        for service_item in project_services:
            if service_entry_name(service_item) != ServiceType.KEYCLOAK.value:
                continue
            if isinstance(service_item, dict):
                body = service_item.get(ServiceType.KEYCLOAK.value) if "name" not in service_item else service_item
                if body is not None and not isinstance(body, dict):
                    raise ValueError(
                        f"Invalid keycloak service format. Expected dict with 'config' key, got {type(body).__name__}"
                    )
            keycloak_type = service_entry_type(service_item)
            user_config = service_entry_config(service_item)
            break

        # If no config specified, use defaults
        if user_config is None:
            logger.debug("No Keycloak config specified, using default template 'sso-only'")
            result = DEFAULT_CONFIG.copy()
            result["type"] = keycloak_type
            return result

        # Validate config is a dict
        if not isinstance(user_config, dict):
            raise TypeError(f"Keycloak config must be a dict, got {type(user_config).__name__}")

        # Merge with defaults
        merged_config = DEFAULT_CONFIG.copy()

        # Set the keycloak type (external or None for normal)
        merged_config["type"] = keycloak_type
        if keycloak_type == "external":
            logger.info("Using external keycloak provider (credentials from Kubernetes secret)")

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

        # Extract and validate additional_redirect_uris. Beide schrijfwijzen, net als het
        # configmodel: een alias die alleen valideert maar hier niet gelezen wordt, maakt
        # de koppeltekenvorm een stille no-op -- erger dan geen alias.
        redirect_uris_key = next(
            (key for key in ("additional_redirect_uris", "additional-redirect-uris") if key in user_config), None
        )
        if redirect_uris_key is not None:
            additional_uris = user_config[redirect_uris_key]
            if not isinstance(additional_uris, list):
                raise ValueError(f"additional_redirect_uris must be a list, got {type(additional_uris).__name__}")
            # Validate all entries are strings
            for uri in additional_uris:
                if not isinstance(uri, str):
                    raise TypeError(f"All additional_redirect_uris must be strings, got {type(uri).__name__}: {uri}")
            merged_config["additional_redirect_uris"] = additional_uris
            logger.info(f"Found {len(additional_uris)} additional redirect URIs in config")

        # Extract and validate restrict-access
        if "restrict-access" in user_config:
            restrict_access = user_config["restrict-access"]
            if not isinstance(restrict_access, dict):
                raise ValueError(f"restrict-access must be a dict, got {type(restrict_access).__name__}")

            # Validate required fields if enabled
            if restrict_access.get("enabled", False):
                # Either role (client role) or realm-role must be specified
                has_role = "role" in restrict_access
                has_realm_role = "realm-role" in restrict_access
                if not has_role and not has_realm_role:
                    raise ValueError(
                        "restrict-access.role or restrict-access.realm-role is required "
                        "when restrict-access.enabled is True"
                    )
                if has_role and not isinstance(restrict_access["role"], str):
                    raise ValueError(
                        f"restrict-access.role must be a string, got {type(restrict_access['role']).__name__}"
                    )
                if has_realm_role and not isinstance(restrict_access["realm-role"], str):
                    raise ValueError(
                        f"restrict-access.realm-role must be a string, "
                        f"got {type(restrict_access['realm-role']).__name__}"
                    )

            merged_config["restrict_access"] = {
                "enabled": restrict_access.get("enabled", False),
                "role": restrict_access.get("role"),  # Client role (may be None if realm-role is used)
                "realm_role": restrict_access.get("realm-role"),  # Realm role (takes precedence)
                "error_message": restrict_access.get("error-message", "${accessDeniedNoPermission}"),
            }
            if merged_config["restrict_access"]["realm_role"]:
                logger.info(
                    f"Access restriction configured: enabled={merged_config['restrict_access']['enabled']}, "
                    f"realm_role={merged_config['restrict_access']['realm_role']}"
                )
            else:
                logger.info(
                    f"Access restriction configured: enabled={merged_config['restrict_access']['enabled']}, "
                    f"role={merged_config['restrict_access']['role']}"
                )

        # Extract and validate additional-clients
        if "additional-clients" in user_config:
            additional_clients = user_config["additional-clients"]
            if not isinstance(additional_clients, list):
                raise ValueError(f"additional-clients must be a list, got {type(additional_clients).__name__}")
            for i, client_config in enumerate(additional_clients):
                if not isinstance(client_config, dict):
                    raise TypeError(f"additional-clients[{i}] must be a dict, got {type(client_config).__name__}")
                if "name" not in client_config:
                    raise ValueError(f"additional-clients[{i}].name is required")
            merged_config["additional_clients"] = additional_clients
            logger.info(f"Found {len(additional_clients)} additional clients to create")

        # Extract and validate realm-roles
        if "realm-roles" in user_config:
            realm_roles = user_config["realm-roles"]
            if not isinstance(realm_roles, list):
                raise ValueError(f"realm-roles must be a list, got {type(realm_roles).__name__}")
            for i, role_config in enumerate(realm_roles):
                if not isinstance(role_config, dict):
                    raise TypeError(f"realm-roles[{i}] must be a dict, got {type(role_config).__name__}")
                if "name" not in role_config:
                    raise ValueError(f"realm-roles[{i}].name is required")
            merged_config["realm_roles"] = realm_roles
            logger.info(f"Found {len(realm_roles)} realm roles to create")

        # Extract and validate account-link (per-realm SSO account-linking mode):
        #   automatic -> link a brokered SSO identity to a pre-existing account silently
        #   confirm   -> same, after one confirmation screen
        #   verify    -> Keycloak's stock flow (prove ownership by email/password); the default
        #                when account-link is omitted
        if "account-link" in user_config:
            account_link = user_config["account-link"]
            if account_link not in ("automatic", "confirm", "verify"):
                raise ValueError(f"account-link must be 'automatic', 'confirm' or 'verify', got {account_link!r}")
            merged_config["account_link"] = account_link
            logger.info(f"Account-link mode configured: {account_link}")

        # For external keycloak, copy the external config fields and skip template validation
        if merged_config["type"] == "external":
            # Copy external keycloak config fields (host, realm, client-id, client-secret)
            # These are passed directly from the YAML config section
            for key in ["host", "realm", "client-id", "client-secret"]:
                if key in user_config:
                    merged_config[key] = user_config[key]
            logger.info("External keycloak - skipping template validation")
            return merged_config

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

    async def handle_service_removal(
        self,
        project_name: str,
        deployment_name: str,
        deployment_data: dict[str, Any],
        project_data: dict[str, Any],
        marked_for_deletion_service: Any = None,
    ) -> dict[str, Any]:
        """Handle cleanup when Keycloak service is removed from a deployment.

        Keycloak resources (clients, redirect URIs) are ephemeral and always
        deleted immediately.  The ``marked_for_deletion_service`` parameter is
        accepted for interface consistency but is ignored.

        Args:
            project_name: Name of the project.
            deployment_name: Name of the deployment losing the service.
            deployment_data: The deployment dict from the *previous* YAML.
            project_data: The *previous* project YAML (so internal service
                checks still pass).
            marked_for_deletion_service: Ignored (Keycloak is always immediate).

        Returns:
            Structured result dict with operations, errors, success.
        """
        result = await self.delete_resources_for_deployment(project_data, deployment_data)
        result["trigger"] = "service_removal"
        return result

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
            component = next(
                (c for c in project_data.get("components", []) if c.get("name") == component_ref),
                None,
            )
            if not component:
                continue
            all_services = ServiceAdapter.extract_service_names_from_project_services(component.get("services", []))

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
                    # Check services list for Keycloak
                    service_names = ServiceAdapter.extract_service_names_from_project_services(
                        component.get("services", [])
                    )
                    component_services = ServiceAdapter.parse_services_from_strings(service_names)
                    has_keycloak_service = ServiceType.KEYCLOAK in component_services

                    if has_keycloak_service:
                        return True

            return False

        except Exception as e:
            logger.exception(f"Error checking Keycloak service for component {component_reference}: {e}")
            return False

    def _deployment_uses_keycloak_via_helm_charts(self, project_data: dict[str, Any], deployment_name: str) -> bool:
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

            # Check services in the helm-chart definition
            service_names = ServiceAdapter.extract_service_names_from_project_services(
                helm_chart_def.get("services", [])
            )
            chart_services = ServiceAdapter.parse_services_from_strings(service_names)
            if ServiceType.KEYCLOAK in chart_services:
                return True

        return False

    def _deployment_uses_keycloak_via_helmfile(self, project_data: dict[str, Any], deployment_name: str) -> bool:
        """
        Check if a deployment uses Keycloak service via helmfile.

        Args:
            project_data: The project configuration data
            deployment_name: Name of the deployment to check

        Returns:
            True if deployment uses Keycloak via helmfile, False otherwise
        """
        # Get helmfile references from the deployment
        helmfile_refs = self.project_manager._project_file_handler.extract_deployment_helmfiles(
            project_data, deployment_name
        )

        if not helmfile_refs:
            return False

        # Check each helmfile reference for keycloak service
        for helmfile_ref in helmfile_refs:
            helmfile_reference = helmfile_ref.get("reference")
            if not helmfile_reference:
                continue

            # Find the helmfile definition
            helmfile_def = self.project_manager._project_file_handler.get_helmfile_by_name(
                project_data, helmfile_reference
            )
            if not helmfile_def:
                continue

            # Check services in the helmfile definition
            service_names = ServiceAdapter.extract_service_names_from_project_services(helmfile_def.get("services", []))
            helmfile_services = ServiceAdapter.parse_services_from_strings(service_names)
            if ServiceType.KEYCLOAK in helmfile_services:
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

                    # Update project file if keycloak host has changed (e.g., domain migration)
                    if keycloak_host != keycloak_url:
                        logger.info(f"Updating project keycloak host from {keycloak_host} to {keycloak_url}")
                        keycloak_host = keycloak_url
                        kc_config["host"] = keycloak_url
                        project_data = await self.project_manager.get_contents()
                        await self.project_manager.save_and_commit_project(
                            project_data,
                            f"Update Keycloak host to {keycloak_url} for project {project_name} ({cluster})",
                            enforce_validation=False,
                        )

                    # Always ensure authentication flow is correctly configured (idempotent)
                    await self._ensure_realm_authentication_flow(realm_name, keycloak_url, config)
                    # Always ensure IdP and platform client have correct URLs (idempotent)
                    await self._ensure_idp_and_platform_client_configuration(project_name, cluster, keycloak_url)
                    # Always reconcile identity providers from YAML template (idempotent diff-based)
                    await self._ensure_realm_identity_providers(project_name, cluster, realm_name, keycloak_url, config)
                    # Always reconcile identity self-service restrictions (idempotent)
                    await self._ensure_realm_self_service(project_name, cluster, realm_name, keycloak_url, config)
                    # Always ensure clients from YAML template are created (idempotent)
                    await self._ensure_realm_clients(
                        project_name, cluster, realm_name, keycloak_url, config, ingress_hosts
                    )
                    # Ensure realm roles exist (idempotent)
                    realm_roles = config.get("realm_roles", [])
                    if realm_roles:
                        await self._ensure_realm_roles(realm_name, keycloak_url, realm_roles)
                    # Create additional clients for other projects (idempotent)
                    additional_clients = config.get("additional_clients", [])
                    if additional_clients:
                        await self._create_additional_clients(realm_name, keycloak_url, additional_clients, cluster)
                    # Ensure the realm admin has the shared OTP credential (idempotent retrofit)
                    await self._ensure_admin_otp(project_name, cluster, realm_name, keycloak_url)
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
                        keycloak_url=keycloak_url,
                        admin_username=settings.KEYCLOAK_ADMIN_USERNAME,
                        admin_password=settings.KEYCLOAK_ADMIN_PASSWORD,
                    )
                    await self._apply_access_restriction(
                        keycloak=keycloak,
                        realm_name=realm_name,
                        client_id=existing_credentials.client_id,
                        restrict_access=restrict_access,
                    )

                # Ensure base_url and discovery_url use the current keycloak URL
                cluster_discovery_url = get_keycloak_discovery_url(cluster)
                expected_discovery_url = f"{cluster_discovery_url}/realms/{realm_name}/.well-known/openid-configuration"
                base_url = existing_credentials.base_url
                discovery_url = existing_credentials.discovery_url

                if base_url != keycloak_url or discovery_url != expected_discovery_url:
                    logger.info(
                        f"Updating stale Keycloak credentials for {deployment_name}: "
                        f"base_url {base_url} -> {keycloak_url}"
                    )
                    base_url = keycloak_url
                    discovery_url = expected_discovery_url

                    # Update the stored secret so the new URLs get written
                    updated_secret = KeycloakSecret(
                        client_id=existing_credentials.client_id,
                        client_secret=existing_credentials.client_secret,
                        public_client_id=existing_credentials.public_client_id,
                        discovery_url=discovery_url,
                        base_url=base_url,
                        realm=realm_name,
                    )
                    self.project_manager._add_secret_to_create(deployment_name, "keycloak", updated_secret)

                return {
                    "client_id": existing_credentials.client_id,
                    "client_secret": existing_credentials.client_secret,
                    "discovery_url": discovery_url,
                    "base_url": base_url,
                    "realm": existing_credentials.realm,
                }

            # No existing credentials, create new client
            logger.info(
                f"Creating new Keycloak client for deployment {project_name}/{deployment_name} "
                f"with {len(ingress_hosts)} redirect URIs"
            )

            keycloak = await create_keycloak_connector(
                keycloak_url=keycloak_url,
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
                additional_redirect_uris=additional_redirect_uris or None,
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
                "public_client_id": client_info.get("public_client_id", ""),
                "discovery_url": realm_discovery_url,
                "base_url": keycloak_url,
                "realm": realm_name,
            }

            # Store credentials in secrets map
            keycloak_secret = KeycloakSecret(
                client_id=client_info["client_id"],
                client_secret=client_info["client_secret"],
                public_client_id=client_info.get("public_client_id", ""),
                discovery_url=realm_discovery_url,
                base_url=keycloak_url,
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
        Apply access restriction to a client using roles and conditional authentication flow.

        Supports both client roles (role) and realm roles (realm_role).
        Realm roles take precedence if specified - this enables unified access control
        across multiple applications sharing the same realm.

        This creates:
        1. A role (client or realm) that users need to access the application
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
                - role: str (client role name) - used if realm_role not specified
                - realm_role: str (realm role name) - takes precedence over role
                - error_message: str (theme message key)
        """
        error_message = restrict_access.get("error_message", "${accessDeniedNoPermission}")
        browser_flow_alias = f"browser-restricted-{client_id}"
        post_broker_flow_alias = f"post-broker-restricted-{client_id}"

        # Determine if using realm role or client role
        realm_role = restrict_access.get("realm_role")
        client_role = restrict_access.get("role")
        use_realm_role = realm_role is not None

        if use_realm_role:
            role_name = realm_role
            logger.info(f"Applying access restriction to client '{client_id}' using REALM role '{role_name}'")
        else:
            role_name = client_role or "allowed-user"
            logger.info(f"Applying access restriction to client '{client_id}' using CLIENT role '{role_name}'")

        logger.info(f"  - Role type: {'realm' if use_realm_role else 'client'}")
        logger.info(f"  - Role name: {role_name}")
        logger.info(f"  - Error message: {error_message}")
        logger.info(f"  - Browser flow alias: {browser_flow_alias}")
        logger.info(f"  - Post-broker flow alias: {post_broker_flow_alias}")

        try:
            if use_realm_role:
                # For realm roles, ensure the role exists (it should have been created earlier)
                logger.info(f"Ensuring realm role '{role_name}' exists")
                await keycloak.create_realm_role(
                    realm_name=realm_name,
                    role_name=role_name,
                    description=f"Realm role for access control: {role_name}",
                )

                # Create the restricted browser flow for realm role (for direct logins)
                logger.info(f"Creating restricted browser flow '{browser_flow_alias}' for realm role")
                await keycloak.create_restricted_browser_flow_realm_role(
                    realm_name=realm_name,
                    flow_alias=browser_flow_alias,
                    role_name=role_name,
                    error_message=error_message,
                )
            else:
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
                use_realm_role=use_realm_role,
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
        use_realm_role: bool = False,
    ) -> None:
        """
        Apply post-broker login restriction to all identity providers in the realm.

        This ensures that users authenticating via SSO/IdP are also checked for the required role.

        Args:
            keycloak: KeycloakConnector instance
            realm_name: Name of the realm
            client_id: Client ID for the role check (used for client roles)
            role_name: Role name that grants access (client or realm role)
            error_message: Error message key from theme
            post_broker_flow_alias: Alias for the post-broker login flow
            use_realm_role: If True, use realm role instead of client role
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
        # Always skip the invite client so users can complete invites without existing roles
        if use_realm_role:
            logger.info(f"Creating post-broker login flow for realm role '{role_name}'")
            await keycloak.create_post_broker_login_flow_realm_role(
                realm_name=realm_name,
                flow_alias=post_broker_flow_alias,
                role_name=role_name,
                error_message=error_message,
                skip_clients=[settings.INVITE_CLIENT_ID],
            )
        else:
            logger.info(f"Creating post-broker login flow for client role '{client_id}.{role_name}'")
            await keycloak.create_post_broker_login_flow(
                realm_name=realm_name,
                flow_alias=post_broker_flow_alias,
                client_id=client_id,
                role_name=role_name,
                error_message=error_message,
                skip_clients=[settings.INVITE_CLIENT_ID],
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

    async def _ensure_realm_authentication_flow(
        self,
        realm_name: str,
        keycloak_url: str,
        config: dict[str, Any],
    ) -> None:
        """
        Ensure the authentication flow is correctly configured for an existing realm.

        This is an idempotent operation that updates the authentication flow configuration
        based on the YAML template. It's called when the realm already exists to ensure:
        1. The browserFlow matches the template (browser vs External IDP Redirector)
        2. The SSO redirect flow uses the correct identity provider alias (for sso-only)

        Args:
            realm_name: Name of the realm to configure
            keycloak_url: Base URL of the Keycloak server
            config: Keycloak configuration dict with template and variables
        """
        template_name = config.get("template", "sso-only")
        yaml_path = Path(__file__).parent.parent / "configs" / "keycloak" / f"{template_name}.yaml"

        if not yaml_path.exists():
            logger.warning(f"Template {template_name} not found, skipping authentication flow update")
            return

        logger.info(f"Ensuring authentication flow configuration for realm {realm_name} using template {template_name}")

        # Create Keycloak connector
        keycloak = await create_keycloak_connector(
            keycloak_url=keycloak_url,
            admin_username=settings.KEYCLOAK_ADMIN_USERNAME,
            admin_password=settings.KEYCLOAK_ADMIN_PASSWORD,
        )

        # Build minimal context for authentication flow processing
        context = {
            "realm_name": realm_name,
            "project_realm_name": realm_name,
        }

        # Merge user-provided variables
        user_variables = config.get("variables", {})
        if isinstance(user_variables, dict):
            context.update(user_variables)

        # Process authentication flows (idempotent - updates if needed). This MUST run
        # before the browser flow is pointed at them: Keycloak rejects a browserFlow
        # naming a flow that does not exist yet, and answers with a bare
        # 500 {"errorMessage":"Failed to update realm"} that says nothing about the
        # cause. Switching an existing sso-support realm to sso-only did exactly that,
        # because "External IDP Redirector" is created here, by this call
        # (toets-hn7, 2026-08-05).
        handler = KeycloakYamlHandler(keycloak)
        await handler.ensure_authentication_flows(yaml_path, context)

        # Converge the browser flow on what the template implies. Both templates already
        # set it themselves (sso-only through setAsBrowserFlow, sso-support through its
        # browserFlow key), so this is normally a no-op; it stays as the explicit
        # assertion for a realm that drifted, and for a template carrying neither signal.
        # sso-only: External IDP Redirector flow (auto-redirect to IdP)
        # sso-support: standard browser flow (shows login form with SSO button)
        expected_browser_flow = "External IDP Redirector" if template_name == "sso-only" else "browser"
        await keycloak.ensure_browser_flow(realm_name, expected_browser_flow)

    async def _ensure_realm_clients(
        self,
        project_name: str,
        cluster: str,
        realm_name: str,
        keycloak_url: str,
        config: dict[str, Any],
        ingress_hosts: list[str] | None = None,
    ) -> None:
        """
        Ensure all clients from YAML template exist in the realm.

        This is an idempotent operation that creates any missing clients defined
        in the YAML template. Used during project refresh to ensure new clients
        (like the invite flow client) are created for existing realms.

        Args:
            project_name: Name of the project
            cluster: Name of the cluster
            realm_name: Name of the realm
            keycloak_url: Base URL of the Keycloak server
            config: Keycloak configuration dict with template and variables
            ingress_hosts: List of ingress hostnames for redirect URIs
        """
        template_name = config.get("template", "sso-only")
        yaml_path = Path(__file__).parent.parent / "configs" / "keycloak" / f"{template_name}.yaml"

        if not yaml_path.exists():
            logger.warning(f"Template {template_name} not found, skipping clients update")
            return

        logger.info(f"Ensuring clients for realm {realm_name} using template {template_name}")

        # Create Keycloak connector
        keycloak = await create_keycloak_connector(
            keycloak_url=keycloak_url,
            admin_username=settings.KEYCLOAK_ADMIN_USERNAME,
            admin_password=settings.KEYCLOAK_ADMIN_PASSWORD,
        )

        # Extract domain from OWN_DOMAIN (strip protocol if present)
        operations_manager_domain = extract_domain_from_url(settings.OWN_DOMAIN)

        # Build context for client template processing
        context = {
            "project_name": project_name,
            "cluster": cluster,
            "keycloak_url": keycloak_url,
            "realm_name": realm_name,
            "project_realm_name": realm_name,
            # Operations manager domain and client ID for invite flow
            "operations_manager_domain": operations_manager_domain,
            "invite_client_id": settings.INVITE_CLIENT_ID,
        }

        # Add redirect URIs from component ingress hosts if provided
        if ingress_hosts:
            support_http = get_keycloak_support_http(cluster)
            first_redirect_uri = f"http://{ingress_hosts[0]}/*" if support_http else f"https://{ingress_hosts[0]}/*"
            context["frontend_redirect_uris"] = first_redirect_uri
            logger.debug(f"Added frontend_redirect_uris to context: {first_redirect_uri}")

        # Merge user-provided variables
        user_variables = config.get("variables", {})
        if isinstance(user_variables, dict):
            context.update(user_variables)

        # Process clients (idempotent - skips existing clients)
        handler = KeycloakYamlHandler(keycloak)
        await handler.ensure_clients(yaml_path, context)

    async def _ensure_realm_identity_providers(
        self,
        project_name: str,
        cluster: str,
        realm_name: str,
        keycloak_url: str,
        config: dict[str, Any],
    ) -> None:
        """
        Ensure identity providers from the YAML template are reconciled for an existing realm.

        This runs platformClients + identityProviders sections through the YAML handler,
        which is idempotent: the federation client's existing secret is reused, and the
        IDP config is only written when a real diff exists (e.g. a new flag like
        backchannelSupported landed in the template).
        """
        template_name = config.get("template", "sso-only")
        yaml_path = Path(__file__).parent.parent / "configs" / "keycloak" / f"{template_name}.yaml"

        if not yaml_path.exists():
            logger.warning(f"Template {template_name} not found, skipping identity provider reconciliation")
            return

        logger.info(f"Ensuring identity providers for realm {realm_name} using template {template_name}")

        keycloak = await create_keycloak_connector(
            keycloak_url=keycloak_url,
            admin_username=settings.KEYCLOAK_ADMIN_USERNAME,
            admin_password=settings.KEYCLOAK_ADMIN_PASSWORD,
        )

        platform_client_id = generate_project_platform_client_id(project_name, cluster)

        context = {
            "project_name": project_name,
            "cluster": cluster,
            "keycloak_url": keycloak_url,
            "platform_realm_name": settings.KEYCLOAK_DEFAULT_REALM,
            "project_realm_name": realm_name,
            "project_display_name": f"{project_name} ({cluster})",
            "platform_client_id": platform_client_id,
            "realm_name": realm_name,
            "realm_display_name": f"{project_name} ({cluster})",
            "account_link": config.get("account_link"),  # None = stock flow (opt-in)
        }

        user_variables = config.get("variables", {})
        if isinstance(user_variables, dict):
            context.update(user_variables)

        handler = KeycloakYamlHandler(keycloak)
        await handler.ensure_identity_providers(yaml_path, context)

    async def _ensure_realm_self_service(
        self,
        project_name: str,
        cluster: str,
        realm_name: str,
        keycloak_url: str,
        config: dict[str, Any],
    ) -> None:
        """
        Ensure the identity self-service restrictions from the YAML template are applied.

        Runs on every reconcile, not just on realm creation: create_realm() is skipped once a
        realm exists, so restrictions added to a template later would otherwise only ever reach
        newly created realms. That is the gap that left every pre-existing realm without the
        identity-field lock.
        """
        template_name = config.get("template", "sso-only")
        yaml_path = Path(__file__).parent.parent / "configs" / "keycloak" / f"{template_name}.yaml"

        if not yaml_path.exists():
            logger.warning(f"Template {template_name} not found, skipping self-service reconciliation")
            return

        keycloak = await create_keycloak_connector(
            keycloak_url=keycloak_url,
            admin_username=settings.KEYCLOAK_ADMIN_USERNAME,
            admin_password=settings.KEYCLOAK_ADMIN_PASSWORD,
        )

        display_name = f"{project_name} ({cluster})"
        context = {
            "project_realm_name": realm_name,
            "project_display_name": display_name,
            "realm_name": realm_name,
            "realm_display_name": display_name,
        }

        user_variables = config.get("variables", {})
        if isinstance(user_variables, dict):
            context.update(user_variables)

        handler = KeycloakYamlHandler(keycloak)
        await handler.ensure_realm_self_service(yaml_path, context)

    async def _ensure_idp_and_platform_client_configuration(
        self,
        project_name: str,
        cluster: str,
        expected_keycloak_url: str,
    ) -> None:
        """
        Ensure IdP configuration and platform client have correct URLs.

        This is an idempotent operation that updates:
        1. The IdP 'rig-platform-oidc' in the project realm - all URLs should use expected_keycloak_url
        2. The platform client in rig-platform realm - redirect URI should use expected_keycloak_url

        This fixes issues where realms were created with http:// but should use https://.

        Args:
            project_name: Name of the project
            cluster: Name of the cluster
            expected_keycloak_url: The expected base URL (e.g., https://keycloak.kind)
        """
        realm_name = generate_project_realm_name(project_name, cluster)
        platform_client_id = generate_project_platform_client_id(project_name, cluster)
        platform_realm = settings.KEYCLOAK_DEFAULT_REALM
        idp_alias = "rig-platform-oidc"

        logger.info(
            f"Ensuring IdP and platform client configuration for {project_name}/{cluster} "
            f"with expected URL: {expected_keycloak_url}"
        )

        keycloak = await create_keycloak_connector(
            keycloak_url=expected_keycloak_url,
            admin_username=settings.KEYCLOAK_ADMIN_USERNAME,
            admin_password=settings.KEYCLOAK_ADMIN_PASSWORD,
        )

        # 1. Check and update IdP configuration in project realm
        idp_configs = await keycloak.get_identity_providers(realm_name)
        for idp in idp_configs:
            if idp.get("alias") == idp_alias:
                config = idp.get("config", {})
                needs_update = False
                updated_fields = []

                # Check all URL fields that should use keycloak_url
                url_fields = [
                    "userInfoUrl",
                    "tokenUrl",
                    "jwksUrl",
                    "authorizationUrl",
                    "logoutUrl",
                    "discoveryEndpoint",
                ]

                for field in url_fields:
                    current_url = config.get(field, "")
                    # Check if URL needs updating (wrong base URL and contains /realms/)
                    if current_url and not current_url.startswith(expected_keycloak_url) and "/realms/" in current_url:
                        # Extract the path part and rebuild with correct base URL
                        # e.g., http://keycloak.kind/realms/... -> https://keycloak.kind/realms/...
                        path_part = "/realms/" + current_url.split("/realms/", 1)[1]
                        new_url = f"{expected_keycloak_url}{path_part}"
                        config[field] = new_url
                        needs_update = True
                        updated_fields.append(field)

                if needs_update:
                    logger.info(
                        f"Updating IdP '{idp_alias}' in realm '{realm_name}' - fixing URLs for fields: {updated_fields}"
                    )
                    await keycloak.update_identity_provider(
                        realm_name=realm_name,
                        provider_alias=idp_alias,
                        config=config,
                    )
                    logger.info(f"Successfully updated IdP '{idp_alias}' URLs to use {expected_keycloak_url}")
                else:
                    logger.debug(f"IdP '{idp_alias}' URLs are already correct")
                break

        # 2. Check and update platform client redirect URI in rig-platform realm
        expected_redirect_uri = f"{expected_keycloak_url}/realms/{realm_name}/broker/{idp_alias}/endpoint/*"

        # Find the platform client in rig-platform realm
        client = await keycloak.find_client_by_client_id(platform_client_id, platform_realm)

        if client:
            current_redirect_uris = client.get("redirectUris", [])

            # Check if expected redirect URI is present
            if expected_redirect_uri not in current_redirect_uris:
                # Build new redirect URIs list - replace any old broker endpoint URIs
                new_redirect_uris = [uri for uri in current_redirect_uris if f"/broker/{idp_alias}/endpoint" not in uri]
                new_redirect_uris.append(expected_redirect_uri)

                logger.info(
                    f"Updating platform client '{platform_client_id}' redirect URIs in realm '{platform_realm}'"
                )

                # Update the client using admin API
                keycloak.admin.change_current_realm(platform_realm)
                try:
                    keycloak.admin.update_client(
                        client_id=client["id"],
                        payload={"redirectUris": new_redirect_uris},
                    )
                finally:
                    keycloak.admin.change_current_realm("master")

                logger.info(f"Successfully updated platform client redirect URI to: {expected_redirect_uri}")
            else:
                logger.debug(f"Platform client '{platform_client_id}' redirect URIs are already correct")
        else:
            logger.warning(f"Platform client '{platform_client_id}' not found in realm '{platform_realm}'")

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

        # Create Keycloak connector first so we can verify the admin user does
        # not already exist before generating a new password.
        keycloak = await create_keycloak_connector(
            keycloak_url=keycloak_url,
            admin_username=settings.KEYCLOAK_ADMIN_USERNAME,
            admin_password=settings.KEYCLOAK_ADMIN_PASSWORD,
        )

        # Guard against silent drift: if the admin user already exists in master,
        # the create_user call later would 409 and silently keep the existing
        # credential, while we would still write a freshly generated password to
        # the project YAML. Refuse to proceed so YAML and Keycloak cannot diverge.
        existing_admin = await keycloak.get_user_by_username("master", admin_username)
        if existing_admin is not None:
            raise RuntimeError(
                f"Refusing to re-create project Keycloak realm for {project_name}/{cluster}: "
                f"admin user '{admin_username}' already exists in master realm. "
                f"Either a previous run failed after creating this user but before its "
                f"generated password was persisted to the project file (the old password "
                f"is then unrecoverable: verify no project file references this user, "
                f"delete it from the master realm, and re-run), or a transient Keycloak "
                f"error caused realm_exists() to return False for a healthy realm "
                f"(retry once Keycloak is healthy)."
            )

        # Generate and encrypt password
        admin_password = generate_secure_password()
        project_data = await self.project_manager.get_contents()
        project_public_key = get_project_public_key(project_data)

        if not project_public_key:
            raise Exception(f"Project public key not found for {project_name}")

        encrypted_password = await encrypt_age_content(admin_password, project_public_key)
        encrypted_password_str = LiteralScalarString(encrypted_password)

        # Generate and encrypt a shared TOTP secret (only when OTP is enabled).
        # Provisioning it as an OTP credential makes Keycloak's conditional-OTP
        # browser step require it at login. The seed is shared (stored in the
        # project file) so every project admin can load it and shared realm
        # access keeps working.
        totp_secret = generate_totp_secret() if settings.KEYCLOAK_ENFORCE_ADMIN_OTP else None
        encrypted_totp_str = (
            LiteralScalarString(await encrypt_age_content(totp_secret, project_public_key)) if totp_secret else None
        )

        # Extract template from config (already validated in _get_keycloak_service_config)
        template_name = config["template"]  # Will KeyError if config malformed
        yaml_path = Path(__file__).parent.parent / "configs" / "keycloak" / f"{template_name}.yaml"

        # Double-check template exists (defensive programming)
        if not yaml_path.exists():
            raise FileNotFoundError(f"Keycloak template not found: {yaml_path}")

        logger.info(f"Loading Keycloak template: {template_name}.yaml for realm {realm_name}")

        # Get cluster-specific HTTP support setting (used for URL protocol)
        support_http = get_keycloak_support_http(cluster)

        # Extract domain from OWN_DOMAIN (strip protocol if present)
        operations_manager_domain = extract_domain_from_url(settings.OWN_DOMAIN)

        # Build base context for YAML template
        context = build_project_realm_context(
            project_name=project_name,
            cluster=cluster,
            keycloak_url=keycloak_url,
            realm_name=realm_name,
            platform_client_id=platform_client_id,
            operations_manager_domain=operations_manager_domain,
            account_link=config.get("account_link"),
        )

        # Add redirect URIs from component ingress hosts if provided
        if ingress_hosts:
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
            raise TypeError(f"Template variables must be a dict, got {type(user_variables).__name__}")

        context.update(user_variables)

        logger.debug(f"Template context variables: {list(context.keys())}")

        # Execute YAML configuration for project realm
        handler = KeycloakYamlHandler(keycloak)
        await handler.execute_config(yaml_path, context)
        logger.info(f"Executed YAML configuration ({template_name}) for realm {realm_name}")

        # Create realm roles if specified
        realm_roles = config.get("realm_roles", [])
        if realm_roles:
            logger.info(f"Creating {len(realm_roles)} realm roles in realm {realm_name}")
            await self._ensure_realm_roles(realm_name, keycloak_url, realm_roles)

        # Create additional clients for other projects if specified
        additional_clients = config.get("additional_clients", [])
        if additional_clients:
            logger.info(f"Creating {len(additional_clients)} additional clients in realm {realm_name}")
            await self._create_additional_clients(realm_name, keycloak_url, additional_clients, cluster)

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
            totp_secret=totp_secret,
        )
        logger.info(f"Created admin user {admin_username} in master realm")

        # Assign realm management permissions for the project realm
        # This grants full admin access to the specific realm via the {realm}-realm client in master
        await keycloak.assign_realm_admin_from_master(target_realm_name=realm_name, user_id=user_info["id"])
        logger.info(f"Assigned realm management permissions for {realm_name} to {admin_username}")

        # Store under the keycloak service config (RC-5 B: relocated from the old
        # project-level config.keycloak). Still keyed by realm.
        view = Project(project_data)
        realms = view.get("services/keycloak/config/realms") or []

        config_entry = {
            "host": keycloak_url,
            "realm": realm_name,
            "username": admin_username,
            "password": encrypted_password_str,
        }
        if encrypted_totp_str:
            config_entry["totp_secret"] = encrypted_totp_str

        existing_config = next((i for i, kc in enumerate(realms) if kc.get("realm") == realm_name), None)
        if existing_config is not None:
            realms[existing_config] = config_entry
            logger.info(f"Updated existing Keycloak config for realm {realm_name}")
        else:
            realms.append(config_entry)
            logger.info(f"Added new Keycloak config for realm {realm_name}")

        # set find-or-creates the keycloak service entry and preserves order.
        view.set("services/keycloak/config/realms", realms)

        # Persist immediately: the generated admin password exists nowhere else.
        # Waiting for the end-of-run commit means any later failure in the task
        # orphans the admin user in Keycloak with an unrecoverable password,
        # wedging every re-run on the duplicate-admin guard above.
        await self.project_manager.save_and_commit_project(
            project_data,
            f"Persist Keycloak realm credentials for {project_name} ({cluster})",
            enforce_validation=False,
        )
        logger.info(f"Stored and pushed Keycloak config in project file for cluster {cluster}")

        result = {
            "host": keycloak_url,
            "realm": realm_name,
            "username": admin_username,
            "password": admin_password,  # Return plain password for immediate use
        }
        if totp_secret:
            result["totp_secret"] = totp_secret  # Plain TOTP secret for immediate use
            result["totp_otpauth_uri"] = build_otpauth_uri(totp_secret, admin_username, realm_name)
        return result

    async def _ensure_admin_otp(
        self,
        project_name: str,
        cluster: str,
        realm_name: str,
        keycloak_url: str,
    ) -> None:
        """Idempotently ensure the realm admin user has the shared OTP credential.

        Realms provisioned before OTP support have an admin user without an OTP
        credential and no ``totp_secret`` in the project file. Keycloak 25 only
        imports OTP credentials at user-creation time, so this retrofits by
        deleting and recreating the admin user - reusing its existing password so
        it does not rotate - with the OTP credential, then re-assigning realm
        management roles.

        Runs at most once per realm: once ``totp_secret`` is stored, later
        deploys short-circuit. The seed becomes visible in the portal, and OTP is
        required at the admin's next login.

        Gated by KEYCLOAK_ENFORCE_ADMIN_OTP (off by default) so enabling OTP is a
        deliberate rollout rather than a side-effect of any reprocess.
        """
        if not settings.KEYCLOAK_ENFORCE_ADMIN_OTP:
            return

        project_data = await self.project_manager.get_contents()
        view = Project(project_data)
        realms = view.get("services/keycloak/config/realms") or []
        entry_index = next((i for i, e in enumerate(realms) if e.get("realm") == realm_name), None)
        if entry_index is None or realms[entry_index].get("totp_secret"):
            return
        kc_entry = realms[entry_index]

        admin_username = generate_project_admin_username(project_name, cluster)
        logger.info(f"Retrofitting shared OTP for realm admin {admin_username} ({realm_name})")

        keycloak = await create_keycloak_connector(keycloak_url=keycloak_url)

        # Reuse the existing admin password so the retrofit only ADDS a factor and
        # does not rotate the password. Decrypt with the project private key.
        project_private_key = await get_decoded_project_private_key(project_data)
        admin_password = await decrypt_password_smart(kc_entry["password"], project_private_key)

        # Delete (if present) and recreate the admin user with the OTP credential.
        totp_secret = generate_totp_secret()
        existing = await keycloak.get_user_by_username("master", admin_username)
        if existing is not None:
            await keycloak.delete_user_by_username("master", admin_username)

        user_info = await keycloak.create_user(
            realm_name="master",
            username=admin_username,
            password=admin_password,
            email=f"{admin_username}@local.invalid",
            first_name="Realm",
            last_name="Administrator",
            enabled=True,
            totp_secret=totp_secret,
        )
        await keycloak.assign_realm_admin_from_master(target_realm_name=realm_name, user_id=user_info["id"])

        # Persist the encrypted secret so this retrofit runs only once.
        project_public_key = get_project_public_key(project_data)
        kc_entry["totp_secret"] = LiteralScalarString(await encrypt_age_content(totp_secret, project_public_key))
        view.set("services/keycloak/config/realms", realms)
        await self.project_manager.save_and_commit_project(
            project_data,
            f"Store shared OTP secret for realm admin of {realm_name}",
            enforce_validation=False,
        )
        logger.info(
            f"Stored shared OTP secret for realm admin {admin_username}; OTP required at next login for {realm_name}"
        )

    async def _create_additional_clients(
        self,
        realm_name: str,
        keycloak_host: str,
        additional_clients: list[dict[str, Any]],
        cluster: str,
    ) -> dict[str, str]:
        """
        Create additional OIDC clients in the realm and store their secrets in Kubernetes.

        This is used when a project needs to create clients for other projects
        that will use this realm (shared realm pattern).

        Args:
            realm_name: Name of the realm to create clients in
            keycloak_host: Base URL of the Keycloak server
            additional_clients: List of client configurations with:
                - name: Client ID
                - redirect-uris: List of allowed redirect URIs
            cluster: Cluster name for determining protocol support

        Returns:
            Dictionary mapping client names to their generated secrets
        """
        client_secrets: dict[str, str] = {}

        keycloak = await create_keycloak_connector(
            keycloak_url=keycloak_host,
            admin_username=settings.KEYCLOAK_ADMIN_USERNAME,
            admin_password=settings.KEYCLOAK_ADMIN_PASSWORD,
        )

        for client_config in additional_clients:
            client_name = client_config.get("name")
            if not client_name:
                logger.warning("Additional client config missing 'name', skipping")
                continue

            redirect_uris = client_config.get("redirect-uris", ["*"])

            logger.info(f"Creating additional client '{client_name}' in realm '{realm_name}'")

            # Create the client
            result = await keycloak.create_oidc_client(
                realm_name=realm_name,
                client_id=client_name,
                client_name=client_name,
                redirect_uris=redirect_uris,
            )

            client_secret = result.get("secret", "")
            client_secrets[client_name] = client_secret

            # Store the secret in Kubernetes
            await self._store_additional_client_secret(
                client_name=client_name,
                client_id=client_name,
                client_secret=client_secret,
                realm=realm_name,
                host=keycloak_host,
                cluster=cluster,
            )

            if result.get("created", True):
                logger.info(f"Created additional client '{client_name}' in realm '{realm_name}'")
            else:
                logger.info(f"Additional client '{client_name}' already exists in realm '{realm_name}'")

        return client_secrets

    async def _store_additional_client_secret(
        self,
        client_name: str,
        client_id: str,
        client_secret: str,
        realm: str,
        host: str,
        cluster: str,
    ) -> None:
        """
        Store additional client credentials as a Kubernetes secret.

        The secret is stored with a normalized name and contains all
        necessary information for another project to use the client.

        Args:
            client_name: Original client name (used for secret naming)
            client_id: Client ID
            client_secret: Generated client secret
            realm: Realm name
            host: Keycloak base URL
            cluster: Cluster name
        """
        # Normalize the secret name
        secret_name = f"keycloak-client-{client_name.lower().replace('_', '-')}"
        discovery_url = f"{host}/realms/{realm}/.well-known/openid-configuration"

        # Get the operations namespace for this cluster
        # This namespace is shared infrastructure accessible to all projects
        namespace = get_namespace(cluster)

        logger.info(f"Storing additional client secret '{secret_name}' in namespace '{namespace}'")

        # Create the secret manifest
        import base64
        import json

        secret_data = {
            "client-id": base64.b64encode(client_id.encode()).decode(),
            "client-secret": base64.b64encode(client_secret.encode()).decode(),
            "realm": base64.b64encode(realm.encode()).decode(),
            "host": base64.b64encode(host.encode()).decode(),
            "discovery-url": base64.b64encode(discovery_url.encode()).decode(),
        }

        secret_manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": secret_name,
                "namespace": namespace,
                "labels": {
                    "app.kubernetes.io/managed-by": "operations-manager",
                    "app.kubernetes.io/component": "keycloak-client",
                },
            },
            "type": "Opaque",
            "data": secret_data,
        }

        # Apply the secret using kubectl
        from opi.connectors.kubectl import KubectlConnector

        kubectl = KubectlConnector()
        args = ["apply", "-f", "-"]
        manifest_json = json.dumps(secret_manifest)

        stdout, stderr, code = await kubectl.run_command(args, stdin_input=manifest_json)

        if code != 0:
            logger.error(f"Failed to create secret '{secret_name}': {stderr}")
            raise RuntimeError(f"Failed to store additional client secret: {stderr}")

        logger.info(f"Successfully stored additional client secret '{secret_name}'")

    async def _ensure_realm_roles(
        self,
        realm_name: str,
        keycloak_host: str,
        roles: list[dict[str, Any]],
    ) -> None:
        """
        Ensure realm roles exist in the specified realm.

        Args:
            realm_name: Name of the realm
            keycloak_host: Base URL of the Keycloak server
            roles: List of role configurations with:
                - name: Role name
                - description: Optional role description
        """
        keycloak = await create_keycloak_connector(
            keycloak_url=keycloak_host,
            admin_username=settings.KEYCLOAK_ADMIN_USERNAME,
            admin_password=settings.KEYCLOAK_ADMIN_PASSWORD,
        )

        for role_config in roles:
            role_name = role_config.get("name")
            if not role_name:
                logger.warning("Realm role config missing 'name', skipping")
                continue

            description = role_config.get("description", "")

            logger.info(f"Ensuring realm role '{role_name}' exists in realm '{realm_name}'")
            await keycloak.create_realm_role(
                realm_name=realm_name,
                role_name=role_name,
                description=description,
            )

    async def _get_external_keycloak_credentials(
        self,
        config: dict[str, Any],
        project_private_key: str,
    ) -> dict[str, Any]:
        """
        Get Keycloak credentials from external config values.

        The config should contain the same values that would be stored in a
        keycloak secret, allowing the deployment secret to be created directly
        from these values.

        Args:
            config: External keycloak configuration with inline credentials:
                - host: Keycloak base URL
                - realm: Realm name
                - client-id: OIDC client ID
                - client-secret: OIDC client secret (AGE encrypted with project's public key)
            project_private_key: The project's AGE private key for decryption

        Returns:
            Dictionary with keycloak credentials

        Raises:
            ValueError: If required fields are missing
        """
        # Validate required fields
        required_fields = ["host", "realm", "client-id", "client-secret"]
        for field in required_fields:
            if field not in config:
                raise ValueError(f"External keycloak config missing required field: '{field}'")

        host = config["host"]
        realm = config["realm"]
        client_id = config["client-id"]
        client_secret_raw = config["client-secret"]

        # Decrypt client secret using project's private key
        from opi.utils.age import decrypt_password_smart

        client_secret = await decrypt_password_smart(client_secret_raw, project_private_key)

        # Build discovery URL from host and realm
        discovery_url = f"{host}/realms/{realm}/.well-known/openid-configuration"

        logger.info(f"Using external keycloak credentials: host={host}, realm={realm}, client_id={client_id}")

        return {
            "client_id": client_id,
            "client_secret": client_secret,
            "realm": realm,
            "base_url": host,
            "discovery_url": discovery_url,
        }

    async def _handle_external_keycloak(
        self,
        project_name: str,
        deployment_name: str,
        cluster: str,
        config: dict[str, Any],
    ) -> None:
        """
        Handle external keycloak configuration by reading credentials from inline config.

        This is used when a project uses a keycloak realm managed by another project.
        Instead of creating a realm/client, we read the credentials from the project
        file config and store them in the deployment's keycloak secret.

        Args:
            project_name: Name of the project
            deployment_name: Name of the deployment
            cluster: Cluster name
            config: External keycloak configuration with inline credentials
        """
        logger.info(f"Handling external keycloak for project {project_name}, deployment {deployment_name}")

        # Get project's private key for decrypting the client-secret
        from opi.utils.age import get_decoded_project_private_key

        project_data = await self.project_manager.get_contents()
        project_private_key = await get_decoded_project_private_key(project_data)

        # Get credentials from the config (decrypt client-secret with project's key)
        credentials = await self._get_external_keycloak_credentials(config, project_private_key)

        # Create KeycloakSecret instance with the external credentials
        # External keycloak has no ZAD-managed public client
        keycloak_secret = KeycloakSecret(
            client_id=credentials["client_id"],
            client_secret=credentials["client_secret"],
            public_client_id="",
            discovery_url=credentials["discovery_url"],
            base_url=credentials["base_url"],
            realm=credentials["realm"],
        )

        # Store the secret for this deployment (same structure as normal keycloak)
        self.project_manager._add_secret_to_create(deployment_name, "keycloak", keycloak_secret)

        logger.info(
            f"External keycloak credentials stored for deployment {deployment_name} "
            f"(client_id: {credentials['client_id']}, realm: {credentials['realm']})"
        )
