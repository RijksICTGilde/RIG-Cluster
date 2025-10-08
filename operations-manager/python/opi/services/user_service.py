"""
In-memory user service for storing and retrieving Keycloak user information.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class UserService:
    """
    In-memory service for managing user information from Keycloak.

    Users are identified and stored by email address. This is a temporary
    implementation until a proper database-backed user service is implemented.

    Includes email-based access control with an allowlist of permitted users.
    """

    def __init__(self):
        """Initialize the in-memory user store and email allowlist."""
        self._users: dict[str, dict[str, Any]] = {}
        self._allowed_emails: set[str] = set()
        logger.info("UserService initialized with in-memory storage and email access control")

    def store_user(self, user_info: dict[str, Any]) -> None:
        """
        Store user information in memory using email as the key.

        Args:
            user_info: Dictionary containing user information from Keycloak
                      Expected keys: sub, email, name, organization.name, organization.number, etc.
        """
        if not user_info.get("email"):
            logger.error("Cannot store user without email address")
            return

        email = user_info["email"]

        # Process and enrich user information
        enriched_user_info = self._enrich_user_info(user_info.copy())

        self._users[email] = enriched_user_info
        logger.info(f"Stored user information for: {email}")

        # Log organization info if present
        if enriched_user_info.get("organization"):
            org_info = enriched_user_info["organization"]
            logger.info(f"User {email} belongs to organization: {org_info.get('name', 'Unknown')}")

        logger.debug(f"User info keys: {list(enriched_user_info.keys())}")

    def _enrich_user_info(self, user_info: dict[str, Any]) -> dict[str, Any]:
        """
        Enrich user information by structuring organization data and adding derived fields.

        Args:
            user_info: Raw user information from Keycloak

        Returns:
            Enriched user information with structured organization data
        """
        # Extract organization information from flat structure
        organization_info = {}

        # Map organization attributes from token claims
        org_mappings = {
            "name": user_info.get("organization.name"),
            "number": user_info.get("organization.number"),
            "role": user_info.get("organization.role"),
        }

        # Only include non-empty organization fields
        for key, value in org_mappings.items():
            if value:
                organization_info[key] = value

        # Add structured organization info if we have any
        if organization_info:
            user_info["organization"] = organization_info

        # Add derived fields for easier template access
        user_info["display_name"] = self._get_display_name(user_info)
        user_info["has_organization"] = bool(organization_info)

        # Add role-based flags for easier template logic
        if organization_info.get("role"):
            role = organization_info["role"].lower()
            user_info["is_admin"] = role in ["admin", "administrator"]
            user_info["is_developer"] = role in ["developer", "dev"]
            user_info["is_manager"] = role in ["manager", "lead", "supervisor"]

        return user_info

    def _get_display_name(self, user_info: dict[str, Any]) -> str:
        """
        Get the best display name for the user.

        Args:
            user_info: User information dictionary

        Returns:
            Best available display name
        """
        # Priority order for display name
        if user_info.get("name"):
            return user_info["name"]

        # Try to construct from given_name + family_name
        given_name = user_info.get("given_name", "")
        family_name = user_info.get("family_name", "")
        if given_name and family_name:
            return f"{given_name} {family_name}"

        # Fall back to preferred_username or email
        if user_info.get("preferred_username"):
            return user_info["preferred_username"]

        if user_info.get("email"):
            return user_info["email"]

        return "Unknown User"

    def get_users_by_organization(self, organization_name: str) -> dict[str, dict[str, Any]]:
        """
        Get all users belonging to a specific organization.

        Args:
            organization_name: Name of the organization

        Returns:
            Dictionary mapping email addresses to user information for users in the organization
        """
        matching_users = {}

        for email, user_info in self._users.items():
            org_info = user_info.get("organization", {})
            if org_info.get("name") == organization_name:
                matching_users[email] = user_info

        logger.debug(f"Found {len(matching_users)} users in organization '{organization_name}'")
        return matching_users

    def get_organization_stats(self) -> dict[str, Any]:
        """
        Get statistics about organizations and users.

        Returns:
            Dictionary containing organization statistics
        """
        stats = {"total_users": len(self._users), "users_with_organization": 0, "organizations": {}, "roles": {}}

        for user_info in self._users.values():
            org_info = user_info.get("organization", {})

            if org_info:
                stats["users_with_organization"] += 1

                # Count organizations
                org_name = org_info.get("name", "Unknown")
                if org_name not in stats["organizations"]:
                    stats["organizations"][org_name] = {"count": 0, "roles": set()}
                stats["organizations"][org_name]["count"] += 1

                # Count roles
                role = org_info.get("role")
                if role:
                    stats["roles"][role] = stats["roles"].get(role, 0) + 1
                    stats["organizations"][org_name]["roles"].add(role)

        # Convert sets to lists for JSON serialization
        for org_data in stats["organizations"].values():
            org_data["roles"] = list(org_data["roles"])

        logger.debug(f"Organization stats: {stats}")
        return stats

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        """
        Retrieve user information by email address.

        Args:
            email: Email address of the user

        Returns:
            User information dictionary if found, None otherwise
        """
        user_info = self._users.get(email)
        if user_info:
            logger.debug(f"Retrieved user information for: {email}")
        else:
            logger.debug(f"User not found for email: {email}")
        return user_info

    def update_user(self, email: str, user_info: dict[str, Any]) -> bool:
        """
        Update existing user information.

        Args:
            email: Email address of the user
            user_info: Updated user information

        Returns:
            True if user was updated, False if user not found
        """
        if email not in self._users:
            logger.warning(f"Cannot update user - not found: {email}")
            return False

        self._users[email].update(user_info)
        logger.info(f"Updated user information for: {email}")
        return True

    def remove_user(self, email: str) -> bool:
        """
        Remove user from memory.

        Args:
            email: Email address of the user

        Returns:
            True if user was removed, False if user not found
        """
        if email not in self._users:
            logger.warning(f"Cannot remove user - not found: {email}")
            return False

        del self._users[email]
        logger.info(f"Removed user: {email}")
        return True

    def get_all_users(self) -> dict[str, dict[str, Any]]:
        """
        Get all stored users.

        Returns:
            Dictionary mapping email addresses to user information
        """
        logger.debug(f"Retrieved all users - count: {len(self._users)}")
        return self._users.copy()

    def clear_all_users(self) -> None:
        """Clear all stored users (mainly for testing purposes)."""
        user_count = len(self._users)
        self._users.clear()
        logger.info(f"Cleared all users from memory - removed {user_count} users")

    # Email Access Control Methods

    def add_allowed_email(self, email: str) -> None:
        """
        Add an email to the allowlist.

        Args:
            email: Email address to allow access
        """
        if not email or "@" not in email:
            logger.warning(f"Invalid email format: {email}")
            return

        self._allowed_emails.add(email.lower())
        logger.info(f"Added email to allowlist: {email}")

    def add_allowed_emails(self, emails: list[str]) -> None:
        """
        Add multiple emails to the allowlist.

        Args:
            emails: List of email addresses to allow access
        """
        valid_emails = []
        for email in emails:
            if email and "@" in email:
                self._allowed_emails.add(email.lower())
                valid_emails.append(email)
            else:
                logger.warning(f"Skipping invalid email format: {email}")

        logger.info(f"Added {len(valid_emails)} emails to allowlist: {valid_emails}")

    def remove_allowed_email(self, email: str) -> bool:
        """
        Remove an email from the allowlist.

        Args:
            email: Email address to remove from allowlist

        Returns:
            True if email was removed, False if not found
        """
        email_lower = email.lower()
        if email_lower in self._allowed_emails:
            self._allowed_emails.remove(email_lower)
            logger.info(f"Removed email from allowlist: {email}")
            return True
        else:
            logger.warning(f"Email not found in allowlist: {email}")
            return False

    def is_email_allowed(self, email: str) -> bool:
        """
        Check if an email address is allowed access.

        Args:
            email: Email address to check

        Returns:
            True if email is in the allowlist, False otherwise
        """
        if not email:
            return False

        is_allowed = email.lower() in self._allowed_emails
        logger.debug(f"Email access check for {email}: {'allowed' if is_allowed else 'denied'}")
        return is_allowed

    def get_allowed_emails(self) -> list[str]:
        """
        Get all allowed email addresses.

        Returns:
            List of allowed email addresses
        """
        return sorted(list(self._allowed_emails))

    def clear_allowed_emails(self) -> None:
        """Clear all allowed emails (mainly for testing purposes)."""
        email_count = len(self._allowed_emails)
        self._allowed_emails.clear()
        logger.info(f"Cleared all allowed emails - removed {email_count} emails")

    def get_access_stats(self) -> dict[str, Any]:
        """
        Get statistics about access control.

        Returns:
            Dictionary containing access control statistics
        """
        return {
            "total_allowed_emails": len(self._allowed_emails),
            "total_stored_users": len(self._users),
            "users_with_access": len([email for email in self._users.keys() if self.is_email_allowed(email)]),
            "allowed_emails": self.get_allowed_emails(),
        }


# Global singleton instance
_user_service: UserService | None = None


def get_user_service() -> UserService:
    """
    Get the singleton UserService instance.

    Returns:
        The global UserService instance
    """
    global _user_service
    if _user_service is None:
        _user_service = UserService()
    return _user_service
