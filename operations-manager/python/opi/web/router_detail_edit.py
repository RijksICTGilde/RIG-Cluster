"""Detail page inline editing via the editables system.

Provides GET/POST endpoints for editing project sections from the
details page modal.  Uses a server-side wizard engine (WizardState)
to drive multi-step edit flows within the modal.
"""

from __future__ import annotations

import logging
from io import StringIO
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from ruamel.yaml import YAML
from starlette.background import BackgroundTask

from opi.core.auth_decorators import get_current_user, requires_sso
from opi.core.templates import get_templates
from opi.forms import FormRenderer, ROOSWidgetAdapter, get_default_nl_translator
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.editables.service_path import smart_get_value, smart_set_value
from opi.forms.visualizers.flows import get_flow
from opi.forms.visualizers.wizard_sections import (
    EDIT_SECTIONS,
    _extract_services,
)
from opi.forms.wizard.resolver import (
    get_section_metadata,
    resolve_active_section_ids,
    resolve_active_sections,
)
from opi.forms.wizard.session import (
    clear_modal_wizard_state,
    get_modal_wizard_state,
    init_modal_wizard_state,
    save_modal_wizard_state,
)
from opi.web.router_wizard import (
    _empty_sequence_item,
    _find_sequence_editable,
    _split_data_across_sections,
)

logger = logging.getLogger(__name__)

