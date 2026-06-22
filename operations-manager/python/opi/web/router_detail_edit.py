"""Detail page inline editing via the editables system.

Provides GET/POST endpoints for editing project sections from the
details page modal.  Uses a server-side wizard engine (WizardState)
to drive multi-step edit flows within the modal.
"""

from __future__ import annotations

import logging
import re
from io import StringIO
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from ruamel.yaml import YAML
from starlette.background import BackgroundTask

from opi.core.auth_decorators import get_current_user, requires_sso
from opi.core.backup_constants import DEFAULT_BACKUP_RESOURCE_TYPES
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
    clear_modal_state_by_token,
    get_modal_state_by_token,
    init_modal_state_tokenized,
    save_modal_state_by_token,
)
from opi.utils.csrf import reject_misfired_form_get
from opi.web.project_edit_security import (
    apply_form_data_to_project,
    require_project_edit_access,
)
from opi.web.router_wizard import (
    _empty_sequence_item,
    _extract_section_data,
    _find_sequence_editable,
    _split_data_across_sections,
)

if TYPE_CHECKING:
    from opi.forms.visualizers.flows import FormFlow
    from opi.forms.visualizers.sections import FormSection
    from opi.forms.wizard.state import WizardState

logger = logging.getLogger(__name__)

_SESSION_EXPIRED = "Sorry, dit had niet mogen gebeuren. Wizard sessie verlopen. Sluit dit venster en probeer opnieuw."


def _get_wizard_token(request: Request) -> str | None:
    """Extract the modal wizard token from query params."""
    return request.query_params.get("_wizard_token")


async def _get_wizard_token_with_body(request: Request) -> str | None:
    """Like _get_wizard_token, but also looks in the request body.

    HTMX may strip query strings from POST URLs depending on how the c-button is
    rendered, so callers that have no preceding form (review/confirm, skip) read
    the token from hx-include / hx-vals as form data or JSON, falling back to
    the query.
    """
    token = request.query_params.get("_wizard_token")
    if token:
        return token

    content_type = request.headers.get("content-type", "")
    try:
        if "application/json" in content_type:
            body = await request.json()
            if isinstance(body, dict):
                value = body.get("_wizard_token")
                if isinstance(value, str) and value:
                    return value
        elif "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
            form = await request.form()
            value = form.get("_wizard_token")
            if isinstance(value, str) and value:
                return value
    except Exception:
        return None
    return None


