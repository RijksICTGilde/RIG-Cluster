"""Inline editing of a project from the details page, through the editables system.

The GET/POST endpoints of the details-page modal, driven by the server-side wizard engine
(``WizardState``) so an edit flow can have several steps inside the modal.
"""

from __future__ import annotations

import copy
import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from opi.core.authz.actions import Action, action
from opi.core.backup_constants import DEFAULT_BACKUP_RESOURCE_TYPES
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.editables.service_path import smart_get_value, smart_set_value
from opi.forms.i18n import get_default_nl_translator
from opi.forms.renderer import FormRenderer
from opi.forms.visualizers.flows import (
    flow_context_from_base,
    get_flow,
    parse_indexed_flow_id,
)
from opi.forms.visualizers.wizard_sections import (
    EDIT_SECTIONS,
    _extract_services,
)
from opi.forms.widgets.lotc import LOTCWidgetAdapter
from opi.forms.wizard.members import members_for_form
from opi.forms.wizard.mutation import apply_component_services_mutation, apply_services_mutation
from opi.forms.wizard.resolver import (
    get_section_metadata,
    resolve_active_section_ids,
    resolve_active_sections,
)
from opi.forms.wizard.save import apply_modal_edit
from opi.forms.wizard.secrets import (
    reachable_leaf_keys,
    redact_unreachable_secrets,
)
from opi.forms.wizard.selection import narrow_to_selection, parse_selection, selectable_names
from opi.forms.wizard.session import (
    clear_modal_state_by_token,
    init_modal_state_tokenized,
    load_modal_state_for,
    save_modal_state_by_token,
)
from opi.http.auth.auth_decorators import get_current_user, requires_sso
from opi.http.auth.authz import (
    authorize,
    authorized_project,
    require_action,
    require_clone_source_access,
    require_on_any_deployment,
)
from opi.http.auth.step_up_dialog import render_step_up_dialog, step_up_dialog_response
from opi.http.render.lotc_switch import render_fragment
from opi.http.render.navigation_lotc import to_nldd_icon
from opi.http.render.templates_lotc import templates_lotc
from opi.manager.clone_validation import build_restore_target_deployment
from opi.services.approvals import ensure_approval_requests
from opi.services.catalog.cross_domain_access.context import build_cross_domain_context
from opi.services.project import Project
from opi.services.project_environment import environment_markings, log_production_downgrades
from opi.services.project_file_handler import extract_attachment_catalog
from opi.services.project_schema import ProjectIntegrityError, ProjectSchemaError
from opi.services.project_store import ConflictError, get_project_store
from opi.services.step_up import (
    has_fresh_step_up,
    log_step_up_refusal,
    production_marking_change,
    save_requires_step_up,
)
from opi.utils.csrf import reject_misfired_form_get
from opi.utils.yaml_util import dump_yaml_to_string
from opi.web.router_wizard import (
    _empty_sequence_item,
    _extract_section_data,
    _find_sequence_editable,
    _split_data_across_sections,
    flow_from_path,
)
from opi.web.router_wizard_attachments import REPLACE_TARGET_KEY

if TYPE_CHECKING:
    from opi.forms.visualizers.flows import FormFlow
    from opi.forms.visualizers.sections import FormSection
    from opi.forms.wizard.state import WizardState
    from opi.services.project_service import ProjectSummary

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


def _create_renderer() -> FormRenderer:
    """The form renderer for this request.

    The PREPARATION per field type sits in the adapter and is business logic: which options,
    which value, how a sequence is built. The component system only decides the templates that
    render the field.
    """
    return FormRenderer(
        widget_adapter=LOTCWidgetAdapter(),
        translator=get_default_nl_translator(),
    )


def _progress_fragment(request: Request, context: dict[str, Any]) -> str:
    """The progress fragment of the dialog, in the design this request chose.

    ONE render, deliberately without ``process_components`` over it: the fragment is a template
    file, so its component tags were already replaced at compile time. A second pass would read
    the rendered HTML as Jinja again, and a step or subtask name carrying ``{{ ... }}`` would be
    executed instead of shown. Same as ``render_progress_fragment`` in
    ``opi/web/task_progress.py``.
    """
    return render_fragment(
        request,
        template="bg/_modal-wizard-progress-fragment.html.j2",
        context=context,
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
    """Render the form fields of a section.

    When the section has a ``guard`` that returns a message, that message is rendered as an info
    alert instead of the fields. ``locked_services`` are the service names to mark visually as
    existing; they travel in the ``_locked_services`` key of ``yaml_data`` so
    ``render_service_cards`` can show them, and they no longer prevent unticking.

    The adapter renders at once, so the result must NOT go through a template render again: it
    carries what someone typed into the form.
    """
    if section.guard is not None and not section.guard(yaml_data):
        return templates_lotc.env.get_template("bg/_modal-guard.html.j2").render({"message": section.guard_message})

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
        # Constant here, not a re-derivation of ``state.is_edit``: every route in this
        # module takes ``project_name`` from the path, so the base always exists.
        edit_mode=True,
    )
    return html


#: Which action a modal-wizard flow asks for. The route itself carries ``project.read`` as its
#: floor; what the flow REALLY asks depends on ``flow_id``, which is a path parameter. Creating
#: a backup and restoring have shared the same matrix row since RC-392: non-production is
#: developer, production is ops; every other flow edits the project file.
FLOW_ACTIONS: dict[str, Action] = {
    "modal-backup": Action.BACKUP_CREATE,
    "modal-restore": Action.RESTORE_RUN,
    # The team wizard owns ``users``, so it asks for the same right as the Team card.
    "modal-edit-team": Action.MEMBER_MANAGE,
}


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


def _flow_action(flow_id: str) -> Action:
    return FLOW_ACTIONS.get(flow_id, Action.PROJECT_EDIT)


def _check_flow_access(request: Request, project_name: str, flow_id: str) -> None:
    """The gate for NAVIGATING a flow: a 403 without an audit row.

    Opening, loading, submitting or skipping a step is not an action, so it gets no row; a
    refusal is recorded by ``require_action``.

    The restore is the exception: ``restore.run`` weighs on the source and on the target, and
    those are only in the session state (``_authorize_restore``). Without a deployment it would
    weigh against the highest marking of the project, and a developer could then not even open
    the wizard while they may restore between two non-production deployments.
    """
    if flow_id == "modal-restore":
        require_on_any_deployment(request, Action.RESTORE_RUN, project_name)
        return
    require_action(request, _flow_action(flow_id), project_name)


def _authorize_flow(request: Request, project_name: str, flow_id: str) -> tuple[ProjectSummary, str]:
    """The gate for the FINISHED flow, the only one that records the decision.

    Returns ``(project, user_email)``. Except for the restore, which records its decision on
    the source and the target it really names; see :func:`_check_flow_access`.
    """
    if flow_id == "modal-restore":
        _check_flow_access(request, project_name, flow_id)
        project = get_project_store().get(project_name)
        if project is None:
            raise HTTPException(status_code=404, detail=f"Project '{project_name}' niet gevonden")
        return project, str((get_current_user(request) or {}).get("email", "")).lower()
    return authorize(request, action=_flow_action(flow_id), project_name=project_name)


def _flow_context_from_state(state: WizardState | None, flow_id: str) -> dict[str, Any]:
    """What the flow builder for *flow_id* needs from this wizard session.

    Each flow family declares that for itself (see ``INDEXED_FLOWS``); this
    only hands it the session's template data.
    """
    if not state:
        return {}
    return flow_context_from_base(flow_id, state.base_data)


