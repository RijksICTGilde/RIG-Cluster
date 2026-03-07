"""Background task wrappers for backup and restore operations.

These functions run as FastAPI BackgroundTasks, providing progress tracking
via TaskProgressManager while executing backup/restore business logic.
"""

from __future__ import annotations

import logging
from typing import Any

from opi.core.task_manager import TaskProgressManager

logger = logging.getLogger(__name__)


async def run_backup_task(
    task_id: str,
    project_name: str,
    deployment_name: str,
    resource_types: list[str],
) -> None:
    """Run a deployment backup as a background task with progress tracking.

    Wraps the backup logic from backup_router.backup_project_deployment()
    with TaskProgressManager subtasks per resource type.
    """
    task_progress = TaskProgressManager(task_id, project_name)

    try:
        # Task 1: Resolve project and deployment info
        resolve_task = task_progress.add_task("Project en deployment opzoeken")
        try:
            project, project_data, app_namespace, current_cluster = await _resolve_deployment_info(
                project_name, deployment_name
            )
        except Exception as e:
            task_progress.fail_task(resolve_task, str(e))
            task_progress.fail_project(str(e))
            return
        task_progress.complete_task(resolve_task)

        # Task 2: Run backups
        backup_task = task_progress.add_task("Backup uitvoeren")

        from opi.connectors.kubectl import create_kubectl_connector
        from opi.handlers.project_file_handler import create_project_file_handler
        from opi.manager.backup import (
            create_backup_manager,
            create_bucket_backup_manager,
            create_database_backup_manager,
        )
        from opi.services import ServiceAdapter, ServiceType
        from opi.utils.naming import generate_backup_run_id
        from opi.utils.secrets import DatabaseSecret, MinIOSecret

        backup_run_id = generate_backup_run_id()
        project_file_handler = create_project_file_handler()
        all_success = True
        total_results = 0

        # PVC backup
        if "pvc" in resource_types:
            pvc_subtask = task_progress.add_subtask(backup_task, "PVC backup")
            try:
                from opi.core.cluster_config import get_prefixed_namespace

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
                    task_progress.fail_subtask(pvc_subtask, f"{len(failed)} PVC backup(s) mislukt")
                else:
                    task_progress.complete_subtask(pvc_subtask)

                # Also backup infra namespace if applicable
                if ServiceAdapter.project_uses_infrastructure_namespace(project.model_dump()):
                    raw_namespace = project_file_handler.extract_deployment_namespace(project_data, deployment_name)
                    infra_namespace = get_prefixed_namespace(current_cluster, f"{raw_namespace}-infra")
                    try:
                        infra_results = await backup_manager.backup_namespace(infra_namespace)
                        total_results += len(infra_results)
                    except Exception as e:
                        logger.warning("Failed to backup infra namespace %s: %s", infra_namespace, e)
            except Exception as e:
                all_success = False
                task_progress.fail_subtask(pvc_subtask, str(e))
                logger.exception("PVC backup failed for %s/%s", project_name, deployment_name)

        # Database backup
        database_service_types = [
            ServiceType.POSTGRESQL_DATABASE.value,
            ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value,
        ]
        if "database" in resource_types and project_file_handler.deployment_uses_service(
            project_data, deployment_name, database_service_types
        ):
            db_subtask = task_progress.add_subtask(backup_task, "Database backup")
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
                        task_progress.fail_subtask(db_subtask, "Database backup mislukt")
                    else:
                        task_progress.complete_subtask(db_subtask)
                else:
                    task_progress.complete_subtask(db_subtask)
            except Exception as e:
                all_success = False
                task_progress.fail_subtask(db_subtask, str(e))
                logger.exception("Database backup failed for %s/%s", project_name, deployment_name)

        # MinIO backup
        minio_service_types = [ServiceType.MINIO_STORAGE.value]
        if "minio" in resource_types and project_file_handler.deployment_uses_service(
            project_data, deployment_name, minio_service_types
        ):
            minio_subtask = task_progress.add_subtask(backup_task, "MinIO backup")
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
                        task_progress.fail_subtask(minio_subtask, "MinIO backup mislukt")
                    else:
                        task_progress.complete_subtask(minio_subtask)
                else:
                    task_progress.complete_subtask(minio_subtask)
            except Exception as e:
                all_success = False
                task_progress.fail_subtask(minio_subtask, str(e))
                logger.exception("MinIO backup failed for %s/%s", project_name, deployment_name)

        if all_success:
            task_progress.complete_task(backup_task)
            task_progress.complete_project()
            logger.info(
                "Backup task %s completed: %d resources backed up for %s/%s",
                task_id,
                total_results,
                project_name,
                deployment_name,
            )
        else:
            task_progress.fail_task(backup_task, "Een of meer backups zijn mislukt")
            task_progress.fail_project("Backup gedeeltelijk mislukt")

    except Exception as e:
        logger.exception("Backup task %s failed unexpectedly", task_id)
        task_progress.fail_project(f"Onverwachte fout: {e}")


