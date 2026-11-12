"""Admin routes for the generic, catalog-driven approver interface.

A listing page of the pending approval items across all projects, plus the admin-scoped modal
wizard endpoints that approve or deny them. The items and the verdicts flow through the catalog
ApprovalSpecs in ``opi/services/approvals.py``, so this router is not domain-specific:
publish-on-web declares a domain and a subdomain approval, send-email declares one for the use
of the service itself.
"""

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.responses import Response

from opi.core.authz.actions import Action, action
from opi.forms.i18n import get_default_nl_translator
from opi.forms.renderer import FormRenderer
from opi.forms.visualizers.flows import get_flow
from opi.forms.widgets.lotc import LOTCWidgetAdapter
from opi.forms.wizard.resolver import (
    get_section_metadata,
    resolve_active_sections,
)
from opi.forms.wizard.session import (
    clear_modal_state_by_token,
    init_modal_state_tokenized,
    load_modal_state_for,
    save_modal_state_by_token,
)
from opi.http.auth.auth_decorators import require_platform_admin, requires_sso
from opi.http.auth.request_authority import RequestAuthority
from opi.http.render.lotc_switch import render_fragment
from opi.http.render.menu import get_menu_items
from opi.http.render.navigation_lotc import to_nldd_icon
from opi.http.task_helpers import queue_rollout

if TYPE_CHECKING:
    from opi.forms.visualizers.sections import FormSection
    from opi.forms.wizard.state import WizardState
from opi.manager.project.update import ProjectChange, record_approval_decision
from opi.manager.project_manager import ProjectManager
from opi.services.approvals import collect_approval_items
from opi.services.project_schema import ProjectIntegrityError, ProjectSchemaError
from opi.services.project_store import ConflictError, get_project_store
from opi.services.project_utils import apply_literal_scalars
from opi.services.registry import get_service
from opi.services.services import ServiceAdapter
from opi.services.services_enums import ServiceType

logger = logging.getLogger(__name__)

approvals_router = APIRouter(prefix="/admin/approvals", tags=["approvals"])

FLOW_ID = "admin-approval"


def _create_renderer() -> FormRenderer:
    """The form renderer for this request."""
    return FormRenderer(
        widget_adapter=LOTCWidgetAdapter(),
        translator=get_default_nl_translator(),
    )


def _render_section_html(
    section: FormSection,
    yaml_data: dict[str, Any],
    errors: dict[str, list[str]] | None = None,
) -> str:
    """Render the form fields of a section.

    The adapter renders at once, and that HTML must NOT go through a template render again: it
    carries what someone typed into the form.
    """
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
    return html


def _render_modal_step(
    request: Request,
    wizard_token: str | None,
    state: WizardState,
    section: FormSection,
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
        # The dialog carries its own title, which says the same as the section head, and this
        # flow has only ONE step, so the section head would be a second heading above it. Off
        # here only: the edit dialogs of a project keep theirs.
        "show_section_head": False,
        # Our sections carry Dutch ROOS icon names; the LOTC templates need the NLDD words.
        "nldd_icon": to_nldd_icon,
    }
    return render_fragment(
        request,
        template="bg/_modal-wizard-step.html.j2",
        context=context,
    )


def _modal_error(request: Request, message: str, status_code: int) -> HTMLResponse:
    """A refusal of the dialog as a readable fragment, with the real status code.

    htmx fills the dialog, so what the route answers is what the user sees. An
    ``HTTPException`` gives JSON here, and htmx swaps nothing on an error code by default,
    which together is a window that opens and stays empty. The status code is kept; the
    ``htmx:beforeSwap`` hook in the template makes htmx show the fragment anyway.
    """
    return HTMLResponse(
        content=render_fragment(
            request,
            template="bg/_modal-fout.html.j2",
            context={"request": request, "melding": message},
        ),
        status_code=status_code,
    )


def _collect_all_projects_approval_data() -> list[dict[str, Any]]:
    """Collect the approval items of every project, for the listing page."""
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


