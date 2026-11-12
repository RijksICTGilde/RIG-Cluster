"""HTMX wizard routes for multi-step project forms."""

from __future__ import annotations

import contextlib
import html
import logging
import re
from typing import TYPE_CHECKING, Any, NamedTuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import TemplateNotFound
from starlette.responses import Response

from opi.core.authz.actions import Action, action
from opi.core.request_user import get_current_user
from opi.forms.editables.path import resolve_path
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.editables.service_path import (
    find_service_in_list,
    parse_service_path,
    smart_get_value,
    smart_set_value,
)
from opi.forms.i18n import get_default_nl_translator
from opi.forms.renderer import FormRenderer
from opi.forms.visualizers.flows import get_flow
from opi.forms.widgets.lotc import LOTCWidgetAdapter
from opi.forms.wizard.members import members_for_form
from opi.forms.wizard.mutation import apply_component_services_mutation, apply_services_mutation
from opi.forms.wizard.resolver import (
    get_section_metadata,
    resolve_active_section_ids,
    resolve_active_sections,
)
from opi.forms.wizard.session import (
    clear_wizard_state,
    get_wizard_state,
    init_wizard_state,
    save_wizard_state,
)
from opi.forms.wizard.state import CLEARED_FIELD
from opi.forms.wizard.write_set import apply_write_paths, flow_write_paths
from opi.http.auth.auth_decorators import requires_sso
from opi.http.auth.authz import authorize, authorized_project
from opi.http.auth.request_authority import RequestAuthority
from opi.http.render.lotc_switch import render
from opi.http.render.menu import get_menu_items
from opi.http.render.navigation_lotc import get_navigation
from opi.http.render.templates_lotc import templates_lotc
from opi.manager.project.update import ProjectChange, StepUpRequiredError, update_project
from opi.services.approvals import ensure_approval_requests
from opi.services.catalog.cross_domain_access.context import build_cross_domain_context
from opi.services.help_text import is_markdown_help, render_service_help
from opi.services.project_file_handler import merge_staged_attachments
from opi.services.project_roles import CREATOR_ROLES
from opi.services.project_schema import ProjectIntegrityError, ProjectSchemaError, validate_project_schema
from opi.services.project_store import ConflictError
from opi.services.project_utils import apply_literal_scalars
from opi.services.schema_migration import normalize_service_entries
from opi.utils.csrf import reject_misfired_form_get
from opi.utils.nldd_iconen import to_nldd_icon
from opi.utils.yaml_util import dump_yaml_to_string

if TYPE_CHECKING:
    from opi.forms.presets.loader import Preset
    from opi.forms.visualizers.flows import FormFlow
    from opi.forms.visualizers.sections import FormSection
    from opi.forms.visualizers.visualizer import EditableVisualizer
    from opi.forms.wizard.state import WizardState

logger = logging.getLogger(__name__)

wizard_router = APIRouter(prefix="/forms/wizard", tags=["wizard"])


def _create_renderer() -> FormRenderer:
    """A configured FormRenderer for the wizard forms.

    The PREPARATION per field type is shared business logic: which options, which value, how a
    sequence is built. Only the adapter changes, and with it the templates that render a field.
    """
    return FormRenderer(
        widget_adapter=LOTCWidgetAdapter(),
        translator=get_default_nl_translator(),
    )


def _lotc_page_context(request: Request, user: dict[str, Any] | None) -> dict[str, Any]:
    """What a WHOLE wizard page needs on top: the navigation.

    The path is the one of the "Nieuw project" menu item, so that item lights up in the side
    column for as long as the wizard is open.
    """
    return {"navigation": get_navigation(user, current_path="/forms/wizard/restart")}


def _get_section_from_flow(flow_id: str, section_id: str) -> FormSection:
    """Look up a section by ID within a flow."""
    flow = get_flow(flow_id)
    for section in flow.sections:
        if section.section_id == section_id:
            return section
    raise HTTPException(status_code=404, detail=f"Stap '{section_id}' niet gevonden")


#: Every Jinja delimiter starts with "{" and ends with "}", so spacing EVERY brace covers all
#: of them at once. Replacing whole delimiter PAIRS is unsafe: those passes feed each other, and
#: "{{{{" comes back out as "{{", which Jinja reads as an expression again. A single-character
#: replacement in one translate pass cannot re-form a delimiter, however deeply braces nest.
_BRACE_SPACING = str.maketrans({"{": "{ ", "}": " }"})


def _defuse_template_syntax(messages: dict[str, list[str]] | None) -> dict[str, list[str]] | None:
    """Space out every brace in per-field messages before they are rendered.

    Field messages end up INSIDE the HTML string this module returns, and
    ``wizard_step.html.j2`` pipes that string through ``process_components``, which compiles it
    as a Jinja template, a second render. HTML-escaping does not help there: ``{{ ... }}`` needs
    no special characters. Several validators quote the rejected value in their message, so
    without this a value typed into a form would be executed as a template.

    Spacing the braces keeps the message readable while making it inert.
    """
    if not messages:
        return messages
    defused: dict[str, list[str]] = {}
    for path, texts in messages.items():
        defused[path] = [text.translate(_BRACE_SPACING) for text in texts]
    return defused


def _render_step_html(
    request: Request,
    section: FormSection,
    yaml_data: dict[str, Any],
    errors: dict[str, list[str]] | None = None,
    edit_mode: bool = False,
    warnings: dict[str, list[str]] | None = None,
    flow_id: str | None = None,
) -> str:
    """Render the form fields of a single wizard step.

    ``request`` only decides WHICH components render the field: the same fields, the same
    values, the same messages, through the adapter that matches the page around it. One without
    the other would yield a page built from two component systems, and that does not render.
    """
    import copy

    from opi.forms.editables.service_path import smart_get_value, smart_set_value

    errors = _defuse_template_syntax(errors)
    warnings = _defuse_template_syntax(warnings)

    renderer = _create_renderer()
    if not section.layout:
        return ""

    # A transient field deferred to a parent is stripped from the saved state, so it is
    # restored from that parent before rendering, or the input shows nothing.
    render_data = copy.deepcopy(yaml_data)

    if section.section_id == "domains":
        logger.debug("[domains render START] processing %d editables", len(section.editables))

    def _restore_transient_fields(editables: list[Any], data: dict[str, Any]) -> None:
        """Recursively restore transient fields from their dependencies, including children."""
        for editable in editables:
            ed = editable.editable

            if section.section_id == "domains":
                logger.debug(
                    "[domains render] checking editable: path=%s, transient=%s, depends_on=%s, has_children=%s",
                    ed.yaml_path,
                    ed.transient,
                    ed.depends_on,
                    bool(editable.children),
                )

            if editable.children:
                _restore_transient_fields(editable.children, data)

            if ed.transient and ed.depends_on:
                # A wildcard path can only be written per item: a bare set_value on it is a
                # ValueError.
                if "[*]" in ed.yaml_path:
                    list_path = ed.yaml_path.split("[*]", 1)[0]
                    items = smart_get_value(data, list_path) or []
                    for index in range(len(items)):
                        dep_value = smart_get_value(data, resolve_path(ed.depends_on, index))
                        if dep_value and dep_value != "__custom__":
                            smart_set_value(data, resolve_path(ed.yaml_path, index), dep_value)
                    continue

                dep_value = smart_get_value(data, ed.depends_on)

                if section.section_id == "domains":
                    logger.debug(
                        "[domains render] FOUND transient field %s, dep_value=%s",
                        ed.yaml_path,
                        dep_value,
                    )

                # A non-sentinel parent value is a custom value, so it is restored into the
                # transient for display.
                if dep_value and dep_value != "__custom__":
                    smart_set_value(data, ed.yaml_path, dep_value)
                    logger.debug(
                        "[domains render] RESTORED transient field %s from dependency %s (value=%s)",
                        ed.yaml_path,
                        ed.depends_on,
                        dep_value,
                    )

    _restore_transient_fields(section.editables, render_data)

    yaml_data = render_data

    return renderer.render_fields_from_editables(
        editables=section.editables,
        yaml_data=yaml_data,
        layout=section.layout,
        errors=errors,
        edit_mode=edit_mode,
        warnings=warnings,
        # The wizard we are in, so a partial with endpoints of its own builds the right URL
        # instead of a fixed create-project.
        flow_id=flow_id,
    )


def _build_step_context(
    request: Request,
    flow_id: str,
    section: FormSection,
    step_html: str,
    errors: dict[str, list[str]] | None = None,
    global_errors: list[str] | None = None,
) -> dict[str, Any]:
    """Build the template context for rendering a wizard step."""
    state = get_wizard_state(request)
    if not state:
        raise HTTPException(status_code=400, detail="Geen wizard sessie gevonden")

    flow = get_flow(flow_id)
    active_sections = resolve_active_sections(flow, state.step_data)
    section_meta = get_section_metadata(active_sections)
    steps = state.get_steps(section_meta)

    yaml_data = state.get_merged_data()
    preset_html = _render_preset_html(
        request, flow_id, section.section_id, yaml_data=yaml_data, csrf_token=request.state.csrf_token
    )

    # Every step already completed means the user came back from the review to fix something.
    all_steps_completed = set(steps.all).issubset(set(steps.completed))

    user = get_current_user(request)
    return {
        "request": request,
        "steps": steps,
        "flow_id": flow_id,
        "section": section,
        "step_html": step_html,
        "preset_html": preset_html,
        "errors": errors or {},
        "global_errors": global_errors or [],
        "show_review": flow.show_review,
        "all_steps_completed": all_steps_completed,
        "menu_items": get_menu_items(user),
        "user": user,
        # Our sections carry Dutch ROOS icon names; the LOTC templates need the NLDD words.
        "nldd_icon": to_nldd_icon,
    }


def _step_response(request: Request, context: dict[str, Any]) -> Response:
    """The answer to a step, in the design this request chose.

    A fragment and not a whole page: htmx swaps the content of ``#wizard-step-content`` with
    it, and the step bar travels along as an out-of-band swap inside the fragment.
    """
    return render(
        request,
        template="bg/_wizard-step.html.j2",
        context=context,
    )


def _render_preset_html(
    request: Request,
    flow_id: str,
    section_id: str,
    yaml_data: dict[str, Any] | None = None,
    csrf_token: str = "",
) -> str:
    """Render preset cards for a section, if any presets exist."""
    from opi.forms.presets.loader import load_presets
    from opi.forms.widgets.fields import render_preset_cards

    presets = load_presets(section_id)
    if not presets:
        return ""

    locked_presets: dict[str, str] = {}

    if yaml_data is not None:
        section = _get_section_from_flow(flow_id, section_id)
        # The forced locked_by_service values first, so preset detection sees them.
        _apply_locked_by_service(section.editables, yaml_data)
        locked_presets = _detect_locked_presets(presets, section.editables, yaml_data)

    return render_preset_cards(
        presets,
        flow_id,
        section_id,
        yaml_data=yaml_data,
        locked_presets=locked_presets,
        csrf_token=csrf_token,
    )


