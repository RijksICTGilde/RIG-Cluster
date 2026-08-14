"""Detail page inline editing via the editables system.

Provides GET/POST endpoints for editing project sections from the
details page modal.  Uses a server-side wizard engine (WizardState)
to drive multi-step edit flows within the modal.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from opi.core.auth_decorators import get_current_user, requires_sso
from opi.core.backup_constants import DEFAULT_BACKUP_RESOURCE_TYPES
from opi.core.project_schema import ProjectIntegrityError, ProjectSchemaError
from opi.core.templates_lotc import templates_lotc
from opi.forms import FormRenderer, get_default_nl_translator
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.editables.service_path import smart_get_value, smart_set_value
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
from opi.forms.wizard.mutation import apply_services_mutation
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
from opi.forms.wizard.session import (
    clear_modal_state_by_token,
    get_modal_state_by_token,
    init_modal_state_tokenized,
    save_modal_state_by_token,
)
from opi.handlers.project_file_handler import extract_attachment_catalog
from opi.services.catalog.cross_domain_access.context import build_cross_domain_context
from opi.services.project_authorization import (
    is_user_authorized_for_project,
)
from opi.services.project_store import ConflictError, get_project_store
from opi.utils.csrf import reject_misfired_form_get
from opi.web.lotc_switch import render_fragment
from opi.web.navigation_lotc import to_nldd_icon
from opi.web.project_edit_security import require_project_edit_access
from opi.web.router_wizard import (
    _empty_sequence_item,
    _extract_section_data,
    _find_sequence_editable,
    _split_data_across_sections,
)
from opi.web.router_wizard_attachments import REPLACE_TARGET_KEY

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


def _create_renderer() -> FormRenderer:
    """De formulierrenderer voor dit verzoek.

    De VOORBEREIDING per veldtype zit in de adapter en is bedrijfslogica - welke opties,
    welke waarde, hoe een reeks wordt opgebouwd. Wat het componentensysteem bepaalt zijn
    de sjablonen die het veld renderen. Precies zoals in ``opi/web/router_wizard.py``.
    """
    return FormRenderer(
        widget_adapter=LOTCWidgetAdapter(),
        translator=get_default_nl_translator(),
    )


def _progress_fragment(request: Request, context: dict[str, Any]) -> str:
    """Het voortgangsfragment van de dialoog, in de weergave die dit verzoek koos.

    EEN render, aan beide kanten. Er gaat met opzet geen ``process_components`` overheen:
    het fragment is een sjabloonbestand, dus zijn componenttags zijn al bij het compileren
    vervangen. Een tweede slag zou de gerenderde HTML nog eens als Jinja lezen, en een
    stapnaam of subtaaknaam met ``{{ ... }}`` erin zou dan uitgevoerd worden in plaats van
    getoond. Zie ``render_progress_fragment`` in ``opi/web/task_progress.py``.
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
    """Render form fields for a section.

    If the section has a ``guard`` and it returns a message, that message
    is rendered as an info alert instead of the form fields.

    Args:
        locked_services: Service names that should be visually marked as existing.
            Passed via ``_locked_services`` key in yaml_data so ``render_service_cards`` can
            indicate them. No longer prevents unchecking.

    De adapter rendert meteen af, dus deze string mag NIET nog een keer door een
    sjabloonrender: hij draagt wat iemand in het formulier heeft getypt, en dat hoort geen
    Jinja te worden. Zie ``opi/web/router_user_admin.py``, waar dezelfde keuze staat.
    """
    # Check guard before rendering fields
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


