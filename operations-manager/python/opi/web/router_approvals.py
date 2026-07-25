"""Admin routes for the generic, catalog-driven approver interface (RC-5).

Lists pending approval items across all projects and drives the approve/deny modal.
The items + verdicts flow through the catalog ApprovalSpecs (opi/services/approvals.py),
so this router is not domain-specific; domains are simply the only approvable today.
Historically ``router_subdomain_admin`` at ``/admin/subdomains``.

Provides a listing page of all domain/subdomain requests across projects,
and admin-scoped modal wizard endpoints for approving/denying requests.
Reuses the editable form framework — no custom form processing.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from opi.core.auth_decorators import get_current_user, requires_sso
from opi.core.project_schema import ProjectIntegrityError, ProjectSchemaError
from opi.core.templates import get_templates
from opi.forms import FormRenderer, ROOSWidgetAdapter, get_default_nl_translator
from opi.forms.visualizers.flows import get_flow
from opi.forms.wizard.resolver import (
    get_section_metadata,
    resolve_active_sections,
)
from opi.forms.wizard.session import (
    clear_modal_state_by_token,
    get_modal_state_by_token,
    init_modal_state_tokenized,
    save_modal_state_by_token,
)
from opi.services.approvals import collect_approval_items
from opi.services.project_store import get_project_store
from opi.services.user_service import get_user_service
from opi.web.menu import get_menu_items
from opi.web.router_wizard import _apply_literal_scalars

logger = logging.getLogger(__name__)

approvals_router = APIRouter(prefix="/admin/approvals", tags=["approvals"])

FLOW_ID = "admin-approval"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_admin(request: Request) -> dict:
    """Return the current user dict or raise 403 if not an admin."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Niet ingelogd")
    email = user.get("email", "").lower()
    if not get_user_service().is_platform_admin(email):
        raise HTTPException(status_code=403, detail="Alleen beheerders hebben toegang")
    return user


def _create_renderer() -> FormRenderer:
    return FormRenderer(
        widget_adapter=ROOSWidgetAdapter(),
        translator=get_default_nl_translator(),
    )


def _render_section_html(
    section: Any,
    yaml_data: dict[str, Any],
    errors: dict[str, list[str]] | None = None,
) -> str:
    """Render form fields for a section."""
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
    templates = get_templates()
    process_components_filter = templates.env.filters.get("process_components")
    if process_components_filter is not None:
        html = str(process_components_filter(html))
    return html


def _render_modal_step(
    request: Request,
    wizard_token: str | None,
    state: Any,
    section: Any,
    step_html: str,
    project_name: str,
    errors: dict[str, list[str]] | None = None,
    global_errors: list[str] | None = None,
) -> str:
    """Render the modal wizard step wrapper."""
    flow = get_flow(FLOW_ID)
    active_sections = resolve_active_sections(flow, state.step_data)
    section_meta = get_section_metadata(active_sections)
    steps = state.get_steps(section_meta)

    templates = get_templates()
    context = {
        "request": request,
        "steps": steps,
        "flow_id": FLOW_ID,
        "section": section,
        "step_html": step_html,
        "project_name": project_name,
        "wizard_token": wizard_token,
        "errors": errors or {},
        "global_errors": global_errors or [],
        "step_base_url": f"/admin/approvals/{project_name}/modal-wizard/{FLOW_ID}/step/",
        "step_target": "#edit-section-inner",
        "step_push_url": False,
        "step_query_params": "",
    }
    rendered = templates.get_template("wizard/modal_wizard_step.html.j2").render(context)
    process_components = templates.env.filters.get("process_components")
    if process_components:
        rendered = str(process_components(rendered))
    return rendered


def _collect_all_projects_approval_data() -> list[dict[str, Any]]:
    """Collect domain/subdomain data across all projects for the listing page."""
    all_projects = get_project_store().get_all()

    result: list[dict[str, Any]] = []
    for project in sorted(all_projects, key=lambda p: p.name):
        project_name = project.name
        project_data = project.data or {}
        items = collect_approval_items(project_data)
        if items:
            result.append(
                {
                    "project_name": project_name,
                    "approval_items": items,
                }
            )
    return result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@approvals_router.get("", response_class=HTMLResponse)
