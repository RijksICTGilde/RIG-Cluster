"""Shared helper functions for backup and restore operations.

These helpers are used by the async task handlers in task_handlers_backup.py
for backup/restore orchestration with progress tracking.
"""

from __future__ import annotations

import copy
import logging
from io import StringIO
from typing import TYPE_CHECKING, Any

from ruamel.yaml import YAML

from opi.api.restore_router import (
    _restore_bucket_with_versioning,
    _restore_database_with_versioning,
    _restore_pvc_with_versioning,
)
from opi.connectors.git import create_git_connector_for_project_files
from opi.core.cluster_config import get_prefixed_namespace, get_storage_access_modes, get_storage_class_name
from opi.core.config import settings
from opi.handlers.project_file_handler import (
    create_project_file_handler,
    extract_storage_from_component_services,
    save_project_file,
)
from opi.manager.backup import create_backup_manager
from opi.manager.project_manager import ProjectManager, create_project_manager
from opi.services import CloneFromType
from opi.services.project_service import get_project_service
from opi.utils.naming import generate_pvc_name, generate_storage_name, generate_unique_name

if TYPE_CHECKING:
    from opi.core.task_manager import TaskProgressManager

logger = logging.getLogger(__name__)


async def restore_items_with_progress(
    backup_items: list[dict[str, Any]],
    project_name: str,
    target_deployment: str,
    project_data: dict[str, Any],
    app_namespace: str,
    current_cluster: str,
    progress: TaskProgressManager,
    restore_task_id: str,
) -> bool:
    """Restore a list of backup items with per-item progress tracking.

    Iterates over backup_items, resolves each item's type and metadata,
    calls _restore_single_resource(), and tracks subtask progress.

    Returns True if all items restored successfully, False otherwise.
    """
    all_success = True

    for item in backup_items:
        resource_type = item.get("resource_type", "unknown")
        component_name = item.get("component_name", "")
        snapshot_id = item.get("snapshot_id", "")
        reference_name = item.get("storage_name") or item.get("reference_name", "")

        label = f"{resource_type.upper()}: {component_name or reference_name}"
        subtask = progress.add_subtask(restore_task_id, f"Herstellen {label}")

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
            progress.complete_subtask(subtask)
        except (ValueError, RuntimeError, KeyError, OSError, ConnectionError) as e:
            all_success = False
            progress.fail_subtask(subtask, str(e))
            logger.exception("Failed to restore %s for %s/%s", label, project_name, target_deployment)

    return all_success


