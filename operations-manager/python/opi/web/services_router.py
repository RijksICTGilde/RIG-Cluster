"""The services overview page (/services)."""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from opi.core.authz.actions import Action, action
from opi.core.config import settings
from opi.core.errors import log_render_failure
from opi.http.auth.auth_decorators import get_current_user, requires_sso
from opi.http.render.menu import get_menu_items
from opi.services.services import ServiceAdapter

logger = logging.getLogger(__name__)

services_router = APIRouter()


def replacement_marker(service_type) -> str | None:
    """The successor's display name when this service is replaced on this cluster, else None."""
    # The import stays inside: the registry loads the service packages and some of those
    # import back into the web layer.
    from opi.services.registry import get_service

    service = get_service(service_type)
    if not service.replaced_on_cluster(settings.CLUSTER_MANAGER):
        return None
    return service.replacement_name()


@services_router.get("/services", response_class=HTMLResponse)
@action(Action.PORTAL_READ)
@requires_sso
async def services_overview(request: Request):
    """A card per service, with its description, icon and the variables it provides."""
    try:
        user = get_current_user(request)

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

        from opi.http.render.lotc_switch import build_lotc_services, render

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
