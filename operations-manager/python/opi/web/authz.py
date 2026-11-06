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
from opi.core.step_up import has_fresh_step_up, is_elevated_admin
from opi.manager.clone_validation import new_or_changed_clone_sources
from opi.services.project_leden import lid_met_naam
from opi.services.project_roles import PLATFORM_ADMIN_ROLES, build_resource, member_roles, resource_path
from opi.services.project_store import get_project_store
from opi.services.user_service import get_user_service

if TYPE_CHECKING:
    from collections.abc import Sequence

    from opi.core.authz.engine import Decision, Resource
    from opi.services.project_store import ProjectSummary

logger = logging.getLogger(__name__)

#: De 403 van een te smalle rol. Eén tekst, want :func:`authorize` en
#: :func:`require_action` weigeren om dezelfde reden.
TE_SMALLE_ROL = "Onvoldoende rechten voor deze handeling op dit project"


def portal_principal(request: Request, project: ProjectSummary) -> Principal:
    """De aanroeper als principal op DIT project.

    Een platformbeheerder met verhoogde rechten krijgt op elk project dezelfde rollen als
    hij nu overal ``admin`` krijgt (B6); hij hoeft geen lid te zijn.
    """
    email = str((get_current_user(request) or {}).get("email", "")).lower()
    if is_elevated_admin(get_user_service(), email):
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
    project: ProjectSummary | None = None,
) -> tuple[ProjectSummary, str]:
    """Laat deze action op dit project toe, of weiger hem. Geeft (project, e-mailadres).

    Een ontbrekende step-up is hier GEEN weigering: ``@requires_step_up`` heeft daar zijn
    eigen antwoord voor (de dialoog), en dat antwoord gaat verloren als deze poort er een
    kale 403 van maakt.

    ``project`` weegt tegen een projectstaat die de store niet heeft, zie :func:`_weigh`.

    Raises:
        HTTPException: 404 als het project niet bestaat, 403 als de rollen te weinig openen.
    """
    if project is None:
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


def _weigh(
    request: Request,
    *,
    action: Action,
    project_name: str,
    deployment: str | None = None,
    component: str | None = None,
    project: ProjectSummary | None = None,
) -> tuple[Resource, Decision] | None:
    """The resource and the outcome, or ``None`` when the project does not exist.

    Without :func:`record_decision`: the caller decides whether this weighing is worth
    an audit row. A fresh step-up counts as present here, because it has its own answer
    and is not a refusal on the role.

    ``project`` weighs against a project state the store does not hold: the restore builds
    the deployment it is about to write, and that deployment's own marking is what decides
    (RC-417). Omitted, the stored project is read.
    """
    if project is None:
        project = get_project_store().get(project_name)
    if project is None:
        return None
    resource = build_resource(
        project,
        project_name=project_name,
        path=resource_path(project_name, deployment, component),
        deployment=deployment,
        component=component,
    )
    return resource, decide(portal_principal(request, project), action, resource, step_up_fresh=True)


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
    weging = _weigh(request, action=action, project_name=project_name, deployment=deployment, component=component)
    return weging is not None and weging[1].allowed


def _deployment_names(project: ProjectSummary | None) -> list[str]:
    deployments = (getattr(project, "data", None) or {}).get("deployments") or []
    return [str(d["name"]) for d in deployments if isinstance(d, dict) and d.get("name")]


def may_on_any_deployment(request: Request, *, action: Action, project_name: str) -> bool:
    """Of deze action op MINSTENS EEN deployment van dit project open is.

    De bodem voor een stroom die zijn deployment pas in een latere stap kiest. Zonder
    deployment weegt een action tegen de hoogste markering van het project, en dan kan een
    developer in een project met productie niet eens meer de restore-wizard openen terwijl
    hij tussen twee niet-productie-deployments wel mag terugzetten (RC-417).
    """
    namen = _deployment_names(get_project_store().get(project_name))
    if not namen:
        # Zonder deployments is er geen doelwit om op te wegen, en dan geldt de weging op
        # het project, net als bij elke andere action zonder deployment.
        return may(request, action=action, project_name=project_name)
    return any(may(request, action=action, project_name=project_name, deployment=naam) for naam in namen)


def require_on_any_deployment(request: Request, needed: Action, project_name: str) -> None:
    """403 als *needed* op geen enkele deployment van dit project open is.

    De echte beslissing wordt gewogen en vastgelegd op de bron en het doel die de stroom
    uiteindelijk aanwijst. Alleen de WEIGERING hoort hier al in de audit, en die laat
    :func:`require_action` vastleggen op het project als geheel.
    """
    if may_on_any_deployment(request, action=needed, project_name=project_name):
        return
    require_action(request, needed, project_name)
    raise HTTPException(status_code=403, detail=TE_SMALLE_ROL)


def require_action(
    request: Request,
    needed: Action,
    project_name: str,
    *,
    deployment: str | None = None,
    project: ProjectSummary | None = None,
) -> None:
    """403 als de rollen *needed* niet openen; alleen de WEIGERING wordt vastgelegd.

    Voor een aanroep die zelf niets doet: een GET die een formulier herrendert, of een
    stap van een modal-wizard. De audit hoort geen rij te krijgen over een back-up of een
    herstel dat niet gebeurde, maar een geweigerde poging hoort er wel in te staan
    (``features/audit.md``).
    """
    weging = _weigh(request, action=needed, project_name=project_name, deployment=deployment, project=project)
    if weging is not None:
        resource, beslissing = weging
        if beslissing.allowed:
            return
        # De weging liet een verse step-up aan, maar de rij draagt de sessie zoals ze is:
        # een weigering hier is er altijd een op de ROL, en een rij die een tweede factor
        # meldt die er niet was leest een onderzoek verkeerd voor.
        record_decision(
            request, action=needed, resource=resource, decision=beslissing, step_up_fresh=has_fresh_step_up(request)
        )
    raise HTTPException(status_code=403, detail=TE_SMALLE_ROL)


def require_clone_source_access(
    request: Request,
    project_name: str,
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    extra_sources: Sequence[str] = (),
) -> None:
    """Weeg ``deployment.clone-from`` op de BRON van elke clone-from die deze opslag zet.

    Eén poort voor beide schrijfwegen uit het portaal: een deployment toevoegen met een
    clone-from, en een restore naar een nieuwe deployment. Klonen vanaf productie is ops,
    waar het ook naartoe gaat.

    ``extra_sources`` zijn bronnen die de NA-staat niet noemt: de restore neemt de config
    van een deployment over terwijl alleen de BACKUP-bron in ``clone-from`` landt, dus de
    config-bron is uit het projectbestand niet terug te vinden (RC-417 securityreview).

    Raises:
        HTTPException: 403 als de rollen de bron niet openen.
    """
    sources = new_or_changed_clone_sources(before, after)
    sources.extend(source for source in extra_sources if source and source not in sources)
    for source in sources:
        authorize(request, action=Action.DEPLOYMENT_CLONE_FROM, project_name=project_name, deployment=source)


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
