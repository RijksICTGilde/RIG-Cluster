"""De rollen van een lid, en de resource waarop een aanroep werkt.

Eén lezer voor beide vragen, zodat API en portaal dezelfde beslissing voeden. Zolang het
projectbestand nog ``role: x`` draagt (tot schemaversie 2.13) vertaalt
:func:`member_roles` die ene rol naar de lijst die het rechtensysteem kent.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opi.core.actor import ActorKind, get_actor
from opi.core.authz.engine import Principal, PrincipalKind, Resource, Role, decide, effective_roles
from opi.core.project_environment import Omgeving, effective_environment, highest_environment
from opi.services.user_service import get_user_service

if TYPE_CHECKING:
    from opi.core.authz.actions import Action
    from opi.services.project_service import ProjectSummary, ProjectUser

logger = logging.getLogger(__name__)

#: De vertaling van de oude rol in het projectbestand naar de rollen van het
#: rechtensysteem. ``owner`` en ``member`` verdwijnen met schemaversie 2.13 (B2).
LEGACY_ROLES: dict[str, frozenset[Role]] = {
    "admin": frozenset({Role.ADMIN, Role.OPS}),
    "owner": frozenset({Role.ADMIN, Role.OPS}),
    "developer": frozenset({Role.DEVELOPER}),
    "member": frozenset({Role.VIEWER}),
}

#: Wat een platformbeheerder op ELK project heeft, zoals hij nu overal ``admin`` krijgt (B6).
PLATFORM_ADMIN_ROLES: frozenset[Role] = frozenset({Role.ADMIN, Role.OPS})


def roles_from_value(value: Any) -> frozenset[Role]:
    """De rollen uit een opgeslagen waarde: één oude rol of een lijst sleutels.

    Een onbekende sleutel opent niets en wordt gelogd: stil negeren zou een typefout in
    een projectbestand onzichtbaar maken.
    """
    if isinstance(value, str):
        gevonden = LEGACY_ROLES.get(value)
        if gevonden is None:
            logger.warning("Onbekende rol '%s' in een projectbestand; die opent niets", value)
            return frozenset()
        return gevonden
    if not isinstance(value, list):
        return frozenset()
    rollen: set[Role] = set()
    for sleutel in value:
        try:
            rollen.add(Role(sleutel))
        except ValueError:
            logger.warning("Onbekende rol '%s' in een projectbestand; die opent niets", sleutel)
    return frozenset(rollen)


def member_roles(member: ProjectUser) -> frozenset[Role]:
    """De rollen van dit lid."""
    return roles_from_value(member.roles)


def actor_may_set(action: Action, project_data: dict[str, Any], *, environment: Omgeving) -> bool:
    """Of de ingelogde gebruiker deze action mag op een deployment met deze markering.

    Voor een keuzelijst, die geen verzoek kent: de actor komt uit de contextvar die de
    ingang zet. Zonder ingelogde persoon zegt dit ja, want dan rendert er geen scherm voor
    een gebruiker en beslist de handhaving bij het opslaan.
    """
    actor = get_actor()
    if actor.kind is not ActorKind.USER or not actor.identity:
        return True
    email = actor.identity.lower()
    if get_user_service().is_platform_admin(email):
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
    """Of een van de deployments van het project effectief op productie staat."""
    return highest_environment(project_data) is Omgeving.PRODUCTIE


def deployment_marking(project_data: dict[str, Any], deployment_name: str | None) -> str | None:
    """De effectieve markering van deze deployment, of None als er geen deployment is."""
    if not deployment_name:
        return None
    for deployment in project_data.get("deployments") or []:
        if isinstance(deployment, dict) and deployment.get("name") == deployment_name:
            return effective_environment(deployment).value
    return None


def component_in_production(project_data: dict[str, Any], component_name: str | None) -> bool:
    """Of een deployment op productie dit component gebruikt."""
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
    """Het pad waarop een aanroep werkt, voor de scope en voor de audit."""
    pad = f"project/{project_name}"
    if deployment:
        pad += f"/deployment/{deployment}"
    if component:
        pad += f"/component/{component}"
    return pad


def build_resource(
    project: ProjectSummary | None,
    *,
    project_name: str,
    path: str,
    deployment: str | None = None,
    component: str | None = None,
) -> Resource:
    """De resource waarop een aanroep werkt, uit één lezing van het projectbestand.

    Een doelwit dat deze aanroep niet NOEMT weegt als het strengste in dit project: de
    hoogste markering van zijn deployments, en "in productie" voor een component. Anders
    las elke route met zijn deployment in de body of de query als niet-productie, en
    opende een developer-grant daar ook een productiedeployment (RC-392 securityreview).

    Zonder project (een route die er geen heeft, of een onbekend project) blijft alles op
    de standaardmarkering staan, en geeft de route zelf zijn 404.
    """
    data: dict[str, Any] = (getattr(project, "data", None) or {}) if project is not None else {}
    heeft_productie = project_has_production(data)
    return Resource(
        project=project_name,
        path=path,
        deployment=deployment,
        environment=deployment_marking(data, deployment) or highest_environment(data).value,
        component=component,
        component_in_production=component_in_production(data, component) if component else heeft_productie,
        project_has_production=heeft_productie,
    )


def has_role(value: Any, role: Role) -> bool:
    """Of een opgeslagen rolwaarde *role* omvat, via de stapeling uit INCLUDES."""
    return role in effective_roles(roles_from_value(value))
