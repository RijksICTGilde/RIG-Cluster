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
from opi.forms.editables.service_path import smart_get_value, smart_set_value
from opi.forms.visualizers.wizard_sections import (
    EDIT_SECTIONS,
    SERVICE_CONFIG_SECTIONS,
    _extract_services,
)
from opi.web.router_wizard import _empty_sequence_item, _find_sequence_editable

logger = logging.getLogger(__name__)

detail_edit_router = APIRouter(prefix="/projects", tags=["detail-edit"])


async def _commit_to_git(
    project_name: str, project_data: dict[str, Any], section_id: str
) -> None:
    """Commit and push a project file change to git without deployment.

    Reuses ``_commit_project_yaml`` from the resource API which handles
    git connector lifecycle and YAML serialization.
    """
    from opi.api.resource_router import _commit_project_yaml

    try:
        filename = f"{project_name}.yaml"
        await _commit_project_yaml(
            project_name, filename, project_data,
            f"Update {project_name} ({section_id})",
        )
    except Exception:
        logger.exception("Failed to commit %s to git", project_name)


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


@detail_edit_router.post("/{project_name}/edit/{section_id}/sequence", response_class=HTMLResponse)
@requires_sso
async def sequence_action(request: Request, project_name: str, section_id: str) -> HTMLResponse:
    """Handle add/remove sequence item and re-render the section form."""
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

    body = await request.json()
    action = body.pop("_seq_action", None)
    seq_path = body.pop("_seq_path", None)
    seq_index = body.pop("_seq_index", None)

    if action not in ("add", "remove") or not seq_path:
        raise HTTPException(status_code=400, detail="Ongeldige reeks-actie")

    # Process submitted data to get current values merged with project data
    processor = EditableFormProcessor()
    yaml_data, _errors = processor.process_json_submission(
        body,
        section.editables,
        project_data,
        edit_mode=True,
    )

    items = smart_get_value(yaml_data, seq_path)
    if not isinstance(items, list):
        items = []

    if action == "add":
        editable = _find_sequence_editable(section, seq_path)
        items.append(_empty_sequence_item(editable))
    elif action == "remove":
        remove_index = int(seq_index) if seq_index not in (None, "") else -1
        if 0 <= remove_index < len(items):
            items.pop(remove_index)

    smart_set_value(yaml_data, seq_path, items)

    fields_html = _render_section_html(section, yaml_data)
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

    # --- save_only: git commit+push only, no deployment ---
    if section.post_save_action == "save_only":
        logger.info("Section '%s' is save_only, committing to git without deployment", section_id)
        response = HTMLResponse(content="", status_code=200)
        response.background = BackgroundTask(
            _commit_to_git, project_name, result_yaml, section_id
        )
        return response

    # --- process_project: git commit+push + full deployment pipeline ---

    yaml_instance = YAML()
    yaml_instance.preserve_quotes = True
    yaml_instance.width = 4096
    yaml_output = StringIO()
    yaml_instance.dump(result_yaml, yaml_output)
    yaml_content = yaml_output.getvalue()

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
