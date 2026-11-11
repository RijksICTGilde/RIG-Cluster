"""Task handlers for backup and restore operations.

These handlers are registered with the TaskWorker and executed via the async task queue.
The steps they drive live in ``opi/manager/backup/``; the provisioning that reports on the
task itself in backup_tasks.py.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opi.core.backup_constants import DEFAULT_BACKUP_RESOURCE_TYPES
from opi.manager.backup.base import ResourceType
from opi.manager.backup.restore_run import (
    NewDeploymentRestore,
    RefreshPolicy,
    RestoreOutcome,
    RestoreRequest,
    create_deployment_for_restore,
    pre_restore_pvcs,
    restore_deployment,
    restore_item_from_backup_item,
    restore_outcome_answer,
)
from opi.manager.backup.run import (
    BackupRunRequest,
    backup_deployment,
    backup_run_answer,
    resolve_deployment_target,
)
from opi.manager.backup.runs import backup_run_items, backup_run_items_for_restore
from opi.runtime.backup.backup_tasks import (
    _provision_deployment_infrastructure,
    refresh_deployment_with_progress,
)
from opi.services.tasks import progress_steps as steps
from opi.utils.naming import generate_backup_run_id

if TYPE_CHECKING:
    from opi.runtime.tasks.persistent_task_progress import PersistentTaskProgressManager

logger = logging.getLogger(__name__)

#: The restore landed but the deployment still runs on the pre-restore manifests.
REFRESH_FAILED = "Herstel gelukt maar configuratie bijwerken mislukt"


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
    create_new_deployment = payload.get("create_new_deployment", False)
    source_deployment = payload.get("source_deployment", "")
    deployment_config = payload.get("deployment_config")
    # The deployment the new one takes its config from, empty for an empty deployment.
    config_source = payload.get("config_source") or None

    try:
        # An empty run or source passes the schema (both are plain strings) and then breaks
        # every bit of processing on clone-from, so it is refused before anything is read.
        if create_new_deployment and (not backup_run_id or not source_deployment):
            msg = (
                f"Backup-run '{backup_run_id}' is niet gevonden bij project '{project_name}', "
                "dus er valt niets terug te zetten"
            )
            lookup_task = progress.add_task(steps.BACKUP_RUN_LOOKUP)
            progress.fail_task(lookup_task, msg)
            progress.fail_project(msg)
            return {"success": False, "error": msg}

        try:
            backup_items = await _restore_items(payload)
        except (ValueError, RuntimeError, KeyError, OSError, ConnectionError) as e:
            lookup_task = progress.add_task(steps.BACKUP_RUN_LOOKUP)
            progress.fail_task(lookup_task, str(e))
            progress.fail_project(str(e))
            return {"success": False, "error": str(e)}

        if create_new_deployment:
            new_deployment = NewDeploymentRestore(
                project_name=project_name,
                target_deployment=target_deployment,
                source_deployment=source_deployment,
                backup_run_id=backup_run_id,
                deployment_config=deployment_config,
                config_source=config_source,
            )
            pvc_items = [
                restore_item_from_backup_item(item)
                for item in backup_items
                if item.get("resource_type") == ResourceType.PVC.value
            ]

            # Step 1: Create new deployment
            create_task = progress.add_task(steps.DEPLOYMENT_CREATE)
            try:
                await create_deployment_for_restore(new_deployment)
            except (ValueError, RuntimeError, KeyError, OSError, ConnectionError) as e:
                progress.fail_task(create_task, str(e))
                progress.fail_project(str(e))
                return {"success": False, "error": str(e)}
            progress.complete_task(create_task)

            # Step 2: Pre-create PVCs
            if pvc_items:
                pvc_task = progress.add_task(steps.PVC_RESTORE)
                try:
                    await pre_restore_pvcs(new_deployment, pvc_items, progress, pvc_task)
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
                await _provision_deployment_infrastructure(f"projects/{project_name}.yaml", target_deployment, progress)
            except (ValueError, RuntimeError, KeyError, OSError, ConnectionError) as e:
                progress.fail_project(str(e))
                return {"success": False, "error": str(e)}
            progress.complete_project()

        else:
            return await _restore_existing_deployment(payload, backup_items, progress)

        return {"success": True}

    except (ValueError, RuntimeError, KeyError, OSError, ConnectionError) as e:
        logger.exception("Restore failed unexpectedly")
        progress.fail_project(f"Onverwachte fout: {e}")
        return {"success": False, "error": str(e)}


async def _restore_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The items of this restore, read at the start of the task.

    The wizard and the backup-run route name the RUN, not its parts: between choosing a run
    and running the task the run can have been swept or have grown an item, so the items are
    read here instead of coming along with the request that queued the task. An entry point
    naming ONE resource passes it in ``backup_items`` instead.

    Raises:
        ValueError: when the run no longer exists, so an empty restore cannot pass for a
            success.
    """
    items = payload.get("backup_items")
    if items is not None:
        return items

    project_name = payload["project_name"]
    source_deployment = payload.get("source_deployment") or ""
    _project, _project_data, namespace, cluster = await resolve_deployment_target(
        project_name, source_deployment or payload["target_deployment"]
    )
    run_id = payload["backup_run_id"]
    if source_deployment:
        # The caller named which deployment made the run, so the lookup stays inside it.
        return await backup_run_items(project_name, cluster, namespace, source_deployment, run_id)
    return await backup_run_items_for_restore(project_name, cluster, namespace, run_id)


async def _restore_existing_deployment(
    payload: dict[str, Any],
    backup_items: list[dict[str, Any]],
    progress: PersistentTaskProgressManager,
) -> dict:
    """Run the restore use case on a deployment that already exists, and report it."""
    project_name = payload["project_name"]
    target_deployment = payload["target_deployment"]

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
    outcome = await restore_deployment(
        RestoreRequest(
            project_name=project_name,
            target_deployment=target_deployment,
            items=[restore_item_from_backup_item(item) for item in backup_items],
            project_file_path=f"projects/{project.filename}",
            project_data=project_data,
            namespace=app_namespace,
            cluster=current_cluster,
            backup_run_id=payload["backup_run_id"],
            refresh=RefreshPolicy(payload.get("refresh", RefreshPolicy.ON_ANY_SUCCESS.value)),
        ),
        progress=progress,
        parent_task_id=restore_task,
        refresh=refresh_deployment_with_progress(progress),
    )
    return _restore_answer(outcome, progress, restore_task)


def _restore_answer(
    outcome: RestoreOutcome,
    progress: PersistentTaskProgressManager,
    restore_task: str,
) -> dict:
    """The outcome as the task answer, and the same verdict on the progress."""
    answer = restore_outcome_answer(outcome)
    if not outcome.all_restored:
        # A half-failed restore is not a success, however green the steps before it were.
        progress.fail_task(restore_task, "Een of meer herstelbewerkingen zijn mislukt")
        progress.fail_project("Herstel gedeeltelijk mislukt")
        return answer

    progress.complete_task(restore_task)
    if outcome.refresh_error:
        progress.fail_project(f"{REFRESH_FAILED}: {outcome.refresh_error}")
        return answer

    progress.complete_project()
    return answer
