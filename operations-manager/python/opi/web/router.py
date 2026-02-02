"""
Web routes for serving HTML pages (non-API endpoints).
"""

import copy
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

if TYPE_CHECKING:
    from opi.manager.project_manager import ProjectManager

from opi.api.router import SelfServiceComponent, SelfServiceProjectRequest
from opi.core.auth_decorators import get_current_user, requires_sso
from opi.core.task_manager import create_task
from opi.core.templates import get_templates
from opi.utils.age import decrypt_password_smart, get_global_private_key
from opi.utils.csrf import ensure_csrf_token
from opi.utils.project_names import generate_project_name
from opi.utils.yaml_util import load_yaml_from_string
from opi.web.menu import get_menu_items

from ..utils.age import decrypt_age_content
from .router_self_service import check_subdomain_availability_web, self_service_portal
from .services_router import services_router

logger = logging.getLogger(__name__)

web_router = APIRouter()

# Include the services router
web_router.include_router(services_router)


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


# Register the self-service portal route
web_router.add_api_route("/projects/new", self_service_portal, methods=["GET"], response_class=HTMLResponse)

# SSO-protected subdomain availability check (prevents unauthenticated enumeration)
web_router.add_api_route("/subdomains/check", check_subdomain_availability_web, methods=["GET"])


