"""
Self-Service Portal route for the web interface.
"""

import logging

from fastapi import HTTPException, Request

from opi.core.auth_decorators import get_current_user, requires_sso
from opi.core.templates import get_templates
from opi.web.menu import get_menu_items

logger = logging.getLogger(__name__)


@requires_sso
async def self_service_portal(request: Request):
    """
    Serve the Self-Service Portal form for creating new projects.

    This is a comprehensive form that allows users to:
    - Define project details (name, description, cluster)
    - Add team members with different roles
    - Select required services (web, SSO, storage, databases)

    The form uses ROOS components with a modern, user-friendly design
    and includes interactive features like user row cloning and
    service card selection.

    Returns:
        HTML response with the self-service portal form
    """
    try:
        templates = get_templates()
        user = get_current_user(request)
        return templates.TemplateResponse(
            "self-service-portal.html.j2",
            {"request": request, "title": "Nieuw Project - Self Service Portal", "menu_items": get_menu_items(user)},
        )
    except Exception as e:
        import traceback

        error_details = traceback.format_exc()
        logger.error(f"Error serving Self-Service Portal form: {e!s}\n{error_details}")

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

        raise HTTPException(status_code=500, detail=f"Template error: {error_msg}")