def _fully_owned_list_keys(flow: Any) -> set[str]:
    """Top-level list keys that an editable fully owns (bare yaml_path, no index).

    Such keys must not be duplicated into ``state.base_data`` because
    ``step_data`` already carries the authoritative list; a shadow copy in
    base_data causes ``get_merged_data`` to silently retain items that
    the user removed (merge-by-index never shrinks).

    Indexed paths like ``deployments[0]/name`` are NOT fully owned: those sections contribute
    partial fields and still need merge-by-index against the template baseline.
    """
    owned: set[str] = set()
    for section in flow.sections:
        for vis in section.editables:
            # A readonly visualizer never writes, so it owns nothing: in a flow where only
            # such a carrier references the list, base_data must keep it or the renderer has no
            # data at all. _split_data_across_sections skips readonly carriers for the same
            # reason.
            if vis.readonly:
                continue
            path = vis.editable.yaml_path
            if "/" not in path and "[" not in path:
                owned.add(path)
    return owned


_ITEM_SELECTION_PATH = re.compile(r"^(\w[\w-]*)\[(\d+)\]/services$")


def _owned_item_selection_paths(flow: Any) -> list[tuple[str, int]]:
    """(list_key, index) pairs whose per-item ``services`` selection an editable owns whole.

    The component form's checklist at ``components[0]/services`` renders the full
    selection, so its step data is authoritative for that list: the same rule
    ``_fully_owned_list_keys`` applies to bare top-level lists, one level down. A shadow copy in
    base_data would win it back through the by-name union in ``get_merged_data``, which never
    removes, so an unticked service would return on every save.
    """

    def _visit(vis_list: list[Any], owned: list[tuple[str, int]]) -> None:
        for vis in vis_list:
            if getattr(vis, "readonly", False):
                continue
            match = _ITEM_SELECTION_PATH.match(vis.editable.yaml_path)
            if match:
                owned.append((match.group(1), int(match.group(2))))
            if getattr(vis, "children", None):
                _visit(vis.children, owned)

    owned: list[tuple[str, int]] = []
    for section in flow.sections:
        _visit(list(section.editables), owned)
    return owned


def _pad_at(body: dict[str, Any], key: str, target_idx: int) -> dict[str, Any]:
    """Re-pad *body*'s *key* list so its single item sits at *target_idx*."""
    items = body.get(key)
    if isinstance(items, list) and len(items) >= 1 and target_idx > 0:
        return {**body, key: [{} for _ in range(target_idx)] + items}
    return body


def _pad_sparse_submission(body: dict[str, Any], flow: FormFlow, section_id: str = "") -> dict[str, Any]:
    """Pad sparse arrays collapsed by json-enc's cleanArrays.

    Single-item edit flows produce form fields at a specific array index
    (e.g. ``components[1]/name``). json-enc's ``cleanArrays`` collapses
    ``{"1": {...}}`` into ``[{...}]``, losing the original index. This
    re-pads the array so that ``get_value`` finds data at the correct
    position.

    Uses the flow's declared target; falls back to section_id for flows like
    modal-restore, where the index sits in the section, not on the flow.
    """
    if flow.target is not None:
        return _pad_at(body, flow.target.list_key, flow.target.index)

    # Fall back to section_id (e.g. "add-deployment-info-1" → deployments index 1)
    for prefix, key in [
        ("add-deployment-info-", "deployments"),
        ("add-deployment-components-", "deployments"),
        ("domain-edit-", "deployments"),
    ]:
        if section_id.startswith(prefix):
            suffix = section_id.removeprefix(prefix)
            if suffix.isdigit():
                return _pad_at(body, key, int(suffix))
            break

    return body