@requires_sso
async def list_subdomains(request: Request) -> HTMLResponse:
    """List all domain/subdomain requests across all projects."""

    user = _require_admin(request)

    # Pull the latest project data from git so an entry added externally
    # (manual yaml edit + push, or a request created elsewhere) shows up
    # on the admin overview instead of returning a stale in-memory cache.

    projects_data = _collect_all_projects_approval_data()

    templates = get_templates()
    return templates.TemplateResponse(
        "admin/approvals.html.j2",
        {
            "request": request,
            "menu_items": get_menu_items(user),
            "projects_data": projects_data,
            "success_message": request.query_params.get("success"),
        },
    )


@approvals_router.get("/{project_name}/modal-wizard/{flow_id}", response_class=HTMLResponse)
@requires_sso
async def modal_wizard_init(request: Request, project_name: str, flow_id: str) -> HTMLResponse:
    """Initialize the domain approval modal wizard for a project."""
    user = _require_admin(request)

    if flow_id != FLOW_ID:
        raise HTTPException(status_code=404, detail="Onbekende flow")

    project = get_project_store().get(project_name)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' niet gevonden")

    project_data = project.data or {}
    approval_items = collect_approval_items(project_data)
    if not approval_items:
        raise HTTPException(status_code=400, detail="Geen domein- of subdomeinaanvragen voor dit project")

    flow = get_flow(flow_id)
    first_section = flow.sections[0]

    # Seed wizard state with approval items
    seed_data: dict[str, Any] = {"_approval_items": approval_items}

    wizard_token, state = init_modal_state_tokenized(
        flow_id=flow_id,
        first_step=first_section.section_id,
        active_sections=[first_section.section_id],
        project_name=project_name,
    )
    state.step_data = {first_section.section_id: seed_data}
    state.template_data = {"_admin_email": user.get("email", "")}
    save_modal_state_by_token(wizard_token, state)

    yaml_data = state.get_merged_data()
    step_html = _render_section_html(first_section, yaml_data)

    rendered = _render_modal_step(request, wizard_token, state, first_section, step_html, project_name)
    return HTMLResponse(content=rendered)


@approvals_router.post(
    "/{project_name}/modal-wizard/{flow_id}/step/{section_id}",
    response_class=HTMLResponse,
)
@requires_sso
async def modal_wizard_submit_step(request: Request, project_name: str, flow_id: str, section_id: str) -> HTMLResponse:
    """Validate and submit the approval step."""
    user = _require_admin(request)

    if flow_id != FLOW_ID:
        raise HTTPException(status_code=404, detail="Onbekende flow")

    wizard_token = request.query_params.get("_wizard_token")
    state = get_modal_state_by_token(wizard_token)
    if not state or state.flow_id != flow_id:
        logger.warning("Modal wizard session lost for %s/%s (state=%s)", project_name, flow_id, state)
        raise HTTPException(
            status_code=400,
            detail="Wizard sessie verlopen. Sluit dit venster en probeer opnieuw.",
        )

    # Parse JSON body
    content_type = request.headers.get("content-type", "")
    if "application/json" not in content_type:
        logger.warning("Expected JSON body, got content-type: %s", content_type)
        raise HTTPException(
            status_code=400,
            detail="Verwacht JSON body (json-enc extensie niet geladen?)",
        )
    body = await request.json()
    body.pop("_wizard_token", None)

    # No editables — store raw form data directly (same pattern as backup/restore)
    state.store_step_data(section_id, body)
    state.mark_completed(section_id)
    save_modal_state_by_token(wizard_token, state)

    # Single-section flow: no review, go straight to submit
    return await _do_submit(request, wizard_token, user, project_name)


