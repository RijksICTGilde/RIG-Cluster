"""Task handlers for project creation and upsert deployment.

These handlers are called by TaskWorker. Each receives a payload dict and a
PersistentTaskProgressManager instance, and returns a result dict.

They have NO dependency on FastAPI Request objects.
"""

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


async def handle_create_project(payload: dict, progress: Any) -> dict:
    """Handle async project creation task.

    Extracted from simple_background.process_project_background() and
    process_project_yaml_background().  Supports two paths:

    1. Full generation -- payload contains SelfServiceProjectRequest fields
       (project_name, display_name, components, etc.) and the handler
       generates YAML via generate_self_service_project_yaml().
    2. Pre-built YAML -- payload contains ``yaml_content`` directly (produced
       by the wizard with editables and generators).  YAML generation is
       skipped.
    """
    from opi.connectors.git import GitConnector
    from opi.core.config import settings
    from opi.core.simple_background import _monitor_argocd_and_deployment
    from opi.manager.project_manager import ProjectManager
    from opi.utils.project_utils import generate_self_service_project_yaml, validate_project_name

    start_time = time.time()
    project_name: str = payload["project_name"]
    pre_built_yaml: str | None = payload.get("yaml_content")

    # ------------------------------------------------------------------
    # Step 1: Validation
    # ------------------------------------------------------------------
    validate_task = progress.add_task("Project validatie")
    progress.update_current_step("Validating project name")

    if not validate_project_name(project_name):
        error_msg = f"Invalid project name format: {project_name}"
        progress.fail_task(validate_task, error_msg)
        progress.fail_project(error_msg)
        return {"project_name": project_name, "status": "failed", "error": error_msg}

    progress.complete_task(validate_task)

    # ------------------------------------------------------------------
    # Step 2: YAML content
    # ------------------------------------------------------------------
    if pre_built_yaml is not None:
        # Path 2: wizard-provided YAML -- just verify we have content.
        yaml_task = progress.add_task("YAML configuratie controleren")
        logger.debug(
            "Using pre-built YAML content for %s (%d chars)",
            project_name,
            len(pre_built_yaml),
        )
        yaml_content = pre_built_yaml
        progress.complete_task(yaml_task)
    else:
        # Path 1: generate YAML from the request payload.
        yaml_task = progress.add_task("YAML configuratie genereren")
        try:
            # generate_self_service_project_yaml expects an object with attributes.
            # Build a lightweight namespace from the payload so it can use attribute
            # access (the generator uses getattr / dot-notation).
            from types import SimpleNamespace

            project_data = SimpleNamespace(**payload)
            yaml_content = await generate_self_service_project_yaml(project_data)
            logger.debug(
                "Generated YAML content for %s (%d chars)",
                project_name,
                len(yaml_content),
            )
            progress.complete_task(yaml_task)
        except Exception as exc:
            error_msg = f"Failed to generate YAML: {exc}"
            progress.fail_task(yaml_task, error_msg)
            progress.fail_project(error_msg)
            return {"project_name": project_name, "status": "failed", "error": error_msg}

    # ------------------------------------------------------------------
    # Step 3: Git operations
    # ------------------------------------------------------------------
    git_task = progress.add_task("Git repository operaties")
    progress.update_current_step("Pushing project file to Git")

    try:
        git_connector_for_project_files = GitConnector(
            repo_url=settings.GIT_PROJECTS_SERVER_URL,
            username=settings.GIT_PROJECTS_SERVER_USERNAME,
            password=settings.GIT_PROJECTS_SERVER_PASSWORD,
            branch=settings.GIT_PROJECTS_SERVER_BRANCH,
            repo_path=settings.GIT_PROJECTS_SERVER_REPO_PATH,
        )

        project_file_path = f"projects/{project_name}.yaml"
        commit_message = f"Create project {project_name}"
        await git_connector_for_project_files.create_or_update_file(
            project_file_path,
            yaml_content,
            do_commit_and_push=True,
            commit_message=commit_message,
        )
        logger.info("Project file created and pushed at %s", project_file_path)
        progress.complete_task(git_task)
    except Exception as exc:
        error_msg = f"Failed Git operations: {exc}"
        progress.fail_task(git_task, error_msg)
        progress.fail_project(error_msg)
        return {"project_name": project_name, "status": "failed", "error": error_msg}

    # ------------------------------------------------------------------
    # Step 4: Project deployment
    # ------------------------------------------------------------------
    deploy_task = progress.add_task("Project deployment")
    progress.update_current_step("Deploying project")

    try:
        project_manager = ProjectManager(
            git_connector_for_project_files=git_connector_for_project_files,
        )
        try:
            processing_result = await project_manager.process_project_from_git(project_file_path, progress)
            logger.info("Project processing completed, result: %s", processing_result)
        finally:
            await project_manager.close()

        if processing_result:
            # ArgoCD monitoring
            monitor_task = progress.add_subtask(deploy_task, "ArgoCD & deployment monitoring")
            await _monitor_argocd_and_deployment(
                task_id="",  # not used meaningfully by the monitor helper
                project_name=project_name,
                task_progress_manager=progress,
                monitor_task=monitor_task,
            )

            progress.complete_task(deploy_task)

            # Schedule fire-and-forget OOM watcher for each deployment
            from opi.core.config import settings as app_settings
            from opi.services.oom_watcher import schedule_oom_check

            if app_settings.OOM_WATCHER_ENABLED:
                from io import StringIO as _StringIO

                from ruamel.yaml import YAML as RuamelYAML

                _yaml_parser = RuamelYAML()
                parsed_data = _yaml_parser.load(_StringIO(yaml_content))
                if parsed_data and isinstance(parsed_data, dict):
                    for dep in parsed_data.get("deployments", []):
                        dep_name = dep.get("name", "")
                        if dep_name:
                            schedule_oom_check(project_name, dep_name)

            progress.update_current_step(f"Project {project_name} succesvol geimplementeerd")
            progress.complete_project()

            elapsed_time = time.time() - start_time
            result: dict[str, Any] = {
                "project_name": project_name,
                "project_description": payload.get("project_description", "No description"),
                "components_count": len(payload.get("components", [])),
                "elapsed_time": f"{elapsed_time:.2f}",
                "file_path": project_file_path,
                "status": "success",
            }
            logger.info(
                "Project creation completed successfully: %s (took %.2fs)",
                project_name,
                elapsed_time,
            )
            return result
        else:
            error_msg = project_manager.get_processing_error() or "Project processing failed"
            progress.fail_task(deploy_task, error_msg)
            progress.fail_project(error_msg)
            return {"project_name": project_name, "status": "failed", "error": error_msg}

    except Exception as exc:
        error_msg = f"Failed deployment: {exc}"
        progress.fail_task(deploy_task, error_msg)
        progress.fail_project(error_msg)
        return {"project_name": project_name, "status": "failed", "error": error_msg}


