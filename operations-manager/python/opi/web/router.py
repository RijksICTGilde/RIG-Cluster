"""
Web routes for serving HTML pages (non-API endpoints).
"""

import asyncio
import copy
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

if TYPE_CHECKING:
    from opi.manager.project_manager import ProjectManager

from datetime import UTC

from opi.core.auth_decorators import get_current_user, requires_sso
from opi.core.templates import get_templates
from opi.utils.age import decrypt_password_smart, get_global_private_key
from opi.utils.csrf import ensure_csrf_token
from opi.utils.yaml_util import load_yaml_from_string
from opi.web.menu import get_menu_items

from ..utils.age import decrypt_age_content
from .metrics_explorer_router import metrics_explorer_router
from .router_detail_edit import detail_edit_router
from .router_self_service import check_subdomain_availability_web
from .router_subdomain_admin import subdomain_admin_router
from .router_usage import usage_router
from .router_user_admin import user_admin_router
from .router_wizard import wizard_router
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
web_router.include_router(subdomain_admin_router)


@web_router.get("/")
async def root():
    """
    Root route that redirects to the architecture overview page.

    Returns:
        Redirect response to /architecture
    """
    return RedirectResponse(url="/architecture", status_code=302)


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

    # Log the permission denied access
    user_email = user.get("email", "unknown") if user else "anonymous"
    logger.warning(f"Permission denied page accessed by: {user_email}")

    # Render the permission denied template
    templates = get_templates()
    return templates.TemplateResponse(
        "permission-denied.html.j2",
        {
            "request": request,
            "user": user,
            "menu_items": get_menu_items(user),  # Same menu as other pages
        },
    )


# Redirect old self-service portal URL to the wizard
@web_router.get("/projects/new")
async def redirect_projects_new_to_wizard():
    """Redirect legacy /projects/new to the wizard-based project creation flow."""
    return RedirectResponse(url="/forms/wizard/create-project", status_code=302)


# SSO-protected subdomain availability check (prevents unauthenticated enumeration)
web_router.add_api_route("/subdomains/check", check_subdomain_availability_web, methods=["GET"])


