"""
Project service for managing project mappings.

This service provides centralized management of projects including:
- In-memory storage of project ID to project data mappings
- Loading projects from project files on startup
- Registration of new projects when they are created
- Validation and lookup functionality for API endpoints
"""

import logging
from datetime import datetime
from typing import Any

from pydantic import BaseModel, model_validator

from opi.core.authz.engine import role_label
from opi.services.members import MemberKind
from opi.services.project import Project
from opi.services.schema_migration import ROLE_MIGRATION, migrate_to_latest
from opi.services.user_service import get_user_service
from opi.utils.age import decrypt_age_content_sync, is_age_encrypted, project_private_key_or_none


class ProjectToken(BaseModel):
    """An issued token of a member, as it stands in the project file.

    The hash only: nobody can read the value itself after it was issued.
    """

    name: str
    expires: datetime
    hash: str


class ProjectUser(BaseModel):
    """A member of a project: a human with an email address, or a service account."""

    email: str | None = None
    roles: list[str]
    type: MemberKind = MemberKind.PERSON
    name: str | None = None
    tokens: list[ProjectToken] = []

    @model_validator(mode="before")
    @classmethod
    def accept_the_old_single_role(cls, data: Any) -> Any:  # noqa: ANN401  # a project-file value
        """``role: admin`` reads as ``roles: [...]``.

        The project store migrates a file as it reads it (schema version 2.13), but this model
        also reads data that did not come that way: a test fixture, a form submission, a file
        that has yet to be migrated. One translation table for all three, the same one the
        migration uses.
        """
        if isinstance(data, dict) and "role" in data and "roles" not in data:
            # An unknown role becomes NO role: this is reading and not migrating, and a
            # member with an unreadable role should open nothing. The migration does give
            # such a member ``viewer``, because the file has to be valid afterwards.
            data = {**data, "roles": list(ROLE_MIGRATION.get(str(data["role"]), ()))}
            data.pop("role", None)
        return data

    @model_validator(mode="after")
    def validate_principal_exists(self) -> ProjectUser:
        """A member has a principal: a human an email address, a service account a name.

        Without this rule a line with only a ``role`` becomes a member with an empty
        principal, and then every comparison against an empty email address matches it.
        """
        if self.type is MemberKind.SERVICE_ACCOUNT:
            if not self.name:
                raise ValueError("Een service account heeft een name nodig")
        elif not self.email:
            raise ValueError("Een lid heeft een email nodig")
        return self

    @property
    def rol_labels(self) -> list[str]:
        """The roles as a human sees them. An unknown key shows itself."""
        return [role_label(role) for role in self.roles]

    @property
    def principal(self) -> str:
        """The name this member stands under in the log and in the git trailer."""
        return (self.name or "") if self.type is MemberKind.SERVICE_ACCOUNT else (self.email or "")


logger = logging.getLogger(__name__)


class ProjectSummary(BaseModel):
    """Pydantic model for project mapping."""

    name: str
    #: The old project key, while the file is still on schema version 2.11. From 2.12 on it
    #: is a token of the ``project-api`` service account and this field is None.
    api_key: str | None = None
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
            self._projects: dict[str, ProjectSummary] = {}
            ProjectService._initialized = True
            logger.debug("ProjectService singleton initialized")
        else:
            logger.debug("ProjectService singleton already initialized")

    def register(
        self,
        project_name: str,
        api_key: str | None,
        filename: str,
        users: list[ProjectUser] | dict[str, Any] | None = None,
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
        # Callers pass the members as ProjectUser or as raw dicts out of the file. Converted
        # once here, so everything past this point works with real members and not two shapes.
        members = [
            member if isinstance(member, ProjectUser) else ProjectUser.model_validate(member) for member in users or []
        ]

        is_update = project_name in self._projects
        project = ProjectSummary(
            name=project_name, api_key=api_key, filename=filename, users=members or None, data=data
        )
        self._projects[project_name] = project
        action = "Updated" if is_update else "Registered"
        logger.debug(f"{action} project: {project_name} (file: {filename}) with {len(members)} users")

        # Keep the global access allowlist in sync with project membership so a
        # newly added member can reach the portal immediately, without waiting
        # for the next periodic Git refresh or an app restart. Without this, a
        # member added via the team-edit modal (which only calls register) is
        # redirected to /permission-denied until the allowlist is rebuilt.
        # The HUMANS only: a service account has no mailbox and does not belong on the login
        # list. See opi/services/project_members.py.
        humans = [member for member in members if member.type is not MemberKind.SERVICE_ACCOUNT]
        if humans:
            get_user_service().add_allowed_emails([member.email for member in humans if member.email])

        return True

    def get_project_by_api_key(self, api_key: str) -> ProjectSummary | None:
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

    def get_project(self, project_name: str) -> ProjectSummary | None:
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

    def get_all_projects(self) -> dict[str, ProjectSummary]:
        """
        Get all project mappings.

        Returns:
            Dictionary of project_name -> ProjectSummary mappings
        """
        return self._projects.copy()

    def replace_all_projects(self, projects: dict[str, ProjectSummary]) -> None:
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

    def _resolve_plaintext_api_key(self, project_name: str, api_key: str, project_data: dict[str, Any]) -> str | None:
        """Return the plaintext API key for in-memory registration.

        ``config.api-key`` is AGE-encrypted with the project key, which is in
        turn wrapped with the project-KEK. A plaintext value (legacy/test data)
        is returned as-is. When decryption fails, the previously registered
        plaintext is kept so a bad save cannot break API authentication for the
        project.
        """
        if not is_age_encrypted(api_key):
            return api_key

        private_key = project_private_key_or_none(project_data)
        if private_key:
            plaintext = decrypt_age_content_sync(api_key, private_key)
            if plaintext:
                return plaintext

        existing = self._projects.get(project_name)
        if existing and existing.api_key and not is_age_encrypted(existing.api_key):
            logger.warning(f"Could not decrypt api-key for project '{project_name}'; keeping previously registered key")
            return existing.api_key

        logger.warning(
            f"Could not decrypt api-key for project '{project_name}' and no usable previous key; "
            "API key authentication for this project will fail until it is re-registered"
        )
        # NOT the ciphertext: it would become its own password, so whoever can read the
        # project file could use it to pass as the project key.
        return None

    def build_project_from_data(self, project_data: dict[str, Any], filename: str) -> ProjectSummary | None:
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

            # Structural projection (name/users/api-key-as-stored) is produced by the
            # Project aggregate root; this service owns the crypto step below.
            summary = Project(project_data).get_summary(filename)
            if summary is None:
                logger.warning("Project data missing 'name' field")
                return None

            # Project files store api-key AGE-encrypted, but every API-layer
            # comparison runs against the registered value as plaintext.
            # Registering the ciphertext here poisons the in-memory key and
            # 401s all API calls for the project until the next process run
            # re-registers the decrypted key.
            if summary.api_key:
                summary.api_key = self._resolve_plaintext_api_key(summary.name, summary.api_key, project_data)
            return summary
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
