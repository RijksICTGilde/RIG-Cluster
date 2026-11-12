"""The lazy-loaded status fragment of the image-registries block.

The route lives here and the answer in the service
(``opi/services/catalog/image_registries/web.py``): an entry point translates and renders,
the service knows what the cluster says.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from opi.core.authz.actions import Action, action
from opi.core.request_user import get_current_user
from opi.http.auth.auth_decorators import requires_sso
from opi.http.render.lotc_switch import render
from opi.services.catalog.image_registries.web import STATUS_TEMPLATE, registry_status_context
from opi.services.project_authorization import is_user_authorized_for_project

image_registries_router = APIRouter()


@image_registries_router.get("/projects/details/{project_name}/image-registries/status", response_class=HTMLResponse)
@action(Action.SERVICE_READ)
@requires_sso
async def registry_status_fragment(request: Request, project_name: str) -> HTMLResponse:
    """The state of this project's proxy organizations, as HTML for the block."""
    user = get_current_user(request) or {}
    if not is_user_authorized_for_project(project_name, user.get("email", "").lower()):
        raise HTTPException(status_code=403, detail="Not authorized")

    return render(request, template=STATUS_TEMPLATE, context=await registry_status_context(project_name))