async def run_restore_task(
    task_id: str,
    project_name: str,
    backup_run_id: str,
    target_deployment: str,
    backup_items: list[dict[str, Any]],
) -> None:
    """Run a deployment restore as a background task with progress tracking.

    Restores all resources from a backup run to the target deployment.
    Each backup item in the run is restored individually with progress tracking.
    """
    task_progress = TaskProgressManager(task_id, project_name)

    try:
        # Task 1: Resolve deployment info
        resolve_task = task_progress.add_task("Project en deployment opzoeken")
        try:
            project, project_data, app_namespace, current_cluster = await _resolve_deployment_info(
                project_name, target_deployment
            )
        except Exception as e:
            task_progress.fail_task(resolve_task, str(e))
            task_progress.fail_project(str(e))
            return
        task_progress.complete_task(resolve_task)

        # Task 2: Restore each resource
        restore_task = task_progress.add_task("Backup herstellen")
        all_success = True

        for item in backup_items:
            resource_type = item.get("resource_type", "unknown")
            component_name = item.get("component_name", "")
            snapshot_id = item.get("snapshot_id", "")
            reference_name = item.get("storage_name") or item.get("reference_name", "")

            label = f"{resource_type.upper()}: {component_name or reference_name}"
            subtask = task_progress.add_subtask(restore_task, f"Herstellen {label}")

            try:
                await _restore_single_resource(
                    project_name=project_name,
                    deployment_name=target_deployment,
                    resource_type=resource_type,
                    snapshot_id=snapshot_id,
                    component_name=component_name,
                    reference_name=reference_name,
                    project_data=project_data,
                    app_namespace=app_namespace,
                    current_cluster=current_cluster,
                )
                task_progress.complete_subtask(subtask)
            except Exception as e:
                all_success = False
                task_progress.fail_subtask(subtask, str(e))
                logger.exception("Failed to restore %s for %s/%s", label, project_name, target_deployment)

        if all_success:
            task_progress.complete_task(restore_task)
            task_progress.complete_project()
            logger.info("Restore task %s completed for %s/%s", task_id, project_name, target_deployment)
        else:
            task_progress.fail_task(restore_task, "Een of meer herstelbewerkingen zijn mislukt")
            task_progress.fail_project("Herstel gedeeltelijk mislukt")

    except Exception as e:
        logger.exception("Restore task %s failed unexpectedly", task_id)
        task_progress.fail_project(f"Onverwachte fout: {e}")


async def _resolve_deployment_info(
    project_name: str,
    deployment_name: str,
) -> tuple[Any, dict[str, Any], str, str]:
    """Resolve project, data, namespace, and cluster for a deployment.

    Returns:
        Tuple of (project, project_data, app_namespace, current_cluster)

    Raises:
        ValueError: If project/deployment not found or on wrong cluster.
    """
    from opi.core.cluster_config import get_prefixed_namespace
    from opi.core.config import settings
    from opi.handlers.project_file_handler import create_project_file_handler
    from opi.services.project_service import get_project_service

    project_service = get_project_service()
    project = project_service.get_project(project_name)
    if not project or not project.data:
        msg = f"Project '{project_name}' niet gevonden"
        raise ValueError(msg)

    project_data = project.data
    project_file_handler = create_project_file_handler()

    raw_namespace = project_file_handler.extract_deployment_namespace(project_data, deployment_name)
    deployment_cluster = project_file_handler.extract_deployment_cluster(project_data, deployment_name)

    if not raw_namespace:
        msg = f"Deployment '{deployment_name}' niet gevonden in project '{project_name}'"
        raise ValueError(msg)

    current_cluster = settings.CLUSTER_MANAGER
    if deployment_cluster != current_cluster:
        msg = (
            f"Deployment '{deployment_name}' is op cluster '{deployment_cluster}', "
            f"maar deze operations-manager draait op '{current_cluster}'"
        )
        raise ValueError(msg)

    app_namespace = get_prefixed_namespace(deployment_cluster, raw_namespace)
    return project, project_data, app_namespace, current_cluster


