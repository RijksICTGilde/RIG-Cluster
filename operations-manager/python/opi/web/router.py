"""
Web routes for serving HTML pages (non-API endpoints).
"""

import asyncio  # noqa: TC003  # used at runtime by the module-level _background_tasks annotation
import copy
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.exc import SQLAlchemyError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from opi.manager.project_manager import ProjectManager

from datetime import UTC

from opi.core.auth_decorators import get_current_user, requires_sso
from opi.core.templates_lotc import templates_lotc
from opi.services.argocd_overview import get_project_argocd_statuses
from opi.services.catalog.deployment_health.disabled import deployment_disabled_state
from opi.services.catalog.publish_on_web.domain_config import (
    DomainSetting,
    get_domain_setting,
    pop_domain_setting,
    set_domain_setting,
)
from opi.services.component_values import ComponentValuesError
from opi.services.component_values import decode as decode_component_values
from opi.services.config_location import binding_label, project_step_config_hint
from opi.services.deployment_state import collect_deployment_state
from opi.services.project import Project
from opi.services.project_authorization import (
    get_user_role_for_project,
    is_user_authorized_for_project,
)
from opi.services.project_env_vars import read_user_env_vars
from opi.services.project_store import get_project_store
from opi.services.registry import collect_service_routers, find_deployment_action
from opi.utils.age import decrypt_password_smart, get_global_private_key
from opi.utils.csrf import ensure_csrf_token
from opi.utils.totp import totp_now
from opi.utils.yaml_util import load_yaml_from_string
from opi.web.lotc_switch import (
    STANDAARD_TAB,
    TABS_MET_DEPLOYMENT,
    TABS_MET_VOORWAARDE,
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
from opi.web.menu import get_menu_items
from opi.web.project_actions import build_project_action
from opi.web.stap_labels import stap_label
from opi.web.task_progress import create_task_and_render_progress, on_complete_for, render_progress_fragment

from ..utils.age import decrypt_age_content
from .metrics_explorer_router import metrics_explorer_router
from .router_approvals import approvals_router
from .router_attachments import attachments_router
from .router_detail_edit import detail_edit_router
from .router_self_service import check_subdomain_availability_web
from .router_tasks import tasks_router
from .router_usage import usage_router
from .router_user_admin import user_admin_router
from .router_wizard import wizard_router
from .router_wizard_attachments import wizard_attachments_router
from .services_router import services_router

logger = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task[None]] = set()

web_router = APIRouter()

# Include sub-routers
web_router.include_router(services_router)
web_router.include_router(metrics_explorer_router)
web_router.include_router(detail_edit_router)
web_router.include_router(wizard_router)
web_router.include_router(user_admin_router)
web_router.include_router(usage_router)
web_router.include_router(approvals_router)
web_router.include_router(attachments_router)
web_router.include_router(wizard_attachments_router)
web_router.include_router(tasks_router)

# Routers the services themselves deliver (RC-24): a service that owns a page block owns
# the endpoints that fill it (the backups fragment, the console/job modals), instead of
# leaving half the block behind in this router. Shared routers are mounted once.
for _service_router in collect_service_routers():
    web_router.include_router(_service_router)


@web_router.get("/")
async def root(request: Request):
    """De voordeur: het dashboard als je ingelogd bent, anders de introductie.

    Wie hier zonder sessie binnenkwam werd naar ``/dashboard`` gestuurd, en die vraagt SSO,
    dus eindigde elke bezoeker zonder rechten op het inlogscherm - zonder ooit gelezen te
    hebben WAT dit is. Sinds de architectuurpagina weg is, was dat de enige uitkomst.

    Doorverwijzen en niet hier renderen, zodat de introductie een eigen adres houdt dat je
    kunt delen en dat ook werkt voor iemand die al ingelogd is.
    """
    if get_current_user(request) is None:
        return RedirectResponse(url="/introductie", status_code=302)
    return RedirectResponse(url="/dashboard", status_code=302)


@web_router.get("/introductie", response_class=HTMLResponse)
async def introductie(request: Request):
    """Wat ZAD is, voor wie hier voor het eerst komt.

    BEWUST ZONDER ``@requires_sso``. Dit is de pagina voor iemand die nog geen rechten
    heeft - de enige groep die hem echt nodig heeft - dus een inlogmuur ervoor maakt hem
    precies voor zijn eigen publiek onbereikbaar. Publiek zijn is hier de hele opzet en
    geen omissie; ``tests/test_introductiepagina.py`` bewaakt dat de decorator er niet
    alsnog opkomt.

    De schil verdraagt ``user=None``: ``get_navigation(None, ...)`` levert de basisitems
    plus "Inloggen" rechtsboven in plaats van account en uitloggen.
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


@web_router.get("/permission-denied", response_class=HTMLResponse)
async def permission_denied(request: Request) -> HTMLResponse:
    """
    Show the permission denied page for users who are authenticated but not authorized.

    This page is shown to users who have successfully authenticated via SSO
    but whose email address is not in the allowed users list.

    Args:
        request: The FastAPI request object

    Returns:
        HTML response with the permission denied page
    """
    # Get user from session if available
    user = request.session.get("user") if hasattr(request, "session") else None

    # One-shot denial reason set by the login callback (server-side flash, not a URL param).
    denied_reason = request.session.pop("denied_reason", None) if hasattr(request, "session") else None

    # Log the permission denied access
    user_email = user.get("email", "unknown") if user else "anonymous"
    logger.warning(f"Permission denied page accessed by: {user_email}")

    from opi.web.navigation_lotc import get_navigation

    return render(
        request,
        template="bg/permission-denied.html.j2",
        context={
            "request": request,
            "user": user,
            "reason": denied_reason,
            "menu_items": get_menu_items(user),  # Same menu as other pages
            "navigation": get_navigation(user, current_path="/permission-denied"),
        },
    )


# Redirect old self-service portal URL to the wizard
@web_router.get("/projects/new")
async def redirect_projects_new_to_wizard():
    """Redirect legacy /projects/new to the wizard-based project creation flow."""
    return RedirectResponse(url="/forms/wizard/create-project", status_code=302)


# SSO-protected subdomain availability check (prevents unauthenticated enumeration)
web_router.add_api_route("/subdomains/check", check_subdomain_availability_web, methods=["GET"])


def _progress_page_context(task: dict, task_id: str) -> dict:
    """The context the progress page and its polled fragment share.

    Both render the same fragment, so both build it here: one weergave, one plek waar
    hij wordt gevuld. The page adds the shell around it, nothing more.
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
        # Whether it finished or failed, the detail page is where the user goes next --
        # to use the project, or to fix what went wrong.
        context["on_complete"] = f"window.location.href='/projects/{project_name}/details'"
        context["on_complete_label"] = "Naar projectdetails"
    return context


@web_router.get("/projects/progress/{task_id}", response_class=HTMLResponse)
@requires_sso
async def project_progress_page(request: Request, task_id: str):
    """
    Show the project creation progress page.

    The page is a shell around the shared progress fragment: the first paint is
    server-rendered here, and htmx polls the fragment route below for the rest.
    Reads task state from the V2 async task service (database-backed).
    """
    try:
        from opi.core.task_helpers import get_task_service

        task_service = get_task_service(request)
        task = await task_service.get_task(task_id)
        user = get_current_user(request)

        from opi.web.navigation_lotc import get_navigation

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
        logger.error(f"Error serving progress page: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error loading progress page: {e!s}")


@web_router.get("/projects/progress/{task_id}/fragment", response_class=HTMLResponse)
@requires_sso
async def project_progress_page_fragment(request: Request, task_id: str) -> HTMLResponse:
    """The progress fragment as the full page polls it.

    Same fragment as the modals use, only with the page's own poll URL and finish
    button. The page has no project name in its path, so the task supplies it and the
    same access rule applies as on the project-scoped fragment route.
    """
    from opi.core.task_helpers import get_task_service

    task_service = get_task_service(request)
    task = await task_service.get_task(task_id)
    if task is None:
        return HTMLResponse(content="<p>Taak niet gevonden</p>", status_code=404)

    _require_task_access(request, task, task.get("project_name", ""))

    # Rendered once on purpose -- see render_progress_fragment for why a second pass
    # over the rendered HTML would execute task text as Jinja.
    #
    # Het eigen fragment van de voortgangsPAGINA, en niet het gedeelde uit
    # task_progress.py: de pagina zet bg/_task-progress.html.j2 neer en dit is de
    # pollroute die datzelfde blok vervangt.
    context = _progress_page_context(task, task_id)
    return HTMLResponse(content=render_fragment(request, template="bg/_task-progress.html.j2", context=context))


@web_router.post("/projects/delete/{project_name}", response_class=HTMLResponse)
@requires_sso
async def delete_project_web(request: Request, project_name: str) -> HTMLResponse:
    """Delete a whole project from the UI, as a task you can follow.

    Deleting tears down git, ArgoCD, the namespace, databases and buckets. Inline that
    left the page on an open POST for minutes with nothing to show, and the dialog was
    dismissable while it ran. It answers with the same shared progress fragment as
    reprocessing, so there is one way an action reports back.
    """
    user = get_current_user(request)
    user_email = user.get("email", "").lower()

    logger.info(f"Web project deletion request for '{project_name}' by user: {user_email}")

    if not is_user_authorized_for_project(project_name, user_email):
        raise HTTPException(status_code=403, detail="Geen toegang tot dit project")

    user_role = get_user_role_for_project(project_name, user_email)
    if user_role not in ["admin", "owner"]:
        raise HTTPException(
            status_code=403,
            detail=f"Alleen admin of owner rollen kunnen projecten verwijderen. Je rol: {user_role}",
        )

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
@requires_sso
async def delete_deployment_web(request: Request, project_name: str, deployment_name: str) -> HTMLResponse:
    """Delete one deployment from the UI, as a task you can follow."""
    user = get_current_user(request)
    user_email = user.get("email", "").lower()

    logger.info(f"Web deployment deletion request for '{deployment_name}' in '{project_name}' by user: {user_email}")

    if not is_user_authorized_for_project(project_name, user_email):
        raise HTTPException(status_code=403, detail="Geen toegang tot dit project")

    user_role = get_user_role_for_project(project_name, user_email)
    if user_role not in ["admin", "owner"]:
        raise HTTPException(
            status_code=403,
            detail=f"Alleen admin of owner rollen kunnen deployments verwijderen. Je rol: {user_role}",
        )

    project = get_project_store().get(project_name)
    if not project:
        raise HTTPException(status_code=404, detail="Project niet gevonden")
    _require_deployment(project.data or {}, project_name, deployment_name)

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
@requires_sso
async def delete_component_web(request: Request, project_name: str, component_name: str) -> HTMLResponse:
    """Delete a component from the UI, as a task you can follow.

    Removing the component from the project file is the quick half; the reprocessing
    that applies it is not, and it always ran as a task already. Both now live in one
    task, so the dialog follows the whole thing instead of reporting success halfway.
    """
    user = get_current_user(request)
    user_email = user.get("email", "").lower()

    logger.info(f"Web component deletion request for '{component_name}' in '{project_name}' by user: {user_email}")

    if not is_user_authorized_for_project(project_name, user_email):
        raise HTTPException(status_code=403, detail="Geen toegang tot dit project")

    user_role = get_user_role_for_project(project_name, user_email)
    if user_role not in ["admin", "owner"]:
        raise HTTPException(
            status_code=403,
            detail=f"Alleen admin of owner rollen kunnen components verwijderen. Je rol: {user_role}",
        )

    project = get_project_store().get(project_name)
    if not project:
        raise HTTPException(status_code=404, detail="Project niet gevonden")
    _require_component(project.data or {}, project_name, component_name)

    return await create_task_and_render_progress(
        request=request,
        project_name=project_name,
        task_type="delete_component",
        # The confirmation dialog names every deployment and component that references this
        # one and says those references go with it, so the user who clicked through it has
        # confirmed exactly what confirm_in_use states. Without it the portal could only
        # delete components nothing uses, which is nearly none of them.
        payload={"project_name": project_name, "component_name": component_name, "confirm_in_use": True},
        current_step=f"Component '{component_name}' verwijderen gestart...",
        success_message=f"Component '{component_name}' succesvol verwijderd",
    )


@web_router.post("/projects/{project_name}/refresh", response_class=HTMLResponse)
@requires_sso
async def refresh_project_web(request: Request, project_name: str) -> HTMLResponse:
    """Reprocess a project from Git via web interface."""

    user = get_current_user(request)
    user_email = user.get("email", "").lower()

    logger.info(f"Web project refresh request for '{project_name}' by user: {user_email}")

    if not is_user_authorized_for_project(project_name, user_email):
        raise HTTPException(status_code=403, detail="Geen toegang tot dit project")

    user_role = get_user_role_for_project(project_name, user_email)
    if user_role not in ["admin", "owner"]:
        raise HTTPException(
            status_code=403,
            detail=f"Alleen admin of owner rollen kunnen een project herverwerken. Je rol: {user_role}",
        )

    project = get_project_store().get(project_name)
    if not project:
        raise HTTPException(status_code=404, detail="Project niet gevonden")

    return await create_task_and_render_progress(
        request=request,
        project_name=project_name,
        task_type="refresh_project",
        payload={"project_name": project_name, "force_clone": False},
        current_step="Project herverwerken gestart...",
        success_message="Project succesvol herverwerkt!",
    )


@web_router.post("/projects/{project_name}/refresh/{deployment_name}", response_class=HTMLResponse)
@requires_sso
async def refresh_deployment_web(request: Request, project_name: str, deployment_name: str) -> HTMLResponse:
    """Reprocess a single deployment from Git via web interface."""

    user = get_current_user(request)
    user_email = user.get("email", "").lower()

    logger.info(f"Web deployment refresh request for '{project_name}/{deployment_name}' by user: {user_email}")

    if not is_user_authorized_for_project(project_name, user_email):
        raise HTTPException(status_code=403, detail="Geen toegang tot dit project")

    user_role = get_user_role_for_project(project_name, user_email)
    if user_role not in ["admin", "owner"]:
        raise HTTPException(
            status_code=403,
            detail=f"Alleen admin of owner rollen kunnen een deployment herverwerken. Je rol: {user_role}",
        )

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
        },
        deployment_name=deployment_name,
        current_step=f"Deployment '{deployment_name}' herverwerken gestart...",
        success_message=f"Deployment '{deployment_name}' succesvol herverwerkt!",
    )


