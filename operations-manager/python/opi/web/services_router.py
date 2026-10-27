"""
Services web route for displaying service information.
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from opi.core.auth_decorators import get_current_user, requires_sso
from opi.core.config import settings
from opi.core.errors import log_render_failure
from opi.services.services import ServiceAdapter
from opi.web.menu import get_menu_items

logger = logging.getLogger(__name__)

services_router = APIRouter()


def replacement_marker(service_type) -> str | None:
    """The successor's display name when this service is replaced on this cluster, else None.

    Choosing a replaced service is already refused in the picker and at save time
    (RC-382); this overview is read-only, so what it owes the reader is the marker that
    says why new projects should not pick it. The service's own help page tells the
    full story.
    """
    # The import stays inside: the registry loads the service packages and some of those
    # import back into the web layer.
    from opi.services.registry import get_service

    service = get_service(service_type)
    if not service.replaced_on_cluster(settings.CLUSTER_MANAGER):
        return None
    return service.replacement_name()


@services_router.get("/services", response_class=HTMLResponse)
@requires_sso
async def services_overview(request: Request):
    """
    Serve the services overview page showing all available services and their variables.

    This page displays cards for each service with their descriptions, icons, and the
    variables they provide (either through secrets or direct env vars).

    Returns:
        HTML response with service cards showing variable information
    """
    try:
        user = get_current_user(request)

        # Get all services and their variable information
        all_services = ServiceAdapter.get_all_services()
        services_info = []

        for service in all_services:
            service_def = ServiceAdapter.get_service_definition(service)
            variables = ServiceAdapter.get_variables(service)

            services_info.append(
                {
                    "service": service,
                    "definition": service_def,
                    "variables": variables,
                    "secret_class": ServiceAdapter.get_secret_class(service),
                    "uses_secrets": ServiceAdapter.uses_secrets(service),
                    "uses_direct_variables": ServiceAdapter.uses_direct_variables(service),
                    "replaced_by": replacement_marker(service),
                }
            )

        # Dezelfde gegevens, twee weergaven. Met ?ui=lotc rendert deze route de
        # hertekende pagina; zonder blijft alles zoals het was. Zie opi/web/lotc_switch.py
        # voor waarom dat een schakelaar is en geen omzetting ineens.
        from opi.web.lotc_switch import build_lotc_services, render

        return render(
            request,
            template="bg/services.html.j2",
            context={
                "request": request,
                "title": "Services Overview",
                "menu_items": get_menu_items(user),
                "services": services_info,
                "user": user,
                **build_lotc_services(services_info, user),
            },
        )

    except Exception as e:
        log_render_failure(logger, "het dienstenoverzicht", e)
        raise HTTPException(
            status_code=500,
            detail="Het dienstenoverzicht kon niet worden opgebouwd. Probeer het over een minuut opnieuw.",
        ) from e
