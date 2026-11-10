"""Invite manager for handling user invitations to project realms."""

import logging
import re
from typing import Any, cast

from keycloak.exceptions import KeycloakError

from opi.connectors.keycloak import (
    DEFAULT_BROWSER_FLOW,
    PASSWORD_FORM_BROWSER_FLOWS,
    KeycloakConnector,
    create_keycloak_connector,
)
from opi.services.project_file_handler import ProjectFileHandler

logger = logging.getLogger(__name__)


class InviteError(Exception):
    def __init__(self, message: str, error_code: str) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code


class InviteNotFoundError(InviteError):
    def __init__(self, key: str) -> None:
        super().__init__(f"Invite '{key}' not found", "invite_not_found")
        self.key = key


class InviteDomainError(InviteError):
    def __init__(self, email: str, required_domain: str) -> None:
        super().__init__(
            f"Email '{email}' does not match required domain '{required_domain}'",
            "domain_mismatch",
        )
        self.email = email
        self.required_domain = required_domain


class InviteAuthMethodError(InviteError):
    def __init__(self, method: str) -> None:
        super().__init__(
            f"Authentication method '{method}' is not allowed for this invite",
            "auth_method_not_allowed",
        )
        self.method = method


class UserExistsError(InviteError):
    def __init__(self, email: str) -> None:
        super().__init__(f"User with email '{email}' already exists", "user_exists")
        self.email = email


