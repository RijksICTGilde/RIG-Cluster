"""Web routes for project form editing using editable-driven forms."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from opi.core.auth_decorators import get_current_user, requires_sso
from opi.core.templates import get_templates
from opi.forms import FormRenderer, ROOSWidgetAdapter, get_default_nl_translator
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.visualizers.project_registry import (
    get_all_project_editables,
    get_project_form_layout,
)
from opi.handlers.project_file_handler import save_project_file
from opi.web.menu import get_menu_items

logger = logging.getLogger(__name__)

project_form_router = APIRouter(prefix="/projects", tags=["project-forms"])


def create_form_renderer() -> FormRenderer:
    """Create a configured FormRenderer for project forms."""
    return FormRenderer(
        widget_adapter=ROOSWidgetAdapter(),
        translator=get_default_nl_translator(),
    )


@project_form_router.get("/edit/{project_name}", response_class=HTMLResponse)
@requires_sso
async def edit_project_form(request: Request, project_name: str) -> HTMLResponse:
    """Display the project edit form."""
    from opi.services.project_service import get_project_service

    user = get_current_user(request)
    templates = get_templates()
    project_service = get_project_service()
    project = project_service.get_project(project_name)

    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' niet gevonden")

    user_email = user.get("email", "").lower()
    if not project_service.is_user_authorized_for_project(project_name, user_email):
        raise HTTPException(status_code=403, detail="Geen toegang tot dit project")

    project_data = project.data
    if not project_data:
        raise HTTPException(status_code=500, detail="Project data niet beschikbaar")

    renderer = create_form_renderer()
    editables = get_all_project_editables()
    layout = get_project_form_layout()

    form_html = renderer.render_from_editables(
        editables=editables,
        yaml_data=project_data,
        layout=layout,
        edit_mode=True,
        action=f"/projects/edit/{project_name}",
    )

    return templates.TemplateResponse(
        "project-edit-form.html.j2",
        {
            "request": request,
            "title": f"Bewerk Project - {project_data.get('display-name', project_name)}",
            "menu_items": get_menu_items(user),
            "project_name": project_name,
            "project_data": project_data,
            "form_html": form_html,
            "user": user,
        },
    )


@project_form_router.post("/edit/{project_name}", response_class=HTMLResponse, response_model=None)
@requires_sso
async def save_project_form(request: Request, project_name: str) -> HTMLResponse | RedirectResponse:
    """Handle project form submission: validate and save."""
    from opi.services.project_service import get_project_service

    user = get_current_user(request)
    project_service = get_project_service()
    project = project_service.get_project(project_name)

    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' niet gevonden")

    user_email = user.get("email", "").lower()
    user_role = project_service.get_user_role_for_project(project_name, user_email)
    if user_role not in ["admin", "owner"]:
        raise HTTPException(status_code=403, detail="Alleen admins kunnen projecten bewerken")

    form_data = await request.form()
    editables = get_all_project_editables()
    processor = EditableFormProcessor()

    parsed = processor.parse_form_data(form_data, editables)

    original_data = project.data or {}
    errors = await processor.validate_editables(parsed, editables, original_data)

    if errors:
        renderer = create_form_renderer()
        layout = get_project_form_layout()
        form_html = renderer.render_from_editables(
            editables=editables,
            yaml_data=original_data,
            layout=layout,
            errors=errors,
            edit_mode=True,
            action=f"/projects/edit/{project_name}",
        )
        templates = get_templates()
        return templates.TemplateResponse(
            "project-edit-form.html.j2",
            {
                "request": request,
                "title": f"Bewerk Project - {original_data.get('display-name', project_name)}",
                "menu_items": get_menu_items(user),
                "project_name": project_name,
                "project_data": original_data,
                "form_html": form_html,
                "errors": errors,
                "user": user,
            },
        )

    updated_data = processor.apply_to_yaml(parsed, editables, original_data, edit_mode=True)

    save_project_file(project.filename, updated_data)

    project_service.load_project_from_data(updated_data, project.filename)

    logger.info("Project %s updated by %s", project_name, user_email)

    return RedirectResponse(
        url=f"/projects/details/{project_name}",
        status_code=302,
    )
