"""Task handlers for add-component, add-component-to-deployment, and add-service.

These handlers are called by TaskWorker. Each receives a payload dict and a
PersistentTaskProgressManager instance, and returns a result dict.

They have NO dependency on FastAPI Request objects.
"""

import logging
from typing import Any

from opi.core.task_rollout import note_rollout_skipped, rollout_requested, skipped_processing

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
        progress.update_current_step("Project- en componentnaam controleren")

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
        progress.update_current_step(f"Component '{component_name}' toevoegen")

        project_file_relative_path = f"projects/{project_name}.yaml"
        project_manager = ProjectManager(project_file_relative_path=project_file_relative_path)

        result = await project_manager.add_component(
            name=component_name,
            component_type=payload.get("type", "single"),
            image=payload["image"],
            deployment_names=deployment_names,
            port=payload.get("port"),
            ports=payload.get("ports"),
            path=payload.get("path", "/"),
            rewrite=payload.get("rewrite"),
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
        # Step 3: Process project (unless the caller deferred the rollout)
        # ------------------------------------------------------------------
        urls: dict[str, dict[str, Any]] = {}

        if not rollout_requested(payload):
            note_rollout_skipped(progress)
            succeeded = True
            processing: dict[str, Any] = skipped_processing()
        else:
            deploy_task = progress.add_task("Project verwerken")
            progress.update_current_step("Project verwerken om Kubernetes-resources aan te maken")

            # Scope the reprocess to the deployments the component was added to, so it
            # doesn't regenerate (and re-commit) every other deployment's manifests.
            processing_success = await project_manager.process_project_from_git(
                project_file_relative_path,
                task_progress_manager=progress,
                deployment_names=deployment_names,
            )

            # Collect URLs from deployment results
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

            succeeded = bool(processing_success)
            processing = {
                "status": "completed" if succeeded else "failed",
                **(
                    {"error": project_manager.get_processing_error()}
                    if not succeeded and project_manager.get_processing_error()
                    else {}
                ),
                **({"component_failures": cf} if (cf := project_manager.get_component_failures()) else {}),
            }

        # ------------------------------------------------------------------
        # Build response
        # ------------------------------------------------------------------
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
            "processing": processing,
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


async def handle_update_component(payload: dict, progress: Any) -> dict:
    """Handle async update-component task.

    Applies a partial update to an existing component and reprocesses the project so the
    new manifests are generated. Mirrors handle_add_component's shape.
    """
    from opi.manager.project_manager import ProjectManager
    from opi.utils.project_utils import validate_project_name

    project_name: str = payload["project_name"]
    component_name: str = payload["name"]
    project_manager: ProjectManager | None = None

    try:
        validate_task = progress.add_task("Component validatie")
        progress.update_current_step("Projectnaam controleren")
        if not validate_project_name(project_name):
            error_msg = (
                "Invalid project name format. Must start with lowercase letter, "
                "then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters"
            )
            progress.fail_task(validate_task, error_msg)
            progress.fail_project(error_msg)
            return {"component_name": component_name, "status": "failed", "error": error_msg}
        progress.complete_task(validate_task)

        update_task = progress.add_task("Component bijwerken")
        progress.update_current_step(f"Component '{component_name}' bijwerken")
        project_file_relative_path = f"projects/{project_name}.yaml"
        project_manager = ProjectManager(project_file_relative_path=project_file_relative_path)

        result = await project_manager.update_component(
            name=component_name,
            image=payload.get("image"),
            port=payload.get("port"),
            ports=payload.get("ports"),
            path=payload.get("path"),
            rewrite=payload.get("rewrite"),
            services=payload.get("services"),
            add_services=payload.get("add_services"),
            remove_services=payload.get("remove_services"),
            cpu_limit=payload.get("cpu_limit"),
            memory_limit=payload.get("memory_limit"),
        )

        if not result["success"]:
            error_msg = result.get("error", "Unknown error updating component")
            progress.fail_task(update_task, error_msg)
            progress.fail_project(error_msg)
            return {
                "component_name": component_name,
                "status": "failed",
                "error": error_msg,
                "error_type": result.get("error_type", "unknown"),
            }
        progress.complete_task(update_task)

        if not rollout_requested(payload):
            note_rollout_skipped(progress)
            succeeded = True
            processing: dict[str, Any] = skipped_processing()
        else:
            deploy_task = progress.add_task("Project verwerken")
            progress.update_current_step("Project verwerken om Kubernetes-resources opnieuw te genereren")
            processing_success = await project_manager.process_project_from_git(
                project_file_relative_path,
                task_progress_manager=progress,
            )

            if processing_success:
                progress.complete_task(deploy_task)
            else:
                processing_error = project_manager.get_processing_error() or "Project processing failed"
                progress.fail_task(deploy_task, processing_error)
                progress.fail_project(processing_error)

            succeeded = bool(processing_success)
            processing = {
                "status": "completed" if succeeded else "failed",
                **(
                    {"error": project_manager.get_processing_error()}
                    if not succeeded and project_manager.get_processing_error()
                    else {}
                ),
                **({"component_failures": cf} if (cf := project_manager.get_component_failures()) else {}),
            }

        response: dict[str, Any] = {
            "status": "success" if succeeded else "failed",
            "message": (
                f"Component '{component_name}' updated successfully"
                if succeeded
                else (project_manager.get_processing_error() or f"Component '{component_name}' processing failed")
            ),
            "component": result.get("component"),
            "processing": processing,
        }
        if result.get("warnings"):
            response["warnings"] = result["warnings"]
        return response

    except Exception as exc:
        error_msg = f"Error updating component: {exc}"
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
        progress.update_current_step("Namen controleren")

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
        progress.update_current_step(f"Component '{component_name}' toevoegen aan deployment '{deployment_name}'")

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
        # Step 3: Process the affected deployment (unless the rollout was deferred)
        # ------------------------------------------------------------------
        urls: dict[str, dict[str, Any]] = {}

        if not rollout_requested(payload):
            note_rollout_skipped(progress)
            succeeded = True
            processing: dict[str, Any] = skipped_processing()
        else:
            deploy_task = progress.add_task("Deployment verwerken")
            progress.update_current_step(f"Deployment '{deployment_name}' verwerken")

            processing_success = await project_manager.process_project_from_git(
                project_file_relative_path,
                task_progress_manager=progress,
                deployment_name=deployment_name,
            )

            # Collect URLs from deployment results
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

            succeeded = bool(processing_success)
            processing = {
                "status": "completed" if succeeded else "failed",
                **(
                    {"error": project_manager.get_processing_error()}
                    if not succeeded and project_manager.get_processing_error()
                    else {}
                ),
                **({"component_failures": cf} if (cf := project_manager.get_component_failures()) else {}),
            }

        # ------------------------------------------------------------------
        # Build response
        # ------------------------------------------------------------------
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
            "processing": processing,
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
        validate_task = progress.add_task("Dienst validatie")
        progress.update_current_step("Projectnaam controleren")

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
        add_task = progress.add_task("Dienst toevoegen")
        progress.update_current_step(f"Dienst '{service_name}' toevoegen")

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
        # Nothing added means nothing to reconcile; a deferred rollout says the same for a
        # different reason, and the reason travels with the status.
        processing: dict[str, Any] = {"status": "skipped"}
        if not rollout_requested(payload):
            note_rollout_skipped(progress)
            processing = skipped_processing()
        elif result.get("services_added"):
            deploy_task = progress.add_task("Project verwerken")
            progress.update_current_step("Project verwerken om de diensten klaar te zetten")

            processing_success = await project_manager.process_project_from_git(
                project_file_relative_path,
                task_progress_manager=progress,
            )

            if processing_success:
                processing = {"status": "completed"}
                progress.complete_task(deploy_task)
            else:
                processing = {"status": "failed"}
                processing_error = project_manager.get_processing_error() or "Project processing failed"
                progress.fail_task(deploy_task, processing_error)
                progress.fail_project(processing_error)

        # ------------------------------------------------------------------
        # Build response
        # ------------------------------------------------------------------
        succeeded = processing["status"] != "failed"
        response: dict[str, Any] = {
            "status": "success" if succeeded else "failed",
            "message": f"Service '{service_name}' added successfully"
            if succeeded
            else (project_manager.get_processing_error() or "Project processing failed"),
            "services_added": result.get("services_added", []),
            "services_skipped": result.get("services_skipped", []),
            "components_updated": result.get("components_updated", []),
            "processing": {
                **processing,
                **(
                    {"error": project_manager.get_processing_error()}
                    if not succeeded and project_manager.get_processing_error()
                    else {}
                ),
                **({"component_failures": cf} if (cf := project_manager.get_component_failures()) else {}),
            },
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


async def handle_configure_service(payload: dict, progress: Any) -> dict:
    """Handle async configure-service task (unified service-config endpoint).

    Upserts or clears one service's config at a target layer, then processes the
    project so the change is reconciled -- mirroring handle_add_service. A clear
    that changed nothing skips processing (and, per the logging rule, is a quiet
    no-op).
    """
    from opi.manager.project_manager import ProjectManager
    from opi.utils.project_utils import validate_project_name

    project_name: str = payload["project_name"]
    service_name: str = payload["service"]
    target: str = payload["target"]
    operation: str = payload.get("operation", "upsert")
    component_name: str | None = payload.get("component")
    deployment_name: str | None = payload.get("deployment")
    config = payload.get("config")
    project_manager: ProjectManager | None = None

    try:
        validate_task = progress.add_task("Dienstconfiguratie validatie")
        progress.update_current_step("Projectnaam controleren")
        if not validate_project_name(project_name):
            error_msg = (
                "Invalid project name format. Must start with lowercase letter, "
                "then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters"
            )
            progress.fail_task(validate_task, error_msg)
            progress.fail_project(error_msg)
            return {"service": service_name, "target": target, "status": "failed", "error": error_msg}
        progress.complete_task(validate_task)

        write_task = progress.add_task("Dienstconfiguratie schrijven")
        progress.update_current_step(f"Dienst '{service_name}' instellen op {target}")
        project_file_relative_path = f"projects/{project_name}.yaml"
        project_manager = ProjectManager(project_file_relative_path=project_file_relative_path)

        if operation == "clear":
            result = await project_manager.clear_service_config(
                service_name, target, component_name=component_name, deployment_name=deployment_name
            )
        elif operation == "patch":
            result = await project_manager.patch_service_config_list(
                service_name,
                target,
                add=payload.get("add") or [],
                remove=payload.get("remove") or [],
                component_name=component_name,
                deployment_name=deployment_name,
            )
        else:
            if config is None:
                msg = f"Upsert van service '{service_name}' zonder config"
                raise ValueError(msg)
            result = await project_manager.configure_service(
                service_name, target, config, component_name=component_name, deployment_name=deployment_name
            )

        if not result["success"]:
            error_msg = result.get("error", "Unknown error configuring service")
            progress.fail_task(write_task, error_msg)
            progress.fail_project(error_msg)
            return {
                "service": service_name,
                "target": target,
                "status": "failed",
                "error": error_msg,
                "error_type": result.get("error_type", "unknown"),
            }
        progress.complete_task(write_task)

        # Only reconcile when the project file actually changed. A clear that found
        # nothing to remove returns removed=False and skips processing entirely.
        changed = operation != "clear" or result.get("removed", False)
        processing: dict[str, Any] = {"status": "skipped"}
        if not rollout_requested(payload):
            note_rollout_skipped(progress)
            processing = skipped_processing()
        elif changed:
            deploy_task = progress.add_task("Project verwerken")
            progress.update_current_step("Project verwerken om de dienstconfiguratie bij te trekken")
            processing_success = await project_manager.process_project_from_git(
                project_file_relative_path, task_progress_manager=progress
            )
            if processing_success:
                processing = {"status": "completed"}
                progress.complete_task(deploy_task)
            else:
                processing = {"status": "failed"}
                processing_error = project_manager.get_processing_error() or "Project processing failed"
                progress.fail_task(deploy_task, processing_error)
                progress.fail_project(processing_error)

        succeeded = processing["status"] != "failed"
        counts = (
            {key: result[key] for key in ("added", "updated", "removed") if key in result}
            if operation == "patch"
            else {"removed": result.get("removed")}
        )
        return {
            "status": "success" if succeeded else "failed",
            "service": service_name,
            "target": target,
            **counts,
            "processing": {
                **processing,
                **(
                    {"error": project_manager.get_processing_error()}
                    if not succeeded and project_manager.get_processing_error()
                    else {}
                ),
            },
        }

    except Exception as exc:
        error_msg = f"Error configuring service: {exc}"
        logger.error(error_msg)
        progress.fail_project(error_msg)
        return {"service": service_name, "target": target, "status": "failed", "error": error_msg}
    finally:
        if project_manager:
            await project_manager.close()


async def handle_configure_service_values(payload: dict, progress: Any) -> dict:
    """Add, patch, delete or clear the values a service owns on one component (RC-55).

    The sibling of ``handle_configure_service`` for ``user-env-vars`` and ``aliases``,
    which own a plain component property rather than a block in a ``services:`` list.
    Write first, then reconcile -- and skip the reconcile when the write changed
    nothing, exactly as a clear that removed nothing does.

    Expected payload keys:
        project_name, service, target, operation, component, deployment, values, keys,
        public, rollout
    """
    from opi.manager.project_manager import ProjectManager
    from opi.utils.project_utils import validate_project_name

    project_name: str = payload["project_name"]
    service_name: str = payload["service"]
    target: str = payload["target"]
    operation: str = payload["operation"]
    component_name: str = payload["component"]
    deployment_name: str | None = payload.get("deployment")
    values: dict[str, str] | None = payload.get("values")
    keys: list[str] | None = payload.get("keys")
    public: list[str] | None = payload.get("public")
    project_manager: ProjectManager | None = None

    def failure(message: str, error_type: str = "unknown") -> dict:
        return {
            "status": "failed",
            "service": service_name,
            "target": target,
            "component": component_name,
            "deployment": deployment_name,
            "operation": operation,
            "error": message,
            "error_type": error_type,
        }

    try:
        validate_task = progress.add_task("Waarden-validatie")
        progress.update_current_step("Projectnaam controleren")
        if not validate_project_name(project_name):
            error_msg = (
                "Invalid project name format. Must start with lowercase letter, "
                "then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters"
            )
            progress.fail_task(validate_task, error_msg)
            progress.fail_project(error_msg)
            return failure(error_msg, "invalid_project_name")
        progress.complete_task(validate_task)

        write_task = progress.add_task("Waarden schrijven")
        progress.update_current_step(f"{operation} van de waarden van '{service_name}' toepassen op {target}")
        project_file_relative_path = f"projects/{project_name}.yaml"
        project_manager = ProjectManager(project_file_relative_path=project_file_relative_path)

        result = await project_manager.set_component_values(
            service_name,
            target,
            operation,
            component_name=component_name,
            deployment_name=deployment_name,
            values=values,
            keys=keys,
            public=public,
        )
        if not result["success"]:
            error_msg = result.get("error", "Unknown error writing service values")
            progress.fail_task(write_task, error_msg)
            progress.fail_project(error_msg)
            return failure(error_msg, result.get("error_type", "unknown"))
        progress.complete_task(write_task)

        changed = bool(result.get("changed"))
        processing: dict[str, Any] = {"status": "skipped"}
        if not rollout_requested(payload):
            note_rollout_skipped(progress)
            processing = skipped_processing()
        elif changed:
            deploy_task = progress.add_task("Project verwerken")
            progress.update_current_step("Project verwerken om de nieuwe waarden bij te trekken")
            processing_success = await project_manager.process_project_from_git(
                project_file_relative_path, task_progress_manager=progress
            )
            if processing_success:
                processing = {"status": "completed"}
                progress.complete_task(deploy_task)
            else:
                processing = {"status": "failed"}
                processing_error = project_manager.get_processing_error() or "Project processing failed"
                progress.fail_task(deploy_task, processing_error)
                progress.fail_project(processing_error)

        succeeded = processing["status"] != "failed"
        return {
            "status": "success" if succeeded else "failed",
            "service": service_name,
            "target": target,
            "component": component_name,
            "deployment": deployment_name,
            "operation": operation,
            "changed": changed,
            "processing": {
                **processing,
                **(
                    {"error": project_manager.get_processing_error()}
                    if not succeeded and project_manager.get_processing_error()
                    else {}
                ),
            },
        }

    except Exception:
        # Never the exception's own text: a failure in this path can carry a decrypted
        # value, and this message reaches the caller and the log.
        logger.exception("Error applying %s of '%s' values in project '%s'", operation, service_name, project_name)
        progress.fail_project("An internal error occurred")
        return failure("An internal error occurred", "internal_error")
    finally:
        if project_manager:
            await project_manager.close()


async def handle_manage_database_schemas(payload: dict, progress: Any) -> dict:
    """Add or remove one extra database schema (RC-59).

    Write first, then reconcile, like its two siblings above -- and skip the reconcile
    when the write changed nothing, which here is a removal of a schema that was already
    marked. The reconcile is what creates the schema in every deployment's database and
    exposes its ``DATABASE_SCHEMA_{POSTFIX}`` variable, so it is not optional for an add.

    Expected payload keys:
        project_name, operation, postfix, description, forget, rollout
    """
    from opi.manager.project_manager import ProjectManager
    from opi.utils.project_utils import validate_project_name

    project_name: str = payload["project_name"]
    operation: str = payload["operation"]
    postfix: str = payload["postfix"]
    description: str = payload.get("description", "")
    forget: bool = bool(payload.get("forget", False))
    project_manager: ProjectManager | None = None

    def failure(message: str, error_type: str = "unknown") -> dict:
        return {
            "status": "failed",
            "postfix": postfix,
            "operation": operation,
            "error": message,
            "error_type": error_type,
        }

    try:
        validate_task = progress.add_task("Schema-validatie")
        progress.update_current_step("Projectnaam controleren")
        if not validate_project_name(project_name):
            error_msg = (
                "Invalid project name format. Must start with lowercase letter, "
                "then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters"
            )
            progress.fail_task(validate_task, error_msg)
            progress.fail_project(error_msg)
            return failure(error_msg, "invalid_project_name")
        progress.complete_task(validate_task)

        write_task = progress.add_task("Schema schrijven")
        progress.update_current_step(f"{operation} van schema '{postfix}' toepassen")
        project_file_relative_path = f"projects/{project_name}.yaml"
        project_manager = ProjectManager(project_file_relative_path=project_file_relative_path)

        result = await project_manager.manage_database_schemas(
            operation, postfix, description=description, forget=forget
        )
        if not result["success"]:
            error_msg = result.get("error", "Unknown error managing database schema")
            progress.fail_task(write_task, error_msg)
            progress.fail_project(error_msg)
            return failure(error_msg, result.get("error_type", "unknown"))
        progress.complete_task(write_task)

        changed = bool(result.get("changed"))
        processing: dict[str, Any] = {"status": "skipped"}
        if not rollout_requested(payload):
            note_rollout_skipped(progress)
            processing = skipped_processing()
        elif changed:
            deploy_task = progress.add_task("Project verwerken")
            progress.update_current_step("Project verwerken om de schemalijst bij te trekken")
            processing_success = await project_manager.process_project_from_git(
                project_file_relative_path, task_progress_manager=progress
            )
            if processing_success:
                processing = {"status": "completed"}
                progress.complete_task(deploy_task)
            else:
                processing = {"status": "failed"}
                processing_error = project_manager.get_processing_error() or "Project processing failed"
                progress.fail_task(deploy_task, processing_error)
                progress.fail_project(processing_error)

        succeeded = processing["status"] != "failed"
        return {
            "status": "success" if succeeded else "failed",
            "postfix": postfix,
            "operation": operation,
            "changed": changed,
            "created": result.get("created"),
            "restored": result.get("restored"),
            "marked": result.get("marked"),
            "forgotten": result.get("forgotten"),
            "processing": {
                **processing,
                **(
                    {"error": project_manager.get_processing_error()}
                    if not succeeded and project_manager.get_processing_error()
                    else {}
                ),
            },
        }

    except Exception as exc:
        error_msg = f"Error managing database schema: {exc}"
        logger.error(error_msg)
        progress.fail_project(error_msg)
        return failure(error_msg, "internal_error")
    finally:
        if project_manager:
            await project_manager.close()


async def handle_delete_component(payload: dict, progress: Any) -> dict:
    """Handle async component deletion task.

    Two steps that always belonged together: remove the component from the project file,
    then reprocess the project so the deletion actually lands in the cluster. The web
    endpoint used to do the first inline and queue the second, so the dialog reported
    success while the real work had not started.

    Expected payload keys:
        project_name: Name of the project
        component_name: Name of the component to remove
        confirm_in_use: Optional; remove the deployment entries and dependency declarations
            naming this component along with it. Absent means no, and a component that is
            still referenced then fails the task instead of being deleted.
    """
    from opi.core.task_handlers_operations import handle_refresh_project
    from opi.manager.project_manager import ProjectManager

    project_name: str = payload["project_name"]
    component_name: str = payload["component_name"]
    confirm_in_use: bool = bool(payload.get("confirm_in_use", False))

    logger.info(f"Task: deleting component {component_name} from {project_name}")

    remove_task = progress.add_task(f"Component '{component_name}' verwijderen")
    # Mutate through the single ProjectManager path: it reads fresh contents from Git,
    # then saves and commits, so a lagging read cache can never overwrite newer Git state.
    project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")
    try:
        result = await project_manager.delete_component(component_name, confirm_in_use=confirm_in_use)
    finally:
        await project_manager.close()

    if not result["success"]:
        error_msg = result["error"]
        progress.fail_task(remove_task, error_msg)
        progress.fail_project(error_msg)
        raise RuntimeError(error_msg)

    progress.complete_task(remove_task)

    # delete_component already refreshed the read cache via save_and_commit_project;
    # reprocess from Git to apply the deletion.
    refresh_result = await handle_refresh_project({"project_name": project_name, "force_clone": True}, progress)

    # A reprocess that failed must stay visible: it reports failure by returning, not by
    # raising, so wrapping it in a fixed "completed" would swallow it.
    failed = isinstance(refresh_result, dict) and refresh_result.get("status") == "failed"
    return {
        "status": "failed" if failed else "completed",
        "message": (
            refresh_result.get("message", "Herverwerken mislukt")
            if failed
            else f"Component '{component_name}' succesvol verwijderd"
        ),
        "project": project_name,
        "component": component_name,
        # What went with it, so a caller who confirmed the deletion learns which deployments
        # and dependency declarations were changed instead of only that the component is gone.
        "uncoupled_from": result.get("uncoupled_from", []),
        "processing": refresh_result.get("processing") if isinstance(refresh_result, dict) else None,
    }
