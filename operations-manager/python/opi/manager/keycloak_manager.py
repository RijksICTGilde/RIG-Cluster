"""Keycloak service manager for handling SSO resources."""

import logging
from typing import Any

from jsonpath_ng.ext import parse as jsonpath_parse

from opi.connectors.keycloak import create_keycloak_connector
from opi.core.cluster_config import get_ingress_postfix, get_keycloak_discovery_url
from opi.core.config import settings
from opi.services import ServiceAdapter, ServiceType
from opi.utils.naming import generate_hostname
from opi.utils.secrets import KeycloakSecret

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

        Args:
            project_data: The project configuration data
            deployment: The specific deployment configuration
        """
        project_name = project_data["name"]
        deployment_name = deployment["name"]
        cluster = deployment["cluster"]

        # Check if any components in this deployment use SSO service
        sso_components = await self._get_sso_components_for_deployment(project_data, deployment_name)
        if not sso_components:
            logger.debug(f"Deployment {deployment_name} has no components using SSO service, skipping")
            return

        logger.info(f"Processing Keycloak SSO resources for project: {project_name}, deployment: {deployment_name}")
        logger.info(f"Found {len(sso_components)} components using SSO: {', '.join(sso_components)}")

        progress_manager = self.project_manager.get_progress_manager()
        keycloak_task = None
        if progress_manager:
            keycloak_task = progress_manager.add_task("Creating Keycloak SSO resources")

        try:
            # Collect all hostnames from all SSO components in this deployment
            all_ingress_hosts = []
            ingress_postfix = get_ingress_postfix(cluster)

            for component_name in sso_components:
                # Check if we should process SSO for this component
                should_process = await self._should_process_sso_rijk(project_data, component_name)
                if not should_process:
                    logger.info(f"Skipping SSO setup for component {component_name} (not configured for SSO-Rijk)")
                    continue

                # Get hostname for this component
                hostname = generate_hostname(component_name, deployment_name, project_name, ingress_postfix)
                all_ingress_hosts.append(hostname)
                logger.debug(f"Added hostname for component {component_name}: {hostname}")

            if not all_ingress_hosts:
                logger.info(f"No SSO-enabled components found in deployment {deployment_name}, skipping")
                return

            logger.info(f"Creating Keycloak client for deployment {deployment_name} with {len(all_ingress_hosts)} redirect URIs")

            # Create ONE Keycloak client for the entire deployment with all redirect URIs
            keycloak_credentials = await self._setup_sso_rijk_integration(
                project_name=project_name,
                deployment_name=deployment_name,
                ingress_hosts=all_ingress_hosts,  # All hostnames from all components
                cluster=cluster,
            )

            if keycloak_credentials:
                # Convert dictionary to KeycloakSecret instance for type safety
                keycloak_secret = KeycloakSecret(
                    client_id=keycloak_credentials["client_id"],
                    client_secret=keycloak_credentials["client_secret"],
                    discovery_url=keycloak_credentials.get("discovery_url", ""),
                )

                # Store ONE Keycloak secret for the deployment (not per component)
                self.project_manager._add_secret_to_create(deployment_name, "keycloak", keycloak_secret)
                logger.info(
                    f"Keycloak credentials stored for deployment {deployment_name} with {len(all_ingress_hosts)} redirect URIs"
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
        project_name = project_data["name"]
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
        kc_config = self.project_manager._get_project_keycloak_config_for_cluster(project_data, cluster)

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

                    # Import retry logic from startup module
                    from opi.core.startup import keycloak_operation_with_retry

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

    async def _get_sso_components_for_deployment(self, project_data: dict[str, Any], deployment_name: str) -> list[str]:
        """
        Get list of components in a deployment that use SSO service.

        Args:
            project_data: The project configuration data
            deployment_name: Name of the deployment to check

        Returns:
            List of component names that use SSO service
        """
        sso_components = []

        # First get component references for this deployment
        component_refs_query = jsonpath_parse(f"$.deployments[?@.name=='{deployment_name}'].components[*].reference")
        component_refs = [match.value for match in component_refs_query.find(project_data)]

        # Then check if any of these components use SSO service
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

            if ServiceType.SSO_RIJK.value in all_services:
                sso_components.append(component_ref)

        return sso_components

    async def _should_process_sso_rijk(self, project_data: dict[str, Any], component_reference: str) -> bool:
        """
        Check if a component has the sso-rijk option enabled.

        Args:
            project_data: The project configuration data
            component_reference: The component reference name

        Returns:
            True if sso-rijk should be processed, False otherwise
        """
        try:
            components = project_data.get("components", [])
            for component in components:
                if component.get("name") == component_reference:
                    # Check uses-services array for SSO
                    uses_services = component.get("uses-services", [])
                    component_services = ServiceAdapter.parse_services_from_strings(uses_services)
                    has_sso_service = ServiceType.SSO_RIJK in component_services

                    if has_sso_service:
                        return True

            return False

        except Exception as e:
            logger.exception(f"Error checking sso-rijk option for component {component_reference}: {e}")
            return False

    # Hostname calculation moved to centralized naming.py

    async def _setup_sso_rijk_integration(
        self,
        project_name: str,
        deployment_name: str,
        ingress_hosts: list[str],
        cluster: str,
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

        Returns:
            Dictionary with Keycloak credentials, or None if failed
        """
        try:
            # Get project data to find the realm for this cluster
            project_data = await self.project_manager.get_contents()

            # Get project realm config for this cluster
            kc_config = self.project_manager._get_project_keycloak_config_for_cluster(project_data, cluster)

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
                await self.project_manager._setup_project_keycloak_realm(project_name, cluster, keycloak_url)
                # Reload project data after realm creation
                project_data = await self.project_manager.get_contents()
                kc_config = self.project_manager._get_project_keycloak_config_for_cluster(project_data, cluster)

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
                return {
                    "client_id": existing_credentials.client_id,
                    "client_secret": existing_credentials.client_secret,
                    "discovery_url": existing_credentials.discovery_url,
                }

            # No existing credentials, create new client
            logger.info(f"Creating new Keycloak client for deployment {project_name}/{deployment_name} with {len(ingress_hosts)} redirect URIs")

            keycloak = await create_keycloak_connector(
                keycloak_url=keycloak_host,
                admin_username=settings.KEYCLOAK_ADMIN_USERNAME,
                admin_password=settings.KEYCLOAK_ADMIN_PASSWORD,
            )

            # Create client in project realm
            client_info = await keycloak.create_deployment_client(
                project_name=project_name,
                deployment_name=deployment_name,
                ingress_hosts=ingress_hosts,
                realm_name=realm_name,
            )

            # Get cluster-specific discovery URL for the project realm
            cluster_discovery_url = get_keycloak_discovery_url(cluster)
            realm_discovery_url = f"{cluster_discovery_url}/realms/{realm_name}/.well-known/openid-configuration"

            credentials = {
                "client_id": client_info["client_id"],
                "client_secret": client_info["client_secret"],
                "discovery_url": realm_discovery_url,
            }

            # Store credentials in secrets map
            keycloak_secret = KeycloakSecret(
                client_id=client_info["client_id"],
                client_secret=client_info["client_secret"],
                discovery_url=realm_discovery_url,
            )
            self.project_manager._add_secret_to_create(deployment_name, "keycloak", keycloak_secret)

            logger.info(f"Successfully created Keycloak client: {client_info['client_id']}")
            return credentials

        except Exception:
            logger.exception(f"Error setting up SSO-Rijk integration for {component_name}")
            raise

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
