"""
Project service for managing project mappings.

This service provides centralized management of projects including:
- In-memory storage of project ID to project data mappings
- Loading projects from project files on startup
- Registration of new projects when they are created
- Validation and lookup functionality for API endpoints
"""

import logging
from typing import Any

from pydantic import BaseModel

from opi.core.config import settings


class ProjectUser(BaseModel):
    """Pydantic model for project user."""

    email: str
    role: str


logger = logging.getLogger(__name__)


class Project(BaseModel):
    """Pydantic model for project mapping."""

    name: str
    api_key: str
    filename: str
    users: list[ProjectUser] | None = None
    data: dict[str, Any] | None = None  # Full project YAML data


class ProjectService:
    """Service for managing project mappings."""

    _instance: "ProjectService | None" = None
    _initialized: bool = False

    def __new__(cls) -> "ProjectService":
        """Ensure only one instance of ProjectService exists (Singleton pattern)."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        # Only initialize once
        if not ProjectService._initialized:
            # In-memory storage for project mappings
            # In the future, this will be replaced with database tables
            self._projects: dict[str, Project] = {}
            ProjectService._initialized = True
            logger.debug("ProjectService singleton initialized")
        else:
            logger.debug("ProjectService singleton already initialized")

    def register(
        self,
        project_name: str,
        api_key: str,
        filename: str,
        users: list[ProjectUser] | None = None,
        data: dict[str, Any] | None = None,
    ) -> bool:
        """
        Register a project with its corresponding data.

        Args:
            project_name: The project identifier
            api_key: The API key for the project
            filename: The project configuration filename
            users: List of project users with their roles
            data: Full project YAML data

        Returns:
            True if registration was successful, False if project already exists and overwrite not allowed
        """
        if project_name in self._projects and not settings.ALLOW_PROJECTFILES_OVERWRITE:
            logger.warning(f"Project already exists: {project_name} (overwrite not allowed)")
            return False

        project = Project(name=project_name, api_key=api_key, filename=filename, users=users, data=data)
        self._projects[project_name] = project
        logger.debug(f"Registered project: {project_name} (file: {filename}) with {len(users) if users else 0} users")
        return True

    def get_project_by_api_key(self, api_key: str) -> Project | None:
        """
        Get project by API key.

        Args:
            api_key: The API key to look up

        Returns:
            Project name if found, None otherwise
        """
        for name, project in self._projects.items():
            if project.api_key == api_key:
                logger.debug(f"Found project for API key: {name}")
                return project

        logger.debug("No project found for provided API key")
        return None

    def get_project(self, project_name: str) -> Project | None:
        """
        Get project data for a specific project.

        Args:
            project_name: The project identifier

        Returns:
            Project object if found, None otherwise
        """
        project = self._projects.get(project_name)
        if project:
            logger.debug(f"Retrieved project: {project_name}")
        else:
            logger.debug(f"No project found: {project_name}")
        return project

    def get_api_key_for_project(self, project_name: str) -> str | None:
        """
        Get API key for a specific project.

        Args:
            project_name: The project identifier

        Returns:
            API key if found, None otherwise
        """
        project = self._projects.get(project_name)
        if project:
            logger.debug(f"Retrieved API key for project: {project_name}")
            return project.api_key
        else:
            logger.debug(f"No project found: {project_name}")
            return None

    def remove_project(self, project_name: str) -> bool:
        """
        Remove project mapping.

        Args:
            project_name: The project identifier

        Returns:
            True if removed, False if not found
        """
        if project_name in self._projects:
            del self._projects[project_name]
            logger.debug(f"Removed project: {project_name}")
            return True

        logger.debug(f"No project found to remove: {project_name}")
        return False

    def get_all_projects(self) -> dict[str, Project]:
        """
        Get all project mappings.

        Returns:
            Dictionary of project_name -> Project mappings
        """
        return self._projects.copy()

    def clear_all_projects(self) -> None:
        """Clear all project mappings. Primarily for testing."""
        self._projects.clear()
        logger.debug("Cleared all project mappings")

    def load_project_from_data(self, project_data: dict[str, Any], filename: str) -> bool:
        """
        Load project from a project data dictionary.

        Args:
            project_data: Project configuration data
            filename: The project configuration filename

        Returns:
            True if project was loaded successfully, False otherwise
        """
        try:
            project_name = project_data.get("name")
            if not project_name:
                logger.warning("Project data missing 'name' field")
                return False

            # Extract API key from config section
            config = project_data.get("config", {})
            api_key = config.get("api-key")

            if not api_key:
                logger.warning(f"No API key found in project config for: {project_name}")
                return False

            # Extract users from project data
            users_data = project_data.get("users", [])
            users = []
            if users_data and isinstance(users_data, list):
                for user_data in users_data:
                    if isinstance(user_data, dict) and "email" in user_data and "role" in user_data:
                        users.append(ProjectUser(email=user_data["email"], role=user_data["role"]))

            # Note: At startup, we allow overwriting since we're loading from authoritative source
            # Temporarily allow overwrite for this call
            original_setting = settings.ALLOW_PROJECTFILES_OVERWRITE
            settings.ALLOW_PROJECTFILES_OVERWRITE = True
            success = self.register(project_name, str(api_key), filename, users if users else None)
            settings.ALLOW_PROJECTFILES_OVERWRITE = original_setting

            if success:
                logger.debug(f"Loaded project from project data: {project_name} (file: {filename})")
                return success
            else:
                logger.error(f"Failed to register project: {project_name}")
                return False

        except Exception:
            logger.exception("Error loading project from project data")
            return False

    def get_project_users(self, project_name: str) -> list[ProjectUser] | None:
        """
        Get users for a specific project.

        Args:
            project_name: The project identifier

        Returns:
            List of project users if found, None otherwise
        """
        project = self._projects.get(project_name)
        if project:
            logger.debug(f"Retrieved {len(project.users) if project.users else 0} users for project: {project_name}")
            return project.users
        else:
            logger.debug(f"No project found: {project_name}")
            return None

    def is_user_authorized_for_project(self, project_name: str, user_email: str) -> bool:
        """
        Check if a user is authorized to access a specific project.

        Args:
            project_name: The project identifier
            user_email: The user's email address

        Returns:
            True if user is authorized, False otherwise
        """
        users = self.get_project_users(project_name)
        if not users:
            logger.debug(f"No users found for project: {project_name}")
            return False

        for user in users:
            if user.email.lower() == user_email.lower():
                logger.debug(f"User {user_email} authorized for project {project_name} with role: {user.role}")
                return True

        logger.debug(f"User {user_email} not authorized for project: {project_name}")
        return False

    def get_user_role_for_project(self, project_name: str, user_email: str) -> str | None:
        """
        Get a user's role for a specific project.

        Args:
            project_name: The project identifier
            user_email: The user's email address

        Returns:
            User's role if found, None otherwise
        """
        users = self.get_project_users(project_name)
        if not users:
            return None

        for user in users:
            if user.email.lower() == user_email.lower():
                logger.debug(f"Found role {user.role} for user {user_email} in project: {project_name}")
                return user.role

        logger.debug(f"No role found for user {user_email} in project: {project_name}")
        return None


def get_project_service() -> ProjectService:
    """
    Get the singleton project service instance.

    Returns:
        The singleton ProjectService instance
    """
    return ProjectService()


def initialize_project_service() -> ProjectService:
    """
    Initialize and return the singleton project service.
    This is called during application startup.

    Returns:
        The singleton ProjectService instance
    """
    service = ProjectService()
    logger.info("Project service singleton ready")
    return service
