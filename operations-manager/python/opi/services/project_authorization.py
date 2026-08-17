"""Authorization decisions derived from project files.

Split out of ProjectService, which had grown three unrelated jobs in one class: the
project cache, these authorization checks, and a platform-admin allowlist. The cache
now lives in ProjectStore and the admin allowlist in UserService, so what is left here
is only the question "may this user touch this project", answered from the project's
``users`` list.

These read through ProjectStore like everything else, so there is exactly one way to
reach a project file.
"""

import logging
from typing import TYPE_CHECKING

from opi.services.project_store import get_project_store
from opi.services.user_service import get_user_service

if TYPE_CHECKING:
    from opi.services.project_service import ProjectUser

logger = logging.getLogger(__name__)

# The roles that may change a project, and therefore the roles that may hold its
# API key: that key opens every mutating per-project API route and carries no role
# of its own, so handing it out wider than this gate widens the gate.
PROJECT_EDIT_ROLES: tuple[str, ...] = ("admin", "owner")


def get_project_users(project_name: str) -> list[ProjectUser] | None:
    """Return the members of a project, or None when the project is unknown."""
    project = get_project_store().get(project_name)
    if project is None:
        logger.debug(f"No project found: {project_name}")
        return None
    logger.debug(f"Retrieved {len(project.users) if project.users else 0} users for project: {project_name}")
    return project.users


def is_user_authorized_for_project(project_name: str, user_email: str) -> bool:
    """Check whether a user may access a project. Platform admins always may."""
    if get_user_service().is_platform_admin(user_email):
        logger.debug(f"User {user_email} authorized for project {project_name} (admin)")
        return True

    users = get_project_users(project_name)
    if not users:
        logger.debug(f"No users found for project: {project_name}")
        return False

    for user in users:
        if user.email.lower() == user_email.lower():
            logger.debug(f"User {user_email} authorized for project {project_name} with role: {user.role}")
            return True

    logger.debug(f"User {user_email} not authorized for project: {project_name}")
    return False


def get_user_role_for_project(project_name: str, user_email: str) -> str | None:
    """Return a user's role in a project. Platform admins always get "admin"."""
    if get_user_service().is_platform_admin(user_email):
        return "admin"

    users = get_project_users(project_name)
    if not users:
        return None

    for user in users:
        if user.email.lower() == user_email.lower():
            return user.role

    return None
