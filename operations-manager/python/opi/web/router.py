"""
Web routes for serving HTML pages (non-API endpoints).
"""

import copy
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from opi.api.router import SelfServiceComponent, SelfServiceProjectRequest
from opi.core.auth_decorators import get_current_user, requires_sso
from opi.core.task_manager import create_task
from opi.core.templates import get_templates
from opi.utils.age import decrypt_password_smart, get_global_private_key
from opi.utils.project_names import generate_project_name
from opi.utils.yaml_util import load_yaml_from_string
from opi.web.menu import get_menu_items

from ..utils.age import decrypt_age_content
from .router_self_service import self_service_portal
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
        # Get current user for logging
        user = get_current_user(request)
        logger.info(f"Processing self-service form submission by user: {user.get('email', 'unknown')}")

        # Parse form data
        form_data = await request.form()
        logger.debug(f"Received form data keys: {list(form_data.keys())}")

        # Extract project details
        display_name = str(form_data.get("display-name", "")).strip()
        project_description = str(form_data.get("project-description", "")).strip()
        cluster = str(form_data.get("cluster", "")).strip()

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
            comp_cpu = str(form_data.get(f"components[{component_index}][cpu_limit]", "")).strip()
            comp_memory = str(form_data.get(f"components[{component_index}][memory_limit]", "")).strip()
            comp_env_vars = str(form_data.get(f"components[{component_index}][env_vars]", "")).strip()
            comp_services = form_data.getlist(f"components[{component_index}][services][]")

            # Parse port as integer
            try:
                port = int(str(comp_port)) if comp_port and str(comp_port).strip() else None
            except ValueError:
                port = None

            component = SelfServiceComponent(
                type=comp_type,
                port=port,
                image=comp_image or "nginx:latest",
                cpu_limit=comp_cpu or None,
                memory_limit=comp_memory or None,
                env_vars=comp_env_vars or None,
                services=comp_services or None,
            )
            components.append(component)
            component_index += 1

        # Create the request object
        project_data = SelfServiceProjectRequest(
            project_name=project_name,
            display_name=display_name,
            project_description=project_description or None,
            cluster=cluster,
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
        deletion_results = await project_manager.delete_project_with_deployment_cleanup(project_name)

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
            other_clusters = set(dep["cluster"] for dep in remaining_deployments)
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

        for deployment in project_data_decrypted.get("deployments", []):
            if deployment.get("configuration"):
                decrypted_yaml = await decrypt_age_content(deployment["configuration"], project_private_key)
                deployment["configuration"] = load_yaml_from_string(decrypted_yaml)

        for component in project_data_decrypted.get("components", []):
            if component.get("user-env-vars"):
                decrypted_yaml = await decrypt_age_content(component["user-env-vars"], project_private_key)
                component["user-env-vars"] = load_yaml_from_string(decrypted_yaml)

        # Process services to add display information
        services_with_info = []
        project_services = project_data.get("services", [])
        for service_value in project_services:
            service_enum = ServiceAdapter.get_service_by_value(service_value)
            if service_enum:
                services_with_info.append(
                    {
                        "enum": service_enum,
                        "value": service_value,
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
            },
        )

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


@web_router.get("/projects", response_class=HTMLResponse)
@requires_sso
async def projects_overview(request: Request):
    """
    Serve the projects overview page with table layout.
    Shows only projects where the current user's email is in the users list.

    Returns:
        HTML response with a table showing user's projects and their status
    """
    try:
        from opi.services.project_service import get_project_service

        templates = get_templates()
        user = get_current_user(request)
        user_email = user.get("email", "").lower()

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
