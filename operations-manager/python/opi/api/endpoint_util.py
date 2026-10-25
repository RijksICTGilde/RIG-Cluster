import logging
import re
import secrets
from collections.abc import Callable, Iterable  # noqa: TC003 -- used in decorator signatures at runtime
from functools import wraps
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from opi.api.principaal import (
    NietHerkend,
    PrincipaalSoort,
    ToegangGeweigerd,
    beslis,
    herken_principaal,
    hoort_bij_project,
    tel_gebruik,
)
from opi.api.rate_limiter import refuse_after_auth_failure, refuse_when_key_failures_exhausted
from opi.api.user_token_auth import REQUIRES_USER_TOKEN, UserTokenError, extract_bearer_token
from opi.core.actor import set_actor
from opi.core.config import settings
from opi.core.handelingen import handeling_van
from opi.services.project_store import get_project_store
from starlette.requests import Request  # noqa: TC002 — FastAPI needs Request at runtime

if TYPE_CHECKING:
    from opi.services.project_service import ProjectSummary

REQUIRES_PROJECT_NAME = "zad_requires_project_name"
"""Markering op een route die met een projectsleutel wordt afgeschermd."""


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
            raise HTTPException(status_code=401, detail="Authentication required - provide X-API-Key header")

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
            raise HTTPException(status_code=401, detail="Invalid API key")

        await handhaaf(request, x_api_key, wrapper, project)

        # Add project_id to kwargs so the route function can access it if needed
        kwargs["project_name"] = project.name
        return await func(*args, request=request, **kwargs)

    setattr(wrapper, REQUIRES_PROJECT_NAME, True)
    return wrapper


async def handhaaf(request: Request, presented: str, endpoint: Callable[..., Any], project: ProjectSummary) -> None:
    """Herken deze ``X-API-Key`` en laat de handeling van deze route toe.

    Een route legt hier zijn eigen endpoint-object in, want daar staat de markering die
    :func:`opi.core.handelingen.handeling` erop zette. Gedeeld met de routers die hun
    sleutelcontrole met de hand doen (taken, slaapstand), zodat een token daar hetzelfde
    werkt als op de rest van de API.

    Het project komt als object binnen, want de aanroeper heeft het al moeten lezen om te
    weten dat het bestaat; nog een lezing hier zou hetzelfde verzoek op twee
    momentopnamen beslissen.

    Hoort het token bij een MENS, dan landt die hier op ``request.state.user``. Dat is de
    plek waar :func:`opi.api.otp_step_up.verify_api_step_up` de eigenaar van het token
    leest, zodat een step-up op een ``zad_``-token precies dezelfde weg loopt als op een
    SSO-bearer. Een service account en de oude projectsleutel laten hem leeg, en ketsen
    daar dus af op ``tokensoort-niet-toegestaan``.

    Raises:
        HTTPException: 401 als het bewijsmiddel niet herkend wordt, 403 als het deze
            handeling hier niet opent.
    """
    logger = logging.getLogger(__name__)
    project_name = project.name
    gevraagde = handeling_van(endpoint)
    if gevraagde is None:
        # De opstartcontrole hoort dit onmogelijk te maken; komt het hier toch langs, dan
        # is stilzwijgend doorlaten de verkeerde uitkomst.
        raise RuntimeError(f"Route {getattr(endpoint, '__name__', endpoint)} draagt geen handeling")

    try:
        principaal = await herken_principaal(presented, project)
    except NietHerkend as exc:
        logger.warning("Authenticatie mislukt voor project %s: %s", project_name, exc)
        refuse_after_auth_failure(request, presented)
        raise HTTPException(status_code=401, detail="Invalid API key") from exc

    try:
        beslis(principaal, project=project, handeling=gevraagde)
    except ToegangGeweigerd as exc:
        logger.warning("Geweigerd voor %s op %s (%s): %s", principaal.actor, project_name, gevraagde.value, exc)
        # De reden als eigen veld en niet alleen in de tekst: een aanroeper moet "deze
        # soort aanroeper mag dit nooit" kunnen onderscheiden van "je rol is te smal",
        # want dat zijn twee verschillende dingen om op te lossen.
        raise HTTPException(status_code=403, detail={"cause": exc.cause, "message": str(exc)}) from exc

    tel_gebruik(project_name, principaal.soort)
    set_actor(principaal.actor)
    if principaal.soort is PrincipaalSoort.PERSOON:
        request.state.user = {"email": principaal.naam}
    logger.debug("Toegestaan: %s mag %s op %s", principaal.actor, gevraagde.value, project_name)


async def legitimeert(presented: str | None, project_name: str) -> bool:
    """Of dit bewijsmiddel bij dit project hoort, zonder al een handeling te beslissen.

    Voor de voorcontrole van de image-push, die vóór de multipart-parser moet weten of de
    aanroeper bekend is; :func:`handhaaf` doet daarachter de echte beslissing. Een token
    komt hier dus even ver als de oude projectsleutel.
    """
    project = get_project_store().get(project_name)
    if not presented or project is None:
        return False
    try:
        principaal = await herken_principaal(presented, project)
    except NietHerkend:
        return False
    return hoort_bij_project(principaal, project)


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
    """Weiger op te starten als een token-bereikbare route geen handeling noemt.

    Token-bereikbaar is: de route draagt :data:`REQUIRES_PROJECT_NAME` of
    :data:`REQUIRES_USER_TOKEN`. Een nieuwe route die een van die decorators krijgt moet
    dus een handeling kiezen, en dat is precies waarom een scope handelingen noemt en geen
    paden: de lijst veroudert niet, de controle wel.

    De routers die hun sleutelcontrole met de hand doen (``task_router``, de slaapstand)
    dragen geen markering en zijn hier dus niet te vinden; ``tests/test_handelingen.py``
    legt die vijf apart vast.

    Raises:
        RuntimeError: Als een token-bereikbare route geen handeling draagt.
    """
    zonder = []
    for route in routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        if not getattr(endpoint, REQUIRES_PROJECT_NAME, False) and not getattr(endpoint, REQUIRES_USER_TOKEN, False):
            continue
        if handeling_van(endpoint) is None:
            methods = ",".join(sorted(getattr(route, "methods", None) or []))
            zonder.append(f"{methods} {getattr(route, 'path', '?')}")

    if zonder:
        raise RuntimeError(
            "Deze routes zijn met een token bereikbaar maar noemen geen handeling: "
            f"{', '.join(sorted(zonder))}. Zet @handeling(Handeling.X) boven de authenticatiedecorator."
        )
