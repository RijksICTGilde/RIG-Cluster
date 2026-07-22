"""
Keycloak connector - thin wrapper around python-keycloak library.

This connector provides access to Keycloak Admin API operations through
the python-keycloak library, maintaining a consistent interface for the
operations manager.
"""

import json
import logging
import secrets
import string
from enum import Enum
from typing import Any

from keycloak import KeycloakAdmin
from keycloak.exceptions import KeycloakError, KeycloakGetError, KeycloakPostError

from opi.core.config import settings

logger = logging.getLogger(__name__)

# Alias of the custom first-broker-login flow that auto-links a brokered SSO identity to a
# pre-existing local account matched by username/email, replacing the stock flow's
# confirm-link + verify-by-email/re-authentication steps. See features/keycloak-auto-link.md.
AUTO_LINK_FIRST_BROKER_LOGIN_FLOW = "first broker login auto-link"


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
        self,
        realm_name: str,
        display_name: str | None = None,
        add_master_idp: bool = False,
        sso_session_idle_timeout: int | None = None,
        sso_session_max_lifespan: int | None = None,
        events_enabled: bool | None = None,
        events_expiration: int | None = None,
        admin_events_enabled: bool | None = None,
        admin_events_details_enabled: bool | None = None,
    ) -> dict[str, Any]:
        """
        Create a new realm in Keycloak.

        Args:
            realm_name: Name of the realm to create
            display_name: Optional display name for the realm
            add_master_idp: Whether to add the master OIDC IDP (default: False)
            sso_session_idle_timeout: Optional SSO session idle timeout in seconds. When
                omitted, Keycloak's default (30 min) is kept.
            sso_session_max_lifespan: Optional SSO session max lifespan in seconds. When
                omitted, Keycloak's default (10 hours) is kept.
            events_enabled: Optional; store user events (login, UPDATE_PROFILE, ...).
            events_expiration: Optional user-event retention in seconds.
            admin_events_enabled: Optional; store admin events.
            admin_events_details_enabled: Optional; include representations in admin events.

        Returns:
            Dictionary containing realm information including client details
        """
        logger.info(f"Creating Keycloak realm: {realm_name}")

        # Only the explicitly provided session settings; used both to seed a new
        # realm and to update an existing one without touching other fields.
        session_settings: dict[str, Any] = {}
        if sso_session_idle_timeout is not None:
            session_settings["ssoSessionIdleTimeout"] = sso_session_idle_timeout
        if sso_session_max_lifespan is not None:
            session_settings["ssoSessionMaxLifespan"] = sso_session_max_lifespan

        # Audit-event settings from the realm blueprint; like the session settings these
        # are applied both on create and on replay, so existing realms are brought in
        # line on reconcile.
        event_settings: dict[str, Any] = {}
        if events_enabled is not None:
            event_settings["eventsEnabled"] = events_enabled
        if events_expiration is not None:
            event_settings["eventsExpiration"] = events_expiration
        if admin_events_enabled is not None:
            event_settings["adminEventsEnabled"] = admin_events_enabled
        if admin_events_details_enabled is not None:
            event_settings["adminEventsDetailsEnabled"] = admin_events_details_enabled

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
            **session_settings,
            **event_settings,
        }

        try:
            # Create the realm (idempotent - handles conflicts)
            try:
                self.admin.create_realm(payload=realm_data)
                logger.info(f"Created new realm: {realm_name}")
            except KeycloakPostError as e:
                if "409" in str(e) or "Conflict" in str(e):
                    logger.info(f"Realm {realm_name} already exists, using existing realm")
                    # Apply only the session and audit-event settings to the existing
                    # realm. A full realm_data update would reset browserFlow etc. and
                    # break the custom "External IDP Redirector" flow on the platform realm.
                    replay_settings = {**session_settings, **event_settings}
                    if replay_settings:
                        self.admin.update_realm(realm_name=realm_name, payload=replay_settings)
                        logger.info(f"Updated settings on existing realm {realm_name}: {replay_settings}")
                else:
                    raise

            # Get the realm details
            realm_info = self.admin.get_realm(realm_name=realm_name)

            # Make identity fields read-only for end users (impersonation hardening).
            await self._lock_identity_fields(realm_name)

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

    # Identity fields that downstream authorization trusts (OPI and app allowlists key on the
    # email claim). They are authoritatively provided by the SSO-Rijk IdP via FORCE mappers, so
    # a user must not be able to self-edit them in the account console and become someone else.
    _IMMUTABLE_USER_PROFILE_FIELDS = frozenset({"email", "firstName", "lastName"})

    async def _lock_identity_fields(self, realm_name: str) -> None:
        """Restrict edit of identity fields to admins in the realm's declarative user profile.

        Setting edit permission to admin-only makes email/firstName/lastName read-only in the
        account console; IdP mappers and admin writes are unaffected. Fails closed: a realm
        whose identity fields cannot be locked is provisioned in the vulnerable configuration
        (self-editable email was the impersonation post-mortem's root cause), so a user-profile
        API failure aborts realm provisioning rather than being logged and forgotten.
        """
        try:
            self.admin.change_current_realm(realm_name)
            profile = self.admin.get_realm_users_profile()
            changed = False
            for attr in profile.get("attributes", []):
                if attr.get("name") in self._IMMUTABLE_USER_PROFILE_FIELDS:
                    permissions = attr.setdefault("permissions", {})
                    if permissions.get("edit") != ["admin"]:
                        permissions["edit"] = ["admin"]
                        changed = True
            if changed:
                self.admin.update_realm_users_profile(payload=profile)
                logger.info(f"Locked identity fields (edit=admin) in user profile for realm {realm_name}")
        except KeycloakError as e:
            logger.error(f"Could not lock identity fields in user profile for realm {realm_name}: {e}")
            raise
        finally:
            self.admin.change_current_realm("master")

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

        Only 404 is treated as "not found". Any other Keycloak error
        (auth failure, 5xx, network) is re-raised so callers do not
        mistake a transient outage for a missing realm.
        """
        try:
            self.admin.get_realm(realm_name=realm_name)
            return True
        except KeycloakGetError as e:
            if e.response_code == 404:
                return False
            raise

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

            try:
                self.admin.create_client(payload=client_data)
                logger.info(f"Successfully created OIDC client '{client_id}'")
                client_data["created"] = True
            except KeycloakPostError as e:
                if "409" in str(e) or "Conflict" in str(e):
                    logger.info(f"Client '{client_id}' already exists in realm '{realm_name}', returning existing")
                    # Get existing client info - we're already in the target realm context
                    all_clients = self.admin.get_clients()
                    existing_client = next((c for c in all_clients if c.get("clientId") == client_id), None)
                    if existing_client:
                        # Get the client secret
                        existing_secret = self.admin.get_client_secrets(existing_client["id"])
                        client_data["secret"] = existing_secret.get("value", "")
                        client_data["created"] = False
                    else:
                        raise
                else:
                    raise

            # Switch back to master
            self.admin.change_current_realm("master")
            return client_data

        except KeycloakError as e:
            logger.error(f"Failed to create OIDC client '{client_id}': {e}")
            # Switch back to master
            self.admin.change_current_realm("master")
            raise

    async def delete_oidc_client(self, realm_name: str, client_id: str) -> bool:
        """
        Delete an OIDC client by its clientId from the specified realm.

        Idempotent: returns False if the client does not exist.

        Args:
            realm_name: Realm the client lives in
            client_id: The client's clientId field

        Returns:
            True if a client was deleted, False if none was found
        """
        logger.info(f"Deleting OIDC client '{client_id}' from realm '{realm_name}'")
        try:
            # find_client_by_client_id switches back to master, so re-switch before delete
            target = await self.find_client_by_client_id(client_id, realm_name)
            if not target:
                logger.debug(f"OIDC client '{client_id}' not found in realm '{realm_name}', nothing to delete")
                return False

            self.admin.change_current_realm(realm_name)
            self.admin.delete_client(client_id=target["id"])
            self.admin.change_current_realm("master")
            logger.info(f"Successfully deleted OIDC client '{client_id}' from realm '{realm_name}'")
            return True

        except KeycloakError as e:
            logger.error(f"Failed to delete OIDC client '{client_id}': {e}")
            self.admin.change_current_realm("master")
            raise

    async def list_client_ids_by_prefix(self, realm_name: str, prefix: str) -> list[str]:
        """
        Return the clientIds in a realm that start with the given prefix.

        Used to garbage-collect orphaned ephemeral clients (e.g. dbconsole-*).
        """
        try:
            self.admin.change_current_realm(realm_name)
            all_clients = self.admin.get_clients()
            self.admin.change_current_realm("master")
            return [c.get("clientId", "") for c in all_clients if c.get("clientId", "").startswith(prefix)]

        except KeycloakError as e:
            logger.error(f"Failed to list clients by prefix '{prefix}' in realm '{realm_name}': {e}")
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
            "attributes": {
                "post.logout.redirect.uris": "+",
            },
        }

        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)

            # Try to create the client
            updated = False
            try:
                self.admin.create_client(payload=client_data)
                created = True
                logger.info(f"Successfully created client '{client_id}' for deployment '{deployment_name}'")
            except KeycloakPostError as e:
                if "409" in str(e) or "Conflict" in str(e):
                    logger.info(f"Client '{client_id}' already exists, checking if update needed")
                    # Find existing client and get secret
                    # Note: find_client_by_client_id switches back to master realm
                    existing_client = await self.find_client_by_client_id(client_id, realm_name)
                    if existing_client:
                        client_secret = await self.get_client_secret(existing_client["id"], realm_name)

                        # Check if redirect URIs need updating
                        existing_redirect_uris = set(existing_client.get("redirectUris", []))
                        existing_web_origins = set(existing_client.get("webOrigins", []))
                        existing_attrs = existing_client.get("attributes", {}) or {}
                        expected_redirect_uris = set(redirect_uris)
                        expected_web_origins = set(web_origins)

                        uris_differ = existing_redirect_uris != expected_redirect_uris
                        origins_differ = existing_web_origins != expected_web_origins
                        post_logout_differ = existing_attrs.get("post.logout.redirect.uris") != "+"

                        if uris_differ or origins_differ or post_logout_differ:
                            logger.info(
                                f"Client '{client_id}' redirect URIs need updating. "
                                f"Current: {existing_redirect_uris}, Expected: {expected_redirect_uris}"
                            )
                            # Switch back to target realm for update
                            self.admin.change_current_realm(realm_name)
                            merged_attrs = {**existing_attrs, "post.logout.redirect.uris": "+"}
                            update_data = {
                                "redirectUris": redirect_uris,
                                "webOrigins": web_origins,
                                "attributes": merged_attrs,
                            }
                            self.admin.update_client(client_id=existing_client["id"], payload=update_data)
                            logger.info(f"Successfully updated redirect URIs for client '{client_id}'")
                            updated = True
                        else:
                            logger.info(f"Client '{client_id}' redirect URIs are already correct")

                    created = False
                else:
                    raise

            # Create public client for keycloak-js / browser-based OIDC flows
            public_client_id = f"{client_id}-public"
            await self._ensure_public_client(
                public_client_id=public_client_id,
                name=f"{project_name} - {deployment_name} (public)",
                description=f"Public OIDC client for browser-based auth in deployment {deployment_name}",
                redirect_uris=redirect_uris,
                web_origins=web_origins,
                realm_name=realm_name,
            )

            # Switch back to master
            self.admin.change_current_realm("master")

            # Get discovery URL
            discovery_url = self.get_discovery_url(realm_name)

            result = {
                "client_id": client_id,
                "client_secret": client_secret,
                "public_client_id": public_client_id,
                "discovery_url": discovery_url,
                "base_url": self.keycloak_url,
                "realm": realm_name,
                "deployment_name": deployment_name,
                "project_name": project_name,
                "ingress_hosts": ingress_hosts,
                "created": created,
                "updated": updated,
            }

            # Assign custom client scope to both clients
            await self._assign_custom_scope_to_client(client_id, realm_name)
            await self._assign_custom_scope_to_client(public_client_id, realm_name)

            return result

        except KeycloakError as e:
            logger.error(f"Failed to create client '{client_id}' for deployment '{deployment_name}': {e}")
            # Switch back to master
            self.admin.change_current_realm("master")
            raise

    async def _ensure_public_client(
        self,
        public_client_id: str,
        name: str,
        description: str,
        redirect_uris: list[str],
        web_origins: list[str],
        realm_name: str,
    ) -> None:
        """Create or update a public OIDC client for browser-based flows (keycloak-js)."""
        public_client_data = {
            "clientId": public_client_id,
            "name": name,
            "description": description,
            "protocol": "openid-connect",
            "enabled": True,
            "publicClient": True,
            "redirectUris": redirect_uris,
            "webOrigins": web_origins,
            "standardFlowEnabled": True,
            "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": False,
            "serviceAccountsEnabled": False,
            "frontchannelLogout": True,
            "attributes": {
                "pkce.code.challenge.method": "S256",
                "post.logout.redirect.uris": "+",
            },
        }

        self.admin.change_current_realm(realm_name)
        try:
            self.admin.create_client(payload=public_client_data)
            logger.info(f"Created public client '{public_client_id}'")
        except KeycloakPostError as e:
            if "409" in str(e) or "Conflict" in str(e):
                logger.info(f"Public client '{public_client_id}' already exists, checking redirect URIs")
                existing = await self.find_client_by_client_id(public_client_id, realm_name)
                if existing:
                    existing_uris = set(existing.get("redirectUris", []))
                    existing_origins = set(existing.get("webOrigins", []))
                    existing_attrs = existing.get("attributes", {}) or {}
                    post_logout_differ = existing_attrs.get("post.logout.redirect.uris") != "+"
                    if (
                        existing_uris != set(redirect_uris)
                        or existing_origins != set(web_origins)
                        or post_logout_differ
                    ):
                        self.admin.change_current_realm(realm_name)
                        merged_attrs = {**existing_attrs, "post.logout.redirect.uris": "+"}
                        self.admin.update_client(
                            client_id=existing["id"],
                            payload={
                                "redirectUris": redirect_uris,
                                "webOrigins": web_origins,
                                "attributes": merged_attrs,
                            },
                        )
                        logger.info(f"Updated redirect URIs for public client '{public_client_id}'")
            else:
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

            # find_client_by_client_id switches back to master, so re-switch to target realm
            self.admin.change_current_realm(realm_name)

            # Delete the client using its internal ID
            self.admin.delete_client(client_id=target_client["id"])

            logger.info(f"Successfully deleted client '{client_id}' for deployment '{deployment_name}'")

            # Also delete the public client (used for keycloak-js / browser-based OIDC flows)
            public_client_id = f"{client_id}-public"
            public_client = await self.find_client_by_client_id(public_client_id, realm_name)
            if public_client:
                self.admin.change_current_realm(realm_name)
                self.admin.delete_client(client_id=public_client["id"])
                logger.info(f"Successfully deleted public client '{public_client_id}'")
            else:
                logger.debug(f"Public client '{public_client_id}' not found in realm '{realm_name}', skipping")

            # Switch back to master
            self.admin.change_current_realm("master")
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

            # Update the client (preserve existing attributes, enforce post.logout.redirect.uris=+)
            existing_attrs = target_client.get("attributes", {}) or {}
            merged_attrs = {**existing_attrs, "post.logout.redirect.uris": "+"}
            update_data = {
                "redirectUris": redirect_uris,
                "webOrigins": web_origins,
                "attributes": merged_attrs,
            }
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
        update_profile_first_login: str = "off",
        config_overrides: dict[str, Any] | None = None,
        first_broker_login_flow_alias: str = "first broker login",
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
            update_profile_first_login: Whether to show profile update on first login (default: "off")
            config_overrides: Optional dict merged into provider config after defaults (YAML wins)

        Returns:
            Dictionary containing provider information
        """
        logger.info(f"Adding identity provider {provider_alias} to realm {realm_name}")

        # Build OIDC configuration
        provider_config: dict[str, Any] = {
            "clientId": client_id,
            "clientSecret": client_secret,
            "discoveryEndpoint": discovery_url,
            "validateSignature": "true",
            "useJwksUrl": "true",
            "syncMode": "IMPORT",
            "backchannelSupported": "false",
        }

        # Add explicit OIDC endpoints derived from discovery URL
        if discovery_url.endswith("/.well-known/openid-configuration"):
            realm_base = discovery_url.replace("/.well-known/openid-configuration", "")
            provider_config["authorizationUrl"] = f"{realm_base}/protocol/openid-connect/auth"
            provider_config["tokenUrl"] = f"{realm_base}/protocol/openid-connect/token"
            provider_config["userInfoUrl"] = f"{realm_base}/protocol/openid-connect/userinfo"
            provider_config["logoutUrl"] = f"{realm_base}/protocol/openid-connect/logout"
            provider_config["jwksUrl"] = f"{realm_base}/protocol/openid-connect/certs"

        # Merge YAML-provided overrides last so they take precedence over defaults
        if config_overrides:
            for key, value in config_overrides.items():
                if value is None:
                    continue
                provider_config[key] = str(value) if isinstance(value, bool) else value

        provider_data = {
            "alias": provider_alias,
            "displayName": display_name,
            "providerId": provider_type,
            "enabled": True,
            "updateProfileFirstLoginMode": update_profile_first_login,
            "trustEmail": True,
            "storeToken": True,
            "addReadTokenRoleOnCreate": True,
            "authenticateByDefault": authenticate_by_default,
            "linkOnly": False,
            "firstBrokerLoginFlowAlias": first_broker_login_flow_alias,
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
                    current_provider = self.admin.get_idp(idp_alias=provider_alias)
                    current_config = current_provider.get("config", {}) or {}

                    # Compute explicit diff: which keys would change?
                    changed_keys: list[str] = []
                    for key, desired in provider_config.items():
                        if current_config.get(key) != desired:
                            changed_keys.append(key)
                    display_name_differs = current_provider.get("displayName") != provider_data["displayName"]
                    first_broker_flow_differs = (
                        current_provider.get("firstBrokerLoginFlowAlias") != provider_data["firstBrokerLoginFlowAlias"]
                    )

                    if changed_keys or display_name_differs or first_broker_flow_differs:
                        logger.info(
                            f"Identity provider {provider_alias} in realm {realm_name} needs update: "
                            f"changed_keys={changed_keys}, displayName_differs={display_name_differs}, "
                            f"firstBrokerLoginFlow_differs={first_broker_flow_differs}"
                        )
                        current_provider["displayName"] = provider_data["displayName"]
                        current_provider["firstBrokerLoginFlowAlias"] = provider_data["firstBrokerLoginFlowAlias"]
                        current_provider["config"] = {**current_config, **provider_config}
                        self.admin.update_idp(idp_alias=provider_alias, payload=current_provider)
                        logger.info(f"Updated identity provider {provider_alias} in realm {realm_name}")
                    else:
                        logger.debug(
                            f"Identity provider {provider_alias} in realm {realm_name} already matches desired config"
                        )
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

    async def add_saml_identity_provider(
        self,
        realm_name: str,
        provider_alias: str,
        display_name: str,
        idp_entity_id: str,
        single_sign_on_service_url: str,
        single_logout_service_url: str | None = None,
        name_id_policy_format: str = "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent",
        principal_type: str = "SUBJECT",
        signing_certificate: str | None = None,
        sp_entity_id: str | None = None,
        validate_signature: bool = True,
        want_assertions_signed: bool = True,
        want_assertions_encrypted: bool = False,
        authenticate_by_default: bool = True,
        sync_mode: str = "FORCE",
        enabled: bool = True,
        update_profile_first_login: str = "off",
        config_overrides: dict[str, Any] | None = None,
        first_broker_login_flow_alias: str = "first broker login",
    ) -> dict[str, Any]:
        """
        Add a SAML identity provider to a realm.

        Args:
            realm_name: Name of the realm
            provider_alias: Alias for the identity provider
            display_name: Display name shown in the UI
            idp_entity_id: Entity ID of the external IDP (metadata URL)
            single_sign_on_service_url: SSO service URL of the external IDP
            single_logout_service_url: Logout service URL of the external IDP
            name_id_policy_format: NameID policy format (default: persistent)
            principal_type: Principal type (SUBJECT, ATTRIBUTE, FRIENDLY_ATTRIBUTE)
            signing_certificate: IDP signing certificate (PEM format, without headers)
            sp_entity_id: Our SP entity ID (defaults to realm issuer)
            validate_signature: Validate SAML response signatures
            want_assertions_signed: Require signed assertions
            want_assertions_encrypted: Require encrypted assertions
            authenticate_by_default: Auto-redirect to this IDP on login
            sync_mode: Sync mode for attributes (FORCE, IMPORT, LEGACY)
            enabled: Whether the IDP is enabled
            update_profile_first_login: Whether to show profile update on first login (default: "off")

        Returns:
            Dictionary containing provider information
        """
        logger.info(f"Adding SAML identity provider {provider_alias} to realm {realm_name}")

        # Fail closed: signature validation without a pinned certificate cannot verify
        # anything. Refuse rather than silently create an IdP that accepts forged assertions.
        if validate_signature and not signing_certificate:
            raise ValueError(
                f"SAML identity provider {provider_alias}: validate_signature is enabled but no "
                "signing_certificate was provided; refusing to create an IdP that cannot verify "
                "assertion signatures"
            )

        # Build SAML configuration
        provider_config = {
            "idpEntityId": idp_entity_id,
            "singleSignOnServiceUrl": single_sign_on_service_url,
            "singleLogoutServiceUrl": single_logout_service_url or "",
            "nameIDPolicyFormat": name_id_policy_format,
            "principalType": principal_type,
            "syncMode": sync_mode,
            "validateSignature": "true" if validate_signature else "false",
            "wantAssertionsSigned": "true" if want_assertions_signed else "false",
            "wantAssertionsEncrypted": "true" if want_assertions_encrypted else "false",
            "wantAuthnRequestsSigned": "false",
            "postBindingAuthnRequest": "false",
            "postBindingResponse": "false",
            "postBindingLogout": "false",
            "backchannelSupported": "false",
            "forceAuthn": "false",
            "allowCreate": "true",
            "loginHint": "false",
            "hideOnLoginPage": "false",
            "addExtensionsElementWithKeyInfo": "false",
            "attributeConsumingServiceIndex": "0",
        }

        # Add SP entity ID if provided
        if sp_entity_id:
            provider_config["entityId"] = sp_entity_id

        # Add signing certificate if provided
        if signing_certificate:
            provider_config["signingCertificate"] = signing_certificate
            provider_config["validateSignature"] = "true" if validate_signature else "false"

        # Merge YAML-provided overrides last so they take precedence over defaults
        if config_overrides:
            for key, value in config_overrides.items():
                if value is None:
                    continue
                provider_config[key] = str(value) if isinstance(value, bool) else value

        provider_data = {
            "alias": provider_alias,
            "displayName": display_name,
            "providerId": "saml",
            "enabled": enabled,
            "updateProfileFirstLoginMode": update_profile_first_login,
            "trustEmail": True,
            "storeToken": False,
            "addReadTokenRoleOnCreate": False,
            "authenticateByDefault": authenticate_by_default,
            "linkOnly": False,
            "firstBrokerLoginFlowAlias": first_broker_login_flow_alias,
            "config": provider_config,
        }

        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)

            try:
                self.admin.create_idp(payload=provider_data)
                logger.info(f"Created new SAML identity provider {provider_alias} in realm {realm_name}")
            except KeycloakPostError as e:
                if "409" in str(e) or "Conflict" in str(e):
                    current_provider = self.admin.get_idp(idp_alias=provider_alias)
                    current_config = current_provider.get("config", {}) or {}

                    # Compute explicit diff
                    changed_keys: list[str] = []
                    for key, desired in provider_config.items():
                        if current_config.get(key) != desired:
                            changed_keys.append(key)
                    display_name_differs = current_provider.get("displayName") != provider_data["displayName"]
                    enabled_differs = current_provider.get("enabled") != provider_data["enabled"]
                    first_broker_flow_differs = (
                        current_provider.get("firstBrokerLoginFlowAlias") != provider_data["firstBrokerLoginFlowAlias"]
                    )

                    if changed_keys or display_name_differs or enabled_differs or first_broker_flow_differs:
                        logger.info(
                            f"SAML identity provider {provider_alias} in realm {realm_name} needs update: "
                            f"changed_keys={changed_keys}, displayName_differs={display_name_differs}, "
                            f"enabled_differs={enabled_differs}, firstBrokerLoginFlow_differs={first_broker_flow_differs}"
                        )
                        current_provider["displayName"] = provider_data["displayName"]
                        current_provider["enabled"] = provider_data["enabled"]
                        current_provider["firstBrokerLoginFlowAlias"] = provider_data["firstBrokerLoginFlowAlias"]
                        current_provider["config"] = {**current_config, **provider_config}
                        self.admin.update_idp(idp_alias=provider_alias, payload=current_provider)
                        logger.info(f"Updated SAML identity provider {provider_alias} in realm {realm_name}")
                    else:
                        logger.debug(
                            f"SAML identity provider {provider_alias} in realm {realm_name} already matches desired config"
                        )
                else:
                    raise

            # Get the provider info
            provider_info = self.admin.get_idp(idp_alias=provider_alias)

            # Switch back to master
            self.admin.change_current_realm("master")

            logger.info(f"Successfully added SAML identity provider {provider_alias} to realm {realm_name}")
            return provider_info

        except KeycloakError as e:
            logger.error(f"Failed to add SAML identity provider {provider_alias} to realm {realm_name}: {e}")
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

    async def get_identity_providers(self, realm_name: str) -> list[dict[str, Any]]:
        """
        Get all identity providers configured in a realm.

        Args:
            realm_name: Name of the realm

        Returns:
            List of identity provider configurations
        """
        try:
            self.admin.change_current_realm(realm_name)
            providers = self.admin.get_idps()
            self.admin.change_current_realm("master")
            return providers or []
        except KeycloakError:
            self.admin.change_current_realm("master")
            return []

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

    async def delete_client_by_client_id(self, realm_name: str, client_id: str) -> bool:
        """
        Delete a single client by its clientId in the given realm.

        Args:
            realm_name: Name of the realm
            client_id: The clientId (not the internal UUID)

        Returns:
            True if the client was deleted, False if it was not found
        """
        target_client = await self.find_client_by_client_id(client_id, realm_name)
        if not target_client:
            logger.warning(f"Client '{client_id}' not found in realm '{realm_name}' - nothing to delete")
            return False
        try:
            self.admin.change_current_realm(realm_name)
            self.admin.delete_client(client_id=target_client["id"])
            logger.info(f"Deleted client '{client_id}' from realm '{realm_name}'")
            return True
        finally:
            self.admin.change_current_realm("master")

    async def list_realms(self) -> list[str]:
        """
        List the names of all realms (read-only).

        Returns:
            List of realm names
        """
        realms = self.admin.get_realms()
        return [realm.get("realm", "") for realm in realms or [] if realm.get("realm")]

    async def list_clients(self, realm_name: str) -> list[dict[str, Any]]:
        """
        List all clients in a realm (read-only).

        Args:
            realm_name: Name of the realm

        Returns:
            List of dicts with client_id, public flag, and internal id
        """
        try:
            self.admin.change_current_realm(realm_name)
            clients = self.admin.get_clients()
            return [
                {
                    "client_id": client.get("clientId", ""),
                    "public": bool(client.get("publicClient", False)),
                    "internal_id": client.get("id", ""),
                }
                for client in clients or []
            ]
        finally:
            self.admin.change_current_realm("master")

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

    async def ensure_browser_flow(self, realm_name: str, flow_alias: str) -> bool:
        """
        Ensure the realm's browser flow is set to the specified flow.

        This is idempotent - if the flow is already set, no change is made.

        Args:
            realm_name: Name of the realm
            flow_alias: Alias of the flow to set as browser flow (e.g., "browser", "External IDP Redirector")

        Returns:
            True if flow was changed, False if already correct
        """
        try:
            # Get current realm config
            realm_config = await self.get_realm(realm_name)
            if not realm_config:
                logger.warning(f"Realm {realm_name} not found, cannot set browser flow")
                return False

            current_flow = realm_config.get("browserFlow")
            if current_flow == flow_alias:
                logger.debug(f"Realm {realm_name} browser flow already set to '{flow_alias}'")
                return False

            # Update the browser flow
            self.admin.change_current_realm(realm_name)
            self.admin.update_realm(realm_name=realm_name, payload={"browserFlow": flow_alias})
            self.admin.change_current_realm("master")

            logger.info(f"Updated realm {realm_name} browser flow from '{current_flow}' to '{flow_alias}'")
            return True

        except Exception as e:
            logger.error(f"Failed to set browser flow for realm {realm_name}: {e}")
            self.admin.change_current_realm("master")
            raise

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
                self.admin.create_execution_config(payload=config_data, execution_id=execution_id)
                logger.info("Created Identity Provider Redirector config")
        else:
            # No config exists, create it
            logger.debug("No config exists, creating new config")
            self.admin.create_execution_config(payload=config_data, execution_id=execution_id)
            logger.info("Created Identity Provider Redirector config")

    async def create_restricted_browser_flow(
        self,
        realm_name: str,
        flow_alias: str,
        client_id: str,
        role_name: str,
        error_message: str = "${accessDeniedNoPermission}",
    ) -> None:
        """
        Create a browser flow that restricts access to users with a specific client role.

        This creates a copy of the browser flow with a conditional sub-flow that:
        1. Checks if the user does NOT have the specified client role
        2. If they don't have the role, denies access with a custom error message

        Args:
            realm_name: Name of the realm
            flow_alias: Alias for the new flow (e.g., "browser-restricted-myapp")
            client_id: Client ID for the role check
            role_name: Client role name that grants access
            error_message: Theme message key in ${key} format (default: "${accessDeniedNoPermission}")
        """
        logger.info(f"Creating restricted browser flow '{flow_alias}' for client '{client_id}' in realm '{realm_name}'")

        try:
            self.admin.change_current_realm(realm_name)

            # Step 1: Copy the browser flow
            await self._copy_browser_flow(flow_alias)

            # Step 2: Find the "forms" sub-flow in our new flow
            forms_flow_id = await self._find_forms_subflow(flow_alias)

            # Step 3: Create a conditional sub-flow for role check
            conditional_subflow_alias = f"{flow_alias}-deny-no-role"
            await self._create_conditional_deny_subflow(
                flow_alias=flow_alias,
                parent_flow_id=forms_flow_id,
                subflow_alias=conditional_subflow_alias,
                role_name=role_name,
                error_message=error_message,
                client_id=client_id,
            )

            self.admin.change_current_realm("master")
            logger.info(f"Successfully created restricted browser flow '{flow_alias}'")

        except KeycloakError as e:
            logger.error(f"Failed to create restricted browser flow '{flow_alias}': {e}")
            self.admin.change_current_realm("master")
            raise

    async def _copy_browser_flow(self, new_flow_alias: str) -> None:
        """
        Copy the browser flow to create a new flow.

        Args:
            new_flow_alias: Alias for the new flow
        """
        logger.debug(f"Copying browser flow to '{new_flow_alias}'")

        # Check if flow already exists
        flows = self.admin.get_authentication_flows()
        for flow in flows:
            if flow.get("alias") == new_flow_alias:
                logger.debug(f"Flow '{new_flow_alias}' already exists, will reuse it")
                return

        # Copy the browser flow
        copy_payload = {"newName": new_flow_alias}
        try:
            self.admin.copy_authentication_flow(payload=copy_payload, flow_alias="browser")
            logger.debug(f"Copied browser flow to '{new_flow_alias}'")
        except KeycloakPostError as e:
            if "409" in str(e) or "Conflict" in str(e):
                logger.debug(f"Flow '{new_flow_alias}' already exists")
            else:
                raise

    async def _find_forms_subflow(self, flow_alias: str) -> str:
        """
        Find the forms sub-flow ID within a browser flow copy.

        Args:
            flow_alias: Alias of the parent flow

        Returns:
            The flow ID of the forms sub-flow
        """
        executions = self.admin.get_authentication_flow_executions(flow_alias=flow_alias)

        for execution in executions:
            # The forms sub-flow is named "{flow_alias} forms" when copied
            display_name = execution.get("displayName", "")
            if "forms" in display_name.lower() and execution.get("authenticationFlow"):
                logger.debug(f"Found forms sub-flow: {display_name} (ID: {execution.get('flowId')})")
                return execution.get("flowId")

        raise KeycloakError(f"Forms sub-flow not found in flow '{flow_alias}'")

    async def _create_conditional_deny_subflow(
        self,
        flow_alias: str,
        parent_flow_id: str,
        subflow_alias: str,
        role_name: str,
        error_message: str,
        client_id: str | None = None,
    ) -> None:
        """
        Create a conditional sub-flow that denies access if user lacks a role.

        Supports both client roles and realm roles:
        - Client role: pass client_id
        - Realm role: omit client_id (None)

        Args:
            flow_alias: Alias of the top-level flow
            parent_flow_id: ID of the parent sub-flow (forms)
            subflow_alias: Alias for the new conditional sub-flow
            role_name: Role name that grants access
            error_message: Error message key from theme
            client_id: Client ID for client roles, None for realm roles
        """
        role_desc = f"{client_id}.{role_name}" if client_id else role_name
        logger.debug(f"Creating conditional deny sub-flow '{subflow_alias}' for role '{role_desc}'")

        # Get the forms sub-flow alias from the parent flow
        flows = self.admin.get_authentication_flows()
        forms_flow_alias = None
        for flow in flows:
            if flow.get("id") == parent_flow_id:
                forms_flow_alias = flow.get("alias")
                break

        if not forms_flow_alias:
            # Try to find it from executions
            executions = self.admin.get_authentication_flow_executions(flow_alias=flow_alias)
            for execution in executions:
                if execution.get("flowId") == parent_flow_id:
                    forms_flow_alias = execution.get("displayName")
                    break

        if not forms_flow_alias:
            raise KeycloakError(f"Could not find forms sub-flow alias for ID '{parent_flow_id}'")

        logger.debug(f"Forms sub-flow alias: {forms_flow_alias}")

        # Step 1: Create the conditional sub-flow
        subflow_data = {
            "alias": subflow_alias,
            "type": "basic-flow",
            "provider": "registration-page-form",
            "description": f"Deny access if user does not have role {role_desc}",
        }

        try:
            self.admin.create_authentication_flow_subflow(
                payload=subflow_data, flow_alias=forms_flow_alias, skip_exists=True
            )
            logger.debug(f"Created conditional sub-flow '{subflow_alias}'")
        except KeycloakPostError as e:
            if "409" in str(e) or "Conflict" in str(e):
                logger.debug(f"Sub-flow '{subflow_alias}' already exists")
            else:
                raise

        # Step 2: Set the sub-flow to CONDITIONAL requirement (if not already)
        executions = self.admin.get_authentication_flow_executions(flow_alias=forms_flow_alias)
        subflow_execution = None
        for execution in executions:
            if execution.get("displayName") == subflow_alias:
                subflow_execution = execution
                break

        if subflow_execution:
            current_requirement = subflow_execution.get("requirement")
            if current_requirement == "CONDITIONAL":
                logger.debug(f"Sub-flow '{subflow_alias}' is already CONDITIONAL, skipping update")
            else:
                update_data = {
                    "id": subflow_execution.get("id"),
                    "requirement": "CONDITIONAL",
                    "displayName": subflow_alias,
                    "level": subflow_execution.get("level", 1),
                    "index": subflow_execution.get("index", 0),
                    "configurable": False,
                    "authenticationFlow": True,
                }
                self.admin.update_authentication_flow_executions(payload=update_data, flow_alias=forms_flow_alias)
                logger.debug(f"Set sub-flow '{subflow_alias}' to CONDITIONAL")

        # Step 3: Add "Condition - User Role" execution (negated - check if user does NOT have role)
        await self._add_condition_user_role(
            subflow_alias=subflow_alias, role_name=role_name, negate=True, client_id=client_id
        )

        # Step 4: Add "Deny Access" execution
        await self._add_deny_access(subflow_alias=subflow_alias, error_message=error_message)

    async def _add_condition_user_role(
        self, subflow_alias: str, role_name: str, negate: bool = True, client_id: str | None = None
    ) -> None:
        """
        Add a "Condition - User Role" execution to a sub-flow.

        Supports both client roles and realm roles:
        - Client role: pass client_id, uses format "clientId.roleName"
        - Realm role: omit client_id (None), uses just "roleName"

        Args:
            subflow_alias: Alias of the sub-flow
            role_name: Role name to check
            negate: If True, condition passes when user does NOT have the role
            client_id: Client ID for client roles, None for realm roles
        """
        role_type = "client" if client_id else "realm"
        logger.debug(f"Adding Condition - User Role ({role_type}) to sub-flow '{subflow_alias}'")

        # First check if execution already exists (idempotency)
        executions = self.admin.get_authentication_flow_executions(flow_alias=subflow_alias)
        condition_execution = None
        for execution in executions:
            if execution.get("providerId") == "conditional-user-role":
                condition_execution = execution
                logger.debug(f"Condition - User Role execution already exists in '{subflow_alias}'")
                break

        # Only create if it doesn't exist
        if not condition_execution:
            execution_data = {"provider": "conditional-user-role"}
            try:
                self.admin.create_authentication_flow_execution(payload=execution_data, flow_alias=subflow_alias)
            except KeycloakPostError as e:
                if "409" not in str(e) and "Conflict" not in str(e):
                    raise

            # Re-fetch executions to get the newly created one
            executions = self.admin.get_authentication_flow_executions(flow_alias=subflow_alias)
            for execution in executions:
                if execution.get("providerId") == "conditional-user-role":
                    condition_execution = execution
                    break

        if not condition_execution:
            raise KeycloakError("Could not find conditional-user-role execution")

        # Set requirement to REQUIRED (if not already)
        current_requirement = condition_execution.get("requirement")
        if current_requirement == "REQUIRED":
            logger.debug(f"Condition - User Role is already REQUIRED in '{subflow_alias}'")
        else:
            update_data = {
                "id": condition_execution.get("id"),
                "requirement": "REQUIRED",
                "displayName": condition_execution.get("displayName"),
                "providerId": "conditional-user-role",
                "level": condition_execution.get("level", 0),
                "index": condition_execution.get("index", 0),
                "configurable": True,
                "authenticationFlow": False,
            }
            self.admin.update_authentication_flow_executions(payload=update_data, flow_alias=subflow_alias)

        # Configure the condition - format differs for client vs realm roles
        if client_id:
            # Client role: "clientId.roleName" format
            cond_user_role = f"{client_id}.{role_name}"
            config_alias = f"check-role-{client_id}-{role_name}"
        else:
            # Realm role: just "roleName"
            cond_user_role = role_name
            config_alias = f"check-realm-role-{role_name}"

        config_data = {
            "alias": config_alias,
            "config": {
                "condUserRole": cond_user_role,
                "negate": str(negate).lower(),
            },
        }

        execution_id = condition_execution.get("id")
        existing_config_id = condition_execution.get("authenticationConfig")

        if existing_config_id:
            # Update existing config
            config_data["id"] = existing_config_id
            self.admin.update_authenticator_config(payload=config_data, config_id=existing_config_id)
        else:
            # Create new config
            self.admin.create_execution_config(payload=config_data, execution_id=execution_id)

        logger.debug(f"Configured Condition - User Role: {cond_user_role}, negate={negate}")

    async def _add_deny_access(self, subflow_alias: str, error_message: str) -> None:
        """
        Add a "Deny Access" execution to a sub-flow.

        Args:
            subflow_alias: Alias of the sub-flow
            error_message: Error message in ${key} format for theme resolution
        """
        logger.debug(f"Adding Deny Access to sub-flow '{subflow_alias}'")

        # First check if execution already exists (idempotency)
        executions = self.admin.get_authentication_flow_executions(flow_alias=subflow_alias)
        deny_execution = None
        for execution in executions:
            if execution.get("providerId") == "deny-access-authenticator":
                deny_execution = execution
                logger.debug(f"Deny Access execution already exists in '{subflow_alias}'")
                break

        # Only create if it doesn't exist
        if not deny_execution:
            execution_data = {"provider": "deny-access-authenticator"}
            try:
                self.admin.create_authentication_flow_execution(payload=execution_data, flow_alias=subflow_alias)
            except KeycloakPostError as e:
                if "409" not in str(e) and "Conflict" not in str(e):
                    raise

            # Re-fetch executions to get the newly created one
            executions = self.admin.get_authentication_flow_executions(flow_alias=subflow_alias)
            for execution in executions:
                if execution.get("providerId") == "deny-access-authenticator":
                    deny_execution = execution
                    break

        if not deny_execution:
            raise KeycloakError("Could not find deny-access-authenticator execution")

        # Set requirement to REQUIRED (if not already)
        current_requirement = deny_execution.get("requirement")
        if current_requirement == "REQUIRED":
            logger.debug(f"Deny Access is already REQUIRED in '{subflow_alias}'")
        else:
            update_data = {
                "id": deny_execution.get("id"),
                "requirement": "REQUIRED",
                "displayName": deny_execution.get("displayName"),
                "providerId": "deny-access-authenticator",
                "level": deny_execution.get("level", 0),
                "index": deny_execution.get("index", 0),
                "configurable": True,
                "authenticationFlow": False,
            }
            self.admin.update_authentication_flow_executions(payload=update_data, flow_alias=subflow_alias)

        # Configure the error message (already wrapped in ${} above if needed)
        config_data = {
            "alias": f"deny-access-{subflow_alias}",
            "config": {
                "denyErrorMessage": error_message,
            },
        }

        execution_id = deny_execution.get("id")
        existing_config_id = deny_execution.get("authenticationConfig")

        if existing_config_id:
            # Update existing config
            config_data["id"] = existing_config_id
            self.admin.update_authenticator_config(payload=config_data, config_id=existing_config_id)
        else:
            # Create new config
            self.admin.create_execution_config(payload=config_data, execution_id=execution_id)

        logger.debug(f"Configured Deny Access with error message: {error_message}")

    async def create_restricted_browser_flow_realm_role(
        self,
        realm_name: str,
        flow_alias: str,
        role_name: str,
        error_message: str = "${accessDeniedNoPermission}",
    ) -> None:
        """
        Create a browser flow that restricts access to users with a specific realm role.

        This creates a copy of the browser flow with a conditional sub-flow that:
        1. Checks if the user does NOT have the specified realm role
        2. If they don't have the role, denies access with a custom error message

        This is similar to create_restricted_browser_flow but uses realm roles
        instead of client roles, enabling unified access control across multiple apps.

        Args:
            realm_name: Name of the realm
            flow_alias: Alias for the new flow (e.g., "browser-restricted-mijnbureau")
            role_name: Realm role name that grants access
            error_message: Theme message key in ${key} format (default: "${accessDeniedNoPermission}")
        """
        logger.info(
            f"Creating restricted browser flow '{flow_alias}' for realm role '{role_name}' in realm '{realm_name}'"
        )

        try:
            self.admin.change_current_realm(realm_name)

            # Step 1: Copy the browser flow
            await self._copy_browser_flow(flow_alias)

            # Step 2: Find the "forms" sub-flow in our new flow
            forms_flow_id = await self._find_forms_subflow(flow_alias)

            # Step 3: Create a conditional sub-flow for realm role check
            conditional_subflow_alias = f"{flow_alias}-deny-no-role"
            await self._create_conditional_deny_subflow(
                flow_alias=flow_alias,
                parent_flow_id=forms_flow_id,
                subflow_alias=conditional_subflow_alias,
                role_name=role_name,
                error_message=error_message,
                # client_id=None means realm role
            )

            self.admin.change_current_realm("master")
            logger.info(f"Successfully created restricted browser flow '{flow_alias}' for realm role")

        except KeycloakError as e:
            logger.error(f"Failed to create restricted browser flow '{flow_alias}': {e}")
            self.admin.change_current_realm("master")
            raise

    async def set_client_authentication_flow_override(
        self, realm_name: str, client_id: str, browser_flow_alias: str
    ) -> None:
        """
        Set the authentication flow override for a client.

        Args:
            realm_name: Name of the realm
            client_id: Client ID (not the internal UUID)
            browser_flow_alias: Alias of the browser flow to use
        """
        logger.info(f"Setting browser flow override '{browser_flow_alias}' for client '{client_id}'")

        try:
            self.admin.change_current_realm(realm_name)

            # Find the client
            clients = self.admin.get_clients()
            target_client = None
            for client in clients:
                if client.get("clientId") == client_id:
                    target_client = client
                    break

            if not target_client:
                raise KeycloakError(f"Client '{client_id}' not found in realm '{realm_name}'")

            client_uuid = target_client["id"]

            # Update the client with the flow override
            update_data = {
                "authenticationFlowBindingOverrides": {
                    "browser": browser_flow_alias,
                }
            }

            # We need to get the flow ID, not alias
            flows = self.admin.get_authentication_flows()
            flow_id = None
            for flow in flows:
                if flow.get("alias") == browser_flow_alias:
                    flow_id = flow.get("id")
                    break

            if not flow_id:
                raise KeycloakError(f"Authentication flow '{browser_flow_alias}' not found")

            update_data["authenticationFlowBindingOverrides"]["browser"] = flow_id

            self.admin.update_client(client_id=client_uuid, payload=update_data)
            logger.info(f"Set browser flow override to '{browser_flow_alias}' for client '{client_id}'")

            self.admin.change_current_realm("master")

        except KeycloakError as e:
            logger.error(f"Failed to set flow override for client '{client_id}': {e}")
            self.admin.change_current_realm("master")
            raise

    async def ensure_auto_link_first_broker_login_flow(
        self,
        realm_name: str,
        require_confirmation: bool = False,
        flow_alias: str = AUTO_LINK_FIRST_BROKER_LOGIN_FLOW,
    ) -> None:
        """
        Create (idempotently) a first-broker-login flow that auto-links a brokered SSO identity
        to a pre-existing local account matched by username/email.

        The stock "first broker login" flow routes an existing-but-unlinked account through
        "Confirm Link Existing Account" + "Verify Existing Account By Email/Re-authentication",
        which needs an email round-trip or a password a pre-created account does not have. This
        flow replaces that with Keycloak's built-in idp-auto-link authenticator, so the link
        happens silently (require_confirmation=False) or after a single confirmation screen
        (require_confirmation=True). No email or password step either way.

        Structure created:
            <flow_alias>                                 (top-level, basic-flow)
            |-- idp-review-profile                       DISABLED
            +-- <flow_alias> user creation or linking    REQUIRED    (subflow)
                |-- idp-create-user-if-unique            ALTERNATIVE
                +-- <flow_alias> handle existing account ALTERNATIVE (subflow)
                    |-- idp-confirm-link                 REQUIRED    (only if require_confirmation)
                    +-- idp-auto-link                    REQUIRED

        Only the "an account with this email already exists" branch differs from the stock
        flow; brand-new users and already-linked users are unaffected. Matching a pre-created
        account requires the realm's duplicateEmailsAllowed=false (set by the SSO setup).

        Args:
            realm_name: Name of the realm
            require_confirmation: Show the single idp-confirm-link screen before linking
            flow_alias: Alias for the top-level flow
        """
        logger.info(
            f"Ensuring auto-link first-broker-login flow '{flow_alias}' in realm '{realm_name}' "
            f"(require_confirmation={require_confirmation})"
        )

        uco_alias = f"{flow_alias} user creation or linking"
        hea_alias = f"{flow_alias} handle existing account"

        try:
            self.admin.change_current_realm(realm_name)

            # Step 1: top-level flow
            flow_data = {
                "alias": flow_alias,
                "description": "Auto-link a brokered SSO identity to a pre-existing local account",
                "providerId": "basic-flow",
                "topLevel": True,
                "builtIn": False,
            }
            try:
                self.admin.create_authentication_flow(payload=flow_data)
                logger.debug(f"Created first-broker-login flow '{flow_alias}'")
            except KeycloakPostError as e:
                if "409" in str(e) or "Conflict" in str(e):
                    logger.debug(f"Flow '{flow_alias}' already exists, will reuse it")
                else:
                    raise

            # Step 2: skip the profile-review page
            await self._ensure_execution_in_flow(
                flow_alias=flow_alias, provider="idp-review-profile", requirement="DISABLED", priority=10
            )

            # Step 3: "user creation or linking" subflow. idp-create-user-if-unique must precede
            # the handle-existing subflow so the existing-user lookup runs and stashes the match.
            # The explicit priority=10 on idp-create-user-if-unique (created before the subflow)
            # makes the subflow land at getNextPriority=11, giving a deterministic order.
            await self._ensure_subflow(
                parent_alias=flow_alias,
                subflow_alias=uco_alias,
                requirement="REQUIRED",
                description="Create the brokered user, or link to an existing account",
            )
            await self._ensure_execution_in_flow(
                flow_alias=uco_alias, provider="idp-create-user-if-unique", requirement="ALTERNATIVE", priority=10
            )

            # Step 4: "handle existing account" subflow with idp-auto-link
            await self._ensure_subflow(
                parent_alias=uco_alias,
                subflow_alias=hea_alias,
                requirement="ALTERNATIVE",
                description="Automatically link the brokered identity to the existing account",
            )
            await self._ensure_handle_existing_account_executions(
                subflow_alias=hea_alias, require_confirmation=require_confirmation
            )

            self.admin.change_current_realm("master")
            logger.info(
                f"Successfully ensured auto-link first-broker-login flow '{flow_alias}' in realm '{realm_name}'"
            )

        except KeycloakError as e:
            logger.error(
                f"Failed to ensure auto-link first-broker-login flow '{flow_alias}' in realm '{realm_name}': {e}"
            )
            self.admin.change_current_realm("master")
            raise

    async def _ensure_subflow(self, parent_alias: str, subflow_alias: str, requirement: str, description: str) -> None:
        """
        Create a basic-flow subflow under a parent (sub)flow and set its requirement (idempotent).

        Args:
            parent_alias: Alias of the parent flow or subflow
            subflow_alias: Alias for the subflow (must be unique within the realm)
            requirement: Requirement level for the subflow (REQUIRED, ALTERNATIVE, ...)
            description: Human-readable description
        """
        subflow_data = {
            "alias": subflow_alias,
            "type": "basic-flow",
            "provider": "registration-page-form",
            "description": description,
        }
        try:
            self.admin.create_authentication_flow_subflow(
                payload=subflow_data, flow_alias=parent_alias, skip_exists=True
            )
            logger.debug(f"Created subflow '{subflow_alias}' under '{parent_alias}'")
        except KeycloakPostError as e:
            if "409" not in str(e) and "Conflict" not in str(e):
                raise

        executions = self.admin.get_authentication_flow_executions(flow_alias=parent_alias)
        subflow_execution = None
        for execution in executions:
            if execution.get("displayName") == subflow_alias:
                subflow_execution = execution
                break

        if not subflow_execution:
            raise KeycloakError(f"Could not find subflow '{subflow_alias}' under '{parent_alias}' after creation")

        if subflow_execution.get("requirement") == requirement:
            return

        # Pass the full fetched object back with only the requirement changed, so the PUT keeps
        # the subflow's priority (a partial payload resets it to 0 and breaks the ordering).
        subflow_execution["requirement"] = requirement
        self.admin.update_authentication_flow_executions(payload=subflow_execution, flow_alias=parent_alias)
        logger.debug(f"Set subflow '{subflow_alias}' to {requirement}")

    async def _ensure_execution_in_flow(
        self, flow_alias: str, provider: str, requirement: str, priority: int | None = None
    ) -> None:
        """
        Add an execution (by provider id) to a flow OR subflow and set its requirement (idempotent).

        Unlike _add_execution_with_requirement this also works for subflows: it looks the flow up
        via get_authentication_flow_executions rather than get_authentication_flows (top-level only).

        An explicit `priority` is passed in the create body. Keycloak >= 25 honors it; without it
        every execution defaults to priority 0, so siblings tie and the order becomes
        non-deterministic (keycloak#43016) and cannot be fixed afterwards (raise-priority is a
        no-op on equal priorities; PUT ignores index/priority per keycloak#8726). Setting explicit,
        gapped priorities is what keycloak-config-cli does and is the reliable ordering mechanism.

        Args:
            flow_alias: Alias of the flow or subflow
            provider: Provider ID for the execution
            requirement: Requirement level (ALTERNATIVE, REQUIRED, DISABLED)
            priority: Explicit priority for deterministic ordering (Keycloak >= 25)
        """
        executions = self.admin.get_authentication_flow_executions(flow_alias=flow_alias)
        target = None
        for execution in executions:
            if execution.get("providerId") == provider:
                target = execution
                break

        if not target:
            payload: dict[str, Any] = {"provider": provider}
            if priority is not None:
                payload["priority"] = priority
            try:
                self.admin.create_authentication_flow_execution(payload=payload, flow_alias=flow_alias)
            except KeycloakPostError as e:
                if "409" not in str(e) and "Conflict" not in str(e):
                    raise
            executions = self.admin.get_authentication_flow_executions(flow_alias=flow_alias)
            for execution in reversed(executions):
                if execution.get("providerId") == provider:
                    target = execution
                    break

        if not target:
            raise KeycloakError(f"Could not find execution '{provider}' in flow '{flow_alias}' after creation")

        if target.get("requirement") == requirement:
            return

        # Pass the full fetched object back with only the requirement changed. A partial payload
        # would drop "priority" and the PUT resets it to 0, undoing the explicit ordering.
        target["requirement"] = requirement
        self.admin.update_authentication_flow_executions(payload=target, flow_alias=flow_alias)
        logger.debug(f"Set execution '{provider}' to {requirement} in flow '{flow_alias}'")

    async def _ensure_handle_existing_account_executions(self, subflow_alias: str, require_confirmation: bool) -> None:
        """
        Ensure the "handle existing account" subflow contains exactly idp-auto-link (and
        idp-confirm-link before it when require_confirmation is True), both REQUIRED and in
        order. Rebuilds the subflow contents when the set or order drifts, which also removes a
        stale idp-confirm-link when a realm switches from 'confirm' to 'automatic'.

        Args:
            subflow_alias: Alias of the "handle existing account" subflow
            require_confirmation: Include idp-confirm-link before idp-auto-link
        """
        link_providers = ("idp-confirm-link", "idp-auto-link")
        desired = ["idp-confirm-link", "idp-auto-link"] if require_confirmation else ["idp-auto-link"]

        executions = self.admin.get_authentication_flow_executions(flow_alias=subflow_alias)
        current = [e for e in executions if e.get("providerId") in link_providers]
        current_providers = [e.get("providerId") for e in current]

        if current_providers != desired:
            # Drift (missing, extra, or wrong order): delete then re-add in the desired order.
            for execution in current:
                self.admin.delete_authentication_flow_execution(execution.get("id"))
                logger.debug(f"Removed execution '{execution.get('providerId')}' from '{subflow_alias}'")

        # Explicit priorities keep idp-confirm-link before idp-auto-link deterministically.
        priorities = {"idp-confirm-link": 10, "idp-auto-link": 20}
        for provider in desired:
            await self._ensure_execution_in_flow(
                flow_alias=subflow_alias, provider=provider, requirement="REQUIRED", priority=priorities[provider]
            )

    async def create_post_broker_login_flow(
        self,
        realm_name: str,
        flow_alias: str,
        client_id: str,
        role_name: str,
        error_message: str = "${accessDeniedNoPermission}",
        skip_clients: list[str] | None = None,
    ) -> None:
        """
        Create a post-broker login flow that restricts access to users with a specific client role.

        This flow runs after SSO/IdP authentication and checks if the user has the required role.
        If they don't have the role, access is denied with a custom error message.

        Uses the custom RequireClientRoleAuthenticator SPI which properly handles both success
        and failure cases in post-broker login flows (unlike conditional sub-flows which fail
        when skipped).

        Args:
            realm_name: Name of the realm
            flow_alias: Alias for the new flow (e.g., "post-broker-restricted-myapp")
            client_id: Client ID for the role check
            role_name: Client role name that grants access
            error_message: Theme message key in ${key} format (default: "${accessDeniedNoPermission}")
            skip_clients: List of OAuth client IDs that should bypass this role check (e.g., invite client)
        """
        logger.info(f"Creating post-broker login flow '{flow_alias}' for client '{client_id}' in realm '{realm_name}'")

        try:
            self.admin.change_current_realm(realm_name)

            # Step 1: Create the post-broker login flow
            flow_data = {
                "alias": flow_alias,
                "description": f"Post-broker login flow restricting access to users with {client_id}.{role_name}",
                "providerId": "basic-flow",
                "topLevel": True,
                "builtIn": False,
            }

            try:
                self.admin.create_authentication_flow(payload=flow_data)
                logger.debug(f"Created post-broker login flow '{flow_alias}'")
            except KeycloakPostError as e:
                if "409" in str(e) or "Conflict" in str(e):
                    logger.debug(f"Flow '{flow_alias}' already exists, will reuse it")
                else:
                    raise

            # Step 2: Add the custom RequireClientRoleAuthenticator execution
            # This authenticator explicitly handles both success (user has role) and
            # failure (user lacks role) cases, avoiding the conditional sub-flow bug
            await self._add_require_role_authenticator(
                flow_alias=flow_alias,
                role_name=role_name,
                error_message=error_message,
                client_id=client_id,
                skip_clients=skip_clients,
            )

            self.admin.change_current_realm("master")
            logger.info(f"Successfully created post-broker login flow '{flow_alias}'")

        except KeycloakError as e:
            logger.error(f"Failed to create post-broker login flow '{flow_alias}': {e}")
            self.admin.change_current_realm("master")
            raise

    async def _add_require_role_authenticator(
        self,
        flow_alias: str,
        role_name: str,
        error_message: str,
        client_id: str | None = None,
        skip_clients: list[str] | None = None,
    ) -> None:
        """
        Add the custom RequireClientRoleAuthenticator to a flow.

        This authenticator checks if the user has a specific role and:
        - Calls success() if the user has the role (allowing the flow to complete)
        - Calls failure() with an error page if the user lacks the role
        - Skips the role check if the OAuth client is in the skip_clients list

        Supports both client roles and realm roles:
        - Client role: pass client_id
        - Realm role: omit client_id (None)

        This approach works correctly for post-broker login flows, unlike conditional
        sub-flows which fail when skipped.

        Args:
            flow_alias: Alias of the parent flow to add the authenticator to
            role_name: Role name that grants access
            error_message: Error message in ${key} format for theme resolution
            client_id: Client ID for client roles, None for realm roles
            skip_clients: List of OAuth client IDs that should bypass this role check
        """
        role_desc = f"{client_id}.{role_name}" if client_id else role_name
        logger.debug(f"Adding RequireClientRoleAuthenticator to flow '{flow_alias}' for role '{role_desc}'")

        # Step 1: Check if execution already exists (idempotency)
        executions = self.admin.get_authentication_flow_executions(flow_alias=flow_alias)
        authenticator_execution = None
        for execution in executions:
            if execution.get("providerId") == "require-client-role-authenticator":
                authenticator_execution = execution
                logger.debug(f"RequireClientRoleAuthenticator already exists in flow '{flow_alias}'")
                break

        # Only create if it doesn't exist
        if not authenticator_execution:
            execution_data = {"provider": "require-client-role-authenticator"}
            self.admin.create_authentication_flow_execution(payload=execution_data, flow_alias=flow_alias)
            logger.debug("Added RequireClientRoleAuthenticator execution")

            # Re-fetch executions to get the newly created one
            executions = self.admin.get_authentication_flow_executions(flow_alias=flow_alias)
            for execution in executions:
                if execution.get("providerId") == "require-client-role-authenticator":
                    authenticator_execution = execution
                    break

        if not authenticator_execution:
            raise KeycloakError("Failed to find RequireClientRoleAuthenticator execution after creation")

        # Step 2: Set requirement to REQUIRED (if not already)
        current_requirement = authenticator_execution.get("requirement")
        if current_requirement == "REQUIRED":
            logger.debug(f"RequireClientRoleAuthenticator is already REQUIRED in flow '{flow_alias}'")
        else:
            update_data = {
                "id": authenticator_execution.get("id"),
                "requirement": "REQUIRED",
                "displayName": authenticator_execution.get("displayName", "Require Client Role"),
                "level": authenticator_execution.get("level", 0),
                "index": authenticator_execution.get("index", 0),
                "configurable": True,
                "authenticationFlow": False,
            }
            self.admin.update_authentication_flow_executions(payload=update_data, flow_alias=flow_alias)
            logger.debug("Set RequireClientRoleAuthenticator to REQUIRED")

        # Step 3: Configure the authenticator with client ID, role name, error message, and skip clients
        execution_id = authenticator_execution.get("id")
        existing_config_id = authenticator_execution.get("authenticationConfig")
        config_alias = f"require-role-{client_id or 'realm'}-{role_name}"
        config_data = {
            "alias": config_alias,
            "config": {
                "roleName": role_name,
                "errorMessage": error_message,
            },
        }

        # Add clientId only if provided (empty = realm role)
        if client_id:
            config_data["config"]["clientId"] = client_id

        # Add skipClients if provided
        if skip_clients:
            config_data["config"]["skipClients"] = ",".join(skip_clients)
            logger.debug(f"Configuring skipClients: {skip_clients}")

        if existing_config_id:
            # Update existing config
            config_data["id"] = existing_config_id
            self.admin.update_authenticator_config(payload=config_data, config_id=existing_config_id)
            logger.debug(f"Updated RequireRoleAuthenticator config: role={role_desc}")
        else:
            # Create new config
            url = f"admin/realms/{self.admin.connection.realm_name}/authentication/executions/{execution_id}/config"
            self.admin.connection.raw_post(url, data=json.dumps(config_data))
            logger.debug(f"Created RequireRoleAuthenticator config: role={role_desc}")

    async def set_identity_provider_post_broker_login_flow(
        self, realm_name: str, provider_alias: str, flow_alias: str
    ) -> None:
        """
        Set the post-broker login flow for an identity provider.

        This flow runs after every successful SSO authentication through this IdP.

        Args:
            realm_name: Name of the realm
            provider_alias: Alias of the identity provider
            flow_alias: Alias of the post-broker login flow to use
        """
        logger.info(f"Setting post-broker login flow '{flow_alias}' for IdP '{provider_alias}' in realm '{realm_name}'")

        try:
            self.admin.change_current_realm(realm_name)

            # Get the identity provider
            try:
                idp = self.admin.get_idp(idp_alias=provider_alias)
            except KeycloakGetError:
                raise KeycloakError(f"Identity provider '{provider_alias}' not found in realm '{realm_name}'")

            # Update the identity provider with the post-broker login flow
            idp["postBrokerLoginFlowAlias"] = flow_alias

            self.admin.update_idp(idp_alias=provider_alias, payload=idp)
            logger.info(f"Set post-broker login flow to '{flow_alias}' for IdP '{provider_alias}'")

            self.admin.change_current_realm("master")

        except KeycloakError as e:
            logger.error(f"Failed to set post-broker login flow for IdP '{provider_alias}': {e}")
            self.admin.change_current_realm("master")
            raise

    async def create_post_broker_login_flow_realm_role(
        self,
        realm_name: str,
        flow_alias: str,
        role_name: str,
        error_message: str = "${accessDeniedNoPermission}",
        skip_clients: list[str] | None = None,
    ) -> None:
        """
        Create a post-broker login flow that restricts access to users with a specific realm role.

        This flow runs after SSO/IdP authentication and checks if the user has the required
        realm role. If they don't have the role, access is denied with a custom error message.

        Uses the custom RequireClientRoleAuthenticator SPI with empty client ID for realm roles,
        which allows unified access control across multiple applications.

        Args:
            realm_name: Name of the realm
            flow_alias: Alias for the new flow (e.g., "post-broker-restricted-realm")
            role_name: Realm role name that grants access
            error_message: Theme message key in ${key} format (default: "${accessDeniedNoPermission}")
            skip_clients: List of OAuth client IDs that should bypass this role check (e.g., invite client)
        """
        logger.info(
            f"Creating post-broker login flow '{flow_alias}' for realm role '{role_name}' in realm '{realm_name}'"
        )

        try:
            self.admin.change_current_realm(realm_name)

            # Step 1: Create the post-broker login flow
            flow_data = {
                "alias": flow_alias,
                "description": f"Post-broker login flow restricting access to users with realm role {role_name}",
                "providerId": "basic-flow",
                "topLevel": True,
                "builtIn": False,
            }

            try:
                self.admin.create_authentication_flow(payload=flow_data)
                logger.debug(f"Created post-broker login flow '{flow_alias}'")
            except KeycloakPostError as e:
                if "409" in str(e) or "Conflict" in str(e):
                    logger.debug(f"Flow '{flow_alias}' already exists, will reuse it")
                else:
                    raise

            # Step 2: Add the custom RequireClientRoleAuthenticator execution (with empty client_id for realm role)
            # This authenticator explicitly handles both success (user has role) and
            # failure (user lacks role) cases, avoiding the conditional sub-flow bug
            await self._add_require_role_authenticator(
                flow_alias=flow_alias,
                role_name=role_name,
                error_message=error_message,
                # client_id=None means realm role
                skip_clients=skip_clients,
            )

            self.admin.change_current_realm("master")
            logger.info(f"Successfully created post-broker login flow '{flow_alias}' for realm role")

        except KeycloakError as e:
            logger.error(f"Failed to create post-broker login flow '{flow_alias}': {e}")
            self.admin.change_current_realm("master")
            raise

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

    # ==================== Client Role Operations ====================

    async def create_client_role(
        self, realm_name: str, client_id: str, role_name: str, description: str | None = None
    ) -> bool:
        """
        Create a client role for the specified client.

        Args:
            realm_name: Name of the realm
            client_id: Client ID (not the internal UUID)
            role_name: Name of the role to create
            description: Optional description for the role

        Returns:
            True if role was created or already exists
        """
        logger.info(f"Creating client role '{role_name}' for client '{client_id}' in realm '{realm_name}'")

        try:
            self.admin.change_current_realm(realm_name)

            # Find the client by clientId
            clients = self.admin.get_clients()
            target_client = None
            for client in clients:
                if client.get("clientId") == client_id:
                    target_client = client
                    break

            if not target_client:
                raise KeycloakError(f"Client '{client_id}' not found in realm '{realm_name}'")

            client_uuid = target_client["id"]

            role_data = {
                "name": role_name,
                "description": description or f"Client role: {role_name}",
                "composite": False,
                "clientRole": True,
            }

            try:
                self.admin.create_client_role(client_role_id=client_uuid, payload=role_data)
                logger.info(f"Created client role '{role_name}' for client '{client_id}'")
            except KeycloakPostError as e:
                if "409" in str(e) or "Conflict" in str(e):
                    logger.info(f"Client role '{role_name}' already exists for client '{client_id}'")
                else:
                    raise

            self.admin.change_current_realm("master")
            return True

        except KeycloakError as e:
            logger.error(f"Failed to create client role '{role_name}': {e}")
            self.admin.change_current_realm("master")
            raise

    async def assign_client_role_to_user(self, realm_name: str, client_id: str, user_id: str, role_name: str) -> None:
        """
        Assign a client role to a user.

        Args:
            realm_name: Name of the realm
            client_id: Client ID (not the internal UUID)
            user_id: User ID (internal UUID)
            role_name: Name of the client role to assign
        """
        logger.info(f"Assigning client role '{role_name}' to user '{user_id}' for client '{client_id}'")

        try:
            self.admin.change_current_realm(realm_name)

            # Find the client by clientId
            clients = self.admin.get_clients()
            target_client = None
            for client in clients:
                if client.get("clientId") == client_id:
                    target_client = client
                    break

            if not target_client:
                raise KeycloakError(f"Client '{client_id}' not found in realm '{realm_name}'")

            client_uuid = target_client["id"]

            # Get the client role
            roles = self.admin.get_client_roles(client_id=client_uuid)
            target_role = None
            for role in roles:
                if role.get("name") == role_name:
                    target_role = role
                    break

            if not target_role:
                raise KeycloakError(f"Client role '{role_name}' not found for client '{client_id}'")

            # Assign the role to the user
            self.admin.assign_client_role(user_id=user_id, client_id=client_uuid, roles=[target_role])
            logger.info(f"Assigned client role '{role_name}' to user '{user_id}'")

            self.admin.change_current_realm("master")

        except KeycloakError as e:
            logger.error(f"Failed to assign client role '{role_name}' to user: {e}")
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

    async def get_user_by_email(self, realm_name: str, email: str) -> dict[str, Any] | None:
        """
        Find a user by email address in the specified realm.

        Args:
            realm_name: Name of the realm
            email: Email address to search for

        Returns:
            User information dictionary or None if not found
        """
        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)

            users = self.admin.get_users(query={"email": email, "exact": "true"})

            # Switch back to master
            self.admin.change_current_realm("master")

            if users and len(users) > 0:
                logger.debug(f"Found user with email '{email}' in realm '{realm_name}'")
                return users[0]

            logger.debug(f"User with email '{email}' not found in realm '{realm_name}'")
            return None

        except KeycloakError as e:
            logger.error(f"Failed to search for user with email '{email}': {e}")
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

    async def assign_realm_admin_from_master(
        self, target_realm_name: str, user_id: str, role_names: list[str] | None = None
    ) -> bool:
        """
        Assign realm management permissions to a master realm user for a specific realm.

        This creates a delegated admin who can login to the main Keycloak admin console
        (/admin/) but can only see and manage the specified realm.

        In master realm, each realm has a corresponding client named '{realm}-realm'
        with roles that grant management access to that realm.

        Args:
            target_realm_name: Name of the realm to grant management access to
            user_id: ID of the user (must exist in master realm)
            role_names: List of role names to assign (default: all management roles)

        Returns:
            True if roles were assigned successfully
        """
        logger.info(f"Assigning realm management permissions for '{target_realm_name}' to master realm user {user_id}")

        try:
            # Ensure we're in master realm
            self.admin.change_current_realm("master")

            # Find the {realm}-realm client in master (grants management access to target realm)
            realm_client_id = f"{target_realm_name}-realm"
            clients = self.admin.get_clients()
            realm_client = None
            for client in clients:
                if client.get("clientId") == realm_client_id:
                    realm_client = client
                    break

            if not realm_client:
                raise KeycloakError(
                    f"Realm management client '{realm_client_id}' not found in master realm. "
                    f"Make sure the realm '{target_realm_name}' exists."
                )

            client_uuid = realm_client["id"]

            # Get available client roles
            available_roles = self.admin.get_client_roles(client_id=client_uuid)

            if not available_roles:
                logger.warning(f"No roles found for client {realm_client_id}")
                return True

            # Filter to requested roles if specified, otherwise assign all
            if role_names:  # noqa: SIM108
                roles_to_assign = [r for r in available_roles if r["name"] in role_names]
            else:
                # Assign all management roles for full realm admin access
                roles_to_assign = available_roles

            if roles_to_assign:
                self.admin.assign_client_role(user_id=user_id, client_id=client_uuid, roles=roles_to_assign)
                logger.info(
                    f"Assigned {len(roles_to_assign)} realm management roles for '{target_realm_name}' "
                    f"to user {user_id}: {[r['name'] for r in roles_to_assign]}"
                )
            else:
                logger.warning(f"No matching roles found for: {role_names}")

            return True

        except KeycloakError as e:
            logger.error(f"Failed to assign realm management roles: {e}")
            raise

    async def assign_realm_roles_to_user(
        self, realm_name: str, user_id: str, role_names: list[str]
    ) -> dict[str, list[str]]:
        """
        Assign realm roles to a user.

        Args:
            realm_name: Name of the realm
            user_id: ID of the user
            role_names: List of role names to assign

        Returns:
            Dict with 'assigned' and 'not_found' lists of role names

        Raises:
            KeycloakError: If role assignment fails
        """
        logger.info(f"Assigning realm roles {role_names} to user {user_id} in realm {realm_name}")

        result: dict[str, list[str]] = {"assigned": [], "not_found": []}

        try:
            # Switch to target realm
            self.admin.change_current_realm(realm_name)

            # Get all realm roles
            all_roles = self.admin.get_realm_roles()

            # Filter to requested roles and track what's missing
            roles_to_assign = []
            for role_name in role_names:
                matching_role = next((r for r in all_roles if r["name"] == role_name), None)
                if matching_role:
                    roles_to_assign.append(matching_role)
                    result["assigned"].append(role_name)
                else:
                    result["not_found"].append(role_name)
                    logger.warning(f"Realm role '{role_name}' not found in realm '{realm_name}'")

            if roles_to_assign:
                self.admin.assign_realm_roles(user_id=user_id, roles=roles_to_assign)
                logger.info(f"Assigned {len(roles_to_assign)} realm roles to user {user_id}")

            if result["not_found"]:
                logger.error(f"Realm roles not found: {result['not_found']}")

            # Switch back to master
            self.admin.change_current_realm("master")

            return result

        except KeycloakError as e:
            logger.error(f"Failed to assign realm roles: {e}")
            # Switch back to master
            self.admin.change_current_realm("master")
            raise

    async def assign_realm_role_to_user(self, realm_name: str, user_id: str, role_name: str) -> bool:
        """
        Assign a single realm role to a user.

        Args:
            realm_name: Name of the realm
            user_id: ID of the user
            role_name: Name of the realm role to assign

        Returns:
            True if role was assigned successfully

        Raises:
            KeycloakError: If role assignment fails or role not found
        """
        logger.info(f"Assigning realm role '{role_name}' to user {user_id} in realm {realm_name}")

        try:
            self.admin.change_current_realm(realm_name)

            # Get the specific realm role
            role = self.admin.get_realm_role(role_name=role_name)
            if not role:
                raise KeycloakError(f"Realm role '{role_name}' not found in realm '{realm_name}'")

            # Assign the role to the user
            self.admin.assign_realm_roles(user_id=user_id, roles=[role])
            logger.info(f"Successfully assigned realm role '{role_name}' to user {user_id}")

            self.admin.change_current_realm("master")
            return True

        except KeycloakError as e:
            logger.error(f"Failed to assign realm role '{role_name}': {e}")
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