async def _create_deployment_from_source(
    project_name: str,
    target_deployment: str,
    source_deployment: str,
    deployment_config: dict[str, Any] | None = None,
    backup_items: list[dict[str, Any]] | None = None,
) -> None:
    """Create a new deployment by copying structure from a source deployment.

    Copies the deployment configuration (cluster, namespace, services, etc.)
    and sets clone-from type to "backup" so managers restore data from the
    backup during infrastructure provisioning.

    When deployment_config is provided (from the wizard editables), the user's
    choices for domain fields (subdomain, base-domain, domain-format) and
    clone-from are used instead of copying from source.
    """
    project_service = get_project_service()
    project = project_service.get_project(project_name)
    if not project or not project.data:
        msg = f"Project '{project_name}' niet gevonden"
        raise ValueError(msg)

    project_data = project.data

    # Find source deployment
    source_dep = None
    for dep in project_data.get("deployments", []):
        if dep.get("name") == source_deployment:
            source_dep = dep
            break

    if not source_dep:
        msg = f"Bron deployment '{source_deployment}' niet gevonden in project '{project_name}'"
        raise ValueError(msg)

    # Build new deployment from the wizard config (user's choices)
    # and only infer system-managed fields that the wizard doesn't collect.
    new_deployment: dict[str, Any] = copy.deepcopy(deployment_config) if deployment_config else {}
    new_deployment["name"] = target_deployment

    # Set clone-from for backup restore
    new_deployment["clone-from"] = {
        "type": CloneFromType.BACKUP.value,
        "reference": source_deployment,
        "mode": "once",
        "backup_items": backup_items or [],
    }

    # Infer system-managed fields from source deployment or project
    if not new_deployment.get("cluster"):
        new_deployment["cluster"] = source_dep.get("cluster") or (project_data.get("clusters", [None])[0])

    if not new_deployment.get("namespace"):
        new_deployment["namespace"] = source_dep.get("namespace") or project_name

    if not new_deployment.get("repository"):
        new_deployment["repository"] = source_dep.get("repository") or (
            project_data.get("repositories", [{}])[0].get("name", "")
        )

    # Copy components from source deployment (deep copy to avoid shared references)
    if "components" not in new_deployment and "components" in source_dep:
        new_deployment["components"] = copy.deepcopy(source_dep["components"])

    # Auto-set subdomain: if source subdomain matches source name, use target name
    if "subdomain" not in new_deployment:
        source_subdomain = source_dep.get("subdomain", "")
        if source_subdomain == source_deployment or source_subdomain:
            new_deployment["subdomain"] = target_deployment

    # Add deployment to project data
    project_data["deployments"].append(new_deployment)

    # Save and commit
    save_project_file(project.filename, project_data)
    project_service.load_project_from_data(project_data, project.filename)

    git_connector = await create_git_connector_for_project_files(project_name)
    yaml_instance = YAML()
    yaml_instance.preserve_quotes = True
    yaml_instance.width = 4096
    yaml_output = StringIO()
    yaml_instance.dump(project_data, yaml_output)

    await git_connector.create_or_update_file(
        f"projects/{project_name}.yaml",
        yaml_output.getvalue(),
        do_commit_and_push=True,
        commit_message=(
            f"Add deployment '{target_deployment}' to project '{project_name}' (restore from '{source_deployment}')"
        ),
    )
    logger.info(
        "Created deployment '%s' in project '%s' (cloned from '%s' for backup restore)",
        target_deployment,
        project_name,
        source_deployment,
    )


