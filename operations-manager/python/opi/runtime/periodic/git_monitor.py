"""Start and stop the git file monitor with the FastAPI application lifecycle."""

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from opi.connectors.git import start_monitoring_task
from opi.connectors.kubectl import KubectlExecutionError, create_kubectl_connector
from opi.core.cluster_config import (
    get_argo_namespace,
    get_pod_security_labels,
    get_prefixed_namespace,
    namespace_labels,
)
from opi.core.config import settings
from opi.manager.project_manager import enforce_namespace_pin
from opi.services.project_schema import ProjectSchemaError, validate_declared_project_schema

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

_monitoring_tasks: dict[str, asyncio.Task] = {}


async def check_and_create_namespaces(project_data: dict[str, Any]) -> bool:
    """Create the namespace of every deployment of this project that is missing.

    Returns False when one of them could not be checked or created.
    """
    kubectl = create_kubectl_connector()

    # Pin the namespace ourselves: this path reads project_data straight
    # from git, not via ProjectManager.get_deployments where the pin lives.
    enforce_namespace_pin(project_data)

    project_name = project_data.get("name")
    logger.info(f"Checking namespaces for project: {project_name}")

    configured_cluster = settings.CLUSTER_MANAGER
    logger.debug(f"Configured cluster manager: {configured_cluster}")

    try:
        deployments = project_data.get("deployments", [])
        if not deployments:
            logger.warning("No deployments defined in project data")
            return True  # Not an error, just nothing to do

        all_succeeded = True

        for deployment in deployments:
            deployment_name = deployment.get("name")
            target_cluster = deployment.get("cluster")
            base_namespace = deployment.get("namespace")

            if target_cluster != configured_cluster:
                logger.info(
                    f"Project '{project_name}' deployment '{deployment_name}' targets cluster '{target_cluster}' but CLUSTER_MANAGER is '{configured_cluster}' - skipping namespace creation"
                )
                continue

            namespace = get_prefixed_namespace(target_cluster, base_namespace)

            logger.info(f"Checking namespace: {namespace} for deployment: {deployment_name}")

            namespace_exists = await kubectl.namespace_exists(namespace)
            if namespace_exists:
                logger.info(f"Namespace {namespace} already exists")
            else:
                logger.info(f"Namespace {namespace} does not exist, creating it...")

                manifest_path = os.path.join(settings.MANIFESTS_PATH, "namespace.yaml.jinja")

                variables = {
                    "namespace": namespace,
                    "manager": get_argo_namespace(configured_cluster),
                    "pod_security_labels": get_pod_security_labels(configured_cluster),
                }

                try:
                    await kubectl.apply_manifest(manifest_path, variables)
                    logger.info(f"Successfully created namespace: {namespace}")
                except KubectlExecutionError as apply_error:
                    logger.error(f"Failed to create namespace {namespace}: {apply_error}")
                    all_succeeded = False
                    continue

            # Always ensure the managed-by and Pod Security labels exist (idempotent)
            label_result = await kubectl.apply_labels_to_resource(
                resource_type="namespace",
                resource_name=namespace,
                labels=namespace_labels(configured_cluster),
            )
            if label_result:
                logger.info(f"Ensured ArgoCD managed-by and Pod Security labels on namespace: {namespace}")
            else:
                logger.error(f"Failed to apply the managed-by and Pod Security labels to namespace: {namespace}")
                all_succeeded = False

        return all_succeeded

    except Exception as e:
        logger.error(f"Error checking/creating namespaces: {e}")
        logger.exception("Stack trace:")
        return False


async def file_change_handler(file_path: str, content: dict) -> None:
    """Handle a change to the monitored YAML file."""
    logger.info(f"Detected changes in {file_path}")

    # Security gate: validate before any processing, and before any migration.
    # Fails closed: a violation aborts handling so a hostile project committed
    # directly to git never reaches the connectors, and never gets written back
    # under our own identity because it happened to migrate clean.
    #
    # Validation is against the version the file DECLARES, not against the newest
    # schema (RC-32). A file that has not been reprocessed for months is held to
    # the rules of its own version; that is what lets the newest schema drop forms
    # whose migration is long written.
    try:
        validate_declared_project_schema(content)
    except ProjectSchemaError as e:
        # An ERROR and not a warning: every version a file may declare has a schema, so a
        # rejection means a genuinely broken or hostile file. Skip the file, keep polling,
        # but say so loudly.
        logger.error(f"Projectbestand {file_path} afgekeurd door schemavalidatie en NIET verwerkt: {e}")
        return

    # Check if this is a project file with deployments
    if "deployments" in content:
        project_name = content.get("name", "unknown")
        deployments_count = len(content["deployments"])
        logger.info(f"Project '{project_name}' has {deployments_count} deployment(s)")

        # The cluster check is answered from the content we already hold: a bare
        # ProjectManager has no contents to answer it with.
        if not [d for d in content["deployments"] if d.get("cluster") == settings.CLUSTER_MANAGER]:
            logger.info(
                f"Project '{project_name}' has no deployments targeting cluster '{settings.CLUSTER_MANAGER}' - this operations manager only handles deployments for this cluster"
            )
            return

        # Task 1: Check and create namespaces for deployments
        logger.info("Task 1: Checking and creating namespaces for deployments...")
        try:
            namespace_success = await check_and_create_namespaces(content)
        except ValueError as exc:
            # Project declared a namespace that doesn't match its name
            # (the pin rejected it). Skip this project but keep the
            # polling loop alive so other projects still get processed.
            logger.error(f"Rejected project '{project_name}' from git monitor: {exc}")
            return

        if namespace_success:
            logger.info("Namespace check/creation completed successfully")
        else:
            logger.error("Namespace check/creation failed")

    # Process other content types if needed
    if "services" in content:
        logger.info(f"Services defined: {len(content['services'])}")
        # Process each service...


async def start_git_monitoring(app: FastAPI) -> None:
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
            ssh_key_path=None,
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
