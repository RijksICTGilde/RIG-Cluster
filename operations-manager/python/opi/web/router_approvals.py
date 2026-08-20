"""Admin routes for the generic, catalog-driven approver interface (RC-5).

Lists pending approval items across all projects and drives the approve/deny modal.
The items + verdicts flow through the catalog ApprovalSpecs (opi/services/approvals.py),
so this router is not domain-specific: publish-on-web declares a domain and a subdomain
approval, send-email declares one for the use of the service itself.
Historically ``router_subdomain_admin`` at ``/admin/subdomains``.

Provides a listing page of all approval requests across projects, and admin-scoped modal
wizard endpoints for approving/denying them. Reuses the editable form framework — no
custom form processing.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.responses import Response

from opi.core.auth_decorators import require_platform_admin, requires_sso
from opi.core.project_schema import ProjectIntegrityError, ProjectSchemaError
from opi.forms import FormRenderer, get_default_nl_translator
from opi.forms.visualizers.flows import get_flow
from opi.forms.widgets.lotc import LOTCWidgetAdapter
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
from opi.services.registry import get_service
from opi.services.services_enums import ServiceType
from opi.web.lotc_switch import render_fragment
from opi.web.menu import get_menu_items
from opi.web.navigation_lotc import to_nldd_icon
from opi.web.router_wizard import _apply_literal_scalars

logger = logging.getLogger(__name__)

approvals_router = APIRouter(prefix="/admin/approvals", tags=["approvals"])

FLOW_ID = "admin-approval"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_renderer() -> FormRenderer:
    """De formulierrenderer voor dit verzoek; alleen de adapter wisselt.

    Zelfde keuze als in ``opi/web/router_detail_edit.py``: dezelfde editables, dezelfde
    waarden, dezelfde foutmeldingen, andere widgets. Een dialoog uit twee
    componentsystemen rendert niet.
    """
    return FormRenderer(
        widget_adapter=LOTCWidgetAdapter(),
        translator=get_default_nl_translator(),
    )


def _render_section_html(
    section: Any,
    yaml_data: dict[str, Any],
    errors: dict[str, list[str]] | None = None,
) -> str:
    """Render form fields for a section.

    De adapter rendert meteen af; die HTML mag daarna NIET nog een keer door een
    sjabloonrender, want hij draagt wat iemand in het formulier heeft getypt.
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
        # Deze dialoog draagt zijn eigen titel ("Domeingoedkeuring - <project>", gezet door
        # openApprovalDialog in bg/admin-approvals.html.j2), en die zegt hetzelfde als de
        # kop van de sectie plus de projectnaam. Met allebei stonden er twee koppen boven
        # elkaar. De flow heeft hier maar EEN stap, dus de sectiekop vertelt ook niet waar
        # je bent. Alleen hier uit; de bewerkdialogen van een project houden hem.
        "show_section_head": False,
        # Onze secties dragen Nederlandse ROOS-iconnamen; de LOTC-sjablonen hebben de
        # NLDD-woordenschat nodig.
        "nldd_icon": to_nldd_icon,
    }
    return render_fragment(
        request,
        template="bg/_modal-wizard-step.html.j2",
        context=context,
    )


