"""Het handhavingspunt van het portaal: één dependency die ``decide()`` aanroept.

Hij vervangt ``require_project_edit_access``, ``require_project_operate_access``,
``_require_project_member_access`` en de losse rolcontroles in ``opi/web/router.py``. De
action komt van de route zelf (``@action``), de resource uit één lezing van het
projectbestand.

Wat niet op een project werkt (de portaalpagina's, het platformbeheer) loopt hier niet
langs: die regels dragen een andere :class:`~opi.core.authz.engine.Audience`, en hun
handhaving blijft de sessiemiddleware respectievelijk ``require_platform_admin``.

Bij ELKE beslissing, ook een weigering, komen de vier attributen uit ontwerp 8.3 op
``request.state``. Taak 3 leest ze; daarom staan ze er ook als het verzoek eindigt in
een 403.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from jinja2 import pass_context
from starlette.requests import Request  # noqa: TC002 -- FastAPI leest de dependency op runtime

from opi.core.actor import user_actor
from opi.core.audit_trail import capture_fields
from opi.core.auth_decorators import get_current_user
from opi.core.authz.actions import Action, action_of, query_targets_of
from opi.core.authz.engine import (
    Audience,
    Cause,
    Principal,
    PrincipalKind,
    decide,
    record_decision,
    rule_for,
)
from opi.core.step_up import has_fresh_step_up
from opi.services.project_leden import lid_met_naam
from opi.services.project_roles import PLATFORM_ADMIN_ROLES, build_resource, member_roles, resource_path
from opi.services.project_store import get_project_store
from opi.services.user_service import get_user_service

if TYPE_CHECKING:
    from opi.services.project_store import ProjectSummary

logger = logging.getLogger(__name__)

#: De 403 van een te smalle rol. Eén tekst, want :func:`authorize` en
#: :func:`require_action` weigeren om dezelfde reden.
TE_SMALLE_ROL = "Onvoldoende rechten voor deze handeling op dit project"


def portal_principal(request: Request, project: ProjectSummary) -> Principal:
    """De aanroeper als principal op DIT project.

    Een platformbeheerder krijgt op elk project dezelfde rollen als hij nu overal
    ``admin`` krijgt (B6); hij hoeft geen lid te zijn.
    """
    email = str((get_current_user(request) or {}).get("email", "")).lower()
    if get_user_service().is_platform_admin(email):
        rollen = PLATFORM_ADMIN_ROLES
    else:
        lid = lid_met_naam(project, email)
        rollen = member_roles(lid) if lid is not None else frozenset()
    return Principal(
        kind=PrincipalKind.HUMAN,
        name=email,
        project=project.name,
        roles=rollen,
        actor=user_actor(email),
    )


def authorize(
    request: Request,
    *,
    action: Action,
    project_name: str,
    deployment: str | None = None,
    component: str | None = None,
) -> tuple[ProjectSummary, str]:
    """Laat deze action op dit project toe, of weiger hem. Geeft (project, e-mailadres).

    Een ontbrekende step-up is hier GEEN weigering: ``@requires_step_up`` heeft daar zijn
    eigen antwoord voor (de dialoog), en dat antwoord gaat verloren als deze poort er een
    kale 403 van maakt.

    Raises:
        HTTPException: 404 als het project niet bestaat, 403 als de rollen te weinig openen.
    """
    project = get_project_store().get(project_name)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' niet gevonden")

    principal = portal_principal(request, project)
    resource = build_resource(
        project,
        project_name=project_name,
        path=resource_path(project_name, deployment, component),
        deployment=deployment,
        component=component,
    )
    vers = has_fresh_step_up(request)
    beslissing = decide(principal, action, resource, step_up_fresh=vers)
    record_decision(request, action=action, resource=resource, decision=beslissing, step_up_fresh=vers)

    if beslissing.allowed or beslissing.cause is Cause.STEP_UP_REQUIRED or beslissing.step_up_would_help:
        return project, principal.name

    logger.info(
        "Geweigerd: %s op '%s' door '%s' (%s)",
        action.value,
        resource.path,
        principal.name,
        beslissing.cause.value if beslissing.cause else "onbekend",
    )
    if not principal.roles:
        raise HTTPException(status_code=403, detail="Geen toegang tot dit project")
    raise HTTPException(status_code=403, detail=TE_SMALLE_ROL)


def authorized_project(request: Request) -> ProjectSummary:
    """Het project dat de poort voor DIT verzoek al toeliet.

    Zo leest een handler het projectbestand uit de beslissing die er al is, in plaats van
    ``authorize`` nog een keer aan te roepen: dat zou een tweede beslissing over dezelfde
    vraag zijn, en de audit legt er een tweede rij voor vast.

    Alleen :func:`enforce_route_action` zet dit, zodat een tweede ``authorize`` voor een
    ANDERE action in de body het project van de route niet kan overschrijven.

    Raises:
        HTTPException: 500 als de poort niet gedraaid heeft, want dan is de route
            verkeerd opgehangen en is doorgaan zonder beslissing geen optie.
    """
    project = getattr(request.state, "authz_project", None)
    if project is None:
        raise HTTPException(status_code=500, detail="Autorisatiepoort niet gedraaid voor deze route")
    return project


def may(
    request: Request,
    *,
    action: Action,
    project_name: str,
    deployment: str | None = None,
    component: str | None = None,
) -> bool:
    """Of de ROLLEN deze action openen, los van een verse step-up.

    Voor een knop. De grens is :func:`authorize`; een knop die zich toont terwijl de
    step-up nog moet komen is juist de bedoeling, want de dialoog volgt bij het klikken.
    """
    project = get_project_store().get(project_name)
    if project is None:
        return False
    resource = build_resource(
        project,
        project_name=project_name,
        path=resource_path(project_name, deployment, component),
        deployment=deployment,
        component=component,
    )
    return decide(portal_principal(request, project), action, resource, step_up_fresh=True).allowed


def require_action(request: Request, needed: Action, project_name: str) -> None:
    """403 als de rollen *needed* niet openen, zonder er een beslissing van te maken.

    Voor een GET die alleen een formulier herrendert: de poort weegt het lezen, en de
    audit hoort geen rij te krijgen over een back-up of een herstel dat niet gebeurde.
    """
    if not may(request, action=needed, project_name=project_name):
        raise HTTPException(status_code=403, detail=TE_SMALLE_ROL)


def _doelwit(request: Request, uit_de_query: frozenset[str], naam: str) -> str | None:
    """De deployment of het component waar deze route op werkt, uit pad of query.

    De query telt alleen mee als de route zich met ``@targets_in_query`` verklaart. Zonder
    die verklaring kon een aanroeper een deployment bij plakken die de handler negeert, en
    dan weegt de poort een andere resource dan de handler gebruikt.
    """
    waarde = request.path_params.get(naam)
    if not waarde and naam in uit_de_query:
        waarde = request.query_params.get(naam)
    return str(waarde) if waarde else None


async def enforce_route_action(request: Request) -> None:
    """Handhaaf de action van DEZE route, voordat de handler draait.

    Als router-dependency geregistreerd, zodat er geen route is die de poort vergeet.
    Alleen projectrechten lopen hier langs: een route zonder action, een route zonder
    ``project_name`` in het pad, of een regel met een ander publiek gaat door.
    """
    route = request.scope.get("route")
    endpoint = getattr(route, "endpoint", None)
    gevraagd = action_of(endpoint) if endpoint is not None else None
    if gevraagd is None or rule_for(gevraagd).audience is not Audience.PROJECT:
        return

    project_name = request.path_params.get("project_name")
    if not project_name:
        return

    uit_de_query = query_targets_of(endpoint)
    project, _ = authorize(
        request,
        action=gevraagd,
        project_name=str(project_name),
        deployment=_doelwit(request, uit_de_query, "deployment_name"),
        component=_doelwit(request, uit_de_query, "component_name"),
    )
    request.state.authz_project = project

    # Voor de audit, en pas NA het toelaten, zoals ``handhaaf`` het op de API doet: de
    # hook draait na het antwoord en kan het formulier dan niet meer lezen, maar een
    # aanroeper die niets mag hoort geen body te laten ontleden.
    await capture_fields(request, endpoint)


@pass_context
def template_can(
    context: Any,
    action: Action | str,
    project: str | None = None,
    deployment: str | None = None,
    component: str | None = None,
) -> bool:
    """De helper ``can(...)`` voor de sjablonen.

    Een knop toont zich als de ROLLEN volstaan; de step-up volgt bij het klikken. Zo
    staat er geen rolnaam meer in een sjabloon, en loopt de knop langs dezelfde regels
    als de poort erachter.

    Als Jinja-global met de context erbij en niet als context_processor: een sjabloon dat
    los gerenderd wordt (een fragment, een test) heeft dan dezelfde helper, in plaats van
    een ``'can' is undefined`` die pas op de pagina opvalt. Zonder verzoek in de context
    mag niets, want er is niemand om iets toe te staan.
    """
    request = context.get("request")
    if request is None:
        return False
    naam = project or getattr(request, "path_params", {}).get("project_name")
    if not naam:
        return False
    return may(
        request,
        action=Action(action),
        project_name=str(naam),
        deployment=deployment,
        component=component,
    )
