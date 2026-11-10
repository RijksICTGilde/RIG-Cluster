"""Task handlers for clone and refresh operations.

These handlers extract long-running logic from API endpoints into standalone
functions that the TaskWorker can execute asynchronously. Each handler receives
a payload dict and a progress manager, and returns a result dict.

No FastAPI dependencies (Request, HTTPException, etc.) are used here.
"""

import logging
from typing import Any

from opi.connectors.git_repo import sync_platform_repos
from opi.connectors.kopia import KopiaConnectionError, KopiaExecutionError
from opi.core.actor import get_actor
from opi.core.cluster_config import get_prefixed_namespace
from opi.core.config import format_age_key_file, settings
from opi.manager.backup.base import BaseBackupManager
from opi.services.oom_watcher import reset_oom_tune_attempts
from opi.services.project_schema import ProjectIntegrityError, ProjectSchemaError
from opi.services.project_store import ProjectStoreError, get_project_store
from opi.services.tasks import progress_steps as steps
from opi.services.tasks.task_errors import platform_failure_fields
from opi.utils.age import (
    decrypt_password_smart_auto,
    generate_age_key_pair,
    get_decoded_project_private_key,
    wrap_project_key,
)
from opi.utils.egress_guard import has_direct_clone_source
from opi.utils.key_rotation import ConversionError, rotate_project_key
from opi.utils.project_names import validate_project_name

logger = logging.getLogger(__name__)


def terminated_sync_fields(project_manager: Any) -> dict[str, list[str]]:
    """The deployments whose running sync this run aborted, when there are any."""
    terminated = project_manager.get_terminated_sync_operations()
    return {"terminated_syncs": terminated} if terminated else {}


async def handle_clone_database(payload: dict, progress: Any) -> dict:
    """Clone a PostgreSQL database from an external source into a deployment, over a direct
    connection or through a Chisel tunnel.

    The payload holds ``project_name``, ``deployment_name``, ``source_database``,
    ``source_schema``, ``source_username`` and ``source_password`` (AGE-encrypted in the task
    payload), plus ``force_clone`` to drop an existing target first. Either ``source_host`` and
    ``source_port`` for a direct connection, or ``tunnel`` with the Chisel config
    (``server_url``, ``username``, ``password``, ``remote_host``, ``remote_port``).
    """
    from opi.manager.project_manager import ProjectManager

    project_name: str = payload["project_name"]
    deployment_name: str = payload["deployment_name"]
    source_database: str = payload["source_database"]
    source_schema: str = payload["source_schema"]
    source_username: str = payload["source_username"]
    source_password: str = await decrypt_password_smart_auto(payload["source_password"])
    force_clone: bool = payload.get("force_clone", False)
    tunnel: dict | None = payload.get("tunnel")

    project_manager: ProjectManager | None = None
    try:
        # Task 1: Initialize project manager
        init_task = progress.add_task(steps.PROJECT_DATA_FETCH)
        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")
        progress.complete_task(init_task)

        # Task 2: Clone database
        clone_method = "via een Chisel-tunnel" if tunnel else "via een directe verbinding"
        clone_task = progress.add_task(steps.DATABASE_CLONE, subject=f"{source_database} {clone_method}")

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
                tunnel_password=await decrypt_password_smart_auto(tunnel["password"]),
                tunnel_remote_host=tunnel["remote_host"],
                tunnel_remote_port=tunnel["remote_port"],
                force_clone=force_clone,
            )
        else:
            # Direct connection
            source_host: str | None = payload.get("source_host")
            source_port: int | None = payload.get("source_port")

            if not has_direct_clone_source(source_host, source_port):
                error_msg = "source_host and source_port are required when not using tunnel configuration"
                progress.fail_task(clone_task, error_msg)
                progress.fail_project(error_msg)
                raise ValueError(error_msg)
            assert source_host is not None  # noqa: S101 - has_direct_clone_source just confirmed it
            assert source_port is not None  # noqa: S101 - has_direct_clone_source just confirmed it

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
    """Clone a MinIO bucket from an external source into a deployment, over a direct connection
    or through a Chisel tunnel.

    The payload holds ``project_name``, ``deployment_name``, ``source_bucket``,
    ``source_access_key`` and ``source_secret_key`` (AGE-encrypted in the task payload), plus
    ``source_secure`` for an HTTPS source (default False) and ``force_clone`` to overwrite an
    existing target bucket. Either ``source_host`` and ``source_port`` for a direct connection,
    or ``tunnel`` with the Chisel config (``server_url``, ``username``, ``password``,
    ``remote_host``, ``remote_port``).
    """
    from opi.manager.project_manager import ProjectManager

    project_name: str = payload["project_name"]
    deployment_name: str = payload["deployment_name"]
    source_bucket: str = payload["source_bucket"]
    source_access_key: str = payload["source_access_key"]
    source_secret_key: str = await decrypt_password_smart_auto(payload["source_secret_key"])
    source_secure: bool = payload.get("source_secure", False)
    force_clone: bool = payload.get("force_clone", False)
    tunnel: dict | None = payload.get("tunnel")

    project_manager: ProjectManager | None = None
    try:
        # Task 1: Initialize project manager
        init_task = progress.add_task(steps.PROJECT_DATA_FETCH)
        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")
        progress.complete_task(init_task)

        # Task 2: Clone bucket
        clone_task = progress.add_task(steps.BUCKET_CLONE, subject=source_bucket)

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
                tunnel_password=await decrypt_password_smart_auto(tunnel["password"]),
                tunnel_remote_host=tunnel["remote_host"],
                tunnel_remote_port=tunnel["remote_port"],
                source_secure=source_secure,
                force_clone=force_clone,
            )
        else:
            # Direct connection
            source_host: str | None = payload.get("source_host")
            source_port: int | None = payload.get("source_port")

            if not has_direct_clone_source(source_host, source_port):
                error_msg = "source_host and source_port are required when not using tunnel configuration"
                progress.fail_task(clone_task, error_msg)
                progress.fail_project(error_msg)
                raise ValueError(error_msg)
            assert source_host is not None  # noqa: S101 - has_direct_clone_source just confirmed it
            assert source_port is not None  # noqa: S101 - has_direct_clone_source just confirmed it

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
        }

    except Exception as exc:
        if not isinstance(exc, ValueError | RuntimeError):
            progress.fail_project(str(exc))
        raise
    finally:
        if project_manager:
            await project_manager.close()


