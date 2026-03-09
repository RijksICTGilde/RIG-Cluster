"""Task handlers for add-component, add-component-to-deployment, and add-service.

These handlers are called by TaskWorker. Each receives a payload dict and a
PersistentTaskProgressManager instance, and returns a result dict.

They have NO dependency on FastAPI Request objects.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def handle_add_component(payload: dict, progress: Any) -> dict:
    """Handle async add-component task.

    Adds a new component definition to a project and processes all affected
    deployments.
    """
    from opi.manager.project_manager import ProjectManager
    from opi.utils.naming import sanitize_kubernetes_name
    from opi.utils.project_utils import validate_project_name

    project_name: str = payload["project_name"]
    component_name: str = payload["name"]
    deployment_names: list[str] = payload["deployment_names"]
    project_manager: ProjectManager | None = None

    try:
        # ------------------------------------------------------------------
        # Step 1: Validation
        # ------------------------------------------------------------------
        validate_task = progress.add_task("Component validatie")
        progress.update_current_step("Validating project and component names")

        if not validate_project_name(project_name):
            error_msg = (
                "Invalid project name format. Must start with lowercase letter, "
                "then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters"
            )
            progress.fail_task(validate_task, error_msg)
            progress.fail_project(error_msg)
            return {"component_name": component_name, "status": "failed", "error": error_msg}

        sanitized_name = sanitize_kubernetes_name(component_name)
        if sanitized_name != component_name.lower():
            error_msg = (
                f"Invalid component name. Use lowercase letters, numbers, and hyphens only. Suggested: {sanitized_name}"
            )
            progress.fail_task(validate_task, error_msg)
            progress.fail_project(error_msg)
            return {"component_name": component_name, "status": "failed", "error": error_msg}

        progress.complete_task(validate_task)

        # ------------------------------------------------------------------
        # Step 2: Add component to project YAML
        # ------------------------------------------------------------------
        add_task = progress.add_task("Component toevoegen")
        progress.update_current_step(f"Adding component '{component_name}'")

        project_file_relative_path = f"projects/{project_name}.yaml"
        project_manager = ProjectManager(project_file_relative_path=project_file_relative_path)

        result = await project_manager.add_component(
            name=component_name,
            component_type=payload.get("type", "single"),
            image=payload["image"],
            deployment_names=deployment_names,
            port=payload.get("port"),
            path=payload.get("path", "/"),
            services=payload.get("services"),
            cpu_limit=payload.get("cpu_limit"),
            memory_limit=payload.get("memory_limit"),
            env_vars=payload.get("env_vars"),
            aliases=payload.get("aliases"),
            root=payload.get("root", False),
        )

        if not result["success"]:
            error_msg = result.get("error", "Unknown error adding component")
            progress.fail_task(add_task, error_msg)
            progress.fail_project(error_msg)
            return {
                "component_name": component_name,
                "status": "failed",
                "error": error_msg,
                "error_type": result.get("error_type", "unknown"),
            }

        progress.complete_task(add_task)

        # ------------------------------------------------------------------
        # Step 3: Process project
        # ------------------------------------------------------------------
        deploy_task = progress.add_task("Project processing")
        progress.update_current_step("Processing project to create K8s resources")

        processing_success = await project_manager.process_project_from_git(
            project_file_relative_path,
            task_progress_manager=progress,
        )

        # Collect URLs from deployment results
        urls: dict[str, dict[str, Any]] = {}
        for dep_name in result.get("deployments_updated", []):
            deployment_results = project_manager.get_deployment_results(dep_name)
            for name, dep_result in deployment_results.items():
                urls[name] = {
                    "cluster": dep_result.cluster,
                    "urls": dep_result.urls,
                }

        if processing_success:
            progress.complete_task(deploy_task)
        else:
            processing_error = project_manager.get_processing_error() or "Project processing failed"
            progress.fail_task(deploy_task, processing_error)
            progress.fail_project(processing_error)

        # ------------------------------------------------------------------
        # Build response
        # ------------------------------------------------------------------
        succeeded = bool(processing_success)
        response: dict[str, Any] = {
            "status": "success" if succeeded else "failed",
            "message": (
                f"Component '{component_name}' added successfully"
                if succeeded
                else (project_manager.get_processing_error() or f"Component '{component_name}' processing failed")
            ),
            "component": result.get("component"),
            "deployments_updated": result.get("deployments_updated", []),
            "urls": urls,
            "processing": {
                "status": "completed" if succeeded else "failed",
                **(
                    {"error": project_manager.get_processing_error()}
                    if not succeeded and project_manager.get_processing_error()
                    else {}
                ),
            },
        }
        if result.get("warnings"):
            response["warnings"] = result["warnings"]
        return response

    except Exception as exc:
        error_msg = f"Error adding component: {exc}"
        logger.error(error_msg)
        progress.fail_project(error_msg)
        return {"component_name": component_name, "status": "failed", "error": error_msg}
    finally:
        if project_manager:
            await project_manager.close()


async def handle_add_component_to_deployment(payload: dict, progress: Any) -> dict:
    """Handle async add-component-to-deployment task.

    Adds an existing component to a deployment that doesn't yet include it.
    """
    from opi.manager.project_manager import ProjectManager
    from opi.utils.naming import sanitize_kubernetes_name
    from opi.utils.project_utils import validate_project_name

    project_name: str = payload["project_name"]
    deployment_name: str = payload["deployment_name"]
    component_name: str = payload["component_name"]
    project_manager: ProjectManager | None = None

    try:
        # ------------------------------------------------------------------
        # Step 1: Validation
        # ------------------------------------------------------------------
        validate_task = progress.add_task("Component validatie")
        progress.update_current_step("Validating names")

        if not validate_project_name(project_name):
            error_msg = (
                "Invalid project name format. Must start with lowercase letter, "
                "then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters"
            )
            progress.fail_task(validate_task, error_msg)
            progress.fail_project(error_msg)
            return {"component_name": component_name, "status": "failed", "error": error_msg}

        sanitized_name = sanitize_kubernetes_name(component_name)
        if sanitized_name != component_name.lower():
            error_msg = (
                f"Invalid component name. Use lowercase letters, numbers, and hyphens only. Suggested: {sanitized_name}"
            )
            progress.fail_task(validate_task, error_msg)
            progress.fail_project(error_msg)
            return {"component_name": component_name, "status": "failed", "error": error_msg}

        progress.complete_task(validate_task)

        # ------------------------------------------------------------------
        # Step 2: Add component to deployment
        # ------------------------------------------------------------------
        add_task = progress.add_task("Component aan deployment toevoegen")
        progress.update_current_step(f"Adding component '{component_name}' to deployment '{deployment_name}'")

        project_file_relative_path = f"projects/{project_name}.yaml"
        project_manager = ProjectManager(project_file_relative_path=project_file_relative_path)

        result = await project_manager.add_component_to_deployment(
            deployment_name=deployment_name,
            component_name=component_name,
            image=payload["image"],
        )

        if not result["success"]:
            error_msg = result.get("error", "Unknown error adding component to deployment")
            progress.fail_task(add_task, error_msg)
            progress.fail_project(error_msg)
            return {
                "component_name": component_name,
                "deployment_name": deployment_name,
                "status": "failed",
                "error": error_msg,
                "error_type": result.get("error_type", "unknown"),
            }

        progress.complete_task(add_task)

        # ------------------------------------------------------------------
        # Step 3: Process the affected deployment
        # ------------------------------------------------------------------
        deploy_task = progress.add_task("Deployment processing")
        progress.update_current_step(f"Processing deployment '{deployment_name}'")

        processing_success = await project_manager.process_project_from_git(
            project_file_relative_path,
            task_progress_manager=progress,
            deployment_name=deployment_name,
        )

        # Collect URLs from deployment results
        urls: dict[str, dict[str, Any]] = {}
        deployment_results = project_manager.get_deployment_results(deployment_name)
        for name, dep_result in deployment_results.items():
            urls[name] = {
                "cluster": dep_result.cluster,
                "urls": dep_result.urls,
            }

        if processing_success:
            progress.complete_task(deploy_task)
        else:
            processing_error = project_manager.get_processing_error() or "Deployment processing failed"
            progress.fail_task(deploy_task, processing_error)
            progress.fail_project(processing_error)

        # ------------------------------------------------------------------
        # Build response
        # ------------------------------------------------------------------
        succeeded = bool(processing_success)
        response: dict[str, Any] = {
            "status": "success" if succeeded else "failed",
            "message": (
                f"Component '{component_name}' added to deployment '{deployment_name}'"
                if succeeded
                else (
                    project_manager.get_processing_error()
                    or f"Component '{component_name}' deployment processing failed"
                )
            ),
            "deployment": deployment_name,
            "component_reference": result.get("component_reference"),
            "urls": urls,
            "processing": {
                "status": "completed" if succeeded else "failed",
                **(
                    {"error": project_manager.get_processing_error()}
                    if not succeeded and project_manager.get_processing_error()
                    else {}
                ),
            },
        }
        if result.get("warnings"):
            response["warnings"] = result["warnings"]
        return response

    except Exception as exc:
        error_msg = f"Error adding component to deployment: {exc}"
        logger.error(error_msg)
        progress.fail_project(error_msg)
        return {
            "component_name": component_name,
            "deployment_name": deployment_name,
            "status": "failed",
            "error": error_msg,
        }
    finally:
        if project_manager:
            await project_manager.close()


async def handle_add_service(payload: dict, progress: Any) -> dict:
    """Handle async add-service task.

    Adds a service to a project and processes affected deployments.
    """
    from opi.manager.project_manager import ProjectManager
    from opi.utils.project_utils import validate_project_name

    project_name: str = payload["project_name"]
    service_name: str = payload["service"]
    component_names: list[str] | None = payload.get("components")
    project_manager: ProjectManager | None = None

    try:
        # ------------------------------------------------------------------
        # Step 1: Validation
        # ------------------------------------------------------------------
        validate_task = progress.add_task("Service validatie")
        progress.update_current_step("Validating project name")

        if not validate_project_name(project_name):
            error_msg = (
                "Invalid project name format. Must start with lowercase letter, "
                "then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters"
            )
            progress.fail_task(validate_task, error_msg)
            progress.fail_project(error_msg)
            return {"service": service_name, "status": "failed", "error": error_msg}

        progress.complete_task(validate_task)

        # ------------------------------------------------------------------
        # Step 2: Add service
        # ------------------------------------------------------------------
        add_task = progress.add_task("Service toevoegen")
        progress.update_current_step(f"Adding service '{service_name}'")

        project_file_relative_path = f"projects/{project_name}.yaml"
        project_manager = ProjectManager(project_file_relative_path=project_file_relative_path)

        result = await project_manager.add_service(
            service_name=service_name,
            component_names=component_names,
        )

        if not result["success"]:
            error_msg = result.get("error", "Unknown error adding service")
            progress.fail_task(add_task, error_msg)
            progress.fail_project(error_msg)
            return {
                "service": service_name,
                "status": "failed",
                "error": error_msg,
                "error_type": result.get("error_type", "unknown"),
            }

        progress.complete_task(add_task)

        # ------------------------------------------------------------------
        # Step 3: Process project (only if new services were added)
        # ------------------------------------------------------------------
        processing_status = "skipped"
        if result.get("services_added"):
            deploy_task = progress.add_task("Project processing")
            progress.update_current_step("Processing project to provision services")

            processing_success = await project_manager.process_project_from_git(
                project_file_relative_path,
                task_progress_manager=progress,
            )

            if processing_success:
                processing_status = "completed"
                progress.complete_task(deploy_task)
            else:
                processing_status = "failed"
                processing_error = project_manager.get_processing_error() or "Project processing failed"
                progress.fail_task(deploy_task, processing_error)
                progress.fail_project(processing_error)

        # ------------------------------------------------------------------
        # Build response
        # ------------------------------------------------------------------
        response: dict[str, Any] = {
            "status": "success",
            "message": f"Service '{service_name}' added successfully",
            "services_added": result.get("services_added", []),
            "services_skipped": result.get("services_skipped", []),
            "components_updated": result.get("components_updated", []),
            "processing": {"status": processing_status},
        }
        if result.get("warnings"):
            response["warnings"] = result["warnings"]
        return response

    except Exception as exc:
        error_msg = f"Error adding service: {exc}"
        logger.error(error_msg)
        progress.fail_project(error_msg)
        return {"service": service_name, "status": "failed", "error": error_msg}
    finally:
        if project_manager:
            await project_manager.close()
