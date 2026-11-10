"""Realms, clients, identity providers, flows, roles, groups and users on Keycloak."""

import logging
import secrets
import string
from enum import Enum
from typing import Any

import aiohttp
from keycloak import KeycloakAdmin, KeycloakOpenIDConnection
from keycloak.exceptions import KeycloakError, KeycloakGetError, KeycloakPostError

from opi.connectors.http import client_session
from opi.core.config import settings
from opi.utils.redirect_uris import derive_web_origin
from opi.utils.totp import build_credential_representation

logger = logging.getLogger(__name__)

#: Budget for a discovery or JWKS read. These serve from memory, so a slow answer means
#: the realm is not serving rather than busy.
DISCOVERY_TIMEOUT_SECONDS = 10.0

# Alias of the custom first-broker-login flow that auto-links a brokered SSO identity to a
# pre-existing local account matched by username/email, replacing the stock flow's
# confirm-link + verify-by-email/re-authentication steps. See features/keycloak-auto-link.md.
AUTO_LINK_FIRST_BROKER_LOGIN_FLOW = "first broker login auto-link"

#: The identity provider a project realm reaches SSO Rijk through. Every project-realm
#: blueprint creates it under this alias (``sso-only.yaml``, ``sso-support.yaml``), and the
#: flow that redirects to it names it as its ``defaultProvider``.
PLATFORM_IDP_ALIAS = "rig-platform-oidc"

#: Browser flow with required OTP on every password login (built by ensure_required_otp_browser_flow).
REQUIRED_OTP_BROWSER_FLOW = "Browser with required OTP"

#: Keycloak's own browser flow, for every blueprint that asks for nothing else.
DEFAULT_BROWSER_FLOW = "browser"

#: Browser flows that show a username/password form. A realm bound to anything else (such as
#: "External IDP Redirector") redirects straight to the IdP, so a local account cannot log in
#: there. Read by whoever decides whether a local account is a way in.
PASSWORD_FORM_BROWSER_FLOWS = frozenset({DEFAULT_BROWSER_FLOW, REQUIRED_OTP_BROWSER_FLOW})

#: Required action that lets a user configure a TOTP authenticator.
CONFIGURE_TOTP = "CONFIGURE_TOTP"

#: Credential types that represent OTP factors in Keycloak.
_OTP_CREDENTIAL_TYPES = frozenset({"otp", "totp", "hotp"})

#: Prefix of service account usernames (no browser login, so OTP does not apply).
_SERVICE_ACCOUNT_PREFIX = "service-account-"


def _redirect_uris_and_origins(
    ingress_hosts: list[str],
    support_http: bool,
    additional_redirect_uris: list[str] | None,
) -> tuple[list[str], list[str]]:
    """Build redirect URIs and web origins from ingress hosts and additional URIs."""
    redirect_uris: set[str] = set()
    web_origins: set[str] = set()

    for host in ingress_hosts:
        redirect_uris.add(f"https://{host}/*")
        web_origins.add(f"https://{host}")
        if support_http:
            redirect_uris.add(f"http://{host}/*")
            web_origins.add(f"http://{host}")

    for uri in additional_redirect_uris or []:
        redirect_uris.add(uri)
        web_origins.add(derive_web_origin(uri))

    return sorted(redirect_uris), sorted(web_origins)


def role_gate_flow_alias(client_id: str) -> str:
    """Alias of the role-restricted browser flow for this client.

    Replace such a flow, never update one: the predecessor ``browser-restricted-<client>``
    sat in the forms subflow, where the Cookie step skipped it and a session from another
    client passed the check unseen.
    """
    return f"browser-role-gate-{client_id}"