@web_router.get("/projects/progress/{task_id}", response_class=HTMLResponse)
@requires_sso
async def project_progress_page(request: Request, task_id: str):
    """
    Show the project creation progress page.

    Reads task state from the V2 async task service (database-backed).
    """
    try:
        from opi.core.task_helpers import get_task_service

        task_service = get_task_service(request)
        task = await task_service.get_task(task_id)
        user = get_current_user(request)
        templates = get_templates()

        if not task:
            return templates.TemplateResponse(
                "project-progress-done.html.j2",
                {
                    "request": request,
                    "title": "Taak niet beschikbaar",
                    "menu_items": get_menu_items(user),
                    "task_id": task_id,
                },
            )

        project_name = task.get("project_name", "")
        status = task.get("status", "pending")
        if status in ("pending", "claimed", "running"):
            template_status = "running"
        elif status == "completed":
            result = task.get("result")
            template_status = "failed" if isinstance(result, dict) and result.get("status") == "failed" else "completed"
        else:
            template_status = "failed"

        return templates.TemplateResponse(
            "project-progress.html.j2",
            {
                "request": request,
                "title": f"Creating Project: {project_name}",
                "menu_items": get_menu_items(user),
                "task_id": task_id,
                "project_name": project_name,
                "initial_progress": task.get("progress_percent", 0),
                "initial_step": task.get("current_step") or "Verwerking gestart...",
                "initial_status": template_status,
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving progress page: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error loading progress page: {e!s}")


@web_router.get("/ui/tasks/{task_id}/status")
@requires_sso
async def get_task_status(request: Request, task_id: str):
    """
    Get current task status and progress.

    Browser-only JSON endpoint polled from the progress page JavaScript.
    Lives under /ui/ (not /api/) because it relies on the session cookie,
    not on an X-API-Key header. Reads task state from the V2 async task
    service (database-backed).
    """
    from fastapi.responses import JSONResponse

    from opi.core.task_helpers import get_task_service

    task_service = get_task_service(request)
    task = await task_service.get_task(task_id)

    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    context = _v2_task_to_template_context(task, task.get("project_name", ""))
    response_data: dict[str, Any] = {
        "task_id": task_id,
        "status": context["status"],
        "current_step": context["current_step"],
        "project_name": context["project_name"],
        "progress": context["progress"],
        "tasks": context["tasks"],
    }
    if context["error"]:
        response_data["error"] = context["error"]

    return JSONResponse(content=response_data)


@web_router.get("/projects/roos", response_class=HTMLResponse)
@requires_sso
async def roos_project_form(request: Request):
    """
    Serve the ROOS-based project creation form using jinja-roos-components.

    Returns:
        HTML response with the ROOS component-based form
    """
    try:
        from opi.core.cluster_config import CLUSTER_CONFIG

        templates = get_templates()
        user = get_current_user(request)
        return templates.TemplateResponse(
            "roos-form-improved.html.j2",
            {
                "request": request,
                "title": "Project Aanmaken - ROOS",
                "clusters": list(CLUSTER_CONFIG.keys()),
                "menu_items": get_menu_items(user),
            },
        )
    except Exception as e:
        import traceback

        error_details = traceback.format_exc()
        logger.error(f"Error serving ROOS project form: {e!s}\n{error_details}")

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


@web_router.post("/projects/delete/{project_name}")
@requires_sso
async def delete_project_web(request: Request, project_name: str):
    """
    Delete a project via web interface with SSO validation.

    This endpoint provides SSO-validated project deletion for the web interface.
    It validates that the current user has the necessary permissions (admin or owner role)
    to delete the specified project.

    Args:
        request: The FastAPI request object
        project_name: Name of the project to delete

    Returns:
        JSON response with deletion results for AJAX consumption
    """
    try:
        from fastapi.responses import JSONResponse

        from opi.manager.project_manager import create_project_manager
        from opi.services.project_service import get_project_service

        # Get current user from SSO
        user = get_current_user(request)
        user_email = user.get("email", "").lower()

        logger.info(f"Web project deletion request for '{project_name}' by user: {user_email}")

        # Get project service to validate authorization
        project_service = get_project_service()

        # Check if project exists and user has access
        if not project_service.is_user_authorized_for_project(project_name, user_email):
            logger.warning(f"User {user_email} not authorized to access project: {project_name}")
            return JSONResponse(content={"error": "You are not authorized to access this project"}, status_code=403)

        # Check if user has admin or owner role for deletion
        user_role = project_service.get_user_role_for_project(project_name, user_email)
        if user_role not in ["admin", "owner"]:
            logger.warning(f"User {user_email} with role '{user_role}' cannot delete project: {project_name}")
            return JSONResponse(
                content={"error": f"Only admin or owner roles can delete projects. Your role: {user_role}"},
                status_code=403,
            )

        # Get project API key for deletion
        # Create project manager for deletion
        project_manager = create_project_manager()

        logger.info(f"Starting project deletion for '{project_name}' by {user_email} (role: {user_role})")

        # Perform the deletion using the deployment-aware deletion logic
        deletion_results = await project_manager.delete_project(project_name)

        # Determine response status and message based on deletion results
        if deletion_results["success"]:
            status_code = 200
            message = f"Project '{project_name}' deleted successfully"
            status = "completed"
            logger.info(f"Project deletion completed successfully for: {project_name}")
        elif deletion_results.get("remaining_deployments"):
            # Project deletion was blocked due to deployments on other clusters
            status_code = 409  # Conflict - cannot complete due to conflicting state
            remaining_deployments = deletion_results["remaining_deployments"]
            other_clusters = {dep["cluster"] for dep in remaining_deployments}
            message = f"Project '{project_name}' cannot be deleted because it has deployments on other clusters: {', '.join(other_clusters)}"
            status = "blocked"
            logger.warning(
                f"Project deletion blocked for {project_name}: deployments on other clusters: {other_clusters}"
            )
        else:
            # Partial success or errors during deletion
            status_code = 207  # Multi-Status - partial success
            message = f"Project '{project_name}' deletion completed with some errors"
            status = "partial"
            logger.warning(f"Project deletion completed with errors for: {project_name}")

        return JSONResponse(
            content={
                "status": status,
                "message": message,
                "project": project_name,
                "deletion_results": deletion_results,
                "success": deletion_results["success"],
            },
            status_code=status_code,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing web project deletion: {e!s}")
        return JSONResponse(content={"error": f"Error deleting project: {e!s}"}, status_code=500)


@web_router.post("/projects/{project_name}/delete-deployment/{deployment_name}")
@requires_sso
async def delete_deployment_web(request: Request, project_name: str, deployment_name: str):
    """Delete a deployment via web interface with SSO validation."""
    try:
        from fastapi.responses import JSONResponse

        from opi.manager.project_manager import create_project_manager
        from opi.services.project_service import get_project_service

        user = get_current_user(request)
        user_email = user.get("email", "").lower()

        logger.info(
            f"Web deployment deletion request for '{deployment_name}' in '{project_name}' by user: {user_email}"
        )

        project_service = get_project_service()

        if not project_service.is_user_authorized_for_project(project_name, user_email):
            return JSONResponse(content={"error": "Geen toegang tot dit project"}, status_code=403)

        user_role = project_service.get_user_role_for_project(project_name, user_email)
        if user_role not in ["admin", "owner"]:
            return JSONResponse(
                content={"error": f"Alleen admin of owner rollen kunnen deployments verwijderen. Uw rol: {user_role}"},
                status_code=403,
            )

        project_manager = create_project_manager()

        logger.info(f"Starting deployment deletion for '{deployment_name}' in '{project_name}' by {user_email}")
        deletion_results = await project_manager.delete_deployment(project_name, deployment_name)

        if deletion_results["success"]:
            logger.info(f"Deployment deletion completed successfully: {deployment_name}")
            return JSONResponse(
                content={
                    "success": True,
                    "message": f"Deployment '{deployment_name}' succesvol verwijderd",
                    "deletion_results": deletion_results,
                },
                status_code=200,
            )
        else:
            errors = deletion_results.get("errors", [])
            message = f"Deployment '{deployment_name}' verwijderen mislukt"
            if errors:
                message += f": {'; '.join(str(e) for e in errors)}"
            logger.warning(f"Deployment deletion failed for {deployment_name}: {errors}")
            return JSONResponse(
                content={"success": False, "error": message, "deletion_results": deletion_results},
                status_code=207,
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing web deployment deletion: {e!s}")
        return JSONResponse(content={"error": f"Fout bij verwijderen van deployment: {e!s}"}, status_code=500)


@web_router.post("/projects/{project_name}/delete-component/{component_name}")
@requires_sso
async def delete_component_web(request: Request, project_name: str, component_name: str):
    """Delete a component from a project via web interface."""
    try:
        from fastapi.responses import JSONResponse

        from opi.handlers.project_file_handler import save_project_file
        from opi.services.project_service import get_project_service

        user = get_current_user(request)
        user_email = user.get("email", "").lower()

        logger.info(f"Web component deletion request for '{component_name}' in '{project_name}' by user: {user_email}")

        project_service = get_project_service()

        if not project_service.is_user_authorized_for_project(project_name, user_email):
            return JSONResponse(content={"error": "Geen toegang tot dit project"}, status_code=403)

        user_role = project_service.get_user_role_for_project(project_name, user_email)
        if user_role not in ["admin", "owner"]:
            return JSONResponse(
                content={"error": f"Alleen admin of owner rollen kunnen components verwijderen. Uw rol: {user_role}"},
                status_code=403,
            )

        project = project_service.get_project(project_name)
        if not project:
            return JSONResponse(content={"error": "Project niet gevonden"}, status_code=404)

        project_data = project.data or {}
        components = list(project_data.get("components", []))

        # Find and remove the component by name
        original_count = len(components)
        components = [c for c in components if c.get("name") != component_name]
        if len(components) == original_count:
            return JSONResponse(content={"error": f"Component '{component_name}' niet gevonden"}, status_code=404)

        project_data["components"] = components
        save_project_file(project.filename, project_data)
        project_service.load_project_from_data(project_data, project.filename)
        logger.info(f"Component '{component_name}' removed from '{project_name}', triggering reprocessing")

        # Trigger reprocessing via V2 async task
        from io import StringIO

        from ruamel.yaml import YAML

        from opi.core.task_helpers import create_async_task

        yaml_instance = YAML()
        yaml_instance.preserve_quotes = True
        yaml_instance.width = 4096
        yaml_output = StringIO()
        yaml_instance.dump(project_data, yaml_output)
        yaml_content = yaml_output.getvalue()

        await create_async_task(
            request=request,
            task_type="create_project",
            project_name=project_name,
            payload={"project_name": project_name, "yaml_content": yaml_content},
            max_attempts=1,
        )

        return JSONResponse(
            content={"success": True, "message": f"Component '{component_name}' succesvol verwijderd"},
            status_code=200,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing web component deletion: {e!s}")
        from fastapi.responses import JSONResponse

        return JSONResponse(content={"error": f"Fout bij verwijderen van component: {e!s}"}, status_code=500)


@web_router.post("/projects/{project_name}/refresh", response_class=HTMLResponse)
@requires_sso
async def refresh_project_web(request: Request, project_name: str) -> HTMLResponse:
    """Reprocess a project from Git via web interface."""
    from opi.services.project_service import get_project_service

    user = get_current_user(request)
    user_email = user.get("email", "").lower()

    logger.info(f"Web project refresh request for '{project_name}' by user: {user_email}")

    project_service = get_project_service()

    if not project_service.is_user_authorized_for_project(project_name, user_email):
        raise HTTPException(status_code=403, detail="Geen toegang tot dit project")

    user_role = project_service.get_user_role_for_project(project_name, user_email)
    if user_role not in ["admin", "owner"]:
        raise HTTPException(
            status_code=403,
            detail=f"Alleen admin of owner rollen kunnen een project herverwerken. Uw rol: {user_role}",
        )

    project = project_service.get_project(project_name)
    if not project:
        raise HTTPException(status_code=404, detail="Project niet gevonden")

    return await _create_task_and_render_progress(
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
    from opi.services.project_service import get_project_service

    user = get_current_user(request)
    user_email = user.get("email", "").lower()

    logger.info(f"Web deployment refresh request for '{project_name}/{deployment_name}' by user: {user_email}")

    project_service = get_project_service()

    if not project_service.is_user_authorized_for_project(project_name, user_email):
        raise HTTPException(status_code=403, detail="Geen toegang tot dit project")

    user_role = project_service.get_user_role_for_project(project_name, user_email)
    if user_role not in ["admin", "owner"]:
        raise HTTPException(
            status_code=403,
            detail=f"Alleen admin of owner rollen kunnen een deployment herverwerken. Uw rol: {user_role}",
        )

    project = project_service.get_project(project_name)
    if not project:
        raise HTTPException(status_code=404, detail="Project niet gevonden")

    return await _create_task_and_render_progress(
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


@web_router.get("/test-architecture", response_class=HTMLResponse)
@requires_sso
async def test_architecture(request: Request):
    """Test route for architecture components."""
    try:
        templates = get_templates()
        return templates.TemplateResponse("test-architecture.html.j2", {"request": request})
    except Exception as e:
        logger.error(f"Error serving test architecture: {e!s}")
        raise HTTPException(status_code=500, detail=f"Template error: {e!s}")


@web_router.get("/test-hero", response_class=HTMLResponse)
@requires_sso
async def test_hero(request: Request):
    """Test route for hero component."""
    try:
        templates = get_templates()
        return templates.TemplateResponse("test-hero.html.j2", {"request": request})
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
        templates = get_templates()
        return templates.TemplateResponse(
            "formulier-template.html.j2", {"request": request, "title": "Formulier Template - RVO Demo"}
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
        from datetime import UTC, datetime

        from opi.core.startup import ensure_projects_fresh
        from opi.services.project_service import get_project_service

        templates = get_templates()
        user = get_current_user(request)
        user_email = user.get("email", "").lower()

        # --- Load user's projects ---
        await ensure_projects_fresh()
        project_service = get_project_service()
        all_projects = project_service.get_all_projects()

        user_projects: list[dict] = []
        all_namespaces: list[str] = []
        total_deployments = 0
        unique_users: set[str] = set()

        for project_name, project in all_projects.items():
            if not project_service.is_user_authorized_for_project(project_name, user_email):
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
                    "users": users,
                    "deployment_count": len(deployments),
                    "user_count": len(users),
                    "namespaces": project_namespaces,
                }
            )

        user_projects.sort(key=lambda p: p["display_name"] or p["name"])

        # --- Query Prometheus metrics (scoped to user's namespaces) ---
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
                        result = await prom.custom_query(
                            f'sum(kubelet_volume_stats_used_bytes{{namespace=~"{ns_regex}"}})'
                        )
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
                    # Per-project CPU usage for resource comparison bar
                    for project in user_projects:
                        proj_ns = project.get("namespaces", [])
                        if not proj_ns:
                            project["cpu_cores"] = 0.0
                            continue
                        try:
                            proj_regex = "|".join(proj_ns)
                            result = await prom.custom_query(
                                f'sum(rate(container_cpu_usage_seconds_total{{namespace=~"{proj_regex}",container!=""}}[5m]))'
                            )
                            if result and result[0].get("value"):
                                project["cpu_cores"] = float(result[0]["value"][1])
                            else:
                                project["cpu_cores"] = 0.0
                        except Exception as e:
                            logger.debug(f"Dashboard per-project CPU query failed for {project['name']}: {e}")
                            project["cpu_cores"] = 0.0

            except Exception as e:
                logger.warning(f"Dashboard: failed to fetch Prometheus metrics: {e}")

        total_cpu_usage = sum(p.get("cpu_cores", 0) for p in user_projects)

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
                                health = status_data.get("status", {}).get("health", {}).get("status", "Unknown")
                                deployment_statuses.append(health)
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
                    if "Degraded" in deployment_statuses:
                        project["health"] = "Degraded"
                    elif "Progressing" in deployment_statuses:
                        project["health"] = "Progressing"
                    elif "Healthy" in deployment_statuses:
                        project["health"] = "Healthy"
                    else:
                        project["health"] = "Unknown"
                    project["last_deployed"] = latest_deploy
        except Exception as e:
            logger.warning(f"Dashboard: failed to connect to ArgoCD: {e}")
            for project in user_projects:
                project["health"] = "Unknown"

        # Compute health counts for summary banner
        health_counts = {"Healthy": 0, "Progressing": 0, "Degraded": 0, "Unknown": 0}
        for p in user_projects:
            health_counts[p.get("health", "Unknown")] += 1

        return templates.TemplateResponse(
            "dashboard.html.j2",
            {
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
                "total_cpu_usage": total_cpu_usage,
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


@web_router.get("/projects/details/{project_name}", response_class=HTMLResponse)
@requires_sso
async def project_details(request: Request, project_name: str):
    """
    Serve the project details page showing comprehensive project information.
    Shows detailed project data including services, components, deployments, and configuration.

    Args:
        request: The FastAPI request object
        project_name: The name of the project to display

    Returns:
        HTML response with detailed project information
    """
    try:
        from opi.core.startup import ensure_projects_fresh
        from opi.services.project_service import get_project_service
        from opi.services.services import ServiceAdapter

        templates = get_templates()
        user = get_current_user(request)

        # Generate CSRF token for form protection (domain settings modal)
        csrf_token = ensure_csrf_token(request)

        # TODO: this logic has to be centralized
        user_email = user.get("email", "").lower()

        # Ensure project data is fresh (refreshes from Git if stale)
        await ensure_projects_fresh()

        # Get project service to validate access
        project_service = get_project_service()

        # Check if user has access to this project
        if not project_service.is_user_authorized_for_project(project_name, user_email):
            logger.warning(f"User {user_email} not authorized to view project: {project_name}")
            raise HTTPException(status_code=403, detail="You are not authorized to view this project")

        # Get project details
        project = project_service.get_project(project_name)
        if not project:
            raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")

        # Get user's role for this project
        user_role = project_service.get_user_role_for_project(project_name, user_email)

        # Use project data from memory if available
        project_data = project.data or {}
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

        # Decrypt Keycloak passwords
        if project_data_decrypted["config"].get("keycloak"):
            for kc_config in project_data_decrypted["config"]["keycloak"]:
                if kc_config.get("password"):
                    try:
                        kc_config["password"] = await decrypt_password_smart(kc_config["password"], project_private_key)
                    except Exception as e:
                        logger.warning(f"Failed to decrypt Keycloak password for realm {kc_config.get('realm')}: {e}")
                        kc_config["password"] = None

        for deployment in project_data_decrypted.get("deployments", []):
            # Decrypt deployment-component-level user-env-vars
            for dep_component in deployment.get("components", []):
                if dep_component.get("user-env-vars"):
                    try:
                        decrypted_yaml = await decrypt_age_content(dep_component["user-env-vars"], project_private_key)
                        dep_component["user-env-vars"] = load_yaml_from_string(decrypted_yaml)
                    except Exception as e:
                        logger.warning(
                            f"Failed to decrypt deployment component user-env-vars for {dep_component.get('reference')}: {e}"
                        )
                        dep_component["user-env-vars"] = None

        logger.info(f"Processing {len(project_data_decrypted.get('components', []))} components for user-env-vars")
        for component in project_data_decrypted.get("components", []):
            component_name = component.get("name", "unknown")

            raw_user_env_vars = component.get("user-env-vars")
            logger.info(
                f"Component '{component_name}': has user-env-vars={raw_user_env_vars is not None}, type={type(raw_user_env_vars).__name__ if raw_user_env_vars else 'None'}"
            )
            if raw_user_env_vars:
                logger.info(f"Processing user-env-vars for component '{component_name}'")
                try:
                    decrypted_yaml = await decrypt_age_content(raw_user_env_vars, project_private_key)

                    # Try YAML parsing first
                    parsed_env_vars = load_yaml_from_string(decrypted_yaml)

                    # If result is a string (not a dict), try parsing as KEY=VALUE format
                    if isinstance(parsed_env_vars, str) or parsed_env_vars is None:
                        from opi.utils.env_vars import validate_and_parse_env_vars

                        logger.info(f"YAML returned {type(parsed_env_vars).__name__}, trying KEY=VALUE format")
                        parsed_env_vars = validate_and_parse_env_vars(decrypted_yaml)

                    logger.info(
                        f"Parsed user-env-vars for component '{component_name}': "
                        f"type={type(parsed_env_vars).__name__}, keys={list(parsed_env_vars.keys()) if isinstance(parsed_env_vars, dict) else 'N/A'}"
                    )
                    component["user-env-vars"] = parsed_env_vars
                except Exception as e:
                    logger.warning(f"Failed to decrypt component user-env-vars for '{component_name}': {e}")
                    component["user-env-vars"] = None
            else:
                logger.debug(f"No user-env-vars found for component '{component_name}'")

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
        }

        # Add ingress URLs for components that have publish-on-web service
        from opi.core.cluster_config import get_ingress_postfix, get_ingress_tls_enabled
        from opi.handlers.project_file_handler import ProjectFileHandler
        from opi.utils.naming import HostnameFormat, generate_public_url, get_component_ingress_map

        project_file_handler = ProjectFileHandler()

        # Add ingress links to deployments for components with publish-on-web
        for deployment in project_details["deployments"]:
            cluster = deployment.get("cluster")
            deployment["ingress_links"] = []

            if cluster:
                try:
                    ingress_postfix = get_ingress_postfix(cluster)
                    use_https = get_ingress_tls_enabled(cluster)
                    subdomain = deployment.get("subdomain")
                    base_domain = deployment.get("base-domain")
                    hostname_format = HostnameFormat.from_domain_mode(deployment.get("domain-mode"))
                    domain_format = deployment.get("domain-format")

                    for component in deployment.get("components", []):
                        component_name = component.get("reference")
                        if component_name:
                            # Check if component has publish-on-web service (same as project_manager)
                            has_publish_on_web = project_file_handler.extract_component_publish_on_web(
                                project_data, component_name
                            )

                            if has_publish_on_web:
                                ingress_map = get_component_ingress_map(
                                    component_name=component_name,
                                    deployment_name=deployment["name"],
                                    project_name=project_name,
                                    ingress_postfix=ingress_postfix,
                                    subdomain=subdomain,
                                    base_domain=base_domain,
                                    hostname_format=hostname_format,
                                    domain_format=domain_format,
                                    project_data=project_data,
                                    cluster=cluster,
                                )

                                # Create links for all ingress hostnames
                                for ingress_name, hostname in ingress_map.items():
                                    public_url = generate_public_url(hostname, use_https)
                                    deployment["ingress_links"].append(
                                        {
                                            "component_name": component_name,
                                            "ingress_name": ingress_name,
                                            "hostname": hostname,
                                            "url": public_url,
                                        }
                                    )
                except Exception as ingress_error:
                    logger.warning(
                        f"Failed to generate ingress links for deployment {deployment.get('name')}: {ingress_error}"
                    )

        # Add ingress links to components that have publish-on-web
        for component in project_details["components"]:
            component["ingress_links"] = []
            component_name = component.get("name")

            if component_name:
                # Check if component has publish-on-web service (same as project_manager)
                has_publish_on_web = project_file_handler.extract_component_publish_on_web(project_data, component_name)

                if has_publish_on_web:
                    # Find all deployments that use this component
                    for deployment in project_details["deployments"]:
                        cluster = deployment.get("cluster")
                        if cluster and any(
                            c.get("reference") == component_name for c in deployment.get("components", [])
                        ):
                            try:
                                ingress_postfix = get_ingress_postfix(cluster)
                                use_https = get_ingress_tls_enabled(cluster)
                                subdomain = deployment.get("subdomain")
                                base_domain = deployment.get("base-domain")
                                hostname_format = HostnameFormat.from_domain_mode(deployment.get("domain-mode"))
                                domain_format = deployment.get("domain-format")

                                ingress_map = get_component_ingress_map(
                                    component_name=component_name,
                                    deployment_name=deployment["name"],
                                    project_name=project_name,
                                    ingress_postfix=ingress_postfix,
                                    subdomain=subdomain,
                                    base_domain=base_domain,
                                    hostname_format=hostname_format,
                                    domain_format=domain_format,
                                    project_data=project_data,
                                    cluster=cluster,
                                )

                                # Create links for all ingress hostnames
                                for ingress_name, hostname in ingress_map.items():
                                    public_url = generate_public_url(hostname, use_https)
                                    component["ingress_links"].append(
                                        {
                                            "deployment_name": deployment["name"],
                                            "cluster": cluster,
                                            "ingress_name": ingress_name,
                                            "hostname": hostname,
                                            "url": public_url,
                                        }
                                    )
                            except Exception as ingress_error:
                                logger.warning(
                                    f"Failed to generate ingress link for component {component_name} in deployment {deployment['name']}: {ingress_error}"
                                )

        # Check Prometheus availability (metrics are lazy-loaded via HTMX)
        prometheus_available = False
        try:
            from opi.connectors.prometheus import get_metrics_connector

            prom = await get_metrics_connector()
            prometheus_available = prom.is_connected
        except Exception as e:
            logger.debug(f"Prometheus not available: {e}")

        # Fetch ArgoCD status for each deployment (in parallel)
        from datetime import datetime

        from opi.services.deployment_diagnostics import gather_deployment_errors
        from opi.services.event_interpreter import interpret_argocd_errors

        def _unavailable_result(app_name: str, message: str, source: str = "Application") -> dict[str, Any]:
            return {
                "app_name": app_name,
                "available": False,
                "health": "Unknown",
                "sync": "Unknown",
                "errors": [{"resource": source, "message": message}],
            }

        def _annotate_age_dutch(errors: list[dict[str, Any]]) -> None:
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
                    pass

        argocd_status: dict[str, dict[str, Any]] = {}
        argocd_available = False
        try:
            from opi.connectors.argo import ArgoConnector, create_argo_connector
            from opi.connectors.kubectl import create_kubectl_connector
            from opi.utils.naming import generate_argocd_application_name

            argo_connector = create_argo_connector()
            argocd_available = argo_connector.auth_token is not None

            if argocd_available:
                kubectl_connector = create_kubectl_connector()

                async def _fetch_deployment_status(
                    deployment: dict[str, Any],
                    argo: ArgoConnector,
                ) -> tuple[str, dict[str, Any]] | None:
                    """Fetch ArgoCD status for a single deployment, with errors when unhealthy."""
                    deployment_name = deployment.get("name")
                    if not deployment_name:
                        return None
                    app_name = generate_argocd_application_name(project_name, deployment_name)
                    try:
                        status_data = await argo.get_application_status(app_name)
                        if not status_data:
                            return deployment_name, _unavailable_result(app_name, "Application not found in ArgoCD")

                        status = status_data.get("status", {})
                        health = status.get("health", {})
                        sync = status.get("sync", {})
                        operation_state = status.get("operationState", {})
                        app_health = health.get("status", "Unknown")

                        raw_errors: list[dict[str, str]] = []
                        if app_health != "Healthy":
                            raw_errors = await gather_deployment_errors(
                                argo=argo,
                                kubectl=kubectl_connector,
                                app_name=app_name,
                                base_namespace=deployment.get("namespace", ""),
                                cluster=deployment.get("cluster", ""),
                                deployment_name=deployment_name,
                                status_data=status_data,
                            )

                        errors = interpret_argocd_errors(raw_errors, deployment_name=deployment_name)
                        _annotate_age_dutch(errors)

                        last_sync = operation_state.get("finishedAt")
                        if not last_sync and sync.get("status") == "Synced":
                            last_sync = status.get("reconciledAt")

                        return deployment_name, {
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
                        return deployment_name, _unavailable_result(app_name, str(app_error), source="API")

                results = await asyncio.gather(
                    *[_fetch_deployment_status(dep, argo_connector) for dep in project_details["deployments"]]
                )
                for result in results:
                    if result is not None:
                        argocd_status[result[0]] = result[1]

        except Exception as argo_error:
            logger.warning(f"Failed to connect to ArgoCD: {argo_error}")

        # Fetch backup snapshots for deployments on current cluster
        from opi.core.cluster_config import get_prefixed_namespace
        from opi.core.config import settings
        from opi.manager.backup import BackupManager

        current_cluster = settings.CLUSTER_MANAGER
        deployment_backups: dict[str, list[dict[str, Any]]] = {}
        backups_available = False

        try:
            backup_manager = BackupManager()
            backups_available = True

            # Query snapshots once per (cluster, namespace) to avoid redundant Kopia calls
            namespace_snapshots_cache: dict[tuple[str, str], list] = {}

            for deployment in project_details["deployments"]:
                deployment_name = deployment.get("name")
                base_namespace = deployment.get("namespace")
                cluster = deployment.get("cluster")

                # Only fetch backups for deployments on the current cluster
                if not deployment_name or not base_namespace or cluster != current_cluster:
                    continue

                # Get the actual Kubernetes namespace with cluster prefix
                k8s_namespace = get_prefixed_namespace(cluster, base_namespace)

                try:
                    cache_key = (cluster, k8s_namespace)
                    if cache_key not in namespace_snapshots_cache:
                        namespace_snapshots_cache[cache_key] = await backup_manager.list_snapshots(
                            cluster, k8s_namespace, project_name=project_name
                        )

                    all_snapshots = namespace_snapshots_cache[cache_key]

                    # Filter snapshots by deployment name
                    deployment_snapshots = [s for s in all_snapshots if s.deployment_name == deployment_name]

                    if deployment_snapshots:
                        deployment_backups[deployment_name] = [
                            {
                                "snapshot_id": s.snapshot_id,
                                "pvc_name": s.pvc_name,
                                "timestamp": s.timestamp,
                                "size_bytes": s.size_bytes,
                                # Extended metadata
                                "cluster": s.cluster,
                                "namespace": s.namespace,
                                "project_name": s.project_name,
                                "deployment_name": s.deployment_name,
                                "component_name": s.component_name,
                                "storage_name": s.storage_name,
                                "generation": s.generation,
                                "backup_run_id": s.backup_run_id,
                                "resource_type": s.resource_type,
                                "trigger": s.trigger,
                                # Raw tags for debugging
                                "tags": s.tags,
                            }
                            for s in deployment_snapshots
                        ]
                        logger.debug(f"Found {len(deployment_snapshots)} backups for deployment {deployment_name}")
                except Exception as backup_err:
                    logger.warning(f"Failed to fetch backups for deployment {deployment_name}: {backup_err}")

        except Exception as backup_init_error:
            logger.warning(f"Failed to initialize backup manager: {backup_init_error}")
            backups_available = False

        # Generate ingress URLs for components with publish-on-web service
        from opi.core.cluster_config import get_ingress_postfix, get_ingress_tls_enabled
        from opi.handlers.project_file_handler import ProjectFileHandler
        from opi.utils.naming import HostnameFormat, generate_public_url, get_component_ingress_map

        project_file_handler = ProjectFileHandler()

        # Add ingress information to deployments
        for deployment in project_details["deployments"]:
            cluster = deployment.get("cluster")
            deployment["ingress_links"] = []
            if cluster:
                try:
                    ingress_postfix = get_ingress_postfix(cluster)
                    use_https = get_ingress_tls_enabled(cluster)
                    subdomain = deployment.get("subdomain")
                    base_domain = deployment.get("base-domain")
                    hostname_format = HostnameFormat.from_domain_mode(deployment.get("domain-mode"))
                    domain_format = deployment.get("domain-format")

                    # Generate ingress links for each component in this deployment
                    for component in deployment.get("components", []):
                        component_name = component.get("reference")
                        if component_name:
                            has_publish_on_web = project_file_handler.extract_component_publish_on_web(
                                project_data, component_name
                            )

                            if has_publish_on_web:
                                ingress_map = get_component_ingress_map(
                                    component_name=component_name,
                                    deployment_name=deployment["name"],
                                    project_name=project_name,
                                    ingress_postfix=ingress_postfix,
                                    subdomain=subdomain,
                                    base_domain=base_domain,
                                    hostname_format=hostname_format,
                                    domain_format=domain_format,
                                    project_data=project_data,
                                    cluster=cluster,
                                )

                                for ingress_name, hostname in ingress_map.items():
                                    public_url = generate_public_url(hostname, use_https)
                                    deployment["ingress_links"].append(
                                        {
                                            "component_name": component_name,
                                            "ingress_name": ingress_name,
                                            "hostname": hostname,
                                            "url": public_url,
                                        }
                                    )
                except Exception as ingress_error:
                    logger.warning(
                        f"Failed to generate ingress links for deployment {deployment.get('name')}: {ingress_error}"
                    )
                    deployment["ingress_links"] = []

        # Also add ingress information directly to components for the components section
        for component in project_details["components"]:
            component["ingress_links"] = []
            component_name = component.get("name")
            if component_name:
                has_publish_on_web = project_file_handler.extract_component_publish_on_web(project_data, component_name)

                if has_publish_on_web:
                    # Find deployments that use this component
                    for deployment in project_details["deployments"]:
                        cluster = deployment.get("cluster")
                        if cluster and any(
                            c.get("reference") == component_name for c in deployment.get("components", [])
                        ):
                            try:
                                ingress_postfix = get_ingress_postfix(cluster)
                                use_https = get_ingress_tls_enabled(cluster)
                                subdomain = deployment.get("subdomain")
                                base_domain = deployment.get("base-domain")
                                hostname_format = HostnameFormat.from_domain_mode(deployment.get("domain-mode"))
                                domain_format = deployment.get("domain-format")

                                ingress_map = get_component_ingress_map(
                                    component_name=component_name,
                                    deployment_name=deployment["name"],
                                    project_name=project_name,
                                    ingress_postfix=ingress_postfix,
                                    subdomain=subdomain,
                                    base_domain=base_domain,
                                    hostname_format=hostname_format,
                                    domain_format=domain_format,
                                    project_data=project_data,
                                    cluster=cluster,
                                )

                                for ingress_name, hostname in ingress_map.items():
                                    public_url = generate_public_url(hostname, use_https)
                                    component["ingress_links"].append(
                                        {
                                            "deployment_name": deployment["name"],
                                            "cluster": cluster,
                                            "ingress_name": ingress_name,
                                            "hostname": hostname,
                                            "url": public_url,
                                        }
                                    )
                            except Exception as ingress_error:
                                logger.warning(
                                    f"Failed to generate ingress links for component {component_name} in deployment {deployment['name']}: {ingress_error}"
                                )

        # Get cluster base domains for domain settings modal
        from opi.web.router_self_service import get_cluster_base_domains_for_template

        cluster_base_domains = get_cluster_base_domains_for_template()

        from opi.forms.visualizers.flows import SERVICE_CONFIG_MODAL_FLOWS

        return templates.TemplateResponse(
            "project-details.html.j2",
            {
                "request": request,
                "title": f"Project Details - {project_details['display_name']}",
                "menu_items": get_menu_items(user),
                "project": project_details,
                "user": user,
                "user_role": user_role,
                "ServiceAdapter": ServiceAdapter,
                "prometheus_available": prometheus_available,
                "argocd_status": argocd_status,
                "argocd_available": argocd_available,
                "deployment_backups": deployment_backups,
                "backups_available": backups_available,
                "current_cluster": current_cluster,
                "cluster_base_domains": cluster_base_domains,
                "csrf_token": csrf_token,
                "service_config_sections": SERVICE_CONFIG_MODAL_FLOWS,
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


@web_router.get("/projects/details/{project_name}/metrics/{deployment_name}", response_class=HTMLResponse)
@requires_sso
async def deployment_metrics_fragment(
    request: Request, project_name: str, deployment_name: str, duration: int = 60
) -> HTMLResponse:
    """Return metrics HTML fragment for a single deployment (HTMX lazy-load)."""

    from opi.core.startup import ensure_projects_fresh
    from opi.services.project_service import get_project_service

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

    templates = get_templates()
    user = get_current_user(request)
    user_email = user.get("email", "").lower()

    await ensure_projects_fresh()

    project_service = get_project_service()
    if not project_service.is_user_authorized_for_project(project_name, user_email):
        raise HTTPException(status_code=403, detail="Not authorized")

    project = project_service.get_project(project_name)
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

    if base_namespace and cluster:
        try:
            from opi.connectors.prometheus import get_metrics_connector
            from opi.core.cluster_config import get_prefixed_namespace

            prom = await get_metrics_connector()
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
            logging.getLogger(__name__).warning(f"Failed to fetch Prometheus metrics: {metrics_error}")

    # Build a deployment-like object for the template (needs .name and .components attributes)
    class DeploymentContext:
        def __init__(self, name: str, components: list):
            self.name = name
            self.components = [type("C", (), {"reference": c.get("reference")})() for c in components]

    deployment_ctx = DeploymentContext(deployment_name, components)

    return templates.TemplateResponse(
        "partials/deployment_metrics.html.j2",
        {
            "request": request,
            "project_name": project_name,
            "deployment": deployment_ctx,
            "metrics": metrics,
            "discovered_workloads": discovered_workloads,
            "pvc_storage": pvc_storage,
            "duration": duration,
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
    from fastapi.responses import JSONResponse

    from opi.api.router import DeploymentDomainSettingsResponse
    from opi.services.project_service import get_project_service
    from opi.web.router_self_service import get_cluster_base_domains_for_template

    try:
        user = get_current_user(request)
        user_email = user.get("email", "").lower()

        # Get project service to validate access
        project_service = get_project_service()

        # Check if user has access to this project
        if not project_service.is_user_authorized_for_project(project_name, user_email):
            logger.warning(f"User {user_email} not authorized to access project: {project_name}")
            raise HTTPException(status_code=403, detail="You are not authorized to access this project")

        # Get project details
        project = project_service.get_project(project_name)
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
        domain_mode = deployment.get("domain-mode")
        subdomain = deployment.get("subdomain")
        base_domain = deployment.get("base-domain")

        # Find root component (if any)
        root_component = deployment.get("root-component")
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


async def _validate_csrf(request: Request, form_data: dict | None = None) -> None:
    """
    Validate CSRF protection (double-submit token + Origin/Referer).

    CSRF enforcement is now central in CSRFMiddleware, so by the time a
    handler runs the request has already been validated. This helper is kept
    for the explicit call site and is idempotent: it re-runs the same checks
    against the same token, which always passes for a legitimately validated
    request.

    Args:
        request: The FastAPI request object
        form_data: Optional pre-parsed form data (to avoid parsing twice)

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
            domain_format=deployment.get("domain-format"),
            project_data=project_data,
            cluster=cluster,
        )

        if not all_ingress_hosts:
            logger.warning(f"No ingress hosts generated for deployment {deployment_name}, skipping Keycloak update")
            return

        logger.info(f"New hostnames for Keycloak client: {all_ingress_hosts}")

        # Get HTTP support setting and additional redirect URIs from config
        support_http = get_keycloak_support_http(cluster)

        # Get additional redirect URIs from project keycloak service config
        additional_redirect_uris = None
        project_services = project_data.get("services", [])
        for service_item in project_services:
            if isinstance(service_item, dict) and "keycloak" in service_item:
                service_data = service_item["keycloak"]
                if isinstance(service_data, dict) and "config" in service_data:
                    additional_redirect_uris = service_data["config"].get("additional_redirect_uris")
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
    from fastapi.responses import JSONResponse

    from opi.connectors.git import GitConnector
    from opi.connectors.subdomain import (
        create_subdomain_connector,
        validate_base_domain,
        validate_subdomain,
    )
    from opi.core.config import settings
    from opi.manager.project_manager import create_project_manager
    from opi.services.project_service import get_project_service

    # Track state for rollback
    original_yaml_content: str | None = None
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
        project_service = get_project_service()

        # Check if user has access to this project
        if not project_service.is_user_authorized_for_project(project_name, user_email):
            logger.warning(f"User {user_email} not authorized to access project: {project_name}")
            raise HTTPException(status_code=403, detail="You are not authorized to access this project")

        # Check if user has admin or owner role
        user_role = project_service.get_user_role_for_project(project_name, user_email)
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
        project = project_service.get_project(project_name)
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

        # Validate nice-url settings if nice-url mode is selected
        if domain_mode == "nice-url":
            if not subdomain:
                raise HTTPException(status_code=400, detail="Subdomain is required for nice-url mode")
            if not base_domain:
                raise HTTPException(status_code=400, detail="Base domain is required for nice-url mode")

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

        # Create Git connector for projects repository
        git_connector = GitConnector(
            repo_url=settings.GIT_PROJECTS_SERVER_URL,
            username=settings.GIT_PROJECTS_SERVER_USERNAME,
            password=settings.GIT_PROJECTS_SERVER_PASSWORD,
            branch=settings.GIT_PROJECTS_SERVER_BRANCH,
            repo_path=settings.GIT_PROJECTS_SERVER_REPO_PATH,
        )

        # Load current YAML content (store for potential rollback)
        project_file_path = f"projects/{project.filename}"
        original_yaml_content = await git_connector.read_file(project_file_path)

        # Update YAML using ruamel.yaml to preserve structure
        from io import StringIO

        from ruamel.yaml import YAML

        yaml = YAML()
        yaml.preserve_quotes = True
        project_yaml = yaml.load(StringIO(original_yaml_content))

        # Find and update deployment
        yaml_deployments = project_yaml.get("deployments", [])
        for yaml_dep in yaml_deployments:
            if yaml_dep.get("name") == deployment_name:
                # Update domain settings
                yaml_dep["domain-mode"] = domain_mode

                # Handle subdomain and base-domain based on mode
                if domain_mode == "nice-url":
                    yaml_dep["subdomain"] = subdomain
                    yaml_dep["base-domain"] = base_domain
                    # Auto-enable Let's Encrypt for nice-url mode (HTTPS by default)
                    yaml_dep["issuer"] = "letsencrypt"
                elif domain_mode == "custom":
                    yaml_dep["subdomain"] = subdomain
                    # Remove base-domain and issuer for custom mode
                    if "base-domain" in yaml_dep:
                        del yaml_dep["base-domain"]
                    if "issuer" in yaml_dep:
                        del yaml_dep["issuer"]
                else:
                    # Remove subdomain, base-domain, and issuer for other modes
                    if "subdomain" in yaml_dep:
                        del yaml_dep["subdomain"]
                    if "base-domain" in yaml_dep:
                        del yaml_dep["base-domain"]
                    if "issuer" in yaml_dep:
                        del yaml_dep["issuer"]

                # Handle root component — set on deployment level
                if root_component:
                    yaml_dep["root-component"] = root_component
                elif "root-component" in yaml_dep:
                    del yaml_dep["root-component"]

                # Clean up any legacy root flags on components
                for comp in yaml_dep.get("components", []):
                    if "root" in comp:
                        del comp["root"]

                break

        # Write updated YAML to string
        stream = StringIO()
        yaml.dump(project_yaml, stream)
        updated_yaml = stream.getvalue()

        # === STEP 1: Save to git ===
        # Sanitize commit message by removing potentially dangerous characters
        safe_deployment_name = "".join(c for c in deployment_name if c.isalnum() or c in "-_")
        safe_project_name = "".join(c for c in project_name if c.isalnum() or c in "-_")

        await git_connector.create_or_update_file(
            project_file_path,
            updated_yaml,
            do_commit_and_push=True,
            commit_message=f"Update domain settings for deployment '{safe_deployment_name}' in project '{safe_project_name}'",
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
                    subdomain=subdomain,
                    base_domain=base_domain,
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
            if git_committed and original_yaml_content:
                try:
                    await git_connector.create_or_update_file(
                        project_file_path,
                        original_yaml_content,
                        do_commit_and_push=True,
                        commit_message=f"Rollback domain settings for '{safe_deployment_name}' (subdomain registration failed)",
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
        project_manager = create_project_manager()
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
            if git_committed and original_yaml_content:
                try:
                    await git_connector.create_or_update_file(
                        project_file_path,
                        original_yaml_content,
                        do_commit_and_push=True,
                        commit_message=f"Rollback domain settings for '{safe_deployment_name}' (processing failed)",
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
                "redirect_url": f"/projects/details/{project_name}",
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
        from opi.core.startup import ensure_projects_fresh
        from opi.services.project_service import get_project_service

        templates = get_templates()
        user = get_current_user(request)
        user_email = user.get("email", "").lower()

        # Ensure project data is fresh (refreshes from Git if stale)
        await ensure_projects_fresh()

        # Get project service to filter by user access
        project_service = get_project_service()

        # Get all projects and filter by user access
        user_projects = []
        all_projects = project_service.get_all_projects()

        for project_name, project in all_projects.items():
            # Check if user has access to this project
            if project_service.is_user_authorized_for_project(project_name, user_email):
                try:
                    # Get user's role for this project
                    user_role = project_service.get_user_role_for_project(project_name, user_email)

                    # Use project data from memory if available
                    project_data = project.data or {}

                    # Get description, filtering out the generic fallback text
                    description = project_data.get("description", "")

                    user_projects.append(
                        {
                            "name": project_name,
                            "display_name": project_data.get("display-name", project_name),
                            "description": description,
                            "users": project.users or [],
                            "user_role": user_role,
                            "services": project_data.get("services", []),
                            "clusters": project_data.get("clusters", []),
                            "components": project_data.get("components", []),
                            "deployments": project_data.get("deployments", []),
                        }
                    )
                except Exception as e:
                    logger.warning(f"Failed to load project data for {project_name}: {e}")
                    continue

        # Sort projects by name
        user_projects.sort(key=lambda p: p["display_name"] or p["name"])

        return templates.TemplateResponse(
            "projects-overview.html.j2",
            {"request": request, "menu_items": get_menu_items(user), "projects": user_projects, "user": user},
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


@web_router.get("/about", response_class=HTMLResponse)
async def about_platform(request: Request):
    """Serve the 'About the platform' page."""
    try:
        templates = get_templates()
        user = get_current_user(request)
        return templates.TemplateResponse("about.html.j2", {"request": request, "menu_items": get_menu_items(user)})
    except Exception as e:
        logger.error(f"Error serving about page: {e!s}")
        raise HTTPException(status_code=500, detail=str(e))


@web_router.get("/architecture", response_class=HTMLResponse)
async def architecture_overview(request: Request):
    """
    Serve the architecture overview page with C4 models and visual diagrams.

    Returns:
        HTML response with comprehensive platform architecture documentation
    """
    try:
        templates = get_templates()
        user = get_current_user(request)
        return templates.TemplateResponse(
            "architecture-overview.html.j2", {"request": request, "menu_items": get_menu_items(user)}
        )

    except Exception as e:
        import traceback

        error_details = traceback.format_exc()
        logger.error(f"Error serving architecture overview: {e!s}\n{error_details}")

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


@web_router.get("/test-template-variables", response_class=HTMLResponse)
@requires_sso
async def test_template_variables(request: Request):
    """Test route for debugging Jinja variables in ROOS components."""
    try:
        templates = get_templates()
        user = get_current_user(request)
        return templates.TemplateResponse(
            "test-template-variables.html.j2", {"request": request, "menu_items": get_menu_items(user)}
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
        templates = get_templates()
        user = get_current_user(request)
        return templates.TemplateResponse(
            "example.html.j2", {"request": request, "title": "Example Page", "menu_items": get_menu_items(user)}
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
        templates = get_templates()
        user = get_current_user(request)
        return templates.TemplateResponse(
            "tools.html.j2", {"request": request, "title": "AGE Encryption Tools", "menu_items": get_menu_items(user)}
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


@web_router.get("/projects/details/{project_name}/memory-check/{deployment_name}", response_class=HTMLResponse)
@requires_sso
async def deployment_memory_check(
    request: Request, project_name: str, deployment_name: str, id_prefix: str | None = None
) -> HTMLResponse:
    """Return a memory overprovision warning fragment (HTMX lazy-load)."""
    from opi.core.startup import ensure_projects_fresh
    from opi.services.project_service import get_project_service
    from opi.services.resource_tuning_service import check_deployment_resources

    templates = get_templates()
    user = get_current_user(request)
    user_email = user.get("email", "").lower()

    await ensure_projects_fresh()

    project_service = get_project_service()
    if not project_service.is_user_authorized_for_project(project_name, user_email):
        return HTMLResponse("")

    try:
        warnings = await check_deployment_resources(project_name, deployment_name)
    except Exception as e:
        logger.debug(f"Memory check failed for {project_name}/{deployment_name}: {e}")
        warnings = []

    # Supplement with kubectl-based OOM detection: Prometheus may report
    # reason="Error" instead of "OOMKilled" for exit-code-137 kills,
    # so kubectl (which checks both reason and exit code) is more reliable.
    try:
        from opi.core.cluster_config import get_prefixed_namespace
        from opi.services.oom_watcher import check_pod_health
        from opi.services.resource_tuning_service import MemoryCheckResult, get_project_data
        from opi.utils.naming import generate_unique_name

        project_data, _ = get_project_data(project_name)
        oom_warned_components = {w.component for w in warnings if w.oom_detected}

        for dep in project_data.get("deployments", []):
            if dep.get("name") != deployment_name:
                continue
            base_ns = dep.get("namespace", "")
            cluster = dep.get("cluster", "")
            if not base_ns or not cluster:
                continue
            namespace = get_prefixed_namespace(cluster, base_ns)

            for comp in dep.get("components", []):
                comp_ref = comp.get("reference", "")
                if not comp_ref or comp_ref in oom_warned_components:
                    continue
                unique_name = generate_unique_name(deployment_name, comp_ref)
                health = await check_pod_health(namespace, unique_name)
                if health.oom_detected:
                    # Extract current limit from project data for display
                    from opi.handlers.project_file_handler import ProjectFileHandler

                    fh = ProjectFileHandler()
                    res = fh.extract_component_resources(project_data, comp_ref)
                    dep_res = fh.extract_deployment_component_resources(project_data, deployment_name, comp_ref)
                    if dep_res:
                        res.update(dep_res)
                    current_limit = res.get("limits_memory", "?")
                    warnings.append(
                        MemoryCheckResult(
                            component=comp_ref,
                            current_limit=current_limit,
                            recommended_limit="(tune to auto-detect)",
                            saving_mb=0,
                            oom_detected=True,
                        )
                    )
    except Exception as e:
        logger.debug(f"kubectl OOM check failed for {project_name}/{deployment_name}: {e}")

    container_id = f"memory-check-{id_prefix or deployment_name}"

    return templates.TemplateResponse(
        "project-details/_memory-check.html.j2",
        {
            "request": request,
            "memory_warnings": warnings,
            "project_name": project_name,
            "deployment_name": deployment_name,
            "container_id": container_id,
        },
    )


async def _create_task_and_render_progress(
    request: Request,
    project_name: str,
    task_type: str,
    payload: dict,
    current_step: str,
    success_message: str,
    deployment_name: str | None = None,
) -> HTMLResponse:
    """Create a V2 async task and return a rendered progress fragment.

    Shared helper for all web UI endpoints that trigger async processing.
    Creates the task via the V2 task service and returns an HTML fragment
    with HTMX polling for progress updates.
    """
    from opi.core.task_helpers import create_async_task

    templates = get_templates()

    task = await create_async_task(
        request=request,
        task_type=task_type,
        project_name=project_name,
        deployment_name=deployment_name,
        payload=payload,
        max_attempts=1,
    )
    task_id = str(task["task_id"])

    context = {
        "task_id": task_id,
        "progress_url": f"/projects/{project_name}/task-progress/{task_id}",
        "progress": 0,
        "current_step": current_step,
        "tasks": [],
        "status": "running",
        "success_message": success_message,
        "on_complete": "location.reload()",
    }

    rendered = templates.get_template("partials/task_progress_fragment.html.j2").render(context)
    process_components = templates.env.filters.get("process_components")
    if process_components:
        rendered = str(process_components(rendered))

    return HTMLResponse(content=rendered)


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
        "current_step": task.get("current_step") or "Verwerking gestart...",
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
        entry = {"name": st.get("name", ""), "status": st.get("status", "pending")}
        parent = st.get("parent_id")
        if parent:
            children.setdefault(parent, []).append(entry)
        else:
            task_entry = {**entry, "id": st.get("id", ""), "subtasks": []}
            main_tasks.append(task_entry)
    for mt in main_tasks:
        mt["subtasks"] = children.get(mt["id"], [])
    return main_tasks


@web_router.get("/projects/{project_name}/task-progress/{task_id}", response_class=HTMLResponse)
@requires_sso
async def task_progress_fragment(request: Request, project_name: str, task_id: str) -> HTMLResponse:
    """Generic task progress fragment for HTMX polling.

    Reads task state from the V2 async task service (database-backed)
    and renders an HTML fragment for HTMX polling.
    """
    from opi.core.task_helpers import get_task_service

    templates = get_templates()
    task_service = get_task_service(request)
    task = await task_service.get_task(task_id)

    if task is None:
        return HTMLResponse(content="<p>Taak niet gevonden</p>", status_code=404)

    context = _v2_task_to_template_context(task, project_name)
    context["task_id"] = task_id
    context["progress_url"] = f"/projects/{project_name}/task-progress/{task_id}"
    context["on_complete"] = "location.reload()"

    rendered = templates.get_template("partials/task_progress_fragment.html.j2").render(context)
    process_components = templates.env.filters.get("process_components")
    if process_components:
        rendered = str(process_components(rendered))

    return HTMLResponse(content=rendered)


@web_router.get("/projects/{project_name}/task-errors/{task_id}", response_class=HTMLResponse)
@requires_sso
async def task_errors_fragment(request: Request, project_name: str, task_id: str) -> HTMLResponse:
    """Render just the component failure alerts for a task.

    Used by the full progress page to load error details via HTMX
    without duplicating the failure rendering logic in JavaScript.
    """
    from opi.core.task_helpers import get_task_service

    templates = get_templates()
    task_service = get_task_service(request)
    task = await task_service.get_task(task_id)

    if task is None:
        return HTMLResponse(content="<p>Taak niet gevonden</p>", status_code=404)

    context = _v2_task_to_template_context(task, project_name)

    rendered = templates.get_template("partials/_component_failures.html.j2").render(context)
    process_components = templates.env.filters.get("process_components")
    if process_components:
        rendered = str(process_components(rendered))

    return HTMLResponse(content=rendered)


@web_router.post("/projects/{project_name}/tune/{deployment_name}", response_class=HTMLResponse)
@requires_sso
async def tune_deployment(request: Request, project_name: str, deployment_name: str) -> HTMLResponse:
    """Start resource tuning as a background task and return progress fragment."""
    from opi.core.startup import ensure_projects_fresh
    from opi.core.task_manager import create_task
    from opi.services.project_service import get_project_service

    templates = get_templates()
    user = get_current_user(request)
    user_email = user.get("email", "").lower()

    await ensure_projects_fresh()

    project_service = get_project_service()
    if not project_service.is_user_authorized_for_project(project_name, user_email):
        raise HTTPException(status_code=403, detail="Not authorized")

    task_id = create_task(project_name)

    from opi.core.task_manager import TaskProgressManager

    progress = TaskProgressManager(task_id, project_name)
    metrics_task = progress.add_task("Prometheus metrics ophalen")

    async def _run_tune() -> None:
        from opi.services.resource_tuning_service import (
            commit_project_yaml,
            get_project_data,
            trigger_reprocessing,
        )

        try:
            # Step 1: Query Prometheus and compute recommendations
            from datetime import UTC, datetime

            from opi.core.cluster_config import get_prefixed_namespace
            from opi.handlers.project_file_handler import ProjectFileHandler
            from opi.services.resource_tuning_service import (
                _analyze_component_resources,
                get_metrics_connector,
            )

            project_data, filename = get_project_data(project_name)
            file_handler = ProjectFileHandler()

            try:
                connector = await get_metrics_connector()
            except Exception as e:
                raise RuntimeError(f"Metrics backend unavailable: {e}") from e

            progress.complete_task(metrics_task)

            # Step 2: Analyze and apply changes
            analyze_task = progress.add_task("Aanbevelingen berekenen")
            changes: list[dict[str, str]] = []
            unchanged: list[str] = []

            deployments = project_data.get("deployments", [])
            for dep in deployments:
                dep_name = dep.get("name", "")
                if deployment_name and dep_name != deployment_name:
                    continue
                base_namespace = dep.get("namespace")
                cluster = dep.get("cluster")
                if not base_namespace or not cluster:
                    continue
                namespace = get_prefixed_namespace(cluster, base_namespace)

                for comp in dep.get("components", []):
                    component_ref = comp.get("reference", "")
                    if not component_ref:
                        continue
                    analysis = await _analyze_component_resources(
                        connector, file_handler, project_data, dep_name, component_ref, namespace, cluster
                    )
                    if analysis is None:
                        unchanged.append(component_ref)
                        continue

                    file_handler.set_deployment_component_resources(
                        project_data,
                        dep_name,
                        component_ref,
                        {"limits_memory": analysis.new_limit, "requests_memory": analysis.new_request},
                    )
                    base_resources = file_handler.extract_component_resources(project_data, component_ref)
                    base_updates: dict[str, str] = {}
                    if analysis.new_request != base_resources["requests_memory"]:
                        base_updates["requests_memory"] = analysis.new_request
                    if analysis.new_limit != base_resources["limits_memory"]:
                        base_updates["limits_memory"] = analysis.new_limit
                    if base_updates:
                        file_handler.set_component_resources(project_data, component_ref, base_updates)

                    source = "oom-watcher" if analysis.has_oom_kills else "auto-tune"
                    now = datetime.now(UTC).isoformat()
                    file_handler.append_deployment_component_resource_history(
                        project_data,
                        dep_name,
                        component_ref,
                        {
                            "timestamp": now,
                            "limits": {"memory": analysis.new_limit},
                            "source": source,
                            "reason": analysis.reason,
                        },
                    )
                    file_handler.append_component_resource_history(
                        project_data,
                        component_ref,
                        {
                            "timestamp": now,
                            "limits": {"memory": analysis.new_limit},
                            "source": source,
                            "deployment": dep_name,
                            "reason": analysis.reason,
                        },
                    )

                    changes.append(
                        {
                            "component": component_ref,
                            "deployment": dep_name,
                            "previous_limits_memory": analysis.current_resources["limits_memory"],
                            "new_limits_memory": analysis.new_limit,
                            "previous_requests_memory": analysis.current_resources["requests_memory"],
                            "new_requests_memory": analysis.new_request,
                            "max_observed_memory_mb": f"{analysis.max_observed_mb:.0f}",
                            "avg_observed_memory_mb": f"{analysis.avg_observed_mb:.0f}",
                            "has_oom_kills": str(analysis.has_oom_kills),
                            "reason": analysis.reason,
                        }
                    )

            progress.complete_task(analyze_task)

            if changes:
                for c in changes:
                    change_task = progress.add_task(
                        f"{c['component']} ({c['deployment']}): "
                        f"{c['previous_limits_memory']} \u2192 {c['new_limits_memory']}"
                    )
                    progress.complete_task(change_task)

                # Step 3: Commit
                commit_task = progress.add_task("Wijzigingen opslaan")
                component_names = [c["component"] for c in changes]
                commit_msg = f"auto-tune: adjust memory resources for {', '.join(component_names)} in {project_name}"
                await commit_project_yaml(project_name, filename, project_data, commit_msg)
                progress.complete_task(commit_task)

                # Step 4: Reprocess
                reprocess_task = progress.add_task("Project opnieuw deployen")
                await trigger_reprocessing(project_name, filename, deployment_name, argocd_resources_changed=False)
                progress.complete_task(reprocess_task)

                progress.update_current_step(f"{len(changes)} component(s) aangepast")
            else:
                no_change = progress.add_task("Geen aanpassingen nodig")
                progress.complete_task(no_change)
                progress.update_current_step("Resources zijn al optimaal")

            progress.complete_project()
        except Exception as e:
            logger.error(f"Tune task failed for {project_name}/{deployment_name}: {e}")
            progress.fail_project(str(e))

    task = asyncio.create_task(_run_tune(), name=f"tune-{project_name}-{deployment_name}")
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    context = {
        "task_id": task_id,
        "progress_url": f"/projects/{project_name}/task-progress/{task_id}",
        "progress": 0,
        "current_step": "Resource tuning gestart...",
        "tasks": [],
        "status": "running",
        "success_message": "Resources succesvol aangepast!",
        "on_complete": "location.reload()",
    }

    rendered = templates.get_template("partials/task_progress_fragment.html.j2").render(context)
    process_components = templates.env.filters.get("process_components")
    if process_components:
        rendered = str(process_components(rendered))

    return HTMLResponse(content=rendered)