def _require_project_member_access(request: Request, project_name: str):
    """Check auth for project member access (any role). Returns (project, user_email)."""

    user = get_current_user(request)
    project = get_project_store().get(project_name)

    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' niet gevonden")

    user_email = user.get("email", "").lower()
    if not is_user_authorized_for_project(project_name, user_email):
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

    from opi.handlers.project_file_handler import find_attachment_data_list

    data = copy.deepcopy(project_data)
    for att in find_attachment_data_list(data.get("services")) or []:
        if isinstance(att, dict):
            att.pop("content", None)
    return data


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
        # Onze secties dragen Nederlandse ROOS-iconnamen; de LOTC-sjablonen hebben de
        # NLDD-woordenschat nodig. Het roos-sjabloon raakt dit niet aan.
        "nldd_icon": to_nldd_icon,
    }
    return render_fragment(
        request,
        template="bg/_modal-wizard-step.html.j2",
        context=context,
    )


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
    base_version: str | None = None,
) -> str:
    """Create a V2 async task for deployment processing. Returns task_id.

    ``base_version`` is the project-file version the wizard was seeded from. The task
    writes the whole file, so without it a change that landed while the user was
    editing is silently overwritten; with it the store merges the two.
    """
    from opi.core.task_helpers import create_async_task
    from opi.utils.yaml_util import dump_yaml_to_string

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
            "base_version": base_version,
        },
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
                    items.append(_empty_sequence_item(COMPONENTS_SEQUENCE))
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

    flow = get_flow(flow_id, **flow_context)

    # Populate transient fields for deferred editables (e.g. custom domain text input)
    processor = EditableFormProcessor()
    for section in flow.sections:
        processor.populate_deferred_fields(project_data, section.editables)

    # Pre-fill step data from existing project. Strip attachment content first: only
    # id/filename are needed to display the catalog, and carrying the encrypted blocks
    # bloats the disk-backed session. The content is re-attached from the stored project
    # at save (PreserveAttachmentContentHook).
    #
    # Then drop every other encrypted value this flow cannot edit, so the disk-backed
    # session stops carrying (and writing back) secrets no step touches. Derived from the
    # flow's own editables rather than a field list -- see opi.forms.wizard.secrets.
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

    step_data = _split_data_across_sections(flow, session_data)

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

    # Record which version of the project file this form is showing. It travels with
    # the eventual save so the edit is applied as a change on top of that version --
    # otherwise the full file written back at the end silently reverts anything that
    # landed while the user was in the wizard.
    state.base_version = await get_project_store().version_of(f"projects/{project_name}.yaml")

    # The full project data is the domain object — providers and converters
    # use it to resolve context (e.g. deployment name from path, component
    # services, etc.).  Step data merges on top during get_merged_data().
    #
    # Editables whose yaml_path points *directly* at a top-level list (e.g.
    # USERS_SEQUENCE at "users", COMPONENTS_SEQUENCE at "components") fully
    # own that list — step_data already carries the complete value. Keeping
    # a copy in base_data would cause get_merged_data's merge-by-index
    # to silently retain items the user removed in the UI.
    owned = _fully_owned_list_keys(flow)
    # ``session_data``, not ``project_data``: base_data is persisted to disk like
    # step_data is, so it needs the same attachment strip and secret redaction. It read
    # from the raw project before, which left the encrypted blocks in the session even
    # though step_data had been stripped of them.
    state.base_data = {k: v for k, v in session_data.items() if k not in owned}
    state.base_data["_wizard_token"] = wizard_token

    # Remember that this was an add: the flow is rebuilt from its id on later
    # requests, and only the session knows the item did not exist yet.
    if flow_context.get("is_new"):
        state.base_data["is_new"] = True
        existing_components = (project.data or {}).get("components", [])
        state.base_data["existing_component_names"] = [
            c.get("name") for c in existing_components if isinstance(c, dict) and c.get("name")
        ]

    # Add-deployment and restore flows need existing names for uniqueness validation
    if (indexed is not None and indexed[0].appends_new_item) or flow_id == "modal-restore":
        existing_deployments = (project.data or {}).get("deployments", [])
        existing_names = [d.get("name") for d in existing_deployments if isinstance(d, dict) and d.get("name")]
        state.base_data["existing_deployment_names"] = existing_names
        state.base_data["_original_deployment_names"] = existing_names

    # Inject backup/restore context (cluster deployments) for manual backup/restore flows
    if _is_backup_restore_flow(flow_id):
        backup_context = await _build_backup_restore_context_async(flow_id, project_name, project_data)
        # Preselect the deployment the caller is viewing (URL hash), not deployments[0]
        requested_dep = request.query_params.get("deployment", "").strip()
        if requested_dep:
            cluster_deps = backup_context.get("_cluster_deployments", [])
            if any(d.get("name") == requested_dep for d in cluster_deps):
                backup_context["_selected_deployment"] = requested_dep
        state.base_data.update(backup_context)

    # Vervangen: the dialog was opened at one specific attachment, and from here on that is
    # the session's business rather than the request's. The upload endpoints check every
    # id they are handed against this value, so a form that shows the id fixed and a POST
    # that sends a different one get the same answer. Same shape as the two above:
    # template-only, no editable names it, so it stays out of the saved project.
    requested_replace = request.query_params.get("replace", "").strip()
    if requested_replace:
        if flow_id != "modal-edit-attachments" or requested_replace not in extract_attachment_catalog(project_data):
            raise HTTPException(status_code=404, detail=f"Bijlage '{requested_replace}' bestaat niet in dit project")
        state.base_data[REPLACE_TARGET_KEY] = requested_replace

    # Cross-domain-access needs the list of authorized peer projects; the same builder the
    # create wizard uses, so the two flows cannot drift apart again. Populated only for flows
    # that carry its section. Template-only: no editable names it, so it falls outside the
    # write set and never reaches the saved project.
    if flow_id in ("modal-edit-cross-domain-config", "modal-edit-services"):
        state.base_data.update(build_cross_domain_context(project_name, _user_email))

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
            items.append(_empty_sequence_item(editable))
        elif seq_action == "remove":
            remove_index = int(seq_index) if seq_index not in (None, "") else -1
            if 0 <= remove_index < len(items):
                items.pop(remove_index)

        smart_set_value(merged, str(data_path), items)
        step_html = _render_section_html(section, merged, locked_services=None)
        rendered = _render_modal_step(request, wizard_token, state, flow_id, section, step_html, project_name)
        return HTMLResponse(content=rendered)

    submitted_data = _pad_sparse_submission(body, flow, section_id)

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

        # Build enforcer context from base_data (e.g. existing names for uniqueness)
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

        # Drop now-hidden dependent values so stale config doesn't re-add a
        # deselected service via ``smart_set_value`` on the next merge. See the
        # rerender path above and ``router_wizard.submit_step`` for the same
        # call in the create wizard.
        processor.clear_hidden_depends_on(section.editables, submitted_yaml)

        # Verzoen de dienstselectie met de basis (zie apply_services_mutation). Dit hing aan
        # de sectienaam "services-edit": elke andere flow met een dienstenlijst kreeg geen
        # aanvulling, en dat is dezelfde fout als 94478afb, een laag verderop.
        apply_services_mutation(section.editables, yaml_data, submitted_yaml)

        # Run section-level enforcer (cross-field validation). Capture warnings
        # too: without a field_warnings dict a FieldWarning (e.g. a subdomain that
        # is "op aanvraag") is silently swallowed — invisible to the user AND to
        # the logs, which makes a stuck wizard impossible to diagnose.
        section_global_errors: list[str] = []
        section_warnings: dict[str, list[str]] = {}
        if not errors and section.enforcer:
            section_global_errors = await processor.enforce_sections(
                submitted_yaml,
                [section],
                enforcer_context=enforcer_ctx,
                field_errors=errors,
                field_warnings=section_warnings,
            )

        if errors or section_global_errors or section_warnings:
            # Log why the step did not advance so it is diagnosable from Loki, not
            # just from the (previously missing) on-screen message.
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

        # Store step data
        section_data = _extract_section_data(section.editables, submitted_yaml)
        state.store_step_data(section_id, section_data)
        state.mark_completed(section_id)

    # Re-resolve active sections (services may add/remove conditional steps).
    # Single-section modal flows bypass resolution, exactly as modal_wizard_init does:
    # a service-config section's ``visible`` lambda reads the real ``services`` list, but
    # the modal's step_data only carries the virtual ``_services-config`` key, so resolution
    # would deem the section inactive and stash_inactive_sections would drop the data we just
    # stored -- reverting the whole save. The edit button already guaranteed visibility.
    if len(flow.sections) == 1:
        active_section_ids = [flow.sections[0].section_id]
    else:
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
@requires_sso
async def restore_select_mode(request: Request, project_name: str) -> HTMLResponse:
    """HTMX endpoint: re-render the restore target step when mode changes."""
    _require_project_member_access(request, project_name)

    wizard_token = _get_wizard_token(request)
    state = get_modal_state_by_token(wizard_token)
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

    A staged replacement carries an id that is already in the catalog, and it is what the
    save is going to write. Listing the stored line for that id would show the user the
    file they are on their way to overwrite, so the replacement takes its place.
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
) -> HTMLResponse:
    """Render the review/confirmation page for the modal wizard."""
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

    # Backup/restore flows skip project file modification
    if action in ("trigger_backup", "trigger_restore"):
        return await _handle_backup_restore_submit(request, wizard_token, project_name, flow_id, action, state)

    # Merge all step data. Keep CLEARED_FIELD tombstones (strip_cleared=False)
    # so the merge into the stored project (which cannot express a deleted key
    # by absence) can honor cleared fields; ``apply_modal_edit`` removes the
    # tombstones before the result is saved.
    merged_data = state.get_merged_data(strip_cleared=False)

    # Merge with existing project data (preserve system-managed fields)
    project = get_project_store().get(project_name)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' niet gevonden")

    # Read fresh from Git, not the cache, so the form merges onto current state and a lagging
    # cache is never committed back over newer Git data (the cache/Git timing fix).
    from opi.manager.project_manager import ProjectManager

    # Explicitly close the ProjectManager so its temp git clone is cleaned up on
    # every exit path (success, validation-error re-render, or an unexpected raise).
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
        # A flow that declares a deployment target deploys only that deployment
        target_deployment_name = _targeted_deployment_name(flow, existing_data)
        task_id = await _start_deployment(
            request,
            project_name,
            existing_data,
            deployment_name=target_deployment_name,
            base_version=state.base_version,
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

    # save_only
    clear_modal_state_by_token(wizard_token)
    rendered = render_fragment(
        request,
        template="bg/_modal-wizard-success.html.j2",
        context={},
    )

    # Run after_save hooks (fire-and-forget)
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

    Returns ``(existing_data, render_response)``. ``render_response`` is non-None
    only when validation rejected the save and the caller should return it (the
    review re-render). The ProjectManager lifecycle (and its temp clone cleanup)
    is owned by the caller, which closes it once this returns.
    """
    existing_data = await project_manager.get_contents()

    # Capture existing attachments' encrypted content before the form merge: the wizard
    # strips it from the session (see _strip_attachment_content), so it is re-attached at
    # save by PreserveAttachmentContentHook keyed by id.
    original_attachment_content = {
        att_id: entry.get("content")
        for att_id, entry in extract_attachment_catalog(existing_data).items()
        if isinstance(entry, dict) and entry.get("content")
    }

    existing_data = await apply_modal_edit(
        existing_data,
        merged_data,
        flow=flow,
        active_sections=active_sections,
        state=state,
        project_name=project_name,
        original_attachment_content=original_attachment_content,
    )

    # Save through the single validated path: schema + structural integrity
    # validation, canonical dumper, commit + push, and cache refresh in one shot.
    # A validation failure (e.g. pre-existing structural drift surfaced by the
    # full-project check) is returned to the caller as a review re-render.
    #
    # ConflictError hoort in dezelfde rij. Hij komt uit de compare-and-swap als er
    # tijdens het bewerken iemand anders in hetzelfde onderdeel schreef, en draagt zelf
    # de uitleg voor de gebruiker mee. Zonder deze regel viel hij door naar buiten als
    # een kale 500: gemeten in de reallife-doorloop van RC-112, waar een API-patch en
    # een verwijdering in de componenten-modal elkaar op hetzelfde bestand raakten.
    try:
        await project_manager.save_and_commit_project(existing_data, f"Update {project_name} ({flow.flow_id})")
    except (ProjectSchemaError, ProjectIntegrityError, ConflictError) as e:
        logger.warning("Modal wizard save rejected by validation for %s (flow=%s): %s", project_name, flow.flow_id, e)
        return existing_data, _render_modal_review(
            request, wizard_token, project_name, flow.flow_id, active_sections, state, global_errors=[str(e)]
        )
    return existing_data, None


async def _handle_backup_restore_submit(
    request: Request,
    wizard_token: str | None,
    project_name: str,
    flow_id: str,
    action: str,
    state,
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
        if state.base_data:
            for run in state.base_data.get("_backup_runs", []):
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

    # Rendered once on purpose -- see render_progress_fragment in opi/web/task_progress.py.
    rendered = render_fragment(
        request,
        template="bg/_modal-wizard-progress.html.j2",
        context={"task_id": task_id, "project_name": project_name},
    )

    clear_modal_state_by_token(wizard_token)
    return HTMLResponse(content=rendered)


@detail_edit_router.get("/{project_name}/modal-wizard/progress/{task_id}", response_class=HTMLResponse)
@requires_sso
async def modal_wizard_progress_html(request: Request, project_name: str, task_id: str) -> HTMLResponse:
    """Return server-rendered progress fragment for HTMX polling.

    Reads task state from the V2 async task service (database-backed).
    """
    from opi.core.task_helpers import get_task_service
    from opi.web.router import _v2_task_to_template_context

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
        return HTMLResponse(content=_progress_fragment(request, context))

    context = _v2_task_to_template_context(task, project_name)
    context["task_id"] = task_id

    # Rendered once on purpose. The fragment is a template file, so the component
    # extension already replaced its <c-...> tags at compile time; a second pass with
    # ``process_components`` would parse the rendered HTML as a Jinja template again,
    # and a step or deployment name carrying ``{{ ... }}`` would be executed instead of
    # shown. Same reason as render_progress_fragment in opi/web/task_progress.py.
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
    if not state.base_data:
        return

    # Get backup_run_id from step 1 (restore-select)
    step1_data = state.step_data.get("restore-select", {})
    backup_run_id = step1_data.get("backup_run_id", "")
    if not backup_run_id:
        return

    # Find matching run in _backup_runs
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
