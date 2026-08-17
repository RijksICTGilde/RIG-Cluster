"""Invite manager for handling user invitations to project realms."""

import logging
import re
from typing import Any, cast

from opi.connectors.keycloak import KeycloakConnector, create_keycloak_connector
from opi.handlers.project_file_handler import ProjectFileHandler

logger = logging.getLogger(__name__)


class InviteError(Exception):
    """Base exception for invite-related errors."""

    def __init__(self, message: str, error_code: str) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code


class InviteNotFoundError(InviteError):
    """Raised when an invite key is not found."""

    def __init__(self, key: str) -> None:
        super().__init__(f"Invite '{key}' not found", "invite_not_found")
        self.key = key


class InviteDomainError(InviteError):
    """Raised when email domain doesn't match restriction."""

    def __init__(self, email: str, required_domain: str) -> None:
        super().__init__(
            f"Email '{email}' does not match required domain '{required_domain}'",
            "domain_mismatch",
        )
        self.email = email
        self.required_domain = required_domain


class InviteAuthMethodError(InviteError):
    """Raised when the requested auth method is not allowed."""

    def __init__(self, method: str) -> None:
        super().__init__(
            f"Authentication method '{method}' is not allowed for this invite",
            "auth_method_not_allowed",
        )
        self.method = method


class UserExistsError(InviteError):
    """Raised when trying to create a local user that already exists."""

    def __init__(self, email: str) -> None:
        super().__init__(f"User with email '{email}' already exists", "user_exists")
        self.email = email


