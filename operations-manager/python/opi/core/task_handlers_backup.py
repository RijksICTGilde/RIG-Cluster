"""Task handlers for backup and restore operations.

These handlers are registered with the TaskWorker and executed via the
async task queue. Shared helper functions live in backup_tasks.py.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opi.connectors.kubectl import create_kubectl_connector
from opi.core.backup_constants import DEFAULT_BACKUP_RESOURCE_TYPES
from opi.core.backup_tasks import (
    _create_deployment_from_source,
    _pre_restore_pvcs,
    _provision_deployment_infrastructure,
    _resolve_deployment_info,
    restore_items_with_progress,
)
from opi.core.cluster_config import get_prefixed_namespace
from opi.handlers.project_file_handler import create_project_file_handler
from opi.manager.backup import (
    create_backup_manager,
    create_bucket_backup_manager,
    create_database_backup_manager,
)
from opi.services import ServiceAdapter, ServiceType
from opi.utils.naming import generate_backup_run_id
from opi.utils.secrets import DatabaseSecret, MinIOSecret

if TYPE_CHECKING:
    from opi.core.persistent_task_progress import PersistentTaskProgressManager

logger = logging.getLogger(__name__)


async def handle_backup(
    payload: dict[str, Any],
    progress: PersistentTaskProgressManager,
) -> dict:
    """Execute a deployment backup via the async task queue.

    Uses PersistentTaskProgressManager for database-backed progress tracking.
    """
    project_name = payload["project_name"]
    deployment_name = payload["deployment_name"]
    resource_types = payload.get("resource_types", DEFAULT_BACKUP_RESOURCE_TYPES)

    # Task 1: Resolve project and deployment info
    resolve_task = progress.add_task("Project en deployment opzoeken")
    try:
        project, project_data, app_namespace, current_cluster = await _resolve_deployment_info(
            project_name, deployment_name
        )
    except (ValueError, RuntimeError, KeyError, OSError, ConnectionError) as e:
        progress.fail_task(resolve_task, str(e))
        progress.fail_project(str(e))
        return {"success": False, "error": str(e)}
    progress.complete_task(resolve_task)

    # Task 2: Run backups
    backup_task = progress.add_task("Backup uitvoeren")

    backup_run_id = generate_backup_run_id()
    project_file_handler = create_project_file_handler()
    all_success = True
    total_results = 0

    # PVC backup
    if "pvc" in resource_types:
        pvc_subtask = progress.add_subtask(backup_task, "PVC backup")
        try:
            backup_manager = create_backup_manager()
            app_results = await backup_manager.backup_project_deployment(
                project_name=project_name,
                project_data=project_data,
                deployment_name=deployment_name,
                namespace=app_namespace,
                cluster=current_cluster,
                backup_run_id=backup_run_id,
            )
            total_results += len(app_results)
            failed = [r for r in app_results if not r.success]
            if failed:
                all_success = False
                progress.fail_subtask(pvc_subtask, f"{len(failed)} PVC backup(s) mislukt")
            else:
                progress.complete_subtask(pvc_subtask)

            # Also backup infra namespace if applicable
            if ServiceAdapter.project_uses_infrastructure_namespace(project.model_dump()):
                raw_namespace = project_file_handler.extract_deployment_namespace(project_data, deployment_name)
                infra_namespace = get_prefixed_namespace(current_cluster, f"{raw_namespace}-infra")
                try:
                    infra_results = await backup_manager.backup_namespace(infra_namespace)
                    total_results += len(infra_results)
                except (ValueError, RuntimeError, KeyError, OSError, ConnectionError):
                    logger.exception("Failed to backup infra namespace %s", infra_namespace)
        except (ValueError, RuntimeError, OSError, ConnectionError) as e:
            all_success = False
            progress.fail_subtask(pvc_subtask, str(e))
            logger.exception("PVC backup failed for %s/%s", project_name, deployment_name)

    # Database backup
    database_service_types = [
        ServiceType.POSTGRESQL_DATABASE.value,
        ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value,
    ]
    if "database" in resource_types and project_file_handler.deployment_uses_service(
        project_data, deployment_name, database_service_types
    ):
        db_subtask = progress.add_subtask(backup_task, "Database backup")
        try:
            kubectl = create_kubectl_connector()
            db_components = project_file_handler.get_components_using_service(
                project_data, deployment_name, database_service_types
            )
            db_secret = await DatabaseSecret.get_data(
                kubectl_connector=kubectl,
                namespace=app_namespace,
                prefix=deployment_name,
            )
            if db_secret and db_components:
                database_backup_manager = create_database_backup_manager()
                component_info = db_components[0]
                component_name = component_info["component_name"]
                reference_name = component_info["reference_name"]
                generation = project_file_handler.get_database_generation(
                    project_data, deployment_name, component_name, reference_name
                )
                uses_namespace_db = project_file_handler.deployment_uses_service(
                    project_data, deployment_name, [ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value]
                )
                source_type = "namespace" if uses_namespace_db else "shared"

                db_result = await database_backup_manager.backup_database(
                    namespace=app_namespace,
                    database_host=db_secret.host,
                    database_port=db_secret.port,
                    database_name=db_secret.database,
                    database_user=db_secret.username,
                    database_password=db_secret.password,
                    reference_name=reference_name,
                    source_type=source_type,
                    project_name=project_name,
                    deployment_name=deployment_name,
                    component_name=component_name,
                    generation=generation,
                    cluster=current_cluster,
                    backup_run_id=backup_run_id,
                )
                total_results += 1
                if not db_result.success:
                    all_success = False
                    progress.fail_subtask(db_subtask, "Database backup mislukt")
                else:
                    progress.complete_subtask(db_subtask)
            else:
                progress.complete_subtask(db_subtask)
        except (ValueError, RuntimeError, OSError, ConnectionError) as e:
            all_success = False
            progress.fail_subtask(db_subtask, str(e))
            logger.exception("Database backup failed for %s/%s", project_name, deployment_name)

    # MinIO backup
    minio_service_types = [ServiceType.MINIO_STORAGE.value]
    if "minio" in resource_types and project_file_handler.deployment_uses_service(
        project_data, deployment_name, minio_service_types
    ):
        minio_subtask = progress.add_subtask(backup_task, "MinIO backup")
        try:
            kubectl = create_kubectl_connector()
            minio_components = project_file_handler.get_components_using_service(
                project_data, deployment_name, minio_service_types
            )
            minio_secret = await MinIOSecret.get_data(
                kubectl_connector=kubectl,
                namespace=app_namespace,
                prefix=deployment_name,
            )
            if minio_secret and minio_components:
                bucket_backup_manager = create_bucket_backup_manager()
                component_info = minio_components[0]
                component_name = component_info["component_name"]
                reference_name = component_info["reference_name"]
                generation = project_file_handler.get_bucket_generation(
                    project_data, deployment_name, component_name, reference_name
                )

                bucket_result = await bucket_backup_manager.backup_bucket(
                    namespace=app_namespace,
                    source_minio_endpoint=minio_secret.endpoint_url,
                    source_bucket_name=minio_secret.bucket_name,
                    source_access_key=minio_secret.access_key,
                    source_secret_key=minio_secret.secret_key,
                    reference_name=reference_name,
                    source_type="shared",
                    use_kopia=True,
                    project_name=project_name,
                    deployment_name=deployment_name,
                    component_name=component_name,
                    generation=generation,
                    cluster=current_cluster,
                    backup_run_id=backup_run_id,
                )
                total_results += 1
                if not bucket_result.success:
                    all_success = False
                    progress.fail_subtask(minio_subtask, "MinIO backup mislukt")
                else:
                    progress.complete_subtask(minio_subtask)
            else:
                progress.complete_subtask(minio_subtask)
        except (ValueError, RuntimeError, OSError, ConnectionError) as e:
            all_success = False
            progress.fail_subtask(minio_subtask, str(e))
            logger.exception("MinIO backup failed for %s/%s", project_name, deployment_name)

    if all_success:
        progress.complete_task(backup_task)
        progress.complete_project()
        logger.info(
            "Backup completed: %d resources backed up for %s/%s",
            total_results,
            project_name,
            deployment_name,
        )
        return {"success": True, "total_results": total_results}
    else:
        progress.fail_task(backup_task, "Een of meer backups zijn mislukt")
        progress.fail_project("Backup gedeeltelijk mislukt")
        return {"success": False, "error": "Backup gedeeltelijk mislukt"}


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
    backup_items = payload.get("backup_items", [])
    create_new_deployment = payload.get("create_new_deployment", False)
    source_deployment = payload.get("source_deployment", "")
    deployment_config = payload.get("deployment_config")

    try:
        if create_new_deployment:
            pvc_items = [item for item in backup_items if item.get("resource_type") == "pvc"]

            # Step 1: Create new deployment
            create_task = progress.add_task("Deployment aanmaken")
            try:
                await _create_deployment_from_source(
                    project_name,
                    target_deployment,
                    source_deployment,
                    deployment_config=deployment_config,
                    backup_items=backup_items,
                )
            except (ValueError, RuntimeError, KeyError, OSError, ConnectionError) as e:
                progress.fail_task(create_task, str(e))
                progress.fail_project(str(e))
                return {"success": False, "error": str(e)}
            progress.complete_task(create_task)

            # Step 2: Pre-create PVCs
            if pvc_items:
                pvc_task = progress.add_task("PVC data herstellen")
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
            # during provisioning (clone-from type "backup" with backup_items).
            # PVC data was pre-restored in step 2.
            infra_task = progress.add_task("Infrastructuur aanmaken en data herstellen")
            try:
                await _provision_deployment_infrastructure(project_name, target_deployment, progress)
            except (ValueError, RuntimeError, KeyError, OSError, ConnectionError) as e:
                progress.fail_task(infra_task, str(e))
                progress.fail_project(str(e))
                return {"success": False, "error": str(e)}
            progress.complete_task(infra_task)
            progress.complete_project()

        else:
            # Existing deployment restore
            resolve_task = progress.add_task("Project en deployment opzoeken")
            try:
                project, project_data, app_namespace, current_cluster = await _resolve_deployment_info(
                    project_name, target_deployment
                )
            except (ValueError, RuntimeError, KeyError, OSError, ConnectionError) as e:
                progress.fail_task(resolve_task, str(e))
                progress.fail_project(str(e))
                return {"success": False, "error": str(e)}
            progress.complete_task(resolve_task)

            restore_task = progress.add_task("Backup herstellen")
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
                infra_task = progress.add_task("Manifesten bijwerken")
                try:
                    await _provision_deployment_infrastructure(project_name, target_deployment, progress)
                    progress.complete_task(infra_task)
                except (ValueError, RuntimeError, KeyError, OSError, ConnectionError) as e:
                    logger.exception("Failed to re-provision after restore for %s/%s", project_name, target_deployment)
                    progress.fail_task(infra_task, str(e))
                    progress.fail_project(f"Herstel gelukt maar manifesten bijwerken mislukt: {e}")
                    return {"success": False, "error": str(e)}

                progress.complete_project()
            else:
                progress.fail_task(restore_task, "Een of meer herstelbewerkingen zijn mislukt")
                progress.fail_project("Herstel gedeeltelijk mislukt")

        return {"success": True}

    except (ValueError, RuntimeError, KeyError, OSError, ConnectionError) as e:
        logger.exception("Restore failed unexpectedly")
        progress.fail_project(f"Onverwachte fout: {e}")
        return {"success": False, "error": str(e)}