@web_router.post("/projects/new", response_class=HTMLResponse)
@requires_sso
async def process_self_service_form(request: Request, background_tasks: BackgroundTasks):
    """
    Process the self-service project creation form submission.

    This endpoint handles the form data from /projects/new and creates the project.
    It requires SSO authentication and processes the comprehensive form data.

    Returns:
        HTML response with creation results or error page
    """
    try:
        # Parse form data first (needed for CSRF validation)
        form_data = await request.form()
        logger.debug(f"Received form data keys: {list(form_data.keys())}")

        # === CSRF PROTECTION (token + origin/referer validation) ===
        await _validate_csrf(request, form_data)

        # Get current user for logging
        user = get_current_user(request)
        logger.info(f"Processing self-service form submission by user: {user.get('email', 'unknown')}")

        # Extract project details
        display_name = str(form_data.get("display-name", "")).strip()
        project_description = str(form_data.get("project-description", "")).strip()
        cluster = str(form_data.get("cluster", "")).strip()

        # Extract web address configuration
        domain_mode = str(form_data.get("domain-mode", "component-specific")).strip()
        subdomain = str(form_data.get("subdomain", "")).strip() or None
        deployment_name = str(form_data.get("deployment-name", "main")).strip() or "main"
        # Note: For nice-url mode, subdomain is now globally unique and required

        # Extract external domain configuration
        base_domain = str(form_data.get("base-domain", "")).strip() or None
        issuer = str(form_data.get("issuer", "")).strip() or None
        contact_email = str(form_data.get("contact-email", "")).strip() or None

        # Auto-enable Let's Encrypt for nice-url mode (HTTPS by default)
        if domain_mode == "nice-url" and base_domain and not issuer:
            issuer = "letsencrypt"
            logger.info(f"Auto-enabled Let's Encrypt issuer for nice-url mode with base domain '{base_domain}'")

        if not display_name or not cluster:
            raise HTTPException(status_code=400, detail="Project name and cluster are required")

        # Generate compliant technical project name from display name
        try:
            project_name, validated_display_name = generate_project_name(display_name)
            logger.info(f"Generated project name '{project_name}' from display name '{display_name}'")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid project name: {e}")

        # Extract users (arrays)
        user_emails = form_data.getlist("user-email[]")
        user_roles = form_data.getlist("user-role[]")

        # Filter out empty entries
        user_emails = [str(email).strip() for email in user_emails if str(email).strip()]
        user_roles = [str(role).strip() for role in user_roles if str(role).strip()]

        # Extract services (checkboxes)
        services = form_data.getlist("services[]")

        # Extract components - this is more complex as it's dynamic
        components = []
        component_index = 0
        while True:
            # Check if we have component data for this index
            comp_type_key = f"components[{component_index}][type]"
            if comp_type_key not in form_data:
                break

            comp_type = str(form_data.get(comp_type_key, "deployment")).strip()
            comp_port = form_data.get(f"components[{component_index}][port]")
            comp_image = str(form_data.get(f"components[{component_index}][image]", "")).strip()
            comp_path = str(form_data.get(f"components[{component_index}][path]", "/")).strip() or "/"
            comp_cpu = str(form_data.get(f"components[{component_index}][cpu_limit]", "")).strip()
            comp_memory = str(form_data.get(f"components[{component_index}][memory_limit]", "")).strip()
            comp_env_vars = str(form_data.get(f"components[{component_index}][env_vars]", "")).strip()
            comp_aliases = str(form_data.get(f"components[{component_index}][aliases]", "")).strip()
            comp_services = form_data.getlist(f"components[{component_index}][services][]")
            comp_root = str(form_data.get(f"components[{component_index}][root]", "")).strip().lower() == "true"

            # Parse port as integer
            try:
                port = int(str(comp_port)) if comp_port and str(comp_port).strip() else None
            except ValueError:
                port = None

            component = SelfServiceComponent(
                type=comp_type,
                port=port,
                image=comp_image or "nginx:latest",
                path=comp_path,
                cpu_limit=comp_cpu or None,
                memory_limit=comp_memory or None,
                env_vars=comp_env_vars or None,
                aliases=comp_aliases or None,
                services=comp_services or None,
                root=comp_root,
            )
            components.append(component)
            component_index += 1

        # Validate paths when using shared domains
        if domain_mode in ["deployment-name", "custom"] and components:
            component_paths = [comp.path for comp in components]
            # Check for duplicate paths
            seen_paths = set()
            duplicate_paths = []
            for path in component_paths:
                if path in seen_paths:
                    duplicate_paths.append(path)
                seen_paths.add(path)

            if duplicate_paths:
                raise HTTPException(
                    status_code=400,
                    detail=f"When using shared domains (domain mode: {domain_mode}), all component paths must be unique. "
                    f"Duplicate paths found: {', '.join(duplicate_paths)}. "
                    f"Please assign different paths to each component (e.g., /, /api, /admin).",
                )

        # Validate root component for nice-url mode
        if domain_mode == "nice-url" and components:
            root_components = [i for i, comp in enumerate(components) if comp.root]
            if len(root_components) > 1:
                raise HTTPException(
                    status_code=400,
                    detail="In nice-url mode, only one component can be marked as root. "
                    "Please select only one component to receive the root path.",
                )
            # Validate that root component has a port (needed for publish-on-web)
            if root_components:
                root_idx = root_components[0]
                root_comp = components[root_idx]
                if root_comp.port is None:
                    raise HTTPException(
                        status_code=400,
                        detail="Component marked as root must have a port specified for web publishing.",
                    )

        # Create the request object
        project_data = SelfServiceProjectRequest(
            project_name=project_name,
            display_name=display_name,
            project_description=project_description or None,
            cluster=cluster,
            deployment_name=deployment_name,
            domain_mode=domain_mode,
            subdomain=subdomain,
            base_domain=base_domain,
            issuer=issuer,
            contact_email=contact_email,
            user_email=user_emails or None,
            user_role=user_roles or None,
            services=services or None,
            components=components or None,
        )

        logger.info(
            f"Starting async processing for project: '{project_name}' (display: '{display_name}') with {len(components)} components"
        )

        # Create task and start background processing (use display name for user-facing messages)
        task_id = create_task(display_name)

        # Start the background task with simple processor
        from opi.core.simple_background import process_project_background

        background_tasks.add_task(process_project_background, task_id, project_data)

        logger.info(f"Started background task {task_id} for project {project_name}")

        # Redirect immediately to progress page
        return RedirectResponse(url=f"/projects/progress/{task_id}", status_code=302)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting background task for self-service form: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error starting project creation: {e!s}")