async def _pre_restore_pvcs(
    project_name: str,
    source_deployment: str,
    target_deployment: str,
    pvc_items: list[dict[str, Any]],
    task_progress: TaskProgressManager,
    pvc_task_id: str,
) -> None:
    """Pre-create PVCs with backup data before infrastructure provisioning.

    For new deployment restores, this creates the namespace and restores PVC data
    before process_project_from_git runs. When ArgoCD later syncs the generated
    manifests, it adopts the existing (already-filled) PVCs via Replace=false.

    Args:
        project_name: Name of the project
        source_deployment: Original deployment the backup came from
        target_deployment: New deployment being created
        pvc_items: Backup items with resource_type=="pvc"
        task_progress: Progress tracker
        pvc_task_id: Parent task ID for subtask tracking
    """
    # Load project data
    project_service = get_project_service()
    project = project_service.get_project(project_name)
    if not project or not project.data:
        msg = f"Project '{project_name}' niet gevonden"
        raise ValueError(msg)

    project_data = project.data
    project_file_handler = create_project_file_handler()

    # Resolve namespace and cluster
    raw_namespace = project_file_handler.extract_deployment_namespace(project_data, target_deployment)
    deployment_cluster = project_file_handler.extract_deployment_cluster(project_data, target_deployment)

    if not raw_namespace or not deployment_cluster:
        msg = f"Namespace of cluster niet gevonden voor deployment '{target_deployment}'"
        raise ValueError(msg)

    current_cluster = settings.CLUSTER_MANAGER
    if deployment_cluster != current_cluster:
        msg = (
            f"Deployment '{target_deployment}' is op cluster '{deployment_cluster}', "
            f"maar deze operations-manager draait op '{current_cluster}'"
        )
        raise ValueError(msg)

    app_namespace = get_prefixed_namespace(deployment_cluster, raw_namespace)

    # Create namespace using existing ProjectManager method (idempotent)
    project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")
    try:
        await project_manager.check_and_create_namespaces(target_deployment)
    finally:
        await project_manager.close()

    # Build base component lookup
    base_components = {c.get("name"): c for c in project_data.get("components", [])}

    backup_manager = create_backup_manager()
    storage_class = get_storage_class_name(deployment_cluster)
    access_modes = get_storage_access_modes(deployment_cluster)

    for item in pvc_items:
        component_name = item.get("component_name", "")
        storage_name = item.get("storage_name") or item.get("reference_name", "")
        snapshot_id = item.get("snapshot_id", "")
        source_generation = item.get("generation", 0)

        label = f"PVC: {component_name}/{storage_name}"
        subtask = task_progress.add_subtask(pvc_task_id, f"Herstellen {label}")

        # Source PVC name: from original deployment (what the backup was tagged with)
        source_unique_name = generate_unique_name(source_deployment, component_name)
        source_pvc_name = generate_pvc_name(source_unique_name, storage_name, source_generation)

        # Target PVC name: gen 0 for new deployment
        target_unique_name = generate_unique_name(target_deployment, component_name)
        target_pvc_name = generate_pvc_name(target_unique_name, storage_name, 0)

        # Get storage config from base component
        base_component = base_components.get(component_name, {})
        storage_list = extract_storage_from_component_services(base_component)

        target_storage = None
        for idx, storage in enumerate(storage_list):
            if storage.get("type") != "persistent":
                continue
            s_name = storage.get("name")
            if not s_name:
                mount_path = storage.get("mount-path", "") or storage.get("mount_path", "")
                s_name = generate_storage_name(mount_path, idx)
            if s_name == storage_name:
                target_storage = storage
                break

        storage_size = target_storage.get("size", "10Gi") if target_storage else "10Gi"
        backup_enabled = target_storage.get("backup", True) if target_storage else True

        logger.info(
            "Pre-restoring PVC %s -> %s (source gen %s) in %s",
            source_pvc_name,
            target_pvc_name,
            source_generation,
            app_namespace,
        )

        result = await backup_manager.restore_to_project_pvc(
            cluster=deployment_cluster,
            namespace=app_namespace,
            source_pvc_name=source_pvc_name,
            target_pvc_name=target_pvc_name,
            storage_size=storage_size,
            storage_class=storage_class,
            access_modes=access_modes,
            snapshot_id=snapshot_id,
            backup_enabled=backup_enabled,
            project_name=project_name,
        )

        if not result.success:
            task_progress.fail_subtask(subtask, f"PVC restore mislukt: {result.error}")
            msg = f"PVC restore mislukt voor {source_pvc_name} -> {target_pvc_name}: {result.error}"
            raise RuntimeError(msg)

        task_progress.complete_subtask(subtask)
        logger.info(
            "Pre-restored PVC %s -> %s in %s",
            source_pvc_name,
            target_pvc_name,
            app_namespace,
        )


async def _provision_deployment_infrastructure(
    project_name: str,
    target_deployment: str,
    task_progress: TaskProgressManager,
) -> None:
    """Provision infrastructure for a newly created deployment.

    Calls process_project_from_git to create namespace, PVC, database, MinIO
    resources. Because clone-from type is "backup", managers will skip live
    data cloning and create empty resources instead.
    """
    project_manager = create_project_manager()
    project_file_path = f"projects/{project_name}.yaml"
    success = await project_manager.process_project_from_git(
        project_file_path,
        task_progress_manager=task_progress,
        deployment_name=target_deployment,
    )
    if not success:
        msg = f"Infrastructuur aanmaken mislukt voor deployment '{target_deployment}'"
        raise RuntimeError(msg)

    logger.info(
        "Infrastructure provisioned for deployment '%s' in project '%s'",
        target_deployment,
        project_name,
    )


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
    project_file_handler = create_project_file_handler()

    if resource_type == "pvc":
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
            git_connector = await create_git_connector_for_project_files(project_name)
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