async def _do_submit(request: Request, wizard_token: str | None, user: dict, project_name: str) -> HTMLResponse:
    """Execute the final approval submission."""
    state = get_modal_state_by_token(wizard_token)
    if not state:
        raise HTTPException(
            status_code=400,
            detail="Wizard sessie verlopen. Sluit dit venster en probeer opnieuw.",
        )

    flow = get_flow(FLOW_ID)
    active_sections = flow.sections

    merged_data = state.get_merged_data()

    # Inject admin email so post_merge can record it in history
    merged_data["_admin_email"] = user.get("email", "")

    # Merge with existing project data
    project = get_project_store().get(project_name)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' niet gevonden")

    # Read fresh from Git, not the cache, so the approval merges onto current state and a
    # lagging cache is never committed back over newer Git data (the cache/Git timing fix).
    from opi.manager.project_manager import ProjectManager

    # Explicitly close the ProjectManager so its temp git clone is cleaned up,
    # on every exit path including the validation-error re-render below.
    project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")
    try:
        existing_data = await project_manager.get_contents()
        existing_data.update(merged_data)

        # Run post_merge — maps _approval_items back to domains structure
        for section in active_sections:
            if section.post_merge:
                section.post_merge(existing_data, merged_data)

        # Determine which deployment(s) are actually affected by this approval so
        # the redeploy can be scoped to just those, instead of reprocessing the
        # whole project. Any non-skip decision (approved OR denied) on a domain or
        # subdomain redeploys every deployment referencing it. An empty result
        # means no current deployment uses the decided domain(s): the status is
        # still persisted, but nothing is redeployed.
        from opi.connectors.subdomain import find_deployments_for_domain_item

        affected_deployments: set[str] = set()
        for item in merged_data.get("_approval_items", []):
            if not isinstance(item, dict) or item.get("status", "skip") == "skip":
                continue
            affected_deployments.update(find_deployments_for_domain_item(existing_data, item))
        deployment_names = sorted(affected_deployments)

        # Strip transient keys that should not persist to YAML
        existing_data.pop("_admin_email", None)
        existing_data.pop("_approval_items", None)

        _apply_literal_scalars(existing_data)

        # Save through the single validated path: schema + structural integrity
        # validation, canonical dumper, commit + push, and cache refresh in one shot.
        # A validation failure re-renders the approval step with the message instead
        # of 500ing (e.g. pre-existing structural drift surfaced by the full check).
        try:
            await project_manager.save_and_commit_project(
                existing_data, f"Update project {project_name} (domain approval)"
            )
        except (ProjectSchemaError, ProjectIntegrityError) as e:
            logger.warning("Domain approval save rejected by validation for %s: %s", project_name, e)
            first_section = active_sections[0]
            step_html = _render_section_html(first_section, state.get_merged_data())
            rendered = _render_modal_step(
                request, wizard_token, state, first_section, step_html, project_name, global_errors=[str(e)]
            )
            return HTMLResponse(content=rendered)
    finally:
        await project_manager.close()
    logger.info("Project %s domains updated via admin approval (by %s)", project_name, user.get("email"))

    # Trigger full project processing
    from opi.utils.yaml_util import dump_yaml_to_string

    yaml_content = dump_yaml_to_string(existing_data)

    from opi.core.task_helpers import create_async_task

    task = await create_async_task(
        request=request,
        task_type="create_project",
        project_name=project_name,
        payload={
            "project_name": project_name,
            "yaml_content": yaml_content,
            "deployment_names": deployment_names,
        },
        max_attempts=1,
    )
    logger.info(
        "Domain approval for %s scoped redeploy to deployment(s): %s",
        project_name,
        deployment_names or "(none affected)",
    )
    task_id = str(task["task_id"])

    templates = get_templates()
    rendered = templates.get_template("wizard/modal_wizard_progress.html.j2").render(
        {"task_id": task_id, "project_name": project_name}
    )
    process_components = templates.env.filters.get("process_components")
    if process_components:
        rendered = str(process_components(rendered))

    clear_modal_state_by_token(wizard_token)
    return HTMLResponse(content=rendered)
