"""
Web routes for project file form editing.

This module provides form-based endpoints for viewing and editing
project files using the dynamic form system.
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from ruamel.yaml import YAML

from opi.core.auth_decorators import get_current_user, requires_sso
from opi.core.templates import get_templates
from opi.forms import (
    FormRenderer,
    ROOSWidgetAdapter,
    get_all_providers,
    get_default_nl_translator,
)
from opi.forms.models.project_file import (
    ProjectFileModel,
    get_project_file_form_layout,
)
from opi.web.menu import get_menu_items

logger = logging.getLogger(__name__)

project_form_router = APIRouter(prefix="/projects", tags=["project-forms"])


def load_project_file(file_path: str) -> dict:
    """Load and parse a project YAML file."""
    yaml = YAML()
    yaml.preserve_quotes = True
    with open(file_path) as f:
        return yaml.load(f)


def save_project_file(file_path: str, data: dict) -> None:
    """Save project data to a YAML file."""
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.default_flow_style = False
    with open(file_path, "w") as f:
        yaml.dump(data, f)


def create_form_renderer() -> FormRenderer:
    """Create a configured FormRenderer for project forms."""
    return FormRenderer(
        widget_adapter=ROOSWidgetAdapter(),
        translator=get_default_nl_translator(),
        options_providers=get_all_providers(),
    )


@project_form_router.get("/edit/{project_name}", response_class=HTMLResponse)
@requires_sso
async def edit_project_form(request: Request, project_name: str) -> HTMLResponse:
    """
    Display the project edit form.

    Loads the project file and renders it as an editable form.
    """
    from opi.services.project_service import get_project_service

    user = get_current_user(request)
    templates = get_templates()

    # Get project service to find the file
    project_service = get_project_service()
    project = project_service.get_project(project_name)

    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' niet gevonden")

    # Check authorization
    user_email = user.get("email", "").lower()
    if not project_service.is_user_authorized_for_project(project_name, user_email):
        raise HTTPException(status_code=403, detail="Geen toegang tot dit project")

    # Load the project data (use cached data if available)
    project_data = project.data
    if not project_data:
        raise HTTPException(status_code=500, detail="Project data niet beschikbaar")

    # Create the renderer
    renderer = create_form_renderer()

    # Populate fields with project data
    populated_fields = renderer.render_fields(
        schema=ProjectFileModel,
        data=project_data,
    )

    # Get layout
    layout = get_project_file_form_layout()

    # Render form HTML
    form_html = renderer.render(
        schema=ProjectFileModel,
        layout=layout,
        data=project_data,
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
            "fields": populated_fields,
            "user": user,
        },
    )


@project_form_router.post("/edit/{project_name}", response_class=HTMLResponse)
@requires_sso
async def save_project_form(request: Request, project_name: str) -> HTMLResponse:
    """
    Handle project form submission.

    Validates the form data and saves the project file.
    """
    from fastapi.responses import RedirectResponse

    from opi.services.project_service import get_project_service

    user = get_current_user(request)

    # Get project service
    project_service = get_project_service()
    project = project_service.get_project(project_name)

    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' niet gevonden")

    # Check authorization (must be admin or owner)
    user_email = user.get("email", "").lower()
    user_role = project_service.get_user_role_for_project(project_name, user_email)
    if user_role not in ["admin", "owner"]:
        raise HTTPException(status_code=403, detail="Alleen admins kunnen projecten bewerken")

    # Parse form data
    form_data = await request.form()

    # Convert form data to dict
    data = _parse_form_data_to_dict(form_data)

    # Create renderer for validation
    renderer = create_form_renderer()

    # Validate using pydantic model
    try:
        validated = ProjectFileModel.model_validate(data)
    except ValidationError as e:
        # Return form with errors
        templates = get_templates()
        form_html = renderer.render(
            schema=ProjectFileModel,
            layout=get_project_file_form_layout(),
            data=data,
            errors=renderer._format_validation_errors(e),
        )
        return templates.TemplateResponse(
            "project-edit-form.html.j2",
            {
                "request": request,
                "title": f"Bewerk Project - {data.get('display-name', project_name)}",
                "menu_items": get_menu_items(user),
                "project_name": project_name,
                "project_data": data,
                "form_html": form_html,
                "errors": renderer._format_validation_errors(e),
                "user": user,
            },
        )

    # TODO: Save the project file
    # For now, just log and redirect
    logger.info(f"Would save project {project_name} with data: {validated.model_dump()}")

    # Redirect to project details
    return RedirectResponse(
        url=f"/projects/details/{project_name}",
        status_code=302,
    )


def _parse_form_data_to_dict(form_data: object) -> dict:
    """Parse form data into a nested dict structure."""
    data: dict = {}
    for key, value in form_data.items():
        # Handle nested keys like "components[0].name"
        if "[" in key:
            _set_nested_value(data, key, value)
        else:
            data[key] = value

    # Handle multi-value fields (checkboxes)
    for key in form_data:
        values = form_data.getlist(key)
        if len(values) > 1:
            data[key] = values

    return data


def _set_nested_value(data: dict, key: str, value: object) -> None:
    """Set a nested value in a dict using bracket notation key."""
    parts = key.replace("]", "").split("[")
    current = data
    for i, part in enumerate(parts[:-1]):
        if part.isdigit():
            idx = int(part)
            while len(current) <= idx:
                current.append({})
            current = current[idx]
        else:
            if part not in current:
                # Check if next part is a number
                next_part = parts[i + 1] if i + 1 < len(parts) else None
                if next_part and next_part.isdigit():
                    current[part] = []
                else:
                    current[part] = {}
            current = current[part]
    # Set the value
    final_key = parts[-1]
    if isinstance(current, list):
        idx = int(final_key)
        while len(current) <= idx:
            current.append({})
        current[idx] = value
    else:
        current[final_key] = value


@project_form_router.get("/form-demo", response_class=HTMLResponse)
@requires_sso
async def project_form_demo(request: Request) -> HTMLResponse:
    """
    Demo page showing the dynamic form system in action.

    Loads a sample project file and renders it as a form.
    """
    user = get_current_user(request)
    templates = get_templates()

    # Sample project data
    sample_data = {
        "name": "demo-project",
        "display-name": "Demo Project",
        "description": "Een demonstratie van het dynamische formulier systeem",
        "clusters": ["local"],
        "services": ["publish-on-web", "keycloak"],
        "users": [
            {"email": "admin@rijksoverheid.nl", "role": "admin"},
            {"email": "developer@rijksoverheid.nl", "role": "developer"},
        ],
        "components": [
            {
                "name": "component-1",
                "type": "single",
                "ports": {"inbound": [8000], "outbound": [80, 443]},
                "resources": {"cpu": "1", "memory": "256Mi"},
                "uses-services": ["publish-on-web"],
            }
        ],
        "repositories": [],
        "deployments": [],
    }

    # Create the renderer
    renderer = create_form_renderer()

    # Extract and populate fields
    fields = renderer.render_fields(
        schema=ProjectFileModel,
        data=sample_data,
    )

    # Get layout
    layout = get_project_file_form_layout()

    # Render form HTML
    form_html = renderer.render(
        schema=ProjectFileModel,
        layout=layout,
        data=sample_data,
    )

    return templates.TemplateResponse(
        "project-form-demo.html.j2",
        {
            "request": request,
            "title": "Dynamic Form Demo",
            "menu_items": get_menu_items(user),
            "project_data": sample_data,
            "form_html": form_html,
            "fields": fields,
            "user": user,
        },
    )