class InviteManager:
    """Manager for invite operations and user onboarding."""

    def __init__(self, project_file_handler: ProjectFileHandler | None = None) -> None:
        """
        Initialize the InviteManager.

        Args:
            project_file_handler: Optional ProjectFileHandler instance
        """
        self.project_file_handler = project_file_handler or ProjectFileHandler()

    def validate_email_domain(self, email: str, invite: dict[str, Any]) -> bool:
        """
        Validate that an email address matches any domain restriction.

        Args:
            email: The email address to validate
            invite: The invite configuration

        Returns:
            True if email is valid

        Raises:
            InviteDomainError: If email doesn't match required domain
        """
        restrict_domain = invite.get("restrict_domain")

        if not restrict_domain:
            return True

        # Normalize domain (ensure it starts with @)
        if not restrict_domain.startswith("@"):
            restrict_domain = f"@{restrict_domain}"

        if not email.lower().endswith(restrict_domain.lower()):
            raise InviteDomainError(email, restrict_domain)

        return True

    def validate_auth_method(
        self,
        project_data: dict[str, Any],
        invite: dict[str, Any],
        method: str,
    ) -> bool:
        """
        Validate that the requested auth method is allowed for this invite.

        Args:
            project_data: The project configuration data
            invite: The invite configuration
            method: The authentication method ('sso' or 'local')

        Returns:
            True if method is allowed

        Raises:
            InviteAuthMethodError: If the method is not allowed
        """
        auth_methods = self.project_file_handler.get_invite_auth_methods(project_data, invite)

        if method not in auth_methods or not auth_methods[method]:
            raise InviteAuthMethodError(method)

        return True

    def get_valid_invite(
        self,
        project_data: dict[str, Any],
        key: str,
    ) -> dict[str, Any]:
        """
        Get and validate an invite by key.

        Args:
            project_data: The project configuration data
            key: The invite key

        Returns:
            The invite configuration

        Raises:
            InviteNotFoundError: If invite not found
        """
        invite = self.project_file_handler.get_invite_by_key(project_data, key)

        if not invite:
            raise InviteNotFoundError(key)

        return invite

    async def _assign_client_roles(
        self,
        keycloak: KeycloakConnector,
        realm_name: str,
        user_id: str,
        client_roles: dict[str, Any],
    ) -> dict[str, Any]:
        """Assign client roles to a user."""
        assigned: dict[str, list[str]] = {}
        errors: list[str] = []

        for client_id, role_names in client_roles.items():
            if not isinstance(role_names, list):
                errors.append(f"Invalid client_roles[{client_id}]: expected list, got {type(role_names)}")
                logger.error(f"Skipping client_roles[{client_id}]: expected list, got {type(role_names)}")
                continue

            assigned_for_client: list[str] = []
            role_name_list = cast("list[Any]", role_names)
            for role_name in [str(r) for r in role_name_list]:
                try:
                    await keycloak.assign_client_role_to_user(realm_name, client_id, user_id, role_name)
                    assigned_for_client.append(role_name)
                    logger.info(f"Assigned client role '{client_id}.{role_name}' to user {user_id}")
                except Exception as e:
                    error_msg = f"Client role '{client_id}.{role_name}' not found or could not be assigned"
                    errors.append(error_msg)
                    logger.error(f"Failed to assign client role '{client_id}.{role_name}': {e}")

            if assigned_for_client:
                assigned[client_id] = assigned_for_client

        return {"assigned": assigned, "errors": errors}

    async def assign_invite_permissions(
        self,
        keycloak: KeycloakConnector,
        realm_name: str,
        user_id: str,
        invite: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Assign roles and groups from invite configuration to a user.

        Supports both realm roles and client roles:
        - roles: ["role1", "role2"] - assigns realm-level roles (legacy)
        - realm_roles: ["role1", "role2"] - assigns realm-level roles (new, preferred)
        - client_roles: {"client-id": ["role1"]} - assigns client-level roles

        Both `roles` and `realm_roles` assign realm-level roles. The `realm_roles`
        field is preferred for consistency with the keycloak config `realm-roles`.

        Args:
            keycloak: KeycloakConnector instance
            realm_name: The Keycloak realm name
            user_id: The Keycloak user ID
            invite: The invite configuration

        Returns:
            Dict with assigned roles, client_roles, and groups
        """
        assigned: dict[str, Any] = {"roles": [], "client_roles": {}, "groups": []}
        errors: list[str] = []

        # Assign realm roles (support both 'roles' and 'realm_roles' fields)
        # 'realm_roles' is the new preferred field for consistency with config
        roles = invite.get("roles", [])
        realm_roles = invite.get("realm_roles", [])

        # Merge both role lists, removing duplicates
        all_realm_roles = list(set(roles + realm_roles))

        if all_realm_roles:
            try:
                result = await keycloak.assign_realm_roles_to_user(realm_name, user_id, all_realm_roles)
                assigned["roles"] = result["assigned"]
                if result["not_found"]:
                    errors.append(f"Realm roles not found: {result['not_found']}")
                logger.info(f"Assigned realm roles {result['assigned']} to user {user_id} in realm {realm_name}")
            except Exception as e:
                errors.append(f"Failed to assign realm roles: {e}")
                logger.error(f"Failed to assign realm roles: {e}")

        # Assign client roles
        client_roles_raw = invite.get("client_roles", {})
        if client_roles_raw and isinstance(client_roles_raw, dict):
            client_roles = cast("dict[str, Any]", client_roles_raw)
            client_result = await self._assign_client_roles(keycloak, realm_name, user_id, client_roles)
            assigned["client_roles"] = client_result["assigned"]
            if client_result["errors"]:
                errors.extend(client_result["errors"])

        # Add to groups
        groups = invite.get("groups", [])
        for group_name in groups:
            try:
                await keycloak.join_user_to_group(realm_name, user_id, group_name)
                assigned["groups"].append(group_name)
                logger.info(f"Added user {user_id} to group {group_name} in realm {realm_name}")
            except Exception as e:
                errors.append(f"Group '{group_name}' not found or could not be joined: {e}")
                logger.error(f"Failed to add user to group {group_name}: {e}")

        # Include errors in the result
        if errors:
            assigned["errors"] = errors

        return assigned

    async def complete_sso_invite(
        self,
        project_data: dict[str, Any],
        project_name: str,
        invite: dict[str, Any],
        user_info: dict[str, Any],
        realm_name: str,
    ) -> dict[str, Any]:
        """
        Complete the SSO invite flow for a user.

        This finds or creates a user in the project realm based on SSO user info,
        then assigns the configured roles and groups.

        Args:
            project_data: The project configuration data
            project_name: The project name
            invite: The invite configuration
            user_info: User info from SSO (contains email, name, sub, etc.)
            realm_name: The project's Keycloak realm name

        Returns:
            Dict with user_id and assigned permissions

        Raises:
            InviteDomainError: If email doesn't match domain restriction
        """
        email = user_info.get("email")
        if not email:
            raise InviteError("SSO user info missing email", "missing_email")

        # NOTE: Intentionally no email_verified check here, unlike the platform login flow
        # (auth_routes.py). That flow gates access to OPI itself, so an unverified email must be
        # rejected. This flow only provisions the user into the *project's own* Keycloak realm and
        # assigns the invite's realm roles/groups -- it grants no OPI platform access. A Keycloak
        # account (even with roles) is not application access: the application behind the realm is
        # the resource server and remains responsible for enforcing email_verified before trusting
        # the identity. This contract matters because get_user_by_email() below binds an incoming
        # SSO login onto any existing realm user with the same email, so an unverified email could
        # otherwise collide onto an existing account. Domain restriction is the only guard applied
        # at this layer by design.

        # Validate email domain
        self.validate_email_domain(email, invite)

        keycloak = await create_keycloak_connector()

        # Check if user already exists
        existing_user = await keycloak.get_user_by_email(realm_name, email)

        if existing_user:
            user_id = existing_user["id"]
            logger.info(f"Found existing user {email} in realm {realm_name}")
        else:
            # Create user from SSO info
            # Generate a random password since SSO users don't need it
            import secrets
            import string

            temp_password = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))

            # Extract first and last name from SSO user info
            full_name: str = str(user_info.get("name", "") or "")
            name_parts: list[str] = full_name.split() if full_name else []
            given_name = user_info.get("given_name")
            family_name = user_info.get("family_name")
            first_name: str | None = str(given_name) if given_name else (name_parts[0] if name_parts else None)
            last_name: str | None = (
                str(family_name) if family_name else (name_parts[-1] if len(name_parts) > 1 else None)
            )

            created_user = await keycloak.create_user(
                realm_name=realm_name,
                username=email,  # Use email as username
                password=temp_password,
                email=email,
                first_name=first_name,
                last_name=last_name,
                enabled=True,
            )
            user_id = created_user["id"]
            logger.info(f"Created new user {email} in realm {realm_name}")

        # Assign permissions
        assigned = await self.assign_invite_permissions(keycloak, realm_name, user_id, invite)

        return {
            "user_id": user_id,
            "email": email,
            "created": existing_user is None,
            "assigned": assigned,
        }

    async def complete_local_invite(
        self,
        project_data: dict[str, Any],
        project_name: str,
        invite: dict[str, Any],
        form_data: dict[str, str],
        realm_name: str,
    ) -> dict[str, Any]:
        """
        Complete the local account invite flow for a user.

        This creates a new local user in the project realm with the provided
        credentials, then assigns the configured roles and groups. If the user
        already exists, just assigns the roles/groups without creating a new user.

        Args:
            project_data: The project configuration data
            project_name: The project name
            invite: The invite configuration
            form_data: Registration form data (email, first_name, last_name, password)
            realm_name: The project's Keycloak realm name

        Returns:
            Dict with user_id, email, created (bool), and assigned permissions

        Raises:
            InviteDomainError: If email doesn't match domain restriction
            InviteError: For validation errors
        """
        email = form_data.get("email", "").strip()
        first_name = form_data.get("first_name", "").strip()
        last_name = form_data.get("last_name", "").strip()
        password = form_data.get("password", "")

        # Validate required fields
        if not email:
            raise InviteError("Email is required", "missing_email")
        if not first_name:
            raise InviteError("First name is required", "missing_first_name")
        if not last_name:
            raise InviteError("Last name is required", "missing_last_name")
        if not password:
            raise InviteError("Password is required", "missing_password")

        # Validate email format
        email_pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        if not re.match(email_pattern, email):
            raise InviteError("Invalid email format", "invalid_email")

        # Validate email domain
        self.validate_email_domain(email, invite)

        # Validate password strength
        self._validate_password(password)

        keycloak = await create_keycloak_connector()

        # Check if user already exists - for local registration, reject existing emails
        existing_user = await keycloak.get_user_by_email(realm_name, email)
        if not existing_user:
            # Also check by username (email)
            existing_user = await keycloak.get_user_by_username(realm_name, email)

        if existing_user:
            # For local account registration, don't allow using existing email addresses
            logger.warning(f"Local registration rejected: email {email} already exists in realm {realm_name}")
            raise UserExistsError(email)

        # Create new user
        created_user = await keycloak.create_user(
            realm_name=realm_name,
            username=email,
            password=password,
            email=email,
            first_name=first_name,
            last_name=last_name,
            enabled=True,
        )
        user_id = created_user["id"]
        logger.info(f"Created new local user {email} in realm {realm_name}")

        # Assign permissions
        assigned = await self.assign_invite_permissions(keycloak, realm_name, user_id, invite)

        return {
            "user_id": user_id,
            "email": email,
            "created": True,
            "assigned": assigned,
        }

    def _validate_password(self, password: str) -> None:
        """
        Validate password meets minimum requirements.

        Requirements:
        - Minimum 12 characters
        - At least one uppercase letter
        - At least one lowercase letter
        - At least one digit

        Args:
            password: The password to validate

        Raises:
            InviteError: If password doesn't meet requirements
        """
        if len(password) < 12:
            raise InviteError("Password must be at least 12 characters", "password_too_short")

        if not re.search(r"[A-Z]", password):
            raise InviteError("Password must contain at least one uppercase letter", "password_no_uppercase")

        if not re.search(r"[a-z]", password):
            raise InviteError("Password must contain at least one lowercase letter", "password_no_lowercase")

        if not re.search(r"\d", password):
            raise InviteError("Password must contain at least one digit", "password_no_digit")

    def detect_language(self, request_lang: str | None, accept_language: str | None, default: str = "nl") -> str:
        """
        Detect the preferred language for the user.

        Priority:
        1. Explicit ?lang= URL parameter
        2. Browser Accept-Language header
        3. Default language from settings

        Args:
            request_lang: Language from URL parameter
            accept_language: Accept-Language header value
            default: Default language

        Returns:
            Two-letter language code (e.g., 'nl', 'en')
        """
        # Check explicit URL parameter
        if request_lang and request_lang.lower() in ("nl", "en"):
            return request_lang.lower()

        # Check Accept-Language header
        if accept_language:
            # Parse Accept-Language header (e.g., "en-US,en;q=0.9,nl;q=0.8")
            for lang_part in accept_language.split(","):
                lang = lang_part.split(";")[0].strip().lower()
                if lang.startswith("nl"):
                    return "nl"
                elif lang.startswith("en"):
                    return "en"

        return default

    async def get_realm_auth_methods(self, realm_name: str) -> dict[str, Any]:
        """
        Query Keycloak to determine available authentication methods for a realm.

        This queries the actual Keycloak configuration to determine:
        - SSO: Whether any identity providers are configured and enabled
        - Local: Whether the realm allows local authentication (based on realm settings)

        Args:
            realm_name: The Keycloak realm name

        Returns:
            Dict with:
                - 'sso': bool - Whether SSO is available
                - 'local': bool - Whether local accounts are available
                - 'identity_providers': list - List of available IdP info (alias, displayName)
        """
        try:
            keycloak = await create_keycloak_connector()

            # Get identity providers
            identity_providers = await keycloak.get_identity_providers(realm_name)
            enabled_idps = [
                {
                    "alias": idp.get("alias"),
                    "displayName": idp.get("displayName", idp.get("alias")),
                    "providerId": idp.get("providerId"),
                }
                for idp in identity_providers
                if idp.get("enabled", True)
            ]

            # Get realm configuration to check local auth settings
            realm_config = await keycloak.get_realm(realm_name)

            # Local auth is available if the browser flow supports username/password login.
            # We create users via Admin API (not self-registration), so registrationAllowed
            # doesn't matter. What matters is: can users log in with username/password?
            #
            # - If browserFlow is "browser" (default), username/password login is shown
            # - If browserFlow is a custom SSO-redirect flow, users are auto-redirected to IdP
            local_available = False
            if realm_config:
                browser_flow = realm_config.get("browserFlow", "browser")

                # Standard browser flow supports username/password login
                # Custom flows like "External IDP Redirector" auto-redirect to SSO
                # We consider local available if using the standard browser flow
                is_standard_browser_flow = browser_flow == "browser"
                local_available = is_standard_browser_flow

                # If there are no identity providers, local must be available
                # (otherwise users couldn't login at all)
                if not enabled_idps:
                    local_available = True

            logger.info(
                f"Realm {realm_name} auth methods: "
                f"sso={len(enabled_idps) > 0}, local={local_available}, "
                f"browserFlow={realm_config.get('browserFlow') if realm_config else 'N/A'}, "
                f"idps={[idp['alias'] for idp in enabled_idps]}"
            )

            return {
                "sso": len(enabled_idps) > 0,
                "local": local_available,
                "identity_providers": enabled_idps,
            }

        except Exception as e:
            logger.warning(f"Failed to query Keycloak for realm {realm_name} auth methods: {e}")
            # Fall back to safe defaults - assume both are available
            return {
                "sso": True,
                "local": True,
                "identity_providers": [],
            }


def create_invite_manager() -> InviteManager:
    """
    Create and return an InviteManager instance.

    Returns:
        InviteManager instance
    """
    return InviteManager()
