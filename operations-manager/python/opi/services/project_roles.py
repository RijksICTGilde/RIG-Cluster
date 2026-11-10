"""A member's roles, and the resource a call works on.

One reader for both questions, so the API and the portal feed the same decision. As long as
the project file still carries ``role: x`` (until schema version 2.13),
:func:`member_roles` translates that single role into the list the authorization engine
knows.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opi.core.actor import ActorKind, get_actor
from opi.core.authz.engine import Principal, PrincipalKind, Resource, Role, decide, effective_roles
from opi.services.project import Project
from opi.services.project_environment import Omgeving, effective_environment, highest_environment
from opi.services.step_up import is_elevated_admin
from opi.services.user_service import get_user_service

if TYPE_CHECKING:
    from opi.core.authz.actions import Action
    from opi.services.project_service import ProjectSummary, ProjectUser

logger = logging.getLogger(__name__)

#: The translation of the old role in the project file into the roles of the authorization
#: engine. ``owner`` and ``member`` go with schema version 2.13.
LEGACY_ROLES: dict[str, frozenset[Role]] = {
    "admin": frozenset({Role.ADMIN, Role.OPS}),
    "owner": frozenset({Role.ADMIN, Role.OPS}),
    "developer": frozenset({Role.DEVELOPER}),
    "member": frozenset({Role.VIEWER}),
}

#: What a platform admin holds on EVERY project.
PLATFORM_ADMIN_ROLES: frozenset[Role] = frozenset({Role.ADMIN, Role.OPS})

#: What the creator of a new project gets. Admin alone manages members but cannot edit the
#: project: that takes developer or ops.
CREATOR_ROLES: frozenset[Role] = frozenset({Role.ADMIN, Role.OPS, Role.DEVELOPER})


def roles_from_value(value: Any) -> frozenset[Role]:
    """The roles out of a stored value: one old role, or a list of keys.

    An unknown key opens nothing and is logged: ignoring it quietly would make a typo in a
    project file invisible.
    """
    if isinstance(value, str):
        legacy = LEGACY_ROLES.get(value)
        if legacy is None:
            logger.warning("Onbekende rol '%s' in een projectbestand; die opent niets", value)
            return frozenset()
        return legacy
    if not isinstance(value, list):
        return frozenset()
    roles: set[Role] = set()
    for key in value:
        try:
            roles.add(Role(key))
        except ValueError:
            logger.warning("Onbekende rol '%s' in een projectbestand; die opent niets", key)
    return frozenset(roles)


def member_roles(member: ProjectUser) -> frozenset[Role]:
    """The roles of this member."""
    return roles_from_value(member.roles)


def actor_may_set(action: Action, project_data: dict[str, Any], *, environment: Omgeving) -> bool:
    """Whether the signed-in user may do this action on a deployment with this marking.

    For a choice list, which has no request of its own: the actor comes from the contextvar
    the entry point sets. Without a signed-in person this says yes, because then no screen
    is rendered for a user and enforcement happens on save.
    """
    actor = get_actor()
    if actor.kind is not ActorKind.USER or not actor.identity:
        return True
    email = actor.identity.lower()
    if is_elevated_admin(get_user_service(), email):
        roles = PLATFORM_ADMIN_ROLES
    else:
        roles = frozenset()
        for member in project_data.get("users") or []:
            if isinstance(member, dict) and str(member.get("email", "")).lower() == email:
                roles = roles_from_value(member.get("roles", member.get("role")))
                break
    name = str(project_data.get("name", ""))
    principal = Principal(kind=PrincipalKind.HUMAN, name=email, project=name, roles=roles, actor=actor)
    resource = Resource(project=name, path=resource_path(name), environment=environment.value)
    return decide(principal, action, resource, step_up_fresh=True).allowed


def project_has_production(project_data: dict[str, Any]) -> bool:
    """Whether one of the project's deployments is effectively in production."""
    return highest_environment(project_data) is Omgeving.PRODUCTIE


def deployment_marking(project_data: dict[str, Any], deployment_name: str | None) -> str | None:
    """The effective marking of this deployment, or None when there is no deployment."""
    if not deployment_name:
        return None
    deployment = Project(project_data).find("deployments", name=deployment_name)
    return effective_environment(deployment).value if deployment is not None else None


def component_in_production(project_data: dict[str, Any], component_name: str | None) -> bool:
    """Whether a deployment in production uses this component."""
    if not component_name:
        return False
    for deployment in project_data.get("deployments") or []:
        if not isinstance(deployment, dict):
            continue
        if effective_environment(deployment) is not Omgeving.PRODUCTIE:
            continue
        for component in deployment.get("components") or []:
            if isinstance(component, dict) and component.get("reference") == component_name:
                return True
    return False


def resource_path(project_name: str, deployment: str | None = None, component: str | None = None) -> str:
    """The path a call works on, for the scope and for the audit."""
    path = f"project/{project_name}"
    if deployment:
        path += f"/deployment/{deployment}"
    if component:
        path += f"/component/{component}"
    return path


def build_resource(
    project: ProjectSummary | None,
    *,
    project_name: str,
    path: str,
    deployment: str | None = None,
    component: str | None = None,
    marking_if_absent: str | None = None,
) -> Resource:
    """The resource a call works on, out of ONE read of the project file.

    A target this call does not NAME weighs as the strictest in this project: the highest
    marking of its deployments, and "in production" for a component. Otherwise every route
    with its deployment in the body or the query would read as non-production, and a
    developer grant there would open a production deployment too.

    Without a project (a route that has none, or an unknown project) everything stays on the
    default marking, and the route gives its own 404.

    *marking_if_absent* is the marking of a named deployment that does not exist, for a
    route that declared one with :func:`opi.core.authz.actions.marking_if_absent`.
    """
    data: dict[str, Any] = (getattr(project, "data", None) or {}) if project is not None else {}
    has_production = project_has_production(data)
    return Resource(
        project=project_name,
        path=path,
        deployment=deployment,
        environment=deployment_marking(data, deployment)
        or (marking_if_absent if deployment else None)
        or highest_environment(data).value,
        component=component,
        component_in_production=component_in_production(data, component) if component else has_production,
        project_has_production=has_production,
    )


def has_role(value: Any, role: Role) -> bool:
    """Whether a stored role value covers *role*, through the stacking in INCLUDES."""
    return role in effective_roles(roles_from_value(value))
