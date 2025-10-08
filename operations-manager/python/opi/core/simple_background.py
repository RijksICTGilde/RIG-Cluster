"""
Simple background task processor using the new TaskProgressManager.
"""

import asyncio
import logging
import time
from typing import Any

from opi.api.router import generate_self_service_project_yaml, validate_project_name
from opi.connectors.git import GitConnector
from opi.core.config import settings
from opi.core.task_manager import TaskProgressManager, TaskStatus, _projects
from opi.manager.project_manager import ProjectManager

logger = logging.getLogger(__name__)


async def _continuous_monitoring(task_id: str, project_name: str) -> None:
    """
    Continuously monitor namespace for updated events and logs.

    Runs in the background to keep updating the monitoring data every 30 seconds.
    """
    try:
        logger.info(f"Starting continuous monitoring for project: {project_name}")

        from opi.connectors.kubectl import KubectlConnector

        kubectl_connector = KubectlConnector()

        # Run monitoring loop for up to 10 minutes (120 cycles of 5 seconds)
        for cycle in range(120):
            await asyncio.sleep(5)  # Wait 5 seconds between checks

            try:
                logger.debug(f"Continuous monitoring cycle {cycle + 1}/120 for {project_name}")

                # Check if project still exists in our tracking
                if task_id not in _projects:
                    logger.info(f"Project {task_id} no longer tracked, stopping monitoring")
                    break

                # Get updated namespace events and logs
                project = _projects[task_id]
                if project.namespace:
                    namespace_exists = await kubectl_connector.namespace_exists(project.namespace)

                    if namespace_exists:
                        # Get fresh events and logs
                        events = await kubectl_connector.get_namespace_events(project.namespace)

                        # Get logs from all deployments in the namespace
                        logs = []
                        deployment_statuses = await kubectl_connector.get_deployment_status(project.namespace)
                        for deployment in deployment_statuses:
                            deployment_name = deployment.get("name", "")
                            if deployment_name:
                                deployment_logs = await kubectl_connector.get_deployment_logs(
                                    deployment_name, project.namespace, lines=50
                                )
                                if deployment_logs:
                                    logs.extend([f"[{deployment_name}] {log}" for log in deployment_logs[-20:]])
                else:
                    # If no namespace is set yet, skip monitoring this cycle
                    continue

                    # Update project with latest monitoring data
                    if events and len(events) > 0:
                        _projects[task_id].events = events[-20:]  # Keep last 20 events
                        logger.debug(f"Updated {len(events)} events for {project_name}")

                    if logs and len(logs) > 0:
                        _projects[task_id].logs = logs[-50:]  # Keep last 50 log lines
                        logger.debug(f"Updated {len(logs)} log lines for {project_name}")

                    # Update current step to show active monitoring
                    current_time = time.strftime("%H:%M:%S")
                    _projects[
                        task_id
                    ].current_step = f"📡 Live monitoring actief voor {project_name} (laatste update: {current_time})"

            except Exception as e:
                logger.warning(f"Error in continuous monitoring cycle {cycle + 1}: {e}")
                continue  # Continue with next cycle

        logger.info(f"Continuous monitoring completed for project: {project_name}")

    except Exception as e:
        logger.error(f"Error in continuous monitoring for {project_name}: {e}")


