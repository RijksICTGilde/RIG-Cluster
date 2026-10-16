import logging
import re
import secrets
from collections.abc import Callable  # noqa: TC003 — used in decorator signatures at runtime
from functools import wraps
from typing import Any

from fastapi import HTTPException
from opi.api.user_token_auth import UserTokenError, extract_bearer_token
from opi.core.config import settings
from opi.services.project_store import get_project_store
from starlette.requests import Request  # noqa: TC002 — FastAPI needs Request at runtime

REQUIRES_PROJECT_NAME = "zad_requires_project_name"
"""Markering op een route die met een projectsleutel wordt afgeschermd.

De controle legitimeert de sleutel TEGEN een project en leest dat project uit de
routeparameters, dus een route zonder ``project_name`` kan hem per definitie niet doorstaan
en antwoordt altijd 401 -- wat de aanroeper ook meestuurt, want FastAPI geeft een route
alleen de parameters die in haar signatuur staan. Dat overkwam
``GET /api/subdomains/check/{subdomain}``: het endpoint was onbereikbaar en het document
noemde de ontbrekende parameter nergens.

De markering staat hier zodat ``tests/test_subdomein_check_endpoint.py`` die eis uit de
routes zelf kan lezen in plaats van uit een lijst met paden.
"""


def validate_api_token(func: Callable[..., Any]) -> Callable[..., Any]:
    """
    Decorator to validate API token for a route.

    This decorator requires project-specific API key via X-API-Key header.
    ALWAYS validates that the API key matches the project_name from the route.

    The route must therefore be able to receive ``project_name`` -- in its path, or as a
    REQUIRED query parameter; see :data:`REQUIRES_PROJECT_NAME`.

    Args:
        func: The route function to decorate

    Returns:
        The decorated function that requires a valid API token and project_name
    """

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

        project = get_project_store().get(project_name_from_url)

        # Op bytes vergelijken, zie de noot in _require_shared_secret.
        if not project or not secrets.compare_digest(project.api_key.encode(), x_api_key.encode()):
            logger.warning(f"Authentication failed for route {func.__name__} - invalid API key")
            raise HTTPException(status_code=401, detail="Invalid API key")

        logger.debug(f"Project API key validation successful for route {func.__name__} (project: {project.name})")
        # Add project_id to kwargs so the route function can access it if needed
        kwargs["project_name"] = project.name
        return await func(*args, request=request, **kwargs)

    setattr(wrapper, REQUIRES_PROJECT_NAME, True)
    return wrapper


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
    presented: str | None,
    configured: str | None,
    setting_name: str,
    route_name: str,
    missing_detail: str,
) -> None:
    """Laat de aanroep alleen door als ``presented`` exact het geconfigureerde secret is.

    Een secret dat niet geconfigureerd is houdt de route dicht (501), nooit open.
    """
    logger = logging.getLogger(__name__)

    if not presented:
        logger.warning(f"Authentication failed for route {route_name} ({setting_name}) - no credential provided")
        raise HTTPException(status_code=401, detail=missing_detail)

    if not configured:
        logger.warning(f"{setting_name} not configured - route {route_name} is disabled")
        raise HTTPException(
            status_code=501,
            detail=f"This endpoint requires {setting_name} to be configured",
        )

    # Op bytes vergelijken: compare_digest op twee str weigert een niet-ASCII teken met een
    # TypeError, en de header komt van buiten met willekeurige bytes.
    if not secrets.compare_digest(presented.encode(), configured.encode()):
        logger.warning(f"Authentication failed for route {route_name} ({setting_name}) - invalid credential")
        raise HTTPException(status_code=401, detail="Invalid API key")

    logger.debug(f"{setting_name} validation successful for route {route_name}")


def validate_admin_api_key(func: Callable[..., Any]) -> Callable[..., Any]:
    """
    Decorator to validate admin API key for maintenance operations.

    This decorator requires the ADMIN_API_KEY via X-API-Key header.
    Used for admin operations like cleanup, reconciliation, and resource management.

    Args:
        func: The route function to decorate

    Returns:
        The decorated function that requires a valid admin API key
    """

    @wraps(func)
    async def wrapper(*args: Any, request: Request, **kwargs: Any) -> Any:
        _require_shared_secret(
            request.headers.get("X-API-Key"),
            settings.ADMIN_API_KEY,
            "ADMIN_API_KEY",
            func.__name__,
            "Authentication required - provide X-API-Key header",
        )
        return await func(*args, request=request, **kwargs)

    return wrapper


def validate_master_api_key(func: Callable[..., Any]) -> Callable[..., Any]:
    """
    Decorator to validate master API key for admin operations.

    This decorator requires the MASTER_API_KEY via X-API-Key header.
    Used for operations that don't have a project context (e.g., namespace-based backups).

    Args:
        func: The route function to decorate

    Returns:
        The decorated function that requires a valid master API key
    """

    @wraps(func)
    async def wrapper(*args: Any, request: Request, **kwargs: Any) -> Any:
        _require_shared_secret(
            request.headers.get("X-API-Key"),
            settings.MASTER_API_KEY,
            "MASTER_API_KEY",
            func.__name__,
            "Authentication required - provide X-API-Key header",
        )
        return await func(*args, request=request, **kwargs)

    return wrapper


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
            token,
            settings.PROMETHEUS_METRICS_AUTH_TOKEN,
            "PROMETHEUS_METRICS_AUTH_TOKEN",
            func.__name__,
            "Authentication required - provide an Authorization: Bearer header",
        )
        return await func(*args, request=request, **kwargs)

    return wrapper
