"""
Services web route for displaying service information.
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from opi.core.auth_decorators import get_current_user, requires_sso
from opi.core.templates import get_templates
from opi.services.services import ServiceAdapter
from opi.web.menu import get_menu_items

logger = logging.getLogger(__name__)

services_router = APIRouter()


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
        templates = get_templates()
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
                }
            )

        return templates.TemplateResponse(
            "services-overview.html.j2",
            {
                "request": request,
                "title": "Services Overview",
                "menu_items": get_menu_items(user),
                "services": services_info,
                "user": user,
            },
        )

    except Exception as e:
        import traceback

        error_details = traceback.format_exc()
        logger.error(f"Error serving services overview: {e!s}\n{error_details}")

        # Try to extract line number from Jinja2 error
        error_msg = str(e)
        if hasattr(e, "lineno"):
            error_msg = f"Line {e.lineno}: {error_msg}"

        # Include template source snippet if available
        if hasattr(e, "source") and hasattr(e, "lineno"):
            lines = e.source.splitlines()
            line_num = e.lineno - 1
            if 0 <= line_num < len(lines):
                error_msg += f"\nSource: {lines[line_num].strip()}"

        from fastapi import HTTPException

        raise HTTPException(status_code=500, detail=f"Template error: {error_msg}")
