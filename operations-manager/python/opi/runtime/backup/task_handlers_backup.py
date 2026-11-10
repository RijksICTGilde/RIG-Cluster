"""Task handlers for backup and restore operations.

These handlers are registered with the TaskWorker and executed via the
async task queue. Shared helper functions live in backup_tasks.py.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opi.core.backup_constants import DEFAULT_BACKUP_RESOURCE_TYPES
from opi.manager.backup.run import (
    BackupRunRequest,
    backup_deployment,
    backup_run_answer,
    resolve_deployment_target,
)
from opi.runtime.backup.backup_tasks import (
    _create_deployment_from_source,
    _pre_restore_pvcs,
    _provision_deployment_infrastructure,
    restore_items_with_progress,
)
from opi.services.tasks import progress_steps as steps
from opi.utils.naming import generate_backup_run_id

if TYPE_CHECKING:
    from opi.runtime.tasks.persistent_task_progress import PersistentTaskProgressManager

logger = logging.getLogger(__name__)


async def handle_backup(
    payload: dict[str, Any],
    progress: PersistentTaskProgressManager,
) -> dict:
    """Execute a deployment backup via the async task queue.

    Translates the payload into a :class:`BackupRunRequest`, runs the use case and reports
    its outcome on the task; the steps themselves live in ``opi/manager/backup/run.py``.
    """
    project_name = payload["project_name"]
    deployment_name = payload["deployment_name"]
    resource_types = payload.get("resource_types", DEFAULT_BACKUP_RESOURCE_TYPES)
    # Default to scheduled for legacy tasks that predate the trigger field.
    trigger = payload.get("trigger", "scheduled")

    resolve_task = progress.add_task(steps.PROJECT_DEPLOYMENT_LOOKUP)
    try:
        _project, project_data, app_namespace, current_cluster = await resolve_deployment_target(
            project_name, deployment_name
        )
    except (ValueError, RuntimeError, KeyError, OSError, ConnectionError) as e:
        progress.fail_task(resolve_task, str(e))
        progress.fail_project(str(e))
        return {"success": False, "error": str(e)}
    progress.complete_task(resolve_task)

    backup_task = progress.add_task(steps.BACKUP_RUN)
    result = await backup_deployment(
        BackupRunRequest(
            project_name=project_name,
            deployment_name=deployment_name,
            project_data=project_data,
            app_namespace=app_namespace,
            cluster=current_cluster,
            resource_types=resource_types,
            trigger=trigger,
            backup_run_id=generate_backup_run_id(),
        ),
        progress=progress,
        parent_task_id=backup_task,
    )

    if result.success:
        progress.complete_task(backup_task)
        progress.complete_project()
        logger.info(
            "Backup completed: %d resources backed up for %s/%s",
            result.total_items,
            project_name,
            deployment_name,
        )
    else:
        progress.fail_task(backup_task, "Een of meer backups zijn mislukt")
        progress.fail_project("Backup gedeeltelijk mislukt")
    return backup_run_answer(result)


async def handle_restore(
    payload: dict[str, Any],
    progress: PersistentTaskProgressManager,
) -> dict:
    """Execute a deployment restore via the async task queue.

    Uses PersistentTaskProgressManager for database-backed progress tracking.
    """
    project_name = payload["project_name"]
    backup_run_id = payload["backup_run_id"]
    target_deployment = payload["target_deployment"]
    # The parts out of the payload are the ones of this run, already fetched by the wizard.
    backup_items = payload.get("backup_items", [])
    create_new_deployment = payload.get("create_new_deployment", False)
    source_deployment = payload.get("source_deployment", "")
    deployment_config = payload.get("deployment_config")
    # The deployment the new one takes its config from, empty for an empty deployment.
    config_source = payload.get("config_source") or None

    try:
        if create_new_deployment:
            # An empty run or source passes the schema (both are plain strings) and then
            # breaks every bit of processing on clone-from, so it is refused here.
            if not backup_run_id or not source_deployment:
                msg = (
                    f"Backup-run '{backup_run_id}' is niet gevonden bij project '{project_name}', "
                    "dus er valt niets terug te zetten"
                )
                lookup_task = progress.add_task(steps.BACKUP_RUN_LOOKUP)
                progress.fail_task(lookup_task, msg)
                progress.fail_project(msg)
                return {"success": False, "error": msg}

            pvc_items = [item for item in backup_items if item.get("resource_type") == "pvc"]

            # Step 1: Create new deployment
            create_task = progress.add_task(steps.DEPLOYMENT_CREATE)
            try:
                await _create_deployment_from_source(
                    project_name,
                    target_deployment,
                    source_deployment,
                    deployment_config=deployment_config,
                    backup_run_id=backup_run_id,
                    config_source=config_source,
                )
            except (ValueError, RuntimeError, KeyError, OSError, ConnectionError) as e:
                progress.fail_task(create_task, str(e))
                progress.fail_project(str(e))
                return {"success": False, "error": str(e)}
            progress.complete_task(create_task)

            # Step 2: Pre-create PVCs
            if pvc_items:
                pvc_task = progress.add_task(steps.PVC_RESTORE)
                try:
                    await _pre_restore_pvcs(
                        project_name=project_name,
                        source_deployment=source_deployment,
                        target_deployment=target_deployment,
                        pvc_items=pvc_items,
                        task_progress=progress,
                        pvc_task_id=pvc_task,
                    )
                except (ValueError, RuntimeError, KeyError, OSError, ConnectionError) as e:
                    progress.fail_task(pvc_task, str(e))
                    progress.fail_project(str(e))
                    return {"success": False, "error": str(e)}
                progress.complete_task(pvc_task)

            # Step 3: Provision infrastructure and restore data
            # Database and minio data is restored by their respective managers
            # during provisioning (clone-from type "backup", which names the run).
            # PVC data was pre-restored in step 2.
            try:
                await _provision_deployment_infrastructure(project_name, target_deployment, progress)
            except (ValueError, RuntimeError, KeyError, OSError, ConnectionError) as e:
                progress.fail_project(str(e))
                return {"success": False, "error": str(e)}
            progress.complete_project()

        else:
            # Existing deployment restore
            resolve_task = progress.add_task(steps.PROJECT_DEPLOYMENT_LOOKUP)
            try:
                project, project_data, app_namespace, current_cluster = await resolve_deployment_target(
                    project_name, target_deployment
                )
            except (ValueError, RuntimeError, KeyError, OSError, ConnectionError) as e:
                progress.fail_task(resolve_task, str(e))
                progress.fail_project(str(e))
                return {"success": False, "error": str(e)}
            progress.complete_task(resolve_task)

            restore_task = progress.add_task(steps.BACKUP_RESTORE)
            all_success = await restore_items_with_progress(
                backup_items=backup_items,
                project_name=project_name,
                target_deployment=target_deployment,
                project_data=project_data,
                app_namespace=app_namespace,
                current_cluster=current_cluster,
                progress=progress,
                restore_task_id=restore_task,
            )

            if all_success:
                progress.complete_task(restore_task)

                # Re-provision infrastructure to update credentials and regenerate manifests.
                # The restore may have changed database passwords (new versioned database),
                # so process_project_from_git ensures K8s secrets match the actual credentials.
                try:
                    await _provision_deployment_infrastructure(project_name, target_deployment, progress)
                except (ValueError, RuntimeError, KeyError, OSError, ConnectionError) as e:
                    logger.exception("Failed to re-provision after restore for %s/%s", project_name, target_deployment)
                    progress.fail_project(f"Herstel gelukt maar configuratie bijwerken mislukt: {e}")
                    return {"success": False, "error": str(e)}

                progress.complete_project()
            else:
                # The answer has to say what happened: a half-failed restore is not a
                # success, however green the steps before it were.
                progress.fail_task(restore_task, "Een of meer herstelbewerkingen zijn mislukt")
                progress.fail_project("Herstel gedeeltelijk mislukt")
                return {"success": False, "error": "Herstel gedeeltelijk mislukt"}

        return {"success": True}

    except (ValueError, RuntimeError, KeyError, OSError, ConnectionError) as e:
        logger.exception("Restore failed unexpectedly")
        progress.fail_project(f"Onverwachte fout: {e}")
        return {"success": False, "error": str(e)}