#: The statuses that can be filtered on: the key goes in the URL (``?status=``), the label in
#: the picker. A list and not a dict, because the ORDER is the order in the picker, with ``""``
#: for everything in front.
#:
#: The keys are the same as the ones in the project file (``current_status``) and the labels the
#: same as the badges in the table (``status_labels`` in the template). Two lists that say the
#: same thing would drift apart; tests/test_approvals_statusfilter.py holds them equal.
APPROVAL_STATUSSEN: list[tuple[str, str]] = [
    ("", "Alle statussen"),
    ("requested", "Aangevraagd"),
    ("approved", "Goedgekeurd"),
    ("denied", "Afgewezen"),
]


def filter_by_status(projects_data: list[dict[str, Any]], status: str) -> list[dict[str, Any]]:
    """Keep only the requests with this status, and the projects that still have one.

    A project left without a single request drops out: a project panel with an empty table
    under it reads as "this project has nothing".

    An empty or unknown status does not filter. Unknown is deliberately the same as empty and
    not "nothing found": a mistyped ``?status=`` in a shared link should show the list, not an
    empty page that reads as a failure.
    """
    if not status or status not in {key for key, _ in APPROVAL_STATUSSEN if key}:
        return projects_data

    filtered: list[dict[str, Any]] = []
    for project in projects_data:
        items = [item for item in project["approval_items"] if item.get("current_status") == status]
        if items:
            filtered.append({**project, "approval_items": items})
    return filtered


