import logging
import re
import secrets
from collections.abc import Callable, Iterable  # noqa: TC003 -- used in decorator signatures at runtime
from functools import wraps
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from opi.api.principal import (
    AccessDenied,
    NotRecognised,
    Principal,
    PrincipalKind,
    beslis,
    recognise_principal,
    tel_gebruik,
)
from opi.api.rate_limiter import refuse_after_auth_failure, refuse_when_key_failures_exhausted
from opi.api.user_token_auth import (
    REQUIRES_PLATFORM_ADMIN_TOKEN,
    REQUIRES_USER_TOKEN,
    UserTokenError,
    extract_bearer_token,
)
from opi.core.actor import set_actor
from opi.core.audit_trail import capture_fields
from opi.core.authz.actions import Action, action_of, deployment_of
from opi.core.authz.engine import RuleError, rule_for
from opi.core.config import settings
from opi.manager.run_support import find_deployment
from opi.services.project_roles import build_resource, resource_path
from opi.services.project_store import get_project_store
from opi.services.services_enums import CloneFromType
from starlette.requests import Request  # noqa: TC002 — FastAPI needs Request at runtime

if TYPE_CHECKING:
    from opi.services.project_service import ProjectSummary

REQUIRES_PROJECT_NAME = "zad_requires_project_name"
"""Markering op een route die met een projectsleutel wordt afgeschermd."""

#: De twee meldingen bij een bewijsmiddel dat niet herkend wordt. Een constante, want de
#: voorcontrole van de image-push geeft dezelfde 401 als de poort hier.
_GEEN_SLEUTEL = "Authentication required - provide X-API-Key header"
_FOUTE_SLEUTEL = "Invalid API key"


REQUIRES_METRICS_SCRAPE_TOKEN = "zad_requires_metrics_scrape_token"  # noqa: S105  # markering op een route, geen secret
"""Markering op een route die met PROMETHEUS_METRICS_AUTH_TOKEN wordt afgeschermd."""


def validate_api_token(func: Callable[..., Any]) -> Callable[..., Any]:
    """Eist project-specifieke X-API-Key, valideert tegen project_name in de route."""

    @wraps(func)
    async def wrapper(*args: Any, request: Request, **kwargs: Any) -> Any:
        logger = logging.getLogger(__name__)
        logger.debug(f"API route {func.__name__} called with authentication")

        x_api_key = request.headers.get("X-API-Key")
        if not x_api_key:
            logger.warning(f"Authentication failed for route {func.__name__} - no X-API-Key provided")
            raise HTTPException(status_code=401, detail=_GEEN_SLEUTEL)

        # ALWAYS require project_name parameter and validate it matches
        project_name_from_url = kwargs.get("project_name")

        if not project_name_from_url:
            # Een routefout, geen aanroepfout: de aanroeper kan hier niets aan doen.
            logger.error(
                f"Route {func.__name__} draagt validate_api_token maar kreeg geen project_name; "
                "zonder project valt een projectsleutel niet te controleren"
            )
            raise HTTPException(status_code=401, detail="Missing project_name parameter")

        refuse_when_key_failures_exhausted(x_api_key)

        project = get_project_store().get(project_name_from_url)
        if not project:
            # Niet onderscheiden van een verkeerde sleutel: of een project bestaat is zelf
            # al iets dat een onbevoegde aanroeper niet hoort te kunnen aflezen.
            logger.warning(f"Authentication failed for route {func.__name__} - unknown project")
            refuse_after_auth_failure(request, x_api_key)
            raise HTTPException(status_code=401, detail=_FOUTE_SLEUTEL)

        await handhaaf(request, x_api_key, wrapper, project, route_kwargs=kwargs)

        # Add project_id to kwargs so the route function can access it if needed
        kwargs["project_name"] = project.name
        return await func(*args, request=request, **kwargs)

    setattr(wrapper, REQUIRES_PROJECT_NAME, True)
    return wrapper


