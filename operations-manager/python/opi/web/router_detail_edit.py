"""Detail page inline editing via the editables system.

Provides GET/POST endpoints for editing project sections from the
details page modal.  Reuses the same ``FormRenderer``,
``EditableFormProcessor``, and section definitions used by the wizard.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.background import BackgroundTask

from opi.core.auth_decorators import get_current_user, requires_sso
from opi.forms import FormRenderer, ROOSWidgetAdapter, get_default_nl_translator
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.visualizers.wizard_sections import (
    EDIT_SECTIONS,
    SERVICE_CONFIG_SECTIONS,
    _extract_services,
)

logger = logging.getLogger(__name__)

detail_edit_router = APIRouter(prefix="/projects", tags=["detail-edit"])


def _create_renderer() -> FormRenderer:
    return FormRenderer(
        widget_adapter=ROOSWidgetAdapter(),
        translator=get_default_nl_translator(),
    )


def _get_edit_section(section_id: str):
    """Look up a section from the edit-sections registry."""
    section = EDIT_SECTIONS.get(section_id)
    if not section:
        raise HTTPException(status_code=404, detail=f"Sectie '{section_id}' niet gevonden")
    return section


def _render_section_html(
    section,
    yaml_data: dict[str, Any],
    errors: dict[str, list[str]] | None = None,
) -> str:
    """Render form fields for a section (same pattern as wizard _render_step_html)."""
    from opi.core.templates import get_templates

    renderer = _create_renderer()
    if not section.layout:
        return ""
    html = renderer.render_fields_from_editables(
        editables=section.editables,
        yaml_data=yaml_data,
        layout=section.layout,
        errors=errors,
        edit_mode=True,
    )
    # Process Jinja component tags in runtime-generated HTML
    templates = get_templates()
    process_components_filter = templates.env.filters.get("process_components")
    if process_components_filter is not None:
        html = str(process_components_filter(html))
    return html


@detail_edit_router.get("/{project_name}/edit/{section_id}", response_class=HTMLResponse)
@requires_sso
async def get_edit_section(request: Request, project_name: str, section_id: str) -> HTMLResponse:
    """Return rendered form HTML for a single edit section (loaded into modal)."""
    from opi.services.project_service import get_project_service

    user = get_current_user(request)
    project_service = get_project_service()
    project = project_service.get_project(project_name)

    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' niet gevonden")

    user_email = user.get("email", "").lower()
    if not project_service.is_user_authorized_for_project(project_name, user_email):
        raise HTTPException(status_code=403, detail="Geen toegang tot dit project")

    user_role = project_service.get_user_role_for_project(project_name, user_email)
    if user_role not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="Onvoldoende rechten om dit project te bewerken")

    section = _get_edit_section(section_id)
    project_data = project.data or {}

    # Check conditional visibility (e.g. keycloak-config requires keycloak service)
    if callable(section.visible) and not section.visible(project_data):
        raise HTTPException(status_code=404, detail=f"Sectie '{section_id}' is niet beschikbaar voor dit project")
    if section.visible is False:
        raise HTTPException(status_code=404, detail=f"Sectie '{section_id}' is niet beschikbaar")

    fields_html = _render_section_html(section, project_data)

    return HTMLResponse(content=fields_html)


@detail_edit_router.post("/{project_name}/edit/{section_id}", response_class=HTMLResponse)
@requires_sso
async def submit_edit_section(request: Request, project_name: str, section_id: str) -> HTMLResponse:
    """Process an edit submission for a single section."""
    from io import StringIO

    from ruamel.yaml import YAML

    from opi.core.task_manager import create_task
    from opi.handlers.project_file_handler import save_project_file
    from opi.services.project_service import get_project_service

    user = get_current_user(request)
    project_service = get_project_service()
    project = project_service.get_project(project_name)

    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' niet gevonden")

    user_email = user.get("email", "").lower()
    if not project_service.is_user_authorized_for_project(project_name, user_email):
        raise HTTPException(status_code=403, detail="Geen toegang tot dit project")

    user_role = project_service.get_user_role_for_project(project_name, user_email)
    if user_role not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="Onvoldoende rechten om dit project te bewerken")

    section = _get_edit_section(section_id)
    project_data = project.data or {}

    # Check conditional visibility (e.g. keycloak-config requires keycloak service)
    if callable(section.visible) and not section.visible(project_data):
        raise HTTPException(status_code=404, detail=f"Sectie '{section_id}' is niet beschikbaar voor dit project")
    if section.visible is False:
        raise HTTPException(status_code=404, detail=f"Sectie '{section_id}' is niet beschikbaar")

    # Parse JSON body
    submitted_data = await request.json()

    # --- Service add-only enforcement (for services-edit section) ---
    if section_id == "services-edit":
        existing_services = set(_extract_services(project_data))
        submitted_services = set(_extract_services(submitted_data))
        removed = existing_services - submitted_services
        if removed:
            errors = {"services": [f"Services kunnen niet verwijderd worden: {', '.join(sorted(removed))}"]}
            fields_html = _render_section_html(section, project_data, errors=errors)
            return HTMLResponse(content=fields_html, status_code=422)

    # Process the submission through the standard pipeline
    processor = EditableFormProcessor()
    result_yaml, errors = processor.process_json_submission(
        submitted_data,
        section.editables,
        project_data,
        edit_mode=True,
    )

    if errors:
        fields_html = _render_section_html(section, project_data, errors=errors)
        return HTMLResponse(content=fields_html, status_code=422)

    # Save the updated project file
    save_project_file(project.filename, result_yaml)
    project_service.load_project_from_data(result_yaml, project.filename)
    logger.info("Project %s section '%s' updated by %s", project_name, section_id, user_email)

    # --- save_only: just save the file, no background processing ---
    if section.post_save_action == "save_only":
        logger.info("Section '%s' is save_only, skipping background processing", section_id)
        return HTMLResponse(content="", status_code=200)

    # --- process_project: trigger background deployment pipeline ---

    # Determine which config sections are needed for newly added services
    config_sections_needed: list[str] = []
    if section_id == "services-edit":
        old_services = set(_extract_services(project_data))
        new_services = set(_extract_services(result_yaml))
        added_services = new_services - old_services
        config_sections_needed.extend(
            SERVICE_CONFIG_SECTIONS[svc_name].section_id
            for svc_name in added_services
            if svc_name in SERVICE_CONFIG_SECTIONS
        )

    yaml_instance = YAML()
    yaml_instance.preserve_quotes = True
    yaml_instance.width = 4096
    yaml_output = StringIO()
    yaml_instance.dump(result_yaml, yaml_output)
    yaml_content = yaml_output.getvalue()

    display_name = result_yaml.get("display-name", project_name)
    task_id = create_task(display_name)

    from opi.core.simple_background import process_project_yaml_background

    logger.info(
        "Starting background project processing for %s (task=%s, section=%s)", project_name, task_id, section_id
    )

    response = HTMLResponse(content="", status_code=200)

    if config_sections_needed:
        response.headers["X-Next-Section"] = config_sections_needed[0]

    response.headers["X-Task-Id"] = task_id
    response.background = BackgroundTask(process_project_yaml_background, task_id, project_name, yaml_content)
    return response