def group_by_service(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bundle the requests of a project per SERVICE, with that service's name and icon.

    The group head takes its name, icon and colour from the ``ServiceDefinition`` in the
    registry and not from a list in the template, so a fourth service with an approval appears
    correctly on its own.

    "toon_soort" is the generic form of "does the kind tag add anything?": for
    publish-on-web it tells a domain from a subdomain, and for a service with one kind it only
    repeats the group head, so the column stays away.

    The order is that of the items themselves, so the list does not shift from run to run. The
    COUNT keeps going over ITEMS, so ``approval_items`` stays next to these groups: "x of y
    requests" counts requests and not groups.
    """
    groups: dict[str, dict[str, Any]] = {}
    for item in items:
        key = item.get("service") or item.get("type") or ""
        group = groups.get(key)
        if group is None:
            group = {"service": key, "naam": item.get("label") or key, "icoon": "", "aanvragen": []}
            try:
                definition = get_service(ServiceType(key)).definition
            except ValueError, KeyError:
                # An item of a service the registry does not know; the label of the spec is
                # then the best there is, and better than leaving the row out.
                logger.warning("Goedkeuringsitem van onbekende dienst %r; groepskop valt terug op het opschrift", key)
            else:
                group["naam"] = definition.name
                group["icoon"] = definition.icon
            groups[key] = group
        group["aanvragen"].append(item)

    for group in groups.values():
        group["toon_soort"] = any(item.get("label") and item["label"] != group["naam"] for item in group["aanvragen"])
    return list(groups.values())


@approvals_router.get("", response_class=HTMLResponse)
@action(Action.PLATFORM_APPROVALS)
@requires_sso
async def list_subdomains(request: Request) -> Response:
    """List all approval requests across all projects."""

    user = require_platform_admin(request)

    all_projects_data = _collect_all_projects_approval_data()

    # Filtering happens HERE and not in the browser: it then works without JavaScript, a
    # filtered list is shareable as a URL, and the chosen value survives a swap because the
    # server renders it along.
    status = (request.query_params.get("status") or "").strip()
    # Filter on ITEMS first and group after: the counter at the bottom counts requests and not
    # groups, and a group that is empty after filtering should not be there.
    projects_data = [
        {**project, "approval_groups": group_by_service(project["approval_items"])}
        for project in filter_by_status(all_projects_data, status)
    ]

    from opi.http.render.lotc_switch import build_lotc_admin, render

    return render(
        request,
        template="bg/admin-approvals.html.j2",
        context={
            "request": request,
            "menu_items": get_menu_items(user),
            "projects_data": projects_data,
            # The UNFILTERED count travels along, so an empty list can say whether there is
            # nothing at all or only nothing with this status.
            "approvals_totaal": sum(len(p["approval_items"]) for p in all_projects_data),
            "approvals_getoond": sum(len(p["approval_items"]) for p in projects_data),
            "approval_status": status,
            "approval_statussen": APPROVAL_STATUSSEN,
            "success_message": request.query_params.get("success"),
            **build_lotc_admin(user=user, current_path="/admin/approvals"),
        },
    )


@approvals_router.get("/{project_name}/modal-wizard/{flow_id}", response_class=HTMLResponse)
@action(Action.PLATFORM_APPROVALS)
@requires_sso
async def modal_wizard_init(request: Request, project_name: str, flow_id: str) -> HTMLResponse:
    """Initialize the domain approval modal wizard for a project."""
    user = require_platform_admin(request)

    # These three refusals land IN the dialog, which is already open, so they go back as a
    # fragment and not as an HTTPException.
    if flow_id != FLOW_ID:
        return _modal_error(request, f"Onbekende flow '{flow_id}'.", 404)

    project = get_project_store().get(project_name)
    if not project:
        return _modal_error(request, f"Project '{project_name}' is niet gevonden.", 404)

    project_data = project.data or {}
    approval_items = collect_approval_items(project_data)
    if not approval_items:
        return _modal_error(request, "Er zijn geen aanvragen voor dit project.", 400)

    flow = get_flow(flow_id)
    first_section = flow.sections[0]

    seed_data: dict[str, Any] = {"_approval_items": approval_items}

    wizard_token, state = init_modal_state_tokenized(
        flow_id=flow_id,
        first_step=first_section.section_id,
        active_sections=[first_section.section_id],
        project_name=project_name,
        owner_email=str(user.get("email", "")),
    )
    state.step_data = {first_section.section_id: seed_data}
    state.base_data = {"_admin_email": user.get("email", "")}
    save_modal_state_by_token(wizard_token, state)

    yaml_data = state.get_merged_data()
    step_html = _render_section_html(first_section, yaml_data)

    rendered = _render_modal_step(request, wizard_token, state, first_section, step_html, project_name)
    return HTMLResponse(content=rendered)


@approvals_router.post(
    "/{project_name}/modal-wizard/{flow_id}/step/{section_id}",
    response_class=HTMLResponse,
)
@action(Action.PLATFORM_APPROVALS)
@requires_sso
async def modal_wizard_submit_step(request: Request, project_name: str, flow_id: str, section_id: str) -> HTMLResponse:
    """Validate and submit the approval step."""
    user = require_platform_admin(request)

    if flow_id != FLOW_ID:
        raise HTTPException(status_code=404, detail="Onbekende flow")

    wizard_token = request.query_params.get("_wizard_token")
    state = load_modal_state_for(request, wizard_token, project_name)
    if not state or state.flow_id != flow_id:
        logger.warning("Modal wizard session lost for %s/%s (state=%s)", project_name, flow_id, state)
        raise HTTPException(
            status_code=400,
            detail="Wizard sessie verlopen. Sluit dit venster en probeer opnieuw.",
        )

    content_type = request.headers.get("content-type", "")
    if "application/json" not in content_type:
        logger.warning("Expected JSON body, got content-type: %s", content_type)
        raise HTTPException(
            status_code=400,
            detail="Verwacht JSON body (json-enc extensie niet geladen?)",
        )
    body = await request.json()
    body.pop("_wizard_token", None)

    # No editables: store the raw form data directly, the same as backup and restore do.
    state.store_step_data(section_id, body)
    state.mark_completed(section_id)
    save_modal_state_by_token(wizard_token, state)

    # Single-section flow: no review, straight to submit.
    return await _do_submit(request, wizard_token, user, project_name)


def _reseed_approval_items(merged_data: dict[str, Any], project_name: str) -> dict[str, Any]:
    """Rebuild ``_approval_items`` from the project, keeping the submitted verdicts.

    The items travel through the form as hidden fields carrying only routing + identity,
    so a re-render off the submission alone loses the verdict history (it is server
    state, never posted back) and the approver sees the items without their past. Read
    the items fresh and re-apply the approver's in-flight choices on top.
    """
    submitted = merged_data.get("_approval_items")
    if not isinstance(submitted, list):
        return merged_data

    project = get_project_store().get(project_name)
    if not project:
        return merged_data
    fresh = collect_approval_items(project.data or {})
    chosen = {
        (item.get("type"), item.get("domain"), item.get("name")): item for item in submitted if isinstance(item, dict)
    }
    for item in fresh:
        pick = chosen.get((item.get("type"), item.get("domain"), item.get("name")))
        if pick:
            item["status"] = pick.get("status", "skip")
            item["message"] = pick.get("message", "")

    return {**merged_data, "_approval_items": fresh}


async def _do_submit(request: Request, wizard_token: str | None, user: dict, project_name: str) -> HTMLResponse:
    """Execute the final approval submission."""
    state = load_modal_state_for(request, wizard_token, project_name)
    if not state:
        raise HTTPException(
            status_code=400,
            detail="Wizard sessie verlopen. Sluit dit venster en probeer opnieuw.",
        )

    flow = get_flow(FLOW_ID)
    active_sections = flow.sections

    merged_data = state.get_merged_data()

    # The admin email travels along so post_merge can record it in the history.
    merged_data["_admin_email"] = user.get("email", "")

    project = get_project_store().get(project_name)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' niet gevonden")

    def apply(existing_data: dict[str, Any]) -> dict[str, Any]:
        stored = copy.deepcopy(existing_data)
        existing_data.update(merged_data)

        # The bulk copy above writes everything the session carried, so a manipulated
        # session could put a realm password or another service's approval state in the
        # file. Guarded here and not through EditGuard.PLATFORM_FIELDS: the approval state
        # is itself platform-managed, so guarding AFTER the verdict below would restore it
        # and throw the decision away. The verdict is written by the platform's own writer,
        # which is exactly the exception the guard documents.
        ServiceAdapter.enforce_platform_managed_fields(existing_data, stored)

        # post_merge maps ``_approval_items`` back onto the domains structure.
        for section in active_sections:
            if section.post_merge:
                section.post_merge(existing_data, merged_data)

        # Transient keys that must not reach the YAML.
        existing_data.pop("_admin_email", None)
        existing_data.pop("_approval_items", None)

        apply_literal_scalars(existing_data)
        return existing_data

    change = ProjectChange(
        project_name=project_name,
        apply=apply,
        commit_message=f"Update project {project_name} (domain approval)",
        # A platform admin decides here, so none of the guards an ordinary edit runs apply.
        # This is also the one write that may set an approval status; see P7 and P8 of RC-483.
        guards=frozenset(),
    )
    # Closed explicitly, so its temp git clone is cleaned up on every exit path.
    project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")
    try:
        result = await record_approval_decision(change, RequestAuthority(request, project_name), project_manager)
    # A validation failure re-renders the approval step with the message instead of 500ing
    # (e.g. pre-existing structural drift surfaced by the full check). ConflictError belongs
    # in the same row; without it, it falls through as a bare 500.
    except (ProjectSchemaError, ProjectIntegrityError, ConflictError) as e:
        logger.warning("Domain approval save rejected for %s (%s): %s", project_name, type(e).__name__, e)
        first_section = active_sections[0]
        render_data = _reseed_approval_items(state.get_merged_data(), project_name)
        step_html = _render_section_html(first_section, render_data)
        # ConflictError goes through as-is; a validation error gets an intro.
        if isinstance(e, ConflictError):
            message = str(e)
        else:
            message = f"Het besluit is niet opgeslagen, want het project is daarna niet geldig: {e}"
        rendered = _render_modal_step(
            request, wizard_token, state, first_section, step_html, project_name, global_errors=[message]
        )
        return HTMLResponse(content=rendered)
    finally:
        await project_manager.close()
    logger.info("Project %s domains updated via admin approval (by %s)", project_name, user.get("email"))

    if not result.needs_rollout:
        # Every item skipped: nothing was written, so there is nothing to reconcile and no
        # progress to follow.
        logger.info("Approval for %s decided nothing, so no round was started", project_name)
        clear_modal_state_by_token(wizard_token)
        return HTMLResponse(content=render_fragment(request, template="bg/_modal-wizard-success.html.j2", context={}))

    deployment_names = result.rollout_deployments
    task_id = await queue_rollout(request, project_name, deployment_names)
    logger.info(
        "Approval for %s scoped redeploy to deployment(s): %s",
        project_name,
        "(whole project)" if deployment_names is None else deployment_names,
    )

    rendered = render_fragment(
        request,
        template="bg/_modal-wizard-progress.html.j2",
        context={"task_id": task_id, "project_name": project_name},
    )

    clear_modal_state_by_token(wizard_token)
    return HTMLResponse(content=rendered)