async def _sync_or_fail_step(progress: Any, task_id: str, *, reconcile_projects: bool = False) -> None:
    """Pick up outside changes, and fail the step when git does not cooperate.

    Closing the step is what tells the user the task failed and why. The message is the
    connector's, so the user sees whether it is the platform's doing.
    """
    try:
        if reconcile_projects:
            await get_project_store().reconcile()
        await sync_platform_repos()
    except RuntimeError as exc:
        progress.fail_task(task_id, f"{type(exc).__name__}: {exc}")
        raise


async def handle_refresh_deployment(payload: dict, progress: Any) -> dict:
    """Reprocess ONE deployment from the project YAML file, re-running its provisioning steps.

    The payload holds ``project_name``, ``deployment_name`` and ``force_clone``.
    ``reset_running_sync`` breaks off a sync that is still running, however young: the refresh
    endpoints set it, an automated caller (the OOM watcher queues a refresh for itself) leaves
    it out. ``rotate_credentials`` names the services whose credential must be replaced during
    this run instead of reused (see ``handle_rotate_credentials``).

    The result matches the V1 sync response structure.
    """

    from opi.manager.project_manager import create_project_manager
    from opi.utils.project_names import validate_project_name

    project_name: str = payload["project_name"]
    deployment_name: str = payload["deployment_name"]
    force_clone: bool = payload.get("force_clone", False)
    reset_running_sync: bool = payload.get("reset_running_sync", False)
    # The automated refresh queued right after an auto-disable must NOT retry
    # moving-tag (e.g. :latest) disables, or a still-broken component would flap.
    # User-initiated refreshes (no flag) are an explicit retry request.
    automated_remediation: bool = payload.get("automated_remediation", False)
    rotate_credentials = frozenset(payload.get("rotate_credentials", []))

    # Clear OOM tune state at start (not end): the sync wait records what it answered,
    # and clearing after would wipe that. Automated refreshes skip this entirely.
    if not automated_remediation:
        reset_oom_tune_attempts(project_name, deployment_name)

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
        lookup_task = progress.add_task(steps.PROJECT_LOOKUP)
        logger.info(
            "Deployment refresh request for: %s/%s (force_clone=%s)",
            project_name,
            deployment_name,
            force_clone,
        )

        # Same reason as on a project refresh: whatever was committed to the manifest
        # repos outside ZAD belongs in the state we generate from.
        await _sync_or_fail_step(progress, lookup_task)

        project = get_project_store().get(project_name)

        if not project:
            error_msg = f"Project '{project_name}' not found in project registry"
            progress.fail_task(lookup_task, error_msg)
            progress.fail_project(error_msg)
            raise ValueError(error_msg)

        progress.complete_task(lookup_task)

        # Task 2: Process deployment
        project_manager = create_project_manager()
        project_file_path = f"projects/{project.filename}"

        processing_result = await project_manager.process_project_from_git(
            project_file_path,
            task_progress_manager=progress,
            deployment_name=deployment_name,
            force_clone=force_clone,
            force_reenable=not automated_remediation,
            reset_running_sync=reset_running_sync,
            rotate_credentials=rotate_credentials,
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
                    # On the successful branch as well: "rolled out, but not healthy" is
                    # formally a success, and this is the channel that carries the
                    # per-component story.
                    **(
                        {"component_failures": component_failures}
                        if (component_failures := project_manager.get_component_failures())
                        else {}
                    ),
                    **terminated_sync_fields(project_manager),
                },
            }
        else:
            processing_error = project_manager.get_processing_error()
            error_msg = (
                processing_error or f"Failed to process deployment '{deployment_name}' in project '{project_name}'"
            )
            logger.warning("Deployment refresh failed: %s/%s", project_name, deployment_name)
            progress.fail_project(error_msg)

            component_failures = project_manager.get_component_failures()
            return {
                "status": "failed",
                "message": error_msg,
                "error": error_msg,
                "error_type": "processing_failed",
                **platform_failure_fields(project_manager),
                "project": {"name": project_name, "file_path": project_file_path},
                "processing": {
                    "status": "failed",
                    "error": error_msg,
                    **({"component_failures": component_failures} if component_failures else {}),
                    **terminated_sync_fields(project_manager),
                },
            }

    except Exception as exc:
        if not isinstance(exc, ValueError):
            progress.fail_project(str(exc))
        raise
    finally:
        if project_manager:
            await project_manager.close()


