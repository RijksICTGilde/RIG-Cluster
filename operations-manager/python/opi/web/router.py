"""The HTML pages of the portal, and the sub-routers that make up the rest of them."""

import asyncio
import copy
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.exc import SQLAlchemyError

from opi.core.authz.actions import Action, action
from opi.core.authz.matrix import columns
from opi.core.cluster_config import get_prefixed_namespace
from opi.core.dns_config import ROUTER_HOSTNAMES, router_addresses_for, router_hostname_for
from opi.core.errors import current_reference, log_render_failure, reference_for, with_reference
from opi.http.auth.auth_decorators import get_current_user, requires_sso, requires_step_up
from opi.http.auth.authz import enforce_route_action, may
from opi.http.render.lotc_switch import (
    STANDAARD_TAB,
    TABS_MET_DEPLOYMENT,
    TABS_MET_VOORWAARDE,
    audit_pagina,
    build_deployment_status_column,
    build_lotc_dashboard,
    build_lotc_introductie,
    build_lotc_project_details,
    build_lotc_projects,
    deployment_pagina_adres,
    kies_deployment,
    project_tab_url,
    render,
    render_fragment,
    tab_from_path,
)
from opi.http.render.menu import get_menu_items
from opi.http.render.navigation_lotc import get_navigation
from opi.http.render.templates_lotc import templates_lotc
from opi.http.task_helpers import require_removable_deployment
from opi.services.api_token_service import is_project_admin
from opi.services.argocd_operation import (
    STUCK_OPERATION_THRESHOLD_SECONDS,
    format_duration,
    is_stuck,
    operation_age_seconds,
    read_sync_operation,
    seconds_since,
)
from opi.services.argocd_overview import get_project_argocd_statuses
from opi.services.catalog.deployment_health.disabled import deployment_disabled_state
from opi.services.component_values import ComponentValuesError
from opi.services.component_values import decode as decode_component_values
from opi.services.config_location import project_step_config_hint, selection_labels
from opi.services.deployment_state import DeploymentState, collect_deployment_state
from opi.services.project import Project
from opi.services.project_authorization import (
    get_user_roles_for_project,
    is_user_authorized_for_project,
)
from opi.services.project_members import human_members
from opi.services.project_store import get_project_store
from opi.services.registry import collect_service_routers, find_deployment_action
from opi.services.step_up import StepUpAction, is_elevated_admin
from opi.services.user_otp_service import UserOtpService
from opi.services.user_service import get_user_service
from opi.services.zad_map import platform_graph, project_graph
from opi.services.zad_map.platform import current_cluster_folder
from opi.services.zad_map.project import usage_targets
from opi.services.zad_map.usage import measure_usage
from opi.utils.age import decrypt_password_smart, get_decoded_project_private_key
from opi.utils.csrf import ensure_csrf_token
from opi.utils.totp import totp_now
from opi.utils.url_paths import local_redirect_path
from opi.utils.yaml_util import load_yaml_from_string
from opi.web.project_actions import build_project_action
from opi.web.security_policy import security_policy_html
from opi.web.step_labels import step_label
from opi.web.task_progress import create_task_and_render_progress, on_complete_for, render_progress_fragment

from ..utils.age import decrypt_age_content, is_age_encrypted
from .metrics_explorer_router import metrics_explorer_router
from .router_approvals import approvals_router
from .router_attachments import attachments_router
from .router_detail_edit import detail_edit_router
from .router_env_vars import env_vars_web_router
from .router_otp import otp_router
from .router_rolmatrix import rolmatrix_router
from .router_self_service import check_subdomain_availability_web
from .router_shared_services import shared_services_router
from .router_step_up import step_up_router
from .router_tasks import tasks_router
from .router_tokens import token_web_router, tokencontext
from .router_usage import usage_router
from .router_user_admin import user_admin_router
from .router_wizard import wizard_router
from .router_wizard_attachments import wizard_attachments_router
from .services_router import services_router

logger = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task[None]] = set()

#: The gate of the portal: every route with a project right passes it, without a handler
#: having to remember. See opi/http/auth/authz.py.
web_router = APIRouter(dependencies=[Depends(enforce_route_action)])

#: The preview and test pages, on a router of their own because opi/server.py registers them
#: only locally and in the sandbox.
dev_router = APIRouter()

web_router.include_router(services_router)
web_router.include_router(metrics_explorer_router)
web_router.include_router(detail_edit_router)
web_router.include_router(wizard_router)
web_router.include_router(user_admin_router)
web_router.include_router(usage_router)
web_router.include_router(shared_services_router)
web_router.include_router(rolmatrix_router)
web_router.include_router(approvals_router)
web_router.include_router(attachments_router)
web_router.include_router(wizard_attachments_router)
web_router.include_router(tasks_router)
web_router.include_router(otp_router)
web_router.include_router(step_up_router)
web_router.include_router(token_web_router)
web_router.include_router(env_vars_web_router)

# The routers the services themselves deliver: a service that owns a page block owns the
# endpoints that fill it, instead of leaving half the block behind in this router.
for _service_router in collect_service_routers():
    web_router.include_router(_service_router)


async def decrypt_helm_values(items: list[dict[str, Any]], name_key: str, where: str, private_key: str) -> None:
    """Make the ``helm-values`` of helm-chart or helmfile items readable for the page.

    The field has no type in the schema and occurs in two shapes in the project files: as an
    AGE block and as a plain tree, sometimes both in one project. So look at what is there
    first: only an AGE block goes through the decryption, the rest stays as it is. Decrypting
    unconditionally turns a tree into ``None``, and the template only shows the block for a
    mapping, so it then disappears from the screen without a word.
    """
    for item in items:
        value = item.get("helm-values")
        if not value:
            continue
        name = item.get(name_key, "unknown")
        if not isinstance(value, str):
            # Already a tree: nothing to decrypt, and nothing to lose.
            continue
        if not is_age_encrypted(value):
            # A string that is not an AGE block is YAML in plain text.
            item["helm-values"] = load_yaml_from_string(value)
            continue
        try:
            item["helm-values"] = load_yaml_from_string(await decrypt_age_content(value, private_key))
            logger.info(f"Decrypted helm-values for {where} '{name}'")
        except Exception as e:
            logger.warning(f"Failed to decrypt helm-values for {where} '{name}': {e}")
            item["helm-values"] = None


def _render_own_domain(request: Request, *, current_path: str) -> Response:
    """The explanation of how to point your own domain here.

    Two entrances, one page: the bare router name, where a visitor lands from someone else's
    DNS record, and the menu item, where a project owner looks it up. The router name shown
    follows the zone you are looking at.
    """
    user = get_current_user(request)
    router_host = router_hostname_for(request.url.hostname)
    addresses = router_addresses_for(router_host)
    return render(
        request,
        template="bg/router.html.j2",
        context={
            "request": request,
            "menu_items": get_menu_items(user),
            "navigation": get_navigation(user, current_path=current_path),
            "router_host": router_host,
            # Empty when we do not know them for this name, as in the sandbox: the page then
            # shows only the CNAME form instead of addresses that do not hold there.
            "router_ipv4": addresses[0] if addresses else "",
            "router_ipv6": addresses[1] if addresses else "",
        },
    )


@web_router.get("/eigen-domein", response_class=HTMLResponse)
async def eigen_domein(request: Request) -> Response:
    """DELIBERATELY WITHOUT ``@requires_sso``: whoever points a domain here is often a DNS
    administrator of another organisation, without an account here, so a login wall would put
    the page out of reach of its own audience. ``tests/test_routerpagina.py`` guards that.
    """
    return _render_own_domain(request, current_path="/eigen-domein")


@web_router.get("/")
async def root(request: Request):
    """The front door: the dashboard when signed in, the introduction otherwise.

    DELIBERATELY WITHOUT ``@requires_sso``: the front door has to be readable without a
    session, or every visitor without rights lands on the login screen without ever having
    read WHAT this is.

    ON A ROUTER NAME IT STARTS SOMEWHERE ELSE. ``router.<zone>`` is the bare entrance of the
    cluster every other name CNAMEs to, so whoever types it met it in someone else's DNS
    record and has a different question than a ZAD user.

    Both redirect rather than render here, so each page keeps an address you can share and
    that works for someone who is already signed in.
    """
    if request.url.hostname in ROUTER_HOSTNAMES:
        return RedirectResponse(url="/eigen-domein", status_code=302)
    if get_current_user(request) is None:
        return RedirectResponse(url="/introductie", status_code=302)
    return RedirectResponse(url="/dashboard", status_code=302)


@web_router.get("/introductie", response_class=HTMLResponse)
async def introductie(request: Request):
    """What ZAD is, for whoever comes here for the first time.

    DELIBERATELY WITHOUT ``@requires_sso``: this is the page for someone who has no rights
    yet, the only group that really needs it, so a login wall would put it out of reach of its
    own audience. Being public is the whole point here and no omission;
    ``tests/test_introductiepagina.py`` guards that the decorator does not appear after all.

    The shell tolerates ``user=None``: ``get_navigation(None, ...)`` yields the base items plus
    a sign-in item instead of the account and sign-out ones.
    """
    user = get_current_user(request)
    return render(
        request,
        template="bg/introductie.html.j2",
        context={
            "request": request,
            "menu_items": get_menu_items(user),
            **build_lotc_introductie(user),
        },
    )


@web_router.get("/getting-started", response_class=HTMLResponse)
async def getting_started(request: Request):
    """The first steps on ZAD.

    DELIBERATELY WITHOUT ``@requires_sso``: public like /introductie, for people without
    access yet.
    """
    return _signpost(
        request, "getting-started.html.j2", "bg/getting-started.html.j2", "/getting-started", rollen=columns()
    )


@web_router.get("/permission-denied", response_class=HTMLResponse)
async def permission_denied(request: Request) -> HTMLResponse:
    """The permission-denied page, for a user who authenticated but is not authorized.

    DELIBERATELY WITHOUT ``@requires_sso``: this page is exactly for whoever has no rights.
    """
    user = request.session.get("user") if hasattr(request, "session") else None

    # One-shot denial reason set by the login callback (server-side flash, not a URL param).
    denied_reason = request.session.pop("denied_reason", None) if hasattr(request, "session") else None

    user_email = user.get("email", "unknown") if user else "anonymous"
    logger.warning(f"Permission denied page accessed by: {user_email}")

    from opi.http.render.navigation_lotc import get_navigation

    return render(
        request,
        template="bg/permission-denied.html.j2",
        context={
            "request": request,
            "user": user,
            "reason": denied_reason,
            "menu_items": get_menu_items(user),
            "navigation": get_navigation(user, current_path="/permission-denied"),
        },
    )


@web_router.get("/projects/new")
async def redirect_projects_new_to_wizard():
    """Redirect the legacy /projects/new to the wizard.

    DELIBERATELY WITHOUT ``@requires_sso``: a blind redirect gives nothing away, and the
    wizard behind it does ask for a session.
    """
    return RedirectResponse(url="/forms/wizard/create-project", status_code=302)


# SSO-protected, so the subdomain registry cannot be enumerated anonymously.
web_router.add_api_route("/subdomains/check", check_subdomain_availability_web, methods=["GET"])


def _progress_page_context(task: dict, task_id: str) -> dict:
    """The context the progress page and its polled fragment share.

    Both render the same fragment, so both build it here; the page only adds the shell.
    """
    project_name = task.get("project_name", "")
    context = _v2_task_to_template_context(task, project_name)
    context["task_id"] = task_id
    context["progress_url"] = f"/projects/progress/{task_id}/fragment"
    context["container_id"] = "project-progress"
    if task.get("task_type") == "create_project":
        context["success_message"] = "Project succesvol aangemaakt. Het is klaar voor gebruik."
    else:
        context["success_message"] = "Verwerking succesvol afgerond."
    if project_name:
        # Whether it finished or failed, the detail page is where the user goes next: to use
        # the project, or to fix what went wrong.
        context["on_complete"] = f"window.location.href='/projects/{project_name}/details'"
        context["on_complete_label"] = "Naar projectdetails"
    return context