async def _monitor_argocd_and_deployment(
    task_id: str, project_name: str, task_progress_manager: TaskProgressManager, monitor_task: str
) -> None:
    """
    Monitor ArgoCD for user-applications sync and then check namespace for events and pod logs.

    This checks if the user application is deployed via ArgoCD and monitors the actual deployment.
    """
    try:
        logger.info(f"Starting ArgoCD monitoring for project: {project_name}")

        # Step 1: Check ArgoCD for user-applications sync
        from opi.connectors.argo import create_argo_connector

        argo_connector = create_argo_connector()

        # Give ArgoCD time to detect new project files (it polls git repos periodically)
        await asyncio.sleep(5)

        # The ProjectManager already triggered ArgoCD syncs, so we just need to wait for completion
        argo_subtask = task_progress_manager.add_subtask(monitor_task, "Wachten op ArgoCD sync voltooiing")

        # Give ArgoCD applications time to sync (ProjectManager already triggered the syncs)
        await asyncio.sleep(8)  # Allow time for the syncs triggered by ProjectManager

        max_argo_retries = 10  # Wait up to 20 seconds for ArgoCD applications
        argo_synced = False

        for attempt in range(max_argo_retries):
            try:
                logger.debug(f"Checking ArgoCD applications status, attempt {attempt + 1}/{max_argo_retries}")

                # Check user-applications first
                user_app_status = await argo_connector.get_application_status("user-applications")
                user_app_healthy = False

                if user_app_status:
                    sync_status = user_app_status.get("status", {}).get("sync", {}).get("status", "Unknown")
                    health_status = user_app_status.get("status", {}).get("health", {}).get("status", "Unknown")

                    logger.debug(f"user-applications - Sync: {sync_status}, Health: {health_status}")
                    user_app_healthy = sync_status in ["Synced"] and health_status in ["Healthy", "Progressing"]

                # Check for project-specific applications (e.g., project_name-app)
                project_apps_healthy = True
                try:
                    # Look for applications starting with the project name
                    all_apps = await argo_connector.list_applications()
                    project_apps = [
                        app
                        for app in all_apps
                        if app.get("metadata", {}).get("name", "").startswith(f"{project_name}-")
                    ]

                    if project_apps:
                        logger.debug(f"Found {len(project_apps)} project applications for {project_name}")
                        for app in project_apps:
                            app_name = app.get("metadata", {}).get("name", "")
                            app_sync = app.get("status", {}).get("sync", {}).get("status", "Unknown")
                            app_health = app.get("status", {}).get("health", {}).get("status", "Unknown")

                            logger.debug(f"{app_name} - Sync: {app_sync}, Health: {app_health}")
                            if not (app_sync in ["Synced"] and app_health in ["Healthy", "Progressing"]):
                                project_apps_healthy = False
                                break
                except Exception as e:
                    logger.debug(f"Could not check project applications: {e}")
                    # If we can't check project apps, just rely on user-applications

                # Consider ArgoCD ready if user-applications is healthy
                if user_app_healthy:
                    argo_synced = True
                    logger.info(f"ArgoCD applications are healthy for project {project_name}")
                    break

                await asyncio.sleep(2)

            except Exception as e:
                logger.warning(f"Error checking ArgoCD applications (attempt {attempt + 1}): {e}")
                await asyncio.sleep(2)

        if argo_synced:
            task_progress_manager.complete_task(argo_subtask)

            # Step 2: Skip namespace monitoring during deployment - it will happen after project creation
        else:
            logger.warning("ArgoCD sync did not complete in time, marking as failed")
            task_progress_manager.fail_task(argo_subtask, "ArgoCD sync timeout - user-applications not synced")

        task_progress_manager.complete_task(monitor_task)

    except Exception as e:
        logger.error(f"Error monitoring ArgoCD and deployment for {project_name}: {e}")
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

            # Create project file in Git repository
            project_file_path = f"projects/{project_data.project_name}.yaml"
            await git_connector_for_project_files.create_or_update_file(project_file_path, yaml_content, False)
            logger.info(f"Task {task_id}: Project file created at {project_file_path}")
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

            # Simple deployment - just process the project
            processing_result = await project_manager.process_project_from_git(project_file_path, task_progress_manager)
            logger.info(f"Task {task_id}: Project processing completed, result: {processing_result}")

            if processing_result:
                # Set the namespace for monitoring (assuming project name as namespace)

                # Start monitoring ArgoCD sync and deployment status
                monitor_task = task_progress_manager.add_subtask(deploy_task, "ArgoCD & deployment monitoring")
                await _monitor_argocd_and_deployment(
                    task_id, project_data.project_name, task_progress_manager, monitor_task
                )

                task_progress_manager.complete_task(deploy_task)
                # Don't mark project as completed - keep it as running for ongoing monitoring
                # task_progress_manager.complete_project()  # Commented out to keep polling active

                # Update project status to indicate deployment is complete but monitoring continues
                if task_id in _projects:
                    _projects[
                        task_id
                    ].current_step = (
                        f"🎉 Project {project_data.project_name} succesvol geïmplementeerd - Live monitoring actief"
                    )

                # Start continuous monitoring in the background
                asyncio.create_task(_continuous_monitoring(task_id, project_data.project_name))

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