async def handle_upsert_deployment(payload: dict, progress: Any) -> dict:
    """Handle async upsert deployment task.

    Extracted from router.upsert_deployment().
    """
    from opi.manager.project_manager import ProjectManager
    from opi.utils.naming import sanitize_kubernetes_name
    from opi.utils.project_utils import normalize_container_image, validate_project_name

    project_name: str = payload["project_name"]
    deployment_name: str = payload["deployment_name"]
    components: list[dict[str, str]] = payload["components"]
    clone_from: str | None = payload.get("clone_from") or payload.get("cloneFrom")
    force_clone: bool = payload.get("force_clone") or payload.get("forceClone", False)
    domain_format: str | None = payload.get("domain_format")
    subdomain: str | None = payload.get("subdomain")
    base_domain: str | None = payload.get("base_domain")

    project_manager: ProjectManager | None = None

    try:
        # ------------------------------------------------------------------
        # Step 1: Validation
        # ------------------------------------------------------------------
        validate_task = progress.add_task("Deployment validatie")
        progress.update_current_step("Validating project and deployment names")

        if not validate_project_name(project_name):
            error_msg = (
                "Invalid project name format. Must start with lowercase letter, "
                "then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters"
            )
            progress.fail_task(validate_task, error_msg)
            progress.fail_project(error_msg)
            return {
                "deployment_name": deployment_name,
                "status": "failed",
                "error": error_msg,
            }

        sanitized_name = sanitize_kubernetes_name(deployment_name)
        if sanitized_name != deployment_name.lower():
            error_msg = (
                f"Invalid deployment name. Use lowercase letters, numbers, and "
                f"hyphens only. Suggested: {sanitized_name}"
            )
            progress.fail_task(validate_task, error_msg)
            progress.fail_project(error_msg)
            return {
                "deployment_name": deployment_name,
                "status": "failed",
                "error": error_msg,
            }

        progress.complete_task(validate_task)

        # ------------------------------------------------------------------
        # Step 2: Upsert deployment in project YAML
        # ------------------------------------------------------------------
        upsert_task = progress.add_task("Deployment upsert")
        progress.update_current_step(f"Upserting deployment '{deployment_name}'")

        project_file_relative_path = f"projects/{project_name}.yaml"
        project_manager = ProjectManager(
            project_file_relative_path=project_file_relative_path,
        )

        # Build component dicts in the shape expected by project_manager
        from types import SimpleNamespace

        component_objects = [SimpleNamespace(reference=c["reference"], image=c["image"]) for c in components]

        result = await project_manager.upsert_deployment(
            deployment_name=deployment_name,
            components=component_objects,
            clone_from=clone_from,
            force_clone=force_clone,
            domain_format=domain_format,
            subdomain=subdomain,
            base_domain=base_domain,
        )

        if not result["success"]:
            error_msg = result.get("error", "Unknown upsert error")
            progress.fail_task(upsert_task, error_msg)
            progress.fail_project(error_msg)
            return {
                "deployment_name": deployment_name,
                "status": "failed",
                "error": error_msg,
                "error_type": result.get("error_type", "unknown"),
            }

        progress.complete_task(upsert_task)

        # ------------------------------------------------------------------
        # Step 3: Process deployment
        # ------------------------------------------------------------------
        deploy_task = progress.add_task("Deployment processing")
        progress.update_current_step(f"Processing deployment '{deployment_name}'")

        # ArgoCD Application/AppProject resources only change when a new
        # deployment is created.  Image updates don't touch ArgoCD resources.
        is_new_deployment = result.get("created", False)

        processing_result = await project_manager.process_project_from_git(
            project_file_relative_path,
            task_progress_manager=progress,
            deployment_name=deployment_name,
            force_clone=force_clone,
            argocd_resources_changed=is_new_deployment,
        )

        action = "created" if is_new_deployment else "updated"

        # Collect URLs from deployment results
        urls: dict[str, dict[str, Any]] = {}
        deployment_results = project_manager.get_deployment_results(deployment_name)
        for dep_name, dep_result in deployment_results.items():
            urls[dep_name] = {
                "cluster": dep_result.cluster,
                "urls": dep_result.urls,
            }
            # Report web addresses for each component URL
            for url_name, url_value in (dep_result.urls or {}).items():
                progress.update_component_web_address(url_name, url_value)

        if processing_result:
            progress.complete_task(deploy_task)

            # Schedule fire-and-forget OOM watcher
            from opi.core.config import settings
            from opi.services.oom_watcher import schedule_oom_check

            if settings.OOM_WATCHER_ENABLED:
                oom_attempt = payload.get("oom_watch_attempt", 1)
                schedule_oom_check(
                    project_name,
                    deployment_name,
                    attempt=oom_attempt,
                )
        else:
            processing_error = project_manager.get_processing_error() or "Deployment processing failed"
            progress.fail_task(deploy_task, processing_error)
            progress.fail_project(processing_error)

        # ------------------------------------------------------------------
        # Build response
        # ------------------------------------------------------------------
        succeeded = bool(processing_result)
        response: dict[str, Any] = {
            "status": "success" if succeeded else "failed",
            "message": (
                f"Deployment '{deployment_name}' {action} successfully"
                if succeeded
                else (project_manager.get_processing_error() or f"Deployment '{deployment_name}' processing failed")
            ),
            "deployment": {
                "name": deployment_name,
                "project": project_name,
                "components": [
                    {
                        "reference": c["reference"],
                        "image": normalize_container_image(c["image"])[0],
                    }
                    for c in components
                ],
                "forceClone": force_clone,
                "created": result.get("created", False),
            },
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
        error_msg = f"Error upserting deployment: {exc}"
        logger.error(error_msg)
        progress.fail_project(error_msg)
        return {
            "deployment_name": deployment_name,
            "status": "failed",
            "error": error_msg,
        }
    finally:
        if project_manager:
            await project_manager.close()