def _apply_locked_by_service(
    editables: list,
    yaml_data: dict[str, Any],
) -> None:
    """Force True for every field locked by an active service; mutates *yaml_data* in place.

    The same rule as ``FormRenderer.render_fields_from_editables``.
    """
    from opi.forms.editables.service_path import smart_set_value
    from opi.forms.visualizers.bridge import _is_service_active

    for editable in editables:
        if editable.locked_by_service and _is_service_active(editable.locked_by_service, yaml_data):
            smart_set_value(yaml_data, editable.editable.yaml_path, True)


def _detect_locked_presets(
    presets: list,
    editables: list,
    yaml_data: dict[str, Any],
) -> dict[str, str]:
    """The presets locked because an active service forces their values.

    A preset is locked when one of its value paths belongs to an editable with
    ``locked_by_service`` whose service is active. Returns preset_id -> hint text.
    """
    from opi.forms.visualizers.bridge import _is_service_active, _service_display_name

    locked_paths: dict[str, str] = {}
    for editable in editables:
        if editable.locked_by_service and _is_service_active(editable.locked_by_service, yaml_data):
            locked_paths[editable.editable.yaml_path] = _service_display_name(editable.locked_by_service)

    if not locked_paths:
        return {}

    result: dict[str, str] = {}
    for preset in presets:
        for path in preset.values:
            if path in locked_paths:
                result[preset.id] = f"Vereist door: {locked_paths[path]}"
                break

    return result


@wizard_router.get("/restart")
@action(Action.WIZARD_USE)
@requires_sso
async def wizard_restart(request: Request) -> RedirectResponse:
    """Clear any existing wizard state and redirect to the start page."""
    clear_wizard_state(request)
    return RedirectResponse(url="/forms/wizard/start", status_code=302)


@wizard_router.get("/start", response_class=HTMLResponse)
@action(Action.WIZARD_USE)
@requires_sso
async def wizard_start(request: Request) -> Response:
    """Render the wizard introduction / landing page."""
    user = get_current_user(request)
    return render(
        request,
        template="bg/wizard-start.html.j2",
        context={
            "request": request,
            "menu_items": get_menu_items(user),
            "user": user,
            **_lotc_page_context(request, user),
        },
    )


def flow_from_path(flow_id: str, **context: Any) -> FormFlow:  # noqa: ANN401  # a project-file value
    """The flow a path points at, or a 404.

    ``get_flow`` raises a KeyError on an unknown id; this turns that into a 404.
    """
    try:
        return get_flow(flow_id, **context)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=f"Onbekende wizard: {flow_id}") from error


def creator_as_member(email: str) -> dict[str, Any]:
    """The signing-in user as the first member of a new project."""
    return {"email": email, "roles": sorted(role.value for role in CREATOR_ROLES)}


@wizard_router.get("/{flow_id}", response_class=HTMLResponse)
@action(Action.WIZARD_USE)
@requires_sso
async def wizard_page(request: Request, flow_id: str) -> HTMLResponse:
    """Render the full wizard page, resuming existing state if available."""
    flow = flow_from_path(flow_id)
    user = get_current_user(request)

    existing_state = get_wizard_state(request)
    if existing_state and existing_state.flow_id == flow_id:
        state = existing_state
    else:
        active_section_ids = resolve_active_section_ids(flow, {})
        if not active_section_ids:
            raise HTTPException(status_code=500, detail="Geen stappen gevonden in de wizard")

        state = init_wizard_state(
            request,
            flow_id=flow_id,
            first_step=active_section_ids[0],
            active_sections=active_section_ids,
        )
        state.populate_virt_mappings(flow.sections)

        # The template data (repositories, base config) is the lowest-priority layer.
        from opi.forms.editables.template import build_new_project_base_data

        state.base_data = build_new_project_base_data()

        user_email = (user or {}).get("email", "")

        # The same peer-project list the edit flow gets. Without it the cross-domain step had
        # three required fields whose select was empty, so the step could not be saved at all.
        state.base_data.update(build_cross_domain_context(user_email))
        if user_email:
            state.store_step_data("team", {"users": [creator_as_member(user_email)]})

        # One default component. The services checkbox is left unset, so the renderer fills it
        # with every project-level service on the first render.
        state.store_step_data(
            "components",
            {
                "components": [
                    # Resources are deliberately NOT seeded here: a seed wins over
                    # ``Editable.default``, so a default changed at the field would have no
                    # effect on this wizard. The outbound ports DO stay, even though their
                    # editable has a default: the step renders no field for them, and a
                    # default only steps in for a rendered field.
                    {
                        "name": "",
                        "path": "/",
                        "ports": {"inbound": [8080], "outbound": [80, 443]},
                    },
                ],
            },
        )

        # One empty deployment entry, so the index-based merge in get_merged_data() has a slot
        # to land on. NOT "name": that comes from the deployment step, and the index-based
        # merge would overwrite it.
        state.store_step_data(
            "domains",
            {
                "deployments": [
                    {},
                ],
            },
        )
        save_wizard_state(request, state)

    yaml_data = state.get_merged_data()
    section = _get_section_from_flow(flow_id, state.current_step)
    step_html = _render_step_html(
        request,
        section,
        yaml_data=yaml_data,
        edit_mode=state.is_edit,
        flow_id=flow_id,
    )

    active_sections = resolve_active_sections(flow, state.step_data)
    section_meta = get_section_metadata(active_sections)
    steps = state.get_steps(section_meta)

    return render(
        request,
        template="bg/wizard-page.html.j2",
        context={
            "request": request,
            "flow_title": flow.title,
            "flow_id": flow_id,
            "project_name": state.project_name,
            "steps": steps,
            "section": section,
            "step_html": step_html,
            "errors": {},
            "global_errors": [],
            "show_review": flow.show_review,
            "menu_items": get_menu_items(user),
            "user": user,
            "nldd_icon": to_nldd_icon,
            **_lotc_page_context(request, user),
        },
    )


@wizard_router.get("/{flow_id}/edit/{project_name}", response_class=HTMLResponse)
@action(Action.PROJECT_EDIT)
@requires_sso
async def wizard_edit_page(request: Request, flow_id: str, project_name: str) -> HTMLResponse:
    """Render the wizard page pre-filled from an existing project."""
    flow = flow_from_path(flow_id)
    user = get_current_user(request)

    # This flow shows users and config as editable, so a member who may not change the project
    # file does not belong in it. The gate on the route weighs that.
    project = authorized_project(request)

    project_data = project.data
    if not project_data:
        raise HTTPException(status_code=500, detail="Project data niet beschikbaar")

    processor = EditableFormProcessor()
    for section in flow.sections:
        processor.populate_deferred_fields(project_data, section.editables)

    # A local import, because ``router_detail_edit`` imports this module.
    from opi.web.router_detail_edit import _fully_owned_list_keys

    # Pre-fill the step data from the existing project. Only the people, as in
    # ``modal_wizard_init``: without this the merge-by-index moves the token hash of a removed
    # member to the row that takes its place.
    form_data = project_data
    if "users" in _fully_owned_list_keys(flow):
        form_data = members_for_form(project_data)
    step_data = _split_data_across_sections(flow, form_data)

    active_section_ids = resolve_active_section_ids(flow, step_data)
    if not active_section_ids:
        raise HTTPException(status_code=500, detail="Geen stappen gevonden in de wizard")

    first_step = active_section_ids[0]

    state = init_wizard_state(
        request,
        flow_id=flow_id,
        first_step=first_step,
        active_sections=active_section_ids,
        project_name=project_name,
    )
    state.populate_virt_mappings(flow.sections)
    state.step_data = step_data
    # Every section that has data counts as completed.
    for section_id in active_section_ids:
        if step_data.get(section_id):
            state.mark_completed(section_id)
    save_wizard_state(request, state)

    section = _get_section_from_flow(flow_id, first_step)
    step_html = _render_step_html(request, section, yaml_data=project_data, edit_mode=True, flow_id=flow_id)

    active_sections = resolve_active_sections(flow, state.step_data)
    section_meta = get_section_metadata(active_sections)
    steps = state.get_steps(section_meta)

    display_name = project_data.get("display-name", project_name)
    return render(
        request,
        template="bg/wizard-page.html.j2",
        context={
            "request": request,
            "flow_title": f"{flow.title} - {display_name}",
            "flow_id": flow_id,
            "project_name": project_name,
            "steps": steps,
            "section": section,
            "step_html": step_html,
            "errors": {},
            "global_errors": [],
            "show_review": flow.show_review,
            "menu_items": get_menu_items(user),
            "user": user,
            "nldd_icon": to_nldd_icon,
            **_lotc_page_context(request, user),
        },
    )