def _strip_attachment_content(project_data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of project_data with attachment ``content`` removed (id/filename kept).

    The wizard only needs the catalog metadata to display attachments; carrying the
    encrypted blocks bloats the disk-backed session. The content is re-attached from the
    stored project at save (PreserveAttachmentContentHook).
    """
    import copy

    from opi.services.project_file_handler import find_attachment_data_list

    data = copy.deepcopy(project_data)
    for att in find_attachment_data_list(data.get("services")) or []:
        if isinstance(att, dict):
            att.pop("content", None)
    return data


def _seed_components_for_new_deployment(state: Any, dep_idx: int) -> None:
    """Pre-fill the components step when adding a new deployment.

    Always seeds ALL project-level components. When a source deployment is chosen
    (``config_source`` in the restore wizard, ``clone-from`` when adding a deployment),
    images are copied from that deployment's components. Otherwise image fields are left
    empty.
    """

    merged = state.get_merged_data()
    deployments = merged.get("deployments", [])
    if dep_idx >= len(deployments):
        return

    project_components = merged.get("components", [])
    if not project_components:
        return

    # The restore wizard picks its source in a field of its own: after the save, ``clone-from``
    # there carries the backup run and not the deployment the config comes from.
    clone_ref = str(merged.get("config_source") or "")

    # clone-from may be a string ("staging") or dict ({"type": ..., "reference": "staging", ...})
    if not clone_ref:
        clone_from_raw = deployments[dep_idx].get("clone-from")
        if isinstance(clone_from_raw, dict):
            clone_ref = clone_from_raw.get("reference", "")
        elif isinstance(clone_from_raw, str):
            clone_ref = clone_from_raw

    # Build image lookup from the source deployment
    source_images: dict[str, str] = {}
    if clone_ref:
        source_dep = Project.locate(deployments, name=clone_ref)
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


def _seed_restore_config_source(state: Any, section_id: str) -> None:
    """Default the restore's config source to the deployment the backup came from.

    Presence, not truth: once the step has the key, the user's own choice stands, the empty
    option "Geen (lege deployment)" included.
    """
    step = dict(state.step_data.get(section_id) or {})
    if "config_source" in step:
        return
    source = (state.base_data or {}).get("_source_deployment", "")
    if not source:
        return
    step["config_source"] = source
    state.store_step_data(section_id, step)


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
    warnings: dict[str, list[str]] | None = None,
) -> str:
    """Render the modal wizard step template and return processed HTML."""
    flow = get_flow(flow_id, **_flow_context_from_state(state, flow_id))
    active_sections = resolve_active_sections(flow, state.step_data)
    section_meta = get_section_metadata(active_sections)
    steps = state.get_steps(section_meta)

    # Flatten the per-field warnings into a plain message list for the banner.
    warning_messages = [msg for msgs in (warnings or {}).values() for msg in msgs]

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
        "warnings": warning_messages,
        "step_base_url": f"/projects/{project_name}/modal-wizard/{flow_id}/step/",
        "step_target": "#edit-section-inner",
        "step_push_url": False,
        "step_query_params": "",
        "show_review": flow.show_review,
        # Our sections carry Dutch ROOS icon names; the LOTC templates need the NLDD words.
        "nldd_icon": to_nldd_icon,
    }
    return render_fragment(
        request,
        template="bg/_modal-wizard-step.html.j2",
        context=context,
    )


async def _validate_modal_flow_for_review(
    request: Request,
    wizard_token: str | None,
    state: WizardState,
    flow_id: str,
    project_name: str,
    active_sections: list[FormSection],
) -> HTMLResponse | None:
    """Validate the whole modal flow before the jump to the summary.

    The modal counterpart of ``router_wizard._validate_whole_flow``: every active section is
    validated against the merged data, the steps the jump skips included. Returns the re-render
    of the first failing step with its errors marked, or None.
    """
    from opi.web.router_wizard import _collect_all_editable_paths, _section_has_errors

    all_editables = [editable for section in active_sections for editable in section.editables]
    yaml_data = state.get_merged_data()
    processor = EditableFormProcessor()
    processor.clear_hidden_depends_on(all_editables, yaml_data)

    enforcer_ctx: dict[str, Any] = {"project_name": project_name}
    if state.base_data and "existing_deployment_names" in state.base_data:
        enforcer_ctx["existing_deployment_names"] = state.base_data["existing_deployment_names"]
    if state.base_data and "existing_component_names" in state.base_data:
        enforcer_ctx["existing_component_names"] = state.base_data["existing_component_names"]

    _final, errors = await processor.process_json_submission(
        yaml_data,
        all_editables,
        yaml_data,
        edit_mode=True,
        enforcer_context=enforcer_ctx,
        strip_transients=False,
    )
    global_errors: list[str] = []
    if not errors:
        field_errors: dict[str, list[str]] = {}
        global_errors = await processor.enforce_sections(
            yaml_data, active_sections, enforcer_context=enforcer_ctx, field_errors=field_errors
        )
        errors = field_errors
    if not errors and not global_errors:
        return None

    logger.warning(
        "Modal review jump blocked for %s/%s: field_errors=%s global_errors=%s",
        project_name,
        flow_id,
        errors,
        global_errors,
    )
    error_section = active_sections[0]
    for section in active_sections:
        if _section_has_errors(_collect_all_editable_paths(section.editables), errors):
            error_section = section
            break
    state.current_step = error_section.section_id
    save_modal_state_by_token(wizard_token, state)
    step_html = _render_section_html(error_section, yaml_data, errors=errors, locked_services=None)
    rendered = _render_modal_step(
        request,
        wizard_token,
        state,
        flow_id,
        error_section,
        step_html,
        project_name,
        errors=errors,
        global_errors=global_errors
        or ["Er zijn nog verplichte velden leeg. Vul deze stap aan voordat je naar de samenvatting gaat."],
    )
    return HTMLResponse(content=rendered)


def _targeted_deployment_name(flow: FormFlow, project_data: dict[str, Any]) -> str | None:
    """The name of the deployment this flow writes to, if it targets one.

    Returns None for project-wide flows (components, services, etc.), which
    process the whole project rather than one deployment.
    """
    target = flow.target
    if target is None or target.list_key != "deployments":
        return None
    deployments = project_data.get("deployments", [])
    if target.index < len(deployments):
        name = deployments[target.index].get("name")
        if name:
            return name
    return None


async def _start_deployment(
    request: Request,
    project_name: str,
    result_yaml: dict[str, Any],
    deployment_name: str | None = None,
) -> str:
    """Create a V2 async task for deployment processing. Returns task_id.

    project_file_already_saved tells the task not to rewrite: the modal already saved.
    """
    from opi.http.task_helpers import create_async_task

    yaml_content = dump_yaml_to_string(result_yaml)

    task = await create_async_task(
        request=request,
        task_type="create_project",
        project_name=project_name,
        deployment_name=deployment_name,
        payload={
            "project_name": project_name,
            "yaml_content": yaml_content,
            "deployment_name": deployment_name,
            "project_file_already_saved": True,
        },
        max_attempts=1,
    )
    return str(task["task_id"])


@detail_edit_router.post("/{project_name}/edit/{section_id}/sequence", response_class=HTMLResponse)
@action(Action.PROJECT_EDIT)
@requires_sso
async def sequence_action(request: Request, project_name: str, section_id: str) -> HTMLResponse:
    """Handle add/remove sequence item and re-render the section form."""
    project = authorized_project(request)

    section = _get_edit_section(section_id)
    project_data = project.data or {}

    body = await request.json()
    action = body.pop("_seq_action", None)
    seq_path = body.pop("_seq_path", None)
    seq_index = body.pop("_seq_index", None)

    if action not in ("add", "remove") or not seq_path:
        raise HTTPException(status_code=400, detail="Ongeldige reeks-actie")

    wizard_token = body.pop("_wizard_token", None) or _get_wizard_token(request)
    state = load_modal_state_for(request, wizard_token, project_name)
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
        items.append(_empty_sequence_item(editable, yaml_data))
    elif action == "remove":
        remove_index = int(seq_index) if seq_index not in (None, "") else -1
        if 0 <= remove_index < len(items):
            items.pop(remove_index)

    smart_set_value(yaml_data, seq_path, items)

    fields_html = _render_section_html(section, yaml_data)
    return HTMLResponse(content=fields_html)


@detail_edit_router.get("/{project_name}/modal-wizard/{flow_id}", response_class=HTMLResponse)
@action(Action.PROJECT_READ)
@requires_sso
async def modal_wizard_init(request: Request, project_name: str, flow_id: str) -> HTMLResponse:
    """Initialize modal wizard and return the first step HTML."""
    _check_flow_access(request, project_name, flow_id)
    project = authorized_project(request)
    user_email = str((get_current_user(request) or {}).get("email", "")).lower()

    project_data = project.data or {}

    # What the flow builder needs, declared by the flow family itself.
    flow_context: dict[str, Any] = dict(flow_context_from_base(flow_id, project_data))

    indexed = parse_indexed_flow_id(flow_id)
    if indexed is not None:
        kind, idx = indexed

        # A component flow opened past the end of the list is an add: make the
        # slot the form writes into.
        if kind.targets_new_item_when_missing:
            from opi.forms.visualizers.fields.components import COMPONENTS_SEQUENCE

            items = list(project_data.get(kind.list_key, []))
            if idx >= len(items):
                flow_context["is_new"] = True
                while len(items) <= idx:
                    items.append(_empty_sequence_item(COMPONENTS_SEQUENCE, project_data))
                project_data = {**project_data, kind.list_key: items}

        # An add flow always writes into a fresh slot at the end of the list.
        if kind.appends_new_item:
            from opi.core.config import settings

            items = list(project_data.get(kind.list_key, []))
            idx = len(items)
            items.append({"cluster": settings.CLUSTER_MANAGER})
            project_data = {**project_data, kind.list_key: items}
            flow_id = f"{kind.prefix}{idx}"

    # Restoring into a new deployment appends the same kind of slot, but the
    # index travels as builder context: the flow id carries no index.
    if flow_id == "modal-restore":
        from opi.core.config import settings

        deployments = list(project_data.get("deployments", []))
        flow_context["deployment_index"] = len(deployments)
        deployments.append({"cluster": settings.CLUSTER_MANAGER})
        project_data = {**project_data, "deployments": deployments}

    flow = flow_from_path(flow_id, **flow_context)

    processor = EditableFormProcessor()
    for section in flow.sections:
        processor.populate_deferred_fields(project_data, section.editables)

    # A selection: the modal shows only these items of the list the flow names. Narrow BEFORE
    # the split, so step_data and base_data both carry only the shown part; the save puts it
    # back among the rest (wizard/selection.py).
    selection = parse_selection(request.query_params.get("components"))
    if selection:
        if flow.selectable is None:
            raise HTTPException(status_code=400, detail=f"Flow '{flow_id}' kent geen selectie van componenten")
        known = selectable_names(project_data, flow.selectable)
        unknown = [name for name in selection if name not in known]
        if unknown:
            raise HTTPException(
                status_code=404,
                detail=f"Component '{', '.join(unknown)}' bestaat niet in dit project",
            )
        project_data = narrow_to_selection(project_data, flow.selectable, selection)

    # Pre-fill step data from the existing project. Strip attachment content first: only the
    # id and filename are needed to display the catalog, and the encrypted blocks bloat the
    # disk-backed session; PreserveAttachmentContentHook re-attaches the content at save.
    #
    # Then drop every other encrypted value this flow cannot edit, so the session stops
    # carrying secrets no step touches. Derived from the flow's own editables rather than a
    # field list; see opi.forms.wizard.secrets.
    session_data = _strip_attachment_content(project_data)
    keep_keys = reachable_leaf_keys([ed for section in flow.sections for ed in section.editables])
    session_data, redacted_paths = redact_unreachable_secrets(session_data, keep_keys)
    if redacted_paths:
        logger.debug(
            "Flow %s cannot edit %d encrypted value(s); kept out of the session: %s",
            flow_id,
            len(redacted_paths),
            ", ".join(redacted_paths),
        )

    # The team form renders only the PEOPLE and their role, so only those travel into the
    # form; ``restore_hidden_members`` puts the tokens and the service accounts back at save.
    owned = _fully_owned_list_keys(flow)
    if "users" in owned:
        session_data = members_for_form(session_data)

    step_data = _split_data_across_sections(flow, session_data)

    # For a single-section edit flow the section's visibility lambda may not have the full
    # project context, and the edit button already guards visibility, so treat every section as
    # active.
    if len(flow.sections) == 1:
        active_section_ids = [flow.sections[0].section_id]
    else:
        active_section_ids = resolve_active_section_ids(flow, step_data)
    if not active_section_ids:
        raise HTTPException(status_code=500, detail="Geen stappen gevonden")

    first_step = active_section_ids[0]

    wizard_token, state = init_modal_state_tokenized(
        flow_id=flow_id,
        first_step=first_step,
        active_sections=active_section_ids,
        project_name=project_name,
        owner_email=user_email,
    )
    state.step_data = step_data
    state.selection = selection
    state.locked_services = _extract_services(project_data)
    state.populate_virt_mappings(flow.sections)

    # The full project data is the domain object: providers and converters use it to resolve
    # context, and step data merges on top in get_merged_data(). An editable whose yaml_path
    # points DIRECTLY at a top-level list owns that list whole, so a copy in base_data would
    # make get_merged_data's merge-by-index silently retain items the user removed.
    #
    # ``session_data`` and not ``project_data``: base_data is persisted to disk like step_data
    # is, so it needs the same attachment strip and secret redaction.
    state.base_data = {k: v for k, v in session_data.items() if k not in owned}
    # The same ownership rule one level down: a per-item selection an editable renders whole
    # is authoritative in step_data, so base_data must not carry a shadow copy. Shallow-copy
    # the touched item: the values in base_data alias session_data and step_data.
    for item_list_key, item_idx in _owned_item_selection_paths(flow):
        items = state.base_data.get(item_list_key)
        if isinstance(items, list) and 0 <= item_idx < len(items) and isinstance(items[item_idx], dict):
            items = list(items)
            slimmed = dict(items[item_idx])
            slimmed.pop("services", None)
            items[item_idx] = slimmed
            state.base_data[item_list_key] = items
    state.base_data["_wizard_token"] = wizard_token

    # Remember that this was an add: the flow is rebuilt from its id on later requests, and
    # only the session knows the item did not exist yet.
    if flow_context.get("is_new"):
        state.base_data["is_new"] = True
        existing_components = (project.data or {}).get("components", [])
        state.base_data["existing_component_names"] = [
            c.get("name") for c in existing_components if isinstance(c, dict) and c.get("name")
        ]

    # Add-deployment and restore flows need the existing names for the uniqueness check.
    if (indexed is not None and indexed[0].appends_new_item) or flow_id == "modal-restore":
        existing_deployments = (project.data or {}).get("deployments", [])
        existing_names = [d.get("name") for d in existing_deployments if isinstance(d, dict) and d.get("name")]
        state.base_data["existing_deployment_names"] = existing_names
        state.base_data["_original_deployment_names"] = existing_names

    if _is_backup_restore_flow(flow_id):
        backup_context = await _build_backup_restore_context_async(flow_id, project_name, project_data)
        # Preselect the deployment the caller is viewing, from the URL hash, not deployments[0].
        requested_dep = request.query_params.get("deployment", "").strip()
        if requested_dep:
            cluster_deps = backup_context.get("_cluster_deployments", [])
            if any(d.get("name") == requested_dep for d in cluster_deps):
                backup_context["_selected_deployment"] = requested_dep
        state.base_data.update(backup_context)

    # Replacing: the dialog was opened at one specific attachment, and from here on that is
    # the session's business and not the request's. The upload endpoints check every id they
    # are handed against this value, so a form that shows the id fixed and a POST that sends a
    # different one get the same answer. Template-only: no editable names it, so it stays out
    # of the saved project.
    requested_replace = request.query_params.get("replace", "").strip()
    if requested_replace:
        if flow_id != "modal-edit-attachments" or requested_replace not in extract_attachment_catalog(project_data):
            raise HTTPException(status_code=404, detail=f"Bijlage '{requested_replace}' bestaat niet in dit project")
        state.base_data[REPLACE_TARGET_KEY] = requested_replace

    # Cross-domain-access needs the list of authorized peer projects, from the same builder
    # the create wizard uses, so the two flows cannot drift apart. Template-only: no editable
    # names it, so it falls outside the write set and never reaches the saved project.
    if flow_id in ("modal-edit-cross-domain-config", "modal-edit-services"):
        state.base_data.update(build_cross_domain_context(user_email))

    # Every section that has data counts as completed, for the step indicator.
    for section_id in active_section_ids:
        if step_data.get(section_id):
            state.mark_completed(section_id)
    save_modal_state_by_token(wizard_token, state)

    section = _get_section_from_flow(flow, first_step)
    yaml_data = state.get_merged_data()

    step_html = _render_section_html(section, yaml_data, locked_services=None)

    rendered = _render_modal_step(request, wizard_token, state, flow_id, section, step_html, project_name)
    return HTMLResponse(content=rendered)


@detail_edit_router.get("/{project_name}/modal-wizard/{flow_id}/step/{section_id}", response_class=HTMLResponse)
@action(Action.PROJECT_READ)
@requires_sso
async def modal_wizard_load_step(request: Request, project_name: str, flow_id: str, section_id: str) -> HTMLResponse:
    """Load a step (for back-navigation)."""
    reject_misfired_form_get(request)
    _check_flow_access(request, project_name, flow_id)

    wizard_token = _get_wizard_token(request)
    state = load_modal_state_for(request, wizard_token, project_name)
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
@action(Action.PROJECT_READ)
@requires_sso
async def modal_wizard_submit_step(request: Request, project_name: str, flow_id: str, section_id: str) -> HTMLResponse:
    """Validate step data and advance to next step, or complete the flow."""
    _check_flow_access(request, project_name, flow_id)

    wizard_token = _get_wizard_token(request)
    state = load_modal_state_for(request, wizard_token, project_name)
    if not state or state.flow_id != flow_id:
        logger.warning("Modal wizard session lost for %s/%s (step=%s)", project_name, flow_id, section_id)
        raise HTTPException(status_code=400, detail=_SESSION_EXPIRED)

    flow = get_flow(flow_id, **_flow_context_from_state(state, flow_id))
    section = _get_section_from_flow(flow, section_id)

    # Needs the htmx json-enc extension on the client.
    content_type = request.headers.get("content-type", "")
    if "application/json" not in content_type:
        raise HTTPException(
            status_code=400,
            detail="Verwacht JSON body (json-enc extensie niet geladen?)",
        )
    body = await request.json()
    body.pop("_wizard_token", None)

    seq_action = body.pop("_seq_action", None)
    seq_path = body.pop("_seq_path", None)
    seq_index = body.pop("_seq_index", None)
    is_rerender = bool(body.pop("_rerender", None))
    goto = body.pop("_goto", None)

    if seq_action in ("add", "remove"):
        yaml_data = state.get_merged_data()
        processor = EditableFormProcessor()
        padded_body = _pad_sparse_submission(body, flow, section_id)
        merged, _err = await processor.process_json_submission(
            padded_body,
            section.editables,
            yaml_data,
            edit_mode=True,
        )

        # The rendered add/remove path is virtualized (e.g. _services-config{attachments}),
        # but items are stored under (and read back from) the REAL path. Devirtualize so
        # the new row lands where the renderer reads it instead of a dead virtual key.
        editable = _find_sequence_editable(section, seq_path) if seq_path else None
        data_path = seq_path
        if editable is not None and getattr(editable.editable, "virtualize", None):
            from opi.forms.editables.editable import reverse_virtualize

            data_path = reverse_virtualize(seq_path, editable.editable.virtualize)

        items = smart_get_value(merged, data_path) if data_path else []
        if not isinstance(items, list):
            items = []

        if seq_action == "add":
            items.append(_empty_sequence_item(editable, merged))
        elif seq_action == "remove":
            remove_index = int(seq_index) if seq_index not in (None, "") else -1
            if 0 <= remove_index < len(items):
                items.pop(remove_index)

        smart_set_value(merged, str(data_path), items)
        step_html = _render_section_html(section, merged, locked_services=None)
        rendered = _render_modal_step(request, wizard_token, state, flow_id, section, step_html, project_name)
        return HTMLResponse(content=rendered)

    submitted_data = _pad_sparse_submission(body, flow, section_id)

    # Re-render only, for a preview update: process the submission to get the merged data but
    # skip validation, so a newly visible field with a default shows no spurious "required".
    if is_rerender:
        processor = EditableFormProcessor()
        yaml_data = state.get_merged_data()
        submitted_yaml, _errors = await processor.process_json_submission(
            submitted_data,
            section.editables,
            yaml_data,
            edit_mode=True,
        )
        # Drop values for editables whose ``depends_on`` no longer holds: when the user
        # deselects a service, its still-rendered config inputs come along in the POST body,
        # and without this they are written back and the smart-set silently re-adds the service
        # to the selection list. The create wizard does the same in
        # ``router_wizard.submit_step``.
        processor.clear_hidden_depends_on(section.editables, submitted_yaml)
        save_modal_state_by_token(wizard_token, state)
        step_html = _render_section_html(section, submitted_yaml, locked_services=None)
        rendered = _render_modal_step(request, wizard_token, state, flow_id, section, step_html, project_name)
        return HTMLResponse(content=rendered)

    # The warnings of this step, which travel to the next one: they do not block navigation,
    # and they must not vanish as soon as you click on.
    section_warnings: dict[str, list[str]] = {}

    # Backup and restore sections have no editables, so the raw form data is stored directly.
    if _is_backup_restore_flow(flow_id) and not section.editables:
        state.store_step_data(section_id, submitted_data)
        state.mark_completed(section_id)
    else:
        processor = EditableFormProcessor()
        yaml_data = state.get_merged_data()

        # The enforcer context out of base_data, such as the existing names for uniqueness.
        enforcer_ctx: dict[str, Any] = {"project_name": project_name}
        if state.base_data and "existing_deployment_names" in state.base_data:
            enforcer_ctx["existing_deployment_names"] = state.base_data["existing_deployment_names"]
        if state.base_data and "existing_component_names" in state.base_data:
            enforcer_ctx["existing_component_names"] = state.base_data["existing_component_names"]

        submitted_yaml, errors = await processor.process_json_submission(
            submitted_data,
            section.editables,
            yaml_data,
            edit_mode=True,
            enforcer_context=enforcer_ctx,
        )

        # Drop now-hidden dependent values, so stale config does not re-add a deselected
        # service through ``smart_set_value`` on the next merge.
        processor.clear_hidden_depends_on(section.editables, submitted_yaml)

        # Reconcile the service selection with the base; see apply_services_mutation. This hung
        # on the section name "services-edit", so every other flow with a service list got no
        # reconciliation.
        apply_services_mutation(section.editables, yaml_data, submitted_yaml)
        apply_component_services_mutation(section.editables, yaml_data, submitted_yaml)

        # The section-level enforcer, for cross-field validation. Capture the warnings too:
        # without a field_warnings dict a FieldWarning is swallowed silently, invisible to the
        # user AND to the logs, which makes a stuck wizard impossible to diagnose.
        section_global_errors: list[str] = []
        if not errors and section.enforcer:
            section_global_errors = await processor.enforce_sections(
                submitted_yaml,
                [section],
                enforcer_context=enforcer_ctx,
                field_errors=errors,
                field_warnings=section_warnings,
            )

        # ONLY errors hold the step back. A warning informs, and it travels to the next step,
        # because that is where something can be done about it.
        if errors or section_global_errors:
            section_global_errors = section_global_errors + _create_renderer().take_unrendered_errors(
                section.editables, submitted_yaml, errors, edit_mode=True
            )
            # Log why the step did not advance, so it is diagnosable from Loki and not only
            # from the on-screen message.
            logger.warning(
                "Wizard step %s/%s/%s did not advance: field_errors=%s global_errors=%s warnings=%s",
                project_name,
                flow_id,
                section_id,
                errors,
                section_global_errors,
                section_warnings,
            )
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
                warnings=section_warnings,
            )
            return HTMLResponse(content=rendered)

        section_data = _extract_section_data(section.editables, submitted_yaml)
        state.store_step_data(section_id, section_data)
        state.mark_completed(section_id)

    # Re-resolve the active sections, because services can add or remove conditional steps.
    # A single-section modal flow bypasses resolution, exactly as modal_wizard_init does: a
    # service-config section's ``visible`` lambda reads the real ``services`` list, while the
    # modal's step_data only carries the virtual key, so resolution would deem the section
    # inactive and stash_inactive_sections would drop the data just stored, reverting the whole
    # save. The edit button already guaranteed visibility.
    if len(flow.sections) == 1:
        active_section_ids = [flow.sections[0].section_id]
    else:
        active_section_ids = resolve_active_section_ids(flow, state.step_data)
    state.active_sections = active_section_ids
    state.stash_inactive_sections(active_section_ids)

    active_sections = resolve_active_sections(flow, state.step_data)
    section_ids = [s.section_id for s in active_sections]

    # The jump to the summary may skip steps but no required value: validate the whole flow,
    # as the page wizard does in its review branch, and land on the first step that misses
    # something.
    if goto == "review" and flow.show_review:
        error_response = await _validate_modal_flow_for_review(
            request, wizard_token, state, flow_id, project_name, active_sections
        )
        if error_response is not None:
            return error_response
        for validated in active_sections:
            state.mark_completed(validated.section_id)
        save_modal_state_by_token(wizard_token, state)
        return _render_modal_review(
            request,
            wizard_token,
            project_name,
            flow_id,
            active_sections,
            state,
            field_warnings=section_warnings or None,
        )

    try:
        current_idx = section_ids.index(section_id)
    except ValueError:
        current_idx = -1

    if current_idx < len(active_sections) - 1:
        # More steps to go
        next_section = active_sections[current_idx + 1]
        state.current_step = next_section.section_id

        if next_section.section_id == "restore-target":
            _enrich_restore_target_context(state)

        # Default the restore's config source to the deployment the backup came from.
        if flow_id == "modal-restore" and next_section.section_id.startswith("add-deployment-info-"):
            _seed_restore_config_source(state, next_section.section_id)

        # Pre-fill the components when navigating from the info step to the components step.
        if section_id.startswith("add-deployment-info-") and next_section.section_id.startswith(
            "add-deployment-components-"
        ):
            suffix = section_id.removeprefix("add-deployment-info-")
            if suffix.isdigit():
                _seed_components_for_new_deployment(state, int(suffix))

        save_modal_state_by_token(wizard_token, state)

        yaml_data = state.get_merged_data()

        step_html = _render_section_html(next_section, yaml_data, locked_services=None)
        rendered = _render_modal_step(
            request,
            wizard_token,
            state,
            flow_id,
            next_section,
            step_html,
            project_name,
            warnings=section_warnings or None,
        )
        return HTMLResponse(content=rendered)

    # All steps completed, so the review when the flow asks for one. The wording is an
    # anchor for "tests/test_wizard_waarschuwing_op_de_laatste_stap.py".
    if flow.show_review:
        save_modal_state_by_token(wizard_token, state)
        return _render_modal_review(
            request,
            wizard_token,
            project_name,
            flow_id,
            active_sections,
            state,
            field_warnings=section_warnings or None,
        )

    save_modal_state_by_token(wizard_token, state)
    return await _modal_do_submit(request, wizard_token, project_name, flow_id)


@detail_edit_router.post("/{project_name}/modal-wizard/{flow_id}/skip", response_class=HTMLResponse)
@action(Action.PROJECT_READ)
@requires_sso
async def modal_wizard_skip(request: Request, project_name: str, flow_id: str) -> HTMLResponse:
    """'Later configureren' - save accumulated data and trigger deployment."""
    _check_flow_access(request, project_name, flow_id)

    wizard_token = await _get_wizard_token_with_body(request)
    state = load_modal_state_for(request, wizard_token, project_name)
    if not state or state.flow_id != flow_id:
        logger.warning("Modal wizard session lost for %s/%s (skip)", project_name, flow_id)
        raise HTTPException(status_code=400, detail=_SESSION_EXPIRED)

    return await _modal_do_submit(request, wizard_token, project_name, flow_id)


@detail_edit_router.post("/{project_name}/modal-wizard/{flow_id}/confirm", response_class=HTMLResponse)
@action(Action.PROJECT_READ)
@requires_sso
async def modal_wizard_confirm(request: Request, project_name: str, flow_id: str) -> HTMLResponse:
    """Confirm after review - execute the final submit."""
    _check_flow_access(request, project_name, flow_id)

    wizard_token = await _get_wizard_token_with_body(request)
    state = load_modal_state_for(request, wizard_token, project_name)
    if not state or state.flow_id != flow_id:
        logger.warning("Modal wizard session lost for %s/%s (confirm)", project_name, flow_id)
        raise HTTPException(status_code=400, detail=_SESSION_EXPIRED)

    return await _modal_do_submit(request, wizard_token, project_name, flow_id)


@detail_edit_router.get(
    "/{project_name}/modal-wizard/modal-backup/select-deployment",
    response_class=HTMLResponse,
)
@action(Action.PROJECT_READ)
@requires_sso
async def backup_select_deployment(request: Request, project_name: str) -> HTMLResponse:
    """HTMX endpoint: re-render the backup step partial when deployment changes."""
    require_action(request, Action.BACKUP_CREATE, project_name)

    wizard_token = _get_wizard_token(request)
    state = load_modal_state_for(request, wizard_token, project_name)
    if not state or state.flow_id != "modal-backup":
        raise HTTPException(status_code=400, detail=_SESSION_EXPIRED)

    selected = request.query_params.get("deployment_name", "")
    if state.base_data:
        state.base_data["_selected_deployment"] = selected
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
@action(Action.PROJECT_READ)
@requires_sso
async def restore_select_mode(request: Request, project_name: str) -> HTMLResponse:
    """HTMX endpoint: re-render the restore target step when mode changes."""
    require_on_any_deployment(request, Action.RESTORE_RUN, project_name)

    wizard_token = _get_wizard_token(request)
    state = load_modal_state_for(request, wizard_token, project_name)
    if not state or state.flow_id != "modal-restore":
        raise HTTPException(status_code=400, detail=_SESSION_EXPIRED)

    restore_mode = request.query_params.get("restore_mode", "existing")
    if state.base_data:
        state.base_data["_restore_mode"] = restore_mode

    _enrich_restore_target_context(state)
    save_modal_state_by_token(wizard_token, state)

    flow = get_flow("modal-restore")
    section = _get_section_from_flow(flow, "restore-target")
    yaml_data = state.get_merged_data()
    step_html = _render_section_html(section, yaml_data, locked_services=None)

    rendered = _render_modal_step(request, wizard_token, state, "modal-restore", section, step_html, project_name)
    return HTMLResponse(content=rendered)


def _attachment_review_items(yaml_data: dict, state) -> list[str]:
    """List committed + staged attachments for the review summary.

    The attachments section is a TemplatePartial whose staged uploads live in the
    wizard session (not yet in the YAML), so _build_section_fields finds nothing.
    Surface both here so the user sees the upload they just made before saving.

    A staged replacement carries an id that is already in the catalog, and it is what the save
    is going to write, so it takes the place of the stored line for that id.
    """
    staged = getattr(state, "staged_attachments", None) or {}
    items: list[str] = []
    seen: set[str] = set()
    for service in yaml_data.get("services", []):
        if isinstance(service, dict) and isinstance(service.get("attachments"), dict):
            for entry in service["attachments"].get("data", []) or []:
                if isinstance(entry, dict) and entry.get("id"):
                    replacement = staged.get(entry["id"]) if staged.get(entry["id"], {}).get("replace") else None
                    if replacement is not None:
                        items.append(f"{replacement.get('filename', entry['id'])} ({entry['id']}, vervangen)")
                    else:
                        items.append(f"{entry.get('filename', entry['id'])} ({entry['id']})")
                    seen.add(entry["id"])
    for att_id, info in staged.items():
        if att_id not in seen:
            items.append(f"{info.get('filename', att_id)} ({att_id})")
    return items


def _render_modal_review(
    request: Request,
    wizard_token: str | None,
    project_name: str,
    flow_id: str,
    active_sections,
    state,
    global_errors: list[str] | None = None,
    field_warnings: dict[str, list[str]] | None = None,
) -> HTMLResponse:
    """Render the review/confirmation page for the modal wizard.

    ``field_warnings`` carries the warnings raised by the step that was just submitted. A
    warning informs and does not block, so on an intermediate step it travels to the next step;
    the LAST step has no next step, which is why they are rendered here, on the screen where
    the user decides to confirm.
    """
    from opi.web.router_wizard import _build_section_fields

    yaml_data = state.get_merged_data()

    section_summaries = []
    for section in active_sections:
        fields = _build_section_fields(section, yaml_data)
        if section.section_id == "attachments":
            att_items = _attachment_review_items(yaml_data, state)
            if att_items:
                fields = [*fields, {"label": "Bijlagen", "is_list": True, "value": att_items}]
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

    # Warnings from the final step, which has no next step to carry them to.
    for messages in (field_warnings or {}).values():
        warnings.extend(messages)

    # A restore can break the running application.
    if flow_id == "modal-restore":
        warnings.append(
            "Het herstellen van een backup overschrijft de huidige data. "
            "Dit kan ertoe leiden dat de applicatie tijdelijk niet beschikbaar is "
            "of niet meer correct werkt."
        )

    # Removed services get a warning on the review screen; only a flow that edits services
    # can have any.
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

    rendered = render_fragment(
        request,
        template="bg/_modal-wizard-review.html.j2",
        context={
            "request": request,
            "steps": steps,
            "flow_id": flow_id,
            "project_name": project_name,
            "wizard_token": wizard_token,
            "section_summaries": section_summaries,
            "action_label": "Bevestigen en verwerken",
            "warnings": warnings,
            "global_errors": global_errors or [],
            "nldd_icon": to_nldd_icon,
        },
    )
    return HTMLResponse(content=rendered)


async def _modal_do_submit(
    request: Request,
    wizard_token: str | None,
    project_name: str,
    flow_id: str,
) -> HTMLResponse:
    """Execute the final modal wizard submission."""

    # TOCTOU recheck on the mutating request, with the gate for this flow: the only weighing
    # of the flow action that records an audit row.
    _authorize_flow(request, project_name, flow_id)

    state = load_modal_state_for(request, wizard_token, project_name)
    if not state:
        raise HTTPException(status_code=400, detail=_SESSION_EXPIRED)

    flow = get_flow(flow_id, **_flow_context_from_state(state, flow_id))

    # For a single-section flow the visibility lambda may not have the full context, and the
    # edit button already guards visibility.
    active_sections = flow.sections if len(flow.sections) == 1 else resolve_active_sections(flow, state.step_data)

    action = _determine_flow_action(flow, active_sections)

    # Backup and restore flows do not touch the project file.
    if action in ("trigger_backup", "trigger_restore"):
        return await _handle_backup_restore_submit(request, wizard_token, project_name, flow_id, action, state)

    # Keep the CLEARED_FIELD tombstones, so the merge into the stored project, which cannot
    # express a deleted key by absence, can honour cleared fields; ``apply_modal_edit`` removes
    # the tombstones before the result is saved.
    merged_data = state.get_merged_data(strip_cleared=False)

    project = get_project_store().get(project_name)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' niet gevonden")

    # Read fresh from Git and not from the cache, so the form merges onto the current state
    # and a lagging cache is never committed back over newer Git data.
    from opi.manager.project_manager import ProjectManager

    # Closed explicitly, so its temp git clone is cleaned up on every exit path.
    project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")
    try:
        existing_data, render_response = await _process_and_save_modal_edit(
            request,
            project_manager,
            project_name,
            flow,
            wizard_token,
            merged_data,
            active_sections,
            state,
        )
    finally:
        await project_manager.close()

    if render_response is not None:
        return render_response
    logger.info("Project %s updated via modal wizard (flow=%s)", project_name, flow_id)

    if action == "process_project":
        # A flow that declares a deployment target deploys only that deployment.
        target_deployment_name = _targeted_deployment_name(flow, existing_data)
        task_id = await _start_deployment(
            request,
            project_name,
            existing_data,
            deployment_name=target_deployment_name,
        )
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

        rendered = render_fragment(
            request,
            template="bg/_modal-wizard-progress.html.j2",
            context={"task_id": task_id, "project_name": project_name},
        )

        clear_modal_state_by_token(wizard_token)
        return HTMLResponse(content=rendered)

    clear_modal_state_by_token(wizard_token)
    rendered = render_fragment(
        request,
        template="bg/_modal-wizard-success.html.j2",
        context={},
    )

    for section in active_sections:
        if section.after_save:
            try:
                await section.after_save(request)
            except Exception:
                logger.exception("after_save hook failed for section %s", section.section_id)

    return HTMLResponse(content=rendered)


async def _process_and_save_modal_edit(
    request: Request,
    project_manager: ProjectManager,
    project_name: str,
    flow: FormFlow,
    wizard_token: str | None,
    merged_data: dict,
    active_sections,
    state,
) -> tuple[dict, HTMLResponse | None]:
    """Read, merge the modal-edit form into the project, and persist it.

    Returns ``(existing_data, render_response)``. ``render_response`` is non-None only when
    validation rejected the save and the caller should return it, the review re-render. The
    caller owns the ProjectManager and closes it once this returns.
    """
    existing_data = await project_manager.get_contents()

    # The OTAP markings from before this edit. Here and not in the store: only the request
    # knows WHO makes the change, and that is what such a notice has to name.
    environment_before = environment_markings(existing_data)

    # The state from before the merge, to read a removal or a downgrade off.
    before_data = copy.deepcopy(existing_data)

    # Capture existing attachments' encrypted content before the form merge: the wizard
    # strips it from the session (see _strip_attachment_content), so it is re-attached at
    # save by PreserveAttachmentContentHook keyed by id.
    original_attachment_content = {
        att_id: entry.get("content")
        for att_id, entry in extract_attachment_catalog(existing_data).items()
        if isinstance(entry, dict) and entry.get("content")
    }

    # The merge belongs in the same row as the save below: it too can REFUSE the edit, when
    # the target of an index flow no longer points at the same deployment. The refusal carries
    # the explanation for the user, so it must not fall through as a bare 500.
    try:
        existing_data = await apply_modal_edit(
            existing_data,
            merged_data,
            flow=flow,
            active_sections=active_sections,
            state=state,
            project_name=project_name,
            original_attachment_content=original_attachment_content,
        )
    except ProjectIntegrityError as e:
        logger.warning("Modal wizard merge rejected for %s (flow=%s): %s", project_name, flow.flow_id, e)
        return existing_data, _render_modal_review(
            request, wizard_token, project_name, flow.flow_id, active_sections, state, global_errors=[str(e)]
        )

    # A state that asks for an approval records its request here, as the edit wizard does. On
    # the save path and not in the merge, which must let an empty edit change nothing.
    ensure_approval_requests(existing_data)

    # The marking is not an ordinary edit of the project file: it decides what "production"
    # is, and with that what every other rule enforces. Which flow the user opened for it does
    # not matter, so the gate sits on the WRITE PATH and not on the route; ``environment`` can
    # be set in a restore flow too. After the merge, because only then is there an AFTER state
    # to read the downgrade or the removal off, and before the save, so a refusal leaves
    # nothing behind.
    if production_marking_change(environment_before, environment_markings(existing_data)):
        authorize(request, action=Action.ENVIRONMENT_SET, project_name=project_name)

    # A clone-from reads the database and the buckets of the SOURCE, so that source weighs
    # separately. On the write path and not on the route, for the same reason as the marking
    # above: more than one flow can set a clone-from. The same gate as on the upsert.
    require_clone_source_access(request, project_name, before_data, existing_data)

    handeling = save_requires_step_up(before_data, existing_data)
    if handeling is not None and not has_fresh_step_up(request):
        log_step_up_refusal(
            handeling,
            project_name=project_name,
            actor=str((get_current_user(request) or {}).get("email", "(onbekend)")),
        )
        # The repeat goes to /confirm and not to the path of this request: a flow without a
        # review arrives here through the step route, which demands a JSON body the bodyless
        # repeat does not have. /confirm is the tail of both roads and reads everything from
        # the session state at the wizard token.
        vervolg = f"/projects/{project_name}/modal-wizard/{flow.flow_id}/confirm"
        if wizard_token:
            vervolg = f"{vervolg}?_wizard_token={quote(wizard_token)}"
        dialog = await render_step_up_dialog(request, vervolg=vervolg, handeling=handeling, vervolg_token=wizard_token)
        return existing_data, step_up_dialog_response(dialog)

    try:
        await project_manager.save_and_commit_project(existing_data, f"Update {project_name} ({flow.flow_id})")
    except (ProjectSchemaError, ProjectIntegrityError, ConflictError) as e:
        logger.warning(
            "Modal wizard save rejected for %s (flow=%s, %s): %s",
            project_name,
            flow.flow_id,
            type(e).__name__,
            e,
        )
        return existing_data, _render_modal_review(
            request, wizard_token, project_name, flow.flow_id, active_sections, state, global_errors=[str(e)]
        )

    log_production_downgrades(
        environment_before,
        environment_markings(existing_data),
        project_name=project_name,
        actor=(get_current_user(request) or {}).get("email", "(onbekend)"),
    )
    return existing_data, None


async def _authorize_restore(
    request: Request,
    project_name: str,
    wizard_token: str | None,
    *,
    source_deployment: str,
    target_deployment: str,
    deployment_config: dict[str, Any] | None,
    config_source: str | None,
    backup_run_id: str,
) -> HTMLResponse | None:
    """Weigh the restore on the state it would produce; returns the step-up dialog or None.

    The new deployment is built here with the same builder the restore task uses, on a copy of
    the project: only on that AFTER state can be read what is written, and so what has to be
    weighed.

    Raises:
        HTTPException: 404 when the project does not exist, 400 when the chosen config source
            is not a deployment of this project, 403 when the roles open too little.
    """
    # The source first: the cheapest role weighing, and it does not need the AFTER state.
    require_action(request, Action.RESTORE_RUN, project_name, deployment=source_deployment)

    project = get_project_store().get(project_name)
    if project is None or not project.data:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' niet gevonden")

    before = project.data
    after = before
    if deployment_config is not None and source_deployment:
        after = copy.deepcopy(before)
        try:
            new_deployment = build_restore_target_deployment(
                after,
                target_deployment=target_deployment,
                backup_source=source_deployment,
                backup_run_id=backup_run_id,
                config_source=config_source,
                wizard_config=deployment_config,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        after.setdefault("deployments", []).append(new_deployment)

    # RESTORE_RUN weighs on the source AND the target and not on the highest marking of the
    # project: inside a project with production, a developer may restore between two
    # non-production deployments. The target weighs on the AFTER state, because a new
    # deployment does not have its marking in the store yet, and it carries the audit row.
    authorize(
        request,
        action=Action.RESTORE_RUN,
        project_name=project_name,
        deployment=target_deployment,
        project=project.model_copy(update={"data": after}),
    )

    # The clone-from of the new deployment reads the data of the source, so that source is
    # weighed separately, with the same gate as on the modal save and on the upsert. The config
    # source goes in on its own: only the BACKUP source lands in ``clone-from``, while the
    # images and the env vars of the config source travel to the target. Without this weighing,
    # a developer takes the env vars of production into their own development deployment.
    require_clone_source_access(
        request, project_name, before, after, extra_sources=[config_source] if config_source else []
    )

    # The marking is not an ordinary edit: it decides what "production" is, and with that
    # what every other rule enforces. The same gate as on the modal save path.
    if production_marking_change(environment_markings(before), environment_markings(after)):
        authorize(request, action=Action.ENVIRONMENT_SET, project_name=project_name)

    handeling = save_requires_step_up(before, after)
    if handeling is None or has_fresh_step_up(request):
        return None

    log_step_up_refusal(
        handeling,
        project_name=project_name,
        actor=str((get_current_user(request) or {}).get("email", "(onbekend)")),
    )
    vervolg = f"/projects/{project_name}/modal-wizard/modal-restore/confirm"
    if wizard_token:
        vervolg = f"{vervolg}?_wizard_token={quote(wizard_token)}"
    dialog = await render_step_up_dialog(request, vervolg=vervolg, handeling=handeling, vervolg_token=wizard_token)
    return step_up_dialog_response(dialog)


async def _handle_backup_restore_submit(
    request: Request,
    wizard_token: str | None,
    project_name: str,
    flow_id: str,
    action: str,
    state,
) -> HTMLResponse:
    """Handle backup/restore wizard submission via the async task queue."""
    from opi.core.config import settings
    from opi.http.task_helpers import get_task_service
    from opi.services.tasks.async_task_service import TaskType

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
        create_new_deployment = restore_mode == RestoreMode.NEW.value

        # The chosen run decides the source: the deployment the backup came from.
        source_deployment = ""
        backup_items: list[Any] = []
        if state.base_data:
            for run in state.base_data.get("_backup_runs", []):
                if run.get("backup_run_id") == backup_run_id:
                    backup_items = run.get("items", [])
                    source_deployment = run.get("deployment_name", "")
                    break

        deployment_config: dict[str, Any] | None = None
        config_source: str | None = None
        if create_new_deployment:
            # The index of the new deployment comes out of the section id.
            deployments = merged_data.get("deployments", [])
            new_dep_idx = _extract_deployment_index_from_sections(state)
            if new_dep_idx is not None and new_dep_idx < len(deployments):
                deployment_config = deployments[new_dep_idx]
            target_deployment = deployment_config.get("name", "") if deployment_config else ""
            config_source = merged_data.get("config_source") or None
        else:
            target_deployment = merged_data.get("target_deployment", "")
        if not target_deployment:
            target_deployment = source_deployment

        step_up = await _authorize_restore(
            request,
            project_name,
            wizard_token,
            source_deployment=source_deployment,
            target_deployment=target_deployment,
            deployment_config=deployment_config,
            config_source=config_source,
            backup_run_id=backup_run_id,
        )
        if step_up is not None:
            return step_up

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
                "config_source": config_source,
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

    # Rendered once on purpose; see render_progress_fragment in opi/web/task_progress.py.
    rendered = render_fragment(
        request,
        template="bg/_modal-wizard-progress.html.j2",
        context={"task_id": task_id, "project_name": project_name},
    )

    clear_modal_state_by_token(wizard_token)
    return HTMLResponse(content=rendered)


# PORTAL_READ for the same reason as the task-progress route in opi/web/router.py.
@detail_edit_router.get("/{project_name}/modal-wizard/progress/{task_id}", response_class=HTMLResponse)
@action(Action.PORTAL_READ)
@requires_sso
async def modal_wizard_progress_html(request: Request, project_name: str, task_id: str) -> HTMLResponse:
    """Return server-rendered progress fragment for HTMX polling.

    Reads task state from the V2 async task service (database-backed). The task id
    alone is not enough: the task has to be this project's and this user's to follow,
    the same gate the task-progress fragment uses.
    """
    from opi.http.task_helpers import get_task_service
    from opi.web.router import _require_task_of_project, _v2_task_to_template_context

    task_service = get_task_service(request)
    # The same screening as the other progress fragments: the task has to be this project's,
    # and the reader has to have access to it or have started it.
    task = await _require_task_of_project(request, task_service, project_name, task_id)

    if task is None:
        return HTMLResponse(content="<p>Taak niet gevonden</p>", status_code=404)

    context: dict[str, Any] = _v2_task_to_template_context(task, project_name)
    context["task_id"] = task_id

    # Rendered once on purpose. The fragment is a template file, so the component extension
    # already replaced its <c-...> tags at compile time; a second pass with
    # ``process_components`` would parse the rendered HTML as Jinja again, and a step or
    # deployment name carrying ``{{ ... }}`` would be executed instead of shown.
    return HTMLResponse(content=_progress_fragment(request, context))


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
    from opi.services import ServiceAdapter
    from opi.services.project_file_handler import create_project_file_handler

    current_cluster = settings.CLUSTER_MANAGER
    project_file_handler = create_project_file_handler()
    backupable_labels = ServiceAdapter.get_backupable_labels()
    context: dict[str, Any] = {
        "_current_cluster": current_cluster,
        "_project_name": project_name,
        "_backupable_labels": backupable_labels,
    }

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

        # Only deployments that have something to back up.
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
    from opi.manager.backup import BackupManager, group_snapshots_into_runs

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
                backup_runs_map.update(group_snapshots_into_runs(snapshots, dep_name))
            except Exception as e:
                logger.warning("Failed to fetch backups for deployment %s: %s", dep_name, e)

    except Exception:
        logger.warning("Failed to gather backup runs for %s", project_name)

    backup_runs = sorted(backup_runs_map.values(), key=lambda r: r.get("timestamp", ""), reverse=True)
    return backup_runs


def _enrich_restore_target_context(state) -> None:
    """Derive _source_deployment from the selected backup_run_id in step data.

    Called before rendering the restore-target step so the template knows
    which deployment the backup originated from.
    """
    if not state.base_data:
        return

    step1_data = state.step_data.get("restore-select", {})
    backup_run_id = step1_data.get("backup_run_id", "")
    if not backup_run_id:
        return

    for run in state.base_data.get("_backup_runs", []):
        if run.get("backup_run_id") == backup_run_id:
            state.base_data["_source_deployment"] = run.get("deployment_name", "")
            break


def _get_section_from_flow(flow, section_id: str):
    """Look up a section by ID within a flow."""
    for section in flow.sections:
        if section.section_id == section_id:
            return section
    raise HTTPException(status_code=404, detail=f"Stap '{section_id}' niet gevonden in flow '{flow.flow_id}'")