def _require_deployment(project_data: dict, project_name: str, deployment_name: str) -> None:
    """404 straight away when the deployment does not exist.

    The inline version raised DeploymentNotFound from flow.sleep, so the caller heard it
    immediately. Now that the work runs as a task, an unknown name would otherwise only
    surface as a task that fails a moment later -- further from the click and harder to
    read. It also keeps a name the project does not have out of the task, and so out of
    everything that later renders the task's text.
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
@requires_sso
async def wake_deployment_web(request: Request, project_name: str, deployment_name: str) -> HTMLResponse:
    """Wake a sleeping deployment from the UI (session + CSRF + role auth).

    The counterpart of the API wake endpoint: same one implementation in
    ``sleep_mode.flow.wake``, but authenticated by the session (no wake token) and
    gated to admin/owner, like the other deployment actions.
    """
    user = get_current_user(request)
    user_email = user.get("email", "").lower()

    if not is_user_authorized_for_project(project_name, user_email):
        raise HTTPException(status_code=403, detail="Geen toegang tot dit project")

    user_role = get_user_role_for_project(project_name, user_email)
    if user_role not in ["admin", "owner"]:
        raise HTTPException(
            status_code=403,
            detail=f"Alleen admin of owner rollen kunnen een deployment wekken. Je rol: {user_role}",
        )

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
@requires_sso
async def sleep_deployment_web(request: Request, project_name: str, deployment_name: str) -> HTMLResponse:
    """Manually put a deployment to sleep from the UI (session + CSRF + role auth).

    The other half of the wake toggle: same one implementation in ``sleep_mode.flow.sleep``,
    gated to admin/owner exactly like the wake action.
    """
    user = get_current_user(request)
    user_email = user.get("email", "").lower()

    if not is_user_authorized_for_project(project_name, user_email):
        raise HTTPException(status_code=403, detail="Geen toegang tot dit project")

    user_role = get_user_role_for_project(project_name, user_email)
    if user_role not in ["admin", "owner"]:
        raise HTTPException(
            status_code=403,
            detail=f"Alleen admin of owner rollen kunnen een deployment slapen. Je rol: {user_role}",
        )

    project = get_project_store().get(project_name)
    if not project:
        raise HTTPException(status_code=404, detail="Project niet gevonden")
    _require_deployment(project.data or {}, project_name, deployment_name)

    logger.info(f"Web sleep for '{project_name}/{deployment_name}' by {user_email}")
    # An async task, not an inline call: sleeping commits to git and then reprocesses,
    # ArgoCD sync included, so doing it in the request left the page on an open POST for
    # tens of seconds with nothing to show. Same progress fragment as reprocessing.
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
@requires_sso
async def deployment_action_confirm(
    request: Request, project_name: str, deployment_name: str, action_key: str
) -> HTMLResponse:
    """The confirmation body for a service-contributed deployment action.

    Loaded into the shared modal shell, so every ``DeploymentAction`` with a
    ``confirm_message`` gets a real dialog instead of ``window.confirm()`` -- generic, so
    a service needs no code here. The action is re-derived from the project's own
    services and matched on its key, so the POST target this renders is always one a
    service really offered for this deployment; an endpoint taken from the URL would be
    an open POST target.
    """
    user = get_current_user(request)
    user_email = user.get("email", "").lower()

    if not is_user_authorized_for_project(project_name, user_email):
        raise HTTPException(status_code=403, detail="Geen toegang tot dit project")

    user_role = get_user_role_for_project(project_name, user_email)
    if user_role not in ["admin", "owner"]:
        raise HTTPException(
            status_code=403,
            detail=f"Alleen admin of owner rollen kunnen deployment acties uitvoeren. Je rol: {user_role}",
        )

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
            # A service may leave the message out; still ask, rather than firing a
            # POST straight from the page.
            "message": action.confirm_message or f"Weet je zeker dat je '{action.label}' wilt uitvoeren?",
        },
    )


@web_router.get("/projects/{project_name}/actions/{action_key}/confirm", response_class=HTMLResponse)
@requires_sso
async def project_action_confirm(
    request: Request, project_name: str, action_key: str, target: str | None = None
) -> HTMLResponse:
    """The confirmation body for a dangerous project action (delete, reprocess).

    The second, equally narrow entrance next to ``deployment_action_confirm``: deleting
    does not come from a service, but it keeps the same property. The page names the
    action by key and, where it applies, which deployment/component/attachment it is
    about; the POST target is built here from the project's own data. An endpoint taken
    from the request would be an open POST target, and one of these deletes a project.
    """
    user = get_current_user(request)
    user_email = user.get("email", "").lower()

    if not is_user_authorized_for_project(project_name, user_email):
        raise HTTPException(status_code=403, detail="Geen toegang tot dit project")

    user_role = get_user_role_for_project(project_name, user_email)
    if user_role not in ["admin", "owner"]:
        raise HTTPException(
            status_code=403,
            detail=f"Alleen admin of owner rollen kunnen deze actie uitvoeren. Je rol: {user_role}",
        )

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


@web_router.get("/test-hero", response_class=HTMLResponse)
@requires_sso
async def test_hero(request: Request):
    """Test route for hero component."""
    try:
        from opi.web.navigation_lotc import get_navigation

        user = get_current_user(request)
        return templates_lotc.TemplateResponse(
            request, "test-hero.html.j2", {"request": request, "navigation": get_navigation(user, current_path="")}
        )
    except Exception as e:
        logger.error(f"Error serving test hero: {e!s}")
        raise HTTPException(status_code=500, detail=f"Template error: {e!s}")


@web_router.get("/forms/formulier", response_class=HTMLResponse)
@requires_sso
async def formulier_demo_form(request: Request):
    """
    Serve the RVO Formulier demo form showcasing all form field components.

    This form demonstrates comprehensive form field usage with proper RVO styling,
    validation states, and accessibility features based on the RVO documentation.

    Returns:
        HTML response with the formulier demo form
    """
    try:
        return templates_lotc.TemplateResponse(
            request, "formulier-template.html.j2", {"request": request, "title": "Formulier Template"}
        )
    except Exception as e:
        import traceback

        error_details = traceback.format_exc()
        logger.error(f"Error serving Formulier demo form: {e!s}\n{error_details}")

        # Try to extract line number from Jinja2 error
        error_msg = str(e)
        if hasattr(e, "lineno"):
            error_msg = f"Line {e.lineno}: {error_msg}"

        # Include template source snippet if available
        if hasattr(e, "source") and hasattr(e, "lineno"):
            lines = e.source.splitlines()
            line_num = e.lineno - 1
            if 0 <= line_num < len(lines):
                error_msg += f"\nSource: {lines[line_num].strip()}"

        raise HTTPException(status_code=500, detail=f"Template error: {error_msg}")


def _deployment_dashboard_status(status_data: dict[str, Any] | None) -> str:
    """The health bucket a deployment contributes to its project's dashboard tile.

    A terminal ArgoCD condition (ComparisonError etc.) means the manifests cannot be
    rendered or compared; health may still read ``Healthy`` from the last good sync, so a
    tile keyed on health alone would be misleadingly green. Treat that as ``Degraded`` so a
    broken render is visible at the top level.
    """
    from opi.manager.argo_manager import terminal_condition_message

    if not status_data:
        return "Unknown"
    if terminal_condition_message(status_data):
        return "Degraded"
    return status_data.get("status", {}).get("health", {}).get("status", "Unknown")


def _deployment_inactivity(project_data: dict[str, Any], deployment_name: str) -> str | None:
    """Why this deployment deliberately runs nothing, or None (RC-31).

    Read from the project file, never from the cluster: zero replicas there can also mean
    something went wrong, and the whole point is telling those two apart.

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

    # "Alle" mag alleen staan als het er ook echt alle zijn. Hier stond
    # `if not disabled and not inactive`, en die tak sloeg dus ook aan met projecten in een
    # VIERDE toestand: Unknown, wat een project is dat geen enkele deployment met een status
    # heeft. Gevolg: bij twee gezonde en een leeg project stond er "Alle 2 projecten zijn
    # gezond" terwijl het er drie waren. De voorwaarde toetst nu het totaal, dus elke
    # toestand die er later bij komt valt vanzelf in de eerlijke tak.
    # Een SLAPENDE deployment is gezond: hij doet precies wat er van hem gevraagd is en
    # komt vanzelf terug op het eerste bezoek. Hij hoorde bij de niet-gezonde, en dan stond
    # er "Geen van je 2 projecten is gezond" terwijl er niets mis was. Uitgeschakeld is wel
    # iets anders: dat blijft uit tot iemand het aanzet, en dat is een keuze van een mens
    # die het overzicht mag noemen. Slapend telt dus mee in de kop EN krijgt zijn eigen
    # regel, want je wilt wel weten dat het zo is.
    if healthy + inactive == total:
        gezond = healthy + inactive
        heading = "Het project is gezond" if gezond == 1 else f"Alle {gezond} projecten zijn gezond"
        return {"kind": "success", "heading": heading, "lines": []}

    # Elke toestand krijgt zijn eigen regel, in eigen woorden: uitgeschakeld blijft uit tot
    # iemand het aanzet, slapend komt vanzelf terug, en onbekend betekent dat er niets
    # draait om iets over te zeggen. Een gedeelde grijze regel voor alle drie zou de lezer
    # niet vertellen of hij iets moet doen.
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
    # Vier gevallen, want "0 van de 1 projecten zijn gezond" is op drie manieren fout:
    # het telwoord voor een enkelvoud, het meervoud "projecten", en het werkwoord.
    gezond = healthy + inactive
    if total == 1:
        heading = "Het project is gezond" if gezond else "Het project is niet gezond"
    elif not gezond:
        heading = f"Geen van je {total} projecten is gezond"
    elif gezond == 1:
        heading = f"1 van de {total} projecten is gezond"
    else:
        heading = f"{gezond} van de {total} projecten zijn gezond"
    return {
        "kind": "info",
        "heading": heading,
        "lines": lines,
    }