def _split_data_across_sections(
    flow: FormFlow,
    project_data: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Split existing project data into per-section dicts, so every step can be pre-filled.

    Each section gets the subset of *project_data* its editables reference. An editable with
    ``virtualize`` stores its data under the virtual key, so a config section does not collide
    with the service selection list in ``services``.
    """
    from opi.forms.editables.service_path import smart_get_value

    step_data: dict[str, dict[str, Any]] = {}
    for section in flow.sections:
        section_data: dict[str, Any] = {}
        for editable in section.editables:
            # A readonly visualizer never writes its path, and renderers read it from the
            # merged data. A copy here would be authoritative but stale, and the by-name union
            # in merge_service_lists would re-add a service the user deselected elsewhere.
            if editable.readonly:
                continue
            ed = editable.editable
            value = smart_get_value(project_data, ed.yaml_path)
            if value is not None:
                if ed.virtualize:
                    # Under the virtual key, to avoid a collision with the selection list.
                    virt_key = ed.virtualize[1]
                    parts = ed.yaml_path.split("/")
                    if len(parts) >= 2:
                        svc_name = parts[1]
                        svc_config = smart_get_value(project_data, f"services/{svc_name}")
                        if svc_config is not None:
                            section_data.setdefault(virt_key, {})[svc_name] = svc_config
                else:
                    top_key = ed.yaml_path.split("/")[0].split("[")[0]
                    section_data[top_key] = project_data.get(top_key, value)
        if section_data:
            step_data[section.section_id] = section_data
    return step_data


def _resolve_goto_target(
    goto: str,
    current_section_id: str,
    active_section_ids: list[str],
) -> str | None:
    """Resolve a navigation direction to a concrete section_id.

    ``goto`` is "next", "prev", "review" or a section_id. Returns the section_id to navigate to,
    "review" for the summary, or None when at the end with no review.
    """
    if goto == "review":
        return "review"

    try:
        current_idx = active_section_ids.index(current_section_id)
    except ValueError:
        return active_section_ids[0] if active_section_ids else None

    if goto == "next":
        if current_idx < len(active_section_ids) - 1:
            return active_section_ids[current_idx + 1]
        return None  # past last step

    if goto == "prev":
        if current_idx > 0:
            return active_section_ids[current_idx - 1]
        return active_section_ids[0]  # already at first step

    # A specific section_id, from a step-indicator jump.
    if goto in active_section_ids:
        return goto

    logger.warning("[resolve_goto] unknown goto=%r, staying on %s", goto, current_section_id)
    return current_section_id


async def _navigate_to_step(
    request: Request,
    state: WizardState,
    flow_id: str,
    target_section_id: str,
) -> HTMLResponse:
    """Save the state and render the target step.

    Used by forward navigation, after validation, and by jump and back navigation, which skip
    it. Validates on load when the target step already has stored data, so its errors show at
    once.
    """
    target_section = _get_section_from_flow(flow_id, target_section_id)
    state.current_step = target_section_id
    save_wizard_state(request, state)
    logger.info(
        "[navigate] target=%s, active_sections=%s, current_step=%s",
        target_section_id,
        state.active_sections,
        state.current_step,
    )

    yaml_data = state.get_merged_data()
    edit_mode = state.is_edit

    # Only a step the user explicitly completed is validated on load; a step that merely has
    # saved data from back-navigation should show no errors.
    errors: dict[str, list[str]] | None = None
    if target_section_id in state.completed_steps and target_section_id in state.step_data:
        processor = EditableFormProcessor()
        _, errors = await processor.process_json_submission(
            state.step_data[target_section_id],
            target_section.editables,
            yaml_data,
            edit_mode=edit_mode,
        )
        if not errors:
            errors = None

    step_html = _render_step_html(
        request,
        target_section,
        yaml_data=yaml_data,
        errors=errors,
        edit_mode=edit_mode,
        flow_id=flow_id,
    )
    context = _build_step_context(
        request,
        flow_id,
        target_section,
        step_html,
        errors=errors,
    )
    response = _step_response(request, context)
    response.headers["HX-Push-Url"] = f"/forms/wizard/{flow_id}/step/{target_section_id}"
    return response


@wizard_router.get("/{flow_id}/step/{section_id}", response_class=HTMLResponse)
@action(Action.WIZARD_USE)
@requires_sso
async def load_step(request: Request, flow_id: str, section_id: str) -> HTMLResponse:
    """Load a wizard step, over HTMX or by direct browser navigation.

    An HTMX request goes through ``_navigate_to_step``, which validates the stored data on
    load; a direct browser request gets the whole page.
    """
    reject_misfired_form_get(request)
    state = get_wizard_state(request)
    if not state or state.flow_id != flow_id:
        # No session: the start page initialises the state.
        return RedirectResponse(url=f"/forms/wizard/{flow_id}", status_code=302)  # type: ignore[return-value]

    is_htmx = request.headers.get("HX-Request") == "true"

    if is_htmx:
        return await _navigate_to_step(request, state, flow_id, section_id)

    section = _get_section_from_flow(flow_id, section_id)
    state.current_step = section_id
    save_wizard_state(request, state)

    yaml_data = state.get_merged_data()
    edit_mode = state.is_edit

    # Only a completed step is validated on load, not one merely saved by back-navigation.
    errors: dict[str, list[str]] | None = None
    if section_id in state.completed_steps and section_id in state.step_data:
        processor = EditableFormProcessor()
        _, errors = await processor.process_json_submission(
            state.step_data[section_id],
            section.editables,
            yaml_data,
            edit_mode=edit_mode,
        )
        if not errors:
            errors = None

    step_html = _render_step_html(
        request,
        section,
        yaml_data=yaml_data,
        errors=errors,
        edit_mode=edit_mode,
        flow_id=flow_id,
    )

    flow = get_flow(flow_id)
    user = get_current_user(request)
    active_sections = resolve_active_sections(flow, state.step_data)
    section_meta = get_section_metadata(active_sections)
    steps = state.get_steps(section_meta)

    preset_html = _render_preset_html(
        request, flow_id, section_id, yaml_data=yaml_data, csrf_token=request.state.csrf_token
    )

    return render(
        request,
        template="bg/wizard-page.html.j2",
        context={
            "request": request,
            "flow_title": flow.title,
            "flow_id": flow_id,
            "project_name": state.project_name,
            "steps": steps,
            "section": section,
            "step_html": step_html,
            "preset_html": preset_html,
            "errors": errors or {},
            "global_errors": [],
            "show_review": flow.show_review,
            "menu_items": get_menu_items(user),
            "user": user,
            "nldd_icon": to_nldd_icon,
            **_lotc_page_context(request, user),
        },
    )


@wizard_router.post("/{flow_id}/step/{section_id}", response_model=None)
@action(Action.WIZARD_USE)
@requires_sso
async def submit_step(request: Request, flow_id: str, section_id: str) -> HTMLResponse | RedirectResponse:
    """Process the form data and navigate to the requested step.

    Handles the sequence add and remove actions too, when ``_seq_action`` is in the form data.

    ``_goto`` decides the direction: ``"next"`` or empty validates and advances, ``"prev"``
    saves without validating and goes back, ``"review"`` validates and shows the summary, and a
    section_id saves without validating and jumps to that step.
    """
    state = get_wizard_state(request)
    if not state or state.flow_id != flow_id:
        return RedirectResponse(url=f"/forms/wizard/{flow_id}", status_code=303)

    # Edit mode only: fail fast per step instead of stripping data at the final save.
    if state.project_name:
        authorize(request, action=Action.PROJECT_EDIT, project_name=state.project_name)

    section = _get_section_from_flow(flow_id, section_id)
    flow = get_flow(flow_id)

    # Submitted through the HTMX json-enc extension.
    body = await request.json()

    # The control fields, prefixed with an underscore.
    seq_action = body.pop("_seq_action", None)
    seq_path = body.pop("_seq_path", None)
    seq_index = body.pop("_seq_index", None)
    is_rerender = body.pop("_rerender", None) == "1"
    goto = body.pop("_goto", "next")

    submitted_data = body

    # A sequence add or remove comes before the normal validation.
    if seq_action in ("add", "remove"):
        return await _handle_sequence_action(
            request,
            flow_id,
            section_id,
            section,
            submitted_data,
            str(seq_action),
            seq_path=str(seq_path or ""),
            seq_index=seq_index,
        )

    processor = EditableFormProcessor()
    yaml_data = state.get_merged_data()
    edit_mode = state.is_edit

    enforcer_context = {"project_name": state.project_name, "edit_mode": edit_mode}

    # Re-render only, for a preview update: process the submission but skip validation, so a
    # newly visible field with a default shows no spurious "required".
    if is_rerender:
        submitted_yaml, _errors = await processor.process_json_submission(
            submitted_data,
            section.editables,
            yaml_data,
            edit_mode=edit_mode,
            enforcer_context=enforcer_context,
        )

        processor.clear_hidden_depends_on(section.editables, submitted_yaml)

        section_data = _extract_section_data(section.editables, submitted_yaml)
        state.store_step_data(section_id, section_data)
        save_wizard_state(request, state)

        step_html = _render_step_html(
            request,
            section,
            yaml_data=submitted_yaml,
            edit_mode=edit_mode,
            warnings=processor.field_warnings,
            flow_id=flow_id,
        )
        context = _build_step_context(request, flow_id, section, step_html)
        return _step_response(request, context)

    # Validate, convert and write to yaml in one pass.
    submitted_yaml, errors = await processor.process_json_submission(
        submitted_data,
        section.editables,
        yaml_data,
        edit_mode=edit_mode,
        enforcer_context=enforcer_context,
    )

    if errors:
        logger.warning(
            "[%s validation FAILED] field-level errors: %s",
            section_id,
            errors,
        )
    else:
        logger.info("[%s validation PASSED] field-level validation ok", section_id)

    is_forward = goto in ("next", "review")
    logger.info("[submit_step %s] goto=%r, is_forward=%s", section_id, goto, is_forward)

    # Reconcile the submitted service selection with the base: what the form did not offer
    # cannot have been unticked, and the server fills in a required service. Both rules live in
    # apply_services_mutation, and both flows run through it.
    apply_services_mutation(section.editables, yaml_data, submitted_yaml)
    apply_component_services_mutation(section.editables, yaml_data, submitted_yaml)

    # Forward navigation blocks on a field-level validation error.
    if is_forward and errors:
        # An error on a path this step draws no field for goes to the general bar.
        group_errors = _create_renderer().take_unrendered_errors(
            section.editables, submitted_yaml, errors, edit_mode=edit_mode
        )

        step_html = _render_step_html(
            request,
            section,
            yaml_data=submitted_yaml,
            errors=errors,
            edit_mode=edit_mode,
            warnings=processor.field_warnings,
            flow_id=flow_id,
        )
        context = _build_step_context(
            request,
            flow_id,
            section,
            step_html,
            errors=errors,
            global_errors=group_errors or None,
        )
        return _step_response(request, context)

    # The section-level enforcer, for cross-field validation.
    if is_forward and section.enforcer:
        global_errors = await processor.enforce_sections(
            submitted_yaml, [section], enforcer_context, field_errors=errors, field_warnings=processor.field_warnings
        )

        if global_errors:
            logger.warning(
                "[%s validation FAILED] section-level (enforcer) errors: %s",
                section_id,
                global_errors,
            )
            step_html = _render_step_html(
                request,
                section,
                yaml_data=submitted_yaml,
                errors=errors,
                edit_mode=edit_mode,
                warnings=processor.field_warnings,
                flow_id=flow_id,
            )
            context = _build_step_context(
                request,
                flow_id,
                section,
                step_html,
                errors=errors,
                global_errors=global_errors,
            )
            return _step_response(request, context)
        else:
            logger.info("[%s validation PASSED] section-level (enforcer) validation ok", section_id)

    section_data = _extract_section_data(section.editables, submitted_yaml)
    state.store_step_data(section_id, section_data)

    # The section's post_merge reconciler, against the merged view, with the affected data
    # persisted back into step_data. The services step uses this to drop component-level
    # service config when a project service is deselected; without persisting it here the
    # components step renders stale config blocks until it is re-submitted itself.
    if section.post_merge is not None:
        section.post_merge(submitted_yaml, submitted_yaml)

        # The OWN section has to be stored again as well. The step data above was stored
        # BEFORE this hook ran, so a hook that fills in its OWN section would otherwise write
        # into the void: the addition would stand in the merged view but not in the wizard
        # state, and that state is what gets saved at the end.
        state.store_step_data(section_id, _extract_section_data(section.editables, submitted_yaml))
        if "components" in submitted_yaml:
            state.store_step_data("components", {"components": submitted_yaml["components"]})

    logger.info(
        "[%s] All validations passed. Forward navigation: is_forward=%s, goto=%r",
        section_id,
        is_forward,
        goto,
    )
    if is_forward:
        state.mark_completed(section_id)

    # Re-resolve the active sections: the services step can add or remove conditional steps.
    active_section_ids = resolve_active_section_ids(flow, state.step_data)
    state.active_sections = active_section_ids
    state.stash_inactive_sections(active_section_ids)

    target_section_id = _resolve_goto_target(goto, section_id, active_section_ids)
    logger.info("[submit_step %s] resolved target=%s", section_id, target_section_id)

    if target_section_id == "review":
        # The jump to the summary may skip steps but no required value: validate the WHOLE flow
        # as the final submission does, and land on the first step that misses something.
        validation = await _validate_whole_flow(request, flow_id, state)
        if validation.error_response is not None:
            return validation.error_response
        # The skipped steps were just validated; without this the step indicator on the
        # summary would show them as unfinished.
        for validated in validation.active_sections:
            state.mark_completed(validated.section_id)
        save_wizard_state(request, state)
        return await _render_review(request, flow_id)

    # The last step, with no review: submit.
    if target_section_id is None:
        save_wizard_state(request, state)
        if flow.show_review:
            return await _render_review(request, flow_id)
        return await _do_submit(request, flow_id)

    return await _navigate_to_step(request, state, flow_id, target_section_id)


@wizard_router.post("/{flow_id}/preset/{section_id}/{preset_id}", response_model=None)
@action(Action.WIZARD_USE)
@requires_sso
async def toggle_preset(
    request: Request,
    flow_id: str,
    section_id: str,
    preset_id: str,
) -> HTMLResponse | RedirectResponse:
    """Toggle a preset: apply if not active, remove if active."""
    from opi.forms.presets.loader import get_preset_by_id
    from opi.forms.widgets.fields import _is_preset_applied

    state = get_wizard_state(request)
    if not state or state.flow_id != flow_id:
        return RedirectResponse(url=f"/forms/wizard/{flow_id}", status_code=303)

    preset = get_preset_by_id(section_id, preset_id)
    if not preset:
        raise HTTPException(status_code=404, detail=f"Preset '{preset_id}' niet gevonden")

    section = _get_section_from_flow(flow_id, section_id)
    yaml_data = state.get_merged_data()

    if _is_preset_applied(preset, yaml_data):
        _remove_preset(preset, yaml_data)
    else:
        _apply_preset(preset, yaml_data)

    section_data = _extract_section_data(section.editables, yaml_data)
    state.store_step_data(section_id, section_data)
    save_wizard_state(request, state)

    step_html = _render_step_html(
        request,
        section,
        yaml_data=yaml_data,
        edit_mode=state.is_edit,
    )

    context = _build_step_context(request, flow_id, section, step_html)
    return _step_response(request, context)


def _apply_preset(preset: Preset, yaml_data: dict[str, Any]) -> None:
    """Apply a preset's values additively to yaml_data (mutates in place)."""
    from opi.forms.editables.service_path import smart_get_value, smart_set_value

    for path, value in preset.values.items():
        if isinstance(value, list):
            existing = smart_get_value(yaml_data, path)
            if isinstance(existing, list):
                for item in value:
                    if not _list_contains_item(existing, item):
                        existing.append(item)
                smart_set_value(yaml_data, path, existing)
            else:
                smart_set_value(yaml_data, path, value)
        else:
            smart_set_value(yaml_data, path, value)


def _remove_preset(preset: Preset, yaml_data: dict[str, Any]) -> None:
    """Remove a preset's values from yaml_data (mutates in place)."""
    from opi.forms.editables.service_path import smart_get_value, smart_set_value

    for path, value in preset.values.items():
        if isinstance(value, list):
            existing = smart_get_value(yaml_data, path)
            if isinstance(existing, list):
                for item in value:
                    _list_remove_item(existing, item)
                smart_set_value(yaml_data, path, existing)
        elif isinstance(value, bool):
            smart_set_value(yaml_data, path, not value)
        else:
            smart_set_value(yaml_data, path, None)


def _list_remove_item(items: list[Any], candidate: Any) -> None:  # noqa: ANN401  # a project-file value
    """Remove a matching item from a list (by name for dicts, by equality for scalars)."""
    if isinstance(candidate, dict):
        name = candidate.get("name")
        if name:
            items[:] = [it for it in items if not (isinstance(it, dict) and it.get("name") == name)]
            return
    with contextlib.suppress(ValueError):
        items.remove(candidate)


def _list_contains_item(items: list[Any], candidate: Any) -> bool:  # noqa: ANN401  # a project-file value
    """Whether a list already contains a matching item: by ``name`` for a dict, by equality
    for a scalar.
    """
    if isinstance(candidate, dict):
        name = candidate.get("name")
        if name:
            return any(isinstance(it, dict) and it.get("name") == name for it in items)
    return candidate in items


#: The files a service package may supply as its explanation.
HELP_FILE_NAMES = ("help.md", "guide.md", "help.html.j2")

#: The shape of a help reference: no ``.`` in the directory name, so no path out of the tree.
HELP_TEMPLATE_PATTERN = re.compile(r"(?:[a-zA-Z0-9_-]+/)?[a-zA-Z0-9_-]+\.(?:html\.j2|md)")


@wizard_router.get("/help/{template_name:path}", response_model=None)
@action(Action.WIZARD_USE)
@requires_sso
async def service_help(request: Request, template_name: str) -> HTMLResponse:
    """Render a help text inside a modal-friendly HTML fragment.

    Three shapes are accepted:

    * ``<service-package>/help.md``: a service's own explanation. It is markdown, and the same
      file ``GET /api/v2/services/{name}`` returns, so the portal and an API client read one
      source. A service's ``guide.md`` travels this route too.
    * ``<service-package>/help.html.j2``: the older Jinja form, still resolved by the Jinja
      loader for any help that has not been converted.
    * ``<name>.html.j2``: a help text that belongs to no single service, under
      ``templates/help/``.

    The file name is checked against a fixed list: Jinja knows every template, and without that
    check this route would render the admin screens too.
    """
    if not HELP_TEMPLATE_PATTERN.fullmatch(template_name):
        raise HTTPException(status_code=400, detail="Invalid template name")

    service_dir, _, file_name = template_name.rpartition("/")
    belongs_here = file_name in HELP_FILE_NAMES if service_dir else file_name.endswith(".html.j2")
    if not belongs_here:
        raise HTTPException(status_code=404, detail="Help template not found")

    if is_markdown_help(template_name):
        try:
            markup = render_service_help(template_name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Help template not found") from None
        # htmx loads this in a dialog and wants the bare fragment; a browser opening the URL
        # directly would get that same fragment WITHOUT a <head>, so without stylesheets.
        # Hence the split: the same content, two wrappers.
        if request.headers.get("HX-Request"):
            return HTMLResponse(markup)
        from opi.http.render.navigation_lotc import get_menu_items, get_navigation

        user = request.session.get("user")
        return templates_lotc.TemplateResponse(
            request,
            "help_page.html.j2",
            {
                "request": request,
                "help_markup": markup,
                "menu_items": get_menu_items(user),
                "navigation": get_navigation(user, current_path=request.url.path),
            },
        )

    template_path = template_name if "/" in template_name else f"help/{template_name}"
    try:
        return templates_lotc.TemplateResponse(
            request,
            template_path,
            {"request": request},
        )
    except TemplateNotFound:
        raise HTTPException(status_code=404, detail="Help template not found") from None


async def _handle_sequence_action(
    request: Request,
    flow_id: str,
    section_id: str,
    section: FormSection,
    submitted_data: dict[str, Any],
    action: str,
    *,
    seq_path: str = "",
    seq_index: Any = None,  # noqa: ANN401  # a project-file value
) -> HTMLResponse | RedirectResponse:
    """Add or remove a sequence item and re-render the current step.

    Keeps every current field value by parsing the submitted JSON, changing the target
    sequence, and rendering again.
    """
    from opi.forms.editables.service_path import smart_get_value, smart_set_value

    state = get_wizard_state(request)
    if not state:
        return RedirectResponse(url=f"/forms/wizard/{flow_id}", status_code=303)

    processor = EditableFormProcessor()
    yaml_data = state.get_merged_data()
    edit_mode = state.is_edit
    yaml_data, _errors = await processor.process_json_submission(
        submitted_data,
        section.editables,
        yaml_data,
        edit_mode=edit_mode,
    )

    if not seq_path:
        raise HTTPException(status_code=400, detail="Ontbrekend pad voor reeks-actie")

    # The rendered add/remove path is virtualized (e.g. _services-config{attachments}),
    # but items are stored under (and read back from) the REAL path. Devirtualize so the
    # new row lands where the renderer reads it instead of a dead virtual key.
    editable = _find_sequence_editable(section, seq_path)
    data_path = seq_path
    if editable is not None and getattr(editable.editable, "virtualize", None):
        from opi.forms.editables.editable import reverse_virtualize

        data_path = reverse_virtualize(seq_path, editable.editable.virtualize)

    items = smart_get_value(yaml_data, data_path)
    if not isinstance(items, list):
        items = []

    if action == "add":
        items.append(_empty_sequence_item(editable, yaml_data))
    elif action == "remove":
        remove_index = int(seq_index) if seq_index not in (None, "") else -1
        if 0 <= remove_index < len(items):
            items.pop(remove_index)

    smart_set_value(yaml_data, data_path, items)

    section_data = _extract_section_data(section.editables, yaml_data)
    state.store_step_data(section_id, section_data)
    save_wizard_state(request, state)

    step_html = _render_step_html(request, section, yaml_data=yaml_data, edit_mode=edit_mode)
    context = _build_step_context(request, flow_id, section, step_html)
    return _step_response(request, context)


def _prune_empty_dicts(data: Any) -> None:  # noqa: ANN401  # a project-file value
    """Remove empty dict values from nested data, recursively.

    After a field removal through ``remove_when_none``, an empty parent dict can remain; this
    keeps the YAML output tidy.
    """
    if isinstance(data, dict):
        for key in list(data.keys()):
            _prune_empty_dicts(data[key])
            if isinstance(data[key], dict) and not data[key]:
                del data[key]
    elif isinstance(data, list):
        for item in data:
            _prune_empty_dicts(item)


def _assemble_deployment(final_data: dict[str, Any]) -> None:
    """Assemble the full deployment structure for create mode.

    The wizard stores the domain config under ``deployments[0]`` and the components separately;
    this merges them into the deployment YAML the project manager expects, setting ``name``,
    ``cluster``, ``namespace`` and ``repository`` and building the ``components`` array.

    The deployment-level ``root-component`` is set during the domain step and is left untouched.
    """
    deployments = final_data.get("deployments", [{}])
    deployment = deployments[0] if deployments else {}

    deployment.setdefault("name", "productie")

    clusters = final_data.get("clusters", [])
    if isinstance(clusters, list) and clusters:
        deployment["cluster"] = clusters[0]
    elif isinstance(clusters, str):
        deployment["cluster"] = clusters

    # The namespace is the project name; ProjectManager's get_prefixed_namespace() adds the
    # cluster-specific prefix at deployment time.
    project_name = final_data.get("name", "")
    if project_name:
        deployment["namespace"] = project_name

    deployment.setdefault("repository", "main-repo")

    components = final_data.get("components", [])

    dep_components: list[dict[str, Any]] = []
    for comp in components:
        if isinstance(comp, dict) and comp.get("name"):
            dep_comp: dict[str, Any] = {
                "reference": comp["name"],
                "image": comp.get("image", ""),
            }
            dep_components.append(dep_comp)

    if dep_components:
        deployment["components"] = dep_components

    final_data["deployments"] = [deployment]


def _find_sequence_editable(
    section: FormSection,
    path: str,
) -> Any | None:  # noqa: ANN401  # a project-file value
    """Find the editable definition for a sequence at *path*.

    Handles a top-level sequence by exact match and a nested one where ``[*]`` in the template
    path matches a concrete ``[N]`` index, searching recursively through the children.
    """
    import re

    def _candidate_paths(ed: Editable) -> list[str]:
        # The rendered sequence path uses the virtualized service-config key while the editable
        # carries the real key, so rewrite with the SAME helper the renderer uses: a brace-only
        # replace of its own misses the segment form silently.
        from opi.forms.editables.editable import apply_virtualize

        paths = [ed.yaml_path]
        virt = getattr(ed, "virtualize", None)
        if virt:
            paths.append(apply_virtualize(ed.yaml_path, virt))
        return paths

    def _match(editable: EditableVisualizer) -> EditableVisualizer | None:
        ed = editable.editable
        for candidate in _candidate_paths(ed):
            if candidate == path:
                return editable
            # Does the template path, with [*], match the concrete path, with [N]?
            if str(editable.widget) == "sequence":
                pattern = re.escape(candidate).replace(r"\[\*\]", r"\[\d+\]")
                if re.fullmatch(pattern, path):
                    return editable
        if editable.children:
            for child in editable.children:
                result = _match(child)
                if result is not None:
                    return result
        return None

    for editable in section.editables:
        result = _match(editable)
        if result is not None:
            return result
    return None


def _empty_sequence_item(editable: EditableVisualizer | None, yaml_data: dict[str, Any]) -> Any:  # noqa: ANN401  # a project-file value
    """The right empty item for a sequence.

    ``""`` for a simple sequence with a single non-sequence child; for a complex one a dict
    with the ``default`` values the child editables declare. A nested sequence child is seeded
    with ``min_items`` empty entries, so the renderer and the processor can find them.
    """
    if not editable or not editable.children:
        return ""
    has_complex = len(editable.children) > 1 or any(str(c.widget) == "sequence" for c in editable.children)
    if not has_complex:
        return ""

    # The framework's own path writer, so a {K} dict-key filter materialises into a list. A
    # hand-walker that ignores {K} writes a literal key like "services{metrics-scraper}", which
    # ``additionalProperties: false`` rejects.
    from opi.forms.editables.path import set_value

    seq_path = editable.editable.yaml_path

    def _relative(child_path: str) -> str:
        # Relative to the sequence item: drop the item's own index prefix but KEEP the {K} and
        # [N] filter segments, so set_value materialises them into lists. A residual wildcard
        # becomes the first entry.
        rest = child_path.removeprefix(seq_path)
        if "/" in rest:
            rest = rest.split("/", 1)[1]
        return rest.replace("[*]", "[0]")

    item: dict[str, Any] = {}
    for child in editable.children:
        child_ed = child.editable
        rel = _relative(child_ed.yaml_path)
        if not rel:
            continue
        if _is_service_config_child(rel):
            # A service's config default belongs to a service the item HAS. Seeding it would
            # materialise that service into the item's services list, which picks a service the
            # user never chose and, the list no longer being unset, suppresses the "select all
            # project services" default a new component starts with.
            continue
        if str(child.widget) == "sequence" and child_ed.min_items:
            set_value(item, rel, ["" for _ in range(child_ed.min_items)])
        elif child_ed.default is not None:
            # A callable default derives its value from the project, like the processor does.
            default = child_ed.default(yaml_data) if callable(child_ed.default) else child_ed.default
            if default is not None:
                set_value(item, rel, default)
    return item


def _is_service_config_child(relative_path: str) -> bool:
    """Whether a sequence child's path targets a service's config (``services{X}/...``)."""
    return relative_path.startswith("services{")


@wizard_router.get("/{flow_id}/review", response_model=None)
@action(Action.WIZARD_USE)
@requires_sso
async def review_page(request: Request, flow_id: str) -> HTMLResponse | RedirectResponse:
    """Render the review page."""
    return await _render_review(request, flow_id)


async def _render_review(
    request: Request,
    flow_id: str,
) -> HTMLResponse | RedirectResponse:
    """Build and render the review page."""
    state = get_wizard_state(request)
    if not state or state.flow_id != flow_id:
        return RedirectResponse(url=f"/forms/wizard/{flow_id}", status_code=303)

    flow = get_flow(flow_id)
    active_sections = resolve_active_sections(flow, state.step_data)
    section_meta = get_section_metadata(active_sections)

    state.current_step = "__review__"
    save_wizard_state(request, state)

    steps = state.get_steps(section_meta)
    user = get_current_user(request)

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

    return render(
        request,
        template="bg/_wizard-review.html.j2",
        context={
            "request": request,
            "steps": steps,
            "flow_id": flow_id,
            "section_summaries": section_summaries,
            "menu_items": get_menu_items(user),
            "user": user,
            "nldd_icon": to_nldd_icon,
        },
    )


def _extract_section_data(
    editables: list[Any],
    submitted_yaml: dict[str, Any],
) -> dict[str, Any]:
    """Extract the section data, keeping only the fields this section's editables own.

    A simple top-level key copies its whole value. For an indexed path into a shared list, the
    list items are pruned to the fields this section's editables define, so one section cannot
    capture, and later overwrite, another section's fields in the shallow merge in
    ``get_merged_data()``.

    An editable with ``virtualize`` is read from the real top-level key in *submitted_yaml* but
    stored under the virtual key, which keeps the service selection and the per-service config
    apart.
    """
    import copy

    from opi.forms.editables.service_path import is_service_config_path, parse_service_path
    from opi.services.services import service_entry_name

    section_keys: set[str] = set()
    indexed_fields: dict[str, set[str]] = {}  # top_key -> the owned field names
    owned_services: dict[str, set[str]] = {}  # top_key -> the services this section configures
    #: The config fields this section writes itself, as full yaml_paths. They get a tombstone
    #: when the submission does not carry them; see _tombstone_service_config.
    service_config_leaves: list[str] = []
    # top_key -> field name -> service names, for a service list INSIDE an indexed item
    indexed_services: dict[str, dict[str, set[str]]] = {}
    # top_key -> the plain per-item fields the section renders WHOLE. For such a field the
    # submission IS the selection, so pruning it to this section's configured services would
    # throw away what the user just ticked.
    selection_fields: dict[str, set[str]] = {}
    # real_key -> virtual_key for virtualized editables
    virt_mapping: dict[str, str] = {}

    def _collect_leaf_paths(vis_list: list[Any]) -> None:
        for vis in vis_list:
            # A group is transparent: collect the paths of its children.
            if vis.children and str(vis.widget) == "group":
                _collect_leaf_paths(vis.children)
                continue

            ed = vis.editable
            parts = ed.yaml_path.split("/")
            top = parts[0]
            top_key = top.split("[")[0]
            section_keys.add(top_key)

            if ed.virtualize:
                virt_mapping[ed.virtualize[0]] = ed.virtualize[1]

            # Which service's config this section actually writes. Without this, a section
            # that configures ONE service copies the WHOLE services list, other services'
            # config included, and overwrites theirs on the merge because it comes later in
            # the section order.
            if is_service_config_path(ed.yaml_path):
                owned_services.setdefault(top_key, set()).add(parse_service_path(ed.yaml_path)[0])
                # Plain fields only. A SEQUENCE carries its own items and decides for itself
                # what empty means, so a tombstone there would wipe a list this step merely
                # did not show.
                if (
                    parse_service_path(ed.yaml_path)[1] is not None
                    and "[*]" not in ed.yaml_path
                    and str(vis.widget) != "sequence"
                ):
                    service_config_leaves.append(ed.yaml_path)

            if "[" in top and len(parts) >= 2:
                field_name = parts[1].split("[")[0].split("{")[0]
                indexed_fields.setdefault(top_key, set()).add(field_name)
                # A field addressed through a service filter is a service LIST inside the item
                # and not a plain field. Recording only the field name would make the section
                # replace the whole list and take other services' deployment config with it,
                # so remember WHICH service it configures: the per-item counterpart of
                # ``owned_services``.
                if "{" in parts[1]:
                    service = parts[1].split("{", 1)[1].split("}", 1)[0]
                    indexed_services.setdefault(top_key, {}).setdefault(field_name, set()).add(service)
                elif len(parts) == 2:
                    selection_fields.setdefault(top_key, set()).add(field_name)

    _collect_leaf_paths(editables)

    result: dict[str, Any] = {}
    for key in section_keys:
        if key not in submitted_yaml:
            continue
        value = submitted_yaml[key]
        # Use virtual key for storage when applicable
        store_key = virt_mapping.get(key, key)

        if key in indexed_fields and isinstance(value, list):
            # Prune the list items to the owned fields. An owned field that is absent, cleared
            # through remove_when_none, gets a tombstone, so the additive merge in
            # get_merged_data() deletes the old value instead of resurrecting it from the
            # template snapshot.
            owned = indexed_fields[key]
            per_item_services = indexed_services.get(key, {})
            pruned = []
            for item in value:
                if isinstance(item, dict):
                    pruned_item = {k: copy.deepcopy(v) for k, v in item.items() if k in owned}
                    for field_name, services in per_item_services.items():
                        # Keep only this section's own service entries, and never tombstone
                        # the list: an item without them says nothing about it.
                        #
                        # UNLESS the section renders the whole selection, as the component
                        # form's services checklist does: then the submitted list IS the user's
                        # decision and every ticked name has to survive, because a newly ticked
                        # service exists in no other copy. A foreign config dict is still
                        # demoted to its bare name: the selection is this section's, another
                        # service's config is not.
                        entries = pruned_item.get(field_name)
                        if isinstance(entries, list):
                            if field_name in selection_fields.get(key, set()):
                                kept = [
                                    copy.deepcopy(entry)
                                    if not isinstance(entry, dict) or service_entry_name(entry) in services
                                    else (service_entry_name(entry) or copy.deepcopy(entry))
                                    for entry in entries
                                ]
                            else:
                                kept = [entry for entry in entries if service_entry_name(entry) in services]
                            pruned_item[field_name] = kept
                        else:
                            pruned_item.pop(field_name, None)
                    for owned_field in owned:
                        if owned_field in per_item_services:
                            continue
                        if owned_field not in pruned_item:
                            pruned_item[owned_field] = CLEARED_FIELD
                    pruned.append(pruned_item)
                else:
                    pruned.append(copy.deepcopy(item))
            result[store_key] = pruned
        elif key in owned_services and isinstance(value, list):
            # The same reasoning as the indexed pruning above, one level up: keep only the
            # services this section configures. A bare string entry stays, because that is the
            # selection and not someone else's config.
            keep = owned_services[key]
            result[store_key] = [
                copy.deepcopy(entry)
                for entry in value
                if not isinstance(entry, dict) or service_entry_name(entry) in keep
            ]
        else:
            result[store_key] = copy.deepcopy(value)

    _tombstone_service_config(result, submitted_yaml, service_config_leaves, virt_mapping)
    return result


def _tombstone_service_config(
    result: dict[str, Any],
    submitted_yaml: dict[str, Any],
    leaves: list[str],
    virt_mapping: dict[str, str],
) -> None:
    """Mark the config fields this section writes that the submission does not carry.

    The same rule as for the indexed lists above. The step fragments are merged ADDITIVELY over
    the base, so a key that is simply NOT there cannot delete the old value: it comes back. A
    tombstone does say what absence means: ``get_merged_data`` removes the key after merging,
    and ``apply_write_paths`` removes it from the project file at save.

    Only a field the processor REALLY emptied gets one. What it skipped, readonly or hidden by
    ``show_when``, still stands in ``submitted_yaml``, which is a copy of the project data.

    The tombstone goes on the DEEPEST level that still exists and not blindly on the field
    path: a field path that misses an intermediate level would otherwise create that level, and
    saving would then put an empty block in the project file, a change the user did not make.
    When the intermediate level is missing, THAT is the tombstone.
    """
    for yaml_path in leaves:
        if smart_get_value(submitted_yaml, yaml_path) is not None:
            continue
        top_key = yaml_path.split("/")[0]
        store_key = virt_mapping.get(top_key, top_key)
        entries = result.get(store_key)
        if not isinstance(entries, list):
            continue
        # Only for a service this section really carries: a tombstone may not create a
        # service entry that was not there.
        if find_service_in_list(entries, parse_service_path(yaml_path)[0])[0] == -1:
            continue
        target_path = _deepest_existing_path(result, store_key + yaml_path[len(top_key) :])
        if target_path is not None:
            smart_set_value(result, target_path, CLEARED_FIELD)


def _deepest_existing_path(result: dict[str, Any], path: str) -> str | None:
    """The path the tombstone may go on: the first piece that is missing from *result*.

    Never above ``<service>/config``: that is the config of the service as a whole, and
    throwing that away is a different decision from emptying a field. When ``config`` itself is
    missing there is nothing to erase, and this returns None.
    """
    segments = path.split("/")
    try:
        first = segments.index("config") + 1
    except ValueError:
        return None
    if smart_get_value(result, "/".join(segments[:first])) is None:
        return None
    for i in range(first, len(segments)):
        partial_path = "/".join(segments[: i + 1])
        value = smart_get_value(result, partial_path)
        if value == CLEARED_FIELD:
            # A level above is already struck out whole; writing deeper in would overwrite
            # that tombstone with a partial one.
            return None
        if value is None:
            return partial_path
    return path


def _summary_text(text: Any) -> str:  # noqa: ANN401  # a project-file value
    """Escape a piece of text that goes into summary HTML.

    The functions below build an HTML string that ``wizard_review.html.j2`` renders with
    ``| safe``, so nothing here is escaped for us. Every label and value that ends up between
    the tags goes through this first: values because they are whatever someone typed into the
    form, labels because escaping a constant costs nothing and a label that stops being a
    constant is then already covered.

    Not for the nested fragments, a sequence summary or a joined list of pairs: those are HTML
    this module built, and escaping them would print tags.
    """
    return html.escape(str(text))


def _summary_pairs_html(items: list[tuple[str, str]]) -> str:
    """Render a section's own (label, value) pairs as escaped summary HTML."""
    parts = [f"<dl><dt>{_summary_text(label)}</dt><dd>{_summary_text(value)}</dd></dl>" for label, value in items]
    return "\n".join(parts) if parts else "<p><em>Geen gegevens ingevuld</em></p>"


def _build_section_summary(section: FormSection, yaml_data: dict[str, Any]) -> str:
    """Build an HTML summary of a section's data.

    Handles every editable type: sequences, select fields with provider label resolution,
    checkbox groups and key-value editors.
    """
    if section.summary_fn:
        # A summary_fn returns (label, value) pairs and not HTML: the markup and the escaping
        # are built here, so a section that summarizes itself lands in the same gate as every
        # other field.
        return _summary_pairs_html(section.summary_fn(yaml_data))

    from opi.forms.editables.service_path import smart_get_value

    parts: list[str] = []

    def _collect_summary(vis_list: list[Any]) -> None:
        for editable in vis_list:
            # A HIDDEN field was never filled in by the user, so it never belongs in a
            # summary. These are carriers: the attachments section carries the whole service
            # list so the upload screen knows which services are on, and that landed here as a
            # raw Python dump, AGE blocks included.
            if str(editable.widget) == "hidden":
                continue
            if str(editable.widget) == "group":
                _collect_summary(editable.children or [])
            elif str(editable.widget) == "sequence":
                parts.append(_build_sequence_summary(editable, yaml_data))
            else:
                value = smart_get_value(yaml_data, editable.editable.yaml_path)
                display = _format_value(editable, value, yaml_data)
                if display is not None:
                    parts.append(f"<dl><dt>{_summary_text(editable.label)}</dt><dd>{_summary_text(display)}</dd></dl>")

    _collect_summary(section.editables)

    return "\n".join(parts) if parts else "<p><em>Geen gegevens ingevuld</em></p>"


def _build_section_fields(
    section: FormSection,
    yaml_data: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build structured field data for a section; the template renders the HTML.

    Returns a list of field dicts with ``label``, ``value`` (a string or a list of strings),
    ``is_list`` for a bullet list, and ``html`` for a pre-rendered sequence. A section's own
    summary_fn returns (label, value) pairs, which land in the escaped path above.
    """
    from opi.forms.editables.service_path import smart_get_value

    fields: list[dict[str, Any]] = []

    def _collect(vis_list: list[Any]) -> None:
        for editable in vis_list:
            if str(editable.widget) == "group":
                _collect(editable.children or [])
            elif str(editable.widget) == "sequence":
                fields.append({"html": _build_sequence_summary(editable, yaml_data)})
            elif str(editable.widget) == "service_cards":
                value = smart_get_value(yaml_data, editable.editable.yaml_path)
                if editable.editable.summarizer:
                    # The same gate as every other leaf: a summarizer decides this field's
                    # summary, leaving it out included. Only the plain case keeps the
                    # bullet-list rendering below, which is what the cards look like.
                    display = _format_value(editable, value, yaml_data)
                    if display is not None:
                        fields.append({"label": editable.label, "value": display, "is_list": False})
                    continue
                labels = _resolve_service_labels(editable, value, yaml_data)
                if labels:
                    fields.append({"label": "Services", "value": labels, "is_list": True})
            else:
                value = smart_get_value(yaml_data, editable.editable.yaml_path)
                display = _format_value(editable, value, yaml_data)
                if display is not None:
                    fields.append({"label": editable.label, "value": display, "is_list": False})

    if section.summary_fn:
        # (label, value) pairs, like any other field; the template escapes them.
        fields.extend(
            {"label": label, "value": value, "is_list": False} for label, value in section.summary_fn(yaml_data)
        )
    else:
        _collect(section.editables)

    return fields


def _resolve_service_labels(
    editable: EditableVisualizer,
    value: Any,  # noqa: ANN401  # a project-file value
    yaml_data: dict[str, Any] | None = None,
) -> list[str]:
    """Resolve selected service values to their display labels."""
    from opi.forms.visualizers.bridge import resolve_options_for_editable

    if value is None or value == "" or value == []:
        return []

    if editable.editable.converter:
        value = editable.editable.converter.view(value, context_data=yaml_data)

    try:
        options = resolve_options_for_editable(editable)
    except Exception:  # noqa: BLE001  # the outer boundary of this page; it must still render
        return [str(v) for v in value] if isinstance(value, list) else [str(value)]

    label_map = {str(opt.get("value", "")): opt.get("label", str(opt.get("value", ""))) for opt in options}
    items = value if isinstance(value, list) else [value]
    return [label_map.get(str(v), str(v)) for v in items]


def _build_sequence_summary(
    editable: EditableVisualizer,
    yaml_data: dict[str, Any],
) -> str:
    """Build HTML summary for a sequence editable (components, users, etc.)."""
    from opi.forms.editables.service_path import smart_get_value

    base_path = editable.editable.yaml_path
    items = smart_get_value(yaml_data, base_path)

    if not items or not isinstance(items, list):
        return f"<p><em>Geen {_summary_text(editable.label.lower())}</em></p>"

    children = editable.children or []
    parts: list[str] = []

    for i, item in enumerate(items):
        if not isinstance(item, dict):
            parts.append(f"<div class='wizard-review__seq-item'><strong>{_summary_text(item)}</strong></div>")
            continue

        item_label = _get_item_label(item, children, i)
        item_parts: list[str] = []

        for child in children:
            if str(child.widget) == "sequence":
                # A nested sequence navigates by its full relative path, which handles the
                # {filter} syntax.
                from opi.forms.editables.path import get_value

                relative_path = _child_key(child)
                child_items = get_value(item, relative_path)
                if child_items and isinstance(child_items, list):
                    summaries = []
                    for ci in child_items:
                        if isinstance(ci, dict) and child.children:
                            parts_ci = []
                            for cc in child.children:
                                cc_key = cc.editable.yaml_path.split("/")[-1].split("[")[0]
                                cc_val = ci.get(cc_key)
                                # Through _format_value like every other leaf, so a
                                # summarizer holds one level deeper too.
                                cc_display = _format_value(cc, cc_val, yaml_data)
                                if cc_display is not None:
                                    parts_ci.append(cc_display)
                            # Nothing left to show for this item: leave it out, rather than
                            # printing the raw dict and dumping what a summarizer just hid.
                            if parts_ci:
                                summaries.append(" - ".join(parts_ci))
                        else:
                            summaries.append(str(ci))
                    formatted = ", ".join(summaries)
                    if formatted:
                        item_parts.append(f"<dt>{_summary_text(child.label)}</dt><dd>{_summary_text(formatted)}</dd>")
                continue

            child_key = _child_key(child)
            # get_value for a path with {filter} syntax, because _nested_get only handles
            # plain dict keys.
            if "{" in child_key:
                from opi.forms.editables.path import get_value

                value = get_value(item, child_key)
            else:
                value = _nested_get(item, child_key)
            display = _format_value(child, value, yaml_data)
            if display is not None:
                item_parts.append(f"<dt>{_summary_text(child.label)}</dt><dd>{_summary_text(display)}</dd>")

        if item_parts:
            parts.append(
                f"<div class='wizard-review__seq-item'>"
                f"<strong>{_summary_text(item_label)}</strong>"
                f"<dl>{''.join(item_parts)}</dl>"
                f"</div>"
            )
        else:
            parts.append(f"<div class='wizard-review__seq-item'><strong>{_summary_text(item_label)}</strong></div>")

    return (
        f"<div class='wizard-review__sequence'>"
        f"<p class='wizard-review__seq-heading'>"
        f"<strong>{_summary_text(editable.label)}</strong> ({len(items)})"
        f"</p>"
        f"{''.join(parts)}"
        f"</div>"
    )


def _get_item_label(item: dict[str, Any], children: list[Any], index: int) -> str:
    """Derive a display label for a sequence item."""
    for key in ("name", "display-name", "email", "reference"):
        if item.get(key):
            return str(item[key])
    # Fall back to the first required child.
    for child in children:
        if child.editable.required:
            child_key = _child_key(child)
            val = _nested_get(item, child_key)
            if val:
                return str(val)
    return f"Item {index + 1}"


def _child_key(child: EditableVisualizer) -> str:
    """Extract the relative key from a child editable's yaml_path.

    E.g. ``components[*]/resources/cpu/request`` → ``resources/cpu/request``
    """
    path = child.editable.yaml_path
    if "[*]/" in path:
        path = path.split("[*]/", 1)[-1]
    path = path.replace("[*]", "")
    return path


def _nested_get(data: dict[str, Any], path: str) -> Any:  # noqa: ANN401  # a project-file value
    """Get a value from nested dict using slash-separated path."""
    current: Any = data
    for key in path.split("/"):
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
    return current


def _format_value(editable: EditableVisualizer, value: Any, yaml_data: dict[str, Any] | None = None) -> str | None:  # noqa: ANN401  # a project-file value
    """Format a value for display in the summary.

    Returns None if the value is empty/unset and should be omitted.
    """
    # A summarizer decides everything about this field's summary, what happens when it is
    # empty included, hence before the empty check. It is the only hook that can say "do not
    # show this at all": returning None from a converter's view() lands in str() further down
    # and prints the word "None".
    if editable.editable.summarizer:
        return editable.editable.summarizer.summarize(value, context_data=yaml_data) or None

    if value is None or value == "" or value == []:
        return None

    # A key-value editor can hold secrets, so its values never go in the summary. A
    # widget-level backstop next to the per-field summarizer above, so a key_value field added
    # later is covered without anyone having to declare it.
    if str(editable.widget) == "key_value":
        return None

    if editable.editable.converter:
        value = editable.editable.converter.view(value, context_data=yaml_data)

    if editable.editable.values_provider and str(editable.widget) in (
        "select",
        "radio",
        "checkbox_group",
        "service_cards",
    ):
        return _resolve_option_labels(editable, value)

    if isinstance(value, bool):
        return "Ja" if value else "Nee"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        formatted = ", ".join(f"{k}={v}" for k, v in value.items())
        return formatted or None

    display = str(value)
    return display or None


def _resolve_option_labels(editable: EditableVisualizer, value: Any) -> str:  # noqa: ANN401  # a project-file value
    """Look up display labels for select/radio/checkbox values."""
    from opi.forms.visualizers.bridge import resolve_options_for_editable

    try:
        options = resolve_options_for_editable(editable)
    except Exception:  # noqa: BLE001  # the outer boundary of this page; it must still render
        # The provider failed: fall back to the raw value.
        return str(value) if not isinstance(value, list) else ", ".join(str(v) for v in value)

    label_map = {str(opt.get("value", "")): opt.get("label", str(opt.get("value", ""))) for opt in options}

    if isinstance(value, list):
        labels = [label_map.get(str(v), str(v)) for v in value]
        return ", ".join(labels)

    return label_map.get(str(value), str(value))


@wizard_router.post("/{flow_id}/submit", response_model=None)
@action(Action.WIZARD_USE)
@requires_sso
async def submit_wizard(request: Request, flow_id: str) -> HTMLResponse | RedirectResponse:
    """Final submission: validate all steps and create/update the project."""
    return await _do_submit(request, flow_id)


def _collect_all_editable_paths(editables: list[EditableVisualizer]) -> set[str]:
    """Recursively collect yaml_paths from editables, including group children."""
    paths: set[str] = set()
    for vis in editables:
        paths.add(vis.editable.yaml_path)
        if vis.children:
            paths.update(_collect_all_editable_paths(vis.children))
    return paths


def _section_has_errors(
    section_paths: set[str],
    errors: dict[str, list[str]],
) -> bool:
    """Whether any error key belongs to this section.

    Section editables use wildcard paths (``users[*]/email``) while error keys use concrete
    indices, so both sides are normalised by replacing ``[<digit>]`` with ``[*]``.
    """
    import re

    index_pattern = re.compile(r"\[\d+\]")
    normalised_section = {index_pattern.sub("[*]", p) for p in section_paths}

    for error_path in errors:
        normalised = index_pattern.sub("[*]", error_path)
        if normalised in normalised_section:
            return True
    return False


def _schema_path_to_editable_path(field_path: str) -> str:
    """Rewrite a schema field path as an editable yaml_path.

    The schema names a list item with a path segment of its own
    (``components/0/command``); editables index the field they hang under
    (``components[0]/command``). Same location, two notations.
    """
    parts: list[str] = []
    for part in field_path.split("/"):
        if part.isdigit() and parts:
            parts[-1] = f"{parts[-1]}[{part}]"
        else:
            parts.append(part)
    return "/".join(parts)


def _locate_schema_error(
    sections: list[FormSection],
    field_path: str,
) -> tuple[FormSection, str] | None:
    """Find the step and editable path a schema violation belongs to.

    Returns None when no step owns the field, because the violation sits on a block rather than
    on a field, or on something the wizard does not edit. The caller then shows the message at
    step level: not being able to place it is no reason to drop it.
    """
    editable_path = _schema_path_to_editable_path(field_path)
    for section in sections:
        if _section_has_errors(_collect_all_editable_paths(section.editables), {editable_path: []}):
            return section, editable_path
    return None


def _validation_message_without_values(error: Exception) -> str:
    """Describe a rejection to the user without repeating the rejected value.

    A ``ProjectSchemaError`` message quotes the instance, because jsonschema puts it there.
    That is fine deep in the write path but not here: the edit flow validates the form data
    MERGED with the stored project, so the offending value can be a stored secret that this
    handler would echo into the browser and into the log. Field path plus reason says the same
    thing to a user.

    A ``ProjectIntegrityError`` carries no reason and names structure and not values, so its
    own message is used as is.
    """
    reason = getattr(error, "reason", None)
    if not reason:
        return str(error)
    field_path = getattr(error, "field_path", None) or "(onbekend)"
    return f"Veld '{field_path}' voldoet niet aan het projectschema: {reason}."


#: What a field gets when the schema rejected it. A constant on purpose: this text is
#: rendered into step_html, which is re-rendered as a Jinja template downstream, so
#: nothing derived from user input may go here. The explanation goes in global_errors.
SCHEMA_FIELD_MARKER = "Deze waarde is afgekeurd door het projectschema; zie de melding bovenaan deze stap."


def _validate_finished_project(data: dict[str, Any], *, project_name: str) -> None:
    """Schema-check a FINISHED project file while the wizard can still show the error.

    Call this at the single point where *data* is the complete file about to be handed to the
    storage layer, not earlier: before that the create flow still adds to it, so an earlier
    check would reject a file that was merely unfinished, and after it the wizard is gone and
    the same rejection surfaces from the git step with nothing left to go back to.

    Validates exactly what the storage layer validates, with no migration applied first,
    because the write path applies none either: validating a migrated copy would let the wizard
    approve a file the store then rejects.

    Reaching this with an invalid file is a bug in the form and not user error: a field wrote
    something the schema forbids without a validator saying so, and the field path in the
    warning is where that missing validation belongs.

    Raises:
        ProjectSchemaError: with ``field_path`` set when the violation was locatable.
    """
    try:
        validate_project_schema(data)
    except ProjectSchemaError as e:
        # Log the reason and never the message: the message quotes the rejected value, and
        # the edit flow validates the file MERGED with the stored project, so that value can
        # be a secret.
        logger.warning(
            "Wizard built an invalid project file for %s (field=%s): %s -- a form field wrote a "
            "value the schema rejects, so validation is missing on that field",
            project_name or "(new)",
            e.field_path or "(unknown)",
            e.reason or "(unknown reason)",
        )
        raise


class _FlowValidation(NamedTuple):
    """The outcome of a whole-flow validation; see ``_validate_whole_flow``."""

    error_response: Response | None
    final_data: dict[str, Any]
    yaml_data: dict[str, Any]
    all_editables: list[Any]
    active_sections: list[Any]
    enforcer_context: dict[str, Any]
    processor: EditableFormProcessor


async def _validate_whole_flow(request: Request, flow_id: str, state: WizardState) -> _FlowValidation:
    """Validate EVERY active section against the merged wizard data.

    The validation half of the final submission, shared with the jump to the summary, which may
    skip a step but no required value. On errors, ``error_response`` is the re-render of the
    first failing section and the rest of the result is unusable; otherwise the result carries
    the validated ``final_data`` and the context the final submission needs.
    """
    flow = get_flow(flow_id)
    active_sections = resolve_active_sections(flow, state.step_data)

    all_editables = []
    for section in active_sections:
        all_editables.extend(section.editables)

    yaml_data = state.get_merged_data()
    processor = EditableFormProcessor()

    # Strip the values of fields hidden by depends_on or show_when.
    processor.clear_hidden_depends_on(all_editables, yaml_data)

    # Compute the derived values.
    processor.apply_dependent_generators(all_editables, yaml_data)

    enforcer_context = {"project_name": state.project_name, "edit_mode": state.is_edit}

    def _fail(errors: dict[str, list[str]], global_errors: list[str]) -> _FlowValidation:
        # Navigate to the first section that has errors.
        error_section = active_sections[0]
        if errors:
            for section in active_sections:
                section_paths = _collect_all_editable_paths(section.editables)
                if _section_has_errors(section_paths, errors):
                    error_section = section
                    break
        state.current_step = error_section.section_id
        save_wizard_state(request, state)

        step_html = _render_step_html(
            request,
            error_section,
            yaml_data=yaml_data,
            errors=errors,
            edit_mode=state.is_edit,
        )
        context = _build_step_context(
            request,
            flow_id,
            error_section,
            step_html,
            errors=errors,
            global_errors=global_errors,
        )
        return _FlowValidation(
            _step_response(request, context), {}, yaml_data, all_editables, active_sections, enforcer_context, processor
        )

    # Validate and build the final YAML in one pass, with the merged yaml_data as both the
    # submitted values and the base. WITHOUT stripping transients, because the generators may
    # still need them.
    final_data, errors = await processor.process_json_submission(
        yaml_data,
        all_editables,
        yaml_data,
        edit_mode=state.is_edit,
        enforcer_context=enforcer_context,
        strip_transients=False,
    )
    if errors:
        logger.warning("Final validation failed: %s", errors)
        return _fail(errors, ["Er zijn nog validatiefouten. Controleer de gemarkeerde velden."])

    enforce_field_errors: dict[str, list[str]] = {}
    global_errors = await processor.enforce_sections(
        yaml_data, active_sections, enforcer_context=enforcer_context, field_errors=enforce_field_errors
    )
    if global_errors or enforce_field_errors:
        logger.warning("Section enforcement failed: global=%s field=%s", global_errors, enforce_field_errors)
        return _fail(enforce_field_errors, global_errors)

    return _FlowValidation(None, final_data, yaml_data, all_editables, active_sections, enforcer_context, processor)


async def _do_submit(
    request: Request,
    flow_id: str,
) -> HTMLResponse | RedirectResponse:
    """Execute the final wizard submission."""
    state = get_wizard_state(request)
    if not state or state.flow_id != flow_id:
        logger.warning("Wizard session lost on submit (flow=%s), redirecting to start", flow_id)
        return RedirectResponse(
            url=f"/forms/wizard/{flow_id}",
            status_code=303,
        )

    flow = get_flow(flow_id)
    validation = await _validate_whole_flow(request, flow_id, state)
    if validation.error_response is not None:
        return validation.error_response
    final_data = validation.final_data
    all_editables = validation.all_editables
    active_sections = validation.active_sections
    enforcer_context = validation.enforcer_context
    processor = validation.processor

    _prune_empty_dicts(final_data)

    # Form context is not project data. The template layer carries keys that exist only to
    # feed the form, and the final submission is built from the whole merged view, so without
    # this they would be written to the project file, where ``additionalProperties: false`` at
    # the root forbids them. One rule and no list to maintain: a leading underscore at the top
    # level means "for the form", as it does for transients and for the virtual services root.
    for key in [key for key in final_data if key.startswith("_")]:
        del final_data[key]

    try:
        # The PRE_SAVE hooks run while the transients are still available; StripTransientsHook
        # has order 999 and removes them last.
        from opi.forms.editables.editable import Editable, FormState, WidgetType
        from opi.forms.editables.hooks import StripTransientsHook
        from opi.forms.editables.lifecycle import run_hooks
        from opi.forms.visualizers.visualizer import EditableVisualizer

        # StripTransientsHook as a system-level hook on a virtual editable.
        strip_hook_editable = EditableVisualizer(
            editable=Editable(
                yaml_path="_system/strip-transients",
                hooks={FormState.PRE_SAVE: StripTransientsHook(all_editables)},
            ),
            widget=WidgetType.HIDDEN,
            label="",
        )
        all_with_system = [*all_editables, strip_hook_editable]
        from opi.forms.editables.resolvers import build_resolver_map

        hook_context = {**enforcer_context, "resolvers": build_resolver_map(all_editables)}
        await run_hooks(FormState.PRE_SAVE, all_with_system, final_data, hook_context)

        if state.project_name:
            return await _save_existing_project(
                request, state.project_name, final_data, active_sections=active_sections
            )
        else:
            # Create mode: the staged uploads go into the project-level attachments catalog
            # before the generators run, because the combine generator encrypts them once the
            # AGE keypair exists.
            staged_attachments = state.staged_attachments or {}
            if staged_attachments:
                merge_staged_attachments(final_data, staged_attachments)

            # The generators set the name, the AGE keys and the staged attachments; the
            # deployment is assembled after, because it needs the name for the namespace.
            final_data = processor.apply_generators(flow.generated_editables, final_data)
            _assemble_deployment(final_data)
            return await _start_project_creation(request, final_data)
    except (ProjectSchemaError, ProjectIntegrityError, ConflictError) as e:
        # Re-render the wizard with the refusal instead of a 500. A ConflictError carries no
        # field and no reason: its own message explains what to do, as it does in the modal.
        field_path = getattr(e, "field_path", None)
        message = _validation_message_without_values(e)
        logger.warning("Wizard save refused for %s: %s", state.project_name or "(new)", message)
        located = _locate_schema_error(active_sections, field_path) if field_path else None
        if located is not None:
            error_section, editable_path = located
            # Mark the field, but keep the text out of it. A field error is rendered into
            # step_html, and wizard_step.html.j2 pipes that through process_components, which
            # renders it a SECOND time as a Jinja template, so a message carrying user input
            # there is code execution. The marker is a constant; the message itself goes in
            # global_errors, which the template renders once, autoescaped.
            field_errors = {editable_path: [SCHEMA_FIELD_MARKER]}
            global_errors = [message]
        else:
            # Not placeable, a violation on a whole block or on a field no step owns: show it
            # at step level on the step the user submitted from, falling back to the last step
            # when current_step names a step that is no longer active, so the message always
            # has somewhere to land.
            error_section = next(
                (section for section in active_sections if section.section_id == state.current_step),
                active_sections[-1],
            )
            field_errors = {}
            global_errors = [message]
        state.current_step = error_section.section_id
        save_wizard_state(request, state)
        # The MERGED state and not final_data: the latter is the storage form, with the
        # transients stripped, the generators applied and the underscore keys removed, and the
        # step has to show the fields as the user left them. Every other call of
        # _render_step_html uses the same source.
        step_html = _render_step_html(
            request, error_section, yaml_data=state.get_merged_data(), errors=field_errors, edit_mode=state.is_edit
        )
        context = _build_step_context(
            request, flow_id, error_section, step_html, errors=field_errors, global_errors=global_errors
        )
        return _step_response(request, context)
    except Exception:
        logger.exception("Wizard submit failed")
        raise


async def _save_existing_project(
    request: Request,
    project_name: str,
    data: dict[str, Any],
    *,
    active_sections: list[Any],
) -> HTMLResponse:
    """Save updated data to an existing project."""
    from opi.manager.project_manager import ProjectManager
    from opi.web.project_edit_security import refuse_immutable_changes

    # TOCTOU recheck on the mutating request.
    authorize(request, action=Action.PROJECT_EDIT, project_name=project_name)

    # Only the paths this flow's editables declare, as the modal save does: the session was
    # seeded when the wizard opened, and a shallow merge of that snapshot reverted whatever
    # landed in git since. The flow declares ``clusters`` and may submit it unchanged, so the
    # immutable fields are guarded on the VALUE and not on the declaration.
    write_paths = flow_write_paths(active_sections)

    def apply(stored: dict[str, Any]) -> dict[str, Any]:
        refuse_immutable_changes(stored, data)
        return apply_write_paths(stored, data, write_paths)

    change = ProjectChange(
        project_name=project_name,
        apply=apply,
        commit_message=f"Update project {project_name} via wizard",
    )
    # Closed explicitly, so its temp git clone is cleaned up.
    project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")
    try:
        result = await update_project(change, RequestAuthority(request, project_name), project_manager)
    except StepUpRequiredError as refusal:
        # A bare 403 and not the step-up dialog: that one hangs on the shared modal of the
        # project page, which the full-page wizard does not have.
        raise HTTPException(
            status_code=403,
            detail=f"{refusal} Doe dit vanaf de projectpagina, daar vraagt het portaal erom.",
        ) from refusal
    finally:
        await project_manager.close()

    clear_wizard_state(request)
    logger.info("Project %s updated via wizard", project_name)

    target = f"/projects/{project_name}/details"
    if result.needs_rollout:
        from opi.http.task_helpers import queue_rollout

        task_id = await queue_rollout(request, project_name, result.rollout_deployments)
        target = f"/projects/progress/{task_id}"

    # HX-Redirect, so HTMX does a full-page navigation instead of swapping the redirect
    # target into the wizard frame.
    response = HTMLResponse(content="", status_code=200)
    response.headers["HX-Redirect"] = target
    return response


async def _start_project_creation(
    request: Request,
    data: dict[str, Any],
) -> HTMLResponse:
    """Start the background project creation through the existing pipeline.

    Generates the YAML content from the wizard data and feeds it into the task worker, which
    handles the git commit, the deployment, the ArgoCD sync and the progress tracking.
    """
    project_name = data.get("name", "")
    if not project_name:
        raise HTTPException(status_code=400, detail="Projectnaam is verplicht")

    # The wizard editables still write component-level service config in the legacy
    # name-as-key shape, so it is normalized to the uniform {reference, config} form and the
    # created file is born in the current schema. The same normalizer the migration uses.
    normalize_service_entries(data)

    # Ensure multiline AGE-encrypted values use literal block scalars
    apply_literal_scalars(data)

    # A new project can be created in a state that already asks for an approval, so creating it
    # records that request, with the same function as the edit wizard.
    ensure_approval_requests(data)

    # The file is complete here: generators ran, the deployment is assembled, staged
    # attachments are merged and the service entries are normalized. This is the last
    # moment the wizard still exists, so it is where the schema is checked.
    _validate_finished_project(data, project_name=project_name)

    yaml_content = dump_yaml_to_string(data)

    # The task worker handles the git commit and the processing.
    from opi.http.task_helpers import create_async_task

    task = await create_async_task(
        request=request,
        task_type="create_project",
        project_name=project_name,
        payload={"project_name": project_name, "yaml_content": yaml_content, "is_new_project": True},
        max_attempts=1,
    )
    task_id = str(task["task_id"])
    logger.info("Created V2 project creation task for %s (task=%s)", project_name, task_id)

    # Only now is the work handed over, so only now may the wizard session go: clearing it
    # earlier throws away everything the user typed while the submission can still fail.
    clear_wizard_state(request)

    # HX-Redirect, so HTMX does a full-page navigation instead of swapping the progress page
    # into the wizard frame.
    response = HTMLResponse(content="", status_code=200)
    response.headers["HX-Redirect"] = f"/projects/progress/{task_id}"
    return response