def legacy_restricted_flow_alias(client_id: str) -> str:
    """The flow ``role_gate_flow_alias`` replaced; only an older realm still has it."""
    return f"browser-restricted-{client_id}"


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
        client_id: str | None = None,
        client_secret: str | None = None,
    ):
        """Authenticate against the master realm.

        With the client-credentials service account (client_id plus client_secret), or as a
        fallback with the admin username and password (direct grant).
        """
        self.keycloak_url = keycloak_url.rstrip("/")
        self.admin_username = admin_username
        self.admin_password = admin_password

        if client_secret:
            if not client_id:
                msg = "client_id is required alongside client_secret for client-credentials auth"
                raise ValueError(msg)
            # Client-credentials mode: OPI's own service account, independent of
            # any human-admin OTP policy.
            connection = KeycloakOpenIDConnection(
                server_url=self.keycloak_url,
                realm_name="master",
                # Always authenticate against master, exactly like the admin-password path
                # below. Without this python-keycloak takes the token realm from
                # ``realm_name`` (openid_connection: ``user_realm_name or realm_name``), and
                # ``change_current_realm()`` -- which OPI calls for every project realm --
                # reassigns it. The next token refresh then asks a PROJECT realm for
                # ``opi-admin-service``, which only exists in master, and Keycloak answers
                # client_not_found. The first token always succeeds, so the failure only
                # appears after a realm switch plus a token expiry.
                user_realm_name="master",
                client_id=client_id,
                client_secret_key=client_secret,
                grant_type="client_credentials",
                verify=True,
            )
            self.admin = KeycloakAdmin(connection=connection)
            logger.debug(f"Initialized KeycloakConnector for {keycloak_url} (client-credentials: {client_id})")
        else:
            # Admin username/password mode (bootstrap/break-glass fallback).
            self.admin = KeycloakAdmin(
                server_url=self.keycloak_url,
                username=self.admin_username,
                password=self.admin_password,
                realm_name="master",
                user_realm_name="master",  # Always authenticate against master realm
                verify=True,
            )
            logger.debug(f"Initialized KeycloakConnector for {keycloak_url} (admin user: {admin_username})")

    def _refresh_admin_token(self) -> None:
        """Mint a fresh admin access token on the underlying connection.

        Needed after creating a realm: see the call site in :meth:`create_realm` for why a
        token issued earlier cannot administer a realm created later. Best-effort -- if the
        refresh itself fails, the caller carries on with the existing token and the
        operation surfaces its own error rather than this one masking it.
        """
        try:
            self.admin.connection.get_token()
        except KeycloakError as e:
            logger.warning(f"Could not refresh the Keycloak admin token: {e}")

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
        """Create a realm, or bring an existing one in line (a 409 is not a failure).

        Args:
            add_master_idp: Whether to add the master OIDC IDP (default: False)
            sso_session_idle_timeout: Optional SSO session idle timeout in seconds. When
                omitted, Keycloak's default (30 min) is kept.
            sso_session_max_lifespan: Optional SSO session max lifespan in seconds. When
                omitted, Keycloak's default (10 hours) is kept.
            events_enabled: Optional; store user events (login, UPDATE_PROFILE, ...).
            events_expiration: Optional user-event retention in seconds.
            admin_events_enabled: Optional; store admin events.
            admin_events_details_enabled: Optional; include representations in admin events.
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

        # The SEED a realm is born with. Four of these are also decided by the blueprint
        # (see ``_BLUEPRINT_REALM_FIELDS`` in the YAML handler), and that is not a second
        # source of truth: the handler applies the blueprint immediately after this call,
        # on the create path and on the 409 path alike, so whatever a blueprint says wins
        # within the same run. They stay here as the CLOSED value a realm has in the
        # meantime, and as what a realm gets whose blueprint says nothing about them.
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
            try:
                self.admin.create_realm(payload=realm_data)
                logger.info(f"Created new realm: {realm_name}")
            except KeycloakPostError as e:
                if "409" in str(e) or "Conflict" in str(e):
                    logger.info(f"Realm {realm_name} already exists, using existing realm")
                    # Apply only the session and audit-event settings to the existing
                    # realm. A full realm_data update would reset browserFlow etc. and
                    # break the custom "External IDP Redirector" flow on the platform realm.
                    # The blueprint-decided fields and the smtpServer are NOT applied here:
                    # they are applied by ``_apply_realm_self_service`` right after this
                    # call, which is also the reconcile path, so an existing realm picks
                    # them up whether it got here or not.
                    replay_settings = {**session_settings, **event_settings}
                    if replay_settings:
                        self.admin.update_realm(realm_name=realm_name, payload=replay_settings)
                        logger.info(f"Updated settings on existing realm {realm_name}: {replay_settings}")
                else:
                    raise

            # A token minted before this realm existed cannot administer it. Keycloak
            # creates a ``<realm>-realm`` client in master alongside every new realm and
            # carries its admin roles in ``resource_access``; an access token issued
            # earlier simply does not have them, so every follow-up call on the fresh
            # realm answers 403. Measured on the sandbox: same service account, same
            # master 'admin' role, POST /admin/realms -> 201 but
            # GET /admin/realms/<new>/users/profile -> 403 with the token that created it,
            # and 200 with a token minted one second later.
            #
            # This bites service accounts only -- an admin USER with the master 'admin'
            # role is a super-admin over realms that did not exist when its token was
            # issued, which is why this never showed before OPI moved to a
            # client-credentials service account. Refreshing unconditionally keeps the two
            # auth paths behaving the same and costs one token request per realm created.
            self._refresh_admin_token()

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

    async def set_required_action_enabled(self, realm_name: str, alias: str, enabled: bool) -> None:
        """Enable or disable a required action provider in a realm.

        Disabling UPDATE_PASSWORD is how self-service password setting is closed off: Keycloak
        hides the account console's password button (CredentialTypeMetadata drops createAction /
        updateAction for providers that are not enabled) and rejects the hand-crafted
        ``kc_action=UPDATE_PASSWORD`` application-initiated action on the login endpoint. Both
        entry points are needed, since removing manage-account only takes away the first.

        Fails closed for the same reason as :meth:`_lock_identity_fields`: a realm that silently
        keeps self-service password setting lets a federated user set a local password and log in
        without SSO Rijk, so an API failure aborts realm provisioning.
        """
        await self._update_required_action(realm_name, alias, {"enabled": enabled})

    async def set_required_action_default(self, realm_name: str, alias: str, default: bool) -> None:
        """Set whether a required action is attached to a NEW user.

        That is how a new local account in a realm with mandatory OTP sets up an
        authenticator app at its first login.
        """
        await self._update_required_action(realm_name, alias, {"defaultAction": default})

    async def _update_required_action(self, realm_name: str, alias: str, changes: dict[str, Any]) -> None:
        """Write fields onto a required action provider, and only when they differ."""
        try:
            self.admin.change_current_realm(realm_name)
            action = self.admin.get_required_action_by_alias(alias)
            if action is None:
                logger.warning(f"Required action {alias} not found in realm {realm_name}, nothing to change")
                return
            if all(action.get(field) == value for field, value in changes.items()):
                return
            action.update(changes)
            self.admin.update_required_action(alias, payload=action)
            logger.info(f"Set required action {alias} to {changes} in realm {realm_name}")
        except KeycloakError as e:
            logger.error(f"Could not set required action {alias} in realm {realm_name}: {e}")
            raise
        finally:
            self.admin.change_current_realm("master")

    async def remove_default_role(self, realm_name: str, client_id: str, role_name: str) -> None:
        """Remove a client role from the realm's ``default-roles-<realm>`` composite.

        Removing ``account:manage-account`` is what stops a user from linking or unlinking an
        identity provider themselves: LinkedAccountsResource requires that role for both add and
        remove. Measured in the sandbox, the effect is broader than "read-only console": without
        the role the access token carries no account client roles at all, so it lacks the
        ``account`` audience and every account REST endpoint answers 401, ``view-profile``
        notwithstanding. The account console is inert rather than read-only. Nothing in the ZAD
        portal links to it, so that is acceptable, but it is more than it looks.

        Idempotent: the role is looked up in the composite first and nothing is written when it is
        already gone. Fails closed, as above.
        """
        try:
            self.admin.change_current_realm(realm_name)
            client_uuid = self.admin.get_client_id(client_id)
            if client_uuid is None:
                logger.warning(f"Client {client_id} not found in realm {realm_name}, cannot remove role {role_name}")
                return

            default_role_id = self.admin.get_default_realm_role_id()
            composites = self.admin.get_role_composites_by_id(default_role_id)
            # Match on the owning client too: a realm role may carry the same name.
            target = next(
                (
                    role
                    for role in composites
                    if role.get("name") == role_name and role.get("containerId") == client_uuid
                ),
                None,
            )
            if target is None:
                return

            self.admin.remove_realm_default_roles(payload=[target])
            logger.info(f"Removed default role {client_id}:{role_name} from realm {realm_name}")
        except KeycloakError as e:
            logger.error(f"Could not remove default role {client_id}:{role_name} from realm {realm_name}: {e}")
            raise
        finally:
            self.admin.change_current_realm("master")

    async def delete_realm(self, realm_name: str) -> bool:
        """Delete a realm.

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
        """Whether the realm exists.

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

    async def realm_discovery_available(self, realm_name: str) -> bool:
        """Second opinion on whether a realm exists, independent of the admin API.

        Every realm serves its own OIDC discovery document, and that document needs
        no token, no master-realm session and no role: a 200 there proves the realm
        is up and serving, whatever the admin API just said about it. Callers use
        this to confirm a negative before anything acts on "the realm is gone" -
        re-creating a realm that is in fact healthy is the expensive mistake.

        Returns False when the endpoint answers 404 AND when it cannot be reached
        at all: this only ever overrules a negative, never a positive, so an
        unreachable endpoint simply leaves the earlier answer standing.
        """
        discovery_url = self.get_discovery_url(realm_name)
        try:
            async with (
                client_session(DISCOVERY_TIMEOUT_SECONDS) as session,
                session.get(discovery_url) as response,
            ):
                if response.status == 200:
                    logger.debug(f"Realm '{realm_name}' serves its OIDC discovery document")
                    return True
                logger.debug(f"Discovery document for realm '{realm_name}' returned HTTP {response.status}")
                return False
        except (aiohttp.ClientError, TimeoutError) as e:
            logger.warning(
                f"Could not reach the discovery document for realm '{realm_name}': {str(e) or type(e).__name__}"
            )
            return False

    async def get_realm(self, realm_name: str) -> dict[str, Any] | None:
        """The realm's configuration, or ``None`` when it does not exist."""
        try:
            return self.admin.get_realm(realm_name=realm_name)
        except KeycloakGetError:
            return None

    async def update_realm_settings(self, realm_name: str, payload: dict[str, Any]) -> None:
        """Apply a partial realm representation to an existing realm.

        Partial on purpose: Keycloak merges what it is given, so only the named fields
        move. A full representation would reset ``browserFlow`` and friends and break the
        custom "External IDP Redirector" flow on the platform realm -- the same reason the
        409 branch of ``create_realm`` never sends one.

        Args:
            payload: The fields to set. An empty payload is a no-op, so a caller that
                found no difference does not have to check.
        """
        if not payload:
            return
        self.admin.update_realm(realm_name=realm_name, payload=payload)
        logger.info(f"Updated realm {realm_name}: {sorted(payload)}")

    def _converge_oidc_client(
        self,
        existing_client: dict[str, Any],
        redirect_uris: list[str],
        web_origins: list[str],
    ) -> None:
        """Sync redirect URIs and web origins of an existing client to match the project file.

        Flows and attributes are not touched, so an existing client keeps them unchanged."""
        if set(existing_client.get("redirectUris", [])) == set(redirect_uris) and set(
            existing_client.get("webOrigins", [])
        ) == set(web_origins):
            logger.info(f"Client '{existing_client.get('clientId')}' redirect URIs and web origins are already correct")
            return

        # These two keys only: Keycloak leaves an omitted field as it is, so the existing
        # client keeps its attributes and its flows.
        self.admin.update_client(
            client_id=existing_client["id"],
            payload={"redirectUris": redirect_uris, "webOrigins": web_origins},
        )
        logger.info(f"Updated redirect URIs and web origins of client '{existing_client.get('clientId')}'")

    def get_discovery_url(self, realm_name: str) -> str:
        discovery_url = f"{self.keycloak_url}/realms/{realm_name}/.well-known/openid-configuration"
        logger.debug(f"Discovery URL for realm '{realm_name}': {discovery_url}")
        return discovery_url

    async def create_oidc_client(
        self,
        realm_name: str,
        client_id: str,
        client_name: str | None = None,
        redirect_uris: list[str] | None = None,
        web_origins: list[str] | None = None,
        direct_access_grants: bool = False,
        service_accounts: bool = False,
    ) -> dict[str, Any]:
        """Create an OIDC client, or converge an existing one.

        Args:
            redirect_uris: Allowed redirect URIs, required and never a wildcard
            web_origins: Allowed web origins; derived from the redirect URIs when omitted
            direct_access_grants: Allow the password grant (ROPC) on a NEW client
            service_accounts: Allow client credentials on a NEW client

        Raises:
            ValueError: When no redirect URIs are given. A client without them is a
                configuration error, not a reason to accept every callback URL.
        """
        logger.info(f"Creating OIDC client '{client_id}' in realm '{realm_name}'")

        if not redirect_uris:
            raise ValueError(f"OIDC client '{client_id}' needs at least one redirect URI")
        if not web_origins:
            web_origins = sorted({derive_web_origin(uri) for uri in redirect_uris})

        client_secret = self._generate_client_secret()

        client_data = {
            "clientId": client_id,
            "name": client_name or client_id,
            "protocol": "openid-connect",
            "enabled": True,
            "publicClient": False,
            "secret": client_secret,
            "redirectUris": redirect_uris,
            "webOrigins": web_origins,
            "standardFlowEnabled": True,
            "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": direct_access_grants,
            "serviceAccountsEnabled": service_accounts,
        }

        try:
            self.admin.change_current_realm(realm_name)

            try:
                self.admin.create_client(payload=client_data)
                logger.info(f"Successfully created OIDC client '{client_id}'")
                client_data["created"] = True
            except KeycloakPostError as e:
                if "409" in str(e) or "Conflict" in str(e):
                    logger.info(f"Client '{client_id}' already exists in realm '{realm_name}', checking for changes")
                    all_clients = self.admin.get_clients()
                    existing_client = next((c for c in all_clients if c.get("clientId") == client_id), None)
                    if existing_client:
                        existing_secret = self.admin.get_client_secrets(existing_client["id"])
                        client_data["secret"] = existing_secret.get("value", "")
                        client_data["created"] = False
                        self._converge_oidc_client(
                            existing_client=existing_client,
                            redirect_uris=redirect_uris,
                            web_origins=web_origins,
                        )
                    else:
                        raise
                else:
                    raise

            self.admin.change_current_realm("master")
            return client_data

        except KeycloakError as e:
            logger.error(f"Failed to create OIDC client '{client_id}': {e}")
            self.admin.change_current_realm("master")
            raise

    async def delete_oidc_client(self, realm_name: str, client_id: str) -> bool:
        """Delete an OIDC client by its clientId; False when no such client was there."""
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
        """The clientIds in a realm that start with ``prefix``, to sweep up ephemeral clients."""
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
        logout_path: str | None = None,
        direct_access_grants: bool = False,
        service_accounts: bool = False,
        require_pkce: bool = False,
    ) -> dict[str, Any]:
        """Create a deployment's OIDC client, public and confidential pair included.

        Args:
            realm_name: Realm name (required, must be explicitly provided)
            support_http: Whether to generate both HTTP and HTTPS redirect URIs (default: False, HTTPS only)
            additional_redirect_uris: Optional list of additional redirect URIs (e.g., localhost URLs for development)
            logout_path: Optional path on the application that ends its own session; becomes this
                client's front-channel logout URL on the first ingress host.
            direct_access_grants: Allow the password grant (ROPC) on a NEW client
            service_accounts: Allow client credentials on a NEW client
            require_pkce: Whether Keycloak must refuse an authorization code without PKCE S256.
                Only the platform's own clients set this; of a project deployment the
                application itself decides.
        """
        client_id = f"{project_name}-{deployment_name}"
        client_secret = self._generate_client_secret()

        logger.info(f"Creating client '{client_id}' for deployment '{deployment_name}' in project '{project_name}'")
        logger.info(f"Received ingress_hosts: {ingress_hosts}")
        logger.info(f"HTTP support: {support_http}")
        if additional_redirect_uris:
            logger.info(f"Additional redirect URIs: {additional_redirect_uris}")

        redirect_uris, web_origins = _redirect_uris_and_origins(ingress_hosts, support_http, additional_redirect_uris)

        logger.info(f"Final redirect_uris: {redirect_uris}")
        logger.info(f"Final web_origins: {web_origins}")

        # Keycloak accepts one front-channel logout URL per client, so with several ingress
        # hosts one has to be picked: the first. The application clears its session
        # server-side, so which of its own hostnames gets called makes no difference.
        frontchannel_logout_url = f"https://{ingress_hosts[0]}{logout_path}" if logout_path and ingress_hosts else ""

        # The front-channel key is ALWAYS stated, empty when there is no path: otherwise the 409
        # comparison below cannot see the field disappear and the client keeps a stale URL.
        expected_attributes: dict[str, str] = {
            "post.logout.redirect.uris": "+",
            "frontchannel.logout.url": frontchannel_logout_url,
        }
        if require_pkce:
            expected_attributes["pkce.code.challenge.method"] = "S256"
        if frontchannel_logout_url:
            logger.info(f"Front-channel logout URL for '{client_id}': {frontchannel_logout_url}")

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
            # Off by default and opt-in per project: an application that sends the user to
            # Keycloak needs neither the password grant nor a service account. The update
            # branch below deliberately leaves them out of its comparison, so an existing
            # client keeps its flows.
            "directAccessGrantsEnabled": direct_access_grants,
            "serviceAccountsEnabled": service_accounts,
            # A precondition for frontchannel.logout.url, and within the federation the reason
            # a realm session survives a logout. _ensure_public_client sets it too, and a
            # change only counts when both move: see the fork in
            # features/keycloak-always-clear-session-logout.md.
            "frontchannelLogout": True,
            "attributes": expected_attributes,
        }

        try:
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

                        existing_redirect_uris = set(existing_client.get("redirectUris", []))
                        existing_web_origins = set(existing_client.get("webOrigins", []))
                        existing_attrs = existing_client.get("attributes", {}) or {}
                        expected_redirect_uris = set(redirect_uris)
                        expected_web_origins = set(web_origins)

                        uris_differ = existing_redirect_uris != expected_redirect_uris
                        origins_differ = existing_web_origins != expected_web_origins
                        # Every attribute this method owns, not just post.logout.redirect.uris:
                        # a client created before an attribute existed would otherwise never
                        # pick it up.
                        attrs_differ = any(
                            existing_attrs.get(key) != value for key, value in expected_attributes.items()
                        )

                        if uris_differ or origins_differ or attrs_differ:
                            logger.info(
                                f"Client '{client_id}' needs updating. "
                                f"Redirect URIs current: {existing_redirect_uris}, expected: {expected_redirect_uris}"
                            )
                            self.admin.change_current_realm(realm_name)
                            merged_attrs = {**existing_attrs, **expected_attributes}
                            update_data = {
                                "redirectUris": redirect_uris,
                                "webOrigins": web_origins,
                                "attributes": merged_attrs,
                            }
                            self.admin.update_client(client_id=existing_client["id"], payload=update_data)
                            logger.info(f"Successfully updated client '{client_id}'")
                            updated = True
                        else:
                            logger.info(f"Client '{client_id}' redirect URIs and attributes are already correct")

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

            self.admin.change_current_realm("master")

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

            await self._assign_custom_scope_to_client(client_id, realm_name)
            await self._assign_custom_scope_to_client(public_client_id, realm_name)

            return result

        except KeycloakError as e:
            logger.error(f"Failed to create client '{client_id}' for deployment '{deployment_name}': {e}")
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
            # The second carrier of the flag, next to create_deployment_client. A change only
            # counts when both move: see the fork in
            # features/keycloak-always-clear-session-logout.md.
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
        """Delete a deployment's client; False when no such client was there.

        ``realm_name`` has no default: the caller names the realm.
        """
        client_id = f"{project_name}-{deployment_name}"

        logger.info(f"Deleting client '{client_id}' for deployment '{deployment_name}' in project '{project_name}'")

        try:
            self.admin.change_current_realm(realm_name)

            target_client = await self.find_client_by_client_id(client_id, realm_name)

            if not target_client:
                logger.warning(f"Client '{client_id}' not found in realm '{realm_name}'")
                self.admin.change_current_realm("master")
                return False

            # find_client_by_client_id switches back to master, so re-switch to target realm
            self.admin.change_current_realm(realm_name)

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

            self.admin.change_current_realm("master")
            return True

        except KeycloakError as e:
            logger.error(f"Failed to delete client '{client_id}' for deployment '{deployment_name}': {e}")
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
        """Update the ingress hosts of an existing deployment client.

        Args:
            realm_name: Realm name (uses default if None)
            support_http: Whether to generate both HTTP and HTTPS redirect URIs (default: False, HTTPS only)
            additional_redirect_uris: Optional list of additional redirect URIs (e.g., localhost URLs for development)
        """
        realm_name = realm_name or settings.KEYCLOAK_DEFAULT_REALM
        client_id = f"{project_name}-{deployment_name}"

        logger.info(f"Updating hosts for client '{client_id}' in deployment '{deployment_name}'")
        logger.info(f"Received ingress_hosts for update: {ingress_hosts}")
        logger.info(f"HTTP support: {support_http}")
        if additional_redirect_uris:
            logger.info(f"Additional redirect URIs: {additional_redirect_uris}")

        try:
            self.admin.change_current_realm(realm_name)

            target_client = await self.find_client_by_client_id(client_id, realm_name)

            if not target_client:
                logger.error(f"Client '{client_id}' not found in realm '{realm_name}'")
                self.admin.change_current_realm("master")
                return False

            redirect_uris, web_origins = _redirect_uris_and_origins(
                ingress_hosts, support_http, additional_redirect_uris
            )

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

            self.admin.change_current_realm("master")

            logger.info(f"Successfully updated hosts for client '{client_id}'")
            return True

        except KeycloakError as e:
            logger.error(f"Failed to update hosts for client '{client_id}': {e}")
            self.admin.change_current_realm("master")
            raise

    async def find_client_by_client_id(self, client_id: str, realm_name: str | None = None) -> dict[str, Any] | None:
        """The client with this clientId, or ``None`` when no client has it.

        Without ``realm_name`` the configured default realm.
        """
        realm_name = realm_name or settings.KEYCLOAK_DEFAULT_REALM

        try:
            self.admin.change_current_realm(realm_name)

            all_clients = self.admin.get_clients()

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
            self.admin.change_current_realm("master")
            return None

    async def get_client_secret(self, client_internal_id: str, realm_name: str | None = None) -> str | None:
        """A client's secret, or ``None`` when it could not be read.

        Without ``realm_name`` the configured default realm.
        """
        realm_name = realm_name or settings.KEYCLOAK_DEFAULT_REALM

        try:
            self.admin.change_current_realm(realm_name)

            secret_data = self.admin.get_client_secrets(client_id=client_internal_id)

            self.admin.change_current_realm("master")

            client_secret = secret_data.get("value")
            if client_secret:
                logger.debug(f"Successfully retrieved client secret for client {client_internal_id}")
                return client_secret

            logger.error(f"No client secret found for client {client_internal_id}")
            return None

        except KeycloakError as e:
            logger.error(f"Failed to retrieve client secret for client {client_internal_id}: {e}")
            self.admin.change_current_realm("master")
            return None

    async def regenerate_client_secret(self, client_internal_id: str, realm_name: str) -> str:
        """Have Keycloak make a new client secret and return it.

        The old secret stops working: Keycloak replaces it.
        """
        try:
            self.admin.change_current_realm(realm_name)
            secret_data = self.admin.generate_client_secrets(client_id=client_internal_id)
        except KeycloakError as e:
            raise RuntimeError(f"Failed to regenerate client secret for client {client_internal_id}: {e}") from e
        finally:
            self.admin.change_current_realm("master")

        client_secret = secret_data.get("value")
        if not client_secret:
            raise RuntimeError(f"Keycloak returned no secret when regenerating it for client {client_internal_id}")

        logger.info(f"Regenerated client secret for client {client_internal_id} in realm {realm_name}")
        return client_secret

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
        """Add an OIDC identity provider to a realm.

        Args:
            provider_type: Type of provider (default: "oidc")
            update_profile_first_login: Whether to show profile update on first login (default: "off")
            config_overrides: Optional dict merged into provider config after defaults (YAML wins)
        """
        logger.info(f"Adding identity provider {provider_alias} to realm {realm_name}")

        provider_config: dict[str, Any] = {
            "clientId": client_id,
            "clientSecret": client_secret,
            "discoveryEndpoint": discovery_url,
            "validateSignature": "true",
            "useJwksUrl": "true",
            "syncMode": "IMPORT",
            "backchannelSupported": "false",
        }

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

            provider_info = self.admin.get_idp(idp_alias=provider_alias)

            self.admin.change_current_realm("master")

            logger.info(f"Successfully added identity provider {provider_alias} to realm {realm_name}")
            return provider_info

        except KeycloakError as e:
            logger.error(f"Failed to add identity provider {provider_alias} to realm {realm_name}: {e}")
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
        """Add a SAML identity provider to a realm.

        Args:
            name_id_policy_format: NameID policy format (default: persistent)
            principal_type: Principal type (SUBJECT, ATTRIBUTE, FRIENDLY_ATTRIBUTE)
            sp_entity_id: Our SP entity ID (defaults to realm issuer)
            sync_mode: Sync mode for attributes (FORCE, IMPORT, LEGACY)
            update_profile_first_login: Whether to show profile update on first login (default: "off")
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

        if sp_entity_id:
            provider_config["entityId"] = sp_entity_id

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

            provider_info = self.admin.get_idp(idp_alias=provider_alias)

            self.admin.change_current_realm("master")

            logger.info(f"Successfully added SAML identity provider {provider_alias} to realm {realm_name}")
            return provider_info

        except KeycloakError as e:
            logger.error(f"Failed to add SAML identity provider {provider_alias} to realm {realm_name}: {e}")
            self.admin.change_current_realm("master")
            raise

    async def update_identity_provider(
        self, realm_name: str, provider_alias: str, provider_type: str = "oidc", config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        logger.info(f"Updating identity provider {provider_alias} in realm {realm_name}")

        try:
            self.admin.change_current_realm(realm_name)

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

            if config:
                current_provider["config"].update(config)

            self.admin.update_idp(idp_alias=provider_alias, payload=current_provider)

            provider_info = self.admin.get_idp(idp_alias=provider_alias)

            self.admin.change_current_realm("master")

            logger.info(f"Successfully updated identity provider {provider_alias} in realm {realm_name}")
            return provider_info

        except KeycloakError as e:
            logger.error(f"Failed to update identity provider {provider_alias} in realm {realm_name}: {e}")
            self.admin.change_current_realm("master")
            raise

    async def get_identity_providers(self, realm_name: str) -> list[dict[str, Any]]:
        try:
            self.admin.change_current_realm(realm_name)
            providers = self.admin.get_idps()
            self.admin.change_current_realm("master")
            return providers or []
        except KeycloakError:
            self.admin.change_current_realm("master")
            return []

    async def get_identity_provider_mappers(self, realm_name: str, provider_alias: str) -> list[dict[str, Any]]:
        try:
            self.admin.change_current_realm(realm_name)
            mappers = self.admin.get_idp_mappers(idp_alias=provider_alias)
            self.admin.change_current_realm("master")
            return mappers or []
        except KeycloakError:
            self.admin.change_current_realm("master")
            return []

    async def create_identity_provider_mapper(
        self, realm_name: str, provider_alias: str, mapper_config: dict[str, Any]
    ) -> dict[str, Any] | None:
        try:
            self.admin.change_current_realm(realm_name)
            result = self.admin.add_mapper_to_idp(idp_alias=provider_alias, payload=mapper_config)
            self.admin.change_current_realm("master")
            return result
        except KeycloakError as e:
            logger.error(f"Failed to create IDP mapper: {e}")
            self.admin.change_current_realm("master")
            return None

    async def update_identity_provider_mapper(
        self, realm_name: str, provider_alias: str, mapper_id: str, mapper_config: dict[str, Any]
    ) -> bool:
        try:
            self.admin.change_current_realm(realm_name)
            self.admin.update_mapper_in_idp(idp_alias=provider_alias, mapper_id=mapper_id, payload=mapper_config)
            self.admin.change_current_realm("master")
            return True
        except KeycloakError:
            self.admin.change_current_realm("master")
            return False

    async def ensure_standard_oidc_mappers(self, realm_name: str, provider_alias: str) -> bool:
        logger.info(f"Ensuring standard OIDC mappers for {provider_alias} in realm {realm_name}")

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

        for mapper in expected_mappers:
            mapper_name = mapper["name"]
            if mapper_name in existing_mapper_names:
                logger.debug(f"Mapper {mapper_name} already exists, skipping")
            else:
                logger.info(f"Creating mapper: {mapper_name}")
                await self.create_identity_provider_mapper(realm_name, provider_alias, mapper)
                logger.debug(f"Created mapper: {mapper_name}")

        return True

    async def create_custom_client_scope(
        self,
        realm_name: str,
        scope_name: str = "custom_attributes_passthrough",
        realm_type: RealmType = RealmType.PROJECT,
    ) -> dict[str, Any] | None:
        """Create the custom_attributes_passthrough client scope; ``None`` when that failed."""
        try:
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

            existing_scopes = self.admin.get_client_scopes()
            for scope in existing_scopes:
                if scope.get("name") == scope_name:
                    logger.info(f"Client scope '{scope_name}' already exists in realm '{realm_name}'")
                    await self._add_custom_attributes_mappers(realm_name, scope["id"], realm_type)
                    await self.assign_client_scope_as_realm_default(realm_name, scope["id"], default=True)
                    self.admin.change_current_realm("master")
                    return scope

            logger.info(f"Creating custom client scope '{scope_name}' in realm '{realm_name}'")
            self.admin.create_client_scope(payload=client_scope_data)

            created_scopes = self.admin.get_client_scopes()
            for scope in created_scopes:
                if scope.get("name") == scope_name:
                    logger.info(f"Successfully created client scope '{scope_name}'")
                    await self._add_custom_attributes_mappers(realm_name, scope["id"], realm_type)
                    await self.assign_client_scope_as_realm_default(realm_name, scope["id"], default=True)
                    self.admin.change_current_realm("master")
                    return scope

            self.admin.change_current_realm("master")
            return None

        except KeycloakError as e:
            logger.error(f"Failed to create client scope '{scope_name}': {e}")
            self.admin.change_current_realm("master")
            return None

    async def _add_custom_attributes_mappers(self, realm_name: str, scope_id: str, realm_type: RealmType) -> None:
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

        try:
            existing_mappers = self.admin.get_mappers_from_client_scope(client_scope_id=scope_id)
            existing_mapper_names = {mapper.get("name") for mapper in (existing_mappers or [])}
        except KeycloakError:
            existing_mapper_names = set()

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
        """Delete one client by its clientId; False when no such client was there."""
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
        """List the names of all realms (read-only)."""
        realms = self.admin.get_realms()
        return [realm.get("realm", "") for realm in realms or [] if realm.get("realm")]

    async def list_clients(self, realm_name: str) -> list[dict[str, Any]]:
        """Per client in the realm its ``client_id``, whether it is public, and its internal id."""
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
        try:
            self.admin.change_current_realm(realm_name)
            scopes = self.admin.get_client_scopes()
            self.admin.change_current_realm("master")
            return scopes or []
        except KeycloakError:
            self.admin.change_current_realm("master")
            return []

    async def assign_client_scope_to_client(
        self, realm_name: str, client_internal_id: str, scope_id: str, default: bool = True
    ) -> bool:
        """Assign a client scope to a client, as its default scope or as an optional one."""
        try:
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

            self.admin.change_current_realm("master")

            logger.info(f"Successfully assigned client scope as {scope_type}")
            return True

        except KeycloakError as e:
            logger.error(f"Failed to assign client scope: {e}")
            self.admin.change_current_realm("master")
            return False

    async def assign_client_scope_as_realm_default(self, realm_name: str, scope_id: str, default: bool = True) -> bool:
        """Make a client scope the realm's default scope, or its optional one.

        True when the scope IS the realm default afterwards, which includes the case where it
        already was.
        """
        scope_type = "default" if default else "optional"
        try:
            self.admin.change_current_realm(realm_name)

            logger.info(f"Assigning client scope '{scope_id}' as realm-level {scope_type} client scope")

            if default:
                self.admin.add_default_default_client_scope(scope_id=scope_id)
            else:
                self.admin.add_default_optional_client_scope(scope_id=scope_id)

            self.admin.change_current_realm("master")

            logger.info(f"Successfully assigned client scope as realm-level {scope_type} client scope")
            return True

        except KeycloakError as e:
            # A 409 is the wanted end state here: the scope already is the realm default.
            if getattr(e, "response_code", None) == 409:
                logger.info(f"Client scope '{scope_id}' is already realm-level {scope_type}, skipping")
                self.admin.change_current_realm("master")
                return True
            logger.error(f"Failed to assign client scope as realm default: {e}")
            self.admin.change_current_realm("master")
            return False

    async def _assign_custom_scope_to_client(self, client_id: str, realm_name: str) -> None:
        try:
            logger.info(f"Assigning custom client scope to client '{client_id}'")

            client = await self.find_client_by_client_id(client_id, realm_name)
            if not client:
                logger.warning(f"Client '{client_id}' not found, cannot assign custom scope")
                return

            scopes = await self.get_client_scopes(realm_name)
            custom_scope = None
            for scope in scopes:
                if scope.get("name") == "custom_attributes_passthrough":
                    custom_scope = scope
                    break

            if not custom_scope:
                logger.warning(f"Custom client scope 'custom_attributes_passthrough' not found in realm '{realm_name}'")
                return

            success = await self.assign_client_scope_to_client(
                realm_name, client["id"], custom_scope["id"], default=True
            )

            if success:
                logger.info(f"Successfully assigned custom client scope to client '{client_id}'")
            else:
                logger.warning(f"Failed to assign custom client scope to client '{client_id}'")

        except Exception as e:
            logger.warning(f"Error assigning custom client scope to client '{client_id}': {e}")

    async def configure_sso_redirect_flow(self, realm_name: str, provider_alias: str) -> None:
        """Point the realm at SSO-only authentication, redirecting to ``provider_alias``."""
        logger.info(f"Configuring External IDP Redirector flow for realm {realm_name}")

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

        self.admin.change_current_realm("master")

        logger.info(f"Successfully configured External IDP Redirector flow for realm {realm_name}")

    async def _create_external_idp_redirector_flow(self, realm_name: str, provider_alias: str) -> None:
        """Create the External IDP Redirector flow, redirecting to ``provider_alias``."""
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
        """Point the realm's browser flow at ``flow_alias``; False when it already was.

        ``flow_alias`` is a flow name such as "browser" or "External IDP Redirector".
        """
        try:
            realm_config = await self.get_realm(realm_name)
            if not realm_config:
                logger.warning(f"Realm {realm_name} not found, cannot set browser flow")
                return False

            current_flow = realm_config.get("browserFlow")
            if current_flow == flow_alias:
                logger.debug(f"Realm {realm_name} browser flow already set to '{flow_alias}'")
                return False

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
        """Add an execution to a flow; ``requirement`` is ALTERNATIVE, REQUIRED or DISABLED."""
        # Already switched to target realm

        flows = self.admin.get_authentication_flows()
        target_flow = None
        for flow in flows:
            if flow.get("alias") == flow_alias:
                target_flow = flow
                break

        if not target_flow:
            raise KeycloakError(f"Flow '{flow_alias}' not found")

        executions = self.admin.get_authentication_flow_executions(flow_alias=flow_alias)

        target_execution = None
        for execution in executions:
            if execution.get("providerId") == provider:
                target_execution = execution
                break

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
            if target_execution.get("requirement") == requirement:
                logger.debug(f"Execution '{provider}' already has requirement '{requirement}'")
                return

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
        """Give the Identity Provider Redirector ``provider_alias`` as its defaultProvider."""
        logger.info(f"Configuring Identity Provider Redirector in flow '{flow_alias}'")

        # Already switched to target realm

        executions = self.admin.get_authentication_flow_executions(flow_alias=flow_alias)

        redirector_execution = None
        for execution in executions:
            if execution.get("providerId") == "identity-provider-redirector":
                redirector_execution = execution
                break

        if not redirector_execution:
            raise KeycloakError(f"Identity Provider Redirector execution not found in flow '{flow_alias}'")

        execution_id = redirector_execution.get("id")
        logger.debug(f"Found Identity Provider Redirector execution: {execution_id}")

        # Keycloak 26 refuses a config alias that already exists in the realm, and the realm
        # flow and every role gate have their own redirector.
        config_alias = f"redirector-{flow_alias}"
        config_data = {
            "alias": config_alias,
            "config": {"defaultProvider": provider_alias},
        }

        existing_config_id = redirector_execution.get("authenticationConfig")

        if existing_config_id:
            # Config exists, check if it needs updating
            try:
                existing_config = self.admin.get_authenticator_config(config_id=existing_config_id)
                current_default_provider = existing_config.get("config", {}).get("defaultProvider")

                if current_default_provider == provider_alias:
                    logger.debug(f"Config already has correct defaultProvider '{provider_alias}'")
                    return

                update_data = {
                    "id": existing_config_id,
                    "alias": config_alias,
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

    async def _clear_redirector_execution(self, flow_alias: str) -> None:
        """Take the ``defaultProvider`` off the Identity Provider Redirector, if it has one.

        The other direction of ``_configure_redirector_execution``, and needed for the same
        reason: a project that moves from ``sso-only`` to ``sso-support`` has to get its login
        screen back. Without this side the role gate keeps redirecting to SSO Rijk, because
        nothing else ever removes such a config.

        Does nothing when there is no redirector or no config, which is the normal state of a
        gate that always belonged to ``sso-support``.
        """
        executions = self.admin.get_authentication_flow_executions(flow_alias=flow_alias)
        redirector = next((e for e in executions if e.get("providerId") == "identity-provider-redirector"), None)
        if redirector is None:
            return

        config_id = redirector.get("authenticationConfig")
        if not config_id:
            return

        self.admin.delete_authenticator_config(config_id=config_id)
        logger.info(f"Removed Identity Provider Redirector config from flow '{flow_alias}': the login screen returns")

    async def create_restricted_browser_flow(
        self,
        realm_name: str,
        flow_alias: str,
        client_id: str,
        role_name: str,
        error_message: str = "${accessDeniedNoPermission}",
        redirect_to_idp: str | None = None,
        require_otp: bool = False,
    ) -> None:
        """Create a browser flow that only admits holders of a client role.

        Args:
            flow_alias: Alias for the new flow (see ``role_gate_flow_alias``)
            client_id: Client ID for the role check
            role_name: Client role name that grants access
            error_message: Theme message key in ${key} format (default: "${accessDeniedNoPermission}")
            redirect_to_idp: Identity provider alias to send the user straight to, or None to
                show Keycloak's own login screen (see ``_build_role_gate_flow``)
            require_otp: Require an OTP on every password login instead of only from users who
                already configured one (see ``_ensure_browser_authenticators``)
        """
        await self._build_role_gate_flow(
            realm_name=realm_name,
            flow_alias=flow_alias,
            role_name=role_name,
            error_message=error_message,
            client_id=client_id,
            redirect_to_idp=redirect_to_idp,
            require_otp=require_otp,
        )

    async def _build_role_gate_flow(
        self,
        realm_name: str,
        flow_alias: str,
        role_name: str,
        error_message: str,
        client_id: str | None = None,
        redirect_to_idp: str | None = None,
        require_otp: bool = False,
    ) -> None:
        """Build a browser flow in which the role check sits NEXT TO the authenticators.

        The structure, and why it is this one::

            <flow>
              <flow>-authenticate            REQUIRED
                Cookie                          ALTERNATIVE
                Identity Provider Redirector    ALTERNATIVE
                <flow>-forms                    ALTERNATIVE
                  Username Password Form           REQUIRED
                  <flow>-otp                       CONDITIONAL
                    Condition - user configured       REQUIRED
                    OTP Form                          REQUIRED
              <flow>-deny-no-role            CONDITIONAL
                Condition - user role (negate)  REQUIRED
                Deny Access                     REQUIRED

        The login steps themselves come from ``_ensure_browser_authenticators``, the same one
        that builds the realm flow of ``sso-support-otp``. ``require_otp`` makes the OTP step
        there mandatory, so the gate demands what the realm around it demands.

        The role check belongs at the same level as the login itself, because only there does
        it run no matter HOW someone got in. Inside the forms subflow the Cookie step skips
        it, and an existing realm session passes unchecked.

        The ``-authenticate`` wrapper is not decoration: Keycloak ignores ALTERNATIVE steps as
        soon as something at that same level is REQUIRED. Putting the role check straight next
        to Cookie and forms therefore switches all of those off and refuses everyone, role
        holders included (measured on 25.0.6: a user WITH the role got "Invalid username or
        password"). With the authenticators together in a REQUIRED subflow they are alone
        among themselves again, and the conditional role check sits beside them instead of
        between them.

        Inside ``-deny-no-role`` the order does not matter: Keycloak picks the condition of a
        CONDITIONAL subflow on type and not on position. Those two steps therefore carry no
        explicit priority.

        Every step is looked up before it is created, so applying this again, which every
        reprocess does, duplicates nothing.

        ``redirect_to_idp`` IS THE PROJECT'S TEMPLATE, AND IT HAS TO COME ALONG

        This flow hangs on the client as ``browserFlowOverride``, and a client override beats
        the realm's browser flow. So everything ``sso-only`` sets on the REALM, the flow
        "External IDP Redirector" with ``defaultProvider`` on the identity provider, does not
        hold for these clients. The ``identity-provider-redirector`` above then stands without
        a config, and such a redirector does nothing: the flow falls through to the password
        form, and a project with ``template: sso-only`` AND ``restrict-access`` gets exactly
        the login screen the blueprint promises to take away.

        With an alias in it the redirector gets the same ``defaultProvider`` as the realm
        flow, and the gate redirects just as well. Without an alias (``sso-support``) it stays
        deliberately unconfigured: there the login screen IS supposed to appear.
        """
        role_desc = f"{client_id}.{role_name}" if client_id else role_name
        logger.info(f"Building role gate browser flow '{flow_alias}' for role '{role_desc}' in realm '{realm_name}'")

        authenticate = f"{flow_alias}-authenticate"
        forms = f"{flow_alias}-forms"
        otp = f"{flow_alias}-otp"
        deny = f"{flow_alias}-deny-no-role"

        try:
            self.admin.change_current_realm(realm_name)

            if not any(flow.get("alias") == flow_alias for flow in self.admin.get_authentication_flows()):
                self.admin.create_authentication_flow(
                    payload={
                        "alias": flow_alias,
                        "providerId": "basic-flow",
                        "topLevel": True,
                        "builtIn": False,
                        "description": f"Browser flow that only admits holders of role {role_desc}",
                    }
                )
                logger.debug(f"Created top-level flow '{flow_alias}'")

            # Order carries the security here: with ``-deny-no-role`` BEFORE
            # ``-authenticate`` the role check runs before there is a user and EVERYONE is
            # refused. It is pinned the same way as in
            # ``ensure_auto_link_first_broker_login_flow``: an explicit priority on the
            # executions, and subflows on their creation order, for which the server returns
            # getNextPriority (highest + 1). What DOES lose that priority is a hand-written,
            # partial PUT on the requirement, which is why everything here goes through
            # ``_ensure_execution_in_flow`` and ``_ensure_subflow``, which send the fetched
            # object back whole.
            await self._ensure_subflow(flow_alias, authenticate, "REQUIRED", "Authenticate the user")
            await self._ensure_browser_authenticators(
                realm_name=realm_name,
                parent_alias=authenticate,
                forms_alias=forms,
                otp_alias=otp,
                redirect_to_idp=redirect_to_idp,
                require_otp=require_otp,
            )

            await self._ensure_subflow(flow_alias, deny, "CONDITIONAL", f"Deny access without role {role_desc}")
            await self._add_condition_user_role(
                subflow_alias=deny, role_name=role_name, negate=True, client_id=client_id
            )
            await self._add_deny_access(subflow_alias=deny, error_message=error_message)

            self.admin.change_current_realm("master")
            logger.info(f"Role gate browser flow '{flow_alias}' is in place")

        except KeycloakError as e:
            logger.error(f"Failed to build role gate browser flow '{flow_alias}': {e}")
            self.admin.change_current_realm("master")
            raise

    async def delete_authentication_flow_by_alias(self, realm_name: str, flow_alias: str) -> bool:
        """Delete a top-level flow when it exists; says whether anything was removed.

        Cleaning up may never be the reason a rollout fails, so a refusal by Keycloak (because
        a client still hangs off the flow, for instance) is logged and not passed on.
        """
        try:
            self.admin.change_current_realm(realm_name)
            flow = next((f for f in self.admin.get_authentication_flows() if f.get("alias") == flow_alias), None)
            if flow is None:
                return False
            self.admin.delete_authentication_flow(flow_id=flow["id"])
            logger.info(f"Deleted authentication flow '{flow_alias}' from realm '{realm_name}'")
            return True
        except KeycloakError as e:
            logger.warning(f"Could not delete authentication flow '{flow_alias}' in realm '{realm_name}': {e}")
            return False
        finally:
            self.admin.change_current_realm("master")

    async def _add_condition_user_role(
        self, subflow_alias: str, role_name: str, negate: bool = True, client_id: str | None = None
    ) -> None:
        """Add a "Condition - User Role" execution to a sub-flow.

        With ``client_id`` the condition is on the client role ``clientId.roleName``; without
        it on the realm role ``roleName``. ``negate`` passes when the user does NOT have it.

        Args:
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
        # Client role: "clientId.roleName" format, realm role: just "roleName"
        cond_user_role = f"{client_id}.{role_name}" if client_id else role_name

        # Keycloak 26 refuses a config alias that already exists in the realm, and the
        # deployments of a project share a realm and usually a role.
        config_data = {
            "alias": f"check-role-{subflow_alias}",
            "config": {
                "condUserRole": cond_user_role,
                "negate": str(negate).lower(),
            },
        }

        execution_id = condition_execution.get("id")
        existing_config_id = condition_execution.get("authenticationConfig")

        if existing_config_id:
            config_data["id"] = existing_config_id
            self.admin.update_authenticator_config(payload=config_data, config_id=existing_config_id)
        else:
            self.admin.create_execution_config(payload=config_data, execution_id=execution_id)

        logger.debug(f"Configured Condition - User Role: {cond_user_role}, negate={negate}")

    async def _add_deny_access(self, subflow_alias: str, error_message: str) -> None:
        """Add a "Deny Access" execution; ``error_message`` is a ${key} the theme resolves."""
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
            config_data["id"] = existing_config_id
            self.admin.update_authenticator_config(payload=config_data, config_id=existing_config_id)
        else:
            self.admin.create_execution_config(payload=config_data, execution_id=execution_id)

        logger.debug(f"Configured Deny Access with error message: {error_message}")

    async def create_restricted_browser_flow_realm_role(
        self,
        realm_name: str,
        flow_alias: str,
        role_name: str,
        error_message: str = "${accessDeniedNoPermission}",
        redirect_to_idp: str | None = None,
        require_otp: bool = False,
    ) -> None:
        """Create a browser flow that only admits holders of a realm role.

        A realm role covers every app in the realm at once.

        Args:
            flow_alias: Alias for the new flow (see ``role_gate_flow_alias``)
            role_name: Realm role name that grants access
            error_message: Theme message key in ${key} format (default: "${accessDeniedNoPermission}")
            redirect_to_idp: Identity provider alias to send the user straight to, or None to
                show Keycloak's own login screen (see ``_build_role_gate_flow``)
            require_otp: Require an OTP on every password login instead of only from users who
                already configured one (see ``_ensure_browser_authenticators``)
        """
        await self._build_role_gate_flow(
            realm_name=realm_name,
            flow_alias=flow_alias,
            role_name=role_name,
            error_message=error_message,
            redirect_to_idp=redirect_to_idp,
            require_otp=require_otp,
        )

    async def set_client_authentication_flow_override(
        self, realm_name: str, client_id: str, browser_flow_alias: str
    ) -> None:
        logger.info(f"Setting browser flow override '{browser_flow_alias}' for client '{client_id}'")

        try:
            self.admin.change_current_realm(realm_name)

            clients = self.admin.get_clients()
            target_client = None
            for client in clients:
                if client.get("clientId") == client_id:
                    target_client = client
                    break

            if not target_client:
                raise KeycloakError(f"Client '{client_id}' not found in realm '{realm_name}'")

            client_uuid = target_client["id"]

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
            require_confirmation: Show the single idp-confirm-link screen before linking
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

    async def _ensure_browser_authenticators(
        self,
        realm_name: str,
        parent_alias: str,
        forms_alias: str,
        otp_alias: str,
        redirect_to_idp: str | None,
        require_otp: bool,
    ) -> None:
        """Ensure login steps of a browser flow (idempotent).

        ``require_otp`` sets the OTP subflow to REQUIRED and the condition to DISABLED,
        so password logins always require an OTP. Both states are written, so realm
        template switches work in both directions.
        """
        await self._ensure_execution_in_flow(parent_alias, "auth-cookie", "ALTERNATIVE", priority=10)
        await self._ensure_execution_in_flow(parent_alias, "identity-provider-redirector", "ALTERNATIVE", priority=20)
        if redirect_to_idp:
            await self._configure_redirector_execution(realm_name, parent_alias, redirect_to_idp)
        else:
            await self._clear_redirector_execution(parent_alias)

        await self._ensure_subflow(parent_alias, forms_alias, "ALTERNATIVE", "Username, password, otp")
        await self._ensure_execution_in_flow(forms_alias, "auth-username-password-form", "REQUIRED", priority=10)
        await self._ensure_subflow(
            forms_alias,
            otp_alias,
            "REQUIRED" if require_otp else "CONDITIONAL",
            "Browser - required OTP" if require_otp else "Browser - conditional OTP",
        )
        await self._ensure_execution_in_flow(
            otp_alias, "conditional-user-configured", "DISABLED" if require_otp else "REQUIRED", priority=10
        )
        await self._ensure_execution_in_flow(otp_alias, "auth-otp-form", "REQUIRED", priority=20)

    async def ensure_required_otp_browser_flow(self, realm_name: str, flow_alias: str) -> None:
        """Ensure browser flow where password login always requires OTP (idempotent).
        Built here (not from YAML) because the template reader cannot create nested subflows.
        """
        forms = f"{flow_alias} forms"
        otp = f"{flow_alias} otp"

        try:
            self.admin.change_current_realm(realm_name)

            if not any(flow.get("alias") == flow_alias for flow in self.admin.get_authentication_flows()):
                self.admin.create_authentication_flow(
                    payload={
                        "alias": flow_alias,
                        "providerId": "basic-flow",
                        "topLevel": True,
                        "builtIn": False,
                        "description": "Browser flow where a password login always requires an OTP",
                    }
                )
                logger.info(f"Created required-OTP browser flow '{flow_alias}' in realm '{realm_name}'")

            await self._ensure_browser_authenticators(
                realm_name=realm_name,
                parent_alias=flow_alias,
                forms_alias=forms,
                otp_alias=otp,
                redirect_to_idp=None,
                require_otp=True,
            )
            logger.info(f"Required-OTP browser flow '{flow_alias}' is in place in realm '{realm_name}'")
        finally:
            self.admin.change_current_realm("master")

    async def ensure_configure_totp_required(
        self, realm_name: str, skip_usernames: frozenset[str] = frozenset()
    ) -> list[str]:
        """Set CONFIGURE_TOTP on human users without OTP. Idempotent.

        Skips service accounts, users with existing OTP credentials, and federated users.
        ``skip_usernames`` spares accounts whose non-browser logins must keep working: an
        open required action makes the password grant unusable ("Account is not fully set up").
        Returns the usernames that received the action.
        """
        gewijzigd: list[str] = []
        try:
            self.admin.change_current_realm(realm_name)
            for user in self._all_realm_users():
                username = user.get("username", "")
                if username.startswith(_SERVICE_ACCOUNT_PREFIX) or username in skip_usernames:
                    continue
                acties = list(user.get("requiredActions") or [])
                if CONFIGURE_TOTP in acties:
                    continue
                user_id = user["id"]
                if self.admin.get_user_social_logins(user_id=user_id):
                    continue
                credentials = self.admin.get_credentials(user_id=user_id)
                if any(c.get("type") in _OTP_CREDENTIAL_TYPES for c in credentials):
                    continue
                self.admin.update_user(user_id=user_id, payload={"requiredActions": [*acties, CONFIGURE_TOTP]})
                gewijzigd.append(username)
        finally:
            self.admin.change_current_realm("master")

        if gewijzigd:
            await self.set_required_action_enabled(realm_name, CONFIGURE_TOTP, enabled=True)
            logger.info(f"Required CONFIGURE_TOTP for {len(gewijzigd)} user(s) in realm '{realm_name}'")
        return gewijzigd

    def _all_realm_users(self, page_size: int = 100) -> list[dict[str, Any]]:
        """All users in the current realm, paginated."""
        users: list[dict[str, Any]] = []
        while True:
            page = self.admin.get_users({"first": len(users), "max": page_size})
            users.extend(page)
            if len(page) < page_size:
                return users

    async def _ensure_subflow(self, parent_alias: str, subflow_alias: str, requirement: str, description: str) -> None:
        """Create a basic-flow subflow under a parent (sub)flow and set its requirement.

        Idempotent. ``subflow_alias`` has to be unique within the realm.
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
        """Create a post-broker login flow that only admits holders of a client role.

        It runs after the SSO login through the IdP and denies access with ``error_message``
        when the user lacks the role. Through the custom RequireClientRoleAuthenticator SPI,
        because a conditional sub-flow FAILS when it is skipped, which in a post-broker flow
        denies everyone.

        Args:
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
        """Add the custom RequireClientRoleAuthenticator to a flow.

        It calls success() for a user who has the role, failure() with an error page for one
        who does not, and skips the check entirely for an OAuth client in ``skip_clients``.
        With ``client_id`` the role is a client role, without it a realm role.

        Args:
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
        # Keycloak 26 refuses a config alias that already exists in the realm, and the
        # deployments of a project share a realm and usually a role.
        config_data = {
            "alias": f"require-role-{flow_alias}",
            "config": {
                "roleName": role_name,
                "errorMessage": error_message,
            },
        }

        # Add clientId only if provided (empty = realm role)
        if client_id:
            config_data["config"]["clientId"] = client_id

        if skip_clients:
            config_data["config"]["skipClients"] = ",".join(skip_clients)
            logger.debug(f"Configuring skipClients: {skip_clients}")

        if existing_config_id:
            config_data["id"] = existing_config_id
            self.admin.update_authenticator_config(payload=config_data, config_id=existing_config_id)
            logger.debug(f"Updated RequireRoleAuthenticator config: role={role_desc}")
        else:
            self.admin.create_execution_config(payload=config_data, execution_id=execution_id)
            logger.debug(f"Created RequireRoleAuthenticator config: role={role_desc}")

    async def set_identity_provider_post_broker_login_flow(
        self, realm_name: str, provider_alias: str, flow_alias: str
    ) -> None:
        """Set an IdP's post-broker login flow, which runs after every SSO login through it."""
        logger.info(f"Setting post-broker login flow '{flow_alias}' for IdP '{provider_alias}' in realm '{realm_name}'")

        try:
            self.admin.change_current_realm(realm_name)

            try:
                idp = self.admin.get_idp(idp_alias=provider_alias)
            except KeycloakGetError:
                raise KeycloakError(f"Identity provider '{provider_alias}' not found in realm '{realm_name}'")

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
        """Create a post-broker login flow that only admits holders of a realm role.

        As :meth:`create_post_broker_login_flow`, with an empty client id, so one role covers
        every application in the realm.

        Args:
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

    async def create_realm_role(self, realm_name: str, role_name: str, description: str | None = None) -> bool:
        """Create a realm role; True both when it was created and when it already existed."""
        logger.info(f"Creating realm role '{role_name}' in realm '{realm_name}'")

        role_data = {
            "name": role_name,
            "description": description or f"Realm role: {role_name}",
            "composite": False,
            "clientRole": False,
        }

        try:
            self.admin.change_current_realm(realm_name)

            try:
                self.admin.create_realm_role(payload=role_data)
                logger.info(f"Created realm role '{role_name}' in realm '{realm_name}'")
            except KeycloakPostError as e:
                if "409" in str(e) or "Conflict" in str(e):
                    logger.info(f"Realm role '{role_name}' already exists in realm '{realm_name}'")
                else:
                    raise

            self.admin.change_current_realm("master")

            logger.info(f"Successfully ensured realm role '{role_name}' exists")
            return True

        except KeycloakError as e:
            logger.error(f"Failed to create realm role '{role_name}': {e}")
            self.admin.change_current_realm("master")
            raise

    async def create_client_role(
        self, realm_name: str, client_id: str, role_name: str, description: str | None = None
    ) -> bool:
        """Create a client role; True both when it was created and when it already existed."""
        logger.info(f"Creating client role '{role_name}' for client '{client_id}' in realm '{realm_name}'")

        try:
            self.admin.change_current_realm(realm_name)

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
        logger.info(f"Assigning client role '{role_name}' to user '{user_id}' for client '{client_id}'")

        try:
            self.admin.change_current_realm(realm_name)

            clients = self.admin.get_clients()
            target_client = None
            for client in clients:
                if client.get("clientId") == client_id:
                    target_client = client
                    break

            if not target_client:
                raise KeycloakError(f"Client '{client_id}' not found in realm '{realm_name}'")

            client_uuid = target_client["id"]

            roles = self.admin.get_client_roles(client_id=client_uuid)
            target_role = None
            for role in roles:
                if role.get("name") == role_name:
                    target_role = role
                    break

            if not target_role:
                raise KeycloakError(f"Client role '{role_name}' not found for client '{client_id}'")

            self.admin.assign_client_role(user_id=user_id, client_id=client_uuid, roles=[target_role])
            logger.info(f"Assigned client role '{role_name}' to user '{user_id}'")

            self.admin.change_current_realm("master")

        except KeycloakError as e:
            logger.error(f"Failed to assign client role '{role_name}' to user: {e}")
            self.admin.change_current_realm("master")
            raise

    async def create_group(self, realm_name: str, group_name: str, path: str | None = None) -> dict[str, Any]:
        logger.info(f"Creating group '{group_name}' in realm '{realm_name}'")

        group_data = {
            "name": group_name,
            "path": path or f"/{group_name}",
        }

        try:
            self.admin.change_current_realm(realm_name)

            try:
                self.admin.create_group(payload=group_data)
                logger.info(f"Created group '{group_name}' in realm '{realm_name}'")
            except KeycloakPostError as e:
                if "409" in str(e) or "Conflict" in str(e):
                    logger.info(f"Group '{group_name}' already exists in realm '{realm_name}'")
                else:
                    raise

            groups = self.admin.get_groups(query={"search": group_name})
            group_info = None
            for group in groups:
                if group.get("name") == group_name:
                    group_info = group
                    break

            self.admin.change_current_realm("master")

            if not group_info:
                raise KeycloakError(f"Failed to retrieve group '{group_name}'")

            logger.info(f"Successfully ensured group '{group_name}' exists with ID {group_info['id']}")
            return group_info

        except KeycloakError as e:
            logger.error(f"Failed to create group '{group_name}': {e}")
            self.admin.change_current_realm("master")
            raise

    async def create_user(
        self,
        realm_name: str,
        username: str,
        password: str,
        email: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        enabled: bool = True,
        totp_secret: str | None = None,
        skip_email_verification: bool = False,
    ) -> dict[str, Any]:
        """Create a user.

        Args:
            totp_secret: Optional raw TOTP secret. When set, an OTP credential is
                imported alongside the password so Keycloak's conditional-OTP
                browser step requires it at login. Note: Keycloak only imports
                credentials on user creation - if the user already exists (409
                below) the OTP credential is not added.
            skip_email_verification: Let this user in without confirming their address.
                Only for an account whose address does not exist, see the callers.
        """
        logger.info(f"Creating user '{username}' in realm '{realm_name}'")

        user_data = {
            "username": username,
            "enabled": enabled,
            "emailVerified": False,
            "credentials": [{"type": "password", "value": password, "temporary": False}],
        }

        if totp_secret:
            user_data["credentials"].append(build_credential_representation(totp_secret))

        if email:
            user_data["email"] = email
            # Whoever has an address confirms it, whatever ``verifyEmail`` says on the realm.
            # Why: features/keycloak-mail.md.
            user_data["emailVerified"] = skip_email_verification
            if not skip_email_verification:
                user_data["requiredActions"] = ["VERIFY_EMAIL"]

        if first_name:
            user_data["firstName"] = first_name

        if last_name:
            user_data["lastName"] = last_name

        # With the provider disabled the browser flow silently skips the stored action.
        # Measured, see features/invites.md.
        if "requiredActions" in user_data:
            await self.set_required_action_enabled(realm_name, "VERIFY_EMAIL", True)

        try:
            self.admin.change_current_realm(realm_name)

            try:
                self.admin.create_user(payload=user_data)
                logger.info(f"Created new user '{username}' in realm '{realm_name}'")
            except KeycloakPostError as e:
                if "409" in str(e) or "Conflict" in str(e):
                    logger.info(f"User '{username}' already exists in realm '{realm_name}'")
                else:
                    raise

            created_user = await self.get_user_by_username(realm_name, username)

            self.admin.change_current_realm("master")

            if not created_user:
                raise KeycloakError(f"Failed to retrieve user '{username}'")

            logger.info(f"Successfully created user '{username}' with ID {created_user['id']}")
            return created_user

        except KeycloakError as e:
            logger.error(f"Failed to create user '{username}': {e}")
            self.admin.change_current_realm("master")
            raise

    async def send_verify_email(self, realm_name: str, user_id: str) -> None:
        """Without ``redirect_uri`` the link lands on Keycloak's own confirmation page, so
        there is no redirect-URI validation to trip over.
        """
        try:
            self.admin.change_current_realm(realm_name)
            self.admin.send_verify_email(user_id=user_id)
            logger.info(f"Sent verification mail to user {user_id} in realm '{realm_name}'")
        finally:
            self.admin.change_current_realm("master")

    async def get_user_by_username(self, realm_name: str, username: str) -> dict[str, Any] | None:
        """The user with this username, or ``None`` when there is none."""
        try:
            self.admin.change_current_realm(realm_name)

            users = self.admin.get_users(query={"username": username, "exact": "true"})

            self.admin.change_current_realm("master")

            if users and len(users) > 0:
                logger.debug(f"Found user '{username}' in realm '{realm_name}'")
                return users[0]

            logger.debug(f"User '{username}' not found in realm '{realm_name}'")
            return None

        except KeycloakError as e:
            logger.error(f"Failed to search for user '{username}': {e}")
            self.admin.change_current_realm("master")
            return None

    async def get_user_by_email(self, realm_name: str, email: str) -> dict[str, Any] | None:
        """The user with this email address, or ``None`` when there is none."""
        try:
            self.admin.change_current_realm(realm_name)

            users = self.admin.get_users(query={"email": email, "exact": "true"})

            self.admin.change_current_realm("master")

            if users and len(users) > 0:
                logger.debug(f"Found user with email '{email}' in realm '{realm_name}'")
                return users[0]

            logger.debug(f"User with email '{email}' not found in realm '{realm_name}'")
            return None

        except KeycloakError as e:
            logger.error(f"Failed to search for user with email '{email}': {e}")
            self.admin.change_current_realm("master")
            return None

    async def delete_user_by_username(self, realm_name: str, username: str) -> bool:
        """Delete a user by username; False when no such user was there."""
        logger.info(f"Deleting user '{username}' from realm '{realm_name}'")

        try:
            self.admin.change_current_realm(realm_name)

            user = await self.get_user_by_username(realm_name, username)
            if not user:
                logger.warning(f"User '{username}' not found in realm '{realm_name}'")
                self.admin.change_current_realm("master")
                return False

            # get_user_by_username switches back to master, so the delete would land there.
            self.admin.change_current_realm(realm_name)
            user_id = user["id"]
            self.admin.delete_user(user_id=user_id)

            self.admin.change_current_realm("master")

            logger.info(f"Successfully deleted user '{username}' from realm '{realm_name}'")
            return True

        except KeycloakError as e:
            logger.error(f"Failed to delete user '{username}': {e}")
            self.admin.change_current_realm("master")
            raise

    async def assign_realm_management_role(self, realm_name: str, user_id: str, target_realm: str) -> bool:
        """Let a user manage ``target_realm``; the user lives in ``realm_name``, usually master."""
        logger.info(f"Assigning realm management roles to user {user_id} for realm {target_realm}")

        try:
            # Switch to source realm (where user exists)
            self.admin.change_current_realm(realm_name)

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

            available_roles = self.admin.get_client_roles_of_user(user_id=user_id, client_id=client_id)

            if not available_roles or len(available_roles) == 0:
                logger.warning(f"No available roles found for client {client_id}")
                self.admin.change_current_realm("master")
                return True

            logger.info(f"Found {len(available_roles)} available roles")

            self.admin.assign_client_role(user_id=user_id, client_id=client_id, roles=available_roles)

            self.admin.change_current_realm("master")

            logger.info("Successfully assigned realm management roles")
            return True

        except KeycloakError as e:
            logger.error(f"Failed to assign realm management roles: {e}")
            self.admin.change_current_realm("master")
            raise

    async def assign_realm_admin_from_master(
        self, target_realm_name: str, user_id: str, role_names: list[str] | None = None
    ) -> bool:
        """Make a master-realm user the delegated admin of one realm.

        Such a user can log in to the admin console at /admin/ but sees and manages only that
        realm. In master every realm has a client named '{realm}-realm' whose roles grant
        management access to it.

        Args:
            role_names: List of role names to assign (default: all management roles)
        """
        logger.info(f"Assigning realm management permissions for '{target_realm_name}' to master realm user {user_id}")

        try:
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

            available_roles = self.admin.get_client_roles(client_id=client_uuid)

            if not available_roles:
                logger.warning(f"No roles found for client {realm_client_id}")
                return True

            # Filter to requested roles if specified, otherwise assign all
            if role_names:  # noqa: SIM108  # a ternary over these two multi-line branches reads worse
                roles_to_assign = [r for r in available_roles if r["name"] in role_names]
            else:
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
        """Assign realm roles; returns ``{"assigned": [...], "not_found": [...]}`` of role names.

        Raises:
            KeycloakError: the assignment itself failed.
        """
        logger.info(f"Assigning realm roles {role_names} to user {user_id} in realm {realm_name}")

        result: dict[str, list[str]] = {"assigned": [], "not_found": []}

        try:
            self.admin.change_current_realm(realm_name)

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

            self.admin.change_current_realm("master")

            return result

        except KeycloakError as e:
            logger.error(f"Failed to assign realm roles: {e}")
            self.admin.change_current_realm("master")
            raise

    async def assign_realm_role_to_user(self, realm_name: str, user_id: str, role_name: str) -> bool:
        """Assign one realm role to a user.

        Raises:
            KeycloakError: the assignment failed, or the role does not exist.
        """
        logger.info(f"Assigning realm role '{role_name}' to user {user_id} in realm {realm_name}")

        try:
            self.admin.change_current_realm(realm_name)

            role = self.admin.get_realm_role(role_name=role_name)
            if not role:
                raise KeycloakError(f"Realm role '{role_name}' not found in realm '{realm_name}'")

            self.admin.assign_realm_roles(user_id=user_id, roles=[role])
            logger.info(f"Successfully assigned realm role '{role_name}' to user {user_id}")

            self.admin.change_current_realm("master")
            return True

        except KeycloakError as e:
            logger.error(f"Failed to assign realm role '{role_name}': {e}")
            self.admin.change_current_realm("master")
            raise

    async def join_user_to_group(self, realm_name: str, user_id: str, group_name: str) -> bool:
        logger.info(f"Adding user {user_id} to group '{group_name}' in realm {realm_name}")

        try:
            self.admin.change_current_realm(realm_name)

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

            self.admin.group_user_add(user_id=user_id, group_id=target_group["id"])
            logger.info(f"Successfully added user {user_id} to group '{group_name}'")

            self.admin.change_current_realm("master")

            return True

        except KeycloakError as e:
            logger.error(f"Failed to add user to group: {e}")
            self.admin.change_current_realm("master")
            raise

    async def remove_all_realm_roles(self, realm_name: str, user_id: str) -> bool:
        """Strip every realm role off a user, so only what is assigned afterwards remains."""
        logger.info(f"Removing all realm roles from user {user_id} in realm {realm_name}")

        try:
            self.admin.change_current_realm(realm_name)

            user_roles = self.admin.get_realm_roles_of_user(user_id=user_id)

            if user_roles:
                self.admin.delete_realm_roles_of_user(user_id=user_id, roles=user_roles)
                role_names = [role.get("name") for role in user_roles]
                logger.info(
                    f"Successfully removed {len(user_roles)} realm roles from user {user_id}: {', '.join(role_names)}"
                )
            else:
                logger.info(f"No realm roles to remove for user {user_id}")

            self.admin.change_current_realm("master")

            return True

        except KeycloakError as e:
            logger.error(f"Failed to remove realm roles from user: {e}")
            self.admin.change_current_realm("master")
            raise

    async def connection_works(self) -> bool:
        """Return True if this connector can authenticate and reach the master realm.

        Used to decide whether OPI's client-credentials service account is already
        functional, or whether a first-boot admin-password bootstrap is needed.
        """
        try:
            self.admin.change_current_realm("master")
            self.admin.get_realm("master")
            return True
        except KeycloakError as e:
            logger.info(f"Keycloak connection check failed: {e}")
            return False

    async def ensure_master_service_account_client(self, client_id: str, client_secret: str) -> None:
        """Ensure a confidential service-account client exists in the master realm.

        Creates (or repairs) a client with the given id and secret, service
        accounts enabled and interactive flows disabled, then grants its service
        account user the master realm 'admin' role (full multi-realm admin). This
        lets OPI authenticate via client-credentials instead of the shared admin
        password.
        """
        self.admin.change_current_realm("master")

        payload = {
            "clientId": client_id,
            "name": "OPI Admin Service Account",
            "protocol": "openid-connect",
            "enabled": True,
            "publicClient": False,
            "secret": client_secret,
            "standardFlowEnabled": False,
            "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": False,
            "serviceAccountsEnabled": True,
            # The master 'admin' role puts every realm in the token. With 39 realms that is
            # 25 kB, and the ODCN router answers such a request with a bare 400.
            "attributes": {"client.use.lightweight.access.token.enabled": "true"},
        }

        existing = next((c for c in self.admin.get_clients() if c.get("clientId") == client_id), None)
        if existing is None:
            self.admin.create_client(payload=payload)
            logger.info(f"Created master service-account client '{client_id}'")
            client = next((c for c in self.admin.get_clients() if c.get("clientId") == client_id), None)
        else:
            self.admin.update_client(client_id=existing["id"], payload=payload)
            logger.info(f"Updated master service-account client '{client_id}'")
            client = existing

        if not client:
            raise KeycloakError(f"Failed to retrieve master service-account client '{client_id}'")

        service_account_user = self.admin.get_client_service_account_user(client["id"])
        await self.assign_realm_roles_to_user("master", service_account_user["id"], ["admin"])
        logger.info(f"Granted master 'admin' role to service account of client '{client_id}'")


async def create_keycloak_connector(
    keycloak_url: str | None = None,
    admin_username: str | None = None,
    admin_password: str | None = None,
    use_client_credentials: bool | None = None,
) -> KeycloakConnector:
    """
    Factory function to create a KeycloakConnector instance.

    By default OPI authenticates with its client-credentials service account when
    KEYCLOAK_ADMIN_CLIENT_SECRET is configured, otherwise it falls back to the
    admin username/password. Pass use_client_credentials=False to force the
    admin-password path (used during first-boot self-bootstrap of the service
    account client).

    Args:
        use_client_credentials: Force client-credentials (True) or admin password
            (False); None auto-selects based on whether a client secret is set.
    """
    if use_client_credentials is None:
        use_client_credentials = bool(settings.KEYCLOAK_ADMIN_CLIENT_SECRET)

    if use_client_credentials and settings.KEYCLOAK_ADMIN_CLIENT_SECRET:
        return KeycloakConnector(
            keycloak_url=keycloak_url or settings.KEYCLOAK_URL,
            client_id=settings.KEYCLOAK_ADMIN_CLIENT_ID,
            client_secret=settings.KEYCLOAK_ADMIN_CLIENT_SECRET,
        )

    return KeycloakConnector(
        keycloak_url=keycloak_url or settings.KEYCLOAK_URL,
        admin_username=admin_username or settings.KEYCLOAK_ADMIN_USERNAME,
        admin_password=admin_password or settings.KEYCLOAK_ADMIN_PASSWORD,
    )


async def fetch_oidc_metadata(discovery_url: str) -> dict[str, Any]:
    """Fetch an OIDC provider's discovery document.

    The document is the authoritative source for the issuer and the JWKS URI, so
    token verification never has to assemble those URLs itself.

    Raises:
        aiohttp.ClientError: When the document cannot be retrieved
    """
    async with client_session(DISCOVERY_TIMEOUT_SECONDS) as session, session.get(discovery_url) as response:
        response.raise_for_status()
        metadata: dict[str, Any] = await response.json()
    logger.debug("Fetched OIDC metadata from %s", discovery_url)
    return metadata


async def fetch_jwks(jwks_uri: str) -> dict[str, Any]:
    """Fetch a realm's JSON Web Key Set.

    Raises:
        aiohttp.ClientError: When the key set cannot be retrieved
    """
    async with client_session(DISCOVERY_TIMEOUT_SECONDS) as session, session.get(jwks_uri) as response:
        response.raise_for_status()
        jwks: dict[str, Any] = await response.json()
    logger.debug("Fetched JWKS from %s", jwks_uri)
    return jwks