detail_edit_router = APIRouter(prefix="/projects", tags=["detail-edit"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _commit_to_git(project_name: str, project_data: dict[str, Any], section_id: str) -> None:
    """Commit and push a project file change to git without deployment."""
    from opi.api.resource_router import _commit_project_yaml

    try:
        filename = f"{project_name}.yaml"
        await _commit_project_yaml(
            project_name,
            filename,
            project_data,
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
    locked_services: list[str] | None = None,
) -> str:
    """Render form fields for a section.

    Args:
        locked_services: Service names that cannot be unchecked (existing project services).
            Passed via ``_locked_services`` key in yaml_data so ``render_service_cards`` can
            lock them independently of dependency-based locking.
    """
    renderer = _create_renderer()
    if not section.layout:
        return ""
    if locked_services is not None:
        yaml_data = {**yaml_data, "_locked_services": locked_services}
    html = renderer.render_fields_from_editables(
        editables=section.editables,
        yaml_data=yaml_data,
        layout=section.layout,
        errors=errors,
        edit_mode=True,
    )
    templates = get_templates()
    process_components_filter = templates.env.filters.get("process_components")
    if process_components_filter is not None:
        html = str(process_components_filter(html))
    return html


def _require_project_edit_access(request: Request, project_name: str):
    """Check auth and return (project, user_email). Raises on failure."""
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

    return project, user_email


def _determine_flow_action(flow, active_sections) -> str:
    """Return 'process_project' if any active section needs deployment, else 'save_only'."""
    for section in active_sections:
        if section.post_save_action == "process_project":
            return "process_project"
    return "save_only"


def _render_modal_step(
    request: Request,
    flow_id: str,
    section,
    step_html: str,
    project_name: str,
    errors: dict[str, list[str]] | None = None,
    global_errors: list[str] | None = None,
) -> str:
    """Render the modal wizard step template and return processed HTML."""
    state = get_modal_wizard_state(request)
    if not state:
        raise HTTPException(status_code=400, detail="Geen modal wizard sessie gevonden")

    flow = get_flow(flow_id)
    active_sections = resolve_active_sections(flow, state.step_data)
    section_meta = get_section_metadata(active_sections)
    steps = state.get_steps(section_meta)

    templates = get_templates()
    context = {
        "request": request,
        "steps": steps,
        "flow_id": flow_id,
        "section": section,
        "step_html": step_html,
        "project_name": project_name,
        "errors": errors or {},
        "global_errors": global_errors or [],
        "step_base_url": f"/projects/{project_name}/modal-wizard/{flow_id}/step/",
        "step_target": "#edit-section-inner",
        "step_push_url": False,
        "step_query_params": "",
    }
    rendered = templates.get_template("wizard/modal_wizard_step.html.j2").render(context)
    process_components = templates.env.filters.get("process_components")
    if process_components:
        rendered = str(process_components(rendered))
    return rendered


def _start_deployment(project_name: str, result_yaml: dict[str, Any]) -> tuple[str, BackgroundTask]:
    """Create a background deployment task. Returns (task_id, background_task)."""
    from opi.core.task_manager import create_task

    yaml_instance = YAML()
    yaml_instance.preserve_quotes = True
    yaml_instance.width = 4096
    yaml_output = StringIO()
    yaml_instance.dump(result_yaml, yaml_output)
    yaml_content = yaml_output.getvalue()

    display_name = result_yaml.get("display-name", project_name)
    task_id = create_task(display_name)

    from opi.core.simple_background import process_project_yaml_background

    bg_task = BackgroundTask(process_project_yaml_background, task_id, project_name, yaml_content)
    return task_id, bg_task


# ---------------------------------------------------------------------------
# Sequence endpoint (add/remove list items) — shared by both old and modal flows
# ---------------------------------------------------------------------------


@detail_edit_router.post("/{project_name}/edit/{section_id}/sequence", response_class=HTMLResponse)
@requires_sso
async def sequence_action(request: Request, project_name: str, section_id: str) -> HTMLResponse:
    """Handle add/remove sequence item and re-render the section form."""
    project, _user_email = _require_project_edit_access(request, project_name)

    section = _get_edit_section(section_id)
    project_data = project.data or {}

    body = await request.json()
    action = body.pop("_seq_action", None)
    seq_path = body.pop("_seq_path", None)
    seq_index = body.pop("_seq_index", None)

    if action not in ("add", "remove") or not seq_path:
        raise HTTPException(status_code=400, detail="Ongeldige reeks-actie")

    # Prefer wizard state data if modal wizard is active
    state = get_modal_wizard_state(request)
    base_data = state.get_merged_data() if state else project_data

    processor = EditableFormProcessor()
    yaml_data, _errors = processor.process_json_submission(
        body,
        section.editables,
        base_data,
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

    locked_services = state.locked_services if state else None
    fields_html = _render_section_html(section, yaml_data, locked_services=locked_services)
    return HTMLResponse(content=fields_html)


# ---------------------------------------------------------------------------
# Modal wizard endpoints
# ---------------------------------------------------------------------------


@detail_edit_router.get("/{project_name}/modal-wizard/{flow_id}", response_class=HTMLResponse)
@requires_sso
async def modal_wizard_init(request: Request, project_name: str, flow_id: str) -> HTMLResponse:
    """Initialize modal wizard and return the first step HTML."""
    project, _user_email = _require_project_edit_access(request, project_name)

    flow = get_flow(flow_id)
    project_data = project.data or {}

    # Pre-fill step data from existing project
    step_data = _split_data_across_sections(flow, project_data)

    # Resolve active sections with pre-filled data
    active_section_ids = resolve_active_section_ids(flow, step_data)
    if not active_section_ids:
        raise HTTPException(status_code=500, detail="Geen stappen gevonden")

    first_step = active_section_ids[0]

    # Initialize modal wizard state
    state = init_modal_wizard_state(
        request,
        flow_id=flow_id,
        first_step=first_step,
        active_sections=active_section_ids,
        project_name=project_name,
    )
    state.step_data = step_data
    state.locked_services = _extract_services(project_data)
    # Mark all sections with data as completed (for step indicator)
    for section_id in active_section_ids:
        if step_data.get(section_id):
            state.mark_completed(section_id)
    save_modal_wizard_state(request, state)

    # Render first step
    section = _get_section_from_flow(flow, first_step)
    yaml_data = state.get_merged_data()
    step_html = _render_section_html(section, yaml_data, locked_services=state.locked_services)

    rendered = _render_modal_step(request, flow_id, section, step_html, project_name)
    return HTMLResponse(content=rendered)


@detail_edit_router.get("/{project_name}/modal-wizard/{flow_id}/step/{section_id}", response_class=HTMLResponse)
@requires_sso
async def modal_wizard_load_step(request: Request, project_name: str, flow_id: str, section_id: str) -> HTMLResponse:
    """Load a step (for back-navigation)."""
    _require_project_edit_access(request, project_name)

    state = get_modal_wizard_state(request)
    if not state or state.flow_id != flow_id:
        raise HTTPException(status_code=400, detail="Geen modal wizard sessie gevonden")

    flow = get_flow(flow_id)
    section = _get_section_from_flow(flow, section_id)

    state.current_step = section_id
    save_modal_wizard_state(request, state)

    yaml_data = state.get_merged_data()
    step_html = _render_section_html(section, yaml_data, locked_services=state.locked_services)

    rendered = _render_modal_step(request, flow_id, section, step_html, project_name)
    return HTMLResponse(content=rendered)


@detail_edit_router.post("/{project_name}/modal-wizard/{flow_id}/step/{section_id}", response_class=HTMLResponse)
@requires_sso
async def modal_wizard_submit_step(request: Request, project_name: str, flow_id: str, section_id: str) -> HTMLResponse:
    """Validate step data and advance to next step, or complete the flow."""
    _require_project_edit_access(request, project_name)

    state = get_modal_wizard_state(request)
    if not state or state.flow_id != flow_id:
        raise HTTPException(status_code=400, detail="Geen modal wizard sessie gevonden")

    flow = get_flow(flow_id)
    section = _get_section_from_flow(flow, section_id)

    # Parse JSON body (requires htmx json-enc extension on the client)
    content_type = request.headers.get("content-type", "")
    if "application/json" not in content_type:
        raise HTTPException(
            status_code=400,
            detail="Verwacht JSON body (json-enc extensie niet geladen?)",
        )
    body = await request.json()

    # Handle sequence actions inline
    seq_action = body.pop("_seq_action", None)
    seq_path = body.pop("_seq_path", None)
    seq_index = body.pop("_seq_index", None)
    body.pop("_rerender", None)

    if seq_action in ("add", "remove"):
        yaml_data = state.get_merged_data()
        processor = EditableFormProcessor()
        merged, _err = processor.process_json_submission(body, section.editables, yaml_data, edit_mode=True)

        items = smart_get_value(merged, seq_path) if seq_path else []
        if not isinstance(items, list):
            items = []

        if seq_action == "add":
            editable = _find_sequence_editable(section, seq_path)
            items.append(_empty_sequence_item(editable))
        elif seq_action == "remove":
            remove_index = int(seq_index) if seq_index not in (None, "") else -1
            if 0 <= remove_index < len(items):
                items.pop(remove_index)

        smart_set_value(merged, str(seq_path), items)
        step_html = _render_section_html(section, merged, locked_services=state.locked_services)
        rendered = _render_modal_step(request, flow_id, section, step_html, project_name)
        return HTMLResponse(content=rendered)

    submitted_data = body

    # Service removal enforcement: only locked (original) services cannot be removed
    if section_id == "services-edit" and state.locked_services:
        submitted_services = set(_extract_services(submitted_data))
        removed = set(state.locked_services) - submitted_services
        if removed:
            global_errors = [
                "Het verwijderen van bestaande services wordt nog niet ondersteund. "
                "De volgende services zijn weer aangevinkt: "
                f"{', '.join(sorted(removed))}."
            ]
            # Re-render with merged data so user doesn't lose other changes
            yaml_data = state.get_merged_data()
            step_html = _render_section_html(section, yaml_data, locked_services=state.locked_services)
            rendered = _render_modal_step(
                request,
                flow_id,
                section,
                step_html,
                project_name,
                global_errors=global_errors,
            )
            return HTMLResponse(content=rendered)

    # Validate
    processor = EditableFormProcessor()
    yaml_data = state.get_merged_data()
    submitted_yaml, errors = processor.process_json_submission(
        submitted_data, section.editables, yaml_data, edit_mode=True
    )

    # Auto-add service dependencies
    if section_id == "services-edit" and isinstance(submitted_yaml.get("services"), list):
        from opi.services.services import ServiceAdapter

        submitted_yaml["services"] = ServiceAdapter.resolve_service_dependencies(submitted_yaml["services"])

    if errors:
        step_html = _render_section_html(section, submitted_yaml, errors=errors, locked_services=state.locked_services)
        rendered = _render_modal_step(request, flow_id, section, step_html, project_name, errors=errors)
        return HTMLResponse(content=rendered)

    # Store step data
    section_keys = {e.editable.yaml_path.split("/")[0].split("[")[0] for e in section.editables}
    section_data = {k: v for k, v in submitted_yaml.items() if k in section_keys}
    state.store_step_data(section_id, section_data)
    state.mark_completed(section_id)

    # Re-resolve active sections (services may add/remove conditional steps)
    active_section_ids = resolve_active_section_ids(flow, state.step_data)
    state.active_sections = active_section_ids
    state.stash_inactive_sections(active_section_ids)

    # Determine next step
    active_sections = resolve_active_sections(flow, state.step_data)
    section_ids = [s.section_id for s in active_sections]

    try:
        current_idx = section_ids.index(section_id)
    except ValueError:
        current_idx = -1

    if current_idx < len(active_sections) - 1:
        # More steps to go
        next_section = active_sections[current_idx + 1]
        state.current_step = next_section.section_id
        save_modal_wizard_state(request, state)

        yaml_data = state.get_merged_data()
        step_html = _render_section_html(next_section, yaml_data, locked_services=state.locked_services)
        rendered = _render_modal_step(request, flow_id, next_section, step_html, project_name)
        return HTMLResponse(content=rendered)

    # Last step — do the final submit
    save_modal_wizard_state(request, state)
    return await _modal_do_submit(request, project_name, flow_id)


@detail_edit_router.post("/{project_name}/modal-wizard/{flow_id}/skip", response_class=HTMLResponse)
@requires_sso
async def modal_wizard_skip(request: Request, project_name: str, flow_id: str) -> HTMLResponse:
    """'Later configureren' — save accumulated data and trigger deployment."""
    _require_project_edit_access(request, project_name)

    state = get_modal_wizard_state(request)
    if not state or state.flow_id != flow_id:
        raise HTTPException(status_code=400, detail="Geen modal wizard sessie gevonden")

    return await _modal_do_submit(request, project_name, flow_id)


async def _modal_do_submit(
    request: Request,
    project_name: str,
    flow_id: str,
) -> HTMLResponse:
    """Execute the final modal wizard submission."""
    from opi.handlers.project_file_handler import save_project_file
    from opi.services.project_service import get_project_service

    state = get_modal_wizard_state(request)
    if not state:
        raise HTTPException(status_code=400, detail="Geen modal wizard sessie gevonden")

    flow = get_flow(flow_id)
    active_sections = resolve_active_sections(flow, state.step_data)

    # Merge all step data
    merged_data = state.get_merged_data()

    # Merge with existing project data (preserve system-managed fields)
    project_service = get_project_service()
    project = project_service.get_project(project_name)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' niet gevonden")

    existing_data = project.data or {}
    existing_data.update(merged_data)

    # Save
    save_project_file(project.filename, existing_data)
    project_service.load_project_from_data(existing_data, project.filename)
    logger.info("Project %s updated via modal wizard (flow=%s)", project_name, flow_id)

    # Determine post-save action
    action = _determine_flow_action(flow, active_sections)
    templates = get_templates()

    if action == "process_project":
        task_id, bg_task = _start_deployment(project_name, existing_data)
        logger.info("Starting background processing for %s (task=%s, flow=%s)", project_name, task_id, flow_id)

        rendered = templates.get_template("wizard/modal_wizard_progress.html.j2").render(
            {"task_id": task_id, "project_name": project_name}
        )
        process_components = templates.env.filters.get("process_components")
        if process_components:
            rendered = str(process_components(rendered))

        clear_modal_wizard_state(request)
        response = HTMLResponse(content=rendered)
        response.background = bg_task
        return response

    # save_only
    clear_modal_wizard_state(request)
    rendered = templates.get_template("wizard/modal_wizard_success.html.j2").render({})
    process_components = templates.env.filters.get("process_components")
    if process_components:
        rendered = str(process_components(rendered))

    response = HTMLResponse(content=rendered)
    response.background = BackgroundTask(_commit_to_git, project_name, existing_data, flow_id)
    return response


def _get_section_from_flow(flow, section_id: str):
    """Look up a section by ID within a flow."""
    for section in flow.sections:
        if section.section_id == section_id:
            return section
    raise HTTPException(status_code=404, detail=f"Stap '{section_id}' niet gevonden in flow '{flow.flow_id}'")