async def _restore_single_resource(
    project_name: str,
    deployment_name: str,
    resource_type: str,
    snapshot_id: str,
    component_name: str,
    reference_name: str,
    project_data: dict[str, Any],
    app_namespace: str,
    current_cluster: str,
) -> None:
    """Restore a single resource using the restore router's versioning logic.

    Calls the appropriate _restore_*_with_versioning() function from
    restore_router and updates the project file.
    """
    from opi.connectors.git import create_git_connector_for_project_files
    from opi.handlers.project_file_handler import create_project_file_handler, save_project_file
    from opi.services.project_service import get_project_service

    project_file_handler = create_project_file_handler()

    if resource_type == "pvc":
        from opi.api.restore_router import _restore_pvc_with_versioning

        result = await _restore_pvc_with_versioning(
            project_name=project_name,
            deployment_name=deployment_name,
            component_name=component_name,
            storage_name=reference_name,
            snapshot_id=snapshot_id,
            deployment_cluster=current_cluster,
            namespace=app_namespace,
            project_data=project_data,
            project_file_handler=project_file_handler,
        )
    elif resource_type == "database":
        from opi.api.restore_router import _restore_database_with_versioning

        result = await _restore_database_with_versioning(
            project_name=project_name,
            deployment_name=deployment_name,
            component_name=component_name,
            reference_name=reference_name,
            snapshot_id=snapshot_id,
            deployment_cluster=current_cluster,
            namespace=app_namespace,
            project_data=project_data,
            project_file_handler=project_file_handler,
        )
    elif resource_type == "bucket":
        from opi.api.restore_router import _restore_bucket_with_versioning

        result = await _restore_bucket_with_versioning(
            project_name=project_name,
            deployment_name=deployment_name,
            component_name=component_name,
            reference_name=reference_name,
            snapshot_id=snapshot_id,
            deployment_cluster=current_cluster,
            namespace=app_namespace,
            project_data=project_data,
            project_file_handler=project_file_handler,
        )
    else:
        msg = f"Onbekend resource type: {resource_type}"
        raise ValueError(msg)

    if not result.get("success"):
        error = result.get("error", "Onbekende fout")
        msg = f"Herstel mislukt voor {resource_type}: {error}"
        raise RuntimeError(msg)

    # Update project file with new generation
    new_generation = result.get("new_generation")
    if new_generation is not None:
        project_service = get_project_service()
        project = project_service.get_project(project_name)
        if project and project.data:
            if resource_type == "pvc":
                project_file_handler.set_storage_generation(
                    project.data, deployment_name, component_name, reference_name, new_generation
                )
            elif resource_type == "database":
                project_file_handler.set_database_generation(
                    project.data, deployment_name, component_name, reference_name, new_generation
                )
            elif resource_type == "bucket":
                project_file_handler.set_bucket_generation(
                    project.data, deployment_name, component_name, reference_name, new_generation
                )

            save_project_file(project.filename, project.data)
            project_service.load_project_from_data(project.data, project.filename)

            # Commit to git
            git_connector = create_git_connector_for_project_files()
            from io import StringIO

            from ruamel.yaml import YAML

            yaml_instance = YAML()
            yaml_instance.preserve_quotes = True
            yaml_instance.width = 4096
            yaml_output = StringIO()
            yaml_instance.dump(project.data, yaml_output)

            await git_connector.create_or_update_file(
                f"projects/{project_name}.yaml",
                yaml_output.getvalue(),
                do_commit_and_push=True,
                commit_message=f"Restore {resource_type} for {project_name}/{deployment_name} (gen {new_generation})",
            )

    logger.info(
        "Restored %s for %s/%s: %s -> %s (gen %s -> %s)",
        resource_type,
        project_name,
        deployment_name,
        result.get("old_resource_name"),
        result.get("new_resource_name"),
        result.get("old_generation"),
        result.get("new_generation"),
    )