async def handhaaf(
    request: Request,
    presented: str,
    endpoint: Callable[..., Any],
    project: ProjectSummary,
    *,
    route_kwargs: dict[str, Any] | None = None,
) -> None:
    """Herken deze ``X-API-Key`` en laat de handeling van deze route toe.

    Een route legt hier zijn eigen endpoint-object in, want daar staat de markering die
    :func:`opi.core.authz.actions.action` erop zette. Gedeeld met de routers die hun
    sleutelcontrole met de hand doen (taken, slaapstand), zodat een token daar hetzelfde
    werkt als op de rest van de API.

    Het project komt als object binnen, want de aanroeper heeft het al moeten lezen om te
    weten dat het bestaat; nog een lezing hier zou hetzelfde verzoek op twee
    momentopnamen beslissen.

    Hoort het token bij een MENS, dan landt die hier op ``request.state.user``. Dat is de
    plek waar :func:`opi.api.otp_step_up.verify_api_step_up` de eigenaar van het token
    leest, zodat een step-up op een ``zad_``-token precies dezelfde weg loopt als op een
    SSO-bearer. Een service account en de oude projectsleutel laten hem leeg, en ketsen
    daar dus af op ``principal-kind-not-allowed``.

    Raises:
        HTTPException: 401 als het bewijsmiddel niet herkend wordt, 403 als het deze
            handeling hier niet opent.
    """
    logger = logging.getLogger(__name__)
    principaal = await _weeg(request, presented, endpoint, project, route_kwargs=route_kwargs)

    # De principal op het verzoek, zodat een handler een TWEEDE handeling kan laten
    # toetsen zonder het bewijsmiddel opnieuw te herkennen (:func:`eis_handeling`).
    request.state.authz_principal = principaal
    tel_gebruik(project.name, principaal.kind)
    if principaal.kind is PrincipalKind.HUMAN:
        request.state.user = {"email": principaal.name}
    logger.debug("Toegestaan: %s mag %s op %s", principaal.actor, action_of(endpoint), project.name)


async def _weeg(
    request: Request,
    presented: str,
    endpoint: Callable[..., Any],
    project: ProjectSummary,
    *,
    route_kwargs: dict[str, Any] | None = None,
) -> Principal:
    """Herken het bewijsmiddel en laat de handeling van ``endpoint`` toe, of weiger.

    Gedeeld met :func:`image_push_refusal`, die vóór de multipart-parser hetzelfde moet
    wegen als de route erachter: wat de voorcontrole beslist en wat de poort beslist kan
    zo niet uiteenlopen.

    Raises:
        HTTPException: 401 als het bewijsmiddel niet herkend wordt, 403 als het deze
            handeling hier niet opent.
    """
    logger = logging.getLogger(__name__)
    project_name = project.name
    gevraagde = action_of(endpoint)
    if gevraagde is None:
        # De opstartcontrole hoort dit onmogelijk te maken; komt het hier toch langs, dan
        # is stilzwijgend doorlaten de verkeerde uitkomst.
        raise RuntimeError(f"Route {getattr(endpoint, '__name__', endpoint)} draagt geen handeling")

    try:
        principaal = await recognise_principal(presented, project)
    except NotRecognised as exc:
        logger.warning("Authenticatie mislukt voor project %s: %s", project_name, exc)
        refuse_after_auth_failure(request, presented)
        raise HTTPException(status_code=401, detail=_FOUTE_SLEUTEL) from exc

    # Hier en niet na de beslissing: een WEIGERING hoort de handelende partij te noemen
    # (BIO2 8.15.01), en die is bekend zodra het bewijsmiddel herkend is. Gemeten op de
    # sandbox: zonder deze regel droeg een geweigerde API-aanroep een lege actor.
    set_actor(principaal.actor)

    # Voor de audit, en pas na het herkennen: de hook draait na het antwoord, als de
    # velden van dit verzoek niet meer te lezen zijn, en een onbekende aanroeper hoort
    # geen body te laten ontleden.
    await capture_fields(request, endpoint)

    # De deployment en het component uit het PAD, zodat de productiescopes op de API
    # hetzelfde wegen als in het portaal. Staat de deployment niet in het pad, dan zegt de
    # route met ``@deployment_from`` waar hij wel staat; zonder die verklaring weegt
    # ``build_resource`` de aanroep tegen de hoogste markering van het project.
    deployment = request.path_params.get("deployment_name") or deployment_of(endpoint, route_kwargs or {})
    component = request.path_params.get("component_name")
    resource = build_resource(
        project,
        project_name=project_name,
        path=resource_path(project_name, deployment, component),
        deployment=deployment,
        component=component,
    )

    try:
        beslis(principaal, project=project, handeling=gevraagde, resource=resource, request=request)
    except AccessDenied as exc:
        logger.warning("Geweigerd voor %s op %s (%s): %s", principaal.actor, project_name, gevraagde.value, exc)
        # De reden als eigen veld en niet alleen in de tekst: een aanroeper moet "deze
        # soort aanroeper mag dit nooit" kunnen onderscheiden van "je rol is te smal",
        # want dat zijn twee verschillende dingen om op te lossen.
        raise HTTPException(status_code=403, detail={"cause": exc.cause, "message": str(exc)}) from exc

    return principaal


