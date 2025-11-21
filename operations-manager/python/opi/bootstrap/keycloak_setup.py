"""
Keycloak bootstrap setup logic.

This module contains the business logic for setting up Keycloak during application startup.
It orchestrates the proper sequence of operations using YAML configuration.
"""

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from opi.connectors.keycloak import create_keycloak_connector
from opi.connectors.kubectl import KubectlConnector
from opi.core.cluster_config import get_namespace
from opi.core.config import settings
from opi.generation.manifests import ManifestGenerator
from opi.handlers.keycloak_yaml_handler import KeycloakYamlHandler

logger = logging.getLogger(__name__)


class KeycloakSetup:
    """Handles the complete Keycloak setup sequence for the operations manager."""

    def __init__(self):
        self.keycloak = None
        self.kubectl = None

    async def setup_all(self) -> bool:
        """
        Run the complete Keycloak setup sequence using YAML configuration.

        Returns:
            True if all setup steps completed successfully
        """
        logger.info("Starting Keycloak setup from bootstrap.yaml")

        try:
            # Initialize connectors
            self.keycloak = await create_keycloak_connector()
            self.kubectl = KubectlConnector()

            # Build context from settings
            context = {
                "realm_name": settings.KEYCLOAK_DEFAULT_REALM,
                "realm_display_name": settings.KEYCLOAK_DEFAULT_REALM_DISPLAY_NAME,
                "sso_client_id": settings.KEYCLOAK_MASTER_OIDC_CLIENT_ID,
                "sso_client_secret": settings.KEYCLOAK_MASTER_OIDC_CLIENT_SECRET,
                "sso_discovery_url": settings.KEYCLOAK_MASTER_OIDC_DISCOVERY_URL,
                "client_id": "rig-platform-operations-manager",
                "client_name": "rig-platform - operations-manager",
                "redirect_uris": [settings.OWN_DOMAIN],
                "web_origins": [settings.OWN_DOMAIN],
            }

            # Execute YAML configuration
            yaml_path = Path(__file__).parent.parent / "configs" / "keycloak" / "bootstrap.yaml"
            handler = KeycloakYamlHandler(self.keycloak)
            await handler.execute_config(yaml_path, context)

            # Create client and update secret (this part stays in Python for now)
            success = await self.setup_operations_client()

            if success:
                logger.info("Keycloak setup completed successfully")
            else:
                logger.error("❌ Failed to setup operations client")

            return success

        except Exception as e:
            logger.error(f"❌ Keycloak setup failed with exception: {e}")
            return False

    # Note: setup_realm, setup_external_sso, and setup_client_scopes have been replaced
    # by YAML configuration (bootstrap.yaml) and are executed in setup_all()

    async def setup_operations_client(self) -> bool:
        """
        Step 4: Setup the operations manager's own client.

        Creates the client for the operations manager GUI and updates the
        Kubernetes secret with the credentials.

        Returns:
            True if operations client setup was successful
        """
        logger.info("🔧 Step 4: Setting up operations manager client")

        try:
            realm_name = settings.KEYCLOAK_DEFAULT_REALM
            deployment_name = "operations-manager"
            project_name = "rig-platform"
            expected_client_id = f"{project_name}-{deployment_name}"
            ingress_hosts = [settings.OWN_DOMAIN]

            logger.info(f"Creating/updating client '{expected_client_id}' with domains: {ingress_hosts}")

            # Create or get the client (without realm setup)
            client_info = await self.keycloak.create_deployment_client(
                deployment_name=deployment_name,
                project_name=project_name,
                ingress_hosts=ingress_hosts,
                realm_name=realm_name,
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
