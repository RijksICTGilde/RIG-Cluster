"""Task handlers for clone and refresh operations.

These handlers extract long-running logic from API endpoints into standalone
functions that the TaskWorker can execute asynchronously. Each handler receives
a payload dict and a progress manager, and returns a result dict.

No FastAPI dependencies (Request, HTTPException, etc.) are used here.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def handle_clone_database(payload: dict, progress: Any) -> dict:
    """Handle async database clone task.

    Clones a PostgreSQL database from an external source into a deployment.
    Supports both direct connections and Chisel tunnel-based connections.

    Args:
        payload: Task payload containing:
            - project_name: Target project name
            - deployment_name: Target deployment name
            - source_database: Source database name
            - source_schema: Source schema name
            - source_username: Source database username
            - source_password: Source database password
            - force_clone: Whether to drop existing target before cloning
            - source_host: Direct connection host (required if no tunnel)
            - source_port: Direct connection port (required if no tunnel)
            - tunnel: Optional dict with Chisel tunnel config:
                - server_url, username, password, remote_host, remote_port
        progress: PersistentTaskProgressManager for reporting progress.

    Returns:
        Dict with source, target, and rows_copied fields.
    """
    from opi.manager.project_manager import ProjectManager

    project_name: str = payload["project_name"]
    deployment_name: str = payload["deployment_name"]
    source_database: str = payload["source_database"]
    source_schema: str = payload["source_schema"]
    source_username: str = payload["source_username"]
    source_password: str = payload["source_password"]
    force_clone: bool = payload.get("force_clone", False)
    tunnel: dict | None = payload.get("tunnel")

    project_manager: ProjectManager | None = None
    try:
        progress.update_current_step("Initializing project manager")
        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")

        if tunnel:
            # Chisel tunnel-based connection
            logger.info(
                "External database clone request via Chisel tunnel: %s/%s "
                "<- %s:%s/%s (via %s)",
                project_name,
                deployment_name,
                tunnel["remote_host"],
                tunnel["remote_port"],
                source_database,
                tunnel["server_url"],
            )

            progress.update_current_step("Cloning database via Chisel tunnel")
            clone_result = await project_manager.clone_database_from_external_with_tunnel(
                project_name=project_name,
                deployment_name=deployment_name,
                source_database=source_database,
                source_schema=source_schema,
                source_username=source_username,
                source_password=source_password,
                tunnel_server_url=tunnel["server_url"],
                tunnel_username=tunnel["username"],
                tunnel_password=tunnel["password"],
                tunnel_remote_host=tunnel["remote_host"],
                tunnel_remote_port=tunnel["remote_port"],
                force_clone=force_clone,
            )
        else:
            # Direct connection
            source_host: str | None = payload.get("source_host")
            source_port: int | None = payload.get("source_port")

            if not source_host or not source_port:
                raise ValueError("source_host and source_port are required when not using tunnel configuration")

            logger.info(
                "External database clone request (direct): %s/%s <- %s:%s/%s",
                project_name,
                deployment_name,
                source_host,
                source_port,
                source_database,
            )

            progress.update_current_step("Cloning database via direct connection")
            clone_result = await project_manager.clone_database_from_external_direct(
                project_name=project_name,
                deployment_name=deployment_name,
                source_host=source_host,
                source_port=source_port,
                source_database=source_database,
                source_schema=source_schema,
                source_username=source_username,
                source_password=source_password,
                force_clone=force_clone,
            )

        if not clone_result["success"]:
            errors = ", ".join(clone_result.get("errors", []))
            raise RuntimeError(f"Database clone failed: {errors}")

        progress.update_current_step("Database clone completed")
        logger.info(
            "External database clone completed: %s/%s (success: %s)",
            project_name,
            deployment_name,
            clone_result["success"],
        )

        return {
            "source": clone_result["source"],
            "target": clone_result.get("target", {}),
            "rows_copied": clone_result.get("operations", {}).get("rows_copied"),
        }

    finally:
        if project_manager:
            await project_manager.close()


async def handle_clone_bucket(payload: dict, progress: Any) -> dict:
    """Handle async bucket clone task.

    Clones a MinIO bucket from an external source into a deployment.
    Supports both direct connections and Chisel tunnel-based connections.

    Args:
        payload: Task payload containing:
            - project_name: Target project name
            - deployment_name: Target deployment name
            - source_bucket: Source bucket name
            - source_access_key: Source MinIO access key
            - source_secret_key: Source MinIO secret key
            - source_secure: Whether source uses HTTPS (default False)
            - force_clone: Whether to overwrite existing target bucket
            - source_host: Direct connection host (required if no tunnel)
            - source_port: Direct connection port (required if no tunnel)
            - tunnel: Optional dict with Chisel tunnel config:
                - server_url, username, password, remote_host, remote_port
        progress: PersistentTaskProgressManager for reporting progress.

    Returns:
        Dict with source, target, and objects_copied fields.
    """
    from opi.manager.project_manager import ProjectManager

    project_name: str = payload["project_name"]
    deployment_name: str = payload["deployment_name"]
    source_bucket: str = payload["source_bucket"]
    source_access_key: str = payload["source_access_key"]
    source_secret_key: str = payload["source_secret_key"]
    source_secure: bool = payload.get("source_secure", False)
    force_clone: bool = payload.get("force_clone", False)
    tunnel: dict | None = payload.get("tunnel")

    project_manager: ProjectManager | None = None
    try:
        progress.update_current_step("Initializing project manager")
        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")

        if tunnel:
            # Chisel tunnel-based connection
            logger.info(
                "External bucket clone request via Chisel tunnel: %s/%s "
                "<- %s:%s/%s (via %s)",
                project_name,
                deployment_name,
                tunnel["remote_host"],
                tunnel["remote_port"],
                source_bucket,
                tunnel["server_url"],
            )

            progress.update_current_step("Cloning bucket via Chisel tunnel")
            clone_result = await project_manager.clone_minio_bucket_from_external_with_tunnel(
                project_name=project_name,
                deployment_name=deployment_name,
                source_bucket=source_bucket,
                source_access_key=source_access_key,
                source_secret_key=source_secret_key,
                tunnel_server_url=tunnel["server_url"],
                tunnel_username=tunnel["username"],
                tunnel_password=tunnel["password"],
                tunnel_remote_host=tunnel["remote_host"],
                tunnel_remote_port=tunnel["remote_port"],
                source_secure=source_secure,
                force_clone=force_clone,
            )
        else:
            # Direct connection
            source_host: str | None = payload.get("source_host")
            source_port: int | None = payload.get("source_port")

            if not source_host or not source_port:
                raise ValueError("source_host and source_port are required when not using tunnel configuration")

            logger.info(
                "External bucket clone request (direct): %s/%s <- %s:%s/%s",
                project_name,
                deployment_name,
                source_host,
                source_port,
                source_bucket,
            )

            progress.update_current_step("Cloning bucket via direct connection")
            clone_result = await project_manager.clone_minio_bucket_from_external_direct(
                project_name=project_name,
                deployment_name=deployment_name,
                source_host=source_host,
                source_port=source_port,
                source_bucket=source_bucket,
                source_access_key=source_access_key,
                source_secret_key=source_secret_key,
                source_secure=source_secure,
                force_clone=force_clone,
            )

        if not clone_result["success"]:
            errors = ", ".join(clone_result.get("errors", []))
            raise RuntimeError(f"Bucket clone failed: {errors}")

        progress.update_current_step("Bucket clone completed")
        logger.info(
            "External bucket clone completed: %s/%s (success: %s)",
            project_name,
            deployment_name,
            clone_result["success"],
        )

        return {
            "source": clone_result["source"],
            "target": clone_result.get("target", {}),
            "objects_copied": clone_result.get("operations", {}).get("objects_copied"),
        }

    finally:
        if project_manager:
            await project_manager.close()


async def handle_refresh_deployment(payload: dict, progress: Any) -> dict:
    """Handle async deployment refresh task.

    Refreshes a single deployment by reprocessing it from the project YAML file,
    re-running provisioning steps for the specified deployment only.

    Args:
        payload: Task payload containing:
            - project_name: Project name
            - deployment_name: Deployment to refresh
            - force_clone: Whether to force clone even if target resources exist
        progress: PersistentTaskProgressManager for reporting progress.

    Returns:
        Dict with deployment_name and changes_detected fields.
    """
    from opi.manager.project_manager import create_project_manager
    from opi.services.project_service import get_project_service
    from opi.utils.project_utils import validate_project_name

    project_name: str = payload["project_name"]
    deployment_name: str = payload["deployment_name"]
    force_clone: bool = payload.get("force_clone", False)

    if not validate_project_name(project_name):
        raise ValueError(
            "Invalid project name format. Must start with lowercase letter, "
            "then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters"
        )

    project_manager = None
    try:
        progress.update_current_step("Looking up project in registry")
        logger.info(
            "Deployment refresh request for: %s/%s (force_clone=%s)",
            project_name,
            deployment_name,
            force_clone,
        )

        project_service = get_project_service()
        project = project_service.get_project(project_name)

        if not project:
            raise ValueError(f"Project '{project_name}' not found in project registry")

        progress.update_current_step("Initializing project manager")
        project_manager = create_project_manager()
        project_file_path = f"projects/{project.filename}"

        progress.update_current_step(f"Processing deployment '{deployment_name}' from git")
        processing_result = await project_manager.process_project_from_git(
            project_file_path, deployment_name=deployment_name, force_clone=force_clone
        )

        if processing_result:
            logger.info("Deployment refresh completed successfully: %s/%s", project_name, deployment_name)

            # Collect deployment results (URLs, cluster info)
            changes_detected: list[str] = []
            deployment_results = project_manager.get_deployment_results()
            for dep_name, dep_result in deployment_results.items():
                changes_detected.append(f"{dep_name}: cluster={dep_result.cluster}, urls={dep_result.urls}")

            progress.update_current_step("Deployment refresh completed successfully")
            return {
                "deployment_name": deployment_name,
                "changes_detected": changes_detected,
            }
        else:
            logger.warning("Deployment refresh failed: %s/%s", project_name, deployment_name)
            raise RuntimeError(
                f"Failed to process deployment '{deployment_name}' in project '{project_name}'"
            )

    finally:
        if project_manager:
            await project_manager.close()