def _modal_error(request: Request, melding: str, status_code: int) -> HTMLResponse:
    """Een weigering van de dialoog als leesbaar fragment, met de echte statuscode.

    De dialoog wordt door htmx gevuld, dus wat de route antwoordt is wat de gebruiker
    ziet. Een ``HTTPException`` levert hier JSON op en htmx wisselt bij een foutcode
    standaard niets in: samen is dat een venster dat opengaat en leeg blijft. Vandaar een
    fragment. De statuscode blijft staan - dat htmx hem toch toont, staat als
    ``htmx:beforeSwap``-haak in bg/admin-approvals.html.j2.
    """
    return HTMLResponse(
        content=render_fragment(
            request,
            template="bg/_modal-fout.html.j2",
            context={"request": request, "melding": melding},
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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


#: De statussen waarop gefilterd kan worden. De sleutel staat in de URL (``?status=``), het
#: label in de keuzelijst. Als lijst en niet als dict, omdat de VOLGORDE de volgorde in de
#: lijst is; ``""`` is alles en staat daarom vooraan.
#:
#: De sleutels zijn dezelfde als die in het projectbestand staan (``current_status``), en de
#: labels dezelfde als de badges in de tabel (``status_labels`` in het sjabloon). Twee lijsten
#: die hetzelfde zeggen zouden uit elkaar lopen; dat is hier nog niet opgelost, maar
#: tests/test_approvals_statusfilter.py legt vast dat ze gelijk blijven.
APPROVAL_STATUSSEN: list[tuple[str, str]] = [
    ("", "Alle statussen"),
    ("requested", "Aangevraagd"),
    ("approved", "Goedgekeurd"),
    ("denied", "Afgewezen"),
]


def filter_op_status(projects_data: list[dict[str, Any]], status: str) -> list[dict[str, Any]]:
    """Houd alleen de aanvragen met deze status over, en de projecten die er nog hebben.

    Een project waarvan geen enkele aanvraag overblijft valt weg: een projectpaneel met een
    lege tabel eronder leest als "dit project heeft niets", terwijl het er wel iets heeft dat
    je nu even niet ziet.

    Een lege of onbekende status filtert niet. Onbekend is bewust hetzelfde als leeg en niet
    "niets gevonden": ``?status=onzin`` in een gedeelde link hoort de lijst te tonen, niet een
    lege pagina die als een storing leest.
    """
    if not status or status not in {sleutel for sleutel, _ in APPROVAL_STATUSSEN if sleutel}:
        return projects_data

    gefilterd: list[dict[str, Any]] = []
    for project in projects_data:
        items = [item for item in project["approval_items"] if item.get("current_status") == status]
        if items:
            gefilterd.append({**project, "approval_items": items})
    return gefilterd


def groepeer_per_dienst(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bundel de aanvragen van een project per DIENST, met de naam en het icoon van die dienst.

    Een domeinaanvraag en een dienstaanvraag zijn verschillende dingen, en de lijst hoort
    dat te laten zien. De groepskop haalt zijn naam, icoon en kleur uit de
    ``ServiceDefinition`` in de registry en niet uit een lijstje in het sjabloon: dan
    verschijnt een vierde dienst met goedkeuring er vanzelf goed op.

    ``toon_soort`` is de generieke vorm van "voegt de soort-tag iets toe?". Bij
    publish-on-web onderscheidt hij domein van subdomein; bij een dienst met een enkele
    soort herhaalt hij alleen de groepskop, en dan blijft de kolom weg.

    De volgorde is die van de items zelf (dus die van de catalogus), zodat de lijst niet
    van run tot run wisselt. De TELLING blijft over ITEMS gaan -- ``approval_items`` blijft
    naast deze groepen bestaan, want "x van y aanvragen" telt aanvragen en geen groepen.
    """
    groepen: dict[str, dict[str, Any]] = {}
    for item in items:
        sleutel = item.get("service") or item.get("type") or ""
        groep = groepen.get(sleutel)
        if groep is None:
            groep = {"service": sleutel, "naam": item.get("label") or sleutel, "icoon": "", "aanvragen": []}
            try:
                definitie = get_service(ServiceType(sleutel)).definition
            except ValueError, KeyError:
                # Een item van een dienst die de registry niet (meer) kent. Het opschrift
                # van de spec is dan het beste dat er is; beter dan de rij weglaten.
                logger.warning(
                    "Goedkeuringsitem van onbekende dienst %r; groepskop valt terug op het opschrift", sleutel
                )
            else:
                groep["naam"] = definitie.name
                groep["icoon"] = definitie.icon
            groepen[sleutel] = groep
        groep["aanvragen"].append(item)

    for groep in groepen.values():
        groep["toon_soort"] = any(item.get("label") and item["label"] != groep["naam"] for item in groep["aanvragen"])
    return list(groepen.values())


@approvals_router.get("", response_class=HTMLResponse)
@requires_sso
async def list_subdomains(request: Request) -> Response:
    """List all approval requests across all projects."""

    user = require_platform_admin(request)

    # Pull the latest project data from git so an entry added externally
    # (manual yaml edit + push, or a request created elsewhere) shows up
    # on the admin overview instead of returning a stale in-memory cache.

    alle_projecten = _collect_all_projects_approval_data()

    # Filteren gebeurt HIER en niet in de browser: dan werkt het ook zonder JavaScript, is
    # een gefilterde lijst deelbaar als URL, en staat de gekozen waarde na een swap nog
    # steeds in de keuzelijst omdat de server hem meerendert. Zelfde opzet als het zoeken
    # en sorteren op /projects (opi/web/lotc_switch.py).
    status = (request.query_params.get("status") or "").strip()
    # Filteren op ITEMS, daarna pas groeperen: de teller onderaan telt aanvragen en geen
    # groepen, en een groep die na het filteren leeg is hoort er niet te staan.
    projects_data = [
        {**project, "approval_groups": groepeer_per_dienst(project["approval_items"])}
        for project in filter_op_status(alle_projecten, status)
    ]

    # Dezelfde gegevens, twee weergaven; zie opi/web/lotc_switch.py. Alleen de LIJST gaat
    # mee: het beoordelingsvenster erin haalt zijn inhoud op bij de modal-wizard hieronder,
    # en die blijft roos renderen zolang de wizard zelf niet om is.
    from opi.web.lotc_switch import build_lotc_admin, render

    return render(
        request,
        template="bg/admin-approvals.html.j2",
        context={
            "request": request,
            "menu_items": get_menu_items(user),
            "projects_data": projects_data,
            # De ONGEFILTERDE telling gaat mee, zodat de lege lijst kan zeggen of er niets
            # is of alleen niets met deze status. Dat verschil is het enige dat een
            # gefilterde lege pagina bruikbaar maakt.
            "approvals_totaal": sum(len(p["approval_items"]) for p in alle_projecten),
            "approvals_getoond": sum(len(p["approval_items"]) for p in projects_data),
            "approval_status": status,
            "approval_statussen": APPROVAL_STATUSSEN,
            "success_message": request.query_params.get("success"),
            **build_lotc_admin(user=user, current_path="/admin/approvals"),
        },
    )


@approvals_router.get("/{project_name}/modal-wizard/{flow_id}", response_class=HTMLResponse)
@requires_sso
async def modal_wizard_init(request: Request, project_name: str, flow_id: str) -> HTMLResponse:
    """Initialize the domain approval modal wizard for a project."""
    user = require_platform_admin(request)

    # Deze drie weigeringen komen IN de dialoog terecht, dus ze gaan als fragment terug en
    # niet als HTTPException: de gebruiker heeft net op "Beheren" geklikt en het venster
    # staat al open.
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

    # Seed wizard state with approval items
    seed_data: dict[str, Any] = {"_approval_items": approval_items}

    wizard_token, state = init_modal_state_tokenized(
        flow_id=flow_id,
        first_step=first_section.section_id,
        active_sections=[first_section.section_id],
        project_name=project_name,
    )
    state.step_data = {first_section.section_id: seed_data}
    state.base_data = {"_admin_email": user.get("email", "")}
    # The version this approval decision is being taken on; it travels with the save so
    # a change made elsewhere in the meantime is merged instead of overwritten.
    state.base_version = await get_project_store().version_of(f"projects/{project_name}.yaml")
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
    user = require_platform_admin(request)

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
            render_data = _reseed_approval_items(state.get_merged_data(), project_name)
            step_html = _render_section_html(first_section, render_data)
            # Say what was blocked. The bare validation message describes the resulting
            # state ("het subdomein is afgewezen"), which reads as a report of the
            # approver's own action instead of a refusal to record it.
            message = f"Het besluit is niet opgeslagen, want het project is daarna niet geldig: {e}"
            rendered = _render_modal_step(
                request, wizard_token, state, first_section, step_html, project_name, global_errors=[message]
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
            "base_version": state.base_version,
        },
        max_attempts=1,
    )
    logger.info(
        "Domain approval for %s scoped redeploy to deployment(s): %s",
        project_name,
        deployment_names or "(none affected)",
    )
    task_id = str(task["task_id"])

    rendered = render_fragment(
        request,
        template="bg/_modal-wizard-progress.html.j2",
        context={"task_id": task_id, "project_name": project_name},
    )

    clear_modal_state_by_token(wizard_token)
    return HTMLResponse(content=rendered)
