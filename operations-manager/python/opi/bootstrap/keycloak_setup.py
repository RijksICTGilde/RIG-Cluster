"""
Keycloak bootstrap setup logic.

This module contains the business logic for setting up Keycloak during application startup.
It orchestrates the proper sequence of operations using the KeycloakConnector.
"""

import logging
import os
import tempfile
from typing import Any

from opi.connectors.keycloak import create_keycloak_connector
from opi.connectors.kubectl import KubectlConnector
from opi.core.cluster_config import get_namespace
from opi.core.config import settings
from opi.generation.manifests import ManifestGenerator

logger = logging.getLogger(__name__)


class KeycloakSetup:
    """Handles the complete Keycloak setup sequence for the operations manager."""

    def __init__(self):
        self.keycloak = None
        self.kubectl = None

    async def setup_all(self) -> bool:
        """
        Run the complete Keycloak setup sequence.

        Returns:
            True if all setup steps completed successfully
        """
        logger.info("Starting complete Keycloak setup sequence")

        try:
            # Initialize connectors
            self.keycloak = await create_keycloak_connector()
            self.kubectl = KubectlConnector()

            # Run setup sequence
            success = (
                await self.setup_realm()
                and await self.setup_external_sso()
                and await self.setup_client_scopes()
                and await self.setup_operations_client()
            )

            if success:
                logger.info("Complete Keycloak setup completed successfully")
            else:
                logger.error("❌ Keycloak setup failed at one or more steps")

            return success

        except Exception as e:
            logger.error(f"❌ Keycloak setup failed with exception: {e}")
            return False

    async def setup_realm(self) -> bool:
        """
        Step 1: Setup the realm.

        Creates the realm if it doesn't exist and configures basic settings.

        Returns:
            True if realm setup was successful
        """
        logger.info("🔧 Step 1: Setting up Keycloak realm")

        try:
            realm_name = settings.KEYCLOAK_DEFAULT_REALM
            display_name = settings.KEYCLOAK_DEFAULT_REALM_DISPLAY_NAME

            # Check if realm exists
            existing_realm = await self.keycloak.get_realm(realm_name)

            if existing_realm:
                logger.info(f"Realm '{realm_name}' already exists")
                return True
            else:
                logger.info(f"Realm '{realm_name}' does not exist, creating it")

                # Create the realm with master IDP (this is the RIG Platform realm that connects to SSO Rijk)
                await self.keycloak.create_realm(realm_name, display_name, add_master_idp=True)
                logger.info(f"Successfully created realm: {realm_name}")
                return True

        except Exception as e:
            logger.error(f"Failed to setup realm: {e}")
            return False

    async def setup_external_sso(self) -> bool:
        """
        Step 2: Setup external SSO identity provider.

        Adds/updates the external Keycloak identity provider and configures
        identity provider mappers for user attributes.

        Returns:
            True if external SSO setup was successful
        """
        logger.info("🔧 Step 2: Setting up external SSO identity provider")

        try:
            realm_name = settings.KEYCLOAK_DEFAULT_REALM
            provider_alias = "master-oidc"

            # Check if identity provider exists
            existing_provider = await self.keycloak.get_identity_provider(realm_name, provider_alias)

            if existing_provider:
                # Provider exists, check if it needs updating
                config = existing_provider.get("config", {})
                current_client_id = config.get("clientId")
                current_discovery_url = config.get("discoveryEndpoint")

                if (
                    current_client_id == settings.KEYCLOAK_MASTER_OIDC_CLIENT_ID
                    and current_discovery_url == settings.KEYCLOAK_MASTER_OIDC_DISCOVERY_URL
                ):
                    logger.info(f"Identity provider '{provider_alias}' exists with correct configuration")
                    # Still need to ensure mappers are current
                    mappers_success = await self._ensure_identity_provider_mappers(realm_name, provider_alias)
                    if not mappers_success:
                        raise Exception(f"CRITICAL: Identity provider mappers failed for {provider_alias}")
                    return True
                else:
                    logger.info(f"Updating identity provider '{provider_alias}' configuration")
                    # Since add_identity_provider is idempotent, just call it to ensure correct config
                    await self.keycloak.add_identity_provider(
                        realm_name=realm_name,
                        provider_alias=provider_alias,
                        display_name="Digilab Keycloak",
                        client_id=settings.KEYCLOAK_MASTER_OIDC_CLIENT_ID,
                        client_secret=settings.KEYCLOAK_MASTER_OIDC_CLIENT_SECRET,
                        discovery_url=settings.KEYCLOAK_MASTER_OIDC_DISCOVERY_URL,
                    )
                    logger.info(f"Updated identity provider: {provider_alias}")
            else:
                logger.info(f"Identity provider '{provider_alias}' does not exist, creating it")

                # Create the identity provider with proper display name
                await self.keycloak.add_identity_provider(
                    realm_name=realm_name,
                    provider_alias=provider_alias,
                    display_name="Digilab Keycloak",
                    client_id=settings.KEYCLOAK_MASTER_OIDC_CLIENT_ID,
                    client_secret=settings.KEYCLOAK_MASTER_OIDC_CLIENT_SECRET,
                    discovery_url=settings.KEYCLOAK_MASTER_OIDC_DISCOVERY_URL,
                )
                logger.info(f"Successfully created identity provider: {provider_alias}")

            # Ensure mappers exist for the identity provider
            mappers_success = await self._ensure_identity_provider_mappers(realm_name, provider_alias)
            if not mappers_success:
                raise Exception(f"CRITICAL: Identity provider mappers failed for {provider_alias}")

            return True

        except Exception as e:
            logger.error(f"Failed to setup external SSO: {e}")
            return False

    async def setup_client_scopes(self) -> bool:
        """
        Step 3: Setup shared client scopes.

        Creates the custom_attributes_passthrough client scope with organization
        protocol mappers. This scope is shared by all clients.

        Returns:
            True if client scopes setup was successful
        """
        logger.info("🔧 Step 3: Setting up shared client scopes")

        try:
            realm_name = settings.KEYCLOAK_DEFAULT_REALM

            # Create/verify the custom attributes scope
            client_scope = await self.keycloak.create_custom_client_scope(realm_name)

            if client_scope:
                logger.info("Custom client scope setup completed successfully")
                return True
            else:
                logger.error("Failed to create/verify custom client scope")
                return False

        except Exception as e:
            logger.error(f"Failed to setup client scopes: {e}")
            return False

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

    async def _ensure_identity_provider_mappers(self, realm_name: str, provider_alias: str) -> bool:
        """
        Ensure all expected identity provider mappers exist.

        Creates missing mappers, skips existing ones.

        Args:
            realm_name: Name of the realm
            provider_alias: Alias of the identity provider

        Returns:
            True if all mappers were successfully ensured
        """
        logger.info(f"Ensuring identity provider mappers for {provider_alias}")

        try:
            # Get existing mappers
            existing_mappers = await self.keycloak.get_identity_provider_mappers(realm_name, provider_alias)
            existing_mapper_names = {mapper.get("name") for mapper in existing_mappers}

            # Define expected mappers
            expected_mappers = [
                {
                    "name": "email-to-username",
                    "identityProviderAlias": provider_alias,
                    "identityProviderMapper": "oidc-username-idp-mapper",
                    "config": {
                        "template": "${CLAIM.email}",
                        "target": "LOCAL",
                    },
                },
                {
                    "name": "email-mapper",
                    "identityProviderAlias": provider_alias,
                    "identityProviderMapper": "oidc-user-attribute-idp-mapper",
                    "config": {"claim": "email", "user.attribute": "email", "syncMode": "INHERIT"},
                },
                {
                    "name": "first-name-mapper",
                    "identityProviderAlias": provider_alias,
                    "identityProviderMapper": "oidc-user-attribute-idp-mapper",
                    "config": {"claim": "given_name", "user.attribute": "firstName", "syncMode": "INHERIT"},
                },
                {
                    "name": "last-name-mapper",
                    "identityProviderAlias": provider_alias,
                    "identityProviderMapper": "oidc-user-attribute-idp-mapper",
                    "config": {"claim": "family_name", "user.attribute": "lastName", "syncMode": "INHERIT"},
                },
                {
                    "name": "full-name-mapper",
                    "identityProviderAlias": provider_alias,
                    "identityProviderMapper": "oidc-user-attribute-idp-mapper",
                    "config": {"claim": "name", "user.attribute": "displayName", "syncMode": "INHERIT"},
                },
                {
                    "name": "organization-number-mapper",
                    "identityProviderAlias": provider_alias,
                    "identityProviderMapper": "oidc-user-attribute-idp-mapper",
                    "config": {
                        "claim": "organization.number",
                        "user.attribute": "organization.number",
                        "syncMode": "INHERIT",
                    },
                },
                {
                    "name": "organization-name-mapper",
                    "identityProviderAlias": provider_alias,
                    "identityProviderMapper": "oidc-user-attribute-idp-mapper",
                    "config": {
                        "claim": "organization.name",
                        "user.attribute": "organization.name",
                        "syncMode": "INHERIT",
                    },
                },
            ]

            all_successful = True

            for mapper in expected_mappers:
                mapper_name = mapper["name"]

                if mapper_name in existing_mapper_names:
                    logger.debug(f"Mapper {mapper_name} already exists, skipping")
                else:
                    logger.info(f"Creating mapper: {mapper_name}")
                    await self.keycloak.create_identity_provider_mapper(realm_name, provider_alias, mapper)
                    logger.debug(f"Created mapper: {mapper_name}")

            return all_successful

        except Exception as e:
            logger.error(f"Error ensuring identity provider mappers: {e}")
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
