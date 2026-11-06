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

from opi.core.step_up import is_elevated_admin
from opi.services.project_leden import human_members
from opi.services.project_roles import PLATFORM_ADMIN_ROLES
from opi.services.project_store import get_project_store
from opi.services.user_service import get_user_service

if TYPE_CHECKING:
    from opi.services.project_service import ProjectUser

logger = logging.getLogger(__name__)


def get_project_users(project_name: str) -> list[ProjectUser] | None:
    """De MENSEN in een project, of None als het project onbekend is.

    Alleen de mensen: deze twee functies beantwoorden "mag deze aanmelding dit project
    openen", en een service account meldt zich nergens aan. Wat een service account mag
    loopt langs :mod:`opi.api.principal`, dat de hele ledenlijst leest.
    """
    project = get_project_store().get(project_name)
    if project is None:
        logger.debug(f"No project found: {project_name}")
        return None
    mensen = human_members(project)
    logger.debug(f"Retrieved {len(mensen)} users for project: {project_name}")
    return mensen


def is_user_authorized_for_project(project_name: str, user_email: str, audit: bool = True) -> bool:
    """Check whether a user may access a project. An elevated platform admin always may.

    Geef ``audit=False`` mee in een lijst die dit per project vraagt: de INFO-regel moet
    het spoor zijn van een geopend project, niet van elk project op het platform.
    """
    if is_elevated_admin(get_user_service(), user_email):
        if audit:
            # INFO, anders is er met LOG_LEVEL=INFO in productie geen spoor van een
            # platformbeheerder die een project opent waar hij geen lid van is.
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
    """De rollen van deze gebruiker op dit project; een platformbeheerder krijgt die van B6.

    Leeg betekent "geen rol hier". Voor een beslissing: gebruik
    :func:`opi.web.authz.authorize`; dit is de lezing die een pagina in zijn context zet.
    """
    if is_elevated_admin(get_user_service(), user_email):
        return sorted(rol.value for rol in PLATFORM_ADMIN_ROLES)

    users = get_project_users(project_name)
    for user in users or []:
        if (user.email or "").lower() == user_email.lower():
            return list(user.roles)

    return []