@web_router.get("/projects/progress/{task_id}", response_class=HTMLResponse)
@requires_sso
async def project_progress_page(request: Request, task_id: str):
    """
    Show the project creation progress page.

    This page displays real-time progress of the background task
    and automatically redirects when complete.
    """
    try:
        from opi.core.task_manager import get_task

        # Get project info
        project = get_task(task_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        user = get_current_user(request)
        templates = get_templates()

        return templates.TemplateResponse(
            "project-progress.html.j2",
            {
                "request": request,
                "title": f"Creating Project: {project.project_name}",
                "menu_items": get_menu_items(user),
                "task_id": task_id,
                "project_name": project.project_name,
                "initial_progress": 0,  # We'll show progress via tasks now
                "initial_step": project.current_step,
                "initial_status": project.status.value,
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving progress page: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error loading progress page: {e!s}")


@web_router.get("/api/tasks/{task_id}/status")
@requires_sso
async def get_task_status(request: Request, task_id: str):
    """
    Get current task status and progress.

    This endpoint is used for polling by the progress page JavaScript.
    """
    try:
        from opi.core.task_manager import TaskStatus, _project_managers, _projects

        # Check if we have project info
        if task_id not in _projects:
            raise HTTPException(status_code=404, detail="Project not found")

        project = _projects[task_id]

        # Get the TaskProgressManager for this project
        task_manager = _project_managers.get(task_id)

        task_hierarchy = []
        if task_manager:
            logger.debug(f"Found TaskProgressManager for {task_id} with {len(task_manager.tasks)} tasks")
            # Build the proper task hierarchy from this project's tasks
            main_tasks = []
            subtasks_by_parent = {}

            # Organize this project's tasks
            for task in task_manager.tasks.values():
                if task.parent_id is None:
                    # Main task
                    main_tasks.append(task)
                else:
                    # Subtask
                    if task.parent_id not in subtasks_by_parent:
                        subtasks_by_parent[task.parent_id] = []
                    subtasks_by_parent[task.parent_id].append(task)

            # Build the hierarchy
            for main_task in main_tasks:
                task_data = {
                    "id": main_task.id,
                    "name": main_task.name,
                    "status": main_task.status.value,
                    "created_at": main_task.created_at.isoformat(),
                    "completed_at": main_task.completed_at.isoformat() if main_task.completed_at else None,
                    "error": main_task.error,
                    "subtasks": [],
                }

                # Add subtasks if any
                if main_task.id in subtasks_by_parent:
                    for subtask in subtasks_by_parent[main_task.id]:
                        task_data["subtasks"].append(
                            {
                                "id": subtask.id,
                                "name": subtask.name,
                                "status": subtask.status.value,
                                "created_at": subtask.created_at.isoformat(),
                                "completed_at": subtask.completed_at.isoformat() if subtask.completed_at else None,
                                "error": subtask.error,
                            }
                        )

                task_hierarchy.append(task_data)

            # Calculate progress based on completed tasks
            total_tasks = len(task_manager.tasks)
            completed_tasks = sum(1 for t in task_manager.tasks.values() if t.status == TaskStatus.COMPLETED)
            progress = int((completed_tasks / total_tasks * 100) if total_tasks > 0 else 0)
            logger.debug(f"Progress: {completed_tasks}/{total_tasks} = {progress}%")
        else:
            # No task manager yet, starting
            logger.debug(f"No TaskProgressManager found for {task_id}")
            progress = 0

        response_data = {
            "task_id": task_id,
            "status": project.status.value,
            "current_step": project.current_step or "Starting...",
            "project_name": project.project_name,
            "created_at": project.created_at.isoformat(),
            "progress": progress,
            "tasks": task_hierarchy,
        }

        # Add logs if available
        if project.logs:
            response_data["logs"] = project.logs[-50:]  # Last 50 lines

        # Add events if available
        if project.events:
            response_data["events"] = project.events[-20:]  # Last 20 events

        # Add namespace if available
        if project.namespace:
            response_data["namespace"] = project.namespace

        # Add web addresses if available
        if project.web_addresses:
            response_data["web_addresses"] = project.web_addresses

        from fastapi.responses import JSONResponse

        logger.debug(f"Returning response with {len(task_hierarchy)} tasks, progress={progress}")
        return JSONResponse(content=response_data)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting task status: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error getting task status: {e!s}")


@web_router.get("/api/tasks/{task_id}/debug")
@requires_sso
async def debug_task(request: Request, task_id: str):
    """
    Debug endpoint to get detailed task information including errors.

    This is useful for troubleshooting failed tasks.
    """
    try:
        from opi.core.task_manager import _project_managers, _projects, get_task

        project = get_task(task_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        debug_info = {
            "task_id": task_id,
            "status": project.status.value,
            "current_step": project.current_step,
            "project_name": project.project_name,
            "created_at": project.created_at.isoformat(),
            "namespace": project.namespace,
            "all_projects_count": len(_projects),
            "project_tasks_count": len(_project_managers.get(task_id).tasks) if task_id in _project_managers else 0,
        }

        from fastapi.responses import JSONResponse

        return JSONResponse(content=debug_info)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error debugging task: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error debugging task: {e!s}")


@web_router.get("/projects/roos", response_class=HTMLResponse)
@requires_sso
async def roos_project_form(request: Request):
    """
    Serve the ROOS-based project creation form using jinja-roos-components.

    Returns:
        HTML response with the ROOS component-based form
    """
    try:
        templates = get_templates()
        user = get_current_user(request)
        return templates.TemplateResponse(
            "roos-form-improved.html.j2",
            {
                "request": request,
                "title": "Project Aanmaken - ROOS",
                "clusters": ["local", "odcn-production"],
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
    Serve the main dashboard page.

    Returns:
        HTML response with the dashboard showing project overview, metrics, and activity
    """
    try:
        templates = get_templates()
        user = get_current_user(request)
        return templates.TemplateResponse("dashboard.html.j2", {"request": request, "menu_items": get_menu_items(user)})

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
        from opi.services.project_service import get_project_service
        from opi.services.services import ServiceAdapter

        templates = get_templates()
        user = get_current_user(request)

        # Generate CSRF token for form protection (domain settings modal)
        csrf_token = ensure_csrf_token(request)

        # TODO: this logic has to be centralized
        user_email = user.get("email", "").lower()

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
            if deployment.get("configuration"):
                decrypted_yaml = await decrypt_age_content(deployment["configuration"], project_private_key)
                deployment["configuration"] = load_yaml_from_string(decrypted_yaml)

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
        from opi.utils.naming import generate_ingress_map, generate_public_url

        project_file_handler = ProjectFileHandler()

        # Add ingress links to deployments for components with publish-on-web
        for deployment in project_details["deployments"]:
            cluster = deployment.get("cluster")
            deployment["ingress_links"] = []

            if cluster:
                try:
                    ingress_postfix = get_ingress_postfix(cluster)
                    use_https = get_ingress_tls_enabled(cluster)

                    for component in deployment.get("components", []):
                        component_name = component.get("reference")
                        if component_name:
                            # Check if component has publish-on-web service (same as project_manager)
                            has_publish_on_web = project_file_handler.extract_component_publish_on_web(
                                project_data, component_name
                            )

                            if has_publish_on_web:
                                # Generate ingress map exactly like project_manager does
                                subdomain = deployment.get("subdomain")
                                ingress_map = generate_ingress_map(
                                    component_name, deployment["name"], project_name, ingress_postfix, subdomain
                                )

                                # Create links for all ingress hostnames (default + subdomain if exists)
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

                                # Generate ingress map exactly like project_manager does
                                subdomain = deployment.get("subdomain")
                                ingress_map = generate_ingress_map(
                                    component_name, deployment["name"], project_name, ingress_postfix, subdomain
                                )

                                # Create links for all ingress hostnames (default + subdomain if exists)
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

        # Fetch Prometheus time-series metrics for each deployment's components
        deployment_metrics_timeseries: dict[str, dict[str, dict[str, list]]] = {}
        # Track discovered workloads for helm-based deployments (for template display)
        discovered_workloads: dict[str, list[dict[str, Any]]] = {}
        prometheus_available = False
        try:
            from opi.connectors.prometheus import get_metrics_connector
            from opi.core.cluster_config import get_prefixed_namespace

            prom = get_metrics_connector()
            prometheus_available = prom.is_connected

            if prometheus_available:
                for deployment in project_details["deployments"]:
                    deployment_name = deployment.get("name")
                    base_namespace = deployment.get("namespace")
                    cluster = deployment.get("cluster")
                    components = deployment.get("components", [])
                    helm_charts = deployment.get("helm-charts", [])

                    if not deployment_name or not base_namespace or not cluster:
                        continue

                    # Get the actual Kubernetes namespace with cluster prefix
                    k8s_namespace = get_prefixed_namespace(cluster, base_namespace)

                    if components:
                        # Component-based deployment: use predefined components
                        component_names = [c.get("reference") for c in components if c.get("reference")]
                        if component_names:
                            metrics = prom.get_deployment_component_metrics_timeseries(
                                namespace=k8s_namespace,
                                components=component_names,
                                deployment_name=deployment_name,
                                duration_minutes=60,
                                step_minutes=5,
                            )
                            deployment_metrics_timeseries[deployment_name] = metrics
                            logger.debug(f"Fetched time-series metrics for deployment {deployment_name}")

                    elif helm_charts:
                        # Helm-based deployment: discover workloads dynamically
                        workloads = prom.discover_workloads_in_namespace(k8s_namespace)
                        if workloads:
                            discovered_workloads[deployment_name] = workloads
                            metrics = prom.get_discovered_workload_metrics_timeseries(
                                namespace=k8s_namespace,
                                workloads=workloads,
                                duration_minutes=60,
                                step_minutes=5,
                            )
                            deployment_metrics_timeseries[deployment_name] = metrics
                            logger.debug(
                                f"Discovered {len(workloads)} workloads and fetched metrics for helm deployment {deployment_name}"
                            )

        except Exception as metrics_error:
            logger.warning(f"Failed to fetch Prometheus metrics: {metrics_error}")

        # Fetch ArgoCD status for each deployment
        from typing import Any

        argocd_status: dict[str, dict[str, Any]] = {}
        argocd_available = False
        try:
            from opi.connectors.argo import create_argo_connector
            from opi.utils.naming import generate_argocd_application_name

            argo_connector = create_argo_connector()
            argocd_available = argo_connector.auth_token is not None

            if argocd_available:
                for deployment in project_details["deployments"]:
                    deployment_name = deployment.get("name")
                    if deployment_name:
                        app_name = generate_argocd_application_name(project_name, deployment_name)
                        try:
                            status_data = await argo_connector.get_application_status(app_name)
                            if status_data:
                                # Extract relevant status information
                                status = status_data.get("status", {})
                                health = status.get("health", {})
                                sync = status.get("sync", {})
                                operation_state = status.get("operationState", {})

                                # Extract errors from resources and conditions
                                errors = []
                                for resource in status.get("resources", []):
                                    resource_health = resource.get("health", {})
                                    if resource_health.get("status") in ["Degraded", "Missing"]:
                                        error_msg = resource_health.get("message", "Unknown error")
                                        resource_name = (
                                            f"{resource.get('kind', 'Resource')}/{resource.get('name', 'unknown')}"
                                        )
                                        errors.append({"resource": resource_name, "message": error_msg})

                                # Check for sync errors
                                if operation_state.get("phase") == "Failed":
                                    op_message = operation_state.get("message", "Sync operation failed")
                                    errors.append({"resource": "SyncOperation", "message": op_message})

                                # Get last sync time
                                last_sync = None
                                if operation_state.get("finishedAt"):
                                    last_sync = operation_state.get("finishedAt")
                                elif sync.get("status") == "Synced":
                                    last_sync = status.get("reconciledAt")

                                argocd_status[deployment_name] = {
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
                                logger.debug(
                                    f"Fetched ArgoCD status for {app_name}: health={health.get('status')}, sync={sync.get('status')}"
                                )
                            else:
                                argocd_status[deployment_name] = {
                                    "app_name": app_name,
                                    "available": False,
                                    "health": "Unknown",
                                    "sync": "Unknown",
                                    "errors": [
                                        {"resource": "Application", "message": "Application not found in ArgoCD"}
                                    ],
                                }
                        except Exception as app_error:
                            logger.warning(f"Failed to fetch ArgoCD status for {app_name}: {app_error}")
                            argocd_status[deployment_name] = {
                                "app_name": app_name,
                                "available": False,
                                "health": "Unknown",
                                "sync": "Unknown",
                                "errors": [{"resource": "API", "message": str(app_error)}],
                            }
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
                    snapshots = await backup_manager.list_snapshots(cluster, k8s_namespace, project_name=project_name)
                    if snapshots:
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
                                # Raw tags for debugging
                                "tags": s.tags,
                            }
                            for s in snapshots
                        ]
                        logger.debug(f"Found {len(snapshots)} backups for deployment {deployment_name}")
                except Exception as backup_err:
                    logger.warning(f"Failed to fetch backups for deployment {deployment_name}: {backup_err}")

        except Exception as backup_init_error:
            logger.warning(f"Failed to initialize backup manager: {backup_init_error}")
            backups_available = False

        # Generate ingress URLs for components with inbound ports
        from opi.core.cluster_config import get_ingress_postfix, get_ingress_tls_enabled
        from opi.utils.naming import generate_ingress_map, generate_public_url

        # Add ingress information to deployments
        for deployment in project_details["deployments"]:
            cluster = deployment.get("cluster")
            if cluster:
                try:
                    ingress_postfix = get_ingress_postfix(cluster)
                    use_https = get_ingress_tls_enabled(cluster)
                    deployment["ingress_links"] = []

                    # Generate ingress links for each component in this deployment
                    for component in deployment.get("components", []):
                        component_name = component.get("reference")
                        if component_name:
                            # Find the component definition to check for inbound ports
                            component_def = next(
                                (c for c in project_details["components"] if c.get("name") == component_name), None
                            )

                            # Only create ingress links for components with inbound ports
                            if component_def and component_def.get("ports", {}).get("inbound"):
                                ingress_map = generate_ingress_map(
                                    component_name,
                                    deployment["name"],
                                    project_name,
                                    ingress_postfix,
                                    deployment.get("subdomain"),
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
            # Only show ingress links for components with inbound ports
            if component.get("ports", {}).get("inbound"):
                # Find deployments that use this component
                for deployment in project_details["deployments"]:
                    cluster = deployment.get("cluster")
                    if cluster and any(
                        c.get("reference") == component["name"] for c in deployment.get("components", [])
                    ):
                        try:
                            ingress_postfix = get_ingress_postfix(cluster)
                            use_https = get_ingress_tls_enabled(cluster)

                            ingress_map = generate_ingress_map(
                                component["name"],
                                deployment["name"],
                                project_name,
                                ingress_postfix,
                                deployment.get("subdomain"),
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
                                f"Failed to generate ingress links for component {component['name']} in deployment {deployment['name']}: {ingress_error}"
                            )

        # Get cluster base domains for domain settings modal
        from opi.web.router_self_service import get_cluster_base_domains_for_template

        cluster_base_domains = get_cluster_base_domains_for_template()

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
                "deployment_metrics_timeseries": deployment_metrics_timeseries,
                "discovered_workloads": discovered_workloads,
                "prometheus_available": prometheus_available,
                "argocd_status": argocd_status,
                "argocd_available": argocd_available,
                "deployment_backups": deployment_backups,
                "backups_available": backups_available,
                "current_cluster": current_cluster,
                "cluster_base_domains": cluster_base_domains,
                "csrf_token": csrf_token,
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
        root_component = None
        components_list = []
        for comp in deployment.get("components", []):
            comp_ref = comp.get("reference")
            is_root = comp.get("root", False)
            if is_root and comp_ref:
                root_component = comp_ref
            if comp_ref:
                components_list.append({"reference": comp_ref, "root": is_root})

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
    Validate CSRF protection using both token validation and Origin/Referer headers.

    This implements defense in depth:
    1. CSRF token validation (primary defense)
    2. Origin/Referer header validation (secondary defense)

    For SSO-protected endpoints that modify data, we require both checks to pass.

    Args:
        request: The FastAPI request object
        form_data: Optional pre-parsed form data (to avoid parsing twice)

    Raises:
        HTTPException: If CSRF validation fails
    """
    from opi.utils.csrf import validate_csrf_token

    # First, validate CSRF token (primary defense)
    # Convert form_data to dict if needed for csrf validation
    csrf_form_data = dict(form_data) if form_data else None
    validate_csrf_token(request, csrf_form_data)

    # Then, validate Origin/Referer headers (secondary defense)
    _validate_csrf_origin(request)


def _validate_csrf_origin(request: Request) -> None:
    """
    Validate Origin/Referer header for CSRF protection.

    This is a secondary defense layer alongside CSRF token validation.
    For SSO-protected endpoints that modify data, we validate that the request
    originates from our own domain to prevent cross-site request forgery.

    Args:
        request: The FastAPI request object

    Raises:
        HTTPException: If Origin/Referer validation fails
    """
    from urllib.parse import urlparse

    from opi.core.config import settings

    # Get the host from the request and strip whitespace
    host = (request.headers.get("host") or "").strip()
    origin = (request.headers.get("origin") or "").strip()
    referer = (request.headers.get("referer") or "").strip()

    # At least one of Origin or Referer must be present for POST requests
    if not origin and not referer:
        logger.warning("CSRF check failed: No Origin or Referer header present")
        raise HTTPException(status_code=403, detail="Request rejected: missing origin information")

    # Extract and validate request host (must be non-empty)
    request_host = (host.split(":")[0] or "").strip()
    if not request_host:
        logger.warning("CSRF check failed: Request has empty or missing Host header")
        raise HTTPException(status_code=403, detail="Request rejected: invalid request")

    # Only allow localhost bypass in DEBUG mode AND when the request is actually to localhost
    # This prevents DEBUG mode from weakening security when running on production hosts
    is_localhost_request = request_host in ("localhost", "127.0.0.1", "::1")
    allow_localhost = settings.DEBUG and is_localhost_request

    # Security warning: log if DEBUG mode is enabled on a non-localhost host
    if settings.DEBUG and not is_localhost_request:
        logger.warning(
            f"SECURITY: DEBUG mode is enabled but request is to non-localhost host '{request_host}'. "
            f"Localhost CSRF bypass is DISABLED for this request. Consider disabling DEBUG in production."
        )

    # Validate Origin header if present
    if origin:
        # Origin should match our host (with or without port)
        parsed_origin = urlparse(origin)
        origin_host = (parsed_origin.netloc.split(":")[0] or "").strip()

        # Origin host must be non-empty and match request host
        if not origin_host:
            logger.warning(f"CSRF check failed: Origin '{origin}' has empty host")
            raise HTTPException(status_code=403, detail="Request rejected: invalid origin")

        # Check if origin matches request host (or localhost in debug mode when request is also localhost)
        origin_matches = origin_host == request_host
        localhost_allowed = allow_localhost and origin_host in ("localhost", "127.0.0.1", "::1")

        if not origin_matches and not localhost_allowed:
            logger.warning(f"CSRF check failed: Origin '{origin}' does not match host '{host}'")
            raise HTTPException(status_code=403, detail="Request rejected: invalid origin")

    # Validate Referer header if Origin not present
    elif referer:
        parsed_referer = urlparse(referer)
        referer_host = (parsed_referer.netloc.split(":")[0] or "").strip()

        # Referer host must be non-empty and match request host
        if not referer_host:
            logger.warning(f"CSRF check failed: Referer '{referer}' has empty host")
            raise HTTPException(status_code=403, detail="Request rejected: invalid referer")

        # Check if referer matches request host (or localhost in debug mode when request is also localhost)
        referer_matches = referer_host == request_host
        localhost_allowed = allow_localhost and referer_host in ("localhost", "127.0.0.1", "::1")

        if not referer_matches and not localhost_allowed:
            logger.warning(f"CSRF check failed: Referer '{referer}' does not match host '{host}'")
            raise HTTPException(status_code=403, detail="Request rejected: invalid referer")


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
    project_manager: "ProjectManager",
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
                    uses_services = component.get("uses-services", [])
                    component_services = ServiceAdapter.parse_services_from_strings(uses_services)
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
            domain_mode=domain_mode,
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

                # Handle root component
                for comp in yaml_dep.get("components", []):
                    # Clear existing root flags
                    if "root" in comp:
                        del comp["root"]
                    # Set root flag on selected component
                    if root_component and comp.get("reference") == root_component:
                        comp["root"] = True

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