async def handle_rotate_credentials(payload: dict, progress: Any) -> dict:
    """Replace the credentials of a deployment and roll the deployment out again.

    The rotation itself happens in the ordinary processing: the managers make a new value for
    every service in ``rotate_credentials`` instead of reusing the stored one. That is why
    this is a refresh with that set added, and not a rollout path of its own.

    ``payload`` is the one of ``handle_refresh_deployment``, with ``rotate_credentials`` as a
    non-empty list of service names. Returns the result of the refresh, with ``rotated``
    added when everything succeeded.
    """
    services: list[str] = payload["rotate_credentials"]
    result = await handle_refresh_deployment(payload, progress)
    if result.get("status") == "success":
        result["rotated"] = services
        result["message"] = (
            f"Credentials van deployment '{payload['deployment_name']}' vervangen voor: {', '.join(services)}"
        )
    return result


async def handle_refresh_project(payload: dict, progress: Any) -> dict:
    """Reprocess EVERY deployment of a project from the project YAML file, the async V2
    equivalent of the V1 GET /:refresh endpoint.

    The payload holds ``project_name`` and ``force_clone``. ``reset_running_sync`` breaks off a
    sync that is still running, however young: the refresh endpoints set it, the internal
    caller in the component-delete path decides for itself.
    """

    from opi.manager.project_manager import create_project_manager
    from opi.utils.project_names import validate_project_name

    project_name: str = payload["project_name"]
    force_clone: bool = payload.get("force_clone", False)
    reset_running_sync: bool = payload.get("reset_running_sync", False)

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
        lookup_task = progress.add_task(steps.PROJECT_LOOKUP)
        logger.info("Project refresh request for: %s (force_clone=%s)", project_name, force_clone)

        # Pick up anything committed outside ZAD before reprocessing, so "refresh"
        # actually means "use what is in git right now". Costs one ls-remote when
        # nothing changed; a fetch and a targeted re-read when it did.
        await _sync_or_fail_step(progress, lookup_task, reconcile_projects=True)

        project = get_project_store().get(project_name)

        if not project:
            error_msg = f"Project '{project_name}' not found in project registry"
            progress.fail_task(lookup_task, error_msg)
            progress.fail_project(error_msg)
            raise ValueError(error_msg)

        progress.complete_task(lookup_task)

        # Task 2: Process project (all deployments)
        project_manager = create_project_manager()
        project_file_path = f"projects/{project.filename}"

        processing_result = await project_manager.process_project_from_git(
            project_file_path,
            task_progress_manager=progress,
            force_clone=force_clone,
            reset_running_sync=reset_running_sync,
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

            return {
                "status": "success",
                "message": f"Project '{project_name}' refreshed and processed successfully",
                "project": {"name": project_name, "file_path": project_file_path},
                "urls": urls,
                "processing": {
                    "status": "completed",
                    "message": "All project resources processed successfully",
                    "result": processing_result,
                    # On the successful branch as well: "rolled out, but not healthy" is
                    # formally a success, and this is the channel that carries the
                    # per-component story.
                    **(
                        {"component_failures": component_failures}
                        if (component_failures := project_manager.get_component_failures())
                        else {}
                    ),
                    **terminated_sync_fields(project_manager),
                },
            }
        else:
            processing_error = project_manager.get_processing_error()
            error_msg = processing_error or f"Failed to process project '{project_name}'"
            logger.warning("Project refresh failed: %s", project_name)
            progress.fail_project(error_msg)

            component_failures = project_manager.get_component_failures()
            return {
                "status": "failed",
                "message": error_msg,
                "error": error_msg,
                "error_type": "processing_failed",
                **platform_failure_fields(project_manager),
                "project": {"name": project_name, "file_path": project_file_path},
                "processing": {
                    "status": "failed",
                    "error": error_msg,
                    **({"component_failures": component_failures} if component_failures else {}),
                    **terminated_sync_fields(project_manager),
                },
            }

    except Exception as exc:
        if not isinstance(exc, ValueError):
            progress.fail_project(str(exc))
        raise
    finally:
        if project_manager:
            await project_manager.close()


async def _carry_over_backup_passwords(
    project_data: dict[str, Any],
    new_private_key: str,
    new_public_key: str,
) -> list[str]:
    """Carry the Kopia password of every namespace of this project over to the new key.

    The password is derived from the Secret ``sops-age-key``; without this step the existing
    repository no longer opens after the replacement and the backup of that namespace stalls.
    """
    project_name = project_data.get("name", "")
    cluster = settings.CLUSTER_MANAGER
    new_age_key = format_age_key_file(new_private_key, new_public_key)

    backup_manager = BaseBackupManager()
    # Deployments share a namespace, so once per namespace: a second attempt with the old
    # password opens nothing any more.
    seen: set[str] = set()
    carried: list[str] = []
    for deployment in project_data.get("deployments", []) or []:
        base_namespace = deployment.get("namespace")
        if not base_namespace or deployment.get("cluster") != cluster:
            continue
        namespace = get_prefixed_namespace(cluster, base_namespace)
        if namespace in seen:
            continue
        seen.add(namespace)
        if await backup_manager.carry_over_backup_password(project_name, cluster, namespace, new_age_key):
            carried.append(namespace)
    return carried


async def handle_rotate_project_key(payload: dict, progress: Any) -> dict:
    """Replace the AGE key pair of one project.

    See ``features/projectsleutel-vervangen.md`` for what this does and does not do.

    Expected payload keys:
        project_name: the project whose key is being rotated
    """
    project_name: str = payload["project_name"]

    if not validate_project_name(project_name):
        error_msg = (
            "Invalid project name format. Must start with lowercase letter, "
            "then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters"
        )
        progress.fail_project(error_msg)
        raise ValueError(error_msg)

    store = get_project_store()

    lookup_task = progress.add_task(steps.PROJECT_FILE_READ)
    await store.reconcile()
    project = store.get(project_name)
    if not project:
        error_msg = f"Project '{project_name}' not found in project registry"
        progress.fail_task(lookup_task, error_msg)
        progress.fail_project(error_msg)
        raise ValueError(error_msg)
    progress.complete_task(lookup_task)

    rotate_task = progress.add_task(steps.PROJECT_KEY_ROTATE)
    new_private_key, new_public_key = generate_age_key_pair()
    # Under the project KEK, as when a project is created; without a KEK wrap_project_key
    # falls back to the platform key (RC-334).
    encrypted_new_private_key = await wrap_project_key(new_private_key)
    # Overwritten per attempt: what the last, successful round did is what landed.
    rotated_fields: list[str] = []
    skipped_fields: list[str] = []

    async def rotate(current: dict[str, Any]) -> dict[str, Any]:
        """The conversion, per attempt again on the fresh state out of git.

        Why mutate beats save is in features/projectsleutel-vervangen.md.
        """
        old_private_key = await get_decoded_project_private_key(current)
        rotation = await rotate_project_key(current, old_private_key, new_public_key)
        rotation.data["config"]["age-public-key"] = new_public_key
        rotation.data["config"]["age-private-key"] = encrypted_new_private_key
        rotated_fields[:] = rotation.rotated
        skipped_fields[:] = rotation.skipped
        return rotation.data

    def commit_message() -> str:
        return f"Projectsleutel van {project_name} vervangen ({len(rotated_fields)} velden omgezet)"

    try:
        mutation = await store.mutate(
            project_name,
            rotate,
            message=commit_message,
            actor=get_actor(),
            filename=project.filename,
        )
    except ConversionError as e:
        error_msg = f"Omzetten naar de nieuwe projectsleutel is mislukt: {e}"
        progress.fail_task(rotate_task, error_msg)
        progress.fail_project(error_msg)
        raise
    except (ProjectStoreError, ProjectSchemaError, ProjectIntegrityError) as e:
        error_msg = f"Opslaan van het projectbestand met de nieuwe sleutel is mislukt: {e}"
        progress.fail_task(rotate_task, error_msg)
        progress.fail_project(error_msg)
        raise
    logger.info(
        "Project key rotation for %s: %d fields converted, %d left on another key",
        project_name,
        len(rotated_fields),
        len(skipped_fields),
    )
    progress.complete_task(rotate_task)

    # After the save (a repair needs both passwords) and before the processing, which puts
    # the key in the Secret.
    backup_task = progress.add_task(steps.BACKUP_PASSWORD_CARRY)
    try:
        carried = await _carry_over_backup_passwords(mutation.after, new_private_key, new_public_key)
    except (KopiaConnectionError, KopiaExecutionError) as e:
        error_msg = f"Het backupwachtwoord kon niet mee naar de nieuwe sleutel: {e}"
        progress.fail_task(backup_task, error_msg)
        progress.fail_project(error_msg)
        raise
    logger.info("Project key rotation for %s: backup password carried over for %s", project_name, carried or "none")
    progress.complete_task(backup_task)

    # Reprocess through refresh_project: same code path as any other project change.
    refresh_result = await handle_refresh_project(
        {"project_name": project_name, "force_clone": False, "reset_running_sync": True}, progress
    )
    failed = refresh_result.get("status") == "failed"

    return {
        "status": "failed" if failed else "success",
        "message": (
            refresh_result.get("message", "Herverwerken na het vervangen van de sleutel mislukt")
            if failed
            else f"Projectsleutel van '{project_name}' vervangen: {len(rotated_fields)} velden omgezet"
        ),
        "project": {"name": project_name, "file_path": f"projects/{project.filename}"},
        "public_key": new_public_key,
        "rotated_fields": rotated_fields,
        "skipped_fields": skipped_fields,
        "backup_namespaces": carried,
        "processing": refresh_result.get("processing"),
        **({"error": refresh_result.get("error"), "error_type": refresh_result.get("error_type")} if failed else {}),
        **(
            {"error_category": refresh_result["error_category"]}
            if failed and refresh_result.get("error_category")
            else {}
        ),
    }