detail_edit_router = APIRouter(prefix="/projects", tags=["detail-edit"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _commit_to_git(project_name: str, project_data: dict[str, Any], section_id: str) -> None:
    """Commit and push a project file change to git without deployment."""
    from opi.services.resource_tuning_service import commit_project_yaml

    try:
        filename = f"{project_name}.yaml"
        await commit_project_yaml(
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

    If the section has a ``guard`` and it returns a message, that message
    is rendered as an info alert instead of the form fields.

    Args:
        locked_services: Service names that should be visually marked as existing.
            Passed via ``_locked_services`` key in yaml_data so ``render_service_cards`` can
            indicate them. No longer prevents unchecking.
    """
    # Check guard before rendering fields
    if section.guard is not None and not section.guard(yaml_data):
        return f'<c-alert kind="info">{section.guard_message}</c-alert>'

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


def _extract_deployment_index_from_sections(state: WizardState) -> int | None:
    """Extract deployment index from add-deployment section IDs in the state."""
    for section_id in state.active_sections:
        if section_id.startswith("add-deployment-info-"):
            suffix = section_id.removeprefix("add-deployment-info-")
            if suffix.isdigit():
                return int(suffix)
    return None


def _is_backup_restore_flow(flow_id: str) -> bool:
    """Check if a flow ID is a backup or restore flow."""
    return flow_id in ("modal-backup", "modal-restore")


def _flow_context_from_state(state: WizardState | None, flow_id: str) -> dict[str, Any]:
    """Extract flow builder context from wizard state.

    Deployment edit flows need ``component_count`` so the sequence
    enforces a max-items limit matching the number of project components.
    Component add flows need ``is_new`` so the name field is editable.
    """
    if not state or not state.template_data:
        return {}
    if flow_id.startswith(("modal-edit-deployment-", "modal-add-deployment-")):
        components = state.template_data.get("components", [])
        return {"component_count": len(components)}
    if flow_id.startswith("modal-edit-component-") and state.template_data.get("is_new"):
        return {"is_new": True}
    if flow_id == "modal-restore":
        # The new deployment index = total deployments - 1 (the appended empty slot)
        deployments = state.template_data.get("deployments", [])
        return {"deployment_index": len(deployments) - 1}
    return {}


def _fully_owned_list_keys(flow: Any) -> set[str]:
    """Top-level list keys that an editable fully owns (bare yaml_path, no index).

    Such keys must not be duplicated into ``state.template_data`` because
    ``step_data`` already carries the authoritative list; a shadow copy in
    template_data causes ``get_merged_data`` to silently retain items that
    the user removed (merge-by-index never shrinks).

    Indexed paths like ``deployments[0]/name`` are *not* fully owned — those
    sections contribute partial fields and still need merge-by-index against
    the template baseline.
    """
    owned: set[str] = set()
    for section in flow.sections:
        for vis in section.editables:
            path = vis.editable.yaml_path
            if "/" not in path and "[" not in path:
                owned.add(path)
    return owned


def _pad_sparse_submission(body: dict[str, Any], flow_id: str, section_id: str = "") -> dict[str, Any]:
    """Pad sparse arrays collapsed by json-enc's cleanArrays.

    Single-item edit flows (component-N, deployment-N, domain-N) produce
    form fields at a specific array index (e.g. ``components[1]/name``).
    json-enc's ``cleanArrays`` collapses ``{"1": {...}}`` into ``[{...}]``,
    losing the original index.  This re-pads the array so that
    ``get_value`` finds data at the correct position.

    Uses flow_id first; falls back to section_id for flows like
    modal-restore where the index is in the section, not the flow.
    """
    # Try flow_id first (most flows encode the index there)
    for prefix, key in [
        ("modal-edit-component-", "components"),
        ("modal-edit-deployment-", "deployments"),
        ("modal-edit-domain-", "deployments"),
        ("modal-add-deployment-", "deployments"),
        ("modal-edit-backup-schedule-", "deployments"),
    ]:
        if flow_id.startswith(prefix):
            suffix = flow_id.removeprefix(prefix)
            if suffix.isdigit():
                target_idx = int(suffix)
                items = body.get(key)
                if isinstance(items, list) and len(items) >= 1 and target_idx > 0:
                    padded = [{} for _ in range(target_idx)] + items
                    return {**body, key: padded}
            break

    # Fall back to section_id (e.g. "add-deployment-info-1" → deployments index 1)
    for prefix, key in [
        ("add-deployment-info-", "deployments"),
        ("add-deployment-components-", "deployments"),
        ("domain-edit-", "deployments"),
    ]:
        if section_id.startswith(prefix):
            suffix = section_id.removeprefix(prefix)
            if suffix.isdigit():
                target_idx = int(suffix)
                items = body.get(key)
                if isinstance(items, list) and len(items) >= 1 and target_idx > 0:
                    padded = [{} for _ in range(target_idx)] + items
                    return {**body, key: padded}
            break

    return body


def _detect_list_target(flow_id: str, state: Any) -> tuple[str, int, bool] | None:
    """Detect if a flow targets a single item in a list.

    Returns (list_key, index, is_new) or None for non-list flows.
    """
    for prefix, list_key in [
        ("modal-edit-component-", "components"),
        ("modal-edit-deployment-", "deployments"),
        ("modal-add-deployment-", "deployments"),
        ("modal-edit-domain-", "deployments"),
        ("modal-edit-backup-schedule-", "deployments"),
    ]:
        if flow_id.startswith(prefix):
            suffix = flow_id.removeprefix(prefix)
            if suffix.isdigit():
                idx = int(suffix)
                is_new = prefix == "modal-add-deployment-" or (
                    prefix == "modal-edit-component-" and state and (state.template_data or {}).get("is_new", False)
                )
                return list_key, idx, is_new
    return None


def _apply_list_item_merge(
    existing_data: dict[str, Any],
    merged_data: dict[str, Any],
    list_key: str,
    idx: int,
    is_new: bool,
) -> None:
    """Merge a single list item into the existing project data.

    For add: appends the new item to the list.
    For edit: updates the item at the given index in-place, preserving
    fields the form didn't touch (e.g. readonly ``name``).
    """
    import copy

    source_list = merged_data.get(list_key)
    if not isinstance(source_list, list) or idx >= len(source_list):
        return

    item_data = copy.deepcopy(source_list[idx])
    existing_list = existing_data.setdefault(list_key, [])

    if is_new:
        existing_list.append(item_data)
    elif idx < len(existing_list) and isinstance(existing_list[idx], dict):
        existing_list[idx].update(item_data)
    elif idx < len(existing_list):
        existing_list[idx] = item_data


def _seed_components_for_new_deployment(state: Any, dep_idx: int) -> None:
    """Pre-fill the components step when adding a new deployment.

    Always seeds ALL project-level components. When clone-from is set,
    images are copied from the source deployment's components. Otherwise
    image fields are left empty.
    """

    merged = state.get_merged_data()
    deployments = merged.get("deployments", [])
    if dep_idx >= len(deployments):
        return

    project_components = merged.get("components", [])
    if not project_components:
        return

    # clone-from may be a string ("staging") or dict ({"type": ..., "reference": "staging", ...})
    clone_from_raw = deployments[dep_idx].get("clone-from")
    clone_ref = ""
    if isinstance(clone_from_raw, dict):
        clone_ref = clone_from_raw.get("reference", "")
    elif isinstance(clone_from_raw, str):
        clone_ref = clone_from_raw

    # Build image lookup from the source deployment
    source_images: dict[str, str] = {}
    if clone_ref:
        source_dep = next(
            (d for d in deployments if isinstance(d, dict) and d.get("name") == clone_ref),
            None,
        )
        if source_dep and source_dep.get("components"):
            source_images = {
                c.get("reference", ""): c.get("image", "") for c in source_dep["components"] if isinstance(c, dict)
            }

    # Seed all project components, filling images from clone source when available
    seeded_components = [
        {"reference": c.get("name", ""), "image": source_images.get(c.get("name", ""), "")}
        for c in project_components
        if isinstance(c, dict) and c.get("name")
    ]

    if not seeded_components:
        return

    components_section_id = f"add-deployment-components-{dep_idx}"
    seed: dict[str, Any] = {"deployments": [{} for _ in range(dep_idx)] + [{"components": seeded_components}]}
    state.store_step_data(components_section_id, seed)


def _determine_flow_action(flow: FormFlow, active_sections: list[FormSection]) -> str:
    """Return the post-save action for the flow.

    Returns 'process_project', 'trigger_backup', 'trigger_restore', or 'save_only'.
    """
    for section in active_sections:
        if section.post_save_action in ("process_project", "trigger_backup", "trigger_restore"):
            return section.post_save_action
    return "save_only"


def _render_modal_step(
    request: Request,
    wizard_token: str | None,
    state: WizardState,
    flow_id: str,
    section: FormSection,
    step_html: str,
    project_name: str,
    errors: dict[str, list[str]] | None = None,
    global_errors: list[str] | None = None,
) -> str:
    """Render the modal wizard step template and return processed HTML."""
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
        "wizard_token": wizard_token,
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


_DEPLOYMENT_FLOW_RE = re.compile(r"^modal-(?:edit|add)-(?:deployment|domain)-(\d+)$")


def _extract_deployment_name_from_flow(flow_id: str, project_data: dict[str, Any]) -> str | None:
    """Extract the targeted deployment name from a deployment-scoped flow_id.

    Returns the deployment name if the flow targets a specific deployment,
    or None for project-wide flows (components, services, etc.).
    """
    match = _DEPLOYMENT_FLOW_RE.match(flow_id)
    if not match:
        return None
    index = int(match.group(1))
    deployments = project_data.get("deployments", [])
    if index < len(deployments):
        name = deployments[index].get("name")
        if name:
            return name
    return None


async def _start_deployment(
    request: Request, project_name: str, result_yaml: dict[str, Any], deployment_name: str | None = None
) -> str:
    """Create a V2 async task for deployment processing. Returns task_id."""
    from opi.core.task_helpers import create_async_task

    yaml_instance = YAML()
    yaml_instance.preserve_quotes = True
    yaml_instance.width = 4096
    yaml_output = StringIO()
    yaml_instance.dump(result_yaml, yaml_output)
    yaml_content = yaml_output.getvalue()

    task = await create_async_task(
        request=request,
        task_type="create_project",
        project_name=project_name,
        deployment_name=deployment_name,
        payload={"project_name": project_name, "yaml_content": yaml_content, "deployment_name": deployment_name},
        max_attempts=1,
    )
    return str(task["task_id"])


# ---------------------------------------------------------------------------
# Sequence endpoint (add/remove list items) - shared by both old and modal flows
# ---------------------------------------------------------------------------


@detail_edit_router.post("/{project_name}/edit/{section_id}/sequence", response_class=HTMLResponse)
@requires_sso
async def sequence_action(request: Request, project_name: str, section_id: str) -> HTMLResponse:
    """Handle add/remove sequence item and re-render the section form."""
    project, _user_email = require_project_edit_access(request, project_name)

    section = _get_edit_section(section_id)
    project_data = project.data or {}

    body = await request.json()
    action = body.pop("_seq_action", None)
    seq_path = body.pop("_seq_path", None)
    seq_index = body.pop("_seq_index", None)

    if action not in ("add", "remove") or not seq_path:
        raise HTTPException(status_code=400, detail="Ongeldige reeks-actie")

    # Prefer wizard state data if modal wizard is active
    wizard_token = body.pop("_wizard_token", None) or _get_wizard_token(request)
    state = get_modal_state_by_token(wizard_token)
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

    fields_html = _render_section_html(section, yaml_data)
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
        project, _user_email = require_project_edit_access(request, project_name)

    project_data = project.data or {}

    # Pass context to dynamic flow builders (e.g. component_count for deployment edit)
    flow_context: dict[str, Any] = {}
    if flow_id.startswith(("modal-edit-deployment-", "modal-add-deployment-")):
        flow_context["component_count"] = len(project_data.get("components", []))

    # When adding a new component, ensure the components list has the target slot
    if flow_id.startswith("modal-edit-component-"):
        from opi.forms.visualizers.fields.components import COMPONENTS_SEQUENCE

        idx = int(flow_id.removeprefix("modal-edit-component-"))
        components = list(project_data.get("components", []))
        if idx >= len(components):
            flow_context["is_new"] = True
            while len(components) <= idx:
                components.append(_empty_sequence_item(COMPONENTS_SEQUENCE))
            project_data = {**project_data, "components": components}

    # When adding a new deployment (or restoring to a new one), append an
    # empty slot so the form targets that slot instead of an existing deployment.
    if flow_id.startswith("modal-add-deployment-") or flow_id == "modal-restore":
        from opi.core.config import settings

        deployments = list(project_data.get("deployments", []))
        idx = len(deployments)
        deployments.append({"cluster": settings.CLUSTER_MANAGER})
        project_data = {**project_data, "deployments": deployments}

        if flow_id.startswith("modal-add-deployment-"):
            flow_id = f"modal-add-deployment-{idx}"
        else:
            flow_context["deployment_index"] = idx

    flow = get_flow(flow_id, **flow_context)

    # Populate transient fields for deferred editables (e.g. custom domain text input)
    processor = EditableFormProcessor()
    for section in flow.sections:
        processor.populate_deferred_fields(project_data, section.editables)

    # Pre-fill step data from existing project
    step_data = _split_data_across_sections(flow, project_data)

    # Resolve active sections with pre-filled data.
    # For single-section edit flows the section's visibility lambda may not
    # have the full project context (e.g. services list).  The edit button
    # already guards visibility, so treat all sections as active.
    if len(flow.sections) == 1:
        active_section_ids = [flow.sections[0].section_id]
    else:
        active_section_ids = resolve_active_section_ids(flow, step_data)
    if not active_section_ids:
        raise HTTPException(status_code=500, detail="Geen stappen gevonden")

    first_step = active_section_ids[0]

    # Initialize modal wizard state
    wizard_token, state = init_modal_state_tokenized(
        flow_id=flow_id,
        first_step=first_step,
        active_sections=active_section_ids,
        project_name=project_name,
    )
    state.step_data = step_data
    state.locked_services = _extract_services(project_data)
    state.populate_virt_mappings(flow.sections)

    # The full project data is the domain object — providers and converters
    # use it to resolve context (e.g. deployment name from path, component
    # services, etc.).  Step data merges on top during get_merged_data().
    #
    # Editables whose yaml_path points *directly* at a top-level list (e.g.
    # USERS_SEQUENCE at "users", COMPONENTS_SEQUENCE at "components") fully
    # own that list — step_data already carries the complete value. Keeping
    # a copy in template_data would cause get_merged_data's merge-by-index
    # to silently retain items the user removed in the UI.
    owned = _fully_owned_list_keys(flow)
    state.template_data = {k: v for k, v in project_data.items() if k not in owned}
    state.template_data["_wizard_token"] = wizard_token

    # Store is_new flag so _detect_list_target can distinguish add vs edit
    if flow_id.startswith("modal-edit-component-") and flow_context.get("is_new"):
        state.template_data["is_new"] = True
        existing_components = (project.data or {}).get("components", [])
        state.template_data["existing_component_names"] = [
            c.get("name") for c in existing_components if isinstance(c, dict) and c.get("name")
        ]

    # Add-deployment and restore flows need existing names for uniqueness validation
    if flow_id.startswith("modal-add-deployment-") or flow_id == "modal-restore":
        existing_deployments = (project.data or {}).get("deployments", [])
        existing_names = [d.get("name") for d in existing_deployments if isinstance(d, dict) and d.get("name")]
        state.template_data["existing_deployment_names"] = existing_names
        state.template_data["_original_deployment_names"] = existing_names

    # Inject backup/restore context (cluster deployments) for manual backup/restore flows
    if _is_backup_restore_flow(flow_id):
        backup_context = await _build_backup_restore_context_async(flow_id, project_name, project_data)
        # Preselect the deployment the caller is viewing (URL hash), not deployments[0]
        requested_dep = request.query_params.get("deployment", "").strip()
        if requested_dep:
            cluster_deps = backup_context.get("_cluster_deployments", [])
            if any(d.get("name") == requested_dep for d in cluster_deps):
                backup_context["_selected_deployment"] = requested_dep
        state.template_data.update(backup_context)

    # Mark all sections with data as completed (for step indicator)
    for section_id in active_section_ids:
        if step_data.get(section_id):
            state.mark_completed(section_id)
    save_modal_state_by_token(wizard_token, state)

    # Render first step
    section = _get_section_from_flow(flow, first_step)
    yaml_data = state.get_merged_data()

    step_html = _render_section_html(section, yaml_data, locked_services=None)

    rendered = _render_modal_step(request, wizard_token, state, flow_id, section, step_html, project_name)
    return HTMLResponse(content=rendered)


@detail_edit_router.get("/{project_name}/modal-wizard/{flow_id}/step/{section_id}", response_class=HTMLResponse)
@requires_sso
async def modal_wizard_load_step(request: Request, project_name: str, flow_id: str, section_id: str) -> HTMLResponse:
    """Load a step (for back-navigation)."""
    reject_misfired_form_get(request)
    require_project_edit_access(request, project_name)

    wizard_token = _get_wizard_token(request)
    state = get_modal_state_by_token(wizard_token)
    if not state or state.flow_id != flow_id:
        raise HTTPException(status_code=400, detail=_SESSION_EXPIRED)

    flow = get_flow(flow_id, **_flow_context_from_state(state, flow_id))
    section = _get_section_from_flow(flow, section_id)

    state.current_step = section_id
    save_modal_state_by_token(wizard_token, state)

    yaml_data = state.get_merged_data()

    step_html = _render_section_html(section, yaml_data, locked_services=None)

    rendered = _render_modal_step(request, wizard_token, state, flow_id, section, step_html, project_name)
    return HTMLResponse(content=rendered)


@detail_edit_router.post("/{project_name}/modal-wizard/{flow_id}/step/{section_id}", response_class=HTMLResponse)
@requires_sso
async def modal_wizard_submit_step(request: Request, project_name: str, flow_id: str, section_id: str) -> HTMLResponse:
    """Validate step data and advance to next step, or complete the flow."""
    if _is_backup_restore_flow(flow_id):
        _require_project_member_access(request, project_name)
    else:
        require_project_edit_access(request, project_name)

    wizard_token = _get_wizard_token(request)
    state = get_modal_state_by_token(wizard_token)
    if not state or state.flow_id != flow_id:
        logger.warning("Modal wizard session lost for %s/%s (step=%s)", project_name, flow_id, section_id)
        raise HTTPException(status_code=400, detail=_SESSION_EXPIRED)

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
    body.pop("_wizard_token", None)  # Strip token from form data

    # Handle sequence actions inline
    seq_action = body.pop("_seq_action", None)
    seq_path = body.pop("_seq_path", None)
    seq_index = body.pop("_seq_index", None)
    is_rerender = bool(body.pop("_rerender", None))

    if seq_action in ("add", "remove"):
        yaml_data = state.get_merged_data()
        processor = EditableFormProcessor()
        padded_body = _pad_sparse_submission(body, flow_id, section_id)
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
        step_html = _render_section_html(section, merged, locked_services=None)
        rendered = _render_modal_step(request, wizard_token, state, flow_id, section, step_html, project_name)
        return HTMLResponse(content=rendered)

    submitted_data = _pad_sparse_submission(body, flow_id, section_id)

    # Re-render only (preview update, e.g. service checkbox toggled) — process
    # the submission to get merged data but skip validation so newly-visible
    # fields with defaults don't show spurious "required" errors.
    if is_rerender:
        processor = EditableFormProcessor()
        yaml_data = state.get_merged_data()
        submitted_yaml, _errors = await processor.process_json_submission(
            submitted_data,
            section.editables,
            yaml_data,
            edit_mode=True,
        )
        # Drop values for editables whose ``depends_on`` no longer holds —
        # e.g. when the user deselects ``persistent-storage`` the still-rendered
        # config inputs come along in the POST body; without this they would
        # be written back into ``services{persistent-storage}/config`` and the
        # smart-set silently re-adds the service to the selection list. Matches
        # what the create wizard does in ``router_wizard.submit_step``.
        processor.clear_hidden_depends_on(section.editables, submitted_yaml)
        save_modal_state_by_token(wizard_token, state)
        step_html = _render_section_html(section, submitted_yaml, locked_services=None)
        rendered = _render_modal_step(request, wizard_token, state, flow_id, section, step_html, project_name)
        return HTMLResponse(content=rendered)

    # Backup/restore sections have no editables - store raw form data directly
    if _is_backup_restore_flow(flow_id) and not section.editables:
        state.store_step_data(section_id, submitted_data)
        state.mark_completed(section_id)
    else:
        # Validate
        processor = EditableFormProcessor()
        yaml_data = state.get_merged_data()

        # Build enforcer context from template_data (e.g. existing names for uniqueness)
        enforcer_ctx: dict[str, Any] = {"project_name": project_name}
        if state.template_data and "existing_deployment_names" in state.template_data:
            enforcer_ctx["existing_deployment_names"] = state.template_data["existing_deployment_names"]
        if state.template_data and "existing_component_names" in state.template_data:
            enforcer_ctx["existing_component_names"] = state.template_data["existing_component_names"]

        submitted_yaml, errors = await processor.process_json_submission(
            submitted_data,
            section.editables,
            yaml_data,
            edit_mode=True,
            enforcer_context=enforcer_ctx,
        )

        # Drop now-hidden dependent values so stale config doesn't re-add a
        # deselected service via ``smart_set_value`` on the next merge. See the
        # rerender path above and ``router_wizard.submit_step`` for the same
        # call in the create wizard.
        processor.clear_hidden_depends_on(section.editables, submitted_yaml)

        # Auto-add service dependencies
        if section_id == "services-edit" and isinstance(submitted_yaml.get("services"), list):
            from opi.services.services import ServiceAdapter

            submitted_yaml["services"] = ServiceAdapter.resolve_service_dependencies(submitted_yaml["services"])

        # Run section-level enforcer (cross-field validation)
        section_global_errors: list[str] = []
        if not errors and section.enforcer:
            section_global_errors = await processor.enforce_sections(
                submitted_yaml,
                [section],
                enforcer_context=enforcer_ctx,
                field_errors=errors,
            )

        if errors or section_global_errors:
            step_html = _render_section_html(section, submitted_yaml, errors=errors, locked_services=None)
            rendered = _render_modal_step(
                request,
                wizard_token,
                state,
                flow_id,
                section,
                step_html,
                project_name,
                errors=errors,
                global_errors=section_global_errors,
            )
            return HTMLResponse(content=rendered)

        # Store step data
        section_data = _extract_section_data(section.editables, submitted_yaml)
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

        # Enrich restore-target context with source deployment info
        if next_section.section_id == "restore-target":
            _enrich_restore_target_context(state)

        # Pre-fill components when navigating from info to components step
        if section_id.startswith("add-deployment-info-") and next_section.section_id.startswith(
            "add-deployment-components-"
        ):
            suffix = section_id.removeprefix("add-deployment-info-")
            if suffix.isdigit():
                _seed_components_for_new_deployment(state, int(suffix))

        save_modal_state_by_token(wizard_token, state)

        yaml_data = state.get_merged_data()

        step_html = _render_section_html(next_section, yaml_data, locked_services=None)
        rendered = _render_modal_step(request, wizard_token, state, flow_id, next_section, step_html, project_name)
        return HTMLResponse(content=rendered)

    # All steps completed - show review if flow requires it
    if flow.show_review:
        save_modal_state_by_token(wizard_token, state)
        return _render_modal_review(request, wizard_token, project_name, flow_id, active_sections, state)

    # No review needed - do the final submit
    save_modal_state_by_token(wizard_token, state)
    return await _modal_do_submit(request, wizard_token, project_name, flow_id)


@detail_edit_router.post("/{project_name}/modal-wizard/{flow_id}/skip", response_class=HTMLResponse)
@requires_sso
async def modal_wizard_skip(request: Request, project_name: str, flow_id: str) -> HTMLResponse:
    """'Later configureren' - save accumulated data and trigger deployment."""
    if _is_backup_restore_flow(flow_id):
        _require_project_member_access(request, project_name)
    else:
        require_project_edit_access(request, project_name)

    wizard_token = await _get_wizard_token_with_body(request)
    state = get_modal_state_by_token(wizard_token)
    if not state or state.flow_id != flow_id:
        logger.warning("Modal wizard session lost for %s/%s (skip)", project_name, flow_id)
        raise HTTPException(status_code=400, detail=_SESSION_EXPIRED)

    return await _modal_do_submit(request, wizard_token, project_name, flow_id)


@detail_edit_router.post("/{project_name}/modal-wizard/{flow_id}/confirm", response_class=HTMLResponse)
@requires_sso
async def modal_wizard_confirm(request: Request, project_name: str, flow_id: str) -> HTMLResponse:
    """Confirm after review - execute the final submit."""
    if _is_backup_restore_flow(flow_id):
        _require_project_member_access(request, project_name)
    else:
        require_project_edit_access(request, project_name)

    wizard_token = await _get_wizard_token_with_body(request)
    state = get_modal_state_by_token(wizard_token)
    if not state or state.flow_id != flow_id:
        logger.warning("Modal wizard session lost for %s/%s (confirm)", project_name, flow_id)
        raise HTTPException(status_code=400, detail=_SESSION_EXPIRED)

    return await _modal_do_submit(request, wizard_token, project_name, flow_id)


@detail_edit_router.get(
    "/{project_name}/modal-wizard/modal-backup/select-deployment",
    response_class=HTMLResponse,
)
@requires_sso
async def backup_select_deployment(request: Request, project_name: str) -> HTMLResponse:
    """HTMX endpoint: re-render the backup step partial when deployment changes."""
    _require_project_member_access(request, project_name)

    wizard_token = _get_wizard_token(request)
    state = get_modal_state_by_token(wizard_token)
    if not state or state.flow_id != "modal-backup":
        raise HTTPException(status_code=400, detail=_SESSION_EXPIRED)

    selected = request.query_params.get("deployment_name", "")
    if state.template_data:
        state.template_data["_selected_deployment"] = selected
        save_modal_state_by_token(wizard_token, state)

    flow = get_flow("modal-backup")
    section = _get_section_from_flow(flow, "backup-select")
    yaml_data = state.get_merged_data()
    step_html = _render_section_html(section, yaml_data, locked_services=None)

    rendered = _render_modal_step(request, wizard_token, state, "modal-backup", section, step_html, project_name)
    return HTMLResponse(content=rendered)


@detail_edit_router.get(
    "/{project_name}/modal-wizard/modal-restore/select-restore-mode",
    response_class=HTMLResponse,
)
@requires_sso
async def restore_select_mode(request: Request, project_name: str) -> HTMLResponse:
    """HTMX endpoint: re-render the restore target step when mode changes."""
    _require_project_member_access(request, project_name)

    wizard_token = _get_wizard_token(request)
    state = get_modal_state_by_token(wizard_token)
    if not state or state.flow_id != "modal-restore":
        raise HTTPException(status_code=400, detail=_SESSION_EXPIRED)

    restore_mode = request.query_params.get("restore_mode", "existing")
    if state.template_data:
        state.template_data["_restore_mode"] = restore_mode

    _enrich_restore_target_context(state)
    save_modal_state_by_token(wizard_token, state)

    flow = get_flow("modal-restore")
    section = _get_section_from_flow(flow, "restore-target")
    yaml_data = state.get_merged_data()
    step_html = _render_section_html(section, yaml_data, locked_services=None)

    rendered = _render_modal_step(request, wizard_token, state, "modal-restore", section, step_html, project_name)
    return HTMLResponse(content=rendered)


def _render_modal_review(
    request: Request,
    wizard_token: str | None,
    project_name: str,
    flow_id: str,
    active_sections,
    state,
) -> HTMLResponse:
    """Render the review/confirmation page for the modal wizard."""
    from opi.web.router_wizard import _build_section_fields

    yaml_data = state.get_merged_data()

    section_summaries = []
    for section in active_sections:
        fields = _build_section_fields(section, yaml_data)
        section_summaries.append(
            {
                "section_id": section.section_id,
                "title": section.title,
                "icon": section.icon,
                "fields": fields,
            }
        )

    section_meta = get_section_metadata(active_sections)
    steps = state.get_steps(section_meta)

    warnings: list[str] = []

    # Restore flows: warn that restoring may break the running application
    if flow_id == "modal-restore":
        warnings.append(
            "Het herstellen van een backup overschrijft de huidige data. "
            "Dit kan ertoe leiden dat de applicatie tijdelijk niet beschikbaar is "
            "of niet meer correct werkt."
        )

    # Detect removed services for the review warning
    # Only relevant for flows that edit services - skip for deployment add/edit flows
    has_services_section = any("services" in s.section_id for s in active_sections)
    if state.locked_services and has_services_section:
        merged_services = set(_extract_services(yaml_data))
        removed_services = set(state.locked_services) - merged_services
        if removed_services:
            names = ", ".join(sorted(removed_services))
            warnings.append(
                f"De volgende services worden verwijderd: {names}. "
                "Dit kan leiden tot dataverlies. Bijbehorende databases, buckets "
                "en andere resources worden gemarkeerd voor verwijdering."
            )

    templates = get_templates()
    rendered = templates.get_template("wizard/modal_wizard_review.html.j2").render(
        {
            "request": request,
            "steps": steps,
            "flow_id": flow_id,
            "project_name": project_name,
            "wizard_token": wizard_token,
            "section_summaries": section_summaries,
            "action_label": "Bevestigen en verwerken",
            "warnings": warnings,
        }
    )
    process_components = templates.env.filters.get("process_components")
    if process_components:
        rendered = str(process_components(rendered))
    return HTMLResponse(content=rendered)


async def _modal_do_submit(
    request: Request,
    wizard_token: str | None,
    project_name: str,
    flow_id: str,
) -> HTMLResponse:
    """Execute the final modal wizard submission."""
    from opi.handlers.project_file_handler import save_project_file
    from opi.services.project_service import get_project_service

    # TOCTOU recheck on the mutating request. Backup/restore has its own
    # member-level gate further down.
    if not _is_backup_restore_flow(flow_id):
        require_project_edit_access(request, project_name)

    state = get_modal_state_by_token(wizard_token)
    if not state:
        raise HTTPException(status_code=400, detail=_SESSION_EXPIRED)

    flow = get_flow(flow_id, **_flow_context_from_state(state, flow_id))

    # Single-section flows: the visibility lambda may not have full context
    # (e.g. keycloak-config checks for keycloak in services, but step_data
    # only has the virtual key).  The edit button already guards visibility.
    active_sections = flow.sections if len(flow.sections) == 1 else resolve_active_sections(flow, state.step_data)

    # Determine post-save action
    action = _determine_flow_action(flow, active_sections)
    templates = get_templates()

    # Backup/restore flows skip project file modification
    if action in ("trigger_backup", "trigger_restore"):
        return await _handle_backup_restore_submit(
            request, wizard_token, project_name, flow_id, action, state, templates
        )

    # Merge all step data
    merged_data = state.get_merged_data()

    # Collect editables for hook execution and the late transient strip.
    # Transients are intentionally preserved on merged_data here so that
    # PRE_SAVE hooks (e.g. SubdomainRequestHook reading ``_request-subdomain``)
    # see them after the list-item merge into ``existing_data``.
    processor = EditableFormProcessor()
    all_editables = [ed for section in active_sections for ed in section.editables]

    # Strip template-only keys: template_data provides context for rendering
    # and validation (e.g. config for AGE decryption, existing_deployment_names
    # for uniqueness checks) but should not overwrite existing project data.
    # JSON session round-trip also strips ruamel.yaml types (LiteralScalarString).
    step_produced_keys = {k for sd in state.step_data.values() for k in sd}
    template_only_keys = set(state.template_data or {}) - step_produced_keys
    for key in template_only_keys:
        merged_data.pop(key, None)

    # Merge with existing project data (preserve system-managed fields)
    project_service = get_project_service()
    project = project_service.get_project(project_name)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' niet gevonden")

    existing_data = project.data or {}

    # Targeted list merge for flows that operate on a single list item.
    # Instead of replacing the entire list, we add or update one entry.
    list_target = _detect_list_target(flow_id, state)
    if list_target:
        list_key, idx, is_new = list_target
        _apply_list_item_merge(existing_data, merged_data, list_key, idx, is_new)
        merged_data.pop(list_key, None)

        # New deployments need system-managed fields that the form doesn't
        # collect. Copy cluster/namespace/repository from an existing deployment
        # or fall back to sensible defaults.
        if list_key == "deployments" and is_new:
            deployments = existing_data.get("deployments", [])
            if deployments and isinstance(deployments[-1], dict):
                new_dep = deployments[-1]
                # Find an existing deployment to copy system fields from
                existing_dep = next(
                    (d for d in deployments[:-1] if isinstance(d, dict)),
                    None,
                )
                new_dep.setdefault("namespace", project_name)
                if existing_dep:
                    for field in ("cluster", "repository"):
                        if field in existing_dep and field not in new_dep:
                            new_dep[field] = existing_dep[field]

    existing_data = apply_form_data_to_project(existing_data, merged_data)

    # Run post_merge hooks (e.g. distribute component refs to deployments)
    for section in active_sections:
        if section.post_merge:
            section.post_merge(existing_data, merged_data)

    # Compute derived values (e.g. issuer from base-domain)
    processor = EditableFormProcessor()
    for section in active_sections:
        processor.apply_dependent_generators(section.editables, existing_data)

    # PRE_SAVE hooks: run while transients are still available so that hooks
    # such as SubdomainRequestHook can read ``_request-subdomain`` and append
    # the corresponding entry to ``domains.allowed-subdomains``. Mirrors the
    # equivalent block in router_wizard.py.
    from opi.forms.editables.editable import Editable, FormState, WidgetType
    from opi.forms.editables.hooks import StripTransientsHook
    from opi.forms.editables.lifecycle import run_hooks
    from opi.forms.editables.resolvers import build_resolver_map
    from opi.forms.visualizers.visualizer import EditableVisualizer

    strip_hook_editable = EditableVisualizer(
        editable=Editable(
            yaml_path="_system/strip-transients",
            hooks={FormState.PRE_SAVE: StripTransientsHook(all_editables)},
        ),
        widget=WidgetType.HIDDEN,
        label="",
    )
    hook_context = {
        "project_name": project_name,
        "resolvers": build_resolver_map(all_editables),
    }
    await run_hooks(FormState.PRE_SAVE, [*all_editables, strip_hook_editable], existing_data, hook_context)

    # Defensive: ensure any transients not stripped by the PRE_SAVE chain
    # are still removed before save.
    for section in active_sections:
        processor.strip_transients_from(existing_data, section.editables)

    # Ensure AGE-encrypted multiline values use literal block scalars
    from opi.web.router_wizard import _apply_literal_scalars

    _apply_literal_scalars(existing_data)

    # Save
    save_project_file(project.filename, existing_data)
    project_service.load_project_from_data(existing_data, project.filename)
    logger.info("Project %s updated via modal wizard (flow=%s)", project_name, flow_id)

    if action == "process_project":
        # Extract targeted deployment name from flow_id when editing a specific deployment
        target_deployment_name = _extract_deployment_name_from_flow(flow_id, existing_data)
        task_id = await _start_deployment(request, project_name, existing_data, deployment_name=target_deployment_name)
        if target_deployment_name:
            logger.info(
                "Starting targeted deployment for %s/%s (task=%s, flow=%s)",
                project_name,
                target_deployment_name,
                task_id,
                flow_id,
            )
        else:
            logger.info("Starting full project processing for %s (task=%s, flow=%s)", project_name, task_id, flow_id)

        rendered = templates.get_template("wizard/modal_wizard_progress.html.j2").render(
            {"task_id": task_id, "project_name": project_name}
        )
        process_components = templates.env.filters.get("process_components")
        if process_components:
            rendered = str(process_components(rendered))

        clear_modal_state_by_token(wizard_token)
        return HTMLResponse(content=rendered)

    # save_only
    clear_modal_state_by_token(wizard_token)
    rendered = templates.get_template("wizard/modal_wizard_success.html.j2").render({})
    process_components = templates.env.filters.get("process_components")
    if process_components:
        rendered = str(process_components(rendered))

    # Run after_save hooks (fire-and-forget)
    for section in active_sections:
        if section.after_save:
            try:
                await section.after_save(request)
            except Exception:
                logger.exception("after_save hook failed for section %s", section.section_id)

    response = HTMLResponse(content=rendered)
    response.background = BackgroundTask(_commit_to_git, project_name, existing_data, flow_id)
    return response


async def _handle_backup_restore_submit(
    request: Request,
    wizard_token: str | None,
    project_name: str,
    flow_id: str,
    action: str,
    state,
    templates,
) -> HTMLResponse:
    """Handle backup/restore wizard submission via the async task queue."""
    from opi.core.async_task_service import TaskType
    from opi.core.config import settings
    from opi.core.task_helpers import get_task_service

    merged_data = state.get_merged_data()
    task_service = get_task_service(request)

    if action == "trigger_backup":
        deployment_name = merged_data.get("deployment_name", "")
        resource_types = merged_data.get("resource_types", DEFAULT_BACKUP_RESOURCE_TYPES)
        if isinstance(resource_types, str):
            resource_types = [resource_types]

        task = await task_service.create_task(
            task_type=TaskType.BACKUP,
            project_name=project_name,
            deployment_name=deployment_name,
            cluster=settings.CLUSTER_MANAGER,
            payload={
                "project_name": project_name,
                "deployment_name": deployment_name,
                "resource_types": resource_types,
                "trigger": "manual",
            },
        )
        task_id = task["task_id"]

        logger.info(
            "Starting backup for %s/%s (task=%s, types=%s)",
            project_name,
            deployment_name,
            task_id,
            resource_types,
        )

    else:  # trigger_restore
        from opi.services import RestoreMode

        backup_run_id = merged_data.get("backup_run_id", "")
        restore_mode = merged_data.get("restore_mode", RestoreMode.EXISTING.value)
        source_deployment = ""
        create_new_deployment = restore_mode == RestoreMode.NEW.value

        deployment_config: dict[str, Any] | None = None
        if create_new_deployment:
            # Find the new deployment index from the section ID
            # (e.g. "add-deployment-info-1" → index 1)
            deployments = merged_data.get("deployments", [])
            new_dep_idx = _extract_deployment_index_from_sections(state)
            if new_dep_idx is not None and new_dep_idx < len(deployments):
                deployment_config = deployments[new_dep_idx]
            target_deployment = deployment_config.get("name", "") if deployment_config else ""
        else:
            target_deployment = merged_data.get("target_deployment", "")

        backup_items = []
        if state.template_data:
            for run in state.template_data.get("_backup_runs", []):
                if run.get("backup_run_id") == backup_run_id:
                    backup_items = run.get("items", [])
                    source_deployment = run.get("deployment_name", "")
                    if not target_deployment:
                        target_deployment = source_deployment
                    break

        task = await task_service.create_task(
            task_type=TaskType.RESTORE,
            project_name=project_name,
            deployment_name=target_deployment,
            cluster=settings.CLUSTER_MANAGER,
            payload={
                "project_name": project_name,
                "backup_run_id": backup_run_id,
                "target_deployment": target_deployment,
                "backup_items": backup_items,
                "create_new_deployment": create_new_deployment,
                "source_deployment": source_deployment,
                "deployment_config": deployment_config,
            },
        )
        task_id = task["task_id"]

        logger.info(
            "Starting restore for %s/%s from run %s (task=%s, items=%d, new=%s)",
            project_name,
            target_deployment,
            backup_run_id,
            task_id,
            len(backup_items),
            create_new_deployment,
        )

    rendered = templates.get_template("wizard/modal_wizard_progress.html.j2").render(
        {"task_id": task_id, "project_name": project_name}
    )
    process_components = templates.env.filters.get("process_components")
    if process_components:
        rendered = str(process_components(rendered))

    clear_modal_state_by_token(wizard_token)
    return HTMLResponse(content=rendered)


# ---------------------------------------------------------------------------
# Job Runner endpoints
# ---------------------------------------------------------------------------


@detail_edit_router.get("/{project_name}/run-job/{deployment_name}", response_class=HTMLResponse)
@requires_sso
async def job_runner_form(request: Request, project_name: str, deployment_name: str) -> HTMLResponse:
    """Return the job runner form HTML fragment."""
    from opi.utils.secrets import get_deployment_service_secrets

    project, _user_email = require_project_edit_access(request, project_name)
    project_data = project.data or {}

    available_services = get_deployment_service_secrets(project_data, deployment_name)

    templates = get_templates()
    context = {
        "request": request,
        "project_name": project_name,
        "deployment_name": deployment_name,
        "available_services": available_services,
    }
    rendered = templates.get_template("project-details/job-runner-form.html.j2").render(context)
    process_components = templates.env.filters.get("process_components")
    if process_components:
        rendered = str(process_components(rendered))
    return HTMLResponse(content=rendered)


@detail_edit_router.post("/{project_name}/run-job/{deployment_name}", response_class=HTMLResponse)
@requires_sso
async def job_runner_submit(request: Request, project_name: str, deployment_name: str) -> HTMLResponse:
    """Validate form and return confirmation view."""
    from opi.utils.secrets import get_deployment_service_secrets

    project, _user_email = require_project_edit_access(request, project_name)
    project_data = project.data or {}

    form = await request.form()
    image = (form.get("image") or "").strip()
    command = (form.get("command") or "").strip() or None
    env_vars_text = (form.get("env_vars_text") or "").strip() or None
    selected_services = form.getlist("selected_services")

    # Validate
    error = None
    if not image:
        error = "Container image is verplicht."
    elif not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_./:@-]*$", image):
        error = "Ongeldig image formaat."

    if not error and env_vars_text:
        from opi.utils.env_vars import validate_and_parse_env_vars

        try:
            validate_and_parse_env_vars(env_vars_text)
        except ValueError as e:
            error = f"Ongeldige omgevingsvariabelen: {e}"

    # Build display labels for selected services
    available_services = get_deployment_service_secrets(project_data, deployment_name)
    secret_to_label = {s["secret_name"]: s["name"] for s in available_services}
    selected_service_labels = [secret_to_label.get(s, s) for s in selected_services]

    # Resolve namespace for display
    from opi.core.cluster_config import get_prefixed_namespace
    from opi.core.config import settings
    from opi.handlers.project_file_handler import create_project_file_handler

    pfh = create_project_file_handler()
    raw_namespace = pfh.extract_deployment_namespace(project_data, deployment_name)
    namespace = get_prefixed_namespace(settings.CLUSTER_MANAGER, raw_namespace) if raw_namespace else "onbekend"

    templates = get_templates()
    context = {
        "request": request,
        "project_name": project_name,
        "deployment_name": deployment_name,
        "image": image,
        "command": command,
        "env_vars_text": env_vars_text,
        "selected_services": selected_services,
        "selected_service_labels": selected_service_labels,
        "namespace": namespace,
        "error": error,
    }
    rendered = templates.get_template("project-details/job-runner-confirm.html.j2").render(context)
    process_components = templates.env.filters.get("process_components")
    if process_components:
        rendered = str(process_components(rendered))
    return HTMLResponse(content=rendered)


@detail_edit_router.post("/{project_name}/run-job/{deployment_name}/confirm", response_class=HTMLResponse)
@requires_sso
async def job_runner_confirm(request: Request, project_name: str, deployment_name: str) -> HTMLResponse:
    """Start the job as a background task and return progress view."""
    require_project_edit_access(request, project_name)

    form = await request.form()
    image = (form.get("image") or "").strip()
    command = (form.get("command") or "").strip() or None
    env_vars_text = (form.get("env_vars_text") or "").strip() or None
    selected_services: list[str] = form.getlist("selected_services")

    # Parse env vars
    env_vars: dict[str, str] = {}
    if env_vars_text:
        from opi.utils.env_vars import validate_and_parse_env_vars

        env_vars = validate_and_parse_env_vars(env_vars_text)

    from opi.core.job_tasks import run_job_task
    from opi.core.task_manager import create_task

    task_id = create_task(project_name)

    logger.info(
        "Starting job for %s/%s: image=%s, services=%s",
        project_name,
        deployment_name,
        image,
        selected_services,
    )

    bg_task = BackgroundTask(
        run_job_task,
        task_id,
        project_name,
        deployment_name,
        image,
        command,
        env_vars,
        selected_services,
    )

    templates = get_templates()
    context = {"task_id": task_id, "project_name": project_name}
    rendered = templates.get_template("wizard/modal_wizard_progress.html.j2").render(context)
    process_components = templates.env.filters.get("process_components")
    if process_components:
        rendered = str(process_components(rendered))

    response = HTMLResponse(content=rendered)
    response.background = bg_task
    return response


@detail_edit_router.get("/{project_name}/modal-wizard/progress/{task_id}", response_class=HTMLResponse)
@requires_sso
async def modal_wizard_progress_html(request: Request, project_name: str, task_id: str) -> HTMLResponse:
    """Return server-rendered progress fragment for HTMX polling.

    Reads task state from the V2 async task service (database-backed).
    """
    from opi.core.task_helpers import get_task_service
    from opi.web.router import _v2_task_to_template_context

    templates = get_templates()
    task_service = get_task_service(request)
    task = await task_service.get_task(task_id)

    if task is None:
        context: dict[str, Any] = {
            "task_id": task_id,
            "project_name": project_name,
            "progress": 0,
            "current_step": "",
            "tasks": [],
            "status": "failed",
            "error": "Taak niet gevonden",
        }
        rendered = templates.get_template("wizard/modal_wizard_progress_fragment.html.j2").render(context)
        process_components = templates.env.filters.get("process_components")
        if process_components:
            rendered = str(process_components(rendered))
        return HTMLResponse(content=rendered)

    context = _v2_task_to_template_context(task, project_name)
    context["task_id"] = task_id

    rendered = templates.get_template("wizard/modal_wizard_progress_fragment.html.j2").render(context)
    process_components = templates.env.filters.get("process_components")
    if process_components:
        rendered = str(process_components(rendered))

    return HTMLResponse(content=rendered)


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
