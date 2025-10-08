"""
Git file monitoring service for FastAPI integration.

This module handles starting and stopping the Git file monitoring service
as part of the FastAPI application lifecycle.
"""

import asyncio
import logging
import os
from typing import Any

from fastapi import FastAPI

from opi.connectors.git import start_monitoring_task
from opi.connectors.kubectl import create_kubectl_connector
from opi.core.cluster_config import get_argo_namespace, get_prefixed_namespace
from opi.core.config import settings
from opi.manager.project_manager import ProjectManager

logger = logging.getLogger(__name__)

# Store active monitoring tasks
_monitoring_tasks: dict[str, asyncio.Task] = {}


async def check_and_create_namespaces(project_data: dict[str, Any]) -> bool:
    """
    Check and create namespaces for all deployments in the project.

    This is adapted from the project_manager.py create_project_namespace method.

    Args:
        project_data: The parsed project data

    Returns:
        True if all namespaces were checked/created successfully, False otherwise
    """
    kubectl = create_kubectl_connector()

    project_name = project_data.get("name")
    logger.info(f"Checking namespaces for project: {project_name}")

    # Get the configured cluster manager
    configured_cluster = settings.CLUSTER_MANAGER
    logger.debug(f"Configured cluster manager: {configured_cluster}")

    try:
        # Check namespace for each deployment
        deployments = project_data.get("deployments", [])
        if not deployments:
            logger.warning("No deployments defined in project data")
            return True  # Not an error, just nothing to do

        all_succeeded = True

        for deployment in deployments:
            deployment_name = deployment.get("name")
            target_cluster = deployment.get("cluster")
            base_namespace = deployment.get("namespace")

            # Check if deployment targets the configured cluster
            if target_cluster != configured_cluster:
                logger.info(
                    f"Project '{project_name}' deployment '{deployment_name}' targets cluster '{target_cluster}' but CLUSTER_MANAGER is '{configured_cluster}' - skipping namespace creation"
                )
                continue

            # Get the prefixed namespace
            namespace = get_prefixed_namespace(target_cluster, base_namespace)

            logger.info(f"Checking namespace: {namespace} for deployment: {deployment_name}")

            # Check if namespace already exists
            namespace_exists = await kubectl.namespace_exists(namespace)
            if namespace_exists:
                logger.info(f"Namespace {namespace} already exists, no action needed")
                continue

            logger.info(f"Namespace {namespace} does not exist, creating it...")

            # Create the namespace using the manifest template
            manifest_path = os.path.join(settings.MANIFESTS_PATH, "namespace.yaml.jinja")

            # Template variables
            variables = {"namespace": namespace, "manager": get_argo_namespace(configured_cluster)}

            result = await kubectl.apply_manifest(manifest_path, variables)

            if result:
                logger.info(f"Successfully created namespace: {namespace}")
            else:
                logger.error(f"Failed to create namespace: {namespace}")
                all_succeeded = False

        return all_succeeded

    except Exception as e:
        logger.error(f"Error checking/creating namespaces: {e}")
        logger.exception("Stack trace:")
        return False


async def file_change_handler(file_path: str, content: dict) -> None:
    """
    Handle changes to the monitored YAML file.

    Args:
        file_path: Path of the file that changed
        content: Parsed YAML content
    """
    logger.info(f"Detected changes in {file_path}")

    # Check if this is a project file with deployments
    if "deployments" in content:
        project_name = content.get("name", "unknown")
        deployments_count = len(content["deployments"])
        logger.info(f"Project '{project_name}' has {deployments_count} deployment(s)")

        # First validate cluster configuration
        project_manager = ProjectManager()

        if not project_manager.has_deployments_for_current_cluster(content):
            logger.info(f"Project '{project_name}' cluster validation failed - skipping processing")
            return

        # Task 1: Check and create namespaces for deployments
        logger.info("Task 1: Checking and creating namespaces for deployments...")
        namespace_success = await check_and_create_namespaces(content)

        if namespace_success:
            logger.info("Namespace check/creation completed successfully")
        else:
            logger.error("Namespace check/creation failed")

    # Keep the original debug output
    logger.debug(f"Full content: {content}")

    # Process other content types if needed
    if "services" in content:
        logger.info(f"Services defined: {len(content['services'])}")
        # Process each service...


async def start_git_monitoring(app: FastAPI) -> None:
    """
    Start Git file monitoring as part of FastAPI startup.

    Args:
        app: FastAPI application instance
    """

    if not settings.ENABLE_GIT_MONITOR:
        logger.info("Git monitoring is disabled in settings")
        return

    # Get configuration from settings
    git_url = settings.GIT_PROJECTS_SERVER_URL  # Using as Git repository URL
    repo_path = settings.GIT_PROJECTS_SERVER_REPO_PATH  # Path within the repository
    file_path = settings.GIT_PROJECTS_SERVER_FILE_PATH  # Path to file to monitor
    branch = settings.GIT_PROJECTS_SERVER_BRANCH
    interval = settings.GIT_PROJECTS_SERVER_POLL_INTERVAL

    # Log configuration
    logger.info("Git monitoring configuration:")
    logger.info(f"  Repository URL: {git_url}")
    logger.info(f"  Repository path: {repo_path}")
    logger.info(f"  File to monitor: {file_path}")
    logger.info(f"  Branch: {branch}")
    logger.info(f"  Poll interval: {interval} seconds")

    # Get SSH key information
    ssh_key_path = None
    if git_url.startswith(("git://", "ssh://")):
        ssh_key_path = settings.GIT_SERVER_KEY_PATH
        logger.debug(f"Using SSH key: {ssh_key_path}")

    # Add HTTP authentication if needed
    if settings.GIT_PROJECTS_SERVER_USERNAME and git_url.startswith(("http://", "https://")):
        # URL will be modified in the connector to include authentication
        logger.debug("Using HTTP username/password authentication")

    try:
        # Create and start the monitoring task
        # Password decryption is handled by GitConnector internally
        task = await start_monitoring_task(
            repo_url=git_url,
            file_path=file_path,
            branch=branch,
            interval=interval,
            repo_path=repo_path,
            callback=file_change_handler,
            ssh_key_path=ssh_key_path,
            username=settings.GIT_PROJECTS_SERVER_USERNAME,
            password=settings.GIT_PROJECTS_SERVER_PASSWORD,
        )

        # Store the task so we can cancel it later
        _monitoring_tasks[file_path] = task

        # Store in app state for access elsewhere
        app.state.git_monitor_tasks = _monitoring_tasks

        logger.info(f"Git monitoring started for {file_path}")

    except Exception as e:
        logger.error(f"Failed to start Git monitoring: {e}")
        logger.exception("Stack trace:")
        # Continue app startup without Git monitoring


async def stop_git_monitoring() -> None:
    """Stop all Git monitoring tasks."""
    for file_path, task in _monitoring_tasks.items():
        if not task.done():
            logger.info(f"Stopping Git monitoring for {file_path}")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Error while stopping monitoring task: {e}")

    _monitoring_tasks.clear()