async def image_push_refusal(
    request: Request, presented: str | None, project_name: str, endpoint: Callable[..., Any]
) -> HTTPException | None:
    """Controleer of dit bewijsmiddel ``image.push`` mag; return antwoord als weigering."""
    logger = logging.getLogger(__name__)
    project = get_project_store().get(project_name)
    if not presented or project is None:
        # Verberg of het project bestaat of de sleutel fout is.
        reden = "onbekend project" if presented else "geen X-API-Key"
        logger.warning("Authenticatie mislukt voor project %s: %s", project_name, reden)
        refuse_after_auth_failure(request, presented)
        return HTTPException(status_code=401, detail=_FOUTE_SLEUTEL if presented else _GEEN_SLEUTEL)

    try:
        await _weeg(request, presented, endpoint, project)
    except HTTPException as geweigerd:
        return geweigerd
    return None


def eis_handeling(request: Request, project_name: str, handeling: Action, *, deployment: str | None = None) -> None:
    """Laat een TWEEDE handeling op dit verzoek toe, naast die van de route.

    Voor een body die meer vraagt dan het pad laat zien: een deployment schrijven die
    ook de OTAP-markering zet vraagt ``environment.set`` naast ``deployment.write``. De
    principal van dit verzoek staat al op het verzoek, dus het bewijsmiddel wordt niet
    opnieuw herkend en de twee toetsen kunnen niet op verschillende aanroepers uitkomen.

    *deployment* zegt op WELKE deployment deze tweede handeling werkt, als dat een andere
    is dan die van de route: een upsert met ``cloneFrom`` leest de database van de BRON,
    en zonder dat doelwit zou die tweede toets tegen de hoogste markering van het project
    wegen in plaats van tegen de bron (RC-392 securityreview). Zonder naam blijft het
    gedrag wat het was.

    Raises:
        HTTPException: 403 als het bewijsmiddel deze handeling hier niet opent.
        RuntimeError: Als de route geen sleutelcontrole droeg; dan is er geen principal
            en zou stilzwijgend doorlaten de verkeerde uitkomst zijn.
    """
    logger = logging.getLogger(__name__)
    principaal = getattr(request.state, "authz_principal", None)
    project = get_project_store().get(project_name)
    if principaal is None or project is None:
        raise RuntimeError(f"'{handeling.value}' getoetst op een verzoek zonder herkend bewijsmiddel")
    doel = (
        build_resource(
            project,
            project_name=project_name,
            path=resource_path(project_name, deployment),
            deployment=deployment,
        )
        if deployment
        else None
    )
    try:
        beslis(principaal, project=project, handeling=handeling, resource=doel, request=request)
    except AccessDenied as exc:
        logger.warning("Geweigerd voor %s op %s (%s): %s", principaal.actor, project_name, handeling.value, exc)
        raise HTTPException(status_code=403, detail={"cause": exc.cause, "message": str(exc)}) from exc