async def _sum_by_namespace(prom: Any, promql: str) -> dict[str, float]:
    """Lees een query met ``by (namespace)`` uit als namespace -> waarde.

    Zo levert EEN query de cijfers van alle projecten tegelijk, in plaats van een query per
    project. Een mislukte query is geen fout maar een lege uitslag: het dashboard toont dan
    nul, zoals het bij de andere Prometheus-queries in dit bestand ook doet.
    """
    try:
        result = await prom.custom_query(promql)
    except Exception as e:
        logger.debug(f"Dashboard per-namespace query failed: {e}")
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
    """Haal het resourcegebruik voor het dashboard op bij Prometheus.

    Uit de dashboardroute getrokken zodat twee plekken hem kunnen gebruiken: die route
    zelf (voor de bestaande pagina) en het fragment dat de nieuwe pagina apart inlaadt.
    Verdubbelen zou betekenen dat een verbetering aan de ene kant stilletjes niet aan de
    andere kant landt.

    Zet ook per project ``cpu_cores``, ``cpu_limit_cores``, ``memory_mb`` en
    ``memory_limit_mb`` in ``user_projects``, want de kaart "Gebruik per project" rekent
    daarmee.

    Returns:
        De metrics, of Prometheus bereikbaar was, en het aantal pods.
    """
    metrics: dict = {}
    prometheus_available = False
    pod_count = 0
    ns_regex = "|".join(all_namespaces)

    if all_namespaces:
        try:
            from opi.connectors.prometheus import get_metrics_connector

            prom = await get_metrics_connector()
            prometheus_available = prom.is_connected

            if prometheus_available:
                # CPU usage and limits
                cpu_usage_val = 0.0
                cpu_limit_val = 0.0
                try:
                    result = await prom.custom_query(
                        f'sum(rate(container_cpu_usage_seconds_total{{namespace=~"{ns_regex}",container!=""}}[5m]))'
                    )
                    if result and result[0].get("value"):
                        cpu_usage_val = float(result[0]["value"][1])
                except Exception as e:
                    logger.debug(f"Dashboard CPU usage query failed: {e}")

                try:
                    result = await prom.custom_query(
                        f'sum(kube_pod_container_resource_limits{{namespace=~"{ns_regex}",resource="cpu"}})'
                    )
                    if result and result[0].get("value"):
                        cpu_limit_val = float(result[0]["value"][1])
                except Exception as e:
                    logger.debug(f"Dashboard CPU limits query failed: {e}")

                # Memory usage and limits
                mem_usage_val = 0.0
                mem_limit_val = 0.0
                try:
                    result = await prom.custom_query(
                        f'sum(container_memory_working_set_bytes{{namespace=~"{ns_regex}",container!=""}})'
                    )
                    if result and result[0].get("value"):
                        mem_usage_val = float(result[0]["value"][1])
                except Exception as e:
                    logger.debug(f"Dashboard memory usage query failed: {e}")

                try:
                    result = await prom.custom_query(
                        f'sum(kube_pod_container_resource_limits{{namespace=~"{ns_regex}",resource="memory"}})'
                    )
                    if result and result[0].get("value"):
                        mem_limit_val = float(result[0]["value"][1])
                except Exception as e:
                    logger.debug(f"Dashboard memory limits query failed: {e}")

                # Storage usage and capacity
                storage_used_val = 0.0
                storage_cap_val = 0.0
                try:
                    result = await prom.custom_query(f'sum(kubelet_volume_stats_used_bytes{{namespace=~"{ns_regex}"}})')
                    if result and result[0].get("value"):
                        storage_used_val = float(result[0]["value"][1])
                except Exception as e:
                    logger.debug(f"Dashboard storage usage query failed: {e}")

                try:
                    result = await prom.custom_query(
                        f'sum(kubelet_volume_stats_capacity_bytes{{namespace=~"{ns_regex}"}})'
                    )
                    if result and result[0].get("value"):
                        storage_cap_val = float(result[0]["value"][1])
                except Exception as e:
                    logger.debug(f"Dashboard storage capacity query failed: {e}")

                # Pod count
                try:
                    result = await prom.custom_query(f'count(kube_pod_info{{namespace=~"{ns_regex}"}})')
                    if result and result[0].get("value"):
                        pod_count = int(float(result[0]["value"][1]))
                except Exception as e:
                    logger.debug(f"Dashboard pod count query failed: {e}")

                # Network traffic time-series (last 30min, 5min step)
                network_in_data: list[dict] = []
                network_out_data: list[dict] = []
                try:
                    now = datetime.now(UTC)
                    start = now.timestamp() - 1800  # 30 minutes ago
                    end = now.timestamp()
                    in_results = await prom.query_range(
                        f'sum(rate(container_network_receive_bytes_total{{namespace=~"{ns_regex}"}}[5m]))',
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
                        f'sum(rate(container_network_transmit_bytes_total{{namespace=~"{ns_regex}"}}[5m]))',
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
                    logger.debug(f"Dashboard network query failed: {e}")

                # Compute display values
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
                    "memory_limit_display": _format_gib(mem_limit_val),
                    "storage_percentage": _pct(storage_used_val, storage_cap_val),
                    "storage_usage_display": _format_gib(storage_used_val),
                    "storage_capacity_display": _format_gib(storage_cap_val),
                    "network_in_data": network_in_data,
                    "network_out_data": network_out_data,
                }
                # Per project het gebruik EN de limiet, voor de kaart "Gebruik per project".
                #
                # Geheugen stond hier niet, en dat is waarom die kaart alleen CPU toonde: er
                # kwam nooit een waarde binnen. Van de twee is geheugen de belangrijkste --
                # daar valt een pod op om als het opraakt -- en op een rustig cluster is het
                # CPU-cijfer bijna nul, waardoor de kaart in de praktijk leeg was.
                #
                # Gebruik is de working set en niet de limiet: dat is wat er werkelijk in
                # gebruik is. De LIMIET komt er apart bij, want de kaart heeft de vorm van
                # "Resourcegebruik (heel project)" op de projectpagina: gebruikt / limiet met
                # een percentage, en dan een balk. Zonder limiet is er geen bovengrens om de
                # balk tegen af te zetten. Dezelfde vier queries als die kaart, zodat een
                # project op beide plekken hetzelfde cijfer laat zien.
                #
                # ``by (namespace)`` en niet vier queries PER project: zo kost dit vier
                # queries ongeacht het aantal projecten, in plaats van vier keer het aantal
                # projecten op een fragment dat al apart geladen wordt. Het was er eerst een
                # per project, dus dit is ook voor CPU minder werk dan voorheen -- zodra er
                # meer dan vier projecten zijn is de hele kaart netto goedkoper.
                cpu_by_ns = await _sum_by_namespace(
                    prom,
                    f'sum by (namespace) (rate(container_cpu_usage_seconds_total{{namespace=~"{ns_regex}",container!=""}}[5m]))',
                )
                cpu_limit_by_ns = await _sum_by_namespace(
                    prom,
                    f'sum by (namespace) (kube_pod_container_resource_limits{{namespace=~"{ns_regex}",resource="cpu"}})',
                )
                mem_by_ns = await _sum_by_namespace(
                    prom,
                    f'sum by (namespace) (container_memory_working_set_bytes{{namespace=~"{ns_regex}",container!=""}})',
                )
                mem_limit_by_ns = await _sum_by_namespace(
                    prom,
                    f'sum by (namespace) (kube_pod_container_resource_limits{{namespace=~"{ns_regex}",resource="memory"}})',
                )

                for project in user_projects:
                    proj_ns = project.get("namespaces", [])
                    # Een project zonder namespaces telt op tot nul, net als voorheen.
                    project["cpu_cores"] = sum(cpu_by_ns.get(ns, 0.0) for ns in proj_ns)
                    project["cpu_limit_cores"] = sum(cpu_limit_by_ns.get(ns, 0.0) for ns in proj_ns)
                    project["memory_mb"] = sum(mem_by_ns.get(ns, 0.0) for ns in proj_ns) / (1024 * 1024)
                    project["memory_limit_mb"] = sum(mem_limit_by_ns.get(ns, 0.0) for ns in proj_ns) / (1024 * 1024)

        except Exception as e:
            logger.warning(f"Dashboard: failed to fetch Prometheus metrics: {e}")

    return metrics, prometheus_available, pod_count


@web_router.get("/dashboard", response_class=HTMLResponse)
@requires_sso
async def dashboard(request: Request):
    """
    Serve the main dashboard page with real project data, Prometheus metrics,
    and ArgoCD status.

    Returns:
        HTML response with the dashboard showing project overview, metrics, and activity
    """
    try:
        user = get_current_user(request)
        user_email = user.get("email", "").lower()

        # --- Load user's projects ---
        all_projects = get_project_store().get_all()

        user_projects: list[dict] = []
        all_namespaces: list[str] = []
        total_deployments = 0
        unique_users: set[str] = set()

        for project in all_projects:
            project_name = project.name
            if not is_user_authorized_for_project(project_name, user_email):
                continue
            project_data = project.data or {}
            deployments = project_data.get("deployments", [])
            users = project.users or []
            total_deployments += len(deployments)
            for u in users:
                if hasattr(u, "email") and u.email:
                    unique_users.add(u.email.lower())

            # Collect k8s namespaces for Prometheus queries
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
                    # Needed to read whether a deployment deliberately runs nothing (RC-31);
                    # that intent lives in the project file, not in the cluster.
                    "project_data": project_data,
                    "users": users,
                    "deployment_count": len(deployments),
                    "user_count": len(users),
                    "namespaces": project_namespaces,
                }
            )

        user_projects.sort(key=lambda p: p["display_name"] or p["name"])

        # --- Query Prometheus metrics (scoped to user's namespaces) ---
        #
        # Zes queries achter elkaar, plus een per project. Dat is wat het dashboard traag
        # maakt, en het is precies waarom de RVO-pagina hier lazy loading voor had. De
        # De pagina haalt dit blok apart op via /dashboard/resource-usage, zodat de pagina
        # er meteen staat en een trage of afwezige Prometheus hem niet ophoudt. Hier
        # blijven ze dus leeg; het sjabloon leest ze wel.

        metrics: dict = {}
        prometheus_available = False
        pod_count = 0

        total_cpu_usage = sum(p.get("cpu_cores", 0) for p in user_projects)
        # Geheugen erbij: dat is waar je op stuurt, en CPU alleen zei te weinig.
        total_memory_usage = sum(p.get("memory_mb", 0) or 0 for p in user_projects)

        # --- Query ArgoCD status per project ---
        argocd_available = False
        try:
            from opi.connectors.argo import create_argo_connector
            from opi.utils.naming import generate_argocd_application_name

            argo_connector = create_argo_connector()
            argocd_available = argo_connector.auth_token is not None

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
                                # verdict the intent replaces (RC-31): Degraded and
                                # Progressing are things ArgoCD really observed and stand.
                                argo_status = _deployment_dashboard_status(status_data)
                                inactivity = _deployment_inactivity(project["project_data"], deployment_name)
                                if inactivity and argo_status == "Healthy":
                                    argo_status = inactivity
                                deployment_statuses.append(argo_status)
                                # Extract last deployed timestamp
                                operation_state = status_data.get("status", {}).get("operationState", {})
                                finished_at = operation_state.get("finishedAt") or status_data.get("status", {}).get(
                                    "reconciledAt"
                                )
                                if finished_at and (not latest_deploy or finished_at > latest_deploy):
                                    latest_deploy = finished_at
                            else:
                                deployment_statuses.append("Unknown")
                        except Exception as e:
                            logger.debug(f"Dashboard: ArgoCD status for {deployment_name}: {e}")
                            deployment_statuses.append("Unknown")

                    # Derive overall project health (worst status wins)
                    project["health"] = _derive_project_health(deployment_statuses)
                    project["last_deployed"] = latest_deploy
        except Exception as e:
            logger.warning(f"Dashboard: failed to connect to ArgoCD: {e}")
            for project in user_projects:
                project["health"] = "Unknown"

        # Compute health counts for summary banner
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
        import traceback

        error_details = traceback.format_exc()
        logger.error(f"Error serving dashboard: {e!s}\n{error_details}")

        # Try to extract line number from Jinja2 error
        error_msg = str(e)
        if hasattr(e, "lineno"):
            error_msg = f"Line {e.lineno}: {error_msg}"

        # Include template source snippet if available
        if hasattr(e, "source") and hasattr(e, "lineno"):
            lines = e.source.splitlines()
            line_num = e.lineno - 1
            if 0 <= line_num < len(lines):
                error_msg += f"\nSource: {lines[line_num].strip()}"

        raise HTTPException(status_code=500, detail=f"Template error: {error_msg}")


@web_router.get("/projects/{project_name}/details", response_class=HTMLResponse)
@web_router.get("/projects/{project_name}/team", response_class=HTMLResponse)
@web_router.get("/projects/{project_name}/componenten", response_class=HTMLResponse)
@web_router.get("/projects/{project_name}/services", response_class=HTMLResponse)
@web_router.get("/projects/{project_name}/services-info", response_class=HTMLResponse)
@web_router.get("/projects/{project_name}/deployments", response_class=HTMLResponse)
@web_router.get("/projects/{project_name}/metrics", response_class=HTMLResponse)
@web_router.get("/projects/{project_name}/backups", response_class=HTMLResponse)
@web_router.get("/projects/{project_name}/taken", response_class=HTMLResponse)
@requires_sso
async def project_details(request: Request, project_name: str):
    """De projectpagina zonder deployment in het pad; zie :func:`render_project_page`."""
    return await render_project_page(request, project_name, "")


@web_router.get("/projects/{project_name}/deployments/{deployment_name}", response_class=HTMLResponse)
@web_router.get("/projects/{project_name}/metrics/{deployment_name}", response_class=HTMLResponse)
@web_router.get("/projects/{project_name}/backups/{deployment_name}", response_class=HTMLResponse)
@requires_sso
async def project_deployment_details(request: Request, project_name: str, deployment_name: str):
    """EEN deployment op de tabbladen Deployments, Metrics en Backups.

    Deployments en Metrics toonden alle deployments en verborgen er alles behalve een met
    CSS. Nu staat de naam in het PAD en rendert de server er een: dat scheelt het werk voor
    blokken die niemand ziet, de pagina is deelbaar, de terugknop werkt, en de keuze blijft
    staan bij het wisselen van tabblad omdat de tabbalk hem in zijn adressen meeneemt.

    Backups is er sinds RC-100 het derde: het backupsblok stond als dienstblok op
    Deployments en heeft nu een eigen tabblad, met dezelfde vorm - een deployment per
    pagina, zijn naam in het pad, dezelfde kiezer.

    De paden staan hier letterlijk en niet als ``/projects/{project}/{tab}/{deployment}``,
    om dezelfde reden als bij de tabbladen zelf: dat laatste vangt ook paden op die een
    andere route toekomen.
    """
    return await render_project_page(request, project_name, deployment_name)


