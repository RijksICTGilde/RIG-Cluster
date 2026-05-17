"""
Keycloak bootstrap setup logic.

This module contains the business logic for setting up Keycloak during application startup.
It orchestrates the proper sequence of operations using YAML configuration.

The setup creates two realms:
1. rig-platform realm (from bootstrap YAML) - the platform realm with SSO configuration
2. operations-manager realm (from project file) - OPI's own realm, like any other project
"""

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from opi.connectors.keycloak import create_keycloak_connector
from opi.connectors.kubectl import KubectlConnector
from opi.core.cluster_config import get_namespace
from opi.core.config import settings
from opi.generation.manifests import ManifestGenerator
from opi.handlers.keycloak_yaml_handler import KeycloakYamlHandler
from opi.utils.naming import extract_domain_from_url

logger = logging.getLogger(__name__)

OPERATIONS_REALM_NAME = "operations-manager"


class KeycloakSetup:
    """Handles the complete Keycloak setup sequence for the operations manager."""

    def __init__(self):
        self.keycloak = None
        self.kubectl = None

    async def setup_all(self) -> bool:
        """
        Run the complete Keycloak setup sequence using YAML configuration.

        This creates:
        1. The rig-platform realm (from bootstrap YAML)
        2. The operations-manager realm (from project file, like any other project)
        3. A deployment client in the operations-manager realm

        Returns:
            True if all setup steps completed successfully
        """
        logger.info("Starting Keycloak setup")

        try:
            # Initialize connectors
            self.keycloak = await create_keycloak_connector()
            self.kubectl = KubectlConnector()

            # Build context from settings
            context = {
                "realm_name": settings.KEYCLOAK_DEFAULT_REALM,
                "realm_display_name": settings.KEYCLOAK_DEFAULT_REALM_DISPLAY_NAME,
                "keycloak_url": settings.KEYCLOAK_URL,
                "sso_client_id": settings.KEYCLOAK_MASTER_OIDC_CLIENT_ID,
                "sso_client_secret": settings.KEYCLOAK_MASTER_OIDC_CLIENT_SECRET,
                "sso_discovery_url": settings.KEYCLOAK_MASTER_OIDC_DISCOVERY_URL,
                # SAML SP Entity ID for direct SSO-Rijk connection
                "saml_sp_entity_id": f"{settings.KEYCLOAK_URL}/realms/{settings.KEYCLOAK_DEFAULT_REALM}",
            }

            # Select bootstrap configuration based on setting
            bootstrap_config = settings.KEYCLOAK_BOOTSTRAP_CONFIG
            if bootstrap_config == "local":
                yaml_filename = "bootstrap-local.yaml"
                logger.info("Using local bootstrap configuration (upstream IDP mode)")
            elif bootstrap_config == "sandbox":
                yaml_filename = "bootstrap-sandbox.yaml"
                logger.info("Using sandbox bootstrap configuration (upstream IDP mode)")
            else:
                yaml_filename = "bootstrap.yaml"
                logger.info("Using default bootstrap configuration (production mode)")

            # Step 1: Execute bootstrap (creates rig-platform realm)
            yaml_path = Path(__file__).parent.parent / "configs" / "keycloak" / yaml_filename
            handler = KeycloakYamlHandler(self.keycloak)
            await handler.execute_config(yaml_path, context)
            logger.info("Bootstrap realm setup completed")

            # Step 2: Create OPI's own realm (like any other project)
            await self.setup_operations_realm()

            # Step 3: Create deployment client in OPI realm and update K8s secret
            success = await self.setup_operations_client(realm_name=OPERATIONS_REALM_NAME)

            if success:
                logger.info("Keycloak setup completed successfully")
            else:
                logger.error("Failed to setup operations client")

            return success

        except Exception as e:
            logger.error(f"Keycloak setup failed with exception: {e}")
            return False

    async def setup_operations_realm(self) -> None:
        """Create OPI's own realm using project file config (like any other project).

        Selects the appropriate project file based on KEYCLOAK_BOOTSTRAP_CONFIG:
        - "local" or "sandbox": uses operations-manager-local.yaml (sso-support + admin user)
        - "default" (production): uses operations-manager.yaml (sso-only)
        """
        # Select project file based on bootstrap config
        if settings.KEYCLOAK_BOOTSTRAP_CONFIG in ("local", "sandbox"):
            project_filename = "operations-manager-local.yaml"
        else:
            project_filename = "operations-manager.yaml"

        project_file = Path(__file__).parent.parent / "configs" / "projects" / project_filename
        logger.info(f"Setting up OPI realm from project file: {project_filename}")

        # Read bundled project file
        yaml_parser = YAML()
        with project_file.open() as f:
            project_data = yaml_parser.load(f)

        # Extract keycloak config from services section
        keycloak_config = self._extract_keycloak_service_config(project_data)
        template_name = keycloak_config["template"]

        # Build context for the template
        operations_manager_domain = extract_domain_from_url(settings.OWN_DOMAIN)

        context = {
            "project_name": "rig-platform",
            "cluster": settings.CLUSTER_MANAGER,
            "keycloak_url": settings.KEYCLOAK_URL,
            "platform_realm_name": settings.KEYCLOAK_DEFAULT_REALM,
            "project_realm_name": OPERATIONS_REALM_NAME,
            "project_display_name": "Operations Manager",
            "platform_client_id": f"operations-manager-{settings.CLUSTER_MANAGER}-platform",
            "realm_name": OPERATIONS_REALM_NAME,
            "realm_display_name": "Operations Manager",
            "operations_manager_domain": operations_manager_domain,
            "invite_client_id": settings.INVITE_CLIENT_ID,
        }
        context.update(keycloak_config.get("variables", {}))

        # Execute YAML template (creates realm, IDP, client scopes, invite client)
        yaml_path = Path(__file__).parent.parent / "configs" / "keycloak" / f"{template_name}.yaml"
        handler = KeycloakYamlHandler(self.keycloak)
        await handler.execute_config(yaml_path, context)
        logger.info(f"Created OPI realm '{OPERATIONS_REALM_NAME}' using template '{template_name}'")

        # Create default users from project file (if defined)
        users = keycloak_config.get("users", [])
        if users:
            variables = {**context, "project_realm_name": OPERATIONS_REALM_NAME}
            await handler._process_users(users, variables)
            logger.info(f"Created {len(users)} default user(s) in realm '{OPERATIONS_REALM_NAME}'")

    def _extract_keycloak_service_config(self, project_data: dict) -> dict:
        """Extract keycloak config from project file services section.

        Simplified version of keycloak_manager._get_keycloak_service_config()
        for the OPI's own project files.

        Args:
            project_data: The project file data

        Returns:
            Dictionary with template, variables, and users config
        """
        default: dict[str, Any] = {"template": "sso-support", "variables": {}, "users": []}
        services = project_data.get("services", [])
        for service in services:
            if isinstance(service, dict) and "keycloak" in service:
                config = service["keycloak"].get("config", {})
                return {
                    "template": config.get("template", "sso-support"),
                    "variables": config.get("variables", {}),
                    "restrict_access": config.get("restrict_access"),
                    "users": config.get("users", []),
                }
        return default

    async def setup_operations_client(self, realm_name: str | None = None) -> bool:
        """
        Setup the operations manager's deployment client.

        Creates the client for the operations manager GUI and updates the
        Kubernetes secret with the credentials.

        Args:
            realm_name: The realm to create the client in. Defaults to KEYCLOAK_DEFAULT_REALM.

        Returns:
            True if operations client setup was successful
        """
        target_realm = realm_name or settings.KEYCLOAK_DEFAULT_REALM
        logger.info(f"Setting up operations manager client in realm '{target_realm}'")

        try:
            deployment_name = "operations-manager"
            project_name = "rig-platform"
            expected_client_id = f"{project_name}-{deployment_name}"
            ingress_hosts = [settings.OWN_DOMAIN]

            # Add additional domains if configured
            if settings.ADDITIONAL_DOMAINS:
                additional = [d.strip() for d in settings.ADDITIONAL_DOMAINS.split(",") if d.strip()]
                ingress_hosts.extend(additional)
                logger.info(f"Added additional domains: {additional}")

            logger.info(f"Creating/updating client '{expected_client_id}' with domains: {ingress_hosts}")

            # Create or get the client
            client_info = await self.keycloak.create_deployment_client(
                deployment_name=deployment_name,
                project_name=project_name,
                ingress_hosts=ingress_hosts,
                realm_name=target_realm,
            )

            logger.info(f"Successfully created/retrieved client: {expected_client_id}")

            # Update the operations-manager-keycloak secret
            success = await self._update_operations_secret(client_info)

            if success:
                logger.info("Operations manager client setup completed successfully")
                return True
            else:
                logger.error("Failed to update operations manager secret")
                return False

        except Exception as e:
            logger.error(f"Failed to setup operations client: {e}")
            return False

    async def _update_operations_secret(self, client_info: dict[str, Any]) -> bool:
        """Update the operations-manager-keycloak secret with client credentials."""
        try:
            logger.info("Updating operations-manager-keycloak secret with OIDC credentials")

            # Create a temporary directory for the manifest
            from opi.core.config import settings

            with tempfile.TemporaryDirectory(dir=settings.TEMP_DIR) as temp_dir:
                manifest_generator = ManifestGenerator()

                # Get the template path for generic secret
                template_path = os.path.join(
                    os.path.dirname(__file__), "..", "..", "manifests", "generic-secret.yaml.to-sops.jinja"
                )

                # Prepare the values for the secret
                secret_values = {
                    "name": "operations-manager-keycloak",
                    "namespace": get_namespace(settings.CLUSTER_MANAGER),
                    "secret_type": "oidc-credentials",
                    "secret_pairs": {
                        "OIDC_CLIENT_ID": client_info["client_id"],
                        "OIDC_CLIENT_SECRET": client_info["client_secret"],
                        "OIDC_DISCOVERY_URL": client_info["discovery_url"],
                        "OIDC_URL": client_info["base_url"],
                        "OIDC_REALM": client_info["realm"],
                    },
                    "secret_annotations": {
                        "operations-manager.rig/managed": "true",
                        "operations-manager.rig/purpose": "OIDC client credentials for operations-manager authentication",
                    },
                }

                # Create the manifest file (without SOPS encryption since we're applying directly)
                manifest_file_path = manifest_generator.create_manifest_file(
                    template_path=template_path,
                    values=secret_values,
                    output_dir=temp_dir,
                    output_filename="operations-manager-keycloak-secret.yaml",
                    use_sops=False,
                )

                # Apply the secret using kubectl
                success = await self.kubectl.apply_manifest(manifest_file_path)

                if success:
                    logger.info("Successfully updated operations-manager-keycloak secret")

                    # Update the current settings if they're not already set
                    if not settings.OIDC_CLIENT_ID:
                        settings.OIDC_CLIENT_ID = client_info["client_id"]
                        logger.info(f"Updated settings.OIDC_CLIENT_ID: {client_info['client_id']}")

                    if not settings.OIDC_CLIENT_SECRET:
                        settings.OIDC_CLIENT_SECRET = client_info["client_secret"]
                        logger.info("Updated settings.OIDC_CLIENT_SECRET")

                    if not settings.OIDC_DISCOVERY_URL:
                        settings.OIDC_DISCOVERY_URL = client_info["discovery_url"]
                        logger.info(f"Updated settings.OIDC_DISCOVERY_URL: {client_info['discovery_url']}")

                    return True
                else:
                    logger.error("Failed to apply operations-manager-keycloak secret")
                    return False

        except Exception as e:
            logger.error(f"Error updating operations secret: {e}")
            return False


def extract_realm_from_discovery_url(discovery_url: str | None) -> str | None:
    """Extract realm name from OIDC discovery URL.

    Args:
        discovery_url: The OIDC discovery URL (e.g., https://keycloak.example.com/realms/my-realm/.well-known/...)

    Returns:
        The realm name, or None if not extractable
    """
    if not discovery_url:
        return None
    match = re.search(r"/realms/([^/]+)/", discovery_url)
    return match.group(1) if match else None


# Convenience function for startup
async def setup_keycloak() -> bool:
    """
    Run the complete Keycloak setup sequence.

    This is the main entry point called from startup.py.

    Returns:
        True if all Keycloak setup completed successfully
    """
    setup = KeycloakSetup()
    return await setup.setup_all()