def deployment_in_body(deployment_data: Any = None, **_: Any) -> str | None:
    """De deployment die een upsert-body aanwijst, voor :func:`deployment_from`.

    De naam staat daar in het veld ``deploymentName``; zonder deze verklaring zou de
    upsert-route tegen de hoogste markering van het project wegen, en kan een pijplijn met
    ``developer-ci`` niets meer zodra het project ergens productie heeft.
    """
    naam = getattr(deployment_data, "deploymentName", None)
    return str(naam) if naam else None


def eis_doelwitten_uit_de_upsertbody(request: Request, project_name: str, deployment_data: Any) -> None:
    """Toets de doelwitten in een upsert-body die de routepoort niet ziet.

    De poort weegt ``deploymentName``; in dezelfde body wijzen drie velden iets anders
    aan (RC-392 securityreview). ``cloneFrom`` kopieert de database van de BRON hierheen,
    dus die bron wordt apart gewogen: klonen vanaf productie is ops, ongeacht het doel.
    ``forceClone`` ZONDER ``cloneFrom`` laat de kloon lopen die al op de deployment staat,
    want de overslag op "mode once en afgerond" wijkt voor force_clone
    (``opi/manager/database_manager.py``); de bron staat dan in het projectbestand en
    weegt daar. Het domeindrietal verzet de hostnaam van een BESTAANDE deployment, en die
    kan door de goedkeuring per project dezelfde zijn als die van productie; een nieuwe
    deployment krijgt zijn domeinvelden mee uit ``deployment.write``.

    Raises:
        HTTPException: 403 als het bewijsmiddel een van deze handelingen niet opent.
    """
    doel = _deployment_van(project_name, deployment_data.deploymentName)
    opgeslagen_kloon = _opgeslagen_kloon(doel)
    if deployment_data.cloneFrom:
        eis_handeling(request, project_name, Action.DEPLOYMENT_CLONE_FROM, deployment=deployment_data.cloneFrom)
    elif deployment_data.forceClone and opgeslagen_kloon is not None:
        eis_handeling(
            request, project_name, Action.DEPLOYMENT_CLONE_FROM, deployment=_bron_deployment(opgeslagen_kloon)
        )
    verzet_domein = (
        deployment_data.domain_format is not None
        or deployment_data.subdomain is not None
        or deployment_data.base_domain is not None
    )
    if verzet_domein and doel is not None:
        eis_handeling(request, project_name, Action.DEPLOYMENT_DOMAIN_SET)


def _deployment_van(project_name: str, deployment_name: str) -> dict[str, Any] | None:
    """De deployment met deze naam in dit project, of None."""
    project = get_project_store().get(project_name)
    if project is None:
        return None
    return find_deployment(project.data or {}, deployment_name)


def _opgeslagen_kloon(deployment: dict[str, Any] | None) -> dict[str, Any] | None:
    """Het ``clone-from``-blok dat al op deze deployment staat."""
    kloon = (deployment or {}).get("clone-from")
    return kloon if isinstance(kloon, dict) else None


def _bron_deployment(kloon: dict[str, Any]) -> str | None:
    """De deployment waarvan dit blok kloont, of None als de bron er geen is.

    Een backup of een externe bron is geen deployment van dit project; zonder doelwit
    weegt de toets tegen de hoogste markering van het project, en dus strenger.
    """
    if kloon.get("type") != CloneFromType.DEPLOYMENT.value:
        return None
    verwijzing = kloon.get("reference")
    return str(verwijzing) if verwijzing else None


def parse_ports(ports_str: str) -> list[int]:
    """
    Parse comma-separated ports string into list of integers.

    Args:
        ports_str: Comma-separated port numbers

    Returns:
        List of port numbers as integers
    """
    if not ports_str:
        return []
    try:
        return [int(port.strip()) for port in ports_str.split(",") if port.strip()]
    except ValueError:
        return []