@web_router.get("/projects/progress/{task_id}", response_class=HTMLResponse)
@action(Action.TASK_READ)
@requires_sso
async def project_progress_page(request: Request, task_id: str):
    """The project-creation progress page.

    A shell around the shared progress fragment: the first paint is server-rendered here, and
    htmx polls the fragment route below for the rest.
    """
    try:
        from opi.http.task_helpers import get_task_service

        task_service = get_task_service(request)
        task = await task_service.get_task(task_id)
        user = get_current_user(request)

        from opi.http.render.navigation_lotc import get_navigation

        if not task:
            return render(
                request,
                template="bg/project-progress-done.html.j2",
                context={
                    "request": request,
                    "title": "Taak niet beschikbaar",
                    "menu_items": get_menu_items(user),
                    "task_id": task_id,
                    "navigation": get_navigation(user, current_path="/projects"),
                },
            )

        _require_task_access(request, task, task.get("project_name", ""))

        context = _progress_page_context(task, task_id)
        context.update(
            {
                "request": request,
                "title": f"Voortgang: {context['project_name']}",
                "menu_items": get_menu_items(user),
            }
        )
        context["navigation"] = get_navigation(user, current_path="/projects")
        return render(
            request,
            template="bg/project-progress.html.j2",
            context=context,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Renderen van de voortgangspagina mislukt")
        raise HTTPException(
            status_code=500,
            detail="De voortgangspagina kon niet worden opgebouwd. Probeer het over een minuut opnieuw.",
        ) from e


@web_router.get("/projects/progress/{task_id}/fragment", response_class=HTMLResponse)
@action(Action.TASK_READ)
@requires_sso
async def project_progress_page_fragment(request: Request, task_id: str) -> HTMLResponse:
    """The progress fragment as the full page polls it.

    Same fragment as the modals use, only with the page's own poll URL and finish
    button. The page has no project name in its path, so the task supplies it and the
    same access rule applies as on the project-scoped fragment route.
    """
    from opi.http.task_helpers import get_task_service

    task_service = get_task_service(request)
    task = await task_service.get_task(task_id)
    if task is None:
        return HTMLResponse(content="<p>Taak niet gevonden</p>", status_code=404)

    _require_task_access(request, task, task.get("project_name", ""))

    # Rendered once on purpose; see render_progress_fragment for why a second pass over the
    # rendered HTML would execute task text as Jinja.
    #
    # The PAGE's own fragment and not the shared one from task_progress.py: the page puts
    # bg/_task-progress.html.j2 down, and this is the poll route that replaces that block.
    context = _progress_page_context(task, task_id)
    return HTMLResponse(content=render_fragment(request, template="bg/_task-progress.html.j2", context=context))


@web_router.post("/projects/delete/{project_name}", response_class=HTMLResponse)
@action(Action.PROJECT_DELETE)
@requires_sso
@requires_step_up(action=StepUpAction.PROJECT_DELETE)
async def delete_project_web(request: Request, project_name: str) -> HTMLResponse:
    """Delete a whole project from the UI, as a task you can follow.

    Deleting tears down git, ArgoCD, the namespace, databases and buckets, so it answers with
    the same shared progress fragment as reprocessing rather than leaving the page on an open
    POST for minutes.
    """
    user = get_current_user(request)
    user_email = user.get("email", "").lower()

    logger.info(f"Web project deletion request for '{project_name}' by user: {user_email}")

    if not get_project_store().get(project_name):
        raise HTTPException(status_code=404, detail="Project niet gevonden")

    return await create_task_and_render_progress(
        request=request,
        project_name=project_name,
        task_type="delete_project",
        payload={"project_name": project_name},
        current_step=f"Project '{project_name}' verwijderen gestart...",
        success_message=f"Project '{project_name}' succesvol verwijderd",
    )


@web_router.post("/projects/{project_name}/delete-deployment/{deployment_name}", response_class=HTMLResponse)
@action(Action.DEPLOYMENT_DELETE)
@requires_sso
@requires_step_up(action=StepUpAction.DEPLOYMENT_DELETE)
async def delete_deployment_web(request: Request, project_name: str, deployment_name: str) -> HTMLResponse:
    """Delete one deployment from the UI, as a task you can follow."""
    user = get_current_user(request)
    user_email = user.get("email", "").lower()

    logger.info(f"Web deployment deletion request for '{deployment_name}' in '{project_name}' by user: {user_email}")

    # The cluster-filtered check: a name that belongs to another cluster is not ours to
    # delete, and used to make the task report "was not there (any more)".
    require_removable_deployment(project_name, deployment_name)

    return await create_task_and_render_progress(
        request=request,
        project_name=project_name,
        deployment_name=deployment_name,
        task_type="delete_deployment",
        payload={"project_name": project_name, "deployment_name": deployment_name},
        current_step=f"Deployment '{deployment_name}' verwijderen gestart...",
        success_message=f"Deployment '{deployment_name}' succesvol verwijderd",
    )


@web_router.post("/projects/{project_name}/delete-component/{component_name}", response_class=HTMLResponse)
@action(Action.COMPONENT_DELETE)
@requires_sso
@requires_step_up(action=StepUpAction.COMPONENT_DELETE)
async def delete_component_web(request: Request, project_name: str, component_name: str) -> HTMLResponse:
    """Delete a component from the UI, as a task you can follow.

    Removing the component from the project file and the reprocessing that applies it are one
    task, so the dialog follows the whole thing instead of reporting success halfway.
    """
    user = get_current_user(request)
    user_email = user.get("email", "").lower()

    logger.info(f"Web component deletion request for '{component_name}' in '{project_name}' by user: {user_email}")

    project = get_project_store().get(project_name)
    if not project:
        raise HTTPException(status_code=404, detail="Project niet gevonden")
    _require_component(project.data or {}, project_name, component_name)

    return await create_task_and_render_progress(
        request=request,
        project_name=project_name,
        task_type="delete_component",
        # The confirmation dialog names every deployment and component that references this
        # one and says those references go with it, so the user has confirmed exactly what
        # confirm_in_use states.
        payload={"project_name": project_name, "component_name": component_name, "confirm_in_use": True},
        current_step=f"Component '{component_name}' verwijderen gestart...",
        success_message=f"Component '{component_name}' succesvol verwijderd",
    )


@web_router.post("/projects/{project_name}/refresh", response_class=HTMLResponse)
@action(Action.PROJECT_REFRESH)
@requires_sso
async def refresh_project_web(request: Request, project_name: str) -> HTMLResponse:
    """Reprocess a project from Git via web interface."""

    user = get_current_user(request)
    user_email = user.get("email", "").lower()

    logger.info(f"Web project refresh request for '{project_name}' by user: {user_email}")

    project = get_project_store().get(project_name)
    if not project:
        raise HTTPException(status_code=404, detail="Project niet gevonden")

    return await create_task_and_render_progress(
        request=request,
        project_name=project_name,
        task_type="refresh_project",
        payload={"project_name": project_name, "force_clone": False, "reset_running_sync": True},
        current_step="Project herverwerken gestart...",
        success_message="Project succesvol herverwerkt!",
    )


@web_router.post("/projects/{project_name}/refresh/{deployment_name}", response_class=HTMLResponse)
@action(Action.DEPLOYMENT_REFRESH)
@requires_sso
async def refresh_deployment_web(request: Request, project_name: str, deployment_name: str) -> HTMLResponse:
    """Reprocess a single deployment from Git via web interface."""

    user = get_current_user(request)
    user_email = user.get("email", "").lower()

    logger.info(f"Web deployment refresh request for '{project_name}/{deployment_name}' by user: {user_email}")

    project = get_project_store().get(project_name)
    if not project:
        raise HTTPException(status_code=404, detail="Project niet gevonden")
    _require_deployment(project.data or {}, project_name, deployment_name)

    return await create_task_and_render_progress(
        request=request,
        project_name=project_name,
        task_type="refresh_deployment",
        payload={
            "project_name": project_name,
            "deployment_name": deployment_name,
            "force_clone": False,
            "reset_running_sync": True,
        },
        deployment_name=deployment_name,
        current_step=f"Deployment '{deployment_name}' herverwerken gestart...",
        success_message=f"Deployment '{deployment_name}' succesvol herverwerkt!",
    )


def _require_deployment(project_data: dict, project_name: str, deployment_name: str) -> None:
    """404 straight away when the deployment does not exist.

    The work runs as a task, so an unknown name would otherwise only surface as a task that
    fails a moment later. It also keeps a name the project does not have out of the task, and
    so out of everything that later renders the task's text.
    """
    names = [d.get("name") for d in project_data.get("deployments", []) or [] if isinstance(d, dict)]
    if deployment_name not in names:
        raise HTTPException(status_code=404, detail=f"Deployment '{deployment_name}' niet gevonden in {project_name}")


def _require_component(project_data: dict, project_name: str, component_name: str) -> None:
    """404 straight away when the component does not exist. See ``_require_deployment``."""
    names = [c.get("name") for c in project_data.get("components", []) or [] if isinstance(c, dict)]
    if component_name not in names:
        raise HTTPException(status_code=404, detail=f"Component '{component_name}' niet gevonden in {project_name}")


@web_router.post("/projects/{project_name}/deployments/{deployment_name}/wake")
@action(Action.SLEEP_WAKE)
@requires_sso
async def wake_deployment_web(request: Request, project_name: str, deployment_name: str) -> HTMLResponse:
    """Wake a sleeping deployment from the UI, on the session and the role.

    The counterpart of the API wake endpoint, on the same implementation in
    ``sleep_mode.flow.wake`` but without a wake token.
    """
    user = get_current_user(request)
    user_email = user.get("email", "").lower()

    project = get_project_store().get(project_name)
    if not project:
        raise HTTPException(status_code=404, detail="Project niet gevonden")
    _require_deployment(project.data or {}, project_name, deployment_name)

    logger.info(f"Web wake for '{project_name}/{deployment_name}' by {user_email}")
    # See sleep_deployment_web: waking reprocesses too, so it runs as a followable task.
    return await create_task_and_render_progress(
        request=request,
        project_name=project_name,
        deployment_name=deployment_name,
        task_type="wake_deployment",
        payload={"project_name": project_name, "deployment_name": deployment_name, "direction": "wake"},
        current_step="Deployment wekken",
        success_message="Deployment is gewekt",
    )


@web_router.post("/projects/{project_name}/deployments/{deployment_name}/sleep")
@action(Action.SLEEP_WAKE)
@requires_sso
async def sleep_deployment_web(request: Request, project_name: str, deployment_name: str) -> HTMLResponse:
    """Put a deployment to sleep from the UI, on the session and the role.

    The other half of the wake toggle, on the same implementation in ``sleep_mode.flow.sleep``.
    """
    user = get_current_user(request)
    user_email = user.get("email", "").lower()

    project = get_project_store().get(project_name)
    if not project:
        raise HTTPException(status_code=404, detail="Project niet gevonden")
    _require_deployment(project.data or {}, project_name, deployment_name)

    logger.info(f"Web sleep for '{project_name}/{deployment_name}' by {user_email}")
    # A task and not an inline call: sleeping commits to git and then reprocesses, the ArgoCD
    # sync included, which in the request would leave the page on an open POST for tens of
    # seconds with nothing to show.
    return await create_task_and_render_progress(
        request=request,
        project_name=project_name,
        deployment_name=deployment_name,
        task_type="sleep_deployment",
        payload={"project_name": project_name, "deployment_name": deployment_name, "direction": "sleep"},
        current_step="Deployment in slaapstand zetten",
        success_message="Deployment slaapt",
    )


@web_router.get(
    "/projects/{project_name}/deployments/{deployment_name}/actions/{action_key}/confirm",
    response_class=HTMLResponse,
)
@action(Action.SERVICE_ACTION)
@requires_sso
async def deployment_action_confirm(
    request: Request, project_name: str, deployment_name: str, action_key: str
) -> HTMLResponse:
    """The confirmation body for a service-contributed deployment action.

    Loaded into the shared modal shell, so every ``DeploymentAction`` with a
    ``confirm_message`` gets a real dialog and a service needs no code here. The action is
    re-derived from the project's own services and matched on its key, so the POST target this
    renders is always one a service really offered for this deployment; an endpoint read from
    the URL would be an open POST target.
    """
    user = get_current_user(request)
    user_email = user.get("email", "").lower()

    project = get_project_store().get(project_name)
    if not project:
        raise HTTPException(status_code=404, detail="Project niet gevonden")

    action = find_deployment_action(project.data or {}, deployment_name, action_key)
    if action is None or not action.endpoint:
        raise HTTPException(status_code=404, detail="Actie niet gevonden")

    return render(
        request,
        template="bg/_action-confirm.html.j2",
        context={
            "request": request,
            "action": action,
            "key": action_key,
            # A service may leave the message out; still ask, rather than firing a POST
            # straight from the page.
            "message": action.confirm_message or f"Weet je zeker dat je '{action.label}' wilt uitvoeren?",
        },
    )


@web_router.get("/projects/{project_name}/actions/{action_key}/confirm", response_class=HTMLResponse)
@action(Action.SERVICE_ACTION)
@requires_sso
async def project_action_confirm(
    request: Request, project_name: str, action_key: str, target: str | None = None
) -> HTMLResponse:
    """The confirmation body for a dangerous project action, such as delete or reprocess.

    The second, equally narrow entrance next to ``deployment_action_confirm``. The page names
    the action by key and, where it applies, which deployment, component or attachment it is
    about; the POST target is built here from the project's own data. An endpoint read from
    the request would be an open POST target, and one of these deletes a project.
    """
    user = get_current_user(request)
    user_email = user.get("email", "").lower()

    project = get_project_store().get(project_name)
    if not project:
        raise HTTPException(status_code=404, detail="Project niet gevonden")

    action = build_project_action(project_name, project.data or {}, action_key, target)
    if action is None:
        raise HTTPException(status_code=404, detail="Actie niet gevonden")

    return render(
        request,
        template="bg/_action-confirm.html.j2",
        context={
            "request": request,
            "action": action,
            "key": action.key,
            "message": action.message,
            "blocked_reason": action.blocked_reason,
        },
    )


@dev_router.get("/test-hero", response_class=HTMLResponse)
@action(Action.PORTAL_READ)
@requires_sso
async def test_hero(request: Request):
    """Test route for hero component."""
    try:
        from opi.http.render.navigation_lotc import get_navigation

        user = get_current_user(request)
        return templates_lotc.TemplateResponse(
            request, "test-hero.html.j2", {"request": request, "navigation": get_navigation(user, current_path="")}
        )
    except Exception as e:
        log_render_failure(logger, "de testpagina van de hero", e)
        raise HTTPException(
            status_code=500,
            detail="De testpagina van de hero kon niet worden opgebouwd; het sjabloon rendert niet. De volledige fout staat in de log.",
        ) from e


@dev_router.get("/forms/formulier", response_class=HTMLResponse)
@action(Action.PORTAL_READ)
@requires_sso
async def formulier_demo_form(request: Request):
    """The demo form that shows every form field component, with its validation states."""
    try:
        return templates_lotc.TemplateResponse(
            request, "formulier-template.html.j2", {"request": request, "title": "Formulier Template"}
        )
    except Exception as e:
        log_render_failure(logger, "de demopagina van het formulier", e)
        raise HTTPException(
            status_code=500,
            detail="De demopagina van het formulier kon niet worden opgebouwd; het sjabloon rendert niet. De volledige fout staat in de log.",
        ) from e


def _deployment_dashboard_status(status_data: dict[str, Any] | None) -> str:
    """The health bucket a deployment contributes to its project's dashboard tile.

    A terminal ArgoCD condition (ComparisonError etc.) means the manifests cannot be
    rendered or compared; health may still read ``Healthy`` from the last good sync, so a
    tile keyed on health alone would be misleadingly green. Treat that as ``Degraded`` so a
    broken render is visible at the top level.

    ``Progressing`` only counts as "wordt bijgewerkt" while something can still move: a
    failed or stuck sync, and a Progressing that outlasts OPI's own wait, are Degraded.
    """
    from opi.manager.argo_manager import terminal_condition_message

    if not status_data:
        return "Unknown"
    if terminal_condition_message(status_data):
        return "Degraded"
    operation = read_sync_operation(status_data)
    if operation.phase in ("Failed", "Error") or is_stuck(
        operation, threshold_seconds=STUCK_OPERATION_THRESHOLD_SECONDS
    ):
        return "Degraded"
    health = status_data.get("status", {}).get("health", {}) or {}
    health_status = health.get("status", "Unknown")
    # A pod rescheduled after a successful rollout that cannot pull its image never reaches
    # the progress deadline: Kubernetes keeps Progressing=True from the previous rollout, so
    # ArgoCD reports Progressing for weeks. The duration decides here.
    if health_status == "Progressing":
        progressing_for = seconds_since(health.get("lastTransitionTime"))
        if progressing_for is not None and progressing_for >= STUCK_OPERATION_THRESHOLD_SECONDS:
            return "Degraded"
    return health_status


def _deployment_inactivity(project_data: dict[str, Any], deployment_name: str) -> str | None:
    """Why this deployment deliberately runs nothing, or None.

    Read from the project file, never from the cluster: zero replicas there can also mean
    something went wrong, and the whole point is to tell those two apart.

    ``Disabled`` and ``Inactive`` stay separate because they ask different things of the
    reader: switched off stays off until someone turns it back on, while a deployment a
    service parked (today: sleep-mode) comes back by itself on the first visit. One grey
    "niet actief" for both would leave a user unable to tell whether to act.
    """
    if deployment_disabled_state(project_data, deployment_name).is_disabled:
        return "Disabled"
    if collect_deployment_state(project_data, deployment_name).expects_no_application_pods:
        return "Inactive"
    return None


def _derive_project_health(statuses: list[str]) -> str:
    """Worst-status-wins aggregation of a project's deployment statuses.

    ``Disabled``/``Inactive`` rank just above ``Healthy``: a project with a switched-off
    deployment is not one the banner may count among "alle projecten zijn gezond", but it
    also does not deserve to outrank a deployment that is genuinely degraded.
    """
    for status in ("Degraded", "Progressing", "Disabled", "Inactive"):
        if status in statuses:
            return status
    if "Healthy" in statuses:
        return "Healthy"
    return "Unknown"


def _dashboard_health_banner(health_counts: dict[str, int]) -> dict[str, Any] | None:
    """The banner sentence for the healthy/switched-off/inactive case, or None.

    Kept in Python rather than in the template because it is a text choice, not a
    rendering detail: "Alle N projecten zijn gezond" was untrue as soon as one of them had
    nothing running, and what should stand there instead had to be decided, not derived.

    The choice: as long as nothing is switched off or parked, the old sentence stands
    unchanged. The moment something is, the banner drops the word "alle", states how many
    of the total really are healthy, and names the rest in their own words. Degraded and
    Progressing are handled by the template, which lists the affected projects.
    """
    healthy = health_counts.get("Healthy", 0)
    disabled = health_counts.get("Disabled", 0)
    inactive = health_counts.get("Inactive", 0)
    unknown = health_counts.get("Unknown", 0)
    if not (healthy or disabled or inactive or unknown):
        return None

    total = sum(health_counts.values())

    # "Alle" may only stand there when it really is all of them, so the condition tests the
    # TOTAL: any state added later then falls into the honest branch on its own.
    #
    # A SLEEPING deployment counts as healthy: it does exactly what was asked of it and comes
    # back by itself on the first visit. Switched off is something else, because it stays off
    # until someone turns it on, so sleeping counts in the heading AND gets a line of its own.
    if healthy + inactive == total:
        healthy_total = healthy + inactive
        heading = "Het project is gezond" if healthy_total == 1 else f"Alle {healthy_total} projecten zijn gezond"
        return {"kind": "success", "heading": heading, "lines": []}

    # Every state gets its own line, in its own words: switched off stays off until someone
    # turns it on, sleeping comes back by itself, and unknown means nothing is running to say
    # anything about. One shared grey line for all three would not tell the reader whether to
    # act.
    lines: list[str] = []
    if disabled:
        lines.append(
            f"{disabled} project{'' if disabled == 1 else 'en'} "
            f"{'heeft' if disabled == 1 else 'hebben'} een uitgeschakelde deployment"
        )
    if inactive:
        lines.append(
            f"{inactive} project{'' if inactive == 1 else 'en'} "
            f"{'heeft' if inactive == 1 else 'hebben'} een slapende deployment"
        )
    if unknown:
        lines.append(
            f"{unknown} project{'' if unknown == 1 else 'en'} "
            f"{'heeft' if unknown == 1 else 'hebben'} nog geen deployment die iets draait"
        )
    # Four cases, because a single project needs another numeral, another plural and another
    # verb than two or more.
    healthy_total = healthy + inactive
    if total == 1:
        heading = "Het project is gezond" if healthy_total else "Het project is niet gezond"
    elif not healthy_total:
        heading = f"Geen van je {total} projecten is gezond"
    elif healthy_total == 1:
        heading = f"1 van de {total} projecten is gezond"
    else:
        heading = f"{healthy_total} van de {total} projecten zijn gezond"
    return {
        "kind": "info",
        "heading": heading,
        "lines": lines,
    }


async def _sum_by_namespace(prom: Any, promql: str) -> dict[str, float]:
    """Read a query with ``by (namespace)`` as namespace -> value.

    One query then yields the figures of every project at once instead of a query per project.
    A failed query is no error but an empty result, so the dashboard shows zero, as it does for
    the other Prometheus queries in this file.
    """
    try:
        result = await prom.query(promql)
    except Exception as e:
        logger.warning(f"Dashboard per-namespace query failed: {e}")
        return {}

    values: dict[str, float] = {}
    for series in result or []:
        namespace = series.get("metric", {}).get("namespace")
        value = series.get("value")
        if namespace and value:
            values[namespace] = float(value[1])
    return values


async def collect_dashboard_metrics(
    all_namespaces: list[str],
    user_projects: list[dict],
) -> tuple[dict, bool, int]:
    """Fetch the resource usage for the dashboard from Prometheus.

    Shared by the dashboard route and by the fragment the page loads separately, so an
    improvement cannot land on one side only.

    Also sets ``cpu_cores``, ``cpu_limit_cores``, ``memory_mb`` and ``memory_limit_mb`` per
    project in ``user_projects``, which the per-project usage card needs.

    Returns the metrics, whether Prometheus was reachable, and the number of pods.
    """
    metrics: dict = {}
    prometheus_available = False
    pod_count = 0

    if all_namespaces:
        try:
            from opi.connectors.prometheus import get_metrics_connector
            from opi.services.metrics_queries import (
                namespace_usage_queries,
                network_traffic_queries,
                usage_per_namespace_queries,
            )

            prom = await get_metrics_connector()
            prometheus_available = prom.is_connected

            if prometheus_available:
                queries = namespace_usage_queries(all_namespaces)
                cpu_usage_val = 0.0
                cpu_limit_val = 0.0
                try:
                    result = await prom.query(queries["cpu_cores"])
                    if result and result[0].get("value"):
                        cpu_usage_val = float(result[0]["value"][1])
                except Exception as e:
                    logger.warning(f"Dashboard CPU usage query failed: {e}")

                try:
                    result = await prom.query(queries["cpu_limit_cores"])
                    if result and result[0].get("value"):
                        cpu_limit_val = float(result[0]["value"][1])
                except Exception as e:
                    logger.warning(f"Dashboard CPU limits query failed: {e}")

                mem_usage_val = 0.0
                mem_limit_val = 0.0
                try:
                    result = await prom.query(queries["memory_bytes"])
                    if result and result[0].get("value"):
                        mem_usage_val = float(result[0]["value"][1])
                except Exception as e:
                    logger.warning(f"Dashboard memory usage query failed: {e}")

                try:
                    result = await prom.query(queries["memory_limit_bytes"])
                    if result and result[0].get("value"):
                        mem_limit_val = float(result[0]["value"][1])
                except Exception as e:
                    logger.warning(f"Dashboard memory limits query failed: {e}")

                # The REQUEST as well, and for memory only: ODCN bills memory as
                # request + clamp_min(usage - request, 0), so per pod the higher of the two,
                # and neither usage nor limit is the number on the bill. CPU does NOT get this,
                # because it is not billed.
                mem_request_val = 0.0
                try:
                    result = await prom.query(queries["memory_request_bytes"])
                    if result and result[0].get("value"):
                        mem_request_val = float(result[0]["value"][1])
                except Exception as e:
                    logger.warning(f"Dashboard memory requests query failed: {e}")

                storage_used_val = 0.0
                storage_cap_val = 0.0
                try:
                    result = await prom.query(queries["storage_used_bytes"])
                    if result and result[0].get("value"):
                        storage_used_val = float(result[0]["value"][1])
                except Exception as e:
                    logger.warning(f"Dashboard storage usage query failed: {e}")

                try:
                    result = await prom.query(queries["storage_capacity_bytes"])
                    if result and result[0].get("value"):
                        storage_cap_val = float(result[0]["value"][1])
                except Exception as e:
                    logger.warning(f"Dashboard storage capacity query failed: {e}")

                try:
                    result = await prom.query(queries["pod_count"])
                    if result and result[0].get("value"):
                        pod_count = int(float(result[0]["value"][1]))
                except Exception as e:
                    logger.warning(f"Dashboard pod count query failed: {e}")

                # The network time series: the last 30 minutes, in steps of 5.
                network_in_data: list[dict] = []
                network_out_data: list[dict] = []
                try:
                    now = datetime.now(UTC)
                    start = now.timestamp() - 1800
                    end = now.timestamp()
                    traffic = network_traffic_queries(all_namespaces)
                    in_results = await prom.query_range(
                        traffic["network_in"],
                        start_time=str(int(start)),
                        end_time=str(int(end)),
                        step="300",
                    )
                    if in_results:
                        for ts, val in in_results[0].get("values", []):
                            network_in_data.append(
                                {
                                    "t": datetime.fromtimestamp(ts, tz=UTC).strftime("%H:%M"),
                                    "v": round(float(val) / 1024, 1),
                                }
                            )

                    out_results = await prom.query_range(
                        traffic["network_out"],
                        start_time=str(int(start)),
                        end_time=str(int(end)),
                        step="300",
                    )
                    if out_results:
                        for ts, val in out_results[0].get("values", []):
                            network_out_data.append(
                                {
                                    "t": datetime.fromtimestamp(ts, tz=UTC).strftime("%H:%M"),
                                    "v": round(float(val) / 1024, 1),
                                }
                            )
                except Exception as e:
                    logger.warning(f"Dashboard network query failed: {e}")

                def _pct(used: float, total: float) -> int:
                    if total <= 0:
                        return 0
                    return min(100, round(used / total * 100))

                def _format_cores(val: float) -> str:
                    if val < 1:
                        return f"{int(val * 1000)}m"
                    return f"{val:.1f}"

                def _format_gib(val_bytes: float) -> str:
                    gib = val_bytes / (1024**3)
                    if gib < 0.1:
                        mib = val_bytes / (1024**2)
                        return f"{mib:.0f} MiB"
                    return f"{gib:.1f} GiB"

                metrics = {
                    "cpu_percentage": _pct(cpu_usage_val, cpu_limit_val),
                    "cpu_usage_display": _format_cores(cpu_usage_val),
                    "cpu_limit_display": _format_cores(cpu_limit_val),
                    "memory_percentage": _pct(mem_usage_val, mem_limit_val),
                    "memory_usage_display": _format_gib(mem_usage_val),
                    "memory_request_display": _format_gib(mem_request_val),
                    "memory_limit_display": _format_gib(mem_limit_val),
                    "storage_percentage": _pct(storage_used_val, storage_cap_val),
                    "storage_usage_display": _format_gib(storage_used_val),
                    "storage_capacity_display": _format_gib(storage_cap_val),
                    "network_in_data": network_in_data,
                    "network_out_data": network_out_data,
                }
                # The usage AND the limit per project, for the per-project usage card. Usage
                # is the working set, and the limit comes along because the card has the shape
                # of the project page's resource card: used over limit with a percentage and a
                # bar, which needs an upper bound. The same four queries as that card, so a
                # project shows the same figure in both places.
                #
                # ``by (namespace)`` and not four queries PER project: this costs four queries
                # whatever the number of projects.
                per_namespace = usage_per_namespace_queries(all_namespaces)
                cpu_by_ns = await _sum_by_namespace(prom, per_namespace["cpu_cores"])
                cpu_limit_by_ns = await _sum_by_namespace(prom, per_namespace["cpu_limit_cores"])
                mem_by_ns = await _sum_by_namespace(prom, per_namespace["memory_bytes"])
                mem_limit_by_ns = await _sum_by_namespace(prom, per_namespace["memory_limit_bytes"])

                for project in user_projects:
                    proj_ns = project.get("namespaces", [])
                    # A project without namespaces sums to zero.
                    project["cpu_cores"] = sum(cpu_by_ns.get(ns, 0.0) for ns in proj_ns)
                    project["cpu_limit_cores"] = sum(cpu_limit_by_ns.get(ns, 0.0) for ns in proj_ns)
                    project["memory_mb"] = sum(mem_by_ns.get(ns, 0.0) for ns in proj_ns) / (1024 * 1024)
                    project["memory_limit_mb"] = sum(mem_limit_by_ns.get(ns, 0.0) for ns in proj_ns) / (1024 * 1024)

        except Exception as e:
            logger.warning(f"Dashboard: failed to fetch Prometheus metrics: {e}")

    return metrics, prometheus_available, pod_count


@web_router.get("/dashboard", response_class=HTMLResponse)
@action(Action.PORTAL_READ)
@requires_sso
async def dashboard(request: Request):
    """The dashboard: the user's projects, the Prometheus metrics and the ArgoCD status."""
    try:
        user = get_current_user(request)
        user_email = user.get("email", "").lower()

        all_projects = get_project_store().get_all()

        user_projects: list[dict] = []
        all_namespaces: list[str] = []
        total_deployments = 0
        unique_users: set[str] = set()

        for project in all_projects:
            project_name = project.name
            if not is_user_authorized_for_project(project_name, user_email, audit=False):
                continue
            project_data = project.data or {}
            deployments = project_data.get("deployments", [])
            # The PEOPLE only: the dashboard counts colleagues, and a service account is none.
            users = human_members(project)
            total_deployments += len(deployments)
            for u in users:
                if hasattr(u, "email") and u.email:
                    unique_users.add(u.email.lower())

            project_namespaces: list[str] = []
            for deployment in deployments:
                cluster = deployment.get("cluster")
                namespace = deployment.get("namespace")
                if cluster and namespace:
                    from opi.core.cluster_config import get_prefixed_namespace

                    k8s_ns = get_prefixed_namespace(cluster, namespace)
                    if k8s_ns not in all_namespaces:
                        all_namespaces.append(k8s_ns)
                    if k8s_ns not in project_namespaces:
                        project_namespaces.append(k8s_ns)

            user_projects.append(
                {
                    "name": project_name,
                    "display_name": project_data.get("display-name", project_name),
                    "description": project_data.get("description", ""),
                    "deployments": deployments,
                    # Needed to read whether a deployment deliberately runs nothing; that
                    # intent lives in the project file and not in the cluster.
                    "project_data": project_data,
                    "users": users,
                    "deployment_count": len(deployments),
                    "user_count": len(users),
                    "namespaces": project_namespaces,
                }
            )

        user_projects.sort(key=lambda p: p["display_name"] or p["name"])

        # The page fetches this block separately, over /dashboard/resource-usage, so it is
        # there at once and a slow or absent Prometheus does not hold it up. They stay empty
        # here; the template does read them.
        metrics: dict = {}
        prometheus_available = False
        pod_count = 0

        total_cpu_usage = sum(p.get("cpu_cores", 0) for p in user_projects)
        total_memory_usage = sum(p.get("memory_mb", 0) or 0 for p in user_projects)

        argocd_available = False
        try:
            from opi.connectors.argo import create_argo_connector
            from opi.utils.naming import generate_argocd_application_name

            # Fail fast: the default budget of 30 seconds would apply PER deployment here,
            # sequentially in a single request. The same rule as in opi/api/v2/router.py; see
            # "docs/post-mortems/argocd-server-herstart-elke-40-minuten.md".
            argo_connector = create_argo_connector(connect_retry_seconds=0)
            argocd_available = await argo_connector.ensure_authenticated()

            if argocd_available:
                for project in user_projects:
                    deployment_statuses: list[str] = []
                    latest_deploy: str | None = None
                    for deployment in project.get("deployments", []):
                        deployment_name = deployment.get("name")
                        if not deployment_name:
                            continue
                        try:
                            app_name = generate_argocd_application_name(project["name"], deployment_name)
                            status_data = await argo_connector.get_application_status(app_name)
                            if status_data:
                                # A deployment that runs nothing on purpose reports zero
                                # replicas, which ArgoCD calls Healthy. That is the only
                                # verdict the intent replaces: Degraded and Progressing are
                                # things ArgoCD really observed, and they stand.
                                argo_status = _deployment_dashboard_status(status_data)
                                inactivity = _deployment_inactivity(project["project_data"], deployment_name)
                                if inactivity and argo_status == "Healthy":
                                    argo_status = inactivity
                                deployment_statuses.append(argo_status)
                                # When something was last really DEPLOYED, with no fall back
                                # to ``reconciledAt``: on a stuck sync that reports the time of
                                # the last COMPARISON as the moment of deploying. No deploy
                                # means no line.
                                finished_at = read_sync_operation(status_data).finished_at
                                if finished_at and (not latest_deploy or finished_at > latest_deploy):
                                    latest_deploy = finished_at
                            else:
                                deployment_statuses.append("Unknown")
                        except Exception as e:
                            logger.debug(f"Dashboard: ArgoCD status for {deployment_name}: {e}")
                            deployment_statuses.append("Unknown")

                    # Worst status wins.
                    project["health"] = _derive_project_health(deployment_statuses)
                    project["last_deployed"] = latest_deploy
        except Exception as e:
            logger.warning(f"Dashboard: failed to connect to ArgoCD: {e}")
            for project in user_projects:
                project["health"] = "Unknown"

        health_counts = {"Healthy": 0, "Progressing": 0, "Degraded": 0, "Disabled": 0, "Inactive": 0, "Unknown": 0}
        for p in user_projects:
            health_counts[p.get("health", "Unknown")] += 1
        health_banner = _dashboard_health_banner(health_counts)

        return render(
            request,
            template="bg/dashboard.html.j2",
            context={
                "request": request,
                "menu_items": get_menu_items(user),
                "active_projects": len(user_projects),
                "total_deployments": total_deployments,
                "total_users": len(unique_users),
                "pod_count": pod_count,
                "prometheus_available": prometheus_available,
                "argocd_available": argocd_available,
                "metrics": metrics,
                "projects": user_projects,
                "health_counts": health_counts,
                "health_banner": health_banner,
                "total_cpu_usage": total_cpu_usage,
                "total_memory_usage": total_memory_usage,
                **build_lotc_dashboard(user=user),
            },
        )

    except Exception as e:
        log_render_failure(logger, "het dashboard", e)
        raise HTTPException(
            status_code=500, detail="Het dashboard kon niet worden opgebouwd. Probeer het over een minuut opnieuw."
        ) from e


@web_router.get("/projects/{project_name}/details", response_class=HTMLResponse)
@action(Action.PROJECT_READ)
@web_router.get("/projects/{project_name}/team", response_class=HTMLResponse)
@web_router.get("/projects/{project_name}/componenten", response_class=HTMLResponse)
@web_router.get("/projects/{project_name}/services", response_class=HTMLResponse)
@web_router.get("/projects/{project_name}/services-info", response_class=HTMLResponse)
@web_router.get("/projects/{project_name}/deployments", response_class=HTMLResponse)
@web_router.get("/projects/{project_name}/metrics", response_class=HTMLResponse)
@web_router.get("/projects/{project_name}/backups", response_class=HTMLResponse)
@web_router.get("/projects/{project_name}/taken", response_class=HTMLResponse)
@web_router.get("/projects/{project_name}/kaart", response_class=HTMLResponse)
@requires_sso
async def project_details(request: Request, project_name: str):
    """The project page without a deployment in the path; see :func:`render_project_page`."""
    return await render_project_page(request, project_name, "")


@web_router.get("/projects/{project_name}/kaart.json")
@action(Action.PROJECT_READ)
@requires_sso
async def project_map_json(request: Request, project_name: str, deployment: str | None = None) -> JSONResponse:
    """The graph behind the map tab; with ``?deployment=`` the one of a deployment.

    A route with an action of its own, like the metrics fragment, because the tab loads its own
    picture. Reads only ``ProjectSummary.data`` and never the decrypted form. Only a
    deployment's map measures sizes.
    """
    try:
        usage = await measure_usage(*usage_targets(project_name, deployment)) if deployment else None
        graph = project_graph(project_name, deployment, usage)
    except KeyError as missing:
        raise HTTPException(status_code=404, detail=f"'{missing.args[0]}' not found") from None
    return JSONResponse(graph.as_json())


@web_router.get("/projects/{project_name}/audit", response_class=HTMLResponse)
@action(Action.AUDIT_READ)
@requires_sso
async def project_audit(request: Request, project_name: str):
    """The audit tab, on a route of its own because it carries an action of its own.

    The same page as the other tabs (:func:`render_project_page`); only the action differs, and
    the router dependency weighs it on THIS project from the path. ``?limit=`` is the number of
    rows.
    """
    return await render_project_page(request, project_name, "")


@web_router.get("/projects/{project_name}/deployments/{deployment_name}", response_class=HTMLResponse)
@action(Action.DEPLOYMENT_READ)
@web_router.get("/projects/{project_name}/metrics/{deployment_name}", response_class=HTMLResponse)
@web_router.get("/projects/{project_name}/backups/{deployment_name}", response_class=HTMLResponse)
@requires_sso
async def project_deployment_details(request: Request, project_name: str, deployment_name: str):
    """ONE deployment, on the Deployments, Metrics and Backups tabs.

    The name is in the PATH and the server renders one deployment, so the page is shareable,
    the back button works, and the choice survives a tab switch because the tab bar carries it
    in its addresses.

    The paths stand here literally and not as ``/projects/{project}/{tab}/{deployment}``, for
    the same reason as for the tabs themselves: that would also catch paths belonging to
    another route.
    """
    return await render_project_page(request, project_name, deployment_name)


#: The older tab paths, with the tab BEFORE the project name. They stand here literally and in
#: the same order as the routes above, so the two shapes read side by side.
#:
#: Every tab belongs here for as long as its old shape is registered as a route below: that
#: route looks its tab up in this table, so a missing line is not a redirect but a 500.
#: tests/test_lotc_tabbladen_url.py walks both lists.
#:
#: ``backups`` is NOT here, and that is no omission: that tab is newer than these paths, so
#: ``/projects/backups/<name>`` never existed and can never have been shared.
OLD_TAB_PATHS = {
    "details": "project",
    "team": "team",
    "componenten": "componenten",
    "services": "services",
    "services-info": "services-info",
    "deployments": "deployments",
    "metrics": "metrics",
    "taken": "taken",
}


@web_router.get("/projects/details/{project_name}", response_class=HTMLResponse)
@web_router.get("/projects/team/{project_name}", response_class=HTMLResponse)
@web_router.get("/projects/componenten/{project_name}", response_class=HTMLResponse)
@web_router.get("/projects/services/{project_name}", response_class=HTMLResponse)
@web_router.get("/projects/services-info/{project_name}", response_class=HTMLResponse)
@web_router.get("/projects/deployments/{project_name}", response_class=HTMLResponse)
@web_router.get("/projects/metrics/{project_name}", response_class=HTMLResponse)
@web_router.get("/projects/taken/{project_name}", response_class=HTMLResponse)
@web_router.get("/projects/deployments/{project_name}/{deployment_name}", response_class=HTMLResponse)
@web_router.get("/projects/metrics/{project_name}/{deployment_name}", response_class=HTMLResponse)
async def project_tab_old_shape(request: Request, project_name: str, deployment_name: str = ""):
    """Redirect the older tab addresses, which had the tab before the project name.

    The project name comes first now. The old shape has been live and may have been shared, and
    a shared link should not silently become a 404.

    DELIBERATELY WITHOUT ``@requires_sso``, which is safe because nothing is looked up: this is
    a blind rewrite of the path that gives nothing away about the project, and the answer is the
    same for every name. The real page behind the new address does the authorization.

    These routes are registered AFTER the new shape, because for a project that happens to be
    called ``deployments`` or ``details`` both shapes read, and then today's address wins.
    """
    tab = OLD_TAB_PATHS[request.url.path.strip("/").split("/")[1]]
    return RedirectResponse(
        url=project_tab_url(project_name, tab, request.url.query, deployment=deployment_name),
        status_code=302,
    )


async def render_project_page(request: Request, project_name: str, deployment_name: str):
    """The project page: its services, components, deployments and configuration.

    Every tab has a PATH OF ITS OWN instead of a ``?tab=`` on a shared address, because a query
    string reads as a filter while a tab is a page. The paths stand above literally and not as
    ``/projects/{project_name}/{tab}``: that would also catch ``/projects/details/<name>``, with
    ``project_name="details"``, and then the order of registration decides which route wins.

    A ``?tab=`` in the URL is ignored; the overview tab is what you get.
    """
    try:
        from opi.services.services import ServiceAdapter

        user = get_current_user(request)

        csrf_token = ensure_csrf_token(request)

        user_email = user.get("email", "").lower()

        if not is_user_authorized_for_project(project_name, user_email):
            logger.warning(f"User {user_email} not authorized to view project: {project_name}")
            raise HTTPException(status_code=403, detail="You are not authorized to view this project")

        project = get_project_store().get(project_name)
        if not project:
            raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")

        user_roles = get_user_roles_for_project(project_name, user_email)

        project_data = project.data or {}

        # WHICH deployment is open, and is that name already in the URL? These tabs show one,
        # so the address has to say which: without a name in the path, or with a name that no
        # longer exists, the server picks one and redirects there, so the URL is right and
        # shareable afterwards. A ``?deployment=<name>`` from the older shape redirects here
        # too.
        #
        # Right after loading the project: everything below is decryption work a redirect does
        # not need.
        deployment_open = ""
        if tab_from_path(request.url.path) in TABS_MET_DEPLOYMENT:
            deployment_open = kies_deployment(
                [deployment["name"] for deployment in project_data.get("deployments") or []],
                deployment_name,
                request.query_params.get("deployment") or "",
            )
            belongs_at = deployment_pagina_adres(request, project_name, deployment_open)
            current = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            if belongs_at != current:
                return RedirectResponse(url=belongs_at, status_code=302)

        project_data_decrypted = copy.deepcopy(project_data)

        project_private_key = await get_decoded_project_private_key(project_data)

        # The map tab loads over a route of its own: no secrets in the context, and no
        # Prometheus, ArgoCD or Kopia.
        map_tab = tab_from_path(request.url.path) == "kaart"

        # Secrets only reach the context for whoever may read them. From the rule register and
        # not from a role name, so what the matrix page shows under ``project-key.read`` is
        # what this page does.
        may_see_secrets = not map_tab and may(request, action=Action.PROJECT_KEY_READ, project_name=project_name)

        # The project key is only still in a file whose migration could not open it, so without
        # this ``if`` every project page would be a 500 on the KeyError once the field is gone.
        legacy_key = project_data["config"].get("api-key")
        if may_see_secrets:
            if legacy_key:
                project_data_decrypted["config"]["api-key"] = await decrypt_password_smart(
                    legacy_key, project_private_key
                )
            project_data_decrypted["config"]["age-private-key"] = project_private_key
        else:
            if legacy_key:
                project_data_decrypted["config"]["api-key"] = None
            project_data_decrypted["config"]["age-private-key"] = None

        # The Keycloak connections live under the keycloak service config.
        for kc_config in Project(project_data_decrypted).get("services/keycloak/config/realms") or []:
            if not may_see_secrets:
                kc_config["password"] = None
            elif kc_config.get("password"):
                try:
                    kc_config["password"] = await decrypt_password_smart(kc_config["password"], project_private_key)
                except Exception as e:
                    logger.warning(f"Failed to decrypt Keycloak password for realm {kc_config.get('realm')}: {e}")
                    kc_config["password"] = None
            # The SEED never reaches the page, because it would yield codes forever. What does
            # reach it is the CODE of this moment, which expires within 30 seconds. It is
            # computed here, because on the page the OTP is a field like the password next to
            # it, and a field has its value at render time. Same block, same role gate, and of
            # the two the code is the shorter-lived.
            kc_config["has_totp"] = bool(kc_config.get("totp_secret"))
            if kc_config["has_totp"] and may_see_secrets:
                try:
                    seed = await decrypt_password_smart(kc_config["totp_secret"], project_private_key)
                    kc_config["totp_code"], _ = totp_now(seed)
                except Exception as e:
                    logger.warning(f"Failed to derive OTP code for realm {kc_config.get('realm')}: {e}")
                    kc_config["totp_code"] = None
            else:
                kc_config["totp_code"] = None
            kc_config["totp_secret"] = None

        # Nothing of the environment variables is on the page, not even a name: values and
        # names ask for a fresh step-up (router_env_vars). The encrypted blocks leave the copy,
        # so nothing of this field can end up in the HTML.
        for deployment in project_data_decrypted.get("deployments", []):
            for dep_component in deployment.get("components", []):
                dep_component.pop("user-env-vars", None)
        for component in project_data_decrypted.get("components", []):
            component.pop("user-env-vars", None)

        for component in project_data_decrypted.get("components", []):
            component_name = component.get("name", "unknown")
            # The aliases too: the card shows them, and without this an AGE block stands on
            # the screen. The same decoder the read endpoints use, so the page and the API
            # cannot disagree about what is there; what is screened off after that is up to
            # the service, in the template.
            if component.get("aliases"):
                try:
                    # In a thread, because decode_component_values is synchronous and stays
                    # that way: the read endpoints use it too, and a second shape does not
                    # belong there.
                    component["aliases"] = await asyncio.to_thread(
                        decode_component_values, component["aliases"], project_data_decrypted, project_private_key
                    )
                except (ComponentValuesError, ValueError) as error:
                    # An unreadable block yields no names to show, so leave it out rather than
                    # putting an AGE block on the screen.
                    logger.warning(f"Aliases of component '{component_name}' could not be read: {error}")
                    component["aliases"] = {}

        # Make helm-values readable at project level, the catalog, and at deployment level,
        # the reference, for helm-charts as well as helmfile.
        await decrypt_helm_values(
            project_data_decrypted.get("helm-charts", []), "name", "helm-chart", project_private_key
        )
        await decrypt_helm_values(project_data_decrypted.get("helmfile", []), "name", "helmfile", project_private_key)
        for deployment in project_data_decrypted.get("deployments", []):
            deployment_name = deployment.get("name", "unknown")
            await decrypt_helm_values(
                deployment.get("helm-charts", []),
                "reference",
                f"deployment '{deployment_name}' helm-chart",
                project_private_key,
            )
            await decrypt_helm_values(
                deployment.get("helmfile", []),
                "reference",
                f"deployment '{deployment_name}' helmfile",
                project_private_key,
            )

        services_with_info = []
        project_services = project_data.get("services", [])
        service_names = ServiceAdapter.extract_service_names_from_project_services(project_services)
        for service_name in service_names:
            service_enum = ServiceAdapter.get_service_by_value(service_name)
            if service_enum:
                services_with_info.append(
                    {
                        "enum": service_enum,
                        "value": service_name,
                    }
                )

        from opi.services.project_file_handler import extract_attachment_catalog, extract_attachment_usage

        _attachment_usage = extract_attachment_usage(project_data)

        project_details = {
            "name": project_name,
            "display_name": project_data.get("display-name", project_name),
            "description": project_data.get("description", "Geen beschrijving beschikbaar"),
            "source_code": project_data.get("source-code", []),
            # The PEOPLE only: the Team block and its edit modal invite people and mail them.
            # The service accounts are in the members-and-tokens block.
            "users": human_members(project),
            "user_roles": user_roles,
            # For the link to the role matrix on the Team card: that page is for platform
            # admins, so a member gets no link to it that answers 403.
            "is_platform_admin": is_elevated_admin(get_user_service(), user_email),
            "services": services_with_info,
            "clusters": project_data.get("clusters", []),
            "components": project_data_decrypted.get("components", []),
            "deployments": project_data_decrypted.get("deployments", []),
            "repositories": project_data.get("repositories", []),
            "config": project_data_decrypted.get("config", {}),
            "helm_charts": project_data_decrypted.get("helm-charts", []),
            "helmfile": project_data_decrypted.get("helmfile", []),
            "attachments": [
                {
                    "id": entry["id"],
                    "filename": entry.get("filename", entry["id"]),
                    "used_by": _attachment_usage.get(entry["id"], []),
                }
                for entry in extract_attachment_catalog(project_data).values()
            ],
        }

        # The metrics themselves are loaded lazily over HTMX.
        prometheus_available = False
        if not map_tab:
            try:
                from opi.connectors.prometheus import get_metrics_connector

                prom = await get_metrics_connector()
                prometheus_available = prom.is_connected
            except Exception as e:
                logger.debug(f"Prometheus not available: {e}")

        # The ArgoCD status is loaded lazily per deployment, so it does not block the page
        # render; only the cheap availability check stays here. Fail fast, because on the
        # default budget this check would hold the page for 30 seconds. See
        # "docs/post-mortems/argocd-server-herstart-elke-40-minuten.md".
        argocd_available = False
        if not map_tab:
            try:
                from opi.connectors.argo import create_argo_connector

                argocd_available = await create_argo_connector(connect_retry_seconds=0).ensure_authenticated()
            except Exception as argo_error:
                logger.warning(f"Failed to connect to ArgoCD: {argo_error}")

        # Backup snapshots are NOT fetched here: listing them opens a Kopia repository over
        # S3, which was 70% of this page's render time for a block most visitors never look
        # at. Each deployment's block loads itself over /backups/{deployment}.
        from opi.core.config import settings
        from opi.manager.backup import BackupManager

        current_cluster = settings.CLUSTER_MANAGER
        # The ungranted approvals per deployment, asked of the catalog, where each service
        # writes its own notice, so this page holds no domain knowledge.
        from opi.services.approvals import collect_deployment_approval_notices

        approval_notices: dict[str, list[dict[str, Any]]] = {}
        for deployment in project_details["deployments"]:
            notices = collect_deployment_approval_notices(project_data_decrypted, deployment)
            if notices:
                approval_notices[deployment.get("name", "")] = notices

        backups_available = False
        if not map_tab:
            try:
                BackupManager()
                backups_available = True
            except Exception as backup_init_error:
                logger.warning(f"Failed to initialize backup manager: {backup_init_error}")

        from opi.services.catalog.publish_on_web.urls import public_urls_for_deployment
        from opi.services.project_file_handler import ProjectFileHandler

        project_file_handler = ProjectFileHandler()

        for deployment in project_details["deployments"]:
            cluster = deployment.get("cluster")
            # One derivation, owned by publish-on-web: the invite form offers these same
            # addresses as the destination of its success button.
            deployment["ingress_links"] = public_urls_for_deployment(
                project_data, deployment, project_name, project_file_handler
            )

        # The same links, hung on the component instead of on the deployment, because the
        # components section lists them per component. One derivation, so the two lists on
        # one page cannot disagree.
        for component in project_details["components"]:
            component_name = component.get("name")
            component["ingress_links"] = [
                {**link, "deployment_name": deployment["name"], "cluster": deployment.get("cluster")}
                for deployment in project_details["deployments"]
                for link in deployment["ingress_links"]
                if link["component_name"] == component_name
            ]

        from opi.web.router_self_service import get_cluster_base_domains_for_template

        cluster_base_domains = get_cluster_base_domains_for_template()

        from opi.forms.visualizers.flows import SERVICE_CONFIG_MODAL_FLOWS

        # The deployment-level action buttons the project's services contribute, keyed by
        # deployment name. Built from the decrypted project data, so it can read the
        # OPI-managed sleep state.
        from opi.services.catalog.base import DeploymentPageContext
        from opi.services.catalog.shared.backups import collect_backups_sections
        from opi.services.registry import (
            collect_deployment_actions,
            collect_deployment_page_sections,
            collect_detail_page_sections,
        )

        deployment_service_actions = {
            dep.get("name"): collect_deployment_actions(project_data_decrypted, dep.get("name", ""))
            for dep in project_data_decrypted.get("deployments", [])
        }

        # What the services report about each deployment: the same facts the health check
        # weighs, here only rendered. Without it a user sees a sleeping deployment with
        # nothing running and no explanation why.
        from opi.services.deployment_state import collect_deployment_state

        deployment_states = {
            dep.get("name"): collect_deployment_state(project_data_decrypted, dep.get("name", ""))
            for dep in project_data_decrypted.get("deployments", [])
        }

        # The status column of the deployment table on the overview tab. Two sources, both
        # fetched per PROJECT and not per row: the facts of the services, computed above, and
        # ONE bundled query to ArgoCD, so twenty rows do not become twenty ArgoCD requests.
        #
        # Only for the tab that shows the table: the other tabs do not have the column.
        argocd_statuses: dict[str, dict[str, Any]] = {}
        if argocd_available and tab_from_path(request.url.path) == STANDAARD_TAB:
            argocd_statuses = await get_project_argocd_statuses(
                project_name, [name for name in deployment_states if name]
            )
        deployment_status_tags = build_deployment_status_column(
            project_details["deployments"], deployment_states, argocd_statuses
        )

        def deployment_page_context(dep: dict[str, Any]) -> DeploymentPageContext:
            """What a service needs to build its block for THIS deployment.

            The availability of the optional back ends is already measured here, because a
            service never calls a connector itself, and travels in.
            """
            return DeploymentPageContext(
                project_data=project_data_decrypted,
                deployment=dep,
                user_roles=list(user_roles),
                current_cluster=current_cluster,
                backend_available={"prometheus": prometheus_available, "backups": backups_available},
            )

        # The per-deployment read-only blocks the services deliver: a block that describes
        # one deployment is asked per deployment instead of being hardcoded in the tab.
        deployment_service_sections = {
            dep.get("name"): collect_deployment_page_sections(deployment_page_context(dep))
            for dep in project_data_decrypted.get("deployments", [])
        }

        # The members and their tokens, only for the tab they stand on.
        token_context: dict[str, Any] = {}
        if tab_from_path(request.url.path) == "team":
            user_email = (user or {}).get("email", "").lower()
            # Admin of THIS project, from the member list in the project file, which is also
            # the source of the refusal behind the button: a platform admin who is not a
            # member would otherwise see buttons that always refuse.
            token_context = tokencontext(
                request,
                project_name,
                user_email,
                is_admin=is_project_admin(project_name, user_email),
            )

        # The backups block, for the deployment that is open on the Backups tab. Asked by
        # NAME and not through the general service mechanism above, because it has a tab of
        # its own while that mechanism delivers everything onto ONE tab.
        #
        # Only for the OPEN deployment: the page shows one, and the rest would be work for
        # blocks nobody sees.
        backups_sections: list[Any] = []
        if tab_from_path(request.url.path) == "backups":
            open_deployment = Project(project_data_decrypted).find("deployments", name=deployment_open)
            if open_deployment is not None:
                backups_sections = collect_backups_sections(deployment_page_context(open_deployment))

        # The read-only detail-page sections the project's services deliver: each service
        # owns its own block instead of the general template hardcoding an include. Built from
        # the decrypted data, so a service can surface the credentials it manages.
        service_detail_sections = collect_detail_page_sections(
            project_data_decrypted,
            # A service block that shows a secret reads the same rule as the route behind it,
            # instead of a role name of its own.
            lambda requested_action: may(request, action=requested_action, project_name=project_name),
        )

        # Those blocks have a TAB of their own: the addresses, keys and files with which you
        # use the services, which is what you come back for.
        #
        # When no service delivers anything there is no tab, because an empty page behind a tab
        # is a promise that is not kept. The tab falls out of the bar ("lege_tabs" below),
        # and whoever still arrives there over a shared link goes to the overview.
        lege_tabs = () if service_detail_sections else TABS_MET_VOORWAARDE
        # The audit tab is only there for whoever may read it. That is a right and not an empty
        # tab, so it does not follow from TABS_MET_VOORWAARDE; the route behind it refuses with
        # a 403, and a tab that ends in one does not belong in the bar.
        if not may(request, action=Action.AUDIT_READ, project_name=project_name):
            lege_tabs = (*lege_tabs, "audit")
        if tab_from_path(request.url.path) in lege_tabs:
            return RedirectResponse(url=project_tab_url(project_name, STANDAARD_TAB), status_code=302)

        # The audit rows of this project, only on the tab that shows them. The project comes
        # from the PATH: the same name @action(AUDIT_READ) weighed, never one from the query
        # string or the body.
        audit_context: dict[str, Any] = {}
        if tab_from_path(request.url.path) == "audit":
            audit_context = await audit_pagina(request, project_name)

        # Changes saved with rollout=false that nobody has rolled out yet, shown above the
        # tabs, because a project file that silently runs ahead of the cluster is worse than a
        # slow rollout. A task service that is not up must not take the page down with it, so a
        # failure here degrades to "no notice".
        pending_rollout: dict[str, Any] = {
            "count": 0,
            "since": None,
            "task_types": [],
            "rollout_in_progress": False,
        }
        task_service = getattr(request.app.state, "task_service", None)
        if task_service is not None:
            try:
                pending_rollout = await task_service.get_deferred_rollouts(project_name)
            except SQLAlchemyError:
                logger.exception("Could not determine deferred rollouts for project %s", project_name)

        response = render(
            request,
            template="bg/project-tabs.html.j2",
            context={
                "request": request,
                "title": f"Project Details - {project_details['display_name']}",
                "menu_items": get_menu_items(user),
                "project": project_details,
                "user": user,
                "user_roles": user_roles,
                "ServiceAdapter": ServiceAdapter,
                # How a service is chosen, and where it IS configured when it has no
                # project-wide settings. Both derived from the registry.
                "service_selection_labels": selection_labels,
                "service_config_hint": project_step_config_hint,
                "prometheus_available": prometheus_available,
                "argocd_available": argocd_available,
                "approval_notices": approval_notices,
                "backups_available": backups_available,
                "current_cluster": current_cluster,
                "cluster_base_domains": cluster_base_domains,
                "csrf_token": csrf_token,
                "pending_rollout": pending_rollout,
                "service_config_sections": SERVICE_CONFIG_MODAL_FLOWS,
                "deployment_service_actions": deployment_service_actions,
                # The status labels per row of the deployment table, and the bundled ArgoCD
                # state the last-sync column reads from.
                "deployment_status_tags": deployment_status_tags,
                "deployment_argocd": argocd_statuses,
                # The per-deployment service-owned blocks, keyed by deployment name.
                "deployment_service_sections": deployment_service_sections,
                # The backups block of the open deployment, for the Backups tab. Empty on
                # every other tab, and for a project that has nothing to back up.
                "backups_sections": backups_sections,
                # The audit of this project, for the Audit tab. Empty on every other tab and
                # for whoever may not read it.
                "audit_rijen": [],
                "audit_fout": False,
                **audit_context,
                # The detail-page sections the project's services own, in place of the
                # hardcoded per-service includes.
                "service_detail_sections": service_detail_sections,
                **token_context,
                **build_lotc_project_details(
                    request,
                    user=user,
                    project=project_details,
                    deployment_open=deployment_open,
                    lege_tabs=lege_tabs,
                ),
            },
        )
        if token_context.get("nieuw_token"):
            # This response carries a token that is valid for months and is shown only once.
            # Without this header a browser may put the page on disk and the back button shows
            # it again, also after it is gone server-side. The same header as on
            # GET /api/v2/projects/{p}/api-key.
            response.headers["Cache-Control"] = "no-store"
        return response

    except HTTPException:
        raise
    except Exception as e:
        log_render_failure(logger, "de projectpagina", e)
        raise HTTPException(
            status_code=500, detail="De projectpagina kon niet worden opgebouwd. Probeer het over een minuut opnieuw."
        ) from e


def _argocd_unavailable_result(app_name: str, message: str, source: str = "Application") -> dict[str, Any]:
    return {
        "app_name": app_name,
        "available": False,
        "health": "Unknown",
        "sync": "Unknown",
        "errors": [{"resource": source, "message": message}],
    }


def _annotate_argocd_error_ages(errors: list[dict[str, Any]]) -> None:
    """Add the Dutch ``age`` field in-place for entries that carry a timestamp."""

    now = datetime.now(UTC)
    for error in errors:
        ts = error.get("timestamp")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts)
            diff_min = int((now - dt).total_seconds() / 60)
            if diff_min < 1:
                error["age"] = "zojuist"
            elif diff_min < 60:
                error["age"] = f"{diff_min} min geleden"
            else:
                error["age"] = f"{diff_min // 60} uur geleden"
        except ValueError, TypeError:
            # No usable timestamp: the entry simply gets no age.
            pass


