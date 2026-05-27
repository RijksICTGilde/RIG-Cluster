"""
Simple background task processor using the new TaskProgressManager.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

from opi.api.router import generate_self_service_project_yaml, validate_project_name
from opi.connectors.git import GitConnector
from opi.core.config import settings
from opi.core.task_manager import TaskProgressManager, TaskStatus, _projects
from opi.manager.project_manager import ProjectManager

logger = logging.getLogger(__name__)


async def _monitor_argocd_and_deployment(
    _task_id: str, project_name: str, task_progress_manager: TaskProgressManager, monitor_task: str
) -> None:
    """Monitor the project's own ArgoCD applications until they are synced and healthy.

    The ``user-applications`` App-of-Apps creates individual ArgoCD
    Application resources for each project deployment (e.g.
    ``myproject-staging``).  Those project apps are what we care about -
    the parent ``user-applications`` syncs on its own schedule and may
    stay ``OutOfSync`` long after the project apps are ready.
    """
    try:
        logger.info(f"Starting ArgoCD monitoring for project: {project_name}")

        from opi.connectors.argo import create_argo_connector

        argo_connector = create_argo_connector()

        # Give ArgoCD time to pick up the git changes and create the project apps
        await asyncio.sleep(5)

        argo_subtask = task_progress_manager.add_subtask(monitor_task, "Wachten op ArgoCD sync voltooiing")

        # Allow the triggered syncs to propagate
        await asyncio.sleep(8)

        max_retries = 15  # Wait up to ~30 seconds for project apps
        argo_synced = False

        for attempt in range(max_retries):
            try:
                logger.debug(f"Checking project ArgoCD apps, attempt {attempt + 1}/{max_retries}")

                all_apps = await argo_connector.list_applications()
                project_apps = [
                    app for app in all_apps if app.get("metadata", {}).get("name", "").startswith(f"{project_name}-")
                ]

                if not project_apps:
                    logger.debug(f"No ArgoCD apps found yet for {project_name}")
                    await asyncio.sleep(2)
                    continue

                logger.debug(f"Found {len(project_apps)} app(s) for {project_name}")

                all_healthy = True
                for app in project_apps:
                    app_name = app.get("metadata", {}).get("name", "")
                    app_sync = app.get("status", {}).get("sync", {}).get("status", "Unknown")
                    app_health = app.get("status", {}).get("health", {}).get("status", "Unknown")
                    logger.debug(f"  {app_name} - Sync: {app_sync}, Health: {app_health}")

                    if not (app_sync == "Synced" and app_health in ["Healthy", "Progressing"]):
                        all_healthy = False
                        break

                if all_healthy:
                    argo_synced = True
                    app_names = [app.get("metadata", {}).get("name", "") for app in project_apps]
                    logger.info(f"All project ArgoCD apps healthy for {project_name}: {app_names}")
                    break

                await asyncio.sleep(2)

            except Exception as e:
                logger.warning(f"Error checking ArgoCD apps (attempt {attempt + 1}): {e}")
                await asyncio.sleep(2)

        if argo_synced:
            task_progress_manager.complete_task(argo_subtask)
        else:
            logger.warning(f"ArgoCD sync timeout - project apps for {project_name} not ready")
            task_progress_manager.fail_task(
                argo_subtask,
                f"ArgoCD sync timeout - project apps for {project_name} not synced",
            )

        task_progress_manager.complete_task(monitor_task)

    except Exception as e:
        logger.error(f"Error monitoring ArgoCD for {project_name}: {e}")
        task_progress_manager.fail_task(monitor_task, f"Monitoring failed: {e}")


async def process_project_background(task_id: str, project_data: Any) -> None:
    """
    Simple background task function that processes a project creation request.
    Uses the new TaskProgressManager for clean task tracking.
    """
    try:
        logger.info(f"Background task {task_id} starting for project: {project_data.project_name}")
        start_time = time.time()

        # Create simple task progress manager (project was already created by create_task)
        task_progress_manager = TaskProgressManager(task_id, project_data.project_name)

        # Step 1: Validation
        validate_task = task_progress_manager.add_task("Project validatie")
        logger.debug(f"Task {task_id}: Validating project name: {project_data.project_name}")
        if not validate_project_name(project_data.project_name):
            error_msg = f"Invalid project name format: {project_data.project_name}"
            task_progress_manager.fail_task(validate_task, error_msg)
            task_progress_manager.fail_project(error_msg)
            return
        task_progress_manager.complete_task(validate_task)

        # Step 2: YAML generation
        yaml_task = task_progress_manager.add_task("YAML configuratie genereren")
        try:
            yaml_content = await generate_self_service_project_yaml(project_data)
            logger.debug(f"Task {task_id}: Generated YAML content ({len(yaml_content)} chars)")
            task_progress_manager.complete_task(yaml_task)
        except Exception as e:
            error_msg = f"Failed to generate YAML: {e}"
            task_progress_manager.fail_task(yaml_task, error_msg)
            task_progress_manager.fail_project(error_msg)
            return

        # Step 3: Git operations
        git_task = task_progress_manager.add_task("Git repository operaties")
        try:
            # Create Git connector for projects repository
            git_connector_for_project_files = GitConnector(
                repo_url=settings.GIT_PROJECTS_SERVER_URL,
                username=settings.GIT_PROJECTS_SERVER_USERNAME,
                password=settings.GIT_PROJECTS_SERVER_PASSWORD,
                branch=settings.GIT_PROJECTS_SERVER_BRANCH,
                repo_path=settings.GIT_PROJECTS_SERVER_REPO_PATH,
            )

            # Create project file in Git repository and commit immediately
            # This ensures the project configuration is preserved even if deployment fails
            project_file_path = f"projects/{project_data.project_name}.yaml"
            commit_message = f"Create project {project_data.project_name}"

            # Tenant-isolation guard: this is the create path, so a project
            # file with this name must not already exist. Without this check
            # a tenant could pick another tenant's project name and silently
            # overwrite their project file, taking over ownership on reload.
            if await git_connector_for_project_files.file_exists(project_file_path):
                error_msg = (
                    f"Project '{project_data.project_name}' bestaat al. "
                    f"Kies een andere projectnaam; een bestaand project kan niet "
                    f"via aanmaken worden overschreven."
                )
                task_progress_manager.fail_task(git_task, error_msg)
                task_progress_manager.fail_project(error_msg)
                return

            await git_connector_for_project_files.create_or_update_file(
                project_file_path, yaml_content, do_commit_and_push=True, commit_message=commit_message
            )
            logger.info(f"Task {task_id}: Project file created and pushed at {project_file_path}")
            task_progress_manager.complete_task(git_task)
        except Exception as e:
            error_msg = f"Failed Git operations: {e}"
            task_progress_manager.fail_task(git_task, error_msg)
            task_progress_manager.fail_project(error_msg)
            return

        # Step 4: Project deployment with detailed service subtasks
        deploy_task = task_progress_manager.add_task("Project deployment")
        try:
            # Add infrastructure creation subtasks based on common services
            infra_subtasks = []

            # Kubernetes namespace
            # namespace_task = task_progress_manager.add_subtask(deploy_task, "Kubernetes namespace aanmaken")
            # await asyncio.sleep(0.5)  # Simulate work
            # task_progress_manager.complete_task(namespace_task)
            #
            # # PostgreSQL database
            # postgres_task = task_progress_manager.add_subtask(deploy_task, "PostgreSQL database instellen")
            # await asyncio.sleep(1.0)  # Simulate work
            # task_progress_manager.complete_task(postgres_task)
            #
            # # Keycloak SSO
            # keycloak_task = task_progress_manager.add_subtask(deploy_task, "Keycloak SSO configureren")
            # await asyncio.sleep(1.5)  # Simulate work
            # task_progress_manager.complete_task(keycloak_task)
            #
            # # MinIO Storage
            # minio_task = task_progress_manager.add_subtask(deploy_task, "MinIO object storage opzetten")
            # await asyncio.sleep(1.0)  # Simulate work
            # task_progress_manager.complete_task(minio_task)
            #
            # # Vault secrets
            # vault_task = task_progress_manager.add_subtask(deploy_task, "HashiCorp Vault secrets beheer")
            # await asyncio.sleep(0.8)  # Simulate work
            # task_progress_manager.complete_task(vault_task)
            #
            # # ArgoCD GitOps
            # argo_task = task_progress_manager.add_subtask(deploy_task, "ArgoCD GitOps deployment")
            # await asyncio.sleep(1.2)  # Simulate work
            # task_progress_manager.complete_task(argo_task)
            #
            # # Ingress and networking
            # ingress_task = task_progress_manager.add_subtask(deploy_task, "Ingress en networking configureren")
            # await asyncio.sleep(0.7)  # Simulate work
            # task_progress_manager.complete_task(ingress_task)

            # Create project manager and process the project
            project_manager = ProjectManager(git_connector_for_project_files=git_connector_for_project_files)
            try:
                # Simple deployment - just process the project
                processing_result = await project_manager.process_project_from_git(
                    project_file_path, task_progress_manager
                )
                logger.info(f"Task {task_id}: Project processing completed, result: {processing_result}")
            finally:
                await project_manager.close()

            if processing_result:
                # Set the namespace for monitoring (assuming project name as namespace)

                # Start monitoring ArgoCD sync and deployment status
                monitor_task = task_progress_manager.add_subtask(deploy_task, "ArgoCD & deployment monitoring")
                await _monitor_argocd_and_deployment(
                    task_id, project_data.project_name, task_progress_manager, monitor_task
                )

                task_progress_manager.complete_task(deploy_task)
                task_progress_manager.update_current_step(
                    f"Project {project_data.project_name} succesvol geimplementeerd"
                )
                task_progress_manager.complete_project()

                # Calculate final result
                elapsed_time = time.time() - start_time
                result = {
                    "project_name": project_data.project_name,
                    "project_description": getattr(project_data, "project_description", "No description"),
                    "components_count": len(getattr(project_data, "components", [])),
                    "elapsed_time": f"{elapsed_time:.2f}",
                    "file_path": project_file_path,
                    "status": "success",
                }

                logger.info(
                    f"Background task {task_id} completed successfully: {project_data.project_name} (took {elapsed_time:.2f} seconds)"
                )
            else:
                error_msg = "Project processing failed"
                task_progress_manager.fail_task(deploy_task, error_msg)
                task_progress_manager.fail_project(error_msg)

        except Exception as e:
            error_msg = f"Failed deployment: {e}"
            task_progress_manager.fail_task(deploy_task, error_msg)
            task_progress_manager.fail_project(error_msg)
            return

    except Exception as e:
        import traceback

        error_traceback = traceback.format_exc()
        error_msg = f"Error processing project: {e!s}"
        logger.error(f"Background task {task_id} failed: {error_msg}")
        logger.debug(f"Background task {task_id} full traceback:\n{error_traceback}")

        # Mark project as failed
        if task_id in _projects:
            _projects[task_id].status = TaskStatus.FAILED
            _projects[task_id].completed_at = datetime.now(tz=UTC)


async def process_project_yaml_background(
    task_id: str,
    project_name: str,
    yaml_content: str,
    deployment_name: str | None = None,
    is_new_project: bool = False,
) -> None:
    """Background task that creates a project from pre-built wizard YAML.

    Same pipeline as ``process_project_background``.

    Args:
        is_new_project: True for the create flow only. When True the file
            must not already exist (or one tenant overwrites another).
            Update/edit flows pass False.
    """
    try:
        logger.info(f"Background task {task_id} starting for project: {project_name} (from wizard YAML)")
        start_time = time.time()

        task_progress_manager = TaskProgressManager(task_id, project_name)

        # Step 1: Validation
        validate_task = task_progress_manager.add_task("Project validatie")
        if not validate_project_name(project_name):
            error_msg = f"Invalid project name format: {project_name}"
            task_progress_manager.fail_task(validate_task, error_msg)
            task_progress_manager.fail_project(error_msg)
            return
        task_progress_manager.complete_task(validate_task)

        # Step 2: YAML already generated - skip generation step
        yaml_task = task_progress_manager.add_task("YAML configuratie controleren")
        logger.debug(f"Task {task_id}: Using pre-built YAML content ({len(yaml_content)} chars)")
        task_progress_manager.complete_task(yaml_task)

        # Step 3: Git operations
        git_task = task_progress_manager.add_task("Git repository operaties")
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

            # Project-create flow: refuse if a project file with this name
            # already exists. Without this a tenant could reuse another
            # tenant's name and take over their project on the next reload.
            if is_new_project and await git_connector_for_project_files.file_exists(project_file_path):
                error_msg = (
                    f"Project '{project_name}' bestaat al. "
                    f"Kies een andere projectnaam; een bestaand project kan niet "
                    f"via aanmaken worden overschreven."
                )
                task_progress_manager.fail_task(git_task, error_msg)
                task_progress_manager.fail_project(error_msg)
                return

            await git_connector_for_project_files.create_or_update_file(
                project_file_path, yaml_content, do_commit_and_push=True, commit_message=commit_message
            )
            logger.info(f"Task {task_id}: Project file created and pushed at {project_file_path}")
            task_progress_manager.complete_task(git_task)
        except Exception as e:
            error_msg = f"Failed Git operations: {e}"
            task_progress_manager.fail_task(git_task, error_msg)
            task_progress_manager.fail_project(error_msg)
            return

        # Step 4: Project deployment
        if deployment_name:
            deploy_task = task_progress_manager.add_task(f"Deployment '{deployment_name}' verwerken")
        else:
            deploy_task = task_progress_manager.add_task("Project deployment")
        try:
            project_manager = ProjectManager(git_connector_for_project_files=git_connector_for_project_files)
            try:
                processing_result = await project_manager.process_project_from_git(
                    project_file_path, task_progress_manager, deployment_name=deployment_name
                )
                logger.info(f"Task {task_id}: Project processing completed, result: {processing_result}")
            finally:
                await project_manager.close()

            if processing_result:
                task_progress_manager.complete_task(deploy_task)
                task_progress_manager.update_current_step(f"Project {project_name} succesvol geimplementeerd")
                task_progress_manager.complete_project()

                elapsed_time = time.time() - start_time
                logger.info(
                    f"Background task {task_id} completed successfully: {project_name} (took {elapsed_time:.2f}s)"
                )
            else:
                error_msg = "Deployment processing failed"
                task_progress_manager.fail_task(deploy_task, error_msg)
                task_progress_manager.fail_project(error_msg)

        except Exception as e:
            error_msg = f"Failed deployment: {e}"
            task_progress_manager.fail_task(deploy_task, error_msg)
            task_progress_manager.fail_project(error_msg)
            return

    except Exception as e:
        import traceback

        error_traceback = traceback.format_exc()
        error_msg = f"Error processing project: {e!s}"
        logger.error(f"Background task {task_id} failed: {error_msg}")
        logger.debug(f"Background task {task_id} full traceback:\n{error_traceback}")

        if task_id in _projects:
            _projects[task_id].status = TaskStatus.FAILED
            _projects[task_id].completed_at = datetime.now(tz=UTC)
