"""
Keycloak connector for managing realms, clients, and OIDC configuration.

This connector handles Keycloak realm creation and OIDC client setup
for projects that specify the sso-rijk option.

TODO: Consider migrating to python-keycloak package for better API support:
https://pypi.org/project/python-keycloak/
This might provide better type safety, error handling, and API coverage
than our current direct HTTP implementation.
"""

import logging
import secrets
import string
from typing import Any

import httpx

from opi.core.config import settings

logger = logging.getLogger(__name__)


class KeycloakConnector:
    """Connector for interacting with Keycloak for SSO configuration."""

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
        self._access_token: str | None = None

        logger.debug(f"Initialized KeycloakConnector for {keycloak_url}")

    async def _get_admin_token(self) -> str:
        """
        Get admin access token for Keycloak API.

        Returns:
            Admin access token

        Raises:
            httpx.HTTPError: If authentication fails
        """
        if self._access_token:
            return self._access_token

        if not self.admin_username or not self.admin_password:
            raise ValueError("Admin username and password are required for API access")

        token_url = f"{self.keycloak_url}/realms/master/protocol/openid-connect/token"

        data = {
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": self.admin_username,
            "password": self.admin_password,
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                token_url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            response.raise_for_status()

            token_data = response.json()
            self._access_token = token_data["access_token"]

            logger.debug("Successfully obtained admin access token")
            return self._access_token

    async def _api_request(
        self, method: str, path: str, json_data: dict[str, Any] | None = None, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        """
        Make an authenticated API request to Keycloak.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE)
            path: API path (without base URL)
            json_data: JSON data for request body
            params: Query parameters

        Returns:
            Response data or None for DELETE requests

        Raises:
            httpx.HTTPError: If request fails
        """
        token = await self._get_admin_token()
        url = f"{self.keycloak_url}/admin/realms{path}"

        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        async with httpx.AsyncClient() as client:
            response = await client.request(method=method, url=url, headers=headers, json=json_data, params=params)

            if response.status_code == 204:  # No content
                return None

            response.raise_for_status()

            if response.headers.get("content-type", "").startswith("application/json"):
                return response.json()

            return None

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
            "registrationAllowed": False,  # Disable local user registration
            "loginWithEmailAllowed": False,  # Disable local email login
            "duplicateEmailsAllowed": False,
            "resetPasswordAllowed": False,  # Disable password reset (force OIDC only)
            "editUsernameAllowed": False,
            "bruteForceProtected": True,
            "rememberMe": False,  # Disable remember me for local accounts
            "verifyEmail": False,  # No email verification needed for OIDC
            "loginTheme": "nl-design-system",  # Use NL Design System theme
            "adminTheme": "nl-design-system",  # Use NL Design System theme for admin
            "accountTheme": "nl-design-system",  # Use NL Design System theme for account
            # Additional settings to disable local login and force SSO redirect
            "identityProviders": [],  # Will be populated after identity provider creation
            "identityProviderMappers": [],
            "authenticationFlows": [],  # Will configure browser flow for direct SSO redirect
            "browserFlow": "browser",  # Default browser flow (will be customized later)
            "directGrantFlow": "direct grant",
            "clientAuthenticationFlow": "clients",
            "dockerAuthenticationFlow": "docker auth",
        }

        try:
            # Create the realm
            try:
                await self._api_request("POST", "", json_data=realm_data)
                logger.info(f"Created new realm: {realm_name}")
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 409:
                    logger.info(f"Realm {realm_name} already exists, using existing realm")
                else:
                    raise

            # Get the realm details (either just created or existing)
            realm_info = await self._api_request("GET", f"/{realm_name}")

            # Optionally add master OIDC identity provider to the realm
            if add_master_idp:
                try:
                    await self.add_identity_provider(
                        realm_name=realm_name,
                        provider_alias="master-oidc",
                        display_name="Digilab Keycloak",
                        client_id=settings.KEYCLOAK_MASTER_OIDC_CLIENT_ID,
                        client_secret=settings.KEYCLOAK_MASTER_OIDC_CLIENT_SECRET,
                        discovery_url=settings.KEYCLOAK_MASTER_OIDC_DISCOVERY_URL,
                    )
                    logger.info(f"Added master OIDC provider to realm {realm_name}")

                    # Configure authentication flow for direct SSO redirect
                    await self.configure_sso_redirect_flow(realm_name, "master-oidc")
                    logger.info(f"Configured direct SSO redirect flow for realm {realm_name}")

                except Exception as e:
                    logger.warning(f"Failed to add master OIDC provider to realm {realm_name}: {e}")
                    # Don't fail realm creation if identity provider setup fails

            # Get the discovery URL
            discovery_url = self.get_discovery_url(realm_name)

            result = {
                "realm": realm_info,
                "discovery_url": discovery_url,
                "created": True,
            }

            logger.info(f"Successfully created realm: {realm_name}")
            return result

        except httpx.HTTPError as e:
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
            httpx.HTTPError: If deletion fails
        """
        logger.info(f"Deleting Keycloak realm: {realm_name}")

        try:
            await self._api_request("DELETE", f"/{realm_name}")
            logger.info(f"Successfully deleted realm: {realm_name}")
            return True

        except httpx.HTTPError as e:
            logger.error(f"Failed to delete realm {realm_name}: {e}")
            raise

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

        Since we don't have Keycloak yet, this returns realistic dummy information.

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

        # Generate a realistic client secret
        client_secret = self._generate_client_secret()

        # For now, return dummy client information
        client_info = {
            "id": f"client-{client_id}-{secrets.token_hex(8)}",
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
            "created": True,
        }

        logger.debug(f"OIDC client created (dummy): {client_info['clientId']}")
        return client_info

    def _generate_client_secret(self) -> str:
        """
        Generate a secure client secret.

        Returns:
            A randomly generated client secret
        """
        # Generate a 32-character random string using secure random
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(32))

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

    async def add_host_to_realm(self, realm_name: str, hostname: str) -> bool:
        """
        Add a host to a realm's valid redirect URIs and web origins.

        Args:
            realm_name: Name of the realm
            hostname: Hostname to add (e.g., 'myapp.example.com')

        Returns:
            True if host was added successfully
        """
        logger.info(f"Adding host {hostname} to realm {realm_name}")

        try:
            # Get all clients in the realm
            clients = await self._api_request("GET", f"/{realm_name}/clients")

            if not clients:
                logger.warning(f"No clients found in realm {realm_name}")
                return False

            # Add the host to all clients (or you could be more selective)
            for client in clients:
                client_id = client["id"]

                # Get current client configuration
                client_config = await self._api_request("GET", f"/{realm_name}/clients/{client_id}")

                # Update redirect URIs
                redirect_uris = client_config.get("redirectUris", [])
                new_redirect_uris = [f"https://{hostname}/*", f"http://{hostname}/*"]

                for uri in new_redirect_uris:
                    if uri not in redirect_uris:
                        redirect_uris.append(uri)

                # Update web origins
                web_origins = client_config.get("webOrigins", [])
                new_web_origins = [f"https://{hostname}", f"http://{hostname}"]

                for origin in new_web_origins:
                    if origin not in web_origins:
                        web_origins.append(origin)

                # Update the client
                update_data = {"redirectUris": redirect_uris, "webOrigins": web_origins}

                await self._api_request("PUT", f"/{realm_name}/clients/{client_id}", json_data=update_data)

                logger.debug(f"Updated client {client_config.get('clientId')} with new host {hostname}")

            logger.info(f"Successfully added host {hostname} to realm {realm_name}")
            return True

        except httpx.HTTPError as e:
            logger.error(f"Failed to add host {hostname} to realm {realm_name}: {e}")
            raise

    async def add_identity_provider(
        self,
        realm_name: str,
        provider_alias: str,
        display_name: str,
        client_id: str,
        client_secret: str,
        discovery_url: str,
        provider_type: str = "oidc",
    ) -> dict[str, Any]:
        """
        Add an OIDC identity provider to a realm.

        All parameters are required - no defaults or implicit behavior.
        The discovery URL should provide all OIDC endpoints automatically.

        Args:
            realm_name: Name of the realm
            provider_alias: Alias for the identity provider
            display_name: Display name shown in the UI
            client_id: OAuth client ID for this IDP
            client_secret: OAuth client secret for this IDP
            discovery_url: OIDC discovery URL (.well-known/openid-configuration)
            provider_type: Type of provider (default: "oidc")

        Returns:
            Dictionary containing provider information
        """
        logger.info(f"Adding identity provider {provider_alias} to realm {realm_name}")

        # Build OIDC configuration with explicit endpoints
        # Even though discovery endpoint should provide URLs, Keycloak may need them explicit
        provider_config = {
            "clientId": client_id,
            "clientSecret": client_secret,
            "discoveryEndpoint": discovery_url,
            "validateSignature": "true",
            "useJwksUrl": "true",
            "syncMode": "IMPORT",
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
            "authenticateByDefault": True,
            "linkOnly": False,
            "firstBrokerLoginFlowAlias": "first broker login",
            "config": provider_config,
        }

        try:
            try:
                await self._api_request("POST", f"/{realm_name}/identity-provider/instances", json_data=provider_data)
                logger.info(f"Created new identity provider {provider_alias} in realm {realm_name}")
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 409:
                    logger.info(
                        f"Identity provider {provider_alias} already exists in realm {realm_name}, using existing provider"
                    )
                else:
                    raise

            # Get the provider (either just created or existing)
            provider_info = await self._api_request(
                "GET", f"/{realm_name}/identity-provider/instances/{provider_alias}"
            )

            logger.info(f"Successfully added identity provider {provider_alias} to realm {realm_name}")
            return provider_info

        except httpx.HTTPError as e:
            logger.error(f"Failed to add identity provider {provider_alias} to realm {realm_name}: {e}")
            raise

    async def update_identity_provider(
        self, realm_name: str, provider_alias: str, provider_type: str = "oidc", config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Update an existing identity provider with the latest configuration.

        Args:
            realm_name: Name of the realm
            provider_alias: Alias for the identity provider
            provider_type: Type of provider (oidc, saml, etc.)
            config: Provider-specific configuration

        Returns:
            Dictionary containing updated provider information
        """
        logger.info(f"Updating identity provider {provider_alias} in realm {realm_name}")

        # Get current provider configuration
        try:
            current_provider = await self._api_request(
                "GET", f"/{realm_name}/identity-provider/instances/{provider_alias}"
            )
        except httpx.HTTPError as e:
            if e.response.status_code == 404:
                logger.warning(f"Identity provider {provider_alias} not found, creating new one")
                return await self.add_identity_provider(realm_name, provider_alias, provider_type, config)
            else:
                raise

        provider_config = config or {}

        # Default OIDC configuration using master realm settings if none provided
        if provider_type == "oidc" and not config:
            # Extract base URL from discovery URL for explicit endpoints
            discovery_url = settings.KEYCLOAK_MASTER_OIDC_DISCOVERY_URL
            if discovery_url.endswith("/.well-known/openid-configuration"):
                external_realm_base = discovery_url.replace("/.well-known/openid-configuration", "")
            else:
                # Fallback construction
                external_realm_base = "https://keycloak.apps.digilab.network/realms/algoritmes"

            provider_config = {
                "clientId": settings.KEYCLOAK_MASTER_OIDC_CLIENT_ID,
                "clientSecret": settings.KEYCLOAK_MASTER_OIDC_CLIENT_SECRET,
                "discoveryEndpoint": settings.KEYCLOAK_MASTER_OIDC_DISCOVERY_URL,
                # Set explicit endpoints to avoid discovery issues
                "authorizationUrl": f"{external_realm_base}/protocol/openid-connect/auth",
                "tokenUrl": f"{external_realm_base}/protocol/openid-connect/token",
                "userInfoUrl": f"{external_realm_base}/protocol/openid-connect/userinfo",
                "logoutUrl": f"{external_realm_base}/protocol/openid-connect/logout",
                "jwksUrl": f"{external_realm_base}/protocol/openid-connect/certs",
                "backchannelSupported": "false",
                "validateSignature": "true",
                "useJwksUrl": "true",
                "syncMode": "IMPORT",
            }

        # Prepare updated provider data
        provider_data = {
            "alias": provider_alias,
            "displayName": "External Keycloak",
            "providerId": provider_type,
            "enabled": True,
            "updateProfileFirstLoginMode": "off",  # Disable profile update for seamless login
            "trustEmail": True,
            "storeToken": True,
            "addReadTokenRoleOnCreate": True,
            "authenticateByDefault": True,  # Make this the default authentication method
            "linkOnly": False,
            "firstBrokerLoginFlowAlias": "first broker login",
            "config": provider_config,
        }

        try:
            await self._api_request(
                "PUT", f"/{realm_name}/identity-provider/instances/{provider_alias}", json_data=provider_data
            )

            # Get the updated provider
            provider_info = await self._api_request(
                "GET", f"/{realm_name}/identity-provider/instances/{provider_alias}"
            )

            logger.info(f"Successfully updated identity provider {provider_alias} in realm {realm_name}")
            return provider_info

        except httpx.HTTPError as e:
            logger.error(f"Failed to update identity provider {provider_alias} in realm {realm_name}: {e}")
            raise

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
            scopes = await self._api_request("GET", f"/{realm_name}/client-scopes")
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
            # Don't fail the entire client creation for scope assignment issues

    async def create_federation_client(
        self, client_id: str, redirect_uris: list[str], realm_name: str
    ) -> dict[str, Any]:
        """
        Create a confidential client for realm-to-realm federation in the specified realm.

        This is used when a project realm needs to federate with the RIG Platform realm.

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
            "webOrigins": ["+"],  # Allow all origins that match redirect URIs
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
            await self._api_request("POST", f"/{realm_name}/clients", json_data=client_data)
            logger.info(f"Successfully created federation client '{client_id}' in realm '{realm_name}'")

            return {
                "client_id": client_id,
                "client_secret": client_secret,
                "realm": realm_name,
            }

        except httpx.HTTPError as e:
            if hasattr(e, "response") and e.response.status_code == 409:
                logger.info(f"Federation client '{client_id}' already exists in realm '{realm_name}'")
                # Client already exists, retrieve it
                clients = await self._api_request("GET", f"/{realm_name}/clients", params={"clientId": client_id})
                if clients and len(clients) > 0:
                    # Get the client secret
                    client_uuid = clients[0]["id"]
                    secret_response = await self._api_request(
                        "GET", f"/{realm_name}/clients/{client_uuid}/client-secret"
                    )
                    return {
                        "client_id": client_id,
                        "client_secret": secret_response.get("value", client_secret),
                        "realm": realm_name,
                    }
            logger.error(f"Failed to create federation client '{client_id}': {e}")
            raise

    async def create_deployment_client(
        self, deployment_name: str, project_name: str, ingress_hosts: list[str], realm_name: str
    ) -> dict[str, Any]:
        """
        Create a client for a specific deployment in the specified realm.

        Args:
            deployment_name: Name of the deployment
            project_name: Name of the project
            ingress_hosts: List of ingress hostnames for redirect URIs
            realm_name: Realm name (required, must be explicitly provided)

        Returns:
            Dictionary containing client information and OIDC configuration
        """

        # Generate client ID for this deployment
        client_id = f"{project_name}-{deployment_name}"
        client_secret = self._generate_client_secret()

        logger.info(f"Creating client '{client_id}' for deployment '{deployment_name}' in project '{project_name}'")
        logger.info(f"Received ingress_hosts: {ingress_hosts}")

        # Build redirect URIs and web origins from ingress hosts (use sets to avoid duplicates)
        redirect_uris_set = set()
        web_origins_set = set()

        for host in ingress_hosts:
            redirect_uris_set.update([f"https://{host}/*", f"http://{host}/*"])
            web_origins_set.update([f"https://{host}", f"http://{host}"])

        # Add localhost for development
        # Add localhost and 127.0.0.1 with specific ports for local development
        local_ports = ["8080", "8000", "9595"]
        for port in local_ports:
            redirect_uris_set.update([f"http://localhost:{port}/*", f"http://127.0.0.1:{port}/*"])
            web_origins_set.update([f"http://localhost:{port}", f"http://127.0.0.1:{port}"])

        # Convert sets back to lists for JSON serialization
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
            "attributes": {
                "saml.assertion.signature": "false",
                "saml.multivalued.roles": "false",
                "saml.force.post.binding": "false",
                "saml.encrypt": "false",
                "saml.server.signature": "false",
                "saml.server.signature.keyinfo.ext": "false",
                "exclude.session.state.from.auth.response": "false",
                "saml_force_name_id_format": "false",
                "saml.client.signature": "false",
                "tls.client.certificate.bound.access.tokens": "false",
                "saml.authnstatement": "false",
                "display.on.consent.screen": "false",
                "saml.onetimeuse.condition": "false",
            },
        }

        try:
            # Try to create the client
            await self._api_request("POST", f"/{realm_name}/clients", json_data=client_data)

            # Get discovery URL
            discovery_url = self.get_discovery_url(realm_name)

            result = {
                "client_id": client_id,
                "client_secret": client_secret,
                "discovery_url": discovery_url,
                "realm": realm_name,
                "deployment_name": deployment_name,
                "project_name": project_name,
                "ingress_hosts": ingress_hosts,
                "created": True,
            }

            logger.info(f"Successfully created client '{client_id}' for deployment '{deployment_name}'")

            # Assign custom client scope to the newly created client
            await self._assign_custom_scope_to_client(client_id, realm_name)

            return result

        except httpx.HTTPError as e:
            # Check if this is a 409 Conflict (client already exists)
            if e.response and e.response.status_code == 409:
                logger.info(f"Client '{client_id}' already exists, retrieving existing credentials")

                try:
                    # Find the existing client
                    existing_client = await self.find_client_by_client_id(client_id, realm_name)
                    if not existing_client:
                        logger.error(f"Client '{client_id}' should exist but was not found")
                        raise

                    # Retrieve the existing client secret
                    existing_secret = await self.get_client_secret(existing_client["id"], realm_name)
                    if not existing_secret:
                        logger.error(f"Could not retrieve secret for existing client '{client_id}'")
                        raise

                    # Get discovery URL
                    discovery_url = self.get_discovery_url(realm_name)

                    result = {
                        "client_id": client_id,
                        "client_secret": existing_secret,
                        "discovery_url": discovery_url,
                        "realm": realm_name,
                        "deployment_name": deployment_name,
                        "project_name": project_name,
                        "ingress_hosts": ingress_hosts,
                        "created": False,  # Indicates we used existing client
                    }

                    logger.info(
                        f"Successfully retrieved existing client '{client_id}' credentials for deployment '{deployment_name}'"
                    )

                    # Ensure custom client scope is assigned to existing client too
                    await self._assign_custom_scope_to_client(client_id, realm_name)

                    return result

                except Exception as retrieve_error:
                    logger.error(f"Failed to retrieve existing client '{client_id}': {retrieve_error}")
                    raise
            else:
                logger.error(f"Failed to create client '{client_id}' for deployment '{deployment_name}': {e}")
                raise

    async def delete_deployment_client(
        self, deployment_name: str, project_name: str, realm_name: str
    ) -> bool:
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
            # Get all clients to find the one with our client_id
            clients = await self._api_request("GET", f"/{realm_name}/clients")

            target_client = None
            for client in clients:
                if client["clientId"] == client_id:
                    target_client = client
                    break

            if not target_client:
                logger.warning(f"Client '{client_id}' not found in realm '{realm_name}'")
                return False

            # Delete the client using its internal ID
            await self._api_request("DELETE", f"/{realm_name}/clients/{target_client['id']}")

            logger.info(f"Successfully deleted client '{client_id}' for deployment '{deployment_name}'")
            return True

        except httpx.HTTPError as e:
            logger.error(f"Failed to delete client '{client_id}' for deployment '{deployment_name}': {e}")
            raise

    async def update_deployment_client_hosts(
        self, deployment_name: str, project_name: str, ingress_hosts: list[str], realm_name: str | None = None
    ) -> bool:
        """
        Update the ingress hosts for an existing deployment client.

        Args:
            deployment_name: Name of the deployment
            project_name: Name of the project
            ingress_hosts: Updated list of ingress hostnames
            realm_name: Realm name (uses default if None)

        Returns:
            True if update was successful
        """
        realm_name = realm_name or settings.KEYCLOAK_DEFAULT_REALM
        client_id = f"{project_name}-{deployment_name}"

        logger.info(f"Updating hosts for client '{client_id}' in deployment '{deployment_name}'")
        logger.info(f"Received ingress_hosts for update: {ingress_hosts}")

        try:
            # Get all clients to find the one with our client_id
            clients = await self._api_request("GET", f"/{realm_name}/clients")

            target_client = None
            for client in clients:
                if client["clientId"] == client_id:
                    target_client = client
                    break

            if not target_client:
                logger.error(f"Client '{client_id}' not found in realm '{realm_name}'")
                return False

            # Build new redirect URIs and web origins (use sets to avoid duplicates)
            redirect_uris_set = set()
            web_origins_set = set()

            for host in ingress_hosts:
                redirect_uris_set.update([f"https://{host}/*", f"http://{host}/*"])
                web_origins_set.update([f"https://{host}", f"http://{host}"])

            # Add localhost for development
            # Add localhost and 127.0.0.1 with specific ports for local development
            local_ports = ["8080", "8000", "9595"]
            for port in local_ports:
                redirect_uris_set.update([f"http://localhost:{port}/*", f"http://127.0.0.1:{port}/*"])
                web_origins_set.update([f"http://localhost:{port}", f"http://127.0.0.1:{port}"])

            # Convert sets back to lists for JSON serialization
            redirect_uris = list(redirect_uris_set)
            web_origins = list(web_origins_set)

            logger.info(f"Final redirect_uris for update: {redirect_uris}")
            logger.info(f"Final web_origins for update: {web_origins}")

            # Update the client
            update_data = {"redirectUris": redirect_uris, "webOrigins": web_origins}

            await self._api_request("PUT", f"/{realm_name}/clients/{target_client['id']}", json_data=update_data)

            logger.info(f"Successfully updated hosts for client '{client_id}'")
            return True

        except httpx.HTTPError as e:
            logger.error(f"Failed to update hosts for client '{client_id}': {e}")
            raise

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
            response = await self._api_request("GET", f"/{realm_name}/clients/{client_internal_id}/client-secret")
            client_secret = response.get("value")
            if client_secret:
                logger.debug(f"Successfully retrieved client secret for client {client_internal_id}")
                return client_secret
            else:
                logger.error(f"No client secret found for client {client_internal_id}")
                return None

        except httpx.HTTPError as e:
            logger.error(f"Failed to retrieve client secret for client {client_internal_id}: {e}")
            return None

    async def create_custom_client_scope(
        self, realm_name: str, scope_name: str = "custom_attributes_passthrough"
    ) -> dict[str, Any] | None:
        """
        Create the custom_attributes_passthrough client scope for passing organization info to tokens.

        Args:
            realm_name: Name of the realm
            scope_name: Name of the client scope to create

        Returns:
            Created client scope data or None if failed
        """
        try:
            client_scope_data = {
                "name": scope_name,
                "description": "Passes custom user attributes (organization info) to tokens",
                "protocol": "openid-connect",
                "attributes": {
                    "include.in.token.scope": "true",
                    "display.on.consent.screen": "false",
                    "gui.order": "",
                    "consent.screen.text": "",
                },
            }

            # Check if client scope already exists
            existing_scopes = await self._api_request("GET", f"/{realm_name}/client-scopes")

            for scope in existing_scopes:
                if scope.get("name") == scope_name:
                    logger.info(f"Client scope '{scope_name}' already exists in realm '{realm_name}'")

                    # Ensure organization mappers exist even for existing scopes
                    logger.info(f"Ensuring organization mappers exist in scope '{scope_name}'")
                    await self._add_organization_mappers(realm_name, scope["id"])

                    return scope

            # Create the client scope
            logger.info(f"Creating custom client scope '{scope_name}' in realm '{realm_name}'")
            response = await self._api_request("POST", f"/{realm_name}/client-scopes", json_data=client_scope_data)

            # Get the created client scope to return with ID
            created_scopes = await self._api_request("GET", f"/{realm_name}/client-scopes")

            for scope in created_scopes:
                if scope.get("name") == scope_name:
                    logger.info(f"Successfully created client scope '{scope_name}', now adding protocol mappers")

                    # Add the organization protocol mappers
                    await self._add_organization_mappers(realm_name, scope["id"])

                    return scope

            logger.error(f"Failed to find created client scope '{scope_name}'")
            return None

        except Exception as e:
            logger.error(f"Failed to create client scope '{scope_name}': {e}")
            return None

    async def _add_organization_mappers(self, realm_name: str, scope_id: str) -> None:
        """Add organization protocol mappers to the custom_attributes_passthrough scope."""
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

        for mapper in mappers:
            try:
                await self._api_request(
                    "POST", f"/{realm_name}/client-scopes/{scope_id}/protocol-mappers/models", json_data=mapper
                )
                logger.info(f"Added protocol mapper: {mapper['name']}")
            except Exception as e:
                logger.warning(f"Failed to add protocol mapper {mapper['name']}: {e}")

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
            scope_type = "default" if default else "optional"

            logger.info(f"Assigning client scope '{scope_id}' as {scope_type} to client '{client_internal_id}'")

            await self._api_request(
                "PUT",
                f"/{realm_name}/clients/{client_internal_id}/default-client-scopes/{scope_id}"
                if default
                else f"/{realm_name}/clients/{client_internal_id}/optional-client-scopes/{scope_id}",
            )

            logger.info(f"Successfully assigned client scope as {scope_type}")
            return True

        except Exception as e:
            logger.error(f"Failed to assign client scope: {e}")
            return False

    async def realm_exists(self, realm_name: str) -> bool:
        """
        Check if a realm exists.

        Args:
            realm_name: Name of the realm

        Returns:
            True if realm exists, False otherwise
        """
        try:
            await self._api_request("GET", f"/{realm_name}")
            return True
        except Exception:
            return False

    async def get_realm(self, realm_name: str) -> dict[str, Any] | None:
        """
        Get realm configuration.

        Args:
            realm_name: Name of the realm

        Returns:
            Realm configuration dict or None if not found
        """
        return await self._api_request("GET", f"/{realm_name}")

    async def get_identity_provider(self, realm_name: str, provider_alias: str) -> dict[str, Any] | None:
        """
        Get identity provider configuration.

        Args:
            realm_name: Name of the realm
            provider_alias: Alias of the identity provider

        Returns:
            Provider configuration dict or None if not found
        """
        return await self._api_request("GET", f"/{realm_name}/identity-provider/instances/{provider_alias}")

    async def get_client_scopes(self, realm_name: str) -> list[dict[str, Any]]:
        """
        Get all client scopes in a realm.

        Args:
            realm_name: Name of the realm

        Returns:
            List of client scope configurations
        """
        result = await self._api_request("GET", f"/{realm_name}/client-scopes")
        return result if result else []

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

    async def get_identity_provider_mappers(
        self, realm_name: str, provider_alias: str
    ) -> dict[str, Any] | list[str | Any]:
        """
        Get all identity provider mappers for a specific provider.

        Args:
            realm_name: Name of the realm
            provider_alias: Alias of the identity provider

        Returns:
            List of mapper configurations
        """
        return await self._api_request("GET", f"/{realm_name}/identity-provider/instances/{provider_alias}/mappers")

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
            Created mapper configuration (may be None if API returns no content)
        """
        return await self._api_request(
            "POST", f"/{realm_name}/identity-provider/instances/{provider_alias}/mappers", json_data=mapper_config
        )

    async def ensure_standard_oidc_mappers(self, realm_name: str, provider_alias: str) -> bool:
        """
        Ensure all standard OIDC identity provider mappers exist.

        Creates missing mappers for email, name, and organization attributes.
        This is idempotent - existing mappers are skipped.

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
            await self._api_request(
                "PUT",
                f"/{realm_name}/identity-provider/instances/{provider_alias}/mappers/{mapper_id}",
                json_data=mapper_config,
            )
            return True
        except Exception:
            return False

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
            # Get all clients to find the one with our client_id
            clients = await self._api_request("GET", f"/{realm_name}/clients")

            for client in clients:
                if client.get("clientId") == client_id:
                    logger.debug(f"Found existing client '{client_id}' with internal ID {client['id']}")
                    return client

            logger.debug(f"Client '{client_id}' not found in realm '{realm_name}'")
            return None

        except httpx.HTTPError as e:
            logger.error(f"Failed to search for client '{client_id}': {e}")
            return None

    async def configure_sso_redirect_flow(self, realm_name: str, provider_alias: str) -> None:
        """
        Configure realm for SSO-only authentication with automatic redirect.

        This creates a new "External IDP Redirector" authentication flow with Cookie and
        Identity Provider Redirector executions, then sets it as the Browser Flow for the realm.
        Based on CloudBlue documentation for automatic external IDP redirect.

        Args:
            realm_name: Name of the realm to configure
            provider_alias: Alias of the identity provider to redirect to
        """
        logger.info(f"Configuring External IDP Redirector flow for realm {realm_name}")

        # Step 1: Disable local authentication capabilities
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

        await self._api_request("PUT", f"/{realm_name}", json_data=realm_update_data)
        logger.debug(f"Updated realm {realm_name} to disable local authentication")

        # Step 2: Create External IDP Redirector flow
        await self._create_external_idp_redirector_flow(realm_name, provider_alias)

        logger.info(f"Successfully configured External IDP Redirector flow for realm {realm_name}")

    async def _create_external_idp_redirector_flow(self, realm_name: str, provider_alias: str) -> None:
        """
        Create External IDP Redirector authentication flow based on CloudBlue documentation.

        This creates a new flow with Cookie and Identity Provider Redirector executions,
        then sets it as the Browser Flow for the realm.

        Args:
            realm_name: Name of the realm
            provider_alias: Alias of the identity provider to redirect to
        """
        flow_alias = "External IDP Redirector"

        logger.info(f"Creating External IDP Redirector flow for realm {realm_name}")

        # Step 1: Create the new authentication flow (idempotent - handle 409)
        flow_data = {
            "alias": flow_alias,
            "description": "External IDP Redirector flow for automatic SSO redirect",
            "providerId": "basic-flow",
            "topLevel": True,
            "builtIn": False,
        }

        try:
            await self._api_request("POST", f"/{realm_name}/authentication/flows", json_data=flow_data)
            logger.debug("Created External IDP Redirector flow")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 409:
                logger.debug(f"Flow '{flow_alias}' already exists, will reuse it")
            else:
                raise

        # Step 2: Add Cookie execution and set to ALTERNATIVE
        await self._add_execution_with_requirement(realm_name, flow_alias, "auth-cookie", "ALTERNATIVE")
        logger.debug("Added Cookie execution (ALTERNATIVE) to External IDP Redirector flow")

        # Step 3: Add Identity Provider Redirector execution and set to ALTERNATIVE
        await self._add_execution_with_requirement(
            realm_name, flow_alias, "identity-provider-redirector", "ALTERNATIVE"
        )
        logger.debug("Added Identity Provider Redirector execution (ALTERNATIVE) to External IDP Redirector flow")

        # Step 4: Configure the Identity Provider Redirector
        await self._configure_redirector_execution(realm_name, flow_alias, provider_alias)

        # Step 5: Set this flow as the Browser Flow for the realm
        realm_flow_update = {"browserFlow": flow_alias}

        await self._api_request("PUT", f"/{realm_name}", json_data=realm_flow_update)
        logger.info(f"Set '{flow_alias}' as Browser Flow for realm {realm_name}")

    async def _configure_redirector_execution(self, realm_name: str, flow_alias: str, provider_alias: str) -> None:
        """
        Configure the Identity Provider Redirector execution in the External IDP Redirector flow.

        Args:
            realm_name: Name of the realm
            flow_alias: Alias of the authentication flow
            provider_alias: Alias of the identity provider to redirect to
        """
        logger.info(f"Configuring Identity Provider Redirector in flow '{flow_alias}'")

        # Get the executions for the External IDP Redirector flow
        executions = await self._api_request("GET", f"/{realm_name}/authentication/flows/{flow_alias}/executions")

        # Find the Identity Provider Redirector execution
        redirector_execution = None
        for execution in executions:
            if execution.get("providerId") == "identity-provider-redirector":
                redirector_execution = execution
                break

        if not redirector_execution:
            raise Exception(f"Identity Provider Redirector execution not found in flow '{flow_alias}'")

        execution_id = redirector_execution.get("id")
        logger.debug(f"Found Identity Provider Redirector execution: {execution_id}")

        # Configure the redirector with the external IDP alias
        config_data = {
            "alias": provider_alias,  # Set Alias to external IDP alias
            "config": {
                "defaultProvider": provider_alias  # Set Default Identity Provider to same alias
            },
        }

        await self._api_request(
            "POST", f"/{realm_name}/authentication/executions/{execution_id}/config", json_data=config_data
        )
        logger.info(
            f"Configured Identity Provider Redirector with alias '{provider_alias}' and defaultProvider '{provider_alias}'"
        )

    async def _add_execution_with_requirement(
        self, realm_name: str, flow_alias: str, provider: str, requirement: str
    ) -> None:
        """
        Add an execution to a flow and set its requirement (idempotent).

        This follows the standard Keycloak pattern: create execution, then update requirement.

        Args:
            realm_name: Name of the realm
            flow_alias: Alias of the authentication flow
            provider: Provider ID for the execution
            requirement: Requirement level (ALTERNATIVE, REQUIRED, DISABLED)
        """
        # First check if execution already exists
        executions = await self._api_request("GET", f"/{realm_name}/authentication/flows/{flow_alias}/executions")

        target_execution = None
        for execution in executions:
            if execution.get("providerId") == provider:
                target_execution = execution
                logger.debug(f"Execution '{provider}' already exists in flow '{flow_alias}'")
                break

        # If not exists, create it
        if not target_execution:
            execution_data = {"provider": provider}
            try:
                await self._api_request(
                    "POST", f"/{realm_name}/authentication/flows/{flow_alias}/executions/execution", json_data=execution_data
                )
                logger.debug(f"Created execution '{provider}' in flow '{flow_alias}'")
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 409:
                    logger.debug(f"Execution '{provider}' already exists (409)")
                else:
                    raise

            # Fetch executions again to get the newly created one
            executions = await self._api_request("GET", f"/{realm_name}/authentication/flows/{flow_alias}/executions")

            # Find the execution we just created
            for execution in reversed(executions):
                if execution.get("providerId") == provider:
                    target_execution = execution
                    break

        if target_execution:
            # Check if requirement already matches
            current_requirement = target_execution.get("requirement")
            if current_requirement == requirement:
                logger.debug(f"Execution '{provider}' already has requirement '{requirement}', skipping update")
                return

            # Update the execution requirement
            update_data = {
                "id": target_execution.get("id"),
                "requirement": requirement,
                "displayName": target_execution.get("displayName"),
                "providerId": provider,
                "level": target_execution.get("level", 0),
                "index": target_execution.get("index", 0),
                "configurable": target_execution.get("configurable", False),
                "authenticationFlow": target_execution.get("authenticationFlow", False),
                "authenticationConfig": target_execution.get("authenticationConfig"),
            }

            await self._api_request(
                "PUT", f"/{realm_name}/authentication/flows/{flow_alias}/executions", json_data=update_data
            )
            logger.debug(f"Set {provider} execution to {requirement} requirement")
        else:
            raise Exception(f"Could not find execution for provider {provider}")

    async def create_user(
        self, realm_name: str, username: str, password: str, email: str | None = None, enabled: bool = True
    ) -> dict[str, Any]:
        """
        Create a user in the specified realm.

        Args:
            realm_name: Name of the realm
            username: Username for the new user
            password: Password for the new user
            email: Optional email address
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

        try:
            await self._api_request("POST", f"/{realm_name}/users", json_data=user_data)
            logger.info(f"Created new user '{username}' in realm '{realm_name}'")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 409:
                logger.info(f"User '{username}' already exists in realm '{realm_name}', using existing user")
            else:
                raise

        created_user = await self.get_user_by_username(realm_name, username)
        if not created_user:
            raise Exception(f"Failed to retrieve user '{username}'")

        logger.info(f"Successfully created user '{username}' with ID {created_user['id']}")
        return created_user

    async def get_user_by_username(self, realm_name: str, username: str) -> dict[str, Any] | None:
        """
        Find a user by username in the specified realm.

        Args:
            realm_name: Name of the realm
            username: Username to search for

        Returns:
            User information dictionary or None if not found
        """
        users = await self._api_request("GET", f"/{realm_name}/users", params={"username": username, "exact": "true"})

        if users and len(users) > 0:
            logger.debug(f"Found user '{username}' in realm '{realm_name}'")
            return users[0]

        logger.debug(f"User '{username}' not found in realm '{realm_name}'")
        return None

    async def assign_realm_management_role(self, realm_name: str, user_id: str, target_realm: str) -> bool:
        """
        Assign realm management roles to a user for managing a target realm.

        This grants the user full administrative permissions for the target realm by assigning
        all available management roles from the realm's management client.

        Args:
            realm_name: Realm where the user exists (typically 'master')
            user_id: ID of the user to grant permissions to
            target_realm: Name of the realm the user will manage

        Returns:
            True if role was assigned successfully
        """
        logger.info(f"Assigning realm management roles to user {user_id} for realm {target_realm}")

        # Get realm-management client for target realm
        clients = await self._api_request("GET", f"/{realm_name}/clients", params={"clientId": f"{target_realm}-realm"})

        if not clients or len(clients) == 0:
            raise Exception(f"Realm management client for '{target_realm}' not found")

        client_id = clients[0]["id"]

        # Get available realm roles for this client
        available_roles = await self._api_request(
            "GET", f"/{realm_name}/users/{user_id}/role-mappings/clients/{client_id}/available"
        )

        if not available_roles or len(available_roles) == 0:
            logger.warning(f"No available roles found for client {client_id}, user may already have all roles")
            return True

        logger.info(
            f"Found {len(available_roles)} available roles for realm {target_realm}: "
            f"{[role.get('name') for role in available_roles]}"
        )

        # Assign ALL available roles to grant full management access
        # This typically includes: manage-realm, manage-users, manage-clients, etc.
        await self._api_request(
            "POST",
            f"/{realm_name}/users/{user_id}/role-mappings/clients/{client_id}",
            json_data=available_roles,
        )

        logger.info(
            f"Successfully assigned {len(available_roles)} realm management roles to user {user_id} for realm {target_realm}"
        )
        return True

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

        user = await self.get_user_by_username(realm_name, username)
        if not user:
            logger.warning(f"User '{username}' not found in realm '{realm_name}'")
            return False

        user_id = user["id"]

        await self._api_request("DELETE", f"/{realm_name}/users/{user_id}")

        logger.info(f"Successfully deleted user '{username}' from realm '{realm_name}'")
        return True


# TODO: always require parameters so it is more clear what this method will return
# TODO: actually, this is a proxy method for creating a KeycloakConnector so it could be removed
async def create_keycloak_connector(
    keycloak_url: str | None = None, admin_username: str | None = None, admin_password: str | None = None
) -> KeycloakConnector:
    """
    Factory function to create a KeycloakConnector instance.

    Uses configuration from settings if parameters are not provided.

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
