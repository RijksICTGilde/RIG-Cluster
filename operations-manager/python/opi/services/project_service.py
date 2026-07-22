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
from opi.services.schema_migration import migrate_to_latest
from opi.services.user_service import get_user_service
from opi.utils.age import decrypt_age_content_sync, is_age_encrypted


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

    _instance: ProjectService | None = None
    _initialized: bool = False

    def __new__(cls) -> ProjectService:
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

        This updates the in-memory project registry. If the project already exists,
        it will be updated with the new data.

        Args:
            project_name: The project identifier
            api_key: The API key for the project
            filename: The project configuration filename
            users: List of project users with their roles
            data: Full project YAML data

        Returns:
            True if registration was successful
        """
        is_update = project_name in self._projects
        project = Project(name=project_name, api_key=api_key, filename=filename, users=users, data=data)
        self._projects[project_name] = project
        action = "Updated" if is_update else "Registered"
        logger.debug(f"{action} project: {project_name} (file: {filename}) with {len(users) if users else 0} users")

        # Keep the global access allowlist in sync with project membership so a
        # newly added member can reach the portal immediately, without waiting
        # for the next periodic Git refresh or an app restart. Without this, a
        # member added via the team-edit modal (which only calls register) is
        # redirected to /permission-denied until the allowlist is rebuilt.
        if users:
            member_emails = [
                (user.get("email") if isinstance(user, dict) else getattr(user, "email", None)) for user in users
            ]
            get_user_service().add_allowed_emails([email for email in member_emails if email])

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

    def replace_all_projects(self, projects: dict[str, Project]) -> None:
        """Atomically replace all project mappings.

        This avoids the race condition of clear-then-rebuild, where concurrent
        requests would see an empty or partially populated cache.
        """
        self._projects = projects
        logger.debug("Replaced all project mappings (%d projects)", len(projects))

    def clear_all_projects(self) -> None:
        """Clear all project mappings. Primarily for testing."""
        self._projects.clear()
        logger.debug("Cleared all project mappings")

    def _resolve_plaintext_api_key(self, project_name: str, api_key: str, config: dict[str, Any]) -> str:
        """Return the plaintext API key for in-memory registration.

        ``config.api-key`` is AGE-encrypted with the project key, which is in
        turn AGE-encrypted with the operations-manager key. A plaintext value
        (legacy/test data) is returned as-is. When decryption fails, the
        previously registered plaintext is kept so a bad save cannot break
        API authentication for the project.
        """
        if not is_age_encrypted(api_key):
            return api_key

        encoded_private_key = config.get("age-private-key")
        if encoded_private_key and settings.SOPS_AGE_PRIVATE_KEY:
            private_key = decrypt_age_content_sync(str(encoded_private_key), settings.SOPS_AGE_PRIVATE_KEY)
            if private_key:
                plaintext = decrypt_age_content_sync(api_key, private_key)
                if plaintext:
                    return plaintext

        existing = self._projects.get(project_name)
        if existing and not is_age_encrypted(existing.api_key):
            logger.warning(f"Could not decrypt api-key for project '{project_name}'; keeping previously registered key")
            return existing.api_key

        logger.warning(
            f"Could not decrypt api-key for project '{project_name}' and no usable previous key; "
            "API key authentication for this project will fail until it is re-registered"
        )
        return api_key

    def build_project_from_data(self, project_data: dict[str, Any], filename: str) -> Project | None:
        """Parse project data into a Project WITHOUT registering it.

        Split out so a caller that needs the whole set before swapping it in
        (ProjectStore.bootstrap) builds fully-formed entries -- in particular
        with the api-key already decrypted. Registering the raw ciphertext and
        decrypting in a second pass leaves a window in which API-key
        authentication compares against the ciphertext.

        Returns None when the data cannot be loaded (no name, no api-key).
        """
        try:
            project_data, _ = migrate_to_latest(project_data)

            project_name = project_data.get("name")
            if not project_name:
                logger.warning("Project data missing 'name' field")
                return None

            # Extract API key from config section
            config = project_data.get("config", {})
            api_key = config.get("api-key")

            if not api_key:
                logger.warning(f"No API key found in project config for: {project_name}")
                return None

            # Project files store api-key AGE-encrypted, but every API-layer
            # comparison runs against the registered value as plaintext.
            # Registering the ciphertext here poisons the in-memory key and
            # 401s all API calls for the project until the next process run
            # re-registers the decrypted key.
            api_key = self._resolve_plaintext_api_key(project_name, str(api_key), config)

            # Extract users from project data
            users_data = project_data.get("users", [])
            users = []
            if users_data and isinstance(users_data, list):
                users.extend(
                    ProjectUser(email=user_data["email"], role=user_data["role"])
                    for user_data in users_data
                    if isinstance(user_data, dict) and "email" in user_data and "role" in user_data
                )

            return Project(
                name=str(project_name),
                api_key=str(api_key),
                filename=filename,
                users=users or None,
                data=project_data,
            )
        except Exception:
            logger.exception("Error building project from project data")
            return None

    def load_project_from_data(self, project_data: dict[str, Any], filename: str) -> bool:
        """
        Load project from a project data dictionary.

        Args:
            project_data: Project configuration data
            filename: The project configuration filename

        Returns:
            True if project was loaded successfully, False otherwise
        """
        project = self.build_project_from_data(project_data, filename)
        if project is None:
            return False

        success = self.register(project.name, project.api_key, project.filename, project.users, data=project.data)

        if success:
            logger.debug(f"Loaded project from project data: {project.name} (file: {filename})")
            return success

        logger.error(f"Failed to register project: {project.name}")
        return False


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