def normalize_project_name(text: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", text.lower())


def _require_shared_secret(
    request: Request,
    presented: str | None,
    configured: str | None,
    setting_name: str,
    route_name: str,
    missing_detail: str,
) -> None:
    """Laat de aanroep alleen door als ``presented`` exact het geconfigureerde secret is.

    Een secret dat niet geconfigureerd is houdt de route dicht, nooit open. Het antwoord
    is dan 404: dat deze instantie de voorziening niet heeft is geen serverfout.
    """
    logger = logging.getLogger(__name__)

    if not presented:
        logger.warning(f"Authentication failed for route {route_name} ({setting_name}) - no credential provided")
        raise HTTPException(status_code=401, detail=missing_detail)

    refuse_when_key_failures_exhausted(presented)

    if not configured:
        logger.warning(f"{setting_name} not configured - route {route_name} is disabled")
        raise HTTPException(status_code=404, detail="Not Found")

    # Op bytes vergelijken: compare_digest op twee str weigert een niet-ASCII teken met een
    # TypeError, en de header komt van buiten met willekeurige bytes.
    if not secrets.compare_digest(presented.encode(), configured.encode()):
        logger.warning(f"Authentication failed for route {route_name} ({setting_name}) - invalid credential")
        refuse_after_auth_failure(request, presented)
        raise HTTPException(status_code=401, detail="Invalid API key")

    logger.debug(f"{setting_name} validation successful for route {route_name}")


def validate_metrics_scrape_token(func: Callable[..., Any]) -> Callable[..., Any]:
    """Eist ``Authorization: Bearer <PROMETHEUS_METRICS_AUTH_TOKEN>`` op een metrics-route.

    Dat is de header die onze Prometheus al meestuurt, met het token dat de dienst
    metrics-scraper ook aan projectapps geeft.
    """

    @wraps(func)
    async def wrapper(*args: Any, request: Request, **kwargs: Any) -> Any:
        try:
            token: str | None = extract_bearer_token(request)
        except UserTokenError:
            token = None

        _require_shared_secret(
            request,
            token,
            settings.PROMETHEUS_METRICS_AUTH_TOKEN,
            "PROMETHEUS_METRICS_AUTH_TOKEN",
            func.__name__,
            "Authentication required - provide an Authorization: Bearer header",
        )
        return await func(*args, request=request, **kwargs)

    setattr(wrapper, REQUIRES_METRICS_SCRAPE_TOKEN, True)
    return wrapper


def controleer_handelingen(routes: Iterable[Any]) -> None:
    """Weiger op te starten als een BEWAAKTE route geen action noemt, of een action geen regel.

    Bewaakt is: de route draagt een van de herkenningen (een sessie, een projectsleutel,
    een persoonlijk token of een platformbeheerderstoken). Een nieuwe route die zo'n
    decorator krijgt moet dus een action kiezen, en die action moet een regel hebben in
    ``opi/core/authz/rules.py``. Een route zonder action zou stil openstaan voor alles wat
    de aanroeper al mocht, en een action zonder regel zou bij elke aanroep falen in plaats
    van bij de start.

    De routers die hun sleutelcontrole met de hand doen (``task_router``, de slaapstand)
    dragen geen markering en zijn hier dus niet te vinden; ``tests/test_actions.py`` legt
    die vijf apart vast.

    Raises:
        RuntimeError: Als een bewaakte route geen action draagt of de action geen regel heeft.
    """
    bewaakt = ("_requires_sso", REQUIRES_PROJECT_NAME, REQUIRES_USER_TOKEN, REQUIRES_PLATFORM_ADMIN_TOKEN)
    zonder_action = []
    zonder_regel = []
    for route in routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not any(getattr(endpoint, markering, False) for markering in bewaakt):
            continue
        methods = ",".join(sorted(getattr(route, "methods", None) or []))
        gevraagd = action_of(endpoint)
        if gevraagd is None:
            zonder_action.append(f"{methods} {getattr(route, 'path', '?')}")
            continue
        try:
            rule_for(gevraagd)
        except RuleError:
            zonder_regel.append(f"{methods} {getattr(route, 'path', '?')} ({gevraagd.value})")

    if zonder_action:
        raise RuntimeError(
            "Deze routes zijn bewaakt maar noemen geen action: "
            f"{', '.join(sorted(zonder_action))}. Zet @action(Action.X) boven de authenticatiedecorator."
        )
    if zonder_regel:
        raise RuntimeError(
            f"Deze routes noemen een action zonder regel in opi/core/authz/rules.py: {', '.join(sorted(zonder_regel))}."
        )
