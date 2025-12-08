"""
Keycloak connector - thin wrapper around python-keycloak library.

This connector provides access to Keycloak Admin API operations through
the python-keycloak library, maintaining a consistent interface for the
operations manager.
"""

import logging
import secrets
import string
from enum import Enum
from typing import Any

from keycloak import KeycloakAdmin
from keycloak.exceptions import KeycloakError, KeycloakGetError, KeycloakPostError

from opi.core.config import settings

logger = logging.getLogger(__name__)


class RealmType(Enum):
    """Type of Keycloak realm for determining mapper configuration."""

    PLATFORM = "platform"  # RIG Platform realm (overrides sub/preferred_username)
    PROJECT = "project"  # Project-specific realms (passthrough only)


class KeycloakConnector:
    """Thin wrapper around python-keycloak for Keycloak API access."""

    def __init__(
        self,
        keycloak_url: str,
        admin_username: str | None = None,
        admin_password: str | None = None,
    ):
        """
        Initialize the Keycloak connector.

        Args:
            keycloak_url: Base URL of the Keycloak server
            admin_username: Admin username for Keycloak API access
            admin_password: Admin password for Keycloak API access
        """
        self.keycloak_url = keycloak_url.rstrip("/")
        self.admin_username = admin_username
        self.admin_password = admin_password

        # Initialize KeycloakAdmin instance
        self.admin = KeycloakAdmin(
            server_url=self.keycloak_url,
            username=self.admin_username,
            password=self.admin_password,
            realm_name="master",
            user_realm_name="master",  # Always authenticate against master realm
            verify=True,
        )

        logger.debug(f"Initialized KeycloakConnector for {keycloak_url}")

    # ==================== Realm Operations ====================

    async def create_realm(
        self, realm_name: str, display_name: str | None = None, add_master_idp: bool = False
    ) -> dict[str, Any]:
        """
        Create a new realm in Keycloak.

        Args:
            realm_name: Name of the realm to create
            display_name: Optional display name for the realm
            add_master_idp: Whether to add the master OIDC IDP (default: False)

        Returns:
            Dictionary containing realm information including client details
        """
        logger.info(f"Creating Keycloak realm: {realm_name}")

        realm_data = {
            "realm": realm_name,
            "displayName": display_name or realm_name.title(),
            "enabled": True,
            "registrationAllowed": False,
            "loginWithEmailAllowed": False,
            "duplicateEmailsAllowed": False,
            "resetPasswordAllowed": False,
            "editUsernameAllowed": False,
            "bruteForceProtected": True,
            "rememberMe": False,
            "verifyEmail": False,
            "loginTheme": "nl-design-system",
            "adminTheme": "nl-design-system",
            "accountTheme": "nl-design-system",
            "browserFlow": "browser",
            "directGrantFlow": "direct grant",
            "clientAuthenticationFlow": "clients",
            "dockerAuthenticationFlow": "docker auth",
        }

        try:
            # Create the realm (idempotent - handles conflicts)
            try:
                self.admin.create_realm(payload=realm_data)
                logger.info(f"Created new realm: {realm_name}")
            except KeycloakPostError as e:
                if "409" in str(e) or "Conflict" in str(e):
                    logger.info(f"Realm {realm_name} already exists, using existing realm")
                else:
                    raise

            # Get the realm details
            realm_info = self.admin.get_realm(realm_name=realm_name)

            # Optionally add master OIDC identity provider
            if add_master_idp:
                try:
                    await self.add_identity_provider(
                        realm_name=realm_name,
                        provider_alias="sso-rijk",
                        display_name="SSO Rijk",
                        client_id=settings.KEYCLOAK_MASTER_OIDC_CLIENT_ID,
                        client_secret=settings.KEYCLOAK_MASTER_OIDC_CLIENT_SECRET,
                        discovery_url=settings.KEYCLOAK_MASTER_OIDC_DISCOVERY_URL,
                    )
                    logger.info(f"Added master OIDC provider to realm {realm_name}")

                    # Configure authentication flow for direct SSO redirect
                    await self.configure_sso_redirect_flow(realm_name, "sso-rijk")
                    logger.info(f"Configured direct SSO redirect flow for realm {realm_name}")

                except Exception as e:
                    logger.warning(f"Failed to add master OIDC provider to realm {realm_name}: {e}")

            # Get the discovery URL
            discovery_url = self.get_discovery_url(realm_name)

            result = {
                "realm": realm_info,
                "discovery_url": discovery_url,
                "created": True,
            }

            logger.info(f"Successfully created realm: {realm_name}")
            return result

        except KeycloakError as e:
            logger.error(f"Failed to create realm {realm_name}: {e}")
            raise

    async def delete_realm(self, realm_name: str) -> bool:
        """
        Delete a realm from Keycloak.

        Args:
            realm_name: Name of the realm to delete

        Returns:
            True if deletion was successful

        Raises:
            KeycloakError: If deletion fails
        """
        logger.info(f"Deleting Keycloak realm: {realm_name}")

        try:
            self.admin.delete_realm(realm_name=realm_name)
            logger.info(f"Successfully deleted realm: {realm_name}")
            return True

        except KeycloakError as e:
            logger.error(f"Failed to delete realm {realm_name}: {e}")
            raise

    async def realm_exists(self, realm_name: str) -> bool:
        """
        Check if a realm exists.

        Args:
            realm_name: Name of the realm

        Returns:
            True if realm exists, False otherwise
        """
        try:
            self.admin.get_realm(realm_name=realm_name)
            return True
        except KeycloakGetError:
            return False

    async def get_realm(self, realm_name: str) -> dict[str, Any] | None:
        """
        Get realm configuration.

        Args:
            realm_name: Name of the realm

        Returns:
            Realm configuration dict or None if not found
        """
        try:
            return self.admin.get_realm(realm_name=realm_name)
        except KeycloakGetError:
            return None

    def get_discovery_url(self, realm_name: str) -> str:
        """
        Get the OIDC discovery URL for a realm.

        Args:
            realm_name: Name of the realm

        Returns:
            OIDC discovery URL
        """
        discovery_url = f"{self.keycloak_url}/realms/{realm_name}/.well-known/openid-configuration"
        logger.debug(f"Discovery URL for realm '{realm_name}': {discovery_url}")
        return discovery_url

    # ==================== Client Operations ====================

    async def create_oidc_client(
        self,
        realm_name: str,
        client_id: str,
        client_name: str | None = None,
        redirect_uris: list[str] | None = None,
        web_origins: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Create an OIDC client in the specified realm.

        Args:
            realm_name: Name of the realm
            client_id: Client ID for the OIDC client
            client_name: Optional display name for the client
            redirect_uris: List of allowed redirect URIs
            web_origins: List of allowed web origins

        Returns:
            Dictionary containing client information including secret
        """
        logger.info(f"Creating OIDC client '{client_id}' in realm '{realm_name}'")

        client_secret = self._generate_client_secret()

        client_data = {
            "clientId": client_id,
            "name": client_name or client_id,
            "protocol": "openid-connect",
            "enabled": True,
            "publicClient": False,
            "secret": client_secret,
            "redirectUris": redirect_uris or ["*"],
            "webOrigins": web_origins or ["*"],
            "standardFlowEnabled": True,
            "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": True,
            "serviceAccountsEnabled": True,
        }

        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)
            self.admin.create_client(payload=client_data)
            # Switch back to master
            self.admin.change_current_realm("master")

            client_data["created"] = True
            logger.info(f"Successfully created OIDC client '{client_id}'")
            return client_data

        except KeycloakError as e:
            logger.error(f"Failed to create OIDC client '{client_id}': {e}")
            # Switch back to master
            self.admin.change_current_realm("master")
            raise

    def _generate_client_secret(self) -> str:
        """
        Generate a secure client secret.

        Returns:
            A randomly generated client secret
        """
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(32))

    async def create_deployment_client(
        self,
        deployment_name: str,
        project_name: str,
        ingress_hosts: list[str],
        realm_name: str,
        support_http: bool = False,
        additional_redirect_uris: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Create a client for a specific deployment in the specified realm.

        Args:
            deployment_name: Name of the deployment
            project_name: Name of the project
            ingress_hosts: List of ingress hostnames for redirect URIs
            realm_name: Realm name (required, must be explicitly provided)
            support_http: Whether to generate both HTTP and HTTPS redirect URIs (default: False, HTTPS only)
            additional_redirect_uris: Optional list of additional redirect URIs (e.g., localhost URLs for development)

        Returns:
            Dictionary containing client information and OIDC configuration
        """
        client_id = f"{project_name}-{deployment_name}"
        client_secret = self._generate_client_secret()

        logger.info(f"Creating client '{client_id}' for deployment '{deployment_name}' in project '{project_name}'")
        logger.info(f"Received ingress_hosts: {ingress_hosts}")
        logger.info(f"HTTP support: {support_http}")
        if additional_redirect_uris:
            logger.info(f"Additional redirect URIs: {additional_redirect_uris}")

        # Build redirect URIs and web origins from ingress hosts
        redirect_uris_set = set()
        web_origins_set = set()

        for host in ingress_hosts:
            # Always add HTTPS
            redirect_uris_set.add(f"https://{host}/*")
            web_origins_set.add(f"https://{host}")

            # Add HTTP only if cluster supports it
            if support_http:
                redirect_uris_set.add(f"http://{host}/*")
                web_origins_set.add(f"http://{host}")

        # Add project-specific additional redirect URIs (e.g., localhost for development)
        if additional_redirect_uris:
            for uri in additional_redirect_uris:
                redirect_uris_set.add(uri)
                # Extract origin from URI (remove /* suffix and path components)
                if uri.endswith("/*"):
                    origin = uri[:-2]
                elif "/*" in uri:
                    origin = uri.split("/*")[0]
                else:
                    origin = uri.rstrip("/")
                web_origins_set.add(origin)

        redirect_uris = list(redirect_uris_set)
        web_origins = list(web_origins_set)

        logger.info(f"Final redirect_uris: {redirect_uris}")
        logger.info(f"Final web_origins: {web_origins}")

        client_data = {
            "clientId": client_id,
            "name": f"{project_name} - {deployment_name}",
            "description": f"OIDC client for deployment {deployment_name} in project {project_name}",
            "protocol": "openid-connect",
            "enabled": True,
            "publicClient": False,
            "secret": client_secret,
            "redirectUris": redirect_uris,
            "webOrigins": web_origins,
            "standardFlowEnabled": True,
            "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": True,
            "serviceAccountsEnabled": True,
            "frontchannelLogout": True,
        }

        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)

            # Try to create the client
            try:
                self.admin.create_client(payload=client_data)
                created = True
                logger.info(f"Successfully created client '{client_id}' for deployment '{deployment_name}'")
            except KeycloakPostError as e:
                if "409" in str(e) or "Conflict" in str(e):
                    logger.info(f"Client '{client_id}' already exists, retrieving existing credentials")
                    # Find existing client and get secret
                    existing_client = await self.find_client_by_client_id(client_id, realm_name)
                    if existing_client:
                        client_secret = await self.get_client_secret(existing_client["id"], realm_name)
                    created = False
                else:
                    raise

            # Switch back to master
            self.admin.change_current_realm("master")

            # Get discovery URL
            discovery_url = self.get_discovery_url(realm_name)

            result = {
                "client_id": client_id,
                "client_secret": client_secret,
                "discovery_url": discovery_url,
                "base_url": self.keycloak_url,
                "realm": realm_name,
                "deployment_name": deployment_name,
                "project_name": project_name,
                "ingress_hosts": ingress_hosts,
                "created": created,
            }

            # Assign custom client scope to the newly created client
            await self._assign_custom_scope_to_client(client_id, realm_name)

            return result

        except KeycloakError as e:
            logger.error(f"Failed to create client '{client_id}' for deployment '{deployment_name}': {e}")
            # Switch back to master
            self.admin.change_current_realm("master")
            raise

    async def delete_deployment_client(self, deployment_name: str, project_name: str, realm_name: str) -> bool:
        """
        Delete a client for a specific deployment from the specified realm.

        Args:
            deployment_name: Name of the deployment
            project_name: Name of the project
            realm_name: Realm name (required, must be explicitly provided)

        Returns:
            True if deletion was successful, False if client not found
        """
        client_id = f"{project_name}-{deployment_name}"

        logger.info(f"Deleting client '{client_id}' for deployment '{deployment_name}' in project '{project_name}'")

        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)

            # Find the client
            target_client = await self.find_client_by_client_id(client_id, realm_name)

            if not target_client:
                logger.warning(f"Client '{client_id}' not found in realm '{realm_name}'")
                self.admin.change_current_realm("master")
                return False

            # Delete the client using its internal ID
            self.admin.delete_client(client_id=target_client["id"])

            # Switch back to master
            self.admin.change_current_realm("master")

            logger.info(f"Successfully deleted client '{client_id}' for deployment '{deployment_name}'")
            return True

        except KeycloakError as e:
            logger.error(f"Failed to delete client '{client_id}' for deployment '{deployment_name}': {e}")
            # Switch back to master
            self.admin.change_current_realm("master")
            raise

    async def update_deployment_client_hosts(
        self,
        deployment_name: str,
        project_name: str,
        ingress_hosts: list[str],
        realm_name: str | None = None,
        support_http: bool = False,
        additional_redirect_uris: list[str] | None = None,
    ) -> bool:
        """
        Update the ingress hosts for an existing deployment client.

        Args:
            deployment_name: Name of the deployment
            project_name: Name of the project
            ingress_hosts: Updated list of ingress hostnames
            realm_name: Realm name (uses default if None)
            support_http: Whether to generate both HTTP and HTTPS redirect URIs (default: False, HTTPS only)
            additional_redirect_uris: Optional list of additional redirect URIs (e.g., localhost URLs for development)

        Returns:
            True if update was successful
        """
        realm_name = realm_name or settings.KEYCLOAK_DEFAULT_REALM
        client_id = f"{project_name}-{deployment_name}"

        logger.info(f"Updating hosts for client '{client_id}' in deployment '{deployment_name}'")
        logger.info(f"Received ingress_hosts for update: {ingress_hosts}")
        logger.info(f"HTTP support: {support_http}")
        if additional_redirect_uris:
            logger.info(f"Additional redirect URIs: {additional_redirect_uris}")

        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)

            # Find the client
            target_client = await self.find_client_by_client_id(client_id, realm_name)

            if not target_client:
                logger.error(f"Client '{client_id}' not found in realm '{realm_name}'")
                self.admin.change_current_realm("master")
                return False

            # Build new redirect URIs and web origins
            redirect_uris_set = set()
            web_origins_set = set()

            for host in ingress_hosts:
                # Always add HTTPS
                redirect_uris_set.add(f"https://{host}/*")
                web_origins_set.add(f"https://{host}")

                # Add HTTP only if cluster supports it
                if support_http:
                    redirect_uris_set.add(f"http://{host}/*")
                    web_origins_set.add(f"http://{host}")

            # Add project-specific additional redirect URIs (e.g., localhost for development)
            if additional_redirect_uris:
                for uri in additional_redirect_uris:
                    redirect_uris_set.add(uri)
                    # Extract origin from URI (remove /* suffix and path components)
                    if uri.endswith("/*"):
                        origin = uri[:-2]
                    elif "/*" in uri:
                        origin = uri.split("/*")[0]
                    else:
                        origin = uri.rstrip("/")
                    web_origins_set.add(origin)

            redirect_uris = list(redirect_uris_set)
            web_origins = list(web_origins_set)

            logger.info(f"Final redirect_uris for update: {redirect_uris}")
            logger.info(f"Final web_origins for update: {web_origins}")

            # Update the client
            update_data = {"redirectUris": redirect_uris, "webOrigins": web_origins}
            self.admin.update_client(client_id=target_client["id"], payload=update_data)

            # Switch back to master
            self.admin.change_current_realm("master")

            logger.info(f"Successfully updated hosts for client '{client_id}'")
            return True

        except KeycloakError as e:
            logger.error(f"Failed to update hosts for client '{client_id}': {e}")
            # Switch back to master
            self.admin.change_current_realm("master")
            raise

    async def find_client_by_client_id(self, client_id: str, realm_name: str | None = None) -> dict[str, Any] | None:
        """
        Find a client by its clientId (not internal ID).

        Args:
            client_id: The client's clientId field
            realm_name: Realm name (uses default if None)

        Returns:
            Client data dictionary or None if not found
        """
        realm_name = realm_name or settings.KEYCLOAK_DEFAULT_REALM

        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)

            # Get all clients and filter manually
            all_clients = self.admin.get_clients()

            # Switch back to master
            self.admin.change_current_realm("master")

            # Filter by clientId
            for client in all_clients:
                if client.get("clientId") == client_id:
                    logger.debug(f"Found existing client '{client_id}' with internal ID {client['id']}")
                    return client

            logger.debug(f"Client '{client_id}' not found in realm '{realm_name}'")
            return None

        except KeycloakError as e:
            logger.error(f"Failed to search for client '{client_id}': {e}")
            # Switch back to master
            self.admin.change_current_realm("master")
            return None

    async def get_client_secret(self, client_internal_id: str, realm_name: str | None = None) -> str | None:
        """
        Retrieve the client secret for an existing client.

        Args:
            client_internal_id: The internal Keycloak client ID (not the clientId)
            realm_name: Realm name (uses default if None)

        Returns:
            The client secret or None if retrieval failed
        """
        realm_name = realm_name or settings.KEYCLOAK_DEFAULT_REALM

        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)

            secret_data = self.admin.get_client_secrets(client_id=client_internal_id)

            # Switch back to master
            self.admin.change_current_realm("master")

            client_secret = secret_data.get("value")
            if client_secret:
                logger.debug(f"Successfully retrieved client secret for client {client_internal_id}")
                return client_secret

            logger.error(f"No client secret found for client {client_internal_id}")
            return None

        except KeycloakError as e:
            logger.error(f"Failed to retrieve client secret for client {client_internal_id}: {e}")
            # Switch back to master
            self.admin.change_current_realm("master")
            return None

    async def create_federation_client(
        self, client_id: str, redirect_uris: list[str], realm_name: str
    ) -> dict[str, Any]:
        """
        Create a confidential client for realm-to-realm federation in the specified realm.

        Args:
            client_id: Unique identifier for the client
            redirect_uris: List of allowed redirect URIs for OIDC callbacks
            realm_name: Realm name (required, must be explicitly provided)

        Returns:
            Dictionary containing client_id, client_secret, and realm
        """
        client_secret = self._generate_client_secret()

        logger.info(f"Creating federation client '{client_id}' in realm '{realm_name}'")

        client_data = {
            "clientId": client_id,
            "name": f"Federation Client: {client_id}",
            "description": "OIDC federation client for project realm",
            "protocol": "openid-connect",
            "enabled": True,
            "publicClient": False,
            "secret": client_secret,
            "redirectUris": redirect_uris,
            "webOrigins": ["+"],
            "standardFlowEnabled": True,
            "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": False,
            "serviceAccountsEnabled": False,
            "attributes": {
                "backchannel.logout.session.required": "true",
                "post.logout.redirect.uris": "+",
            },
        }

        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)

            try:
                self.admin.create_client(payload=client_data)
                logger.info(f"Successfully created federation client '{client_id}' in realm '{realm_name}'")
            except KeycloakPostError as e:
                if "409" in str(e) or "Conflict" in str(e):
                    logger.info(f"Federation client '{client_id}' already exists in realm '{realm_name}'")
                    # Get existing secret
                    existing_client = await self.find_client_by_client_id(client_id, realm_name)
                    if existing_client:
                        client_secret = await self.get_client_secret(existing_client["id"], realm_name)
                else:
                    raise

            # Switch back to master
            self.admin.change_current_realm("master")

            return {
                "client_id": client_id,
                "client_secret": client_secret,
                "realm": realm_name,
            }

        except KeycloakError as e:
            logger.error(f"Failed to create federation client '{client_id}': {e}")
            # Switch back to master
            self.admin.change_current_realm("master")
            raise

    # ==================== Identity Provider Operations ====================

    async def add_identity_provider(
        self,
        realm_name: str,
        provider_alias: str,
        display_name: str,
        client_id: str,
        client_secret: str,
        discovery_url: str,
        provider_type: str = "oidc",
        authenticate_by_default: bool = True,
    ) -> dict[str, Any]:
        """
        Add an OIDC identity provider to a realm.

        Args:
            realm_name: Name of the realm
            provider_alias: Alias for the identity provider
            display_name: Display name shown in the UI
            client_id: OAuth client ID for this IDP
            client_secret: OAuth client secret for this IDP
            discovery_url: OIDC discovery URL (.well-known/openid-configuration)
            provider_type: Type of provider (default: "oidc")
            authenticate_by_default: Auto-redirect to this IDP on login (default: True)

        Returns:
            Dictionary containing provider information
        """
        logger.info(f"Adding identity provider {provider_alias} to realm {realm_name}")

        # Build OIDC configuration
        provider_config = {
            "clientId": client_id,
            "clientSecret": client_secret,
            "discoveryEndpoint": discovery_url,
            "validateSignature": "true",
            "useJwksUrl": "true",
            "syncMode": "IMPORT",
            "backchannelSupported": "true",
        }

        # Add explicit OIDC endpoints derived from discovery URL
        if discovery_url.endswith("/.well-known/openid-configuration"):
            realm_base = discovery_url.replace("/.well-known/openid-configuration", "")
            provider_config["authorizationUrl"] = f"{realm_base}/protocol/openid-connect/auth"
            provider_config["tokenUrl"] = f"{realm_base}/protocol/openid-connect/token"
            provider_config["userInfoUrl"] = f"{realm_base}/protocol/openid-connect/userinfo"
            provider_config["logoutUrl"] = f"{realm_base}/protocol/openid-connect/logout"
            provider_config["jwksUrl"] = f"{realm_base}/protocol/openid-connect/certs"

        provider_data = {
            "alias": provider_alias,
            "displayName": display_name,
            "providerId": provider_type,
            "enabled": True,
            "updateProfileFirstLoginMode": "off",
            "trustEmail": True,
            "storeToken": True,
            "addReadTokenRoleOnCreate": True,
            "authenticateByDefault": authenticate_by_default,
            "linkOnly": False,
            "firstBrokerLoginFlowAlias": "first broker login",
            "config": provider_config,
        }

        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)

            try:
                self.admin.create_idp(payload=provider_data)
                logger.info(f"Created new identity provider {provider_alias} in realm {realm_name}")
            except KeycloakPostError as e:
                if "409" in str(e) or "Conflict" in str(e):
                    logger.info(f"Identity provider {provider_alias} already exists in realm {realm_name}")
                else:
                    raise

            # Get the provider info
            provider_info = self.admin.get_idp(idp_alias=provider_alias)

            # Switch back to master
            self.admin.change_current_realm("master")

            logger.info(f"Successfully added identity provider {provider_alias} to realm {realm_name}")
            return provider_info

        except KeycloakError as e:
            logger.error(f"Failed to add identity provider {provider_alias} to realm {realm_name}: {e}")
            # Switch back to master
            self.admin.change_current_realm("master")
            raise

    async def update_identity_provider(
        self, realm_name: str, provider_alias: str, provider_type: str = "oidc", config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Update an existing identity provider.

        Args:
            realm_name: Name of the realm
            provider_alias: Alias for the identity provider
            provider_type: Type of provider (oidc, saml, etc.)
            config: Provider-specific configuration

        Returns:
            Dictionary containing updated provider information
        """
        logger.info(f"Updating identity provider {provider_alias} in realm {realm_name}")

        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)

            # Get current provider
            try:
                current_provider = self.admin.get_idp(idp_alias=provider_alias)
            except KeycloakGetError:
                logger.warning(f"Identity provider {provider_alias} not found, creating new one")
                self.admin.change_current_realm("master")
                return await self.add_identity_provider(
                    realm_name,
                    provider_alias,
                    "External Keycloak",
                    settings.KEYCLOAK_MASTER_OIDC_CLIENT_ID,
                    settings.KEYCLOAK_MASTER_OIDC_CLIENT_SECRET,
                    settings.KEYCLOAK_MASTER_OIDC_DISCOVERY_URL,
                    provider_type,
                )

            # Update configuration
            if config:
                current_provider["config"].update(config)

            self.admin.update_idp(idp_alias=provider_alias, payload=current_provider)

            # Get updated provider
            provider_info = self.admin.get_idp(idp_alias=provider_alias)

            # Switch back to master
            self.admin.change_current_realm("master")

            logger.info(f"Successfully updated identity provider {provider_alias} in realm {realm_name}")
            return provider_info

        except KeycloakError as e:
            logger.error(f"Failed to update identity provider {provider_alias} in realm {realm_name}: {e}")
            # Switch back to master
            self.admin.change_current_realm("master")
            raise

    async def get_identity_provider(self, realm_name: str, provider_alias: str) -> dict[str, Any] | None:
        """
        Get identity provider configuration.

        Args:
            realm_name: Name of the realm
            provider_alias: Alias of the identity provider

        Returns:
            Provider configuration dict or None if not found
        """
        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)
            provider = self.admin.get_idp(idp_alias=provider_alias)
            # Switch back to master
            self.admin.change_current_realm("master")
            return provider
        except KeycloakGetError:
            # Switch back to master
            self.admin.change_current_realm("master")
            return None

    async def get_identity_provider_mappers(self, realm_name: str, provider_alias: str) -> list[dict[str, Any]]:
        """
        Get all identity provider mappers for a specific provider.

        Args:
            realm_name: Name of the realm
            provider_alias: Alias of the identity provider

        Returns:
            List of mapper configurations
        """
        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)
            mappers = self.admin.get_idp_mappers(idp_alias=provider_alias)
            # Switch back to master
            self.admin.change_current_realm("master")
            return mappers or []
        except KeycloakError:
            # Switch back to master
            self.admin.change_current_realm("master")
            return []

    async def create_identity_provider_mapper(
        self, realm_name: str, provider_alias: str, mapper_config: dict[str, Any]
    ) -> dict[str, Any] | None:
        """
        Create an identity provider mapper.

        Args:
            realm_name: Name of the realm
            provider_alias: Alias of the identity provider
            mapper_config: Mapper configuration

        Returns:
            Created mapper configuration
        """
        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)
            result = self.admin.add_mapper_to_idp(idp_alias=provider_alias, payload=mapper_config)
            # Switch back to master
            self.admin.change_current_realm("master")
            return result
        except KeycloakError as e:
            logger.error(f"Failed to create IDP mapper: {e}")
            # Switch back to master
            self.admin.change_current_realm("master")
            return None

    async def update_identity_provider_mapper(
        self, realm_name: str, provider_alias: str, mapper_id: str, mapper_config: dict[str, Any]
    ) -> bool:
        """
        Update an identity provider mapper.

        Args:
            realm_name: Name of the realm
            provider_alias: Alias of the identity provider
            mapper_id: ID of the mapper to update
            mapper_config: Updated mapper configuration

        Returns:
            True if update was successful
        """
        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)
            self.admin.update_mapper_in_idp(idp_alias=provider_alias, mapper_id=mapper_id, payload=mapper_config)
            # Switch back to master
            self.admin.change_current_realm("master")
            return True
        except KeycloakError:
            # Switch back to master
            self.admin.change_current_realm("master")
            return False

    async def ensure_standard_oidc_mappers(self, realm_name: str, provider_alias: str) -> bool:
        """
        Ensure all standard OIDC identity provider mappers exist.

        Args:
            realm_name: Name of the realm
            provider_alias: Alias of the identity provider

        Returns:
            True if all mappers were successfully ensured
        """
        logger.info(f"Ensuring standard OIDC mappers for {provider_alias} in realm {realm_name}")

        # Get existing mappers
        existing_mappers = await self.get_identity_provider_mappers(realm_name, provider_alias)
        existing_mapper_names = {mapper.get("name") for mapper in existing_mappers}

        # Define standard OIDC mappers
        expected_mappers = [
            {
                "name": "email-to-username",
                "identityProviderAlias": provider_alias,
                "identityProviderMapper": "oidc-username-idp-mapper",
                "config": {"template": "${CLAIM.email}", "target": "LOCAL"},
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
                "config": {"claim": "organization.name", "user.attribute": "organization.name", "syncMode": "INHERIT"},
            },
            {
                "name": "sso-rijk-userid-mapper",
                "identityProviderAlias": provider_alias,
                "identityProviderMapper": "oidc-user-attribute-idp-mapper",
                "config": {"claim": "sub", "user.attribute": "sso-rijk-userid", "syncMode": "FORCE"},
            },
            {
                "name": "sso-rijk-userid-lowercase-mapper",
                "identityProviderAlias": provider_alias,
                "identityProviderMapper": "oidc-user-attribute-idp-mapper",
                "config": {
                    "claim": "preferred_username",
                    "user.attribute": "sso-rijk-userid-lowercase",
                    "syncMode": "FORCE",
                },
            },
        ]

        # Create missing mappers
        for mapper in expected_mappers:
            mapper_name = mapper["name"]
            if mapper_name in existing_mapper_names:
                logger.debug(f"Mapper {mapper_name} already exists, skipping")
            else:
                logger.info(f"Creating mapper: {mapper_name}")
                await self.create_identity_provider_mapper(realm_name, provider_alias, mapper)
                logger.debug(f"Created mapper: {mapper_name}")

        return True

    # ==================== Client Scope Operations ====================

    async def create_custom_client_scope(
        self,
        realm_name: str,
        scope_name: str = "custom_attributes_passthrough",
        realm_type: RealmType = RealmType.PROJECT,
    ) -> dict[str, Any] | None:
        """
        Create the custom_attributes_passthrough client scope.

        Args:
            realm_name: Name of the realm
            scope_name: Name of the client scope to create
            realm_type: Type of realm (PLATFORM or PROJECT)

        Returns:
            Created client scope data or None if failed
        """
        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)

            client_scope_data = {
                "name": scope_name,
                "description": "Passes custom user attributes (organization info, SSO-Rijk attributes) to tokens",
                "protocol": "openid-connect",
                "attributes": {
                    "include.in.token.scope": "true",
                    "display.on.consent.screen": "false",
                },
            }

            # Check if client scope already exists
            existing_scopes = self.admin.get_client_scopes()
            for scope in existing_scopes:
                if scope.get("name") == scope_name:
                    logger.info(f"Client scope '{scope_name}' already exists in realm '{realm_name}'")
                    # Ensure mappers exist
                    await self._add_custom_attributes_mappers(realm_name, scope["id"], realm_type)
                    await self.assign_client_scope_as_realm_default(realm_name, scope["id"], default=True)
                    # Switch back to master
                    self.admin.change_current_realm("master")
                    return scope

            # Create the client scope
            logger.info(f"Creating custom client scope '{scope_name}' in realm '{realm_name}'")
            self.admin.create_client_scope(payload=client_scope_data)

            # Get the created scope
            created_scopes = self.admin.get_client_scopes()
            for scope in created_scopes:
                if scope.get("name") == scope_name:
                    logger.info(f"Successfully created client scope '{scope_name}'")
                    await self._add_custom_attributes_mappers(realm_name, scope["id"], realm_type)
                    await self.assign_client_scope_as_realm_default(realm_name, scope["id"], default=True)
                    # Switch back to master
                    self.admin.change_current_realm("master")
                    return scope

            # Switch back to master
            self.admin.change_current_realm("master")
            return None

        except KeycloakError as e:
            logger.error(f"Failed to create client scope '{scope_name}': {e}")
            # Switch back to master
            self.admin.change_current_realm("master")
            return None

    async def _add_custom_attributes_mappers(self, realm_name: str, scope_id: str, realm_type: RealmType) -> None:
        """
        Add custom attribute mappers to the custom_attributes_passthrough scope.

        Args:
            realm_name: Name of the realm
            scope_id: ID of the client scope
            realm_type: Type of realm (PLATFORM or PROJECT)
        """
        # Switch to target realm (already switched in parent method)

        # Common mappers for all realms
        mappers = [
            {
                "name": "Organization Name Passthrough",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-usermodel-attribute-mapper",
                "consentRequired": False,
                "config": {
                    "aggregate.attrs": "false",
                    "introspection.token.claim": "true",
                    "multivalued": "false",
                    "userinfo.token.claim": "true",
                    "user.attribute": "organization.name",
                    "id.token.claim": "true",
                    "lightweight.claim": "false",
                    "access.token.claim": "true",
                    "claim.name": "organization.name",
                    "jsonType.label": "String",
                },
            },
            {
                "name": "Organization Number Passthrough",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-usermodel-attribute-mapper",
                "consentRequired": False,
                "config": {
                    "introspection.token.claim": "true",
                    "userinfo.token.claim": "true",
                    "user.attribute": "organization.number",
                    "id.token.claim": "true",
                    "lightweight.claim": "false",
                    "access.token.claim": "true",
                    "claim.name": "organization.number",
                    "jsonType.label": "String",
                },
            },
        ]

        # Platform realm only - SSO-Rijk override mappers
        if realm_type == RealmType.PLATFORM:
            mappers.extend(
                [
                    {
                        "name": "SSO-Rijk UserID Override (sub)",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-usermodel-attribute-mapper",
                        "consentRequired": False,
                        "config": {
                            "introspection.token.claim": "true",
                            "userinfo.token.claim": "true",
                            "user.attribute": "sso-rijk-userid",
                            "id.token.claim": "true",
                            "lightweight.claim": "false",
                            "access.token.claim": "true",
                            "claim.name": "sub",
                            "jsonType.label": "String",
                        },
                    },
                    {
                        "name": "SSO-Rijk UserID Lowercase Override (preferred_username)",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-usermodel-attribute-mapper",
                        "consentRequired": False,
                        "config": {
                            "introspection.token.claim": "true",
                            "userinfo.token.claim": "true",
                            "user.attribute": "sso-rijk-userid-lowercase",
                            "id.token.claim": "true",
                            "lightweight.claim": "false",
                            "access.token.claim": "true",
                            "claim.name": "preferred_username",
                            "jsonType.label": "String",
                        },
                    },
                ]
            )

        # All realms - SSO-Rijk passthrough
        mappers.extend(
            [
                {
                    "name": "SSO-Rijk UserID Passthrough",
                    "protocol": "openid-connect",
                    "protocolMapper": "oidc-usermodel-attribute-mapper",
                    "consentRequired": False,
                    "config": {
                        "introspection.token.claim": "true",
                        "userinfo.token.claim": "true",
                        "user.attribute": "sso-rijk-userid",
                        "id.token.claim": "true",
                        "lightweight.claim": "false",
                        "access.token.claim": "true",
                        "claim.name": "sso-rijk-userid",
                        "jsonType.label": "String",
                    },
                },
                {
                    "name": "SSO-Rijk UserID Lowercase Passthrough",
                    "protocol": "openid-connect",
                    "protocolMapper": "oidc-usermodel-attribute-mapper",
                    "consentRequired": False,
                    "config": {
                        "introspection.token.claim": "true",
                        "userinfo.token.claim": "true",
                        "user.attribute": "sso-rijk-userid-lowercase",
                        "id.token.claim": "true",
                        "lightweight.claim": "false",
                        "access.token.claim": "true",
                        "claim.name": "sso-rijk-userid-lowercase",
                        "jsonType.label": "String",
                    },
                },
            ]
        )

        # Get existing mappers
        try:
            existing_mappers = self.admin.get_mappers_from_client_scope(client_scope_id=scope_id)
            existing_mapper_names = {mapper.get("name") for mapper in (existing_mappers or [])}
        except KeycloakError:
            existing_mapper_names = set()

        # Add mappers with idempotency check
        for mapper in mappers:
            if mapper["name"] in existing_mapper_names:
                logger.debug(f"Mapper '{mapper['name']}' already exists, skipping")
                continue

            try:
                self.admin.add_mapper_to_client_scope(client_scope_id=scope_id, payload=mapper)
                logger.info(f"Added protocol mapper: {mapper['name']}")
            except KeycloakError as e:
                logger.warning(f"Failed to add protocol mapper {mapper['name']}: {e}")

    async def get_client_scopes(self, realm_name: str) -> list[dict[str, Any]]:
        """
        Get all client scopes in a realm.

        Args:
            realm_name: Name of the realm

        Returns:
            List of client scope configurations
        """
        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)
            scopes = self.admin.get_client_scopes()
            # Switch back to master
            self.admin.change_current_realm("master")
            return scopes or []
        except KeycloakError:
            # Switch back to master
            self.admin.change_current_realm("master")
            return []

    async def get_client_scope(self, realm_name: str, scope_name: str) -> dict[str, Any] | None:
        """
        Get client scope configuration.

        Args:
            realm_name: Name of the realm
            scope_name: Name of the client scope

        Returns:
            Client scope configuration dict or None if not found
        """
        scopes = await self.get_client_scopes(realm_name)
        for scope in scopes:
            if scope.get("name") == scope_name:
                return scope
        return None

    async def assign_client_scope_to_client(
        self, realm_name: str, client_internal_id: str, scope_id: str, default: bool = True
    ) -> bool:
        """
        Assign a client scope to a client.

        Args:
            realm_name: Name of the realm
            client_internal_id: Internal ID of the client
            scope_id: ID of the client scope
            default: Whether to assign as default scope (True) or optional (False)

        Returns:
            True if scope was assigned successfully
        """
        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)

            scope_type = "default" if default else "optional"
            logger.info(f"Assigning client scope '{scope_id}' as {scope_type} to client '{client_internal_id}'")

            if default:
                self.admin.add_client_default_client_scope(
                    client_id=client_internal_id, client_scope_id=scope_id, payload={}
                )
            else:
                self.admin.add_client_optional_client_scope(
                    client_id=client_internal_id, client_scope_id=scope_id, payload={}
                )

            # Switch back to master
            self.admin.change_current_realm("master")

            logger.info(f"Successfully assigned client scope as {scope_type}")
            return True

        except KeycloakError as e:
            logger.error(f"Failed to assign client scope: {e}")
            # Switch back to master
            self.admin.change_current_realm("master")
            return False

    async def assign_client_scope_as_realm_default(self, realm_name: str, scope_id: str, default: bool = True) -> bool:
        """
        Assign a client scope as a realm-level default client scope.

        Args:
            realm_name: Name of the realm
            scope_id: ID of the client scope
            default: Whether to assign as default scope (True) or optional (False)

        Returns:
            True if scope was assigned successfully as realm default
        """
        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)

            scope_type = "default" if default else "optional"
            logger.info(f"Assigning client scope '{scope_id}' as realm-level {scope_type} client scope")

            if default:
                self.admin.add_default_default_client_scope(scope_id=scope_id)
            else:
                self.admin.add_default_optional_client_scope(scope_id=scope_id)

            # Switch back to master
            self.admin.change_current_realm("master")

            logger.info(f"Successfully assigned client scope as realm-level {scope_type} client scope")
            return True

        except KeycloakError as e:
            logger.error(f"Failed to assign client scope as realm default: {e}")
            # Switch back to master
            self.admin.change_current_realm("master")
            return False

    async def _assign_custom_scope_to_client(self, client_id: str, realm_name: str) -> None:
        """
        Helper method to assign the custom attributes client scope to a client.

        Args:
            client_id: Client ID (not internal ID)
            realm_name: Name of the realm
        """
        try:
            logger.info(f"Assigning custom client scope to client '{client_id}'")

            # Find the client
            client = await self.find_client_by_client_id(client_id, realm_name)
            if not client:
                logger.warning(f"Client '{client_id}' not found, cannot assign custom scope")
                return

            # Find the custom client scope
            scopes = await self.get_client_scopes(realm_name)
            custom_scope = None
            for scope in scopes:
                if scope.get("name") == "custom_attributes_passthrough":
                    custom_scope = scope
                    break

            if not custom_scope:
                logger.warning(f"Custom client scope 'custom_attributes_passthrough' not found in realm '{realm_name}'")
                return

            # Assign the scope to the client as default
            success = await self.assign_client_scope_to_client(
                realm_name, client["id"], custom_scope["id"], default=True
            )

            if success:
                logger.info(f"Successfully assigned custom client scope to client '{client_id}'")
            else:
                logger.warning(f"Failed to assign custom client scope to client '{client_id}'")

        except Exception as e:
            logger.warning(f"Error assigning custom client scope to client '{client_id}': {e}")

    # ==================== Authentication Flow Operations ====================

    async def configure_sso_redirect_flow(self, realm_name: str, provider_alias: str) -> None:
        """
        Configure realm for SSO-only authentication with automatic redirect.

        Args:
            realm_name: Name of the realm to configure
            provider_alias: Alias of the identity provider to redirect to
        """
        logger.info(f"Configuring External IDP Redirector flow for realm {realm_name}")

        # Switch to target realm
        self.admin.change_current_realm(realm_name)

        # Step 1: Disable local authentication
        realm_update_data = {
            "registrationAllowed": False,
            "resetPasswordAllowed": False,
            "rememberMe": False,
            "loginWithEmailAllowed": False,
            "duplicateEmailsAllowed": False,
            "editUsernameAllowed": False,
            "userManagedAccessAllowed": False,
            "verifyEmail": False,
            "registrationEmailAsUsername": False,
            "bruteForceProtected": False,
        }

        self.admin.update_realm(realm_name=realm_name, payload=realm_update_data)
        logger.debug(f"Updated realm {realm_name} to disable local authentication")

        # Step 2: Create External IDP Redirector flow
        await self._create_external_idp_redirector_flow(realm_name, provider_alias)

        # Switch back to master
        self.admin.change_current_realm("master")

        logger.info(f"Successfully configured External IDP Redirector flow for realm {realm_name}")

    async def _create_external_idp_redirector_flow(self, realm_name: str, provider_alias: str) -> None:
        """
        Create External IDP Redirector authentication flow.

        Args:
            realm_name: Name of the realm
            provider_alias: Alias of the identity provider to redirect to
        """
        flow_alias = "External IDP Redirector"

        logger.info(f"Creating External IDP Redirector flow for realm {realm_name}")

        # Already switched to target realm in parent method

        # Step 1: Create the authentication flow
        flow_data = {
            "alias": flow_alias,
            "description": "External IDP Redirector flow for automatic SSO redirect",
            "providerId": "basic-flow",
            "topLevel": True,
            "builtIn": False,
        }

        try:
            self.admin.create_authentication_flow(payload=flow_data)
            logger.debug("Created External IDP Redirector flow")
        except KeycloakPostError as e:
            if "409" in str(e) or "Conflict" in str(e):
                logger.debug(f"Flow '{flow_alias}' already exists, will reuse it")
            else:
                raise

        # Step 2-3: Add executions (Cookie and Identity Provider Redirector)
        await self._add_execution_with_requirement(realm_name, flow_alias, "auth-cookie", "ALTERNATIVE")
        logger.debug("Added Cookie execution (ALTERNATIVE)")

        await self._add_execution_with_requirement(
            realm_name, flow_alias, "identity-provider-redirector", "ALTERNATIVE"
        )
        logger.debug("Added Identity Provider Redirector execution (ALTERNATIVE)")

        # Step 4: Configure the Identity Provider Redirector
        await self._configure_redirector_execution(realm_name, flow_alias, provider_alias)

        # Step 5: Set as Browser Flow
        realm_flow_update = {"browserFlow": flow_alias}
        self.admin.update_realm(realm_name=realm_name, payload=realm_flow_update)
        logger.info(f"Set '{flow_alias}' as Browser Flow for realm {realm_name}")

    async def _add_execution_with_requirement(
        self, realm_name: str, flow_alias: str, provider: str, requirement: str
    ) -> None:
        """
        Add an execution to a flow and set its requirement.

        Args:
            realm_name: Name of the realm
            flow_alias: Alias of the authentication flow
            provider: Provider ID for the execution
            requirement: Requirement level (ALTERNATIVE, REQUIRED, DISABLED)
        """
        # Already switched to target realm

        # Get flow executions
        flows = self.admin.get_authentication_flows()
        target_flow = None
        for flow in flows:
            if flow.get("alias") == flow_alias:
                target_flow = flow
                break

        if not target_flow:
            raise KeycloakError(f"Flow '{flow_alias}' not found")

        # Get executions for this flow
        executions = self.admin.get_authentication_flow_executions(flow_alias=flow_alias)

        # Check if execution already exists
        target_execution = None
        for execution in executions:
            if execution.get("providerId") == provider:
                target_execution = execution
                break

        # Create execution if it doesn't exist
        if not target_execution:
            execution_data = {"provider": provider}
            try:
                self.admin.create_authentication_flow_execution(payload=execution_data, flow_alias=flow_alias)
                logger.debug(f"Created execution '{provider}'")
            except KeycloakPostError as e:
                if "409" not in str(e) and "Conflict" not in str(e):
                    raise

            # Fetch executions again
            executions = self.admin.get_authentication_flow_executions(flow_alias=flow_alias)
            for execution in reversed(executions):
                if execution.get("providerId") == provider:
                    target_execution = execution
                    break

        if target_execution:
            # Check if requirement already matches
            if target_execution.get("requirement") == requirement:
                logger.debug(f"Execution '{provider}' already has requirement '{requirement}'")
                return

            # Update requirement
            update_data = {
                "id": target_execution.get("id"),
                "requirement": requirement,
                "displayName": target_execution.get("displayName"),
                "providerId": provider,
                "level": target_execution.get("level", 0),
                "index": target_execution.get("index", 0),
                "configurable": target_execution.get("configurable", False),
                "authenticationFlow": target_execution.get("authenticationFlow", False),
            }

            self.admin.update_authentication_flow_executions(payload=update_data, flow_alias=flow_alias)
            logger.debug(f"Set {provider} execution to {requirement} requirement")

    async def _configure_redirector_execution(self, realm_name: str, flow_alias: str, provider_alias: str) -> None:
        """
        Configure the Identity Provider Redirector execution.

        Args:
            realm_name: Name of the realm
            flow_alias: Alias of the authentication flow
            provider_alias: Alias of the identity provider to redirect to
        """
        logger.info(f"Configuring Identity Provider Redirector in flow '{flow_alias}'")

        # Already switched to target realm

        # Get executions
        executions = self.admin.get_authentication_flow_executions(flow_alias=flow_alias)

        # Find the Identity Provider Redirector execution
        redirector_execution = None
        for execution in executions:
            if execution.get("providerId") == "identity-provider-redirector":
                redirector_execution = execution
                break

        if not redirector_execution:
            raise KeycloakError(f"Identity Provider Redirector execution not found in flow '{flow_alias}'")

        execution_id = redirector_execution.get("id")
        logger.debug(f"Found Identity Provider Redirector execution: {execution_id}")

        # Configure the redirector
        config_data = {
            "alias": provider_alias,
            "config": {"defaultProvider": provider_alias},
        }

        # Check if config already exists
        existing_config_id = redirector_execution.get("authenticationConfig")

        if existing_config_id:
            # Config exists, check if it needs updating
            try:
                existing_config = self.admin.get_authenticator_config(config_id=existing_config_id)
                current_default_provider = existing_config.get("config", {}).get("defaultProvider")

                if current_default_provider == provider_alias:
                    logger.debug(f"Config already has correct defaultProvider '{provider_alias}'")
                    return

                # Update existing config
                update_data = {
                    "id": existing_config_id,
                    "alias": provider_alias,
                    "config": {"defaultProvider": provider_alias},
                }
                self.admin.update_authenticator_config(payload=update_data, config_id=existing_config_id)
                logger.info("Updated Identity Provider Redirector config")

            except KeycloakGetError:
                # Config ID exists but config was deleted, recreate
                logger.warning(f"Config ID {existing_config_id} not found, creating new config")
                self.admin.create_authenticator_config(payload=config_data, execution_id=execution_id)
                logger.info("Created Identity Provider Redirector config")
        else:
            # No config exists, create it
            logger.debug("No config exists, creating new config")
            self.admin.create_authenticator_config(payload=config_data, execution_id=execution_id)
            logger.info("Created Identity Provider Redirector config")

    # ==================== Realm Role Operations ====================

    async def create_realm_role(self, realm_name: str, role_name: str, description: str | None = None) -> bool:
        """
        Create a realm role in the specified realm.

        Args:
            realm_name: Name of the realm
            role_name: Name of the role to create
            description: Optional description for the role

        Returns:
            True if role was created or already exists
        """
        logger.info(f"Creating realm role '{role_name}' in realm '{realm_name}'")

        role_data = {
            "name": role_name,
            "description": description or f"Realm role: {role_name}",
            "composite": False,
            "clientRole": False,
        }

        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)

            try:
                self.admin.create_realm_role(payload=role_data)
                logger.info(f"Created realm role '{role_name}' in realm '{realm_name}'")
            except KeycloakPostError as e:
                if "409" in str(e) or "Conflict" in str(e):
                    logger.info(f"Realm role '{role_name}' already exists in realm '{realm_name}'")
                else:
                    raise

            # Switch back to master
            self.admin.change_current_realm("master")

            logger.info(f"Successfully ensured realm role '{role_name}' exists")
            return True

        except KeycloakError as e:
            logger.error(f"Failed to create realm role '{role_name}': {e}")
            # Switch back to master
            self.admin.change_current_realm("master")
            raise

    # ==================== Group Operations ====================

    async def create_group(self, realm_name: str, group_name: str, path: str | None = None) -> dict[str, Any]:
        """
        Create a group in the specified realm.

        Args:
            realm_name: Name of the realm
            group_name: Name of the group to create
            path: Optional group path (e.g., "/parent/child")

        Returns:
            Dictionary containing group information including group ID
        """
        logger.info(f"Creating group '{group_name}' in realm '{realm_name}'")

        group_data = {
            "name": group_name,
            "path": path or f"/{group_name}",
        }

        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)

            try:
                self.admin.create_group(payload=group_data)
                logger.info(f"Created group '{group_name}' in realm '{realm_name}'")
            except KeycloakPostError as e:
                if "409" in str(e) or "Conflict" in str(e):
                    logger.info(f"Group '{group_name}' already exists in realm '{realm_name}'")
                else:
                    raise

            # Get the created/existing group
            groups = self.admin.get_groups(query={"search": group_name})
            group_info = None
            for group in groups:
                if group.get("name") == group_name:
                    group_info = group
                    break

            # Switch back to master
            self.admin.change_current_realm("master")

            if not group_info:
                raise KeycloakError(f"Failed to retrieve group '{group_name}'")

            logger.info(f"Successfully ensured group '{group_name}' exists with ID {group_info['id']}")
            return group_info

        except KeycloakError as e:
            logger.error(f"Failed to create group '{group_name}': {e}")
            # Switch back to master
            self.admin.change_current_realm("master")
            raise

    # ==================== User Operations ====================

    async def create_user(
        self,
        realm_name: str,
        username: str,
        password: str,
        email: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """
        Create a user in the specified realm.

        Args:
            realm_name: Name of the realm
            username: Username for the new user
            password: Password for the new user
            email: Optional email address
            first_name: Optional first name
            last_name: Optional last name
            enabled: Whether the user is enabled (default: True)

        Returns:
            User information dictionary including user ID
        """
        logger.info(f"Creating user '{username}' in realm '{realm_name}'")

        user_data = {
            "username": username,
            "enabled": enabled,
            "emailVerified": False,
            "credentials": [{"type": "password", "value": password, "temporary": False}],
        }

        if email:
            user_data["email"] = email
            user_data["emailVerified"] = True

        if first_name:
            user_data["firstName"] = first_name

        if last_name:
            user_data["lastName"] = last_name

        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)

            try:
                self.admin.create_user(payload=user_data)
                logger.info(f"Created new user '{username}' in realm '{realm_name}'")
            except KeycloakPostError as e:
                if "409" in str(e) or "Conflict" in str(e):
                    logger.info(f"User '{username}' already exists in realm '{realm_name}'")
                else:
                    raise

            # Get the created user
            created_user = await self.get_user_by_username(realm_name, username)

            # Switch back to master
            self.admin.change_current_realm("master")

            if not created_user:
                raise KeycloakError(f"Failed to retrieve user '{username}'")

            logger.info(f"Successfully created user '{username}' with ID {created_user['id']}")
            return created_user

        except KeycloakError as e:
            logger.error(f"Failed to create user '{username}': {e}")
            # Switch back to master
            self.admin.change_current_realm("master")
            raise

    async def get_user_by_username(self, realm_name: str, username: str) -> dict[str, Any] | None:
        """
        Find a user by username in the specified realm.

        Args:
            realm_name: Name of the realm
            username: Username to search for

        Returns:
            User information dictionary or None if not found
        """
        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)

            users = self.admin.get_users(query={"username": username, "exact": "true"})

            # Switch back to master
            self.admin.change_current_realm("master")

            if users and len(users) > 0:
                logger.debug(f"Found user '{username}' in realm '{realm_name}'")
                return users[0]

            logger.debug(f"User '{username}' not found in realm '{realm_name}'")
            return None

        except KeycloakError as e:
            logger.error(f"Failed to search for user '{username}': {e}")
            # Switch back to master
            self.admin.change_current_realm("master")
            return None

    async def delete_user_by_username(self, realm_name: str, username: str) -> bool:
        """
        Delete a user from the specified realm by username.

        Args:
            realm_name: Name of the realm
            username: Username of the user to delete

        Returns:
            True if deletion was successful, False if user not found
        """
        logger.info(f"Deleting user '{username}' from realm '{realm_name}'")

        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)

            user = await self.get_user_by_username(realm_name, username)
            if not user:
                logger.warning(f"User '{username}' not found in realm '{realm_name}'")
                self.admin.change_current_realm("master")
                return False

            user_id = user["id"]
            self.admin.delete_user(user_id=user_id)

            # Switch back to master
            self.admin.change_current_realm("master")

            logger.info(f"Successfully deleted user '{username}' from realm '{realm_name}'")
            return True

        except KeycloakError as e:
            logger.error(f"Failed to delete user '{username}': {e}")
            # Switch back to master
            self.admin.change_current_realm("master")
            raise

    async def assign_realm_management_role(self, realm_name: str, user_id: str, target_realm: str) -> bool:
        """
        Assign realm management roles to a user for managing a target realm.

        Args:
            realm_name: Realm where the user exists (typically 'master')
            user_id: ID of the user to grant permissions to
            target_realm: Name of the realm the user will manage

        Returns:
            True if role was assigned successfully
        """
        logger.info(f"Assigning realm management roles to user {user_id} for realm {target_realm}")

        try:
            # Switch to source realm (where user exists)
            self.admin.change_current_realm(realm_name)

            # Get realm-management client for target realm
            all_clients = self.admin.get_clients()
            target_client_id = f"{target_realm}-realm"

            # Filter for the realm-management client
            client_id = None
            for client in all_clients:
                if client.get("clientId") == target_client_id:
                    client_id = client["id"]
                    break

            if not client_id:
                raise KeycloakError(f"Realm management client for '{target_realm}' not found")

            # Get available roles
            available_roles = self.admin.get_client_roles_of_user(user_id=user_id, client_id=client_id)

            if not available_roles or len(available_roles) == 0:
                logger.warning(f"No available roles found for client {client_id}")
                self.admin.change_current_realm("master")
                return True

            logger.info(f"Found {len(available_roles)} available roles")

            # Assign all available roles
            self.admin.assign_client_role(user_id=user_id, client_id=client_id, roles=available_roles)

            # Switch back to master
            self.admin.change_current_realm("master")

            logger.info("Successfully assigned realm management roles")
            return True

        except KeycloakError as e:
            logger.error(f"Failed to assign realm management roles: {e}")
            # Switch back to master
            self.admin.change_current_realm("master")
            raise

    async def assign_realm_roles_to_user(self, realm_name: str, user_id: str, role_names: list[str]) -> bool:
        """
        Assign realm roles to a user.

        Args:
            realm_name: Name of the realm
            user_id: ID of the user
            role_names: List of role names to assign

        Returns:
            True if roles were assigned successfully
        """
        logger.info(f"Assigning realm roles {role_names} to user {user_id} in realm {realm_name}")

        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)

            # Get all realm roles
            all_roles = self.admin.get_realm_roles()

            # Filter to requested roles
            roles_to_assign = []
            for role in all_roles:
                if role["name"] in role_names:
                    roles_to_assign.append(role)

            if roles_to_assign:
                self.admin.assign_realm_roles(user_id=user_id, roles=roles_to_assign)
                logger.info(f"Assigned {len(roles_to_assign)} realm roles to user {user_id}")
            else:
                logger.warning(f"No matching realm roles found for: {role_names}")

            # Switch back to master
            self.admin.change_current_realm("master")

            return True

        except KeycloakError as e:
            logger.error(f"Failed to assign realm roles: {e}")
            # Switch back to master
            self.admin.change_current_realm("master")
            raise

    async def join_user_to_group(self, realm_name: str, user_id: str, group_name: str) -> bool:
        """
        Add a user to a group.

        Args:
            realm_name: Name of the realm
            user_id: ID of the user
            group_name: Name of the group

        Returns:
            True if user was added to group successfully
        """
        logger.info(f"Adding user {user_id} to group '{group_name}' in realm {realm_name}")

        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)

            # Find the group
            groups = self.admin.get_groups(query={"search": group_name})
            target_group = None
            for group in groups:
                if group.get("name") == group_name:
                    target_group = group
                    break

            if not target_group:
                logger.warning(f"Group '{group_name}' not found in realm {realm_name}")
                self.admin.change_current_realm("master")
                return False

            # Add user to group
            self.admin.group_user_add(user_id=user_id, group_id=target_group["id"])
            logger.info(f"Successfully added user {user_id} to group '{group_name}'")

            # Switch back to master
            self.admin.change_current_realm("master")

            return True

        except KeycloakError as e:
            logger.error(f"Failed to add user to group: {e}")
            # Switch back to master
            self.admin.change_current_realm("master")
            raise

    async def remove_all_realm_roles(self, realm_name: str, user_id: str) -> bool:
        """
        Remove all realm roles from a user (useful for removing default role assignments).
        After this, only explicitly assigned roles will remain.

        Args:
            realm_name: Name of the realm
            user_id: ID of the user

        Returns:
            True if user roles were removed successfully
        """
        logger.info(f"Removing all realm roles from user {user_id} in realm {realm_name}")

        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)

            # Get user's current realm roles
            user_roles = self.admin.get_realm_roles_of_user(user_id=user_id)

            if user_roles:
                # Remove all realm roles
                self.admin.delete_realm_roles_of_user(user_id=user_id, roles=user_roles)
                role_names = [role.get("name") for role in user_roles]
                logger.info(
                    f"Successfully removed {len(user_roles)} realm roles from user {user_id}: {', '.join(role_names)}"
                )
            else:
                logger.info(f"No realm roles to remove for user {user_id}")

            # Switch back to master
            self.admin.change_current_realm("master")

            return True

        except KeycloakError as e:
            logger.error(f"Failed to remove realm roles from user: {e}")
            # Switch back to master
            self.admin.change_current_realm("master")
            raise


# ==================== Factory Function ====================


async def create_keycloak_connector(
    keycloak_url: str | None = None, admin_username: str | None = None, admin_password: str | None = None
) -> KeycloakConnector:
    """
    Factory function to create a KeycloakConnector instance.

    Args:
        keycloak_url: Base URL of the Keycloak server (uses config default if None)
        admin_username: Admin username for Keycloak API access (uses config default if None)
        admin_password: Admin password for Keycloak API access (uses config default if None)

    Returns:
        KeycloakConnector instance
    """
    return KeycloakConnector(
        keycloak_url=keycloak_url or settings.KEYCLOAK_URL,
        admin_username=admin_username or settings.KEYCLOAK_ADMIN_USERNAME,
        admin_password=admin_password or settings.KEYCLOAK_ADMIN_PASSWORD,
    )