#: De tabbladpaden van voor RC-93, met het tabblad VOOR de projectnaam. Ze staan hier
#: letterlijk en in dezelfde volgorde als hierboven, zodat de twee vormen naast elkaar te
#: lezen zijn.
#:
#: Elk tabblad hoort hier te staan zolang zijn oude vorm hieronder als route geregistreerd
#: is: die route zoekt zijn tabblad in deze tabel op, en een ontbrekende regel is dus geen
#: doorverwijzing maar een 500. Zo stond ``team`` er niet in terwijl de route wel bestond
#: (RC-101). tests/test_lotc_tabbladen_url.py loopt nu beide lijsten af.
#: ``backups`` staat hier NIET bij, en dat is geen omissie: dat tabblad bestaat pas sinds
#: RC-100, dus ``/projects/backups/<naam>`` heeft nooit bestaan en kan dus ook nooit
#: gedeeld zijn. Een doorverwijzing voor een adres dat niemand kan hebben is onderhoud
#: zonder lezer.
OUDE_TABBLADPADEN = {
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
async def project_tab_oude_vorm(request: Request, project_name: str, deployment_name: str = ""):
    """De oude tabbladadressen (``/projects/deployments/<naam>``) verwijzen door.

    Sinds RC-93 staat de projectnaam voorop: ``/projects/<naam>/deployments``. De oude vorm
    heeft in de sandbox gestaan en kan gedeeld zijn, en een gedeelde link hoort niet stil
    een 404 te worden - dus verwijst hij door in plaats van te verdwijnen.

    Hier zit GEEN autorisatie op, en dat kan omdat er niets wordt opgezocht: dit is een
    blinde herschrijving van het pad die niets prijsgeeft over het project (of het bestaat,
    of je erbij mag). Het antwoord is voor elke naam hetzelfde. De echte pagina achter het
    nieuwe adres doet de autorisatie zoals altijd.

    Ze staan NA de nieuwe vorm geregistreerd, want bij een project dat toevallig
    ``deployments`` of ``details`` heet zijn beide vormen te lezen; dan wint het adres van
    vandaag, niet dat van gisteren.
    """
    tab = OUDE_TABBLADPADEN[request.url.path.strip("/").split("/")[1]]
    return RedirectResponse(
        url=project_tab_url(project_name, tab, request.url.query, deployment=deployment_name),
        status_code=302,
    )


async def render_project_page(request: Request, project_name: str, deployment_name: str):
    """
    Serve the project details page showing comprehensive project information.
    Shows detailed project data including services, components, deployments, and configuration.

    Elk tabblad heeft een EIGEN PAD - ``/projects/<naam>/deployments`` en zo voor de
    andere - in plaats van ``?tab=deployments`` op een gedeeld adres. Een querystring
    leest als een filter, terwijl een tabblad een pagina is. De paden staan hierboven
    letterlijk en niet als ``/projects/{project_name}/{tab}``: dat laatste zou ook
    ``/projects/details/<naam>`` opvangen, met ``project_name="details"``, en dan hangt het
    van de volgorde van registreren af welke route wint.

    Args:
        request: The FastAPI request object
        project_name: The name of the project to display

    Returns:
        HTML response with detailed project information
    """
    # ``?tab=`` bestaat niet meer, ook niet als doorverwijzing. Er is bewust GEEN
    # overgangspad: de oude vorm heeft nooit buiten deze applicatie geleefd (de links
    # erheen staan in deze sjablonen en in de tests, en die wijzen nu naar de paden), en
    # een doorverwijzing die niemand gebruikt is een tweede adres dat onderhouden moet
    # worden. Een ?tab= in de URL wordt dus gewoon genegeerd; je krijgt Overzicht.
    try:
        from opi.services.services import ServiceAdapter

        user = get_current_user(request)

        # Generate CSRF token for form protection (domain settings modal)
        csrf_token = ensure_csrf_token(request)

        # TODO: this logic has to be centralized
        user_email = user.get("email", "").lower()

        # Ensure project data is fresh (refreshes from Git if stale)

        # Get project service to validate access

        # Check if user has access to this project
        if not is_user_authorized_for_project(project_name, user_email):
            logger.warning(f"User {user_email} not authorized to view project: {project_name}")
            raise HTTPException(status_code=403, detail="You are not authorized to view this project")

        # Get project details
        project = get_project_store().get(project_name)
        if not project:
            raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")

        # Get user's role for this project
        user_role = get_user_role_for_project(project_name, user_email)

        # Use project data from memory if available
        project_data = project.data or {}

        # WELKE deployment staat open, en staat die naam al in de URL? Deze twee
        # tabbladen tonen er een, dus het adres hoort te zeggen welke: zonder naam in het
        # pad (of met een naam die niet meer bestaat) kiest de server er een en verwijst
        # hij daarheen, zodat de URL daarna klopt en deelbaar is. ``?deployment=<naam>``
        # was de oude vorm en verwijst hier ook naartoe, zodat een gedeelde link uit die
        # tijd blijft werken.
        #
        # Hier, direct na het laden van het project: alles hieronder is ontsleutelwerk dat
        # voor een doorverwijzing niet gedaan hoeft te worden.
        deployment_open = ""
        if tab_from_path(request.url.path) in TABS_MET_DEPLOYMENT:
            deployment_open = kies_deployment(
                [deployment["name"] for deployment in project_data.get("deployments") or []],
                deployment_name,
                request.query_params.get("deployment") or "",
            )
            hoort_op = deployment_pagina_adres(request, project_name, deployment_open)
            huidig = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            if hoort_op != huidig:
                return RedirectResponse(url=hoort_op, status_code=302)

        settings_private_key = get_global_private_key()

        project_data_decrypted = copy.deepcopy(project_data)

        project_private_key = await decrypt_password_smart(
            project_data["config"]["age-private-key"], settings_private_key
        )

        project_data_decrypted["config"]["api-key"] = await decrypt_password_smart(
            project_data["config"]["api-key"], project_private_key
        )

        # Store decrypted private key for display (admins only see this in UI)
        project_data_decrypted["config"]["age-private-key"] = project_private_key

        # Decrypt Keycloak passwords (RC-5 B: connections live under the keycloak
        # service config now, relocated from the old project-level config.keycloak).
        for kc_config in Project(project_data_decrypted).get("services/keycloak/config/realms") or []:
            if kc_config.get("password"):
                try:
                    kc_config["password"] = await decrypt_password_smart(kc_config["password"], project_private_key)
                except Exception as e:
                    logger.warning(f"Failed to decrypt Keycloak password for realm {kc_config.get('realm')}: {e}")
                    kc_config["password"] = None
            # De SEED bereikt de pagina nooit: die zou voor altijd codes opleveren. Wat er
            # wel op komt is de CODE van dit moment, en die vergaat binnen een periode van
            # 30 seconden.
            #
            # Hij wordt HIER berekend en niet meer op verzoek achter een knop (RC-101): de
            # OTP is op het tabblad Toegang een veld zoals het wachtwoord ernaast, en een
            # veld heeft zijn waarde bij het renderen. Dat zet de code in de HTML, net als
            # het admin-wachtwoord dat er al stond - hetzelfde blok, dezelfde rolpoort
            # (alleen admin/owner), en van de twee is de code de kortstlevende.
            kc_config["has_totp"] = bool(kc_config.get("totp_secret"))
            if kc_config["has_totp"]:
                try:
                    seed = await decrypt_password_smart(kc_config["totp_secret"], project_private_key)
                    kc_config["totp_code"], _ = totp_now(seed)
                except Exception as e:
                    logger.warning(f"Failed to derive OTP code for realm {kc_config.get('realm')}: {e}")
                    kc_config["totp_code"] = None
            kc_config["totp_secret"] = None

        # The same reader the read-only API uses (RC-61): one decrypt-and-parse path, so
        # the page and the API can never disagree about what a component's variables are.
        for deployment in project_data_decrypted.get("deployments", []):
            for dep_component in deployment.get("components", []):
                if dep_component.get("user-env-vars"):
                    dep_component["user-env-vars"] = await read_user_env_vars(
                        dep_component["user-env-vars"],
                        project_private_key,
                        where=f"deployment component '{dep_component.get('reference')}'",
                    )

        logger.info(f"Processing {len(project_data_decrypted.get('components', []))} components for user-env-vars")
        for component in project_data_decrypted.get("components", []):
            component_name = component.get("name", "unknown")
            if component.get("user-env-vars"):
                component["user-env-vars"] = await read_user_env_vars(
                    component["user-env-vars"],
                    project_private_key,
                    where=f"component '{component_name}'",
                )
            # Aliassen ook, en om dezelfde reden: de kaart toont ze en zonder dit staat er
            # een AGE-blok op het scherm. Sinds RC-106 is dat hetzelfde blok als hierboven,
            # en het is dezelfde decoder als de leesendpoints gebruiken, zodat de pagina en
            # de API niet uiteen kunnen lopen over wat er staat. Wat er daarna nog
            # afgeschermd wordt bepaalt de dienst, in het sjabloon via het filter
            # is_verwijzing.
            if component.get("aliases"):
                try:
                    component["aliases"] = decode_component_values(
                        component["aliases"], project_data_decrypted, project_private_key
                    )
                except (ComponentValuesError, ValueError) as error:
                    # Een onleesbaar blok levert geen namen op om te tonen; laat het weg in
                    # plaats van er een AGE-blok van te maken op het scherm.
                    logger.warning(f"Aliases of component '{component_name}' could not be read: {error}")
                    component["aliases"] = {}

        # Decrypt helm-charts base helm-values
        for helm_chart in project_data_decrypted.get("helm-charts", []):
            chart_name = helm_chart.get("name", "unknown")
            if helm_chart.get("helm-values"):
                try:
                    decrypted_yaml = await decrypt_age_content(helm_chart["helm-values"], project_private_key)
                    helm_chart["helm-values"] = load_yaml_from_string(decrypted_yaml)
                    logger.info(f"Decrypted helm-values for helm-chart '{chart_name}'")
                except Exception as e:
                    logger.warning(f"Failed to decrypt helm-values for helm-chart '{chart_name}': {e}")
                    helm_chart["helm-values"] = None

        # Decrypt helmfile base helm-values
        for helmfile in project_data_decrypted.get("helmfile", []):
            helmfile_name = helmfile.get("name", "unknown")
            if helmfile.get("helm-values"):
                try:
                    decrypted_yaml = await decrypt_age_content(helmfile["helm-values"], project_private_key)
                    helmfile["helm-values"] = load_yaml_from_string(decrypted_yaml)
                    logger.info(f"Decrypted helm-values for helmfile '{helmfile_name}'")
                except Exception as e:
                    logger.warning(f"Failed to decrypt helm-values for helmfile '{helmfile_name}': {e}")
                    helmfile["helm-values"] = None

        # Decrypt deployment-level helm-charts and helmfile helm-values
        for deployment in project_data_decrypted.get("deployments", []):
            deployment_name = deployment.get("name", "unknown")

            # Decrypt deployment helm-charts helm-values
            for helm_chart in deployment.get("helm-charts", []):
                chart_ref = helm_chart.get("reference", "unknown")
                if helm_chart.get("helm-values"):
                    try:
                        decrypted_yaml = await decrypt_age_content(helm_chart["helm-values"], project_private_key)
                        helm_chart["helm-values"] = load_yaml_from_string(decrypted_yaml)
                        logger.info(
                            f"Decrypted helm-values for deployment '{deployment_name}' helm-chart '{chart_ref}'"
                        )
                    except Exception as e:
                        logger.warning(
                            f"Failed to decrypt helm-values for deployment '{deployment_name}' helm-chart '{chart_ref}': {e}"
                        )
                        helm_chart["helm-values"] = None

            # Decrypt deployment helmfile helm-values
            for helmfile in deployment.get("helmfile", []):
                helmfile_ref = helmfile.get("reference", "unknown")
                if helmfile.get("helm-values"):
                    try:
                        decrypted_yaml = await decrypt_age_content(helmfile["helm-values"], project_private_key)
                        helmfile["helm-values"] = load_yaml_from_string(decrypted_yaml)
                        logger.info(
                            f"Decrypted helm-values for deployment '{deployment_name}' helmfile '{helmfile_ref}'"
                        )
                    except Exception as e:
                        logger.warning(
                            f"Failed to decrypt helm-values for deployment '{deployment_name}' helmfile '{helmfile_ref}': {e}"
                        )
                        helmfile["helm-values"] = None

        # Process services to add display information
        services_with_info = []
        project_services = project_data.get("services", [])
        # Extract service names from project services (handles both string and dict formats)
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

        # Prepare project details for template
        from opi.handlers.project_file_handler import extract_attachment_catalog, extract_attachment_usage

        _attachment_usage = extract_attachment_usage(project_data)

        project_details = {
            "name": project_name,
            "display_name": project_data.get("display-name", project_name),
            "description": project_data.get("description", "Geen beschrijving beschikbaar"),
            "users": project.users or [],
            "user_role": user_role,
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

        # Check Prometheus availability (metrics are lazy-loaded via HTMX)
        prometheus_available = False
        try:
            from opi.connectors.prometheus import get_metrics_connector

            prom = await get_metrics_connector()
            prometheus_available = prom.is_connected
        except Exception as e:
            logger.debug(f"Prometheus not available: {e}")

        # ArgoCD status is loaded lazily per deployment via /argocd-status/{deployment} so it
        # does not block the page render; only the cheap availability check stays here.
        argocd_available = False
        try:
            from opi.connectors.argo import create_argo_connector

            argocd_available = create_argo_connector().auth_token is not None
        except Exception as argo_error:
            logger.warning(f"Failed to connect to ArgoCD: {argo_error}")

        # Backup snapshots are NOT fetched here: listing them opens a Kopia repository
        # over S3, which measured 2.1s to connect plus 0.4s to list on this page --
        # 70% of its total render time, for a block most visitors never look at. Each
        # deployment's block now loads itself via /backups/{deployment}, like the
        # ArgoCD blocks already did.
        from opi.core.config import settings
        from opi.manager.backup import BackupManager

        current_cluster = settings.CLUSTER_MANAGER
        # Ungranted approvals per deployment, asked of the catalog (each service writes
        # its own notice) so this page holds no domain knowledge.
        from opi.services.approvals import collect_deployment_approval_notices

        approval_notices: dict[str, list[dict[str, Any]]] = {}
        for deployment in project_details["deployments"]:
            notices = collect_deployment_approval_notices(project_data_decrypted, deployment)
            if notices:
                approval_notices[deployment.get("name", "")] = notices

        try:
            BackupManager()
            backups_available = True
        except Exception as backup_init_error:
            logger.warning(f"Failed to initialize backup manager: {backup_init_error}")
            backups_available = False

        # Generate ingress URLs for components with publish-on-web service
        from opi.handlers.project_file_handler import ProjectFileHandler
        from opi.services.catalog.publish_on_web.urls import public_urls_for_deployment

        project_file_handler = ProjectFileHandler()

        # Add ingress information to deployments
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

        # Get cluster base domains for domain settings modal
        from opi.web.router_self_service import get_cluster_base_domains_for_template

        cluster_base_domains = get_cluster_base_domains_for_template()

        from opi.forms.visualizers.flows import SERVICE_CONFIG_MODAL_FLOWS

        # Deployment-level action buttons contributed by the project's services
        # (e.g. sleep-mode "wake"), keyed by deployment name. Built from the decrypted
        # project data so it can read the OPI-managed sleep state.
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

        # What the services report about each deployment (RC-28): the same facts the
        # health check weighs, here only rendered. Without it a user sees a sleeping
        # deployment with nothing running and no explanation why.
        from opi.services.deployment_state import collect_deployment_state

        deployment_states = {
            dep.get("name"): collect_deployment_state(project_data_decrypted, dep.get("name", ""))
            for dep in project_data_decrypted.get("deployments", [])
        }

        # De statuskolom van de deploymenttabel op het tabblad Overzicht. Twee bronnen die
        # allebei per PROJECT worden opgehaald en niet per rij: de feiten van de diensten
        # (hierboven al berekend) en EEN gebundelde bevraging bij ArgoCD. Twintig rijen
        # leveren dus geen twintig ArgoCD-verzoeken op - zie
        # opi/services/argocd_overview.py voor de bevraging en de vervaltijd.
        #
        # Alleen voor het tabblad dat de tabel toont: de andere tabbladen hebben de kolom
        # niet, en dan hoeft ArgoCD er ook niet voor bevraagd te worden.
        argocd_statuses: dict[str, dict[str, Any]] = {}
        if argocd_available and tab_from_path(request.url.path) == STANDAARD_TAB:
            argocd_statuses = await get_project_argocd_statuses(
                project_name, [name for name in deployment_states if name]
            )
        deployment_status_tags = build_deployment_status_column(
            project_details["deployments"], deployment_states, argocd_statuses
        )

        # A viewer whose role could not be determined is not a member with a role; the
        # services gate on the role string, and an empty one matches no gate.
        role_for_services = user_role or ""

        def deployment_page_context(dep: dict[str, Any]) -> DeploymentPageContext:
            """Wat een dienst nodig heeft om zijn blok voor DEZE deployment te maken.

            De beschikbaarheid van de optionele achterkanten is hier al gemeten (een
            dienst belt zelf nooit een connector) en gaat mee naar binnen.
            """
            return DeploymentPageContext(
                project_data=project_data_decrypted,
                deployment=dep,
                user_role=role_for_services,
                current_cluster=current_cluster,
                backend_available={"prometheus": prometheus_available, "backups": backups_available},
            )

        # Per-deployment read-only blocks the services deliver (RC-24): a block that
        # describes one deployment is asked per deployment instead of being hardcoded in
        # the Deployments tab.
        deployment_service_sections = {
            dep.get("name"): collect_deployment_page_sections(deployment_page_context(dep))
            for dep in project_data_decrypted.get("deployments", [])
        }

        # Het backupsblok, voor de deployment die op het tabblad Backups openstaat (RC-100).
        #
        # Bij NAAM gevraagd en niet via het algemene dienstenmechanisme hierboven, want het
        # heeft een eigen tabblad gekregen en dat mechanisme levert alles op EEN tabblad af.
        # De afweging staat in opi/services/catalog/shared/backups.py; kort: van de twee
        # diensten die een deploymentblok leveren is dit de enige kandidaat voor een eigen
        # tabblad, dus is een haak voor "welke dienst wil een tabblad" machinerie voor een
        # geval dat niet bestaat.
        #
        # Alleen voor de OPEN deployment: de pagina toont er een, en voor de rest zou dit
        # werk zijn voor blokken die niemand ziet.
        backups_sections: list[Any] = []
        if tab_from_path(request.url.path) == "backups":
            open_deployment = next(
                (dep for dep in project_data_decrypted.get("deployments", []) if dep.get("name") == deployment_open),
                None,
            )
            if open_deployment is not None:
                backups_sections = collect_backups_sections(deployment_page_context(open_deployment))

        # Read-only detail-page sections the project's services deliver (WP2): each
        # service owns its own block instead of the general template hardcoding an
        # include. Built from the decrypted data so a service can surface its managed
        # credentials (e.g. keycloak realm admin details).
        service_detail_sections = collect_detail_page_sections(project_data_decrypted, role_for_services)

        # Die blokken hebben sinds RC-101 een EIGEN tabblad (Toegang): het zijn de
        # adressen, sleutels en bestanden waarmee je de diensten gebruikt, en daarvoor kom
        # je terug. Tussen de rest van Overzicht waren ze niet te vinden.
        #
        # Levert geen enkele dienst iets, dan is er geen tabblad: een lege pagina achter
        # een tab is een belofte die niet waargemaakt wordt. De tab valt uit de balk
        # (``lege_tabs`` hieronder), en wie er via een gedeelde link toch op uitkomt gaat
        # naar Overzicht in plaats van naar het niets.
        lege_tabs = () if service_detail_sections else TABS_MET_VOORWAARDE
        if tab_from_path(request.url.path) in lege_tabs:
            return RedirectResponse(url=project_tab_url(project_name, STANDAARD_TAB), status_code=302)

        # Changes saved with rollout=false that nobody has rolled out yet (RC-46). Shown
        # above the tabs, because a project file that silently runs ahead of the cluster is
        # worse than a slow rollout. A task service that is not up must not take the page
        # down with it, so a failure here degrades to "no notice".
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

        return render(
            request,
            template="bg/project-tabs.html.j2",
            context={
                "request": request,
                "title": f"Project Details - {project_details['display_name']}",
                "menu_items": get_menu_items(user),
                "project": project_details,
                "user": user,
                "user_role": user_role,
                "ServiceAdapter": ServiceAdapter,
                # How a service is chosen, and -- when it has no project-wide settings --
                # where it IS configured. Both derived from the registry (RC-33).
                "service_binding_label": binding_label,
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
                # De statuslabels per rij van de deploymenttabel (Overzicht), en de
                # gebundelde ArgoCD-stand waar de kolom "Laatste sync" uit leest.
                "deployment_status_tags": deployment_status_tags,
                "deployment_argocd": argocd_statuses,
                # Per-deployment service-owned blocks (RC-24), keyed by deployment name.
                "deployment_service_sections": deployment_service_sections,
                # Het backupsblok van de open deployment, voor het tabblad Backups (RC-100).
                # Leeg op elk ander tabblad, en leeg voor een project dat niets kan backuppen.
                "backups_sections": backups_sections,
                # Detail-page sections the project's services own (WP2). Replaces the
                # hardcoded per-service includes (e.g. the Keycloak realm block, which
                # after RC-5's config move kept reading the old project-level
                # ``config.keycloak`` and silently stopped rendering).
                "service_detail_sections": service_detail_sections,
                **build_lotc_project_details(
                    request,
                    user=user,
                    project=project_details,
                    deployment_open=deployment_open,
                    lege_tabs=lege_tabs,
                ),
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        import traceback

        error_details = traceback.format_exc()
        logger.error(f"Error serving project details: {e!s}\n{error_details}")

        # Try to extract line number from Jinja2 error
        error_msg = str(e)
        if hasattr(e, "lineno"):
            error_msg = f"Line {e.lineno}: {error_msg}"

        # Include template source snippet if available
        if hasattr(e, "source") and hasattr(e, "lineno"):
            lines = e.source.splitlines()
            line_num = e.lineno - 1
            if 0 <= line_num < len(lines):
                error_msg += f"\nSource: {lines[line_num].strip()}"

        raise HTTPException(status_code=500, detail=f"Template error: {error_msg}")


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
    from datetime import datetime

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
            pass


async def _fetch_argocd_deployment_status(
    project_name: str, deployment: dict[str, Any], argo: Any, kubectl: Any
) -> dict[str, Any]:
    """Fetch ArgoCD status for one deployment, with interpreted errors when unhealthy."""
    from opi.services.deployment_diagnostics import conditions_to_errors, gather_deployment_errors
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

        # Components intentionally scaled to zero (auto-disabled by the watcher or manually):
        # their resources must not be reported as a live "busy"/problem (WP6).
        disabled_components = frozenset(
            ref
            for comp in deployment.get("components", []) or []
            if comp.get("disabled") and (ref := comp.get("reference"))
        )

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
        else:
            # Healthy last-known state can still hide a fresh ComparisonError (sync=Unknown):
            # read the cheap app-level conditions unconditionally - no extra API call - so a
            # render/compare error is not filtered out on precisely the moment it matters.
            raw_errors = conditions_to_errors(status_data)

        component_names = [c.get("reference") for c in deployment.get("components", []) or [] if c.get("reference")]
        errors = interpret_argocd_errors(raw_errors, deployment_name=deployment_name, component_names=component_names)
        _annotate_argocd_error_ages(errors)

        last_sync = operation_state.get("finishedAt")
        if not last_sync and sync.get("status") == "Synced":
            last_sync = status.get("reconciledAt")

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
            "errors": errors,
        }
    except Exception as app_error:
        logger.warning(f"Failed to fetch ArgoCD status for {app_name}: {app_error}")
        return _argocd_unavailable_result(app_name, str(app_error), source="API")


@web_router.get("/dashboard/resource-usage", response_class=HTMLResponse)
@requires_sso
async def dashboard_resource_usage_fragment(request: Request) -> HTMLResponse:
    """Het resourcegebruik van het dashboard, apart opgehaald.

    De zes Prometheus-queries plus een per project duren te lang om de pagina op te laten
    wachten. Dit is dezelfde aanpak als de projectpagina al gebruikte, en dezelfde die de
    RVO-pagina hier had.
    """
    from opi.services.project_store import get_project_store

    user = get_current_user(request)
    user_email = (user or {}).get("email", "")

    # Dezelfde verzameling namespaces als de dashboardroute: alleen projecten waar deze
    # gebruiker bij mag. Zonder die grens zou dit fragment meer laten zien dan de pagina.
    from opi.core.cluster_config import get_prefixed_namespace

    all_namespaces: list[str] = []
    user_projects: list[dict] = []
    for project in get_project_store().get_all():
        if not is_user_authorized_for_project(project.name, user_email):
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
    # Geheugen erbij: dat is waar je op stuurt, en CPU alleen zei te weinig.
    total_memory_usage = sum(project.get("memory_mb", 0) or 0 for project in user_projects)

    return render(
        request,
        template="bg/_dashboard-usage.html.j2",
        context={
            "request": request,
            "metrics": metrics,
            "prometheus_available": prometheus_available,
            "projects": user_projects,
            # Beide totalen, net als de dashboardroute. Het fragment rekende
            # total_memory_usage al uit maar gaf het niet mee, en de omgeving staat op
            # StrictUndefined: een sjabloon dat ernaar vraagt levert dan een 500 op het
            # hele fragment in plaats van een lege regel.
            "total_cpu_usage": total_cpu_usage,
            "total_memory_usage": total_memory_usage,
            # De kaart Pods staat in de pagina maar het getal komt uit deze queries; het
            # fragment schuift hem er out-of-band overheen.
            "pod_count": pod_count,
        },
    )


@web_router.get("/projects/details/{project_name}/resource-usage", response_class=HTMLResponse)
@requires_sso
async def project_resource_usage_fragment(request: Request, project_name: str) -> HTMLResponse:
    """Compact project-wide CPU and memory totals (HTMX lazy-load).

    A project's deployments -- including every PR environment -- share one namespace,
    so the per-deployment metrics blocks never show the total. This sums across the
    project's namespaces, the same Prometheus queries the dashboard's Resource Usage
    card uses, so a project with 18 PRs shows one honest number for its footprint.

    Memory is the working set (what is actually resident), not the limit, which is
    the number that answers "how much is this project really using".

    Lazy on its own request: Prometheus is cheap, but this keeps it off the page's
    render and out of the way if Prometheus is down.
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
        ns_regex = "|".join(namespaces)
        try:
            from opi.connectors.prometheus import get_metrics_connector

            prom = await get_metrics_connector()
            if not prom.is_connected:
                ctx["usage_error"] = "Prometheus is niet beschikbaar"
            else:

                async def _scalar(promql: str) -> float:
                    result = await prom.custom_query(promql)
                    if result and result[0].get("value"):
                        return float(result[0]["value"][1])
                    return 0.0

                cpu_used = await _scalar(
                    f'sum(rate(container_cpu_usage_seconds_total{{namespace=~"{ns_regex}",container!=""}}[5m]))'
                )
                cpu_limit = await _scalar(
                    f'sum(kube_pod_container_resource_limits{{namespace=~"{ns_regex}",resource="cpu"}})'
                )
                mem_used = await _scalar(
                    f'sum(container_memory_working_set_bytes{{namespace=~"{ns_regex}",container!=""}})'
                )
                mem_limit = await _scalar(
                    f'sum(kube_pod_container_resource_limits{{namespace=~"{ns_regex}",resource="memory"}})'
                )
                pods = await _scalar(f'count(kube_pod_info{{namespace=~"{ns_regex}"}})')

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
        except Exception as e:
            logger.warning(f"Failed to fetch project resource usage for {project_name}: {e}")
            ctx["usage_error"] = str(e)

    # Ook dit fragment kent de LOTC-weergave. Zonder zou de projectpagina onder ?ui=lotc
    # wel LOTC zijn, maar het blokje dat htmx erin laadt nog roos - een pagina die
    # halverwege van vormgeving wisselt.

    return render(
        request,
        template="bg/_resource-usage.html.j2",
        context=ctx,
    )


@web_router.get("/projects/details/{project_name}/argocd-status/{deployment_name}", response_class=HTMLResponse)
@requires_sso
async def argocd_status_fragment(
    request: Request, project_name: str, deployment_name: str, prefix: str = ""
) -> HTMLResponse:
    """ArgoCD status HTML fragment for a single deployment (HTMX lazy-load)."""
    from opi.connectors.argo import create_argo_connector
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

    deployment = next(
        (d for d in (project.data or {}).get("deployments", []) if d.get("name") == deployment_name),
        None,
    )
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    argo = create_argo_connector()
    if argo.auth_token is None:
        status = _argocd_unavailable_result(
            generate_argocd_application_name(project_name, deployment_name), "ArgoCD niet beschikbaar"
        )
    else:
        status = await _fetch_argocd_deployment_status(project_name, deployment, argo, create_kubectl_connector())

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
            # What the services report about this deployment (RC-35). Read from the project
            # file, not from the cluster: zero replicas can also mean something went wrong,
            # and the card has to tell those two apart.
            "deployment_states": {deployment_name: collect_deployment_state(project.data or {}, deployment_name)},
        },
    )


#: De reeksen die het metingenfragment tekent. Een meting kan ook alleen limieten
#: bevatten (cpu_limit, memory_limit): die komen uit de deploymentdefinitie en niet uit
#: een meting, dus ze tellen niet mee voor "is er iets gemeten".
METINGREEKSEN = ("cpu", "memory", "network_in", "network_out", "disk_read", "disk_write")


def _heeft_metingen(metrics: dict[str, dict[str, Any]], pvc_storage: dict[str, dict[str, Any]]) -> bool:
    """Heeft Prometheus ergens een waarde teruggegeven?

    Zo niet, dan is dat een TOESTAND ("nog niets gemeten") en geen leegte, en zegt het
    fragment dat met een melding in plaats van met zes lege grafieken.
    """
    if any(pvc.get("values") for pvc in pvc_storage.values()):
        return True
    return any(meting.get(reeks) for meting in metrics.values() for reeks in METINGREEKSEN)


@web_router.get("/projects/details/{project_name}/metrics/{deployment_name}", response_class=HTMLResponse)
@requires_sso
async def deployment_metrics_fragment(
    request: Request, project_name: str, deployment_name: str, duration: int = 60
) -> HTMLResponse:
    """Return metrics HTML fragment for a single deployment (HTMX lazy-load)."""

    # Validate and compute step interval based on duration
    allowed_durations = {60, 120, 360, 720, 1440}
    if duration not in allowed_durations:
        duration = 60
    # Scale step to keep ~12-20 data points per chart
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
    deployments = project_data.get("deployments", [])

    # Find the requested deployment
    deployment = None
    for d in deployments:
        if d.get("name") == deployment_name:
            deployment = d
            break

    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    base_namespace = deployment.get("namespace")
    cluster = deployment.get("cluster")
    components = deployment.get("components", [])
    helm_charts = deployment.get("helm-charts", [])

    metrics: dict[str, dict[str, Any]] = {}
    discovered_workloads: list[dict[str, Any]] = []
    pvc_storage: dict[str, dict[str, Any]] = {}
    # Prometheus die niet antwoordt is iets anders dan Prometheus die antwoordt met niets,
    # en het fragment hoort die twee niet dezelfde melding te geven. Zonder deze vlag zag
    # de lezer in beide gevallen zes lege grafieken.
    prometheus_bereikbaar = True

    if base_namespace and cluster:
        try:
            from opi.connectors.prometheus import get_metrics_connector
            from opi.core.cluster_config import get_prefixed_namespace

            prom = await get_metrics_connector()
            prometheus_bereikbaar = prom.is_connected
            if prom.is_connected:
                k8s_namespace = get_prefixed_namespace(cluster, base_namespace)

                if components:
                    component_names = [c.get("reference") for c in components if c.get("reference")]
                    if component_names:
                        metrics = await prom.get_deployment_component_metrics_timeseries(
                            namespace=k8s_namespace,
                            components=component_names,
                            deployment_name=deployment_name,
                            duration_minutes=duration,
                            step_minutes=step,
                        )
                elif helm_charts:
                    workloads = await prom.discover_workloads_in_namespace(k8s_namespace)
                    if workloads:
                        discovered_workloads = workloads
                        metrics = await prom.get_discovered_workload_metrics_timeseries(
                            namespace=k8s_namespace,
                            workloads=workloads,
                            duration_minutes=duration,
                            step_minutes=step,
                        )

                try:
                    pvc_data = await prom.get_pvc_storage_by_namespace(
                        namespace=k8s_namespace,
                        duration_minutes=duration,
                        step_minutes=step,
                    )
                    if pvc_data:
                        # Filter PVCs to only those belonging to this deployment
                        # PVC names follow the pattern: {deployment_name}-{component_name}-...
                        prefix = f"{deployment_name}-"
                        pvc_storage = {name: data for name, data in pvc_data.items() if name.startswith(prefix)}
                except Exception as pvc_error:
                    logging.getLogger(__name__).warning(
                        f"Failed to fetch PVC storage for deployment {deployment_name}: {pvc_error}"
                    )
        except Exception as metrics_error:
            # Een bevraging die stukloopt is geen "nog geen metingen": er is niets
            # opgehaald omdat de meting zelf faalde.
            prometheus_bereikbaar = False
            logging.getLogger(__name__).warning(f"Failed to fetch Prometheus metrics: {metrics_error}")

    # Build a deployment-like object for the template (needs .name and .components attributes)
    class DeploymentContext:
        def __init__(self, name: str, components: list):
            self.name = name
            self.components = [type("C", (), {"reference": c.get("reference")})() for c in components]

    deployment_ctx = DeploymentContext(deployment_name, components)

    # Een deployment op een ANDER cluster levert hier geen metingen op, want deze OPI
    # bevraagt alleen de Prometheus van zijn eigen cluster. Zonder deze vlag zou dat als
    # "geen data" lezen, en dan zoekt de lezer de fout bij zijn applicatie. Deze melding
    # stond in het dienstblok van de metrics-scraper op het tabblad Deployments; dat blok
    # is weg omdat het dezelfde grafieken dubbel toonde, en de melding hoort thuis waar de
    # grafieken staan.
    from opi.core.config import settings

    ander_cluster = cluster if cluster and cluster != settings.CLUSTER_MANAGER else ""

    return render(
        request,
        template="bg/_deployment-metrics.html.j2",
        context={
            "request": request,
            "project_name": project_name,
            "deployment": deployment_ctx,
            "metrics": metrics,
            "discovered_workloads": discovered_workloads,
            "pvc_storage": pvc_storage,
            "duration": duration,
            "prometheus_bereikbaar": prometheus_bereikbaar,
            "ander_cluster": ander_cluster,
            "eigen_cluster": settings.CLUSTER_MANAGER,
            "metingen_leeg": not _heeft_metingen(metrics, pvc_storage),
        },
    )


@web_router.get("/projects/{project_name}/deployments/{deployment_name}/domain-settings")
@requires_sso
async def get_deployment_domain_settings(request: Request, project_name: str, deployment_name: str):
    """
    Get current domain settings for a deployment.

    This endpoint returns the current domain configuration for a specific deployment,
    including domain mode, subdomain, base domain, and component information.

    Args:
        request: The FastAPI request object
        project_name: Name of the project
        deployment_name: Name of the deployment

    Returns:
        JSON response with domain settings
    """

    from opi.api.router import DeploymentDomainSettingsResponse
    from opi.web.router_self_service import get_cluster_base_domains_for_template

    try:
        user = get_current_user(request)
        user_email = user.get("email", "").lower()

        # Get project service to validate access

        # Check if user has access to this project
        if not is_user_authorized_for_project(project_name, user_email):
            logger.warning(f"User {user_email} not authorized to access project: {project_name}")
            raise HTTPException(status_code=403, detail="You are not authorized to access this project")

        # Get project details
        project = get_project_store().get(project_name)
        if not project:
            raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")

        # === PATH TRAVERSAL PROTECTION ===
        _validate_path_safe(project.filename)

        project_data = project.data or {}

        # Find the deployment
        deployments = project_data.get("deployments", [])
        deployment = None
        for dep in deployments:
            if dep.get("name") == deployment_name:
                deployment = dep
                break

        if not deployment:
            raise HTTPException(
                status_code=404, detail=f"Deployment '{deployment_name}' not found in project '{project_name}'"
            )

        # Extract domain settings from deployment
        cluster = deployment.get("cluster", "")
        domain_mode = get_domain_setting(deployment, DomainSetting.DOMAIN_MODE)
        domain_format = get_domain_setting(deployment, DomainSetting.DOMAIN_FORMAT)
        subdomain = get_domain_setting(deployment, DomainSetting.SUBDOMAIN)
        base_domain = get_domain_setting(deployment, DomainSetting.BASE_DOMAIN)

        # Find root component (if any)
        root_component = get_domain_setting(deployment, DomainSetting.ROOT_COMPONENT)
        components_list = []
        for comp in deployment.get("components", []):
            comp_ref = comp.get("reference")
            if comp_ref:
                components_list.append({"reference": comp_ref, "root": comp_ref == root_component})

        # Get supported base domains for this cluster
        cluster_base_domains = get_cluster_base_domains_for_template()
        supported_domains = cluster_base_domains.get(cluster, [])

        response_data = DeploymentDomainSettingsResponse(
            deployment_name=deployment_name,
            cluster=cluster,
            domain_mode=domain_mode,
            domain_format=domain_format,
            subdomain=subdomain,
            base_domain=base_domain,
            root_component=root_component,
            components=components_list,
            supported_base_domains=supported_domains,
        )

        return JSONResponse(content=response_data.model_dump())

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting domain settings for {project_name}/{deployment_name}: {e!s}")
        # Don't expose internal error details to the user
        raise HTTPException(status_code=500, detail="An error occurred while fetching domain settings.")


async def _validate_csrf(request: Request, form_data: Mapping[str, Any] | None = None) -> None:
    """
    Validate CSRF protection (double-submit token + Origin/Referer).

    CSRF enforcement is now central in CSRFMiddleware, so by the time a
    handler runs the request has already been validated. This helper is kept
    for the explicit call site and is idempotent: it re-runs the same checks
    against the same token, which always passes for a legitimately validated
    request.

    Args:
        request: The FastAPI request object
        form_data: Optional pre-parsed form data (to avoid parsing twice). Any mapping;
            the handlers pass starlette's ``FormData``, which is not a dict.

    Raises:
        HTTPException: If CSRF validation fails
    """
    from opi.utils.csrf import validate_csrf_origin, validate_csrf_token

    csrf_form_data = dict(form_data) if form_data else None
    validate_csrf_token(request, csrf_form_data)
    validate_csrf_origin(request)


def _validate_path_safe(filename: str) -> None:
    """
    Validate that a filename is safe and doesn't contain path traversal sequences.

    Args:
        filename: The filename to validate

    Raises:
        HTTPException: If the filename contains path traversal sequences
    """
    import os

    # Check for path traversal patterns
    if ".." in filename:
        logger.warning(f"Path traversal attempt detected in filename: {filename}")
        raise HTTPException(status_code=400, detail="Invalid project filename")

    # Check for absolute paths
    if os.path.isabs(filename):
        logger.warning(f"Absolute path detected in filename: {filename}")
        raise HTTPException(status_code=400, detail="Invalid project filename")

    # Check for other dangerous patterns
    dangerous_patterns = ["//", "\\", "\x00"]
    for pattern in dangerous_patterns:
        if pattern in filename:
            logger.warning(f"Dangerous pattern '{pattern}' detected in filename: {filename}")
            raise HTTPException(status_code=400, detail="Invalid project filename")


async def _update_keycloak_redirect_uris_for_deployment(
    project_manager: ProjectManager,
    project_name: str,
    deployment_name: str,
    cluster: str,
    domain_mode: str,
    subdomain: str | None,
    base_domain: str | None,
) -> None:
    """
    Update Keycloak client redirect URIs after domain settings change.

    This function checks if the deployment uses Keycloak service and updates
    the redirect URIs to match the new hostnames based on domain settings.

    Args:
        project_manager: The ProjectManager instance with project data
        project_name: Name of the project
        deployment_name: Name of the deployment
        cluster: Name of the cluster
        domain_mode: Domain mode (component-specific, deployment-name, custom, nice-url)
        subdomain: Subdomain for nice-url or custom mode
        base_domain: Base domain for nice-url or custom mode
    """
    from opi.connectors.keycloak import create_keycloak_connector
    from opi.core.cluster_config import get_ingress_postfix, get_keycloak_support_http
    from opi.core.config import settings
    from opi.services import ServiceAdapter, ServiceType
    from opi.utils.naming import get_deployment_hostnames

    try:
        # Get refreshed project data
        project_data = await project_manager.get_contents()

        # Check if deployment uses Keycloak service
        deployment = None
        for dep in project_data.get("deployments", []):
            if dep.get("name") == deployment_name:
                deployment = dep
                break

        if not deployment:
            logger.warning(f"Deployment {deployment_name} not found in project data, skipping Keycloak update")
            return

        # Get component references for this deployment that use Keycloak
        component_refs = [comp.get("reference") for comp in deployment.get("components", []) if comp.get("reference")]

        sso_components = []
        for component_ref in component_refs:
            for component in project_data.get("components", []):
                if component.get("name") == component_ref:
                    service_names = ServiceAdapter.extract_service_names_from_project_services(
                        component.get("services", [])
                    )
                    component_services = ServiceAdapter.parse_services_from_strings(service_names)
                    if ServiceType.KEYCLOAK in component_services:
                        sso_components.append(component_ref)
                    break

        if not sso_components:
            logger.debug(f"No SSO components found in deployment {deployment_name}, skipping Keycloak update")
            return

        logger.info(
            f"Updating Keycloak redirect URIs for deployment {deployment_name} with {len(sso_components)} SSO components"
        )

        # Get Keycloak configuration for this cluster
        kc_config = await project_manager._get_project_keycloak_config_for_cluster(cluster)
        if not kc_config:
            logger.warning(f"No Keycloak config found for cluster {cluster}, skipping redirect URI update")
            return

        realm_name = kc_config["realm"]
        keycloak_host = kc_config["host"]

        # Calculate new hostnames based on domain settings
        ingress_postfix = get_ingress_postfix(cluster)
        all_ingress_hosts = get_deployment_hostnames(
            component_names=sso_components,
            deployment_name=deployment_name,
            project_name=project_name,
            ingress_postfix=ingress_postfix,
            subdomain=subdomain,
            base_domain=base_domain,
            domain_format=get_domain_setting(deployment, DomainSetting.DOMAIN_FORMAT),
            project_data=project_data,
            cluster=cluster,
        )

        if not all_ingress_hosts:
            logger.warning(f"No ingress hosts generated for deployment {deployment_name}, skipping Keycloak update")
            return

        logger.info(f"New hostnames for Keycloak client: {all_ingress_hosts}")

        # Get HTTP support setting and additional redirect URIs from config
        support_http = get_keycloak_support_http(cluster)

        # Get additional redirect URIs from project keycloak service config.
        # Format-agnostic: ``"keycloak" in service_item`` only matched the legacy
        # single-key form, so extra redirect URIs were dropped for record-form projects.
        from opi.services.services import service_entry_config, service_entry_name

        additional_redirect_uris = None
        project_services = project_data.get("services", [])
        for service_item in project_services:
            if service_entry_name(service_item) != ServiceType.KEYCLOAK.value:
                continue
            config = service_entry_config(service_item)
            if isinstance(config, dict):
                additional_redirect_uris = config.get("additional_redirect_uris")
            break

        # Create Keycloak connector and update redirect URIs
        keycloak = await create_keycloak_connector(
            keycloak_url=keycloak_host,
            admin_username=settings.KEYCLOAK_ADMIN_USERNAME,
            admin_password=settings.KEYCLOAK_ADMIN_PASSWORD,
        )

        result = await keycloak.update_deployment_client_hosts(
            deployment_name=deployment_name,
            project_name=project_name,
            ingress_hosts=all_ingress_hosts,
            realm_name=realm_name,
            support_http=support_http,
            additional_redirect_uris=additional_redirect_uris,
        )

        if result:
            logger.info(f"Successfully updated Keycloak redirect URIs for deployment {deployment_name}")
        else:
            logger.warning(
                f"Failed to update Keycloak redirect URIs for deployment {deployment_name} (client not found)"
            )

    except Exception as e:
        # Log the error but don't fail the entire operation
        # The ingresses have already been updated, SSO might work with existing URIs
        logger.error(f"Error updating Keycloak redirect URIs for {deployment_name}: {e}")


@web_router.post("/projects/{project_name}/deployments/{deployment_name}/domain-settings")
@requires_sso
async def update_deployment_domain_settings(request: Request, project_name: str, deployment_name: str):
    """
    Update domain settings for a deployment.

    This endpoint updates the domain configuration for a specific deployment.
    It validates subdomain availability (for nice-url mode), updates the project YAML,
    and re-processes the project to apply changes.

    Security:
        - Requires SSO authentication
        - Validates CSRF via Origin/Referer headers
        - Validates path safety for project filenames
        - Validates root_component against actual deployment components

    Args:
        request: The FastAPI request object
        project_name: Name of the project
        deployment_name: Name of the deployment

    Returns:
        JSON response with update status and redirect URL
    """

    from opi.connectors.subdomain import (
        validate_base_domain,
        validate_subdomain,
    )
    from opi.manager.project_manager import ProjectManager
    from opi.services.persistence.subdomain_registry import create_subdomain_connector

    # Track state for rollback
    original_data: dict[str, Any] | None = None
    git_committed = False
    subdomain_registered = False
    old_subdomain_info: dict | None = None

    try:
        # Parse form data first (needed for CSRF validation)
        form_data = await request.form()

        # === CSRF PROTECTION (token + origin/referer validation) ===
        await _validate_csrf(request, form_data)

        user = get_current_user(request)
        user_email = user.get("email", "").lower()

        # Get project service to validate access

        # Check if user has access to this project
        if not is_user_authorized_for_project(project_name, user_email):
            logger.warning(f"User {user_email} not authorized to access project: {project_name}")
            raise HTTPException(status_code=403, detail="You are not authorized to access this project")

        # Check if user has admin or owner role
        user_role = get_user_role_for_project(project_name, user_email)
        if user_role not in ["admin", "owner"]:
            logger.warning(f"User {user_email} with role '{user_role}' cannot edit domain settings: {project_name}")
            raise HTTPException(
                status_code=403, detail=f"Only admin or owner roles can edit domain settings. Your role: {user_role}"
            )

        # Extract form fields
        domain_mode = str(form_data.get("domain-mode", "")).strip()
        subdomain = str(form_data.get("subdomain", "")).strip() or None
        base_domain = str(form_data.get("base-domain", "")).strip() or None
        root_component = str(form_data.get("root-component", "")).strip() or None

        # Validate domain mode
        valid_modes = ["component-specific", "deployment-name", "custom", "nice-url"]
        if domain_mode not in valid_modes:
            raise HTTPException(status_code=400, detail=f"Invalid domain mode. Valid modes: {', '.join(valid_modes)}")

        # Get project details
        project = get_project_store().get(project_name)
        if not project:
            raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")

        # === PATH TRAVERSAL PROTECTION ===
        _validate_path_safe(project.filename)

        project_data = project.data or {}

        # Find the deployment
        deployments = project_data.get("deployments", [])
        deployment = None
        for dep in deployments:
            if dep.get("name") == deployment_name:
                deployment = dep
                break

        if not deployment:
            raise HTTPException(
                status_code=404, detail=f"Deployment '{deployment_name}' not found in project '{project_name}'"
            )

        cluster = deployment.get("cluster", "")

        # === VALIDATE ROOT COMPONENT ===
        # Get list of valid component references for this deployment
        deployment_components = deployment.get("components", [])
        valid_component_refs = {comp.get("reference") for comp in deployment_components if comp.get("reference")}

        if root_component and root_component not in valid_component_refs:
            logger.warning(
                f"Invalid root_component '{root_component}' for deployment '{deployment_name}'. "
                f"Valid components: {valid_component_refs}"
            )
            raise HTTPException(
                status_code=400,
                detail=f"Invalid root component. Must be one of: {', '.join(sorted(valid_component_refs)) or 'none available'}",
            )

        # === CAPTURE EXISTING SUBDOMAIN INFO FOR ROLLBACK ===
        # This must happen BEFORE we check domain_mode, so we can restore if switching away from nice-url
        subdomain_connector = create_subdomain_connector()
        existing_subdomain = await subdomain_connector.get_by_deployment(project_name, deployment_name)
        if existing_subdomain:
            old_subdomain_info = existing_subdomain  # Store for potential rollback

        # Validate nice-url settings if nice-url mode is selected. The two validated
        # values are kept separately: the registration further down needs them, and by
        # then nothing can still tell that this branch proved they are set.
        nice_url_subdomain = ""
        nice_url_base_domain = ""
        if domain_mode == "nice-url":
            if not subdomain:
                raise HTTPException(status_code=400, detail="Subdomain is required for nice-url mode")
            if not base_domain:
                raise HTTPException(status_code=400, detail="Base domain is required for nice-url mode")
            nice_url_subdomain = subdomain
            nice_url_base_domain = base_domain

            # Validate subdomain format
            is_valid, error_msg = validate_subdomain(subdomain)
            if not is_valid:
                raise HTTPException(status_code=400, detail=error_msg)

            # Validate base domain
            is_valid, error_msg = validate_base_domain(base_domain, cluster)
            if not is_valid:
                raise HTTPException(status_code=400, detail=error_msg)

            # Check subdomain availability (allow if already registered to this deployment)
            if existing_subdomain:
                # Check if subdomain changed
                if (
                    existing_subdomain["subdomain"] != subdomain.lower()
                    or existing_subdomain["base_domain"] != base_domain.lower()
                ):
                    # Check if new subdomain is available
                    is_available = await subdomain_connector.check_availability(subdomain, base_domain)
                    if not is_available:
                        raise HTTPException(
                            status_code=400, detail=f"Subdomain '{subdomain}.{base_domain}' is not available"
                        )
            else:
                # No existing registration, check availability
                is_available = await subdomain_connector.check_availability(subdomain, base_domain)
                if not is_available:
                    raise HTTPException(
                        status_code=400, detail=f"Subdomain '{subdomain}.{base_domain}' is not available"
                    )

        # Validate custom subdomain format if custom mode is selected
        elif domain_mode == "custom":
            if not subdomain:
                raise HTTPException(status_code=400, detail="Subdomain is required for custom mode")

            # Validate subdomain format (same rules as nice-url, but without base_domain or availability check)
            # This ensures the subdomain is DNS-compatible and not a reserved name
            is_valid, error_msg = validate_subdomain(subdomain)
            if not is_valid:
                raise HTTPException(status_code=400, detail=error_msg)

        # Mutate through the bound ProjectManager: it reads fresh, migrated
        # contents from Git and persists via the single validated save path.
        project_file_path = f"projects/{project.filename}"
        project_manager = ProjectManager(project_file_relative_path=project_file_path)
        project_yaml = await project_manager.get_contents()

        # Keep a copy of the pre-edit state for rollback on failure.
        original_data = copy.deepcopy(project_yaml)

        # Find and update deployment
        yaml_deployments = project_yaml.get("deployments", [])
        for yaml_dep in yaml_deployments:
            if yaml_dep.get("name") == deployment_name:
                # Update domain settings. Every write and every removal goes through the
                # service's own accessors (RC-60), so the modal cannot leave a value behind
                # in the deployment root that a later read would resurrect.
                set_domain_setting(yaml_dep, DomainSetting.DOMAIN_MODE, domain_mode)

                # Handle subdomain and base-domain based on mode
                if domain_mode == "nice-url":
                    set_domain_setting(yaml_dep, DomainSetting.SUBDOMAIN, subdomain)
                    set_domain_setting(yaml_dep, DomainSetting.BASE_DOMAIN, base_domain)
                    # Auto-enable Let's Encrypt for nice-url mode (HTTPS by default)
                    set_domain_setting(yaml_dep, DomainSetting.ISSUER, "letsencrypt")
                elif domain_mode == "custom":
                    set_domain_setting(yaml_dep, DomainSetting.SUBDOMAIN, subdomain)
                    # Remove base-domain and issuer for custom mode
                    pop_domain_setting(yaml_dep, DomainSetting.BASE_DOMAIN)
                    pop_domain_setting(yaml_dep, DomainSetting.ISSUER)
                else:
                    # Remove subdomain, base-domain, and issuer for other modes
                    pop_domain_setting(yaml_dep, DomainSetting.SUBDOMAIN)
                    pop_domain_setting(yaml_dep, DomainSetting.BASE_DOMAIN)
                    pop_domain_setting(yaml_dep, DomainSetting.ISSUER)

                # Handle root component — set on deployment level
                if root_component:
                    set_domain_setting(yaml_dep, DomainSetting.ROOT_COMPONENT, root_component)
                else:
                    pop_domain_setting(yaml_dep, DomainSetting.ROOT_COMPONENT)

                # Clean up any legacy root flags on components
                for comp in yaml_dep.get("components", []):
                    if "root" in comp:
                        del comp["root"]

                break

        # === STEP 1: Save to git ===
        # Sanitize commit message by removing potentially dangerous characters
        safe_deployment_name = "".join(c for c in deployment_name if c.isalnum() or c in "-_")
        safe_project_name = "".join(c for c in project_name if c.isalnum() or c in "-_")

        await project_manager.save_and_commit_project(
            project_yaml,
            f"Update domain settings for deployment '{safe_deployment_name}' in project '{safe_project_name}'",
        )
        git_committed = True
        logger.info(f"Updated domain settings for {project_name}/{deployment_name} by {user_email}")

        # === STEP 2: Register/update subdomain ===
        # Track whether we deleted an existing subdomain (for rollback)
        subdomain_deleted = False

        try:
            if domain_mode == "nice-url":
                # Reuse the subdomain_connector created earlier
                await subdomain_connector.register_or_update_for_deployment(
                    subdomain=nice_url_subdomain,
                    base_domain=nice_url_base_domain,
                    project_name=project_name,
                    deployment_name=deployment_name,
                    cluster=cluster,
                    created_by=user_email,
                )
                subdomain_registered = True
                logger.info(f"Registered subdomain '{subdomain}.{base_domain}' for {project_name}/{deployment_name}")
            else:
                # If switching away from nice-url, delete any existing subdomain registration
                # Reuse the subdomain_connector created earlier
                deleted = await subdomain_connector.delete_by_deployment(project_name, deployment_name)
                if deleted:
                    subdomain_deleted = True
                    logger.info(f"Deleted subdomain registration for {project_name}/{deployment_name}")
        except Exception as subdomain_error:
            logger.error(f"Subdomain registration failed: {subdomain_error}")

            # Rollback git commit first
            if git_committed and original_data is not None:
                try:
                    await project_manager.save_and_commit_project(
                        original_data,
                        f"Rollback domain settings for '{safe_deployment_name}' (subdomain registration failed)",
                    )
                    logger.info(f"Rolled back git commit for {project_name}/{deployment_name}")
                except Exception as rollback_error:
                    logger.error(f"Failed to rollback git commit: {rollback_error}")

            # Restore deleted subdomain if we deleted one before the error
            if subdomain_deleted and old_subdomain_info:
                try:
                    await subdomain_connector.register_or_update_for_deployment(
                        subdomain=old_subdomain_info["subdomain"],
                        base_domain=old_subdomain_info["base_domain"],
                        project_name=project_name,
                        deployment_name=deployment_name,
                        cluster=old_subdomain_info["cluster"],
                        created_by=user_email,
                    )
                    logger.info(f"Restored deleted subdomain for {project_name}/{deployment_name}")
                except Exception as restore_error:
                    logger.error(f"Failed to restore deleted subdomain: {restore_error}")

            raise HTTPException(status_code=500, detail="Failed to register subdomain. Changes have been rolled back.")

        # === STEP 3: Re-process project to apply changes ===
        try:
            processing_result = await project_manager.process_project_from_git(
                project_file_path, deployment_name=deployment_name
            )
            logger.info(f"Re-processed project {project_name} after domain settings update: {processing_result}")

            # === STEP 4: Update Keycloak redirect URIs ===
            # After domain settings change, update Keycloak client redirect URIs to match new hostnames
            await _update_keycloak_redirect_uris_for_deployment(
                project_manager=project_manager,
                project_name=project_name,
                deployment_name=deployment_name,
                cluster=cluster,
                domain_mode=domain_mode,
                subdomain=subdomain,
                base_domain=base_domain,
            )
        except Exception as processing_error:
            logger.error(f"Project re-processing failed: {processing_error}")

            # Rollback in reverse order of operations:
            # 1. First rollback git (was step 1)
            # 2. Then rollback subdomain (was step 2)
            # This ensures consistent state even if one rollback fails

            # Rollback git commit first
            if git_committed and original_data is not None:
                try:
                    await project_manager.save_and_commit_project(
                        original_data,
                        f"Rollback domain settings for '{safe_deployment_name}' (processing failed)",
                    )
                    logger.info(f"Rolled back git commit for {project_name}/{deployment_name}")
                except Exception as git_rollback_error:
                    logger.error(f"Failed to rollback git commit: {git_rollback_error}")

            # Rollback subdomain changes (reuse existing subdomain_connector)
            try:
                if subdomain_registered and domain_mode == "nice-url":
                    # We registered a new subdomain - need to undo that
                    if old_subdomain_info:
                        # Restore old subdomain (was changed)
                        await subdomain_connector.register_or_update_for_deployment(
                            subdomain=old_subdomain_info["subdomain"],
                            base_domain=old_subdomain_info["base_domain"],
                            project_name=project_name,
                            deployment_name=deployment_name,
                            cluster=old_subdomain_info["cluster"],
                            created_by=user_email,
                        )
                        logger.info(f"Restored old subdomain for {project_name}/{deployment_name}")
                    else:
                        # Delete newly registered subdomain (there was no old one)
                        await subdomain_connector.delete_by_deployment(project_name, deployment_name)
                        logger.info(f"Deleted new subdomain for {project_name}/{deployment_name}")

                elif subdomain_deleted and old_subdomain_info:
                    # We deleted an existing subdomain when switching away from nice-url
                    # Need to restore it
                    await subdomain_connector.register_or_update_for_deployment(
                        subdomain=old_subdomain_info["subdomain"],
                        base_domain=old_subdomain_info["base_domain"],
                        project_name=project_name,
                        deployment_name=deployment_name,
                        cluster=old_subdomain_info["cluster"],
                        created_by=user_email,
                    )
                    logger.info(f"Restored deleted subdomain for {project_name}/{deployment_name}")

            except Exception as subdomain_rollback_error:
                logger.error(f"Failed to rollback subdomain changes: {subdomain_rollback_error}")

            raise HTTPException(status_code=500, detail="Failed to apply changes. Settings have been rolled back.")
        finally:
            await project_manager.close()

        return JSONResponse(
            content={
                "success": True,
                "message": f"Domain settings updated successfully for deployment '{deployment_name}'",
                "redirect_url": f"/projects/{project_name}/details",
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating domain settings for {project_name}/{deployment_name}: {e!s}")
        # Don't expose internal error details to the user
        raise HTTPException(
            status_code=500, detail="An error occurred while updating domain settings. Please try again."
        )


def _projects_for_user(user: dict) -> list[dict]:
    """De projecten waar deze gebruiker bij mag, met de gegevens die het overzicht toont.

    Apart van de route zodat de opbouw op een plek staat: /projects levert zowel de hele
    pagina als - via hx-select - het stuk dat het zoekveld ververst.
    """
    user_email = user.get("email", "").lower()

    user_projects: list[dict] = []
    for project in get_project_store().get_all():
        project_name = project.name
        if not is_user_authorized_for_project(project_name, user_email):
            continue
        try:
            project_data = project.data or {}
            user_projects.append(
                {
                    "name": project_name,
                    "display_name": project_data.get("display-name", project_name),
                    "description": project_data.get("description", ""),
                    "users": project.users or [],
                    "user_role": get_user_role_for_project(project_name, user_email),
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


@web_router.get("/projects", response_class=HTMLResponse)
@requires_sso
async def projects_overview(request: Request):
    """
    Serve the projects overview page with table layout.
    Shows only projects where the current user's email is in the users list.

    Project data is automatically refreshed from Git if stale (older than 30 seconds).

    Args:
        request: The HTTP request

    Returns:
        HTML response with a table showing user's projects and their status
    """
    try:
        # `or {}` omdat get_current_user None kan geven. Dat valt hier veilig uit: een lege
        # gebruiker heeft geen e-mailadres, is_platform_admin weigert een lege string en
        # geen enkel projectlidmaatschap matcht erop, dus de lijst wordt leeg in plaats van
        # volledig. Dezelfde vorm als de andere aanroepers van get_current_user.
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
        import traceback

        error_details = traceback.format_exc()
        logger.error(f"Error serving projects overview: {e!s}\n{error_details}")

        # Try to extract line number from Jinja2 error
        error_msg = str(e)
        if hasattr(e, "lineno"):
            error_msg = f"Line {e.lineno}: {error_msg}"

        # Include template source snippet if available
        if hasattr(e, "source") and hasattr(e, "lineno"):
            lines = e.source.splitlines()
            line_num = e.lineno - 1
            if 0 <= line_num < len(lines):
                error_msg += f"\nSource: {lines[line_num].strip()}"

        raise HTTPException(status_code=500, detail=f"Template error: {error_msg}")


@web_router.get("/cli", response_class=HTMLResponse)
@requires_sso
async def cli_pagina(request: Request):
    """De ZAD CLI: wat het is, hoe je hem installeert, wat je ermee doet.

    Een wegwijzer en geen handleiding - de repository houdt zijn eigen documentatie bij,
    en die loopt vooruit op wat hier zou staan.
    """
    return _wegwijzer(request, "cli.html.j2", "bg/cli.html.j2", "/cli")


@web_router.get("/actions", response_class=HTMLResponse)
@requires_sso
async def actions_pagina(request: Request):
    """De ZAD Actions: uitrollen vanuit je eigen pijplijn."""
    return _wegwijzer(request, "actions.html.j2", "bg/actions.html.j2", "/actions")


def _wegwijzer(request: Request, roos: str, lotc: str, pad: str):
    """Een pagina zonder eigen gegevens: alleen de schil en zijn navigatie."""
    user = get_current_user(request)
    from opi.web.navigation_lotc import get_navigation

    return render(
        request,
        template=lotc,
        context={
            "request": request,
            "menu_items": get_menu_items(user),
            "navigation": get_navigation(user, current_path=pad),
        },
    )


@web_router.get("/account", response_class=HTMLResponse)
@requires_sso
async def account_pagina(request: Request):
    """Je eigen account: wat er in de sessie staat, en verder niets.

    De naam rechtsboven linkte naar /account en die route bestond niet - een 404 op een
    link die in elke schil staat. ZAD houdt zelf geen profiel bij; naam en e-mailadres
    komen uit de inlogdienst, dus er valt hier niets in te stellen.
    """
    user = get_current_user(request)
    from opi.web.navigation_lotc import get_navigation

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
        },
    )


@web_router.get("/weergave")
async def kies_weergave(request: Request, scheme: str = "", terug: str = "/"):
    """Onthoud de weergavekeuze (systeem, licht, donker) en ga terug waar je vandaan kwam.

    Server-side en niet in localStorage, om dezelfde reden als de layoutschakelaar: dan
    rendert de server de pagina meteen in de goede stand en flitst er niets. NLDD leest
    ``data-scheme`` op ``<html>``; de schil zet dat uit dit koekje.

    ``terug`` komt uit de URL en wordt daarom streng gehouden: alleen een pad op deze
    site. Een waarde die met ``//`` begint is geen relatief pad maar een ander domein, en
    dat zou van deze route een open doorverwijzing maken.
    """
    from opi.web.lotc_switch import SCHEME_COOKIE, SCHEMES

    bestemming = terug if terug.startswith("/") and not terug.startswith("//") else "/"
    antwoord = RedirectResponse(url=bestemming, status_code=303)
    antwoord.set_cookie(
        SCHEME_COOKIE,
        scheme if scheme in SCHEMES else "",
        max_age=60 * 60 * 24 * 365,
        samesite="lax",
        httponly=False,
    )
    return antwoord


@web_router.get("/about", response_class=HTMLResponse)
async def about_platform(request: Request):
    """Serve the 'About the platform' page."""
    try:
        user = get_current_user(request)
        from opi.web.navigation_lotc import get_navigation

        return render(
            request,
            template="bg/about.html.j2",
            context={
                "request": request,
                "menu_items": get_menu_items(user),
                "navigation": get_navigation(user, current_path="/about"),
            },
        )
    except Exception as e:
        logger.error(f"Error serving about page: {e!s}")
        raise HTTPException(status_code=500, detail=str(e))


@web_router.get("/test-template-variables", response_class=HTMLResponse)
@requires_sso
async def test_template_variables(request: Request):
    """Test route for debugging Jinja variables in componenttags."""
    try:
        from opi.web.navigation_lotc import get_navigation

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
        logger.error(f"Error serving test template variables: {e!s}")
        raise HTTPException(status_code=500, detail=f"Template error: {e!s}")


@web_router.get("/example", response_class=HTMLResponse)
@requires_sso
async def example_page(request: Request):
    """
    Serve a simple example page with just a header.

    Returns:
        HTML response with a basic c-page template
    """
    try:
        user = get_current_user(request)
        return templates_lotc.TemplateResponse(
            request,
            "example.html.j2",
            {"request": request, "title": "Example Page", "menu_items": get_menu_items(user)},
        )

    except Exception as e:
        import traceback

        error_details = traceback.format_exc()
        logger.error(f"Error serving example page: {e!s}\n{error_details}")

        # Try to extract line number from Jinja2 error
        error_msg = str(e)
        if hasattr(e, "lineno"):
            error_msg = f"Line {e.lineno}: {error_msg}"

        # Include template source snippet if available
        if hasattr(e, "source") and hasattr(e, "lineno"):
            lines = e.source.splitlines()
            line_num = e.lineno - 1
            if 0 <= line_num < len(lines):
                error_msg += f"\nSource: {lines[line_num].strip()}"

        raise HTTPException(status_code=500, detail=f"Template error: {error_msg}")


@web_router.get("/tools", response_class=HTMLResponse)
@requires_sso
async def tools_page(request: Request):
    """
    Serve the AGE encryption/decryption tools page.

    Returns:
        HTML response with AGE tooling interface
    """
    try:
        user = get_current_user(request)
        return templates_lotc.TemplateResponse(
            request,
            "tools.html.j2",
            {"request": request, "title": "AGE Encryption Tools", "menu_items": get_menu_items(user)},
        )

    except Exception as e:
        import traceback

        error_details = traceback.format_exc()
        logger.error(f"Error serving tools page: {e!s}\n{error_details}")

        error_msg = str(e)
        if hasattr(e, "lineno"):
            error_msg = f"Line {e.lineno}: {error_msg}"

        if hasattr(e, "source") and hasattr(e, "lineno"):
            lines = e.source.splitlines()
            line_num = e.lineno - 1
            if 0 <= line_num < len(lines):
                error_msg += f"\nSource: {lines[line_num].strip()}"

        raise HTTPException(status_code=500, detail=f"Template error: {error_msg}")


@web_router.post("/tools/encrypt")
@requires_sso
async def encrypt_text(request: Request):
    """
    Encrypt text using AGE public key.

    Returns:
        JSON response with encrypted content or error
    """
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

    except Exception as e:
        logger.error(f"Error encrypting text: {e!s}")
        return JSONResponse(content={"error": f"Encryption failed: {e!s}"}, status_code=500)


@web_router.post("/tools/decrypt")
@requires_sso
async def decrypt_text(request: Request):
    """
    Decrypt AGE-encrypted text using private key.

    Returns:
        JSON response with decrypted content or error
    """
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

    except Exception as e:
        logger.error(f"Error decrypting text: {e!s}")
        return JSONResponse(content={"error": f"Decryption failed: {e!s}"}, status_code=500)


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
    task_hierarchy = _build_task_hierarchy(subtasks)

    error = task.get("error_message")
    component_failures = None
    result = task.get("result")
    if isinstance(result, dict):
        processing = result.get("processing")
        if isinstance(processing, dict):
            component_failures = processing.get("component_failures")
            if not error:
                error = processing.get("error")

    return {
        "progress": task.get("progress_percent", 0),
        "current_step": stap_label(task.get("current_step")) or "Verwerking gestart...",
        "tasks": task_hierarchy,
        "status": template_status,
        "error": error,
        "component_failures": component_failures,
        "project_name": project_name,
    }


def _build_task_hierarchy(subtasks: list[dict]) -> list[dict]:
    """Convert flat V2 subtask list to nested task hierarchy for the template."""
    main_tasks = []
    children: dict[str, list] = {}
    for st in subtasks:
        # ``subject`` is absent from steps written before it existed, and from every
        # step that runs once per project; .get keeps both cases a plain None.
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
    return main_tasks


def _require_task_access(request: Request, task: dict, project_name: str) -> None:
    """Raise 403 unless this user may follow this task.

    Either the user is authorized for the project, or they are the one who started the
    task -- which is what keeps a delete followable (the project is gone from the store
    before the task ends) and a creation too (it is not in the store yet).
    """
    user_email = (get_current_user(request) or {}).get("email", "").lower()
    if (task.get("created_by") or "").lower() != user_email and not is_user_authorized_for_project(
        project_name, user_email
    ):
        raise HTTPException(status_code=403, detail="Geen toegang tot dit project")


async def _require_task_of_project(request: Request, task_service: Any, project_name: str, task_id: str) -> dict | None:
    """The task with this id, but only if it is this project's and this user's to see.

    The task id alone used to be enough to read any task's steps, from any account with
    a session. Both fragment routes sit under ``/projects/{project_name}/``, so scope
    them to it: the task must be that project's own, and the user must pass
    ``_require_task_access``. Returns None when there is no such task, so the caller
    answers 404.
    """
    task = await task_service.get_task(task_id)
    if task is None or task.get("project_name") != project_name:
        return None

    _require_task_access(request, task, project_name)
    return task


@web_router.get("/projects/{project_name}/task-progress/{task_id}", response_class=HTMLResponse)
@requires_sso
async def task_progress_fragment(request: Request, project_name: str, task_id: str) -> HTMLResponse:
    """Generic task progress fragment for HTMX polling.

    Reads task state from the V2 async task service (database-backed)
    and renders an HTML fragment for HTMX polling.
    """
    from opi.core.task_helpers import get_task_service

    task_service = get_task_service(request)
    task = await _require_task_of_project(request, task_service, project_name, task_id)
    if task is None:
        return HTMLResponse(content="<p>Taak niet gevonden</p>", status_code=404)

    context = _v2_task_to_template_context(task, project_name)
    context["task_id"] = task_id
    context["progress_url"] = f"/projects/{project_name}/task-progress/{task_id}"
    context["on_complete"] = on_complete_for(task.get("task_type"))

    # Rendered once on purpose -- see render_progress_fragment for why a second pass
    # over the rendered HTML would execute task text as Jinja.
    return HTMLResponse(content=render_progress_fragment(request, context))
