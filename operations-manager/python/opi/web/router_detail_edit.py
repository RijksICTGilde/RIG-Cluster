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


def _require_project_member_access(request: Request, project_name: str):
    """Check auth for project member access (any role). Returns (project, user_email)."""
    from opi.services.project_service import get_project_service

    user = get_current_user(request)
    project_service = get_project_service()
    project = project_service.get_project(project_name)

    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' niet gevonden")

    user_email = user.get("email", "").lower()
    if not project_service.is_user_authorized_for_project(project_name, user_email):
        raise HTTPException(status_code=403, detail="Geen toegang tot dit project")

    return project, user_email


def _is_backup_restore_flow(flow_id: str) -> bool:
    """Check if a flow ID is a backup or restore flow."""
    return flow_id in ("modal-backup", "modal-restore")


def _flow_context_from_state(state, flow_id: str) -> dict[str, Any]:
    """Extract flow builder context from wizard state.

    Deployment edit flows need ``component_count`` so the sequence
    enforces a max-items limit matching the number of project components.
    """
    if flow_id.startswith("modal-edit-deployment-") and state and state.template_data:
        components = state.template_data.get("components", [])
        return {"component_count": len(components)}
    return {}


def _pad_sparse_submission(body: dict[str, Any], flow_id: str) -> dict[str, Any]:
    """Pad sparse arrays collapsed by json-enc's cleanArrays.

    Single-item edit flows (component-N, deployment-N, domain-N) produce
    form fields at a specific array index (e.g. ``components[1]/name``).
    json-enc's ``cleanArrays`` collapses ``{"1": {...}}`` into ``[{...}]``,
    losing the original index.  This re-pads the array so that
    ``get_value`` finds data at the correct position.
    """
    for prefix, key in [
        ("modal-edit-component-", "components"),
        ("modal-edit-deployment-", "deployments"),
        ("modal-edit-domain-", "deployments"),
    ]:
        if flow_id.startswith(prefix):
            suffix = flow_id.removeprefix(prefix)
            if suffix.isdigit():
                target_idx = int(suffix)
                items = body.get(key)
                if isinstance(items, list) and len(items) >= 1 and target_idx > 0:
                    # Insert empty placeholders so the actual data sits at target_idx
                    padded = [{} for _ in range(target_idx)] + items
                    return {**body, key: padded}
            break
    return body