async def _fetch_argocd_deployment_status(
    project_name: str,
    deployment: dict[str, Any],
    argo: Any,
    kubectl: Any,
    deployment_state: DeploymentState | None = None,
    project_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch the ArgoCD status of one deployment, with interpreted errors when unhealthy.

    ``deployment_state`` is what the services report about this deployment (see
    ``collect_deployment_state``). It only decides whether the pod summary is worth asking
    for: a deployment whose pods are MEANT to be absent, asleep or switched off, would
    otherwise be told "nothing is running", which is true and not a problem.

    ``project_data`` lets the card name an image in the project's own private registry instead
    of its bare registry URL.
    """
    from opi.services.deployment_diagnostics import (
        conditions_to_errors,
        describe_image_pull_failures,
        gather_deployment_errors,
        gather_sync_deviations,
        summarize_component_pods,
    )
    from opi.services.event_interpreter import interpret_argocd_errors
    from opi.utils.naming import generate_argocd_application_name

    deployment_name = deployment.get("name") or ""
    app_name = generate_argocd_application_name(project_name, deployment_name)
    try:
        status_data = await argo.get_application_status(app_name)
        if not status_data:
            return _argocd_unavailable_result(app_name, "Application not found in ArgoCD")

        status = status_data.get("status", {})
        health = status.get("health", {})
        sync = status.get("sync", {})
        operation_state = status.get("operationState", {})
        app_health = health.get("status", "Unknown")

        # Components intentionally scaled to zero, by the watcher or by hand: their resources
        # must not be reported as a live problem.
        disabled_components = frozenset(
            ref
            for comp in deployment.get("components", []) or []
            if comp.get("disabled") and (ref := comp.get("reference"))
        )

        # What is actually serving traffic, per component. Deliberately only on the unhealthy
        # branch, next to the diagnostics that already cost a cluster call: a green deployment
        # answers "is it running" with its own badge.
        pod_summaries: list[Any] = []

        if app_health != "Healthy":
            # Not healthy: the full (more expensive) diagnostics, which already include the
            # app-level conditions along with the resource tree and namespace events.
            raw_errors = await gather_deployment_errors(
                argo=argo,
                kubectl=kubectl,
                app_name=app_name,
                base_namespace=deployment.get("namespace", ""),
                cluster=deployment.get("cluster", ""),
                deployment_name=deployment_name,
                status_data=status_data,
                disabled_components=disabled_components,
            )
            expects_no_pods = deployment_state is not None and deployment_state.expects_no_application_pods
            if kubectl is not None and not expects_no_pods:
                # No net of its own: get_application_pods is best-effort by contract, and a
                # non-zero exit or unparsable output logs and returns [].
                pods = await kubectl.get_application_pods(
                    get_prefixed_namespace(deployment.get("cluster", ""), deployment.get("namespace", "")),
                    deployment_name,
                )
                pod_summaries = summarize_component_pods(pods, deployment=deployment, project_data=project_data)
        else:
            # A healthy last-known state can still hide a fresh ComparisonError, so the cheap
            # app-level conditions are read unconditionally, at no extra API call, and a
            # render or compare error is not filtered out on the moment it matters.
            raw_errors = conditions_to_errors(status_data)

        component_names = [c.get("reference") for c in deployment.get("components", []) or [] if c.get("reference")]
        errors = interpret_argocd_errors(
            raw_errors,
            deployment_name=deployment_name,
            component_names=component_names,
            serving_components={s.reference for s in pod_summaries if s.is_serving},
            project_data=project_data,
        )
        _annotate_argocd_error_ages(errors)

        # Deviations next to errors: which resources keep the badges off green, and why. Only
        # computed when there is something to deviate from; green stays quiet.
        deviations: list[dict[str, str]] = []
        if sync.get("status") == "OutOfSync" or (app_health == "Progressing" and not errors):
            deviations = gather_sync_deviations(
                status_data, deployment_name=deployment_name, disabled_components=disabled_components
            )

        last_sync = operation_state.get("finishedAt")
        if not last_sync and sync.get("status") == "Synced":
            last_sync = status.get("reconciledAt")

        # Since when the running sync has been running, and whether it is stuck with that:
        # without those two, a phase of ``Running`` cannot be judged.
        operation = read_sync_operation(status_data)
        operation_duration = (
            format_duration(operation_age_seconds(operation)) if operation.ongoing and operation.started_at else None
        )

        return {
            "app_name": app_name,
            "available": True,
            "health": health.get("status", "Unknown"),
            "health_message": health.get("message"),
            "sync": sync.get("status", "Unknown"),
            "revision": sync.get("revision", "")[:7] if sync.get("revision") else None,
            "last_sync": last_sync,
            "operation_phase": operation_state.get("phase"),
            "operation_message": operation_state.get("message"),
            "operation_duration": operation_duration,
            "operation_is_stuck": is_stuck(operation, threshold_seconds=STUCK_OPERATION_THRESHOLD_SECONDS),
            "errors": errors,
            "deviations": deviations,
            "pods": pod_summaries,
            # Costs no cluster call: the observer counted this cluster at most a minute ago.
            # Read on the healthy branch too, deliberately: if ArgoCD ever says otherwise, the
            # card still has to say what is wrong.
            "image_pull_failures": describe_image_pull_failures(deployment, project_data),
        }
    except Exception:
        # The message goes into ``errors``, which draws the card; what ArgoCD throws here
        # names its own address. In full in the log, with the reference next to it.
        logger.exception("Failed to fetch ArgoCD status for %s", app_name)
        return _argocd_unavailable_result(
            app_name,
            with_reference(
                "De status is nu niet op te halen. Probeer het over een minuut opnieuw.", current_reference()
            ),
            source="API",
        )


@web_router.get("/dashboard/resource-usage", response_class=HTMLResponse)
@action(Action.PORTAL_READ)
@requires_sso
async def dashboard_resource_usage_fragment(request: Request) -> HTMLResponse:
    """The resource usage of the dashboard, fetched separately.

    The Prometheus queries take too long to keep the page waiting, so the page loads this
    block on its own, as the project page does.
    """
    from opi.services.project_store import get_project_store

    user = get_current_user(request)
    user_email = (user or {}).get("email", "")

    # The same set of namespaces as the dashboard route: only the projects this user may see,
    # or this fragment would show more than the page.
    from opi.core.cluster_config import get_prefixed_namespace

    all_namespaces: list[str] = []
    user_projects: list[dict] = []
    for project in get_project_store().get_all():
        if not is_user_authorized_for_project(project.name, user_email, audit=False):
            continue

        project_data = project.data or {}
        namespaces: list[str] = []
        for deployment in project_data.get("deployments", []):
            cluster = deployment.get("cluster")
            namespace = deployment.get("namespace")
            if not (cluster and namespace):
                continue
            k8s_namespace = get_prefixed_namespace(cluster, namespace)
            if k8s_namespace not in namespaces:
                namespaces.append(k8s_namespace)
            if k8s_namespace not in all_namespaces:
                all_namespaces.append(k8s_namespace)

        user_projects.append(
            {
                "name": project.name,
                "display_name": project_data.get("display-name", project.name),
                "namespaces": namespaces,
            }
        )

    metrics, prometheus_available, pod_count = await collect_dashboard_metrics(all_namespaces, user_projects)
    total_cpu_usage = sum(project.get("cpu_cores", 0) for project in user_projects)
    total_memory_usage = sum(project.get("memory_mb", 0) or 0 for project in user_projects)

    return render(
        request,
        template="bg/_dashboard-usage.html.j2",
        context={
            "request": request,
            "metrics": metrics,
            "prometheus_available": prometheus_available,
            "projects": user_projects,
            # Both totals, like the dashboard route. The environment is on StrictUndefined,
            # so a template asking for a missing one is a 500 on the whole fragment instead of
            # an empty line.
            "total_cpu_usage": total_cpu_usage,
            "total_memory_usage": total_memory_usage,
            # The Pods card stands in the page, but its number comes out of these queries;
            # the fragment slides it over out of band.
            "pod_count": pod_count,
        },
    )


@web_router.get("/projects/details/{project_name}/resource-usage", response_class=HTMLResponse)
@action(Action.PROJECT_READ)
@requires_sso
async def project_resource_usage_fragment(request: Request, project_name: str) -> HTMLResponse:
    """The project-wide CPU and memory totals, lazily loaded over HTMX.

    A project's deployments, every PR environment included, share one namespace, so the
    per-deployment metrics blocks never show the total. This sums across the project's
    namespaces, with the same Prometheus queries the dashboard's resource card uses.

    Memory is the working set, what is actually resident, and not the limit.
    """
    from opi.core.cluster_config import get_prefixed_namespace
    from opi.core.config import settings

    user = get_current_user(request)
    user_email = user.get("email", "").lower()

    if not is_user_authorized_for_project(project_name, user_email):
        raise HTTPException(status_code=403, detail="Not authorized")

    project = get_project_store().get(project_name)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    current_cluster = settings.CLUSTER_MANAGER
    namespaces = sorted(
        {
            get_prefixed_namespace(current_cluster, d["namespace"])
            for d in (project.data or {}).get("deployments", [])
            if d.get("cluster") == current_cluster and d.get("namespace")
        }
    )

    ctx: dict[str, Any] = {"project": project, "usage": None, "usage_error": None}

    if namespaces:
        # Above the try, because the except below names these errors.
        from opi.connectors.metrics_backend import MetricsUnavailableError
        from opi.connectors.prometheus import get_metrics_connector
        from opi.services.metrics_queries import namespace_usage_queries

        try:
            prom = await get_metrics_connector()
            if not prom.is_connected:
                ctx["usage_error"] = "Metrics zijn op dit moment niet beschikbaar."
            else:

                async def _scalar(promql: str) -> float:
                    result = await prom.query(promql)
                    if result and result[0].get("value"):
                        return float(result[0]["value"][1])
                    return 0.0

                queries = namespace_usage_queries(namespaces)
                cpu_used = await _scalar(queries["cpu_cores"])
                cpu_limit = await _scalar(queries["cpu_limit_cores"])
                mem_used = await _scalar(queries["memory_bytes"])
                mem_limit = await _scalar(queries["memory_limit_bytes"])
                pods = await _scalar(queries["pod_count"])

                ctx["usage"] = {
                    "cpu_used": cpu_used,
                    "cpu_limit": cpu_limit,
                    "cpu_pct": min(100, round(cpu_used / cpu_limit * 100)) if cpu_limit > 0 else 0,
                    "mem_used": mem_used,
                    "mem_limit": mem_limit,
                    "mem_pct": min(100, round(mem_used / mem_limit * 100)) if mem_limit > 0 else 0,
                    "pods": int(pods),
                    "deployments": len(
                        [d for d in (project.data or {}).get("deployments", []) if d.get("cluster") == current_cluster]
                    ),
                }
        except MetricsUnavailableError as error:
            # The metrics source fell away DURING the query, which for the reader is the same
            # as it not being there: nothing to measure now.
            logger.warning("Metrics niet beschikbaar voor %s: %s", project_name, error)
            ctx["usage_error"] = "Metrics zijn op dit moment niet beschikbaar."
        except Exception:
            # Never the message of the metrics layer: for an unreachable Prometheus that is
            # the host name, the port and the errno of that service, on a project page. The
            # full error goes in the log, tied to it by the reference.
            logger.exception("Ophalen van het resourcegebruik voor %s mislukt", project_name)
            # The heading of the notice already says WHAT is failing, so the line below it
            # says what you can do about it.
            ctx["usage_error"] = with_reference("Probeer het over een minuut opnieuw.", reference_for(request))

    return render(
        request,
        template="bg/_resource-usage.html.j2",
        context=ctx,
    )


@web_router.get("/projects/details/{project_name}/argocd-status/{deployment_name}", response_class=HTMLResponse)
@action(Action.DEPLOYMENT_READ)
@requires_sso
async def argocd_status_fragment(
    request: Request, project_name: str, deployment_name: str, prefix: str = ""
) -> HTMLResponse:
    """The ArgoCD status of a single deployment, lazily loaded over HTMX."""
    from opi.connectors.argo import ArgoAuthConfigurationError, create_argo_connector
    from opi.connectors.kubectl import create_kubectl_connector
    from opi.core.config import settings
    from opi.utils.naming import generate_argocd_application_name

    user = get_current_user(request)
    user_email = user.get("email", "").lower()

    if not is_user_authorized_for_project(project_name, user_email):
        raise HTTPException(status_code=403, detail="Not authorized")

    project = get_project_store().get(project_name)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    deployment = Project(project.data or {}).find("deployments", name=deployment_name)
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    # What the services report about this deployment. Read from the project file and not from
    # the cluster: zero replicas can also mean something went wrong, and the card has to tell
    # those two apart. Computed BEFORE the fetch, because the fetch needs it as well: it
    # decides whether asking the cluster which pod is serving makes sense at all.
    deployment_state = collect_deployment_state(project.data or {}, deployment_name)

    # Fail fast, for the same reason as on /dashboard: this fragment loads inside a page, so
    # sitting out an ArgoCD restart holds up a user and not a task. See
    # "docs/post-mortems/argocd-server-herstart-elke-40-minuten.md".
    #
    # Without a token and without a password there is no connector to make, and that has to
    # give the same card as an ArgoCD that does not answer, not a 500 in this fragment.
    argo = None
    try:
        argo = create_argo_connector(connect_retry_seconds=0)
    except ArgoAuthConfigurationError as auth_error:
        logger.warning(f"Geen ArgoCD-credentials voor dit fragment: {auth_error}")
    if argo is None or not await argo.ensure_authenticated():
        status = _argocd_unavailable_result(
            generate_argocd_application_name(project_name, deployment_name), "ArgoCD niet beschikbaar"
        )
    else:
        status = await _fetch_argocd_deployment_status(
            project_name, deployment, argo, create_kubectl_connector(), deployment_state, project.data or {}
        )

    return render(
        request,
        template="bg/_argocd-deployment-card.html.j2",
        context={
            "request": request,
            "project": project,
            "deployment": deployment,
            "argocd_status": {deployment_name: status},
            "_argocd_card_id_prefix": prefix or deployment_name,
            "current_cluster": settings.CLUSTER_MANAGER,
            "deployment_states": {deployment_name: deployment_state},
        },
    )


@web_router.get("/projects/details/{project_name}/oom-status/{deployment_name}", response_class=HTMLResponse)
@action(Action.DEPLOYMENT_READ)
@requires_sso
async def oom_status_fragment(request: Request, project_name: str, deployment_name: str) -> HTMLResponse:
    """The memory shortage of ONE deployment, as a fragment of its own, lazily loaded.

    A fragment and not a block in the ArgoCD card, because it looks at something else: that
    card reports what is wrong NOW, this reports what happened in the last day, which
    disappears from that card as soon as the auto-tuner has put it right. It also talks to
    Prometheus while the card does not, and a metrics source that does not resolve costs the
    whole DNS and retry chain.

    There is exactly ONE per page, like the ArgoCD card: the deployment panel renders only the
    deployment from the path, so this is no query per row.
    """
    from opi.services.resource_tuning_service import (
        OOM_OBSERVATION_WINDOW_HOURS,
        observe_recent_oom_kills,
    )

    user = get_current_user(request)
    user_email = user.get("email", "").lower()

    if not is_user_authorized_for_project(project_name, user_email):
        raise HTTPException(status_code=403, detail="Not authorized")

    project = get_project_store().get(project_name)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project_data = project.data or {}
    deployment = Project(project_data).find("deployments", name=deployment_name)
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    # What the tuner has already done. Free, because it stands in the project file that is
    # loaded above anyway, and unlike the measurement it outlives every retention period.
    # Newest first, so the first oom-watcher entry per component is the tuner's last act.
    tunes: list[dict[str, Any]] = []
    for comp in deployment.get("components") or []:
        reference = comp.get("reference")
        if not reference:
            continue
        entries = [
            entry
            for entry in ((comp.get("resources") or {}).get("history") or [])
            if entry.get("source") == "oom-watcher"
        ]
        if not entries:
            continue
        newest = entries[0]
        tunes.append(
            {
                "component": reference,
                "timestamp": newest.get("timestamp"),
                "limit": (newest.get("limits") or {}).get("memory"),
                "count": len(entries),
            }
        )

    observations = await observe_recent_oom_kills(project_data, deployment_name)

    return render(
        request,
        template="bg/_oom-status.html.j2",
        context={
            "request": request,
            "project": project,
            "deployment": deployment,
            "oom_components": [o.component for o in observations],
            "oom_tunes": sorted(tunes, key=lambda t: t["component"]),
            "oom_window_hours": OOM_OBSERVATION_WINDOW_HOURS,
        },
    )


#: The series the metrics fragment draws. A measurement can also hold limits only, and those
#: come from the deployment definition and not from a measurement, so they do not count
#: towards "is anything measured".
METRIC_SERIES = ("cpu", "memory", "network_in", "network_out", "disk_read", "disk_write")


def _has_measurements(metrics: dict[str, dict[str, Any]], pvc_storage: dict[str, dict[str, Any]]) -> bool:
    """Did Prometheus return a value anywhere?

    If not, that is a STATE and not emptiness, and the fragment says so with a notice instead
    of with six empty charts.
    """
    if any(pvc.get("values") for pvc in pvc_storage.values()):
        return True
    return any(measurement.get(series) for measurement in metrics.values() for series in METRIC_SERIES)


@web_router.get("/projects/details/{project_name}/metrics/{deployment_name}", response_class=HTMLResponse)
@action(Action.DEPLOYMENT_READ)
@requires_sso
async def deployment_metrics_fragment(
    request: Request, project_name: str, deployment_name: str, duration: int = 60
) -> HTMLResponse:
    """The metrics of a single deployment, lazily loaded over HTMX."""
    allowed_durations = {60, 120, 360, 720, 1440}
    if duration not in allowed_durations:
        duration = 60
    # The step scales with the duration, to keep roughly 12 to 20 points per chart.
    if duration <= 120:
        step = 5
    elif duration <= 360:
        step = 15
    elif duration <= 720:
        step = 30
    else:
        step = 60

    user = get_current_user(request)
    user_email = user.get("email", "").lower()

    if not is_user_authorized_for_project(project_name, user_email):
        raise HTTPException(status_code=403, detail="Not authorized")

    project = get_project_store().get(project_name)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project_data = project.data or {}
    deployment = Project(project_data).find("deployments", name=deployment_name)
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    base_namespace = deployment.get("namespace")
    cluster = deployment.get("cluster")
    components = deployment.get("components", [])
    helm_charts = deployment.get("helm-charts", [])

    metrics: dict[str, dict[str, Any]] = {}
    discovered_workloads: list[dict[str, Any]] = []
    deployment_volumes: dict[str, dict[str, Any]] = {}
    # A Prometheus that does not answer is something else than one that answers with nothing,
    # and the fragment must not give those two the same notice.
    prometheus_reachable = True

    if base_namespace and cluster:
        try:
            from opi.connectors.prometheus import get_metrics_connector
            from opi.core.cluster_config import get_prefixed_namespace
            from opi.services.metrics_queries import (
                TimeWindow,
                deployment_pod_prefixes,
                discover_workloads,
                pvc_storage,
                timeseries_per_workload,
                workload_pod_prefixes,
            )

            prom = await get_metrics_connector()
            prometheus_reachable = prom.is_connected
            if prom.is_connected:
                k8s_namespace = get_prefixed_namespace(cluster, base_namespace)
                window = TimeWindow(duration_minutes=duration, step_minutes=step)

                if components:
                    component_names = [c.get("reference") for c in components if c.get("reference")]
                    if component_names:
                        metrics = await timeseries_per_workload(
                            prom, k8s_namespace, deployment_pod_prefixes(deployment_name, component_names), window
                        )
                elif helm_charts:
                    workloads = await discover_workloads(prom, k8s_namespace)
                    if workloads:
                        discovered_workloads = workloads
                        metrics = await timeseries_per_workload(
                            prom, k8s_namespace, workload_pod_prefixes(workloads), window
                        )

                try:
                    pvc_data = await pvc_storage(prom, k8s_namespace, window)
                    if pvc_data:
                        # Only the PVCs of this deployment; a PVC name starts with its name.
                        prefix = f"{deployment_name}-"
                        deployment_volumes = {name: data for name, data in pvc_data.items() if name.startswith(prefix)}
                except Exception as pvc_error:
                    logging.getLogger(__name__).warning(
                        f"Failed to fetch PVC storage for deployment {deployment_name}: {pvc_error}"
                    )
        except Exception as metrics_error:
            # A query that breaks is not "nothing measured yet": nothing was fetched because
            # the measurement itself failed.
            prometheus_reachable = False
            logging.getLogger(__name__).warning(f"Failed to fetch Prometheus metrics: {metrics_error}")

    # The template reads ``.name`` and ``.components`` as attributes.
    class DeploymentContext:
        def __init__(self, name: str, components: list):
            self.name = name
            self.components = [type("C", (), {"reference": c.get("reference")})() for c in components]

    deployment_ctx = DeploymentContext(deployment_name, components)

    # A deployment on ANOTHER cluster yields no measurements here, because this OPI only
    # queries the Prometheus of its own cluster. Without this flag that would read as "no
    # data", and the reader would look for the fault in their application.
    from opi.core.config import settings

    other_cluster = cluster if cluster and cluster != settings.CLUSTER_MANAGER else ""

    return render(
        request,
        template="bg/_deployment-metrics.html.j2",
        context={
            "request": request,
            "project_name": project_name,
            "deployment": deployment_ctx,
            "metrics": metrics,
            "discovered_workloads": discovered_workloads,
            "pvc_storage": deployment_volumes,
            "duration": duration,
            "prometheus_bereikbaar": prometheus_reachable,
            "ander_cluster": other_cluster,
            "eigen_cluster": settings.CLUSTER_MANAGER,
            "metingen_leeg": not _has_measurements(metrics, deployment_volumes),
        },
    )


def _projects_for_user(user: dict) -> list[dict]:
    """The projects this user may see, with the data the overview shows.

    Apart from the route so the build sits in one place: /projects serves both the whole page
    and, over hx-select, the part the search field refreshes.
    """
    user_email = user.get("email", "").lower()

    user_projects: list[dict] = []
    for project in get_project_store().get_all():
        project_name = project.name
        if not is_user_authorized_for_project(project_name, user_email, audit=False):
            continue
        try:
            project_data = project.data or {}
            user_projects.append(
                {
                    "name": project_name,
                    "display_name": project_data.get("display-name", project_name),
                    "description": project_data.get("description", ""),
                    "users": human_members(project),
                    "user_roles": get_user_roles_for_project(project_name, user_email),
                    "services": project_data.get("services", []),
                    "clusters": project_data.get("clusters", []),
                    "components": project_data.get("components", []),
                    "deployments": project_data.get("deployments", []),
                }
            )
        except Exception as e:
            logger.warning(f"Failed to load project data for {project_name}: {e}")
            continue

    user_projects.sort(key=lambda p: p["display_name"] or p["name"])
    return user_projects


@web_router.get("/platform", response_class=HTMLResponse)
@action(Action.PORTAL_READ)
@requires_sso
async def platform_map(request: Request) -> Response:
    """The map of the platform: the Operations Manager and the systems around it.

    For everyone who is signed in and has access to ZAD, not public: the real gate is the
    middleware, as it is for /projects.
    """
    user = get_current_user(request)
    return render(
        request,
        template="bg/platform.html.j2",
        context={
            "request": request,
            "menu_items": get_menu_items(user),
            "navigation": get_navigation(user, current_path="/platform"),
            "cluster": current_cluster_folder(),
        },
    )


@web_router.get("/platform.json")
@action(Action.PORTAL_READ)
@requires_sso
async def platform_map_json(request: Request) -> JSONResponse:
    """The graph behind /platform, from the platform.json in the repo."""
    return JSONResponse(platform_graph().as_json())


@web_router.get("/projects", response_class=HTMLResponse)
@action(Action.PORTAL_READ)
@requires_sso
async def projects_overview(request: Request):
    """The projects overview: only the projects whose member list holds this user."""
    try:
        # ``or {}`` because get_current_user can give None. That falls out safely here: an
        # empty user has no email address, no project membership matches on it, so the list
        # comes out empty rather than complete.
        user = get_current_user(request) or {}
        user_projects = _projects_for_user(user)
        laatst_gewijzigd = await get_project_store().last_modified_all()

        return render(
            request,
            template="bg/projects.html.j2",
            context={
                "request": request,
                "menu_items": get_menu_items(user),
                "projects": user_projects,
                "user": user,
                **build_lotc_projects(request, user=user, projects=user_projects, laatst_gewijzigd=laatst_gewijzigd),
            },
        )

    except Exception as e:
        log_render_failure(logger, "het projectoverzicht", e)
        raise HTTPException(
            status_code=500,
            detail="Het projectoverzicht kon niet worden opgebouwd. Probeer het over een minuut opnieuw.",
        ) from e


@web_router.get("/cli", response_class=HTMLResponse)
@action(Action.PORTAL_READ)
@requires_sso
async def cli_pagina(request: Request):
    """The ZAD CLI: what it is, how to install it, what it does.

    A signpost and not a manual: the repository keeps its own documentation, which runs ahead
    of anything that would stand here.
    """
    return _signpost(request, "cli.html.j2", "bg/cli.html.j2", "/cli")


@web_router.get("/actions", response_class=HTMLResponse)
@action(Action.PORTAL_READ)
@requires_sso
async def actions_pagina(request: Request):
    """The ZAD Actions: deploying from your own pipeline."""
    return _signpost(request, "actions.html.j2", "bg/actions.html.j2", "/actions")


def _signpost(request: Request, roos: str, lotc: str, path: str, **context: object):
    """A page without project data: the shell, its navigation and whatever ``context`` adds."""
    user = get_current_user(request)
    from opi.http.render.navigation_lotc import get_navigation

    return render(
        request,
        template=lotc,
        context={
            "request": request,
            "menu_items": get_menu_items(user),
            "navigation": get_navigation(user, current_path=path),
            **context,
        },
    )


@web_router.get("/account", response_class=HTMLResponse)
@action(Action.PORTAL_READ)
@requires_sso
async def account_pagina(request: Request):
    """Your own account: what is in the session, plus your second factor.

    ZAD keeps no profile of its own; the name and the email address come from the login
    service. The second factor is the only thing that CAN be set here.
    """
    user = get_current_user(request)
    from opi.http.render.navigation_lotc import get_navigation

    otp_status = await UserOtpService().status(str(user.get("email", "")).lower())

    return render(
        request,
        template="bg/account.html.j2",
        context={
            "request": request,
            "menu_items": get_menu_items(user),
            "navigation": get_navigation(user, current_path="/account"),
            "account": {
                "name": user.get("name") or user.get("display_name") or user.get("email", "Onbekend"),
                "email": user.get("email", ""),
                "organisatie": user.get("organization") or user.get("organisation") or "",
            },
            "otp_status": otp_status,
        },
    )


@web_router.get("/weergave")
async def kies_weergave(request: Request, scheme: str = "", terug: str = "/"):
    """Remember the colour-scheme choice and go back where you came from.

    DELIBERATELY WITHOUT ``@requires_sso``: the choice only sets a cookie and reads no data.

    Server-side and not in localStorage, so the server renders the page in the right state at
    once and nothing flashes. NLDD reads ``data-scheme`` on ``<html>``, which the shell sets
    from this cookie.

    The return path comes from the URL and is therefore held strictly to a path on this site:
    a value starting with ``//`` is not a relative path but another domain, which would make
    this route an open redirect.
    """
    from opi.http.render.lotc_switch import SCHEME_COOKIE, SCHEMES

    destination = local_redirect_path(terug)
    response = RedirectResponse(url=destination, status_code=303)
    response.set_cookie(
        SCHEME_COOKIE,
        scheme if scheme in SCHEMES else "",
        max_age=60 * 60 * 24 * 365,
        samesite="lax",
        httponly=False,
    )
    return response


@web_router.get("/about")
async def about_platform() -> RedirectResponse:
    """The about page has become the introduction; the old links keep working.

    DELIBERATELY WITHOUT ``@requires_sso``: a redirect to the introduction, which is open too.
    """
    return RedirectResponse(url="/introductie", status_code=301)


@web_router.get("/security", response_class=HTMLResponse)
async def security_policy(request: Request) -> HTMLResponse:
    """The security policy, ``SECURITY.md`` as a page.

    DELIBERATELY WITHOUT ``@requires_sso``: whoever wants to report a vulnerability is usually
    not signed in.
    """
    user = get_current_user(request)
    return render(
        request,
        template="bg/security.html.j2",
        context={
            "request": request,
            "menu_items": get_menu_items(user),
            "navigation": get_navigation(user, current_path="/security"),
            "security_policy_html": security_policy_html(),
        },
    )


@dev_router.get("/test-template-variables", response_class=HTMLResponse)
@action(Action.PORTAL_READ)
@requires_sso
async def test_template_variables(request: Request):
    """Test route for debugging Jinja variables in component tags."""
    try:
        from opi.http.render.navigation_lotc import get_navigation

        user = get_current_user(request)
        return templates_lotc.TemplateResponse(
            request,
            "test-template-variables.html.j2",
            {
                "request": request,
                "menu_items": get_menu_items(user),
                "navigation": get_navigation(user, current_path=""),
            },
        )
    except Exception as e:
        log_render_failure(logger, "de testpagina van de sjabloonvariabelen", e)
        raise HTTPException(
            status_code=500,
            detail="De testpagina van de sjabloonvariabelen kon niet worden opgebouwd; het sjabloon rendert niet. De volledige fout staat in de log.",
        ) from e


@dev_router.get("/example", response_class=HTMLResponse)
@action(Action.PORTAL_READ)
@requires_sso
async def example_page(request: Request):
    """A simple example page with just a header."""
    try:
        user = get_current_user(request)
        return templates_lotc.TemplateResponse(
            request,
            "example.html.j2",
            {"request": request, "title": "Example Page", "menu_items": get_menu_items(user)},
        )

    except Exception as e:
        log_render_failure(logger, "de voorbeeldpagina", e)
        raise HTTPException(
            status_code=500,
            detail="De voorbeeldpagina kon niet worden opgebouwd; het sjabloon rendert niet. De volledige fout staat in de log.",
        ) from e


@web_router.get("/tools", response_class=HTMLResponse)
@action(Action.TOOLS_USE)
@requires_sso
async def tools_page(request: Request):
    """The AGE encryption and decryption tools page."""
    try:
        user = get_current_user(request)
        return templates_lotc.TemplateResponse(
            request,
            "tools.html.j2",
            {"request": request, "title": "AGE Encryption Tools", "menu_items": get_menu_items(user)},
        )

    except Exception as e:
        log_render_failure(logger, "de gereedschapspagina", e)
        raise HTTPException(
            status_code=500,
            detail="De gereedschapspagina kon niet worden opgebouwd; het sjabloon rendert niet. De volledige fout staat in de log.",
        ) from e


@web_router.post("/tools/encrypt")
@action(Action.TOOLS_USE)
@requires_sso
async def encrypt_text(request: Request):
    """Encrypt text with an AGE public key."""
    try:
        from fastapi.responses import JSONResponse

        from opi.utils.age import encrypt_age_content

        form_data = await request.form()
        public_key = str(form_data.get("public_key", "")).strip()
        input_text = str(form_data.get("input_text", "")).strip()

        if not public_key:
            return JSONResponse(content={"error": "Public key is required"}, status_code=400)

        if not input_text:
            return JSONResponse(content={"error": "Input text is required"}, status_code=400)

        encrypted_content = await encrypt_age_content(input_text, public_key)

        return JSONResponse(content={"success": True, "result": encrypted_content}, status_code=200)

    except Exception:
        logger.exception("Versleutelen mislukt")
        message = "Versleutelen is niet gelukt. Controleer de publieke sleutel en de invoer."
        return JSONResponse(
            content={"error": with_reference(message, reference_for(request))},
            status_code=500,
        )


@web_router.post("/tools/decrypt")
@action(Action.TOOLS_USE)
@requires_sso
async def decrypt_text(request: Request):
    """Decrypt AGE-encrypted text with a private key."""
    try:
        from fastapi.responses import JSONResponse

        from opi.utils.age import decrypt_age_content

        form_data = await request.form()
        private_key = str(form_data.get("private_key", "")).strip()
        input_text = str(form_data.get("input_text", "")).strip()

        if not private_key:
            return JSONResponse(content={"error": "Private key is required"}, status_code=400)

        if not input_text:
            return JSONResponse(content={"error": "Input text is required"}, status_code=400)

        decrypted_content = await decrypt_age_content(input_text, private_key)

        return JSONResponse(content={"success": True, "result": decrypted_content}, status_code=200)

    except Exception:
        logger.exception("Ontsleutelen mislukt")
        message = "Ontsleutelen is niet gelukt. Controleer de private sleutel en of de invoer echt AGE-versleuteld is."
        return JSONResponse(
            content={"error": with_reference(message, reference_for(request))},
            status_code=500,
        )


def _v2_task_to_template_context(task: dict, project_name: str) -> dict:
    """Map a V2 async task dict to the template context expected by progress fragments.

    Shared by all progress polling endpoints (inline, modal, JSON).
    """
    db_status = task.get("status", "pending")
    if db_status in ("pending", "claimed", "running"):
        template_status = "running"
    elif db_status == "completed":
        result = task.get("result")
        template_status = "failed" if isinstance(result, dict) and result.get("status") == "failed" else "completed"
    else:
        template_status = "failed"

    subtasks = task.get("subtasks") or []
    task_hierarchy = _build_task_hierarchy(subtasks, template_status)

    error = task.get("error_message")
    component_failures = None
    result = task.get("result")
    if isinstance(result, dict):
        processing = result.get("processing")
        if isinstance(processing, dict):
            component_failures = processing.get("component_failures")
            if not error:
                error = processing.get("error")

    details_purged = bool(task.get("details_purged_at"))

    return {
        "progress": task.get("progress_percent", 0),
        # A purged task has no step line left, and the default would show it as running.
        "current_step": step_label(task.get("current_step"))
        or ("Details niet meer beschikbaar" if details_purged else "Verwerking gestart..."),
        "tasks": task_hierarchy,
        "total_duration_seconds": _total_duration_seconds(subtasks) if template_status == "completed" else None,
        "status": template_status,
        "error": error,
        "component_failures": component_failures,
        "project_name": project_name,
        # The working data of a finished task is purged after an hour, so there are no steps
        # or log left to show.
        "details_purged": details_purged,
    }


def _step_duration_seconds(step: dict) -> float | None:
    """The duration of a finished step in seconds, or None when it cannot be determined."""
    started, completed = step.get("started_at"), step.get("completed_at")
    if not started or not completed:
        return None
    try:
        duration = datetime.fromisoformat(completed) - datetime.fromisoformat(started)
    except ValueError:
        return None
    return max(duration.total_seconds(), 0.0)


def _total_duration_seconds(subtasks: list[dict]) -> float | None:
    """From the first step's start to the last step's end, or None without timestamps."""
    started = [st["started_at"] for st in subtasks if st.get("started_at")]
    completed = [st["completed_at"] for st in subtasks if st.get("completed_at")]
    if not started or not completed:
        return None
    return _step_duration_seconds({"started_at": min(started), "completed_at": max(completed)})


def _build_task_hierarchy(subtasks: list[dict], task_status: str = "running") -> list[dict]:
    """Turn the flat subtask list into the nested hierarchy the template renders.

    A step with steps under it that is still marked running in a task that already ended shows
    the task's outcome, because a handler that raised never closed its phase.
    """
    main_tasks = []
    children: dict[str, list] = {}
    for st in subtasks:
        # ``subject`` is absent from a step written before it existed and from every step
        # that runs once per project, so .get keeps both cases a plain None.
        entry = {
            "name": st.get("name", ""),
            "status": st.get("status", "pending"),
            "error": st.get("error"),
            "subject": st.get("subject"),
        }
        parent = st.get("parent_id")
        if parent:
            children.setdefault(parent, []).append(entry)
        else:
            task_entry = {**entry, "id": st.get("id", ""), "subtasks": []}
            main_tasks.append(task_entry)
    for mt in main_tasks:
        mt["subtasks"] = children.get(mt["id"], [])
        if mt["subtasks"] and mt["status"] == "running" and task_status != "running":
            mt["status"] = task_status
    return main_tasks


def _require_task_access(request: Request, task: dict, project_name: str) -> None:
    """Raise 403 unless this user may follow this task.

    Either the user is authorized for the project, or they started the task themselves, which
    is what keeps a delete followable, the project being gone from the store before the task
    ends, and a creation too, it not being in the store yet.
    """
    user_email = (get_current_user(request) or {}).get("email", "").lower()
    if (task.get("created_by") or "").lower() != user_email and not is_user_authorized_for_project(
        project_name, user_email
    ):
        raise HTTPException(status_code=403, detail="Geen toegang tot dit project")


async def _require_task_of_project(request: Request, task_service: Any, project_name: str, task_id: str) -> dict | None:
    """The task with this id, but only if it is this project's and this user's to see.

    The task id alone must not be enough to read any task's steps. Both fragment routes sit
    under ``/projects/{project_name}/``, so they are scoped to it: the task has to be that
    project's own, and the user has to pass ``_require_task_access``. Returns None when there
    is no such task, so the caller answers 404.
    """
    task = await task_service.get_task(task_id)
    if task is None or task.get("project_name") != project_name:
        return None

    _require_task_access(request, task, project_name)
    return task


# PORTAL_READ and not a project right: the project gate in front of the handler answers
# 404 once the project is gone, and a project deletion ends exactly there, so its dialog
# polled 404s forever. The handler checks access itself (_require_task_of_project: the
# task is this project's, and the user started it or may see the project).
@web_router.get("/projects/{project_name}/task-progress/{task_id}", response_class=HTMLResponse)
@action(Action.PORTAL_READ)
@requires_sso
async def task_progress_fragment(request: Request, project_name: str, task_id: str) -> HTMLResponse:
    """The generic task-progress fragment HTMX polls."""
    from opi.http.task_helpers import get_task_service

    task_service = get_task_service(request)
    task = await _require_task_of_project(request, task_service, project_name, task_id)
    if task is None:
        return HTMLResponse(content="<p>Taak niet gevonden</p>", status_code=404)

    context = _v2_task_to_template_context(task, project_name)
    context["task_id"] = task_id
    context["progress_url"] = f"/projects/{project_name}/task-progress/{task_id}"
    context["on_complete"] = on_complete_for(task.get("task_type"))

    # Rendered once on purpose; see render_progress_fragment for why a second pass over the
    # rendered HTML would execute task text as Jinja.
    return HTMLResponse(content=render_progress_fragment(request, context))