class InviteManager:
    def __init__(self, project_file_handler: ProjectFileHandler | None = None) -> None:
        self.project_file_handler = project_file_handler or ProjectFileHandler()

    def validate_email_domain(self, email: str, invite: dict[str, Any]) -> bool:
        """Raises:
        InviteDomainError: the address is outside the invite's domain restriction.
        """
        restrict_domain = invite.get("restrict_domain")

        if not restrict_domain:
            return True

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
        """Args:
            method: 'sso' or 'local'.

        Raises:
            InviteAuthMethodError: that method is not allowed for this invite.
        """
        auth_methods = self.project_file_handler.get_invite_auth_methods(project_data, invite)

        if method not in auth_methods or not auth_methods[method]:
            raise InviteAuthMethodError(method)

        return True

    async def _assign_client_roles(
        self,
        keycloak: KeycloakConnector,
        realm_name: str,
        user_id: str,
        client_roles: dict[str, Any],
    ) -> dict[str, Any]:
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
        """Assign the invite's roles and groups to a user, and report what was assigned.

        Both ``roles`` and ``realm_roles`` hold realm-level roles; ``realm_roles`` is the
        preferred spelling, matching the keycloak config's ``realm-roles``. ``client_roles``
        maps a client id to its role names.
        """
        assigned: dict[str, Any] = {"roles": [], "client_roles": {}, "groups": []}
        errors: list[str] = []

        roles = invite.get("roles", [])
        realm_roles = invite.get("realm_roles", [])

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

        client_roles_raw = invite.get("client_roles", {})
        if client_roles_raw and isinstance(client_roles_raw, dict):
            client_roles = cast("dict[str, Any]", client_roles_raw)
            client_result = await self._assign_client_roles(keycloak, realm_name, user_id, client_roles)
            assigned["client_roles"] = client_result["assigned"]
            if client_result["errors"]:
                errors.extend(client_result["errors"])

        groups = invite.get("groups", [])
        for group_name in groups:
            try:
                await keycloak.join_user_to_group(realm_name, user_id, group_name)
                assigned["groups"].append(group_name)
                logger.info(f"Added user {user_id} to group {group_name} in realm {realm_name}")
            except Exception as e:
                errors.append(f"Group '{group_name}' not found or could not be joined: {e}")
                logger.error(f"Failed to add user to group {group_name}: {e}")

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
        """Find or create the SSO user in the project realm and assign the invite's permissions.

        Returns the user_id and the assigned permissions.

        Raises:
            InviteDomainError: the address is outside the invite's domain restriction.
        """
        email = user_info.get("email")
        if not email:
            raise InviteError("SSO user info missing email", "missing_email")

        # Deliberately no email_verified check, unlike the platform login flow in
        # auth_routes.py: this route grants no OPI access, only a user plus roles in the
        # project's OWN realm, and the application behind that realm stays responsible for
        # enforcing email_verified. Mind that get_user_by_email() below binds an incoming SSO
        # login onto any existing realm user with the same address. The domain restriction is
        # the only guard at this layer.
        self.validate_email_domain(email, invite)

        keycloak = await create_keycloak_connector()

        existing_user = await keycloak.get_user_by_email(realm_name, email)

        if existing_user:
            user_id = existing_user["id"]
            logger.info(f"Found existing user {email} in realm {realm_name}")
        else:
            # An SSO user never uses this password.
            import secrets
            import string

            temp_password = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))

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
        """Create a local user in the project realm and assign the invite's permissions.

        An address that already exists is refused, not reused.

        Returns user_id, email, created, the assigned permissions and verification_mail_sent.

        Raises:
            InviteDomainError: the address is outside the invite's domain restriction.
            InviteError: any other validation error.
        """
        email = form_data.get("email", "").strip()
        first_name = form_data.get("first_name", "").strip()
        last_name = form_data.get("last_name", "").strip()
        password = form_data.get("password", "")

        if not email:
            raise InviteError("Email is required", "missing_email")
        if not first_name:
            raise InviteError("First name is required", "missing_first_name")
        if not last_name:
            raise InviteError("Last name is required", "missing_last_name")
        if not password:
            raise InviteError("Password is required", "missing_password")

        email_pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        if not re.match(email_pattern, email):
            raise InviteError("Invalid email format", "invalid_email")

        self.validate_email_domain(email, invite)

        self._validate_password(password)

        keycloak = await create_keycloak_connector()

        existing_user = await keycloak.get_user_by_email(realm_name, email)
        if not existing_user:
            existing_user = await keycloak.get_user_by_username(realm_name, email)

        if existing_user:
            logger.warning(f"Local registration rejected: email {email} already exists in realm {realm_name}")
            raise UserExistsError(email)

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

        # Sent now, because the success page asks the user to confirm. If it fails, Keycloak
        # still sends it at the first login: the account carries the required action.
        verification_mail_sent = True
        try:
            await keycloak.send_verify_email(realm_name, user_id)
        except KeycloakError as e:
            logger.warning(f"Could not send the verification mail to {email} in realm {realm_name}: {e}")
            verification_mail_sent = False

        assigned = await self.assign_invite_permissions(keycloak, realm_name, user_id, invite)

        return {
            "user_id": user_id,
            "email": email,
            "created": True,
            "assigned": assigned,
            "verification_mail_sent": verification_mail_sent,
        }

    def _validate_password(self, password: str) -> None:
        """At least 12 characters, with an upper-case letter, a lower-case letter and a digit.

        Raises:
            InviteError: the password does not meet those requirements.
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
        if request_lang and request_lang.lower() in ("nl", "en"):
            return request_lang.lower()

        if accept_language:
            # The header looks like "en-US,en;q=0.9,nl;q=0.8".
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

            realm_config = await keycloak.get_realm(realm_name)

            # Local auth is available if the browser flow shows a username/password form.
            # We create users via Admin API (not self-registration), so registrationAllowed
            # doesn't matter. PASSWORD_FORM_BROWSER_FLOWS is the shared rule: it also covers
            # the required-OTP flow of sso-support-otp, where a local account is still a way
            # in (it just adds a factor).
            local_available = False
            if realm_config:
                browser_flow = realm_config.get("browserFlow", DEFAULT_BROWSER_FLOW)
                local_available = browser_flow in PASSWORD_FORM_BROWSER_FLOWS

                # Without an identity provider local must be available, or nobody can log in.
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
            # Safe default: assume both are available.
            return {
                "sso": True,
                "local": True,
                "identity_providers": [],
            }
