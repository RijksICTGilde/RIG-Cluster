"""Authorization decisions derived from project files.

One question only: "may this user touch this project", answered from the project's
``users`` list, read through ProjectStore like everything else.
"""

import logging
from typing import TYPE_CHECKING

from opi.services.project_members import human_members
from opi.services.project_roles import PLATFORM_ADMIN_ROLES
from opi.services.project_store import get_project_store
from opi.services.step_up import is_elevated_admin
from opi.services.user_service import get_user_service

if TYPE_CHECKING:
    from opi.services.project_service import ProjectUser

logger = logging.getLogger(__name__)


def get_project_users(project_name: str) -> list[ProjectUser] | None:
    """The HUMANS in a project, or None when the project is unknown.

    Humans only: these two functions answer "may this sign-in open this project", and a
    service account signs in nowhere. What a service account may do runs through
    :mod:`opi.http.auth.principal`, which reads the whole member list.
    """
    project = get_project_store().get(project_name)
    if project is None:
        logger.debug(f"No project found: {project_name}")
        return None
    humans = human_members(project)
    logger.debug(f"Retrieved {len(humans)} users for project: {project_name}")
    return humans


def is_user_authorized_for_project(project_name: str, user_email: str, audit: bool = True) -> bool:
    """Check whether a user may access a project. An elevated platform admin always may.

    Pass ``audit=False`` in a listing that asks this per project: the INFO line has to be the
    trace of an OPENED project, not of every project on the platform.
    """
    if is_elevated_admin(get_user_service(), user_email):
        if audit:
            # INFO, or with LOG_LEVEL=INFO in production there is no trace of a platform
            # admin opening a project they are not a member of.
            logger.info(f"User {user_email} authorized for project {project_name} (admin)")
        return True

    users = get_project_users(project_name)
    if not users:
        logger.debug(f"No users found for project: {project_name}")
        return False

    for user in users:
        if (user.email or "").lower() == user_email.lower():
            logger.debug(f"User {user_email} authorized for project {project_name} with roles: {user.roles}")
            return True

    logger.debug(f"User {user_email} not authorized for project: {project_name}")
    return False


def get_user_roles_for_project(project_name: str, user_email: str) -> list[str]:
    """This user's roles on this project; a platform admin gets PLATFORM_ADMIN_ROLES.

    Empty means "no role here". For a DECISION use
    :func:`opi.http.auth.authz.authorize`; this is the reading a page puts in its context.
    """
    if is_elevated_admin(get_user_service(), user_email):
        return sorted(role.value for role in PLATFORM_ADMIN_ROLES)

    users = get_project_users(project_name)
    for user in users or []:
        if (user.email or "").lower() == user_email.lower():
            return list(user.roles)

    return []
