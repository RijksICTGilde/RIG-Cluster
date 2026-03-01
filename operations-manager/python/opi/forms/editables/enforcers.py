from __future__ import annotations

from typing import Any


class AdminRequiredEnforcer:
    """Ensures at least one user has role='admin'."""

    def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        """
        Args:
            value: List of user dicts, each with 'email' and 'role' keys.
            context: Not used.

        Returns:
            The value unchanged if valid.

        Raises:
            ValueError: If no user has role='admin'.
        """
        if not value or not isinstance(value, list):
            raise ValueError("Er moet minimaal één gebruiker zijn")
        has_admin = any(isinstance(user, dict) and user.get("role") == "admin" for user in value)
        if not has_admin:
            raise ValueError("Er moet minimaal één administrator zijn")
        return value


class UniqueNamesEnforcer:
    """Ensures all items in a sequence have unique values for a given field."""

    def __init__(self, field_name: str = "name") -> None:
        self.field_name = field_name

    def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        """
        Args:
            value: List of dicts (e.g., components, deployments).
            context: Not used.

        Returns:
            The value unchanged if valid.

        Raises:
            ValueError: If duplicate names found.
        """
        if not value or not isinstance(value, list):
            return value
        names: list[str] = []
        for item in value:
            if isinstance(item, dict):
                name = item.get(self.field_name)
                if name:
                    names.append(str(name))
        duplicates = [name for name in set(names) if names.count(name) > 1]
        if duplicates:
            dup_str = ", ".join(sorted(duplicates))
            raise ValueError(f"Dubbele namen gevonden: {dup_str}")
        return value


class ServiceDependencyEnforcer:
    """Ensures component services are valid project-level services."""

    def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        """
        Args:
            value: List of service name strings (component's uses-services).
            context: Must contain 'project_services' key with list of valid service names.

        Returns:
            The value unchanged if valid.

        Raises:
            ValueError: If any service is not in the project services list.
        """
        if not value or not isinstance(value, list):
            return value
        project_services = context.get("project_services", [])
        if not project_services:
            return value
        invalid = [s for s in value if s not in project_services]
        if invalid:
            invalid_str = ", ".join(invalid)
            raise ValueError(f"Ongeldige services: {invalid_str}. Beschikbare services: {', '.join(project_services)}")
        return value