def _determine_flow_action(flow, active_sections) -> str:
    """Return the post-save action for the flow.

    Returns 'process_project', 'trigger_backup', 'trigger_restore', or 'save_only'.
    """
    for section in active_sections:
        if section.post_save_action in ("process_project", "trigger_backup", "trigger_restore"):
            return section.post_save_action
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

    flow = get_flow(flow_id, **_flow_context_from_state(state, flow_id))
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
    yaml_data, _errors = await processor.process_json_submission(
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
    # Backup/restore flows only require project membership, not admin/owner
    if _is_backup_restore_flow(flow_id):
        project, _user_email = _require_project_member_access(request, project_name)
    else:
        project, _user_email = _require_project_edit_access(request, project_name)

    project_data = project.data or {}

    # Pass context to dynamic flow builders (e.g. component_count for deployment edit)
    flow_context: dict[str, Any] = {}
    if flow_id.startswith("modal-edit-deployment-"):
        flow_context["component_count"] = len(project_data.get("components", []))

    flow = get_flow(flow_id, **flow_context)

    # When adding a new component, ensure the components list has the target slot
    if flow_id.startswith("modal-edit-component-"):
        idx = int(flow_id.removeprefix("modal-edit-component-"))
        components = list(project_data.get("components", []))
        if idx >= len(components):
            while len(components) <= idx:
                components.append({})
            project_data = {**project_data, "components": components}

    # Populate transient fields for deferred editables (e.g. custom domain text input)
    processor = EditableFormProcessor()
    for section in flow.sections:
        processor.populate_deferred_fields(project_data, section.editables)

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

    # Deployment edit flows need component names for the reference provider
    if flow_id.startswith("modal-edit-deployment-"):
        components = project_data.get("components", [])
        state.template_data = {"components": components}

    # Inject backup/restore context into template_data for wizard partials
    if _is_backup_restore_flow(flow_id):
        state.template_data = await _build_backup_restore_context_async(flow_id, project_name, project_data)

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

    flow = get_flow(flow_id, **_flow_context_from_state(state, flow_id))
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
    if _is_backup_restore_flow(flow_id):
        _require_project_member_access(request, project_name)
    else:
        _require_project_edit_access(request, project_name)

    state = get_modal_wizard_state(request)
    if not state or state.flow_id != flow_id:
        raise HTTPException(status_code=400, detail="Geen modal wizard sessie gevonden")

    flow = get_flow(flow_id, **_flow_context_from_state(state, flow_id))
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
    is_rerender = bool(body.pop("_rerender", None))

    if seq_action in ("add", "remove"):
        yaml_data = state.get_merged_data()
        processor = EditableFormProcessor()
        padded_body = _pad_sparse_submission(body, flow_id)
        merged, _err = await processor.process_json_submission(
            padded_body,
            section.editables,
            yaml_data,
            edit_mode=True,
        )

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

    submitted_data = _pad_sparse_submission(body, flow_id)

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

    # Backup/restore sections have no editables — store raw form data directly
    if _is_backup_restore_flow(flow_id) and not section.editables:
        state.store_step_data(section_id, submitted_data)
        state.mark_completed(section_id)
    else:
        # Validate
        processor = EditableFormProcessor()
        yaml_data = state.get_merged_data()
        submitted_yaml, errors = await processor.process_json_submission(
            submitted_data, section.editables, yaml_data, edit_mode=True
        )

        # Auto-add service dependencies
        if section_id == "services-edit" and isinstance(submitted_yaml.get("services"), list):
            from opi.services.services import ServiceAdapter

            submitted_yaml["services"] = ServiceAdapter.resolve_service_dependencies(submitted_yaml["services"])

        if errors:
            step_html = _render_section_html(
                section, submitted_yaml, errors=errors, locked_services=state.locked_services
            )
            rendered = _render_modal_step(request, flow_id, section, step_html, project_name, errors=errors)
            return HTMLResponse(content=rendered)

        # Store step data
        section_keys = {e.editable.yaml_path.split("/")[0].split("[")[0] for e in section.editables}
        section_data = {k: v for k, v in submitted_yaml.items() if k in section_keys}
        state.store_step_data(section_id, section_data)
        state.mark_completed(section_id)

    # Re-render only (preview update) — stay on the same step
    if is_rerender:
        save_modal_wizard_state(request, state)
        yaml_data = state.get_merged_data()
        step_html = _render_section_html(section, submitted_yaml, locked_services=state.locked_services)
        rendered = _render_modal_step(request, flow_id, section, step_html, project_name)
        return HTMLResponse(content=rendered)

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

        # Enrich restore-target context with source deployment info
        if next_section.section_id == "restore-target":
            _enrich_restore_target_context(state)

        save_modal_wizard_state(request, state)

        yaml_data = state.get_merged_data()
        step_html = _render_section_html(next_section, yaml_data, locked_services=state.locked_services)
        rendered = _render_modal_step(request, flow_id, next_section, step_html, project_name)
        return HTMLResponse(content=rendered)

    # All steps completed — show review if flow requires it
    if flow.show_review:
        save_modal_wizard_state(request, state)
        return _render_modal_review(request, project_name, flow_id, active_sections, state)

    # No review needed — do the final submit
    save_modal_wizard_state(request, state)
    return await _modal_do_submit(request, project_name, flow_id)


@detail_edit_router.post("/{project_name}/modal-wizard/{flow_id}/skip", response_class=HTMLResponse)
@requires_sso
async def modal_wizard_skip(request: Request, project_name: str, flow_id: str) -> HTMLResponse:
    """'Later configureren' — save accumulated data and trigger deployment."""
    if _is_backup_restore_flow(flow_id):
        _require_project_member_access(request, project_name)
    else:
        _require_project_edit_access(request, project_name)

    state = get_modal_wizard_state(request)
    if not state or state.flow_id != flow_id:
        raise HTTPException(status_code=400, detail="Geen modal wizard sessie gevonden")

    return await _modal_do_submit(request, project_name, flow_id)


@detail_edit_router.post("/{project_name}/modal-wizard/{flow_id}/confirm", response_class=HTMLResponse)
@requires_sso
async def modal_wizard_confirm(request: Request, project_name: str, flow_id: str) -> HTMLResponse:
    """Confirm after review — execute the final submit."""
    if _is_backup_restore_flow(flow_id):
        _require_project_member_access(request, project_name)
    else:
        _require_project_edit_access(request, project_name)

    state = get_modal_wizard_state(request)
    if not state or state.flow_id != flow_id:
        raise HTTPException(status_code=400, detail="Geen modal wizard sessie gevonden")

    return await _modal_do_submit(request, project_name, flow_id)


@detail_edit_router.get(
    "/{project_name}/modal-wizard/modal-backup/select-deployment",
    response_class=HTMLResponse,
)
@requires_sso
async def backup_select_deployment(request: Request, project_name: str) -> HTMLResponse:
    """HTMX endpoint: re-render the backup step partial when deployment changes."""
    _require_project_member_access(request, project_name)

    state = get_modal_wizard_state(request)
    if not state or state.flow_id != "modal-backup":
        raise HTTPException(status_code=400, detail="Geen modal wizard sessie gevonden")

    selected = request.query_params.get("deployment_name", "")
    if state.template_data:
        state.template_data["_selected_deployment"] = selected
        save_modal_wizard_state(request, state)

    flow = get_flow("modal-backup")
    section = _get_section_from_flow(flow, "backup-select")
    yaml_data = state.get_merged_data()
    step_html = _render_section_html(section, yaml_data, locked_services=state.locked_services)

    rendered = _render_modal_step(request, "modal-backup", section, step_html, project_name)
    return HTMLResponse(content=rendered)


@detail_edit_router.get(
    "/{project_name}/modal-wizard/modal-restore/select-restore-mode",
    response_class=HTMLResponse,
)
@requires_sso
async def restore_select_mode(request: Request, project_name: str) -> HTMLResponse:
    """HTMX endpoint: re-render the restore target step when mode changes."""
    _require_project_member_access(request, project_name)

    state = get_modal_wizard_state(request)
    if not state or state.flow_id != "modal-restore":
        raise HTTPException(status_code=400, detail="Geen modal wizard sessie gevonden")

    restore_mode = request.query_params.get("restore_mode", "existing")
    if state.template_data:
        state.template_data["_restore_mode"] = restore_mode

    _enrich_restore_target_context(state)
    save_modal_wizard_state(request, state)

    flow = get_flow("modal-restore")
    section = _get_section_from_flow(flow, "restore-target")
    yaml_data = state.get_merged_data()
    step_html = _render_section_html(section, yaml_data, locked_services=state.locked_services)

    rendered = _render_modal_step(request, "modal-restore", section, step_html, project_name)
    return HTMLResponse(content=rendered)


def _render_modal_review(
    request: Request,
    project_name: str,
    flow_id: str,
    active_sections,
    state,
) -> HTMLResponse:
    """Render the review/confirmation page for the modal wizard."""
    from opi.web.router_wizard import _build_section_summary

    yaml_data = state.get_merged_data()
    section_summaries = []
    for section in active_sections:
        summary_html = _build_section_summary(section, yaml_data)
        section_summaries.append(
            {
                "section_id": section.section_id,
                "title": section.title,
                "icon": section.icon,
                "summary_html": summary_html,
            }
        )

    section_meta = get_section_metadata(active_sections)
    steps = state.get_steps(section_meta)

    templates = get_templates()
    rendered = templates.get_template("wizard/modal_wizard_review.html.j2").render(
        {
            "request": request,
            "steps": steps,
            "flow_id": flow_id,
            "project_name": project_name,
            "section_summaries": section_summaries,
            "action_label": "Bevestigen en verwerken",
        }
    )
    process_components = templates.env.filters.get("process_components")
    if process_components:
        rendered = str(process_components(rendered))
    return HTMLResponse(content=rendered)


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

    flow = get_flow(flow_id, **_flow_context_from_state(state, flow_id))
    active_sections = resolve_active_sections(flow, state.step_data)

    # Determine post-save action
    action = _determine_flow_action(flow, active_sections)
    templates = get_templates()

    # Backup/restore flows skip project file modification
    if action in ("trigger_backup", "trigger_restore"):
        return await _handle_backup_restore_submit(request, project_name, flow_id, action, state, templates)

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


async def _handle_backup_restore_submit(
    request: Request,
    project_name: str,
    flow_id: str,
    action: str,
    state,
    templates,
) -> HTMLResponse:
    """Handle backup/restore wizard submission — no project file changes."""
    from opi.core.backup_tasks import run_backup_task, run_restore_task
    from opi.core.task_manager import create_task

    merged_data = state.get_merged_data()
    task_id = create_task(project_name)

    if action == "trigger_backup":
        deployment_name = merged_data.get("deployment_name", "")
        resource_types = merged_data.get("resource_types", ["pvc", "database", "minio"])
        if isinstance(resource_types, str):
            resource_types = [resource_types]

        logger.info(
            "Starting backup for %s/%s (task=%s, types=%s)",
            project_name,
            deployment_name,
            task_id,
            resource_types,
        )
        bg_task = BackgroundTask(
            run_backup_task,
            task_id,
            project_name,
            deployment_name,
            resource_types,
        )

    else:  # trigger_restore
        from opi.services import RestoreMode

        backup_run_id = merged_data.get("backup_run_id", "")
        restore_mode = merged_data.get("restore_mode", RestoreMode.EXISTING.value)
        source_deployment = ""
        create_new_deployment = restore_mode == RestoreMode.NEW.value

        # Extract deployment config from editables (new deployment step)
        deployment_config: dict[str, Any] | None = None
        if create_new_deployment:
            deployments = merged_data.get("deployments", [])
            if deployments:
                deployment_config = deployments[0]
            target_deployment = deployment_config.get("name", "") if deployment_config else ""
        else:
            target_deployment = merged_data.get("target_deployment", "")

        # Get backup items for the selected run from template_data
        backup_items = []
        if state.template_data:
            for run in state.template_data.get("_backup_runs", []):
                if run.get("backup_run_id") == backup_run_id:
                    backup_items = run.get("items", [])
                    source_deployment = run.get("deployment_name", "")
                    # If no explicit target, use the source deployment
                    if not target_deployment:
                        target_deployment = source_deployment
                    break

        logger.info(
            "Starting restore for %s/%s from run %s (task=%s, items=%d, new=%s)",
            project_name,
            target_deployment,
            backup_run_id,
            task_id,
            len(backup_items),
            create_new_deployment,
        )
        bg_task = BackgroundTask(
            run_restore_task,
            task_id,
            project_name,
            backup_run_id,
            target_deployment,
            backup_items,
            create_new_deployment=create_new_deployment,
            source_deployment=source_deployment,
            deployment_config=deployment_config,
        )

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


async def _build_backup_restore_context_async(
    flow_id: str,
    project_name: str,
    project_data: dict[str, Any],
) -> dict[str, Any]:
    """Build template context for backup/restore wizard partials.

    Populates _cluster_deployments with deployment info and resource types,
    and _backup_runs with grouped backup data for restore flows.
    """
    from opi.core.cluster_config import get_prefixed_namespace
    from opi.core.config import settings
    from opi.handlers.project_file_handler import create_project_file_handler
    from opi.services import ServiceAdapter

    current_cluster = settings.CLUSTER_MANAGER
    project_file_handler = create_project_file_handler()
    backupable_labels = ServiceAdapter.get_backupable_labels()
    context: dict[str, Any] = {
        "_current_cluster": current_cluster,
        "_project_name": project_name,
        "_backupable_labels": backupable_labels,
    }

    # Build cluster deployments with available resource types
    deployments = project_data.get("deployments", [])
    cluster_deployments: list[dict[str, Any]] = []
    for dep in deployments:
        dep_name = dep.get("name", "")
        dep_cluster = dep.get("cluster", "")
        if dep_cluster != current_cluster:
            continue

        raw_ns = project_file_handler.extract_deployment_namespace(project_data, dep_name)
        k8s_ns = get_prefixed_namespace(dep_cluster, raw_ns) if raw_ns else ""

        resource_types: list[str] = []
        for bl in backupable_labels:
            svc_types = ServiceAdapter.get_service_types_for_backup_label(bl["label"])
            if project_file_handler.deployment_uses_service(project_data, dep_name, svc_types):
                resource_types.append(bl["label"])

        # Only include deployments that have backupable resources
        if not resource_types:
            continue

        cluster_deployments.append(
            {
                "name": dep_name,
                "namespace": k8s_ns,
                "resource_types": resource_types,
            }
        )

    context["_cluster_deployments"] = cluster_deployments

    # For restore flows, also gather backup runs
    if flow_id == "modal-restore":
        context["_backup_runs"] = await _gather_backup_runs_async(project_name, project_data, current_cluster)

    return context


async def _gather_backup_runs_async(
    project_name: str,
    project_data: dict[str, Any],
    current_cluster: str,
) -> list[dict[str, Any]]:
    """Gather backup runs grouped by backup_run_id for the restore wizard (async)."""
    from opi.core.cluster_config import get_prefixed_namespace
    from opi.manager.backup import BackupManager

    backup_runs_map: dict[str, dict[str, Any]] = {}
    try:
        backup_manager = BackupManager()
        deployments = project_data.get("deployments", [])

        for dep in deployments:
            dep_name = dep.get("name", "")
            dep_cluster = dep.get("cluster", "")
            base_ns = dep.get("namespace", "")

            if dep_cluster != current_cluster or not dep_name or not base_ns:
                continue

            k8s_ns = get_prefixed_namespace(dep_cluster, base_ns)
            try:
                snapshots = await backup_manager.list_snapshots(dep_cluster, k8s_ns, project_name=project_name)
                dep_snapshots = [s for s in snapshots if s.deployment_name == dep_name]

                for s in dep_snapshots:
                    run_id = s.backup_run_id or s.snapshot_id
                    if run_id not in backup_runs_map:
                        backup_runs_map[run_id] = {
                            "backup_run_id": run_id,
                            "timestamp": s.timestamp,
                            "deployment_name": dep_name,
                            "resource_count": 0,
                            "resource_types": [],
                            "items": [],
                        }
                    run = backup_runs_map[run_id]
                    run["resource_count"] += 1
                    rt = s.resource_type or "pvc"
                    if rt not in run["resource_types"]:
                        run["resource_types"].append(rt)
                    run["items"].append(
                        {
                            "snapshot_id": s.snapshot_id,
                            "resource_type": rt,
                            "component_name": s.component_name,
                            "storage_name": s.storage_name,
                            "reference_name": s.storage_name or s.pvc_name,
                            "generation": s.generation,
                        }
                    )
            except Exception as e:
                logger.warning("Failed to fetch backups for deployment %s: %s", dep_name, e)

    except Exception:
        logger.warning("Failed to gather backup runs for %s", project_name)

    # Sort by timestamp descending
    backup_runs = sorted(backup_runs_map.values(), key=lambda r: r.get("timestamp", ""), reverse=True)
    return backup_runs


def _enrich_restore_target_context(state) -> None:
    """Derive _source_deployment from the selected backup_run_id in step data.

    Called before rendering the restore-target step so the template knows
    which deployment the backup originated from.
    """
    if not state.template_data:
        return

    # Get backup_run_id from step 1 (restore-select)
    step1_data = state.step_data.get("restore-select", {})
    backup_run_id = step1_data.get("backup_run_id", "")
    if not backup_run_id:
        return

    # Find matching run in _backup_runs
    for run in state.template_data.get("_backup_runs", []):
        if run.get("backup_run_id") == backup_run_id:
            state.template_data["_source_deployment"] = run.get("deployment_name", "")
            break


def _get_section_from_flow(flow, section_id: str):
    """Look up a section by ID within a flow."""
    for section in flow.sections:
        if section.section_id == section_id:
            return section
    raise HTTPException(status_code=404, detail=f"Stap '{section_id}' niet gevonden in flow '{flow.flow_id}'")
