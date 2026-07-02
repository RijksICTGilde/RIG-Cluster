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
        # Task 1: Initialize project manager
        init_task = progress.add_task("Initializing project manager")
        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")
        progress.complete_task(init_task)

        # Task 2: Clone database
        clone_method = "via Chisel tunnel" if tunnel else "via direct connection"
        clone_task = progress.add_task(f"Cloning database {clone_method}")

        if tunnel:
            # Chisel tunnel-based connection
            logger.info(
                "External database clone request via Chisel tunnel: %s/%s <- %s:%s/%s (via %s)",
                project_name,
                deployment_name,
                tunnel["remote_host"],
                tunnel["remote_port"],
                source_database,
                tunnel["server_url"],
            )

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
                error_msg = "source_host and source_port are required when not using tunnel configuration"
                progress.fail_task(clone_task, error_msg)
                progress.fail_project(error_msg)
                raise ValueError(error_msg)

            logger.info(
                "External database clone request (direct): %s/%s <- %s:%s/%s",
                project_name,
                deployment_name,
                source_host,
                source_port,
                source_database,
            )

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
            error_msg = f"Database clone failed: {errors}"
            progress.fail_task(clone_task, error_msg)
            progress.fail_project(error_msg)
            raise RuntimeError(error_msg)

        progress.complete_task(clone_task)
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

    except Exception as exc:
        # Ensure fail_project is called for unexpected errors not already handled
        if not isinstance(exc, ValueError | RuntimeError):
            progress.fail_project(str(exc))
        raise
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
        # Task 1: Initialize project manager
        init_task = progress.add_task("Initializing project manager")
        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")
        progress.complete_task(init_task)

        # Task 2: Clone bucket
        clone_task = progress.add_task("Cloning bucket")

        if tunnel:
            # Chisel tunnel-based connection
            logger.info(
                "External bucket clone request via Chisel tunnel: %s/%s <- %s:%s/%s (via %s)",
                project_name,
                deployment_name,
                tunnel["remote_host"],
                tunnel["remote_port"],
                source_bucket,
                tunnel["server_url"],
            )

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
                error_msg = "source_host and source_port are required when not using tunnel configuration"
                progress.fail_task(clone_task, error_msg)
                progress.fail_project(error_msg)
                raise ValueError(error_msg)

            logger.info(
                "External bucket clone request (direct): %s/%s <- %s:%s/%s",
                project_name,
                deployment_name,
                source_host,
                source_port,
                source_bucket,
            )

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
            error_msg = f"Bucket clone failed: {errors}"
            progress.fail_task(clone_task, error_msg)
            progress.fail_project(error_msg)
            raise RuntimeError(error_msg)

        progress.complete_task(clone_task)
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

    except Exception as exc:
        if not isinstance(exc, ValueError | RuntimeError):
            progress.fail_project(str(exc))
        raise
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
        Dict matching the V1 sync response structure with status, message,
        project info, urls, and processing details.
    """

    from opi.manager.project_manager import create_project_manager
    from opi.services.project_service import get_project_service
    from opi.utils.project_utils import validate_project_name

    project_name: str = payload["project_name"]
    deployment_name: str = payload["deployment_name"]
    force_clone: bool = payload.get("force_clone", False)
    # The automated refresh queued right after an auto-disable must NOT retry
    # moving-tag (e.g. :latest) disables, or a still-broken component would flap.
    # User-initiated refreshes (no flag) are an explicit retry request.
    automated_remediation: bool = payload.get("automated_remediation", False)

    if not validate_project_name(project_name):
        error_msg = (
            "Invalid project name format. Must start with lowercase letter, "
            "then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters"
        )
        progress.fail_project(error_msg)
        raise ValueError(error_msg)

    project_manager = None
    try:
        # Task 1: Look up project
        lookup_task = progress.add_task("Looking up project")
        logger.info(
            "Deployment refresh request for: %s/%s (force_clone=%s)",
            project_name,
            deployment_name,
            force_clone,
        )

        project_service = get_project_service()
        project = project_service.get_project(project_name)

        if not project:
            error_msg = f"Project '{project_name}' not found in project registry"
            progress.fail_task(lookup_task, error_msg)
            progress.fail_project(error_msg)
            raise ValueError(error_msg)

        progress.complete_task(lookup_task)

        # Task 2: Process deployment
        deploy_task = progress.add_task("Processing deployment")
        project_manager = create_project_manager()
        project_file_path = f"projects/{project.filename}"

        processing_result = await project_manager.process_project_from_git(
            project_file_path,
            task_progress_manager=progress,
            deployment_name=deployment_name,
            force_clone=force_clone,
            allow_mutable_tag_retry=not automated_remediation,
        )

        if processing_result:
            logger.info("Deployment refresh completed successfully: %s/%s", project_name, deployment_name)

            # Collect deployment results (URLs, cluster info)
            urls: dict[str, dict[str, Any]] = {}
            deployment_results = project_manager.get_deployment_results()
            for dep_name, dep_result in deployment_results.items():
                urls[dep_name] = {
                    "cluster": dep_result.cluster,
                    "urls": dep_result.urls,
                }
                # Report web addresses from deployment results
                for url_name, url_value in (dep_result.urls or {}).items():
                    progress.update_component_web_address(url_name, url_value)

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

            return {
                "status": "success",
                "message": f"Deployment '{deployment_name}' in project '{project_name}' refreshed successfully",
                "project": {"name": project_name, "file_path": project_file_path},
                "urls": urls,
                "processing": {
                    "status": "completed",
                    "message": f"Deployment '{deployment_name}' processed successfully",
                    "result": processing_result,
                },
            }
        else:
            processing_error = project_manager.get_processing_error()
            error_msg = (
                processing_error or f"Failed to process deployment '{deployment_name}' in project '{project_name}'"
            )
            logger.warning("Deployment refresh failed: %s/%s", project_name, deployment_name)
            progress.fail_task(deploy_task, error_msg)
            progress.fail_project(error_msg)

            component_failures = project_manager.get_component_failures()
            return {
                "status": "failed",
                "message": error_msg,
                "project": {"name": project_name, "file_path": project_file_path},
                "processing": {
                    "status": "failed",
                    "error": error_msg,
                    **({"component_failures": component_failures} if component_failures else {}),
                },
            }

    except Exception as exc:
        if not isinstance(exc, ValueError):
            progress.fail_project(str(exc))
        raise
    finally:
        if project_manager:
            await project_manager.close()


async def handle_refresh_project(payload: dict, progress: Any) -> dict:
    """Handle async project refresh task.

    Refreshes all deployments in a project by reprocessing from the project
    YAML file. This is the async V2 equivalent of the V1 GET /:refresh endpoint.

    Args:
        payload: Task payload containing:
            - project_name: Project name
            - force_clone: Whether to force clone even if target resources exist
        progress: PersistentTaskProgressManager for reporting progress.

    Returns:
        Dict with status, message, project info, urls, and processing details.
    """

    from opi.manager.project_manager import create_project_manager
    from opi.services.project_service import get_project_service
    from opi.utils.project_utils import validate_project_name

    project_name: str = payload["project_name"]
    force_clone: bool = payload.get("force_clone", False)

    if not validate_project_name(project_name):
        error_msg = (
            "Invalid project name format. Must start with lowercase letter, "
            "then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters"
        )
        progress.fail_project(error_msg)
        raise ValueError(error_msg)

    project_manager = None
    try:
        # Task 1: Look up project
        lookup_task = progress.add_task("Looking up project")
        logger.info("Project refresh request for: %s (force_clone=%s)", project_name, force_clone)

        project_service = get_project_service()
        project = project_service.get_project(project_name)

        if not project:
            error_msg = f"Project '{project_name}' not found in project registry"
            progress.fail_task(lookup_task, error_msg)
            progress.fail_project(error_msg)
            raise ValueError(error_msg)

        progress.complete_task(lookup_task)

        # Task 2: Process project (all deployments)
        process_task = progress.add_task("Processing project")
        project_manager = create_project_manager()
        project_file_path = f"projects/{project.filename}"

        processing_result = await project_manager.process_project_from_git(
            project_file_path,
            task_progress_manager=progress,
            force_clone=force_clone,
        )

        if processing_result:
            logger.info("Project refresh completed successfully: %s", project_name)

            # Collect deployment results (URLs, cluster info)
            urls: dict[str, dict[str, Any]] = {}
            deployment_results = project_manager.get_deployment_results()
            for dep_name, dep_result in deployment_results.items():
                urls[dep_name] = {
                    "cluster": dep_result.cluster,
                    "urls": dep_result.urls,
                }
                for url_name, url_value in (dep_result.urls or {}).items():
                    progress.update_component_web_address(url_name, url_value)

            progress.complete_task(process_task)

            return {
                "status": "success",
                "message": f"Project '{project_name}' refreshed and processed successfully",
                "project": {"name": project_name, "file_path": project_file_path},
                "urls": urls,
                "processing": {
                    "status": "completed",
                    "message": "All project resources processed successfully",
                    "result": processing_result,
                },
            }
        else:
            processing_error = project_manager.get_processing_error()
            error_msg = processing_error or f"Failed to process project '{project_name}'"
            logger.warning("Project refresh failed: %s", project_name)
            progress.fail_task(process_task, error_msg)
            progress.fail_project(error_msg)

            component_failures = project_manager.get_component_failures()
            return {
                "status": "failed",
                "message": error_msg,
                "project": {"name": project_name, "file_path": project_file_path},
                "processing": {
                    "status": "failed",
                    "error": error_msg,
                    **({"component_failures": component_failures} if component_failures else {}),
                },
            }

    except Exception as exc:
        if not isinstance(exc, ValueError):
            progress.fail_project(str(exc))
        raise
    finally:
        if project_manager:
            await project_manager.close()
