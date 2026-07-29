"""PVC manager for handling PersistentVolumeClaim resources and lifecycle."""

import glob
import logging
import os
import re
from typing import Any

from opi.services import CloneFromType
from opi.utils.naming import generate_manifest_name, generate_pvc_manifest_type

logger = logging.getLogger(__name__)

MARKED_FOR_DELETION_SUFFIX = ".marked-for-deletion.yaml"


class PVCManager:
    """Manager for PVC-related operations and resources."""

    def __init__(self, project_manager: ProjectManager) -> None:
        """
        Initialize the PVCManager with reference to ProjectManager.

        Args:
            project_manager: The main ProjectManager instance for accessing shared resources
        """
        self.project_manager = project_manager

    async def delete_old_pvc_manifests(
        self,
        project_dir: str,
        component_name: str,
        storage_name: str,
        current_generation: int,
    ) -> list[str]:
        """
        Delete old PVC manifest files when generation changes.

        This method finds and deletes PVC manifest files with generation numbers
        lower than the current generation. This ensures ArgoCD will prune the
        old PVCs when syncing. The current generation manifest is kept.

        Uses naming.py utilities to determine correct patterns:
        - generation 0: {component}-{storage}-pvc.yaml
        - generation N: {component}-{storage}-pvc-vN.yaml

        Args:
            project_dir: Directory containing the project manifests
            component_name: Name of the component
            storage_name: Name of the storage volume
            current_generation: Current generation number

        Returns:
            List of deleted manifest file paths
        """
        deleted_files = []

        # Use naming utilities to get base pattern
        manifest_type = generate_pvc_manifest_type(storage_name)
        base_name = generate_manifest_name(component_name, manifest_type, generation=0)

        # Pattern to match all PVC manifest files for this component and storage
        pattern = os.path.join(project_dir, f"{base_name}*.yaml")

        logger.info(
            f"Searching for old PVC manifests in {project_dir} "
            f"with pattern {base_name}*.yaml (current generation: {current_generation})"
        )

        # Find all matching manifest files
        matching_files = glob.glob(pattern)

        # Regex to extract generation number from filename
        # Matches: component-storage-pvc-v1.yaml -> 1
        # Also matches: component-storage-pvc.yaml -> None (generation 0)
        generation_regex = re.compile(rf"{re.escape(base_name)}-v(\d+)\.yaml$")
        no_generation_regex = re.compile(rf"{re.escape(base_name)}\.yaml$")

        for file_path in matching_files:
            filename = os.path.basename(file_path)

            # Check if file has generation suffix
            match = generation_regex.search(filename)
            if match:
                file_generation = int(match.group(1))
                if file_generation < current_generation:
                    logger.info(
                        f"Deleting old PVC manifest: {filename} (generation {file_generation} < {current_generation})"
                    )
                    os.remove(file_path)
                    deleted_files.append(file_path)
                else:
                    logger.debug(
                        f"Keeping PVC manifest: {filename} (generation {file_generation} >= {current_generation})"
                    )
            # Check if file has no generation suffix (generation 0)
            elif no_generation_regex.search(filename):
                if current_generation > 0:
                    logger.info(f"Deleting old PVC manifest: {filename} (generation 0 < {current_generation})")
                    os.remove(file_path)
                    deleted_files.append(file_path)
                else:
                    logger.debug(f"Keeping PVC manifest: {filename} (current generation is 0)")

        if deleted_files:
            logger.info(f"Deleted {len(deleted_files)} old PVC manifest(s) for {component_name}/{storage_name}")
        else:
            logger.debug(f"No old PVC manifests found for {component_name}/{storage_name}")

        return deleted_files

    def get_pvc_manifest_filename(
        self,
        component_name: str,
        storage_name: str,
        generation: int,
    ) -> str:
        """
        Get the PVC manifest filename for a given generation.

        Uses the naming utility to ensure consistency across the codebase.

        Args:
            component_name: Name of the component
            storage_name: Name of the storage volume
            generation: Current generation number

        Returns:
            Manifest filename (e.g., "webapp-data-pvc-v2.yaml" or "webapp-data-pvc.yaml")
        """
        manifest_type = generate_pvc_manifest_type(storage_name)
        return f"{generate_manifest_name(component_name, manifest_type, generation)}.yaml"

    async def create_pvc_manifests_for_component(
        self,
        project_data: dict,
        deployment: dict,
        component_name: str,
        unique_name: str,
        persistent_storage: list[dict],
        namespace: str,
        cluster: str,
        full_output_dir: str,
        manifest_generator: ManifestGenerator,
        force_clone_override: bool = False,
    ) -> list[str]:
        """
        Create PVC manifests for a component, handling generation and cleanup.

        This method:
        1. Reads generation for each storage from project data
        2. Deletes old generation manifest files if generation > 0
        3. Creates new PVC manifest with current generation
        4. Handles clone-from logic with mode/status checking (consistent with database/minio)

        Args:
            project_data: The parsed project data
            deployment: Deployment configuration
            component_name: Name of the component
            unique_name: Unique name for resources (from generate_unique_name)
            persistent_storage: List of persistent storage configs with names
            namespace: Kubernetes namespace
            cluster: Cluster name
            full_output_dir: Output directory for manifests
            manifest_generator: ManifestGenerator instance
            force_clone_override: Runtime override for force_clone (from API)

        Returns:
            List of created manifest filenames

        Raises:
            Exception: If manifest creation fails
        """
        from opi.core.cluster_config import get_storage_access_modes, get_storage_class_name
        from opi.utils.naming import generate_pvc_name, generate_unique_name

        created_files = []
        deployment_name = deployment["name"]

        # Get cluster storage configuration
        storage_class_name = get_storage_class_name(cluster)
        access_modes = get_storage_access_modes(cluster)

        pvc_template_path = os.path.join(os.path.dirname(__file__), "..", "..", "manifests", "pvc.yaml.jinja")

        for storage in persistent_storage:
            storage_name = storage["name"]

            # Get generation for this storage from project data (default 0 = normal name, no suffix)
            generation = (
                self.project_manager._project_file_handler.get_storage_generation(
                    project_data, deployment_name, component_name, storage_name
                )
                or 0
            )

            # Get backup setting for this storage (project-level or per-storage override)
            backup_enabled = self.project_manager._project_file_handler.get_storage_backup_enabled(
                project_data, component_name, storage_name
            )

            logger.info(
                f"Creating PVC manifest for {component_name}/{storage_name} "
                f"with generation {generation}, backup_enabled={backup_enabled}"
            )

            # DELETE OLD MANIFESTS if generation > 0
            if generation is not None and generation > 0:
                deleted_files = await self.delete_old_pvc_manifests(
                    project_dir=full_output_dir,
                    component_name=component_name,
                    storage_name=storage_name,
                    current_generation=generation,
                )
                if deleted_files:
                    logger.info(f"Deleted {len(deleted_files)} old PVC manifest(s) for {component_name}/{storage_name}")

            # Prepare PVC variables using centralized naming utility with generation
            pvc_variables = {
                "name": generate_pvc_name(unique_name, storage_name, generation),
                "namespace": namespace,
                "size": storage.get("size", "10Gi"),
                "storage_class_name": storage_class_name,
                "access_modes": access_modes,
                "backup_enabled": backup_enabled,
            }

            # Handle clone-from logic for PVC (consistent with database_manager and minio_manager)
            clone_from = deployment.get("clone-from")
            force_clone = force_clone_override or deployment.get("force-clone", False)
            should_skip_clone = False

            if clone_from:
                # clone-from must be a dict with type/reference/mode keys
                if not isinstance(clone_from, dict):
                    raise ValueError(
                        f"clone-from for deployment '{deployment_name}' must be a dict with 'type', 'reference', "
                        f"and 'mode' keys, got: {type(clone_from).__name__} = {clone_from!r}"
                    )

                clone_mode = clone_from.get("mode", "once")
                clone_status = clone_from.get("status", {})
                clone_completed = clone_status.get("completed", False) if isinstance(clone_status, dict) else False

                if clone_mode == "once" and clone_completed and not force_clone:
                    logger.info(
                        f"Skipping PVC clone for {deployment_name}/{component_name}/{storage_name}: "
                        f"mode is 'once' and already completed "
                        f"(timestamp: {clone_status.get('timestamp', 'unknown')}). "
                        "Proceeding with normal PVC creation."
                    )
                    should_skip_clone = True
                elif clone_mode == "once" and clone_completed and force_clone:
                    logger.info(
                        f"PVC clone mode 'once' for {deployment_name}/{component_name}/{storage_name} "
                        f"but force_clone=True, proceeding with clone"
                    )
                elif clone_mode == "always":
                    logger.info(f"PVC clone mode 'always' for {deployment_name}/{component_name}/{storage_name}")

            if clone_from and not should_skip_clone:
                clone_type = clone_from.get("type")
                source_deployment: str | None = None

                if clone_type == CloneFromType.DEPLOYMENT.value:
                    source_deployment = clone_from.get("reference")
                elif clone_type == CloneFromType.REMOTE_SOURCE.value:
                    # PVC cannot clone from remote source (Kubernetes dataSource only works locally)
                    logger.warning(
                        f"PVC clone-from type 'remote-source' is not supported for {deployment_name}/{component_name}/{storage_name}. "
                        "Kubernetes PVC dataSource only supports local cloning. Skipping PVC clone."
                    )
                elif clone_type == CloneFromType.BACKUP.value:
                    # Backup restore: skip PVC cloning, data will come from backup restore
                    logger.info(
                        f"Clone-from type 'backup' for PVC {deployment_name}/{component_name}/{storage_name}: "
                        "skipping live PVC clone, data will come from backup restore"
                    )
                    source_deployment = None
                else:
                    raise ValueError(
                        f"Unknown clone-from type '{clone_type}' for PVC in deployment '{deployment_name}'"
                    )

                if source_deployment:
                    # Look up the SOURCE deployment's PVC generation (not target's)
                    source_generation = self.project_manager._project_file_handler.get_storage_generation(
                        project_data, source_deployment, component_name, storage_name
                    )
                    logger.info(
                        f"Source deployment '{source_deployment}' has PVC generation: {source_generation} "
                        f"for {component_name}/{storage_name}"
                    )

                    # Generate source PVC name using source deployment's generation
                    source_unique_name = generate_unique_name(source_deployment, component_name)
                    source_pvc_name = generate_pvc_name(source_unique_name, storage_name, source_generation)
                    pvc_variables["source_pvc_name"] = source_pvc_name
                    logger.info(f"PVC {pvc_variables['name']} will be cloned from {source_pvc_name}")

                    # Record revision at component level (PVC tracking is per component, not deployment)
                    self.project_manager._revision_manager.record_component_clone(
                        project_data=project_data,
                        deployment_name=deployment_name,
                        component_name=component_name,
                        service_type="persistent-storage",
                        reference_name=storage_name,
                        generation=generation,
                        resource_name=pvc_variables["name"],
                        source=f"deployment:{source_deployment}",
                    )

                    # Report clone to project_manager for status tracking
                    self.project_manager.report_clone_performed(deployment_name, "persistent-storage", generation)

            # Create PVC manifest using centralized naming utility with generation
            manifest_type = generate_pvc_manifest_type(storage_name)
            pvc_manifest_name = generate_manifest_name(component_name, manifest_type, generation)

            # A previous removal of this storage may have left a
            # <base>.marked-for-deletion.yaml twin behind (kept so ArgoCD did not
            # prune the volume). Writing <base>.yaml next to it would put two files
            # with the same PVC identity in kustomization.yaml, which makes kustomize
            # refuse to render the whole deployment. Clear the twin first.
            self._remove_marked_twin(full_output_dir, f"{pvc_manifest_name}.yaml")

            pvc_manifest_path = manifest_generator.create_manifest_file(
                template_path=pvc_template_path,
                values=pvc_variables,
                output_dir=full_output_dir,
                output_filename=pvc_manifest_name,
                use_sops=False,
            )

            created_files.append(f"{pvc_manifest_name}.yaml")
            logger.info(f"Successfully created PVC manifest: {pvc_manifest_path}")

        # Reconcile the marked_for_deletion table against the manifests now on disk: any
        # pvc mark for this deployment whose file was removed above (or earlier) is stale.
        await self._prune_stale_pvc_marks(
            full_output_dir=full_output_dir,
            project_name=project_data.get("name", ""),
            deployment_name=deployment_name,
            cluster=cluster,
        )

        return created_files

    def _remove_marked_twin(self, full_output_dir: str, manifest_filename: str) -> bool:
        """Remove a leftover ``*.marked-for-deletion.yaml`` twin of a manifest being written.

        When a persistent-storage service is removed, ``handle_service_removal`` renames the
        PVC manifest to ``<base>.marked-for-deletion.yaml`` so ArgoCD keeps the volume alive.
        If the same storage is later re-added, ``create_pvc_manifests_for_component`` writes
        ``<base>.yaml`` again. Both files then carry the identical PVC resource identity, and
        kustomize refuses to render the whole deployment ("may not add resource with an
        already registered id"). Removing the marked twin here restores a single manifest.

        The live volume is not affected: ArgoCD re-adopts the existing PVC through the
        restored plain manifest, so nothing is pruned. This is a pure filesystem operation -
        the hard requirement that repairs the render; the stale ``marked_for_deletion`` row
        is reconciled separately by :meth:`_prune_stale_pvc_marks`, which keys off the file
        no longer existing.

        Returns:
            True if a marked twin was found and removed.
        """
        base = manifest_filename.removesuffix(".yaml")
        marked_path = os.path.join(full_output_dir, f"{base}{MARKED_FOR_DELETION_SUFFIX}")

        if not os.path.exists(marked_path):
            return False

        os.remove(marked_path)
        logger.info(
            "Removed stale marked-for-deletion PVC manifest %s%s because %s was recreated - "
            "deferred deletion is cancelled",
            base,
            MARKED_FOR_DELETION_SUFFIX,
            manifest_filename,
        )
        return True

    async def _prune_stale_pvc_marks(
        self,
        full_output_dir: str,
        project_name: str,
        deployment_name: str,
        cluster: str,
    ) -> None:
        """Drop ``pvc`` marks for this deployment whose marked file no longer exists.

        The marked-for-deletion *file* is the source of truth: a row only means something
        while its ``<name>.marked-for-deletion.yaml`` still sits in the deployment directory.
        Once the file is gone - because the resource came back (twin removed above) or was
        cleaned up by hand - the row is stale and is deleted here.

        Selection is on ``(project, deployment, cluster, type=pvc)`` and then filtered by
        file existence, so a sibling storage that is genuinely still marked (its file is
        still present) is left untouched. Best-effort: guarded on database availability, so
        a missing pool warns and continues. Removing the file already repaired the render;
        this is administration.
        """
        try:
            from opi.core.database_pools import get_database_pool
            from opi.services.marked_for_deletion_service import MarkedForDeletionService

            get_database_pool("main")  # raises if the DB is unavailable
        except KeyError, ValueError:
            logger.warning(
                "Database pool not available - leaving any stale pvc marks for %s/%s in place",
                project_name,
                deployment_name,
            )
            return

        service = MarkedForDeletionService()
        marks = await service.get_marks_for_deployment(project_name, deployment_name, cluster, "pvc")
        for mark in marks:
            marked_path = os.path.join(full_output_dir, mark["resource_name"])
            if os.path.exists(marked_path):
                continue
            await service.delete_mark(mark["id"])
            logger.info(
                "Deleted stale pvc mark %s (%s) - its manifest no longer exists",
                mark["id"],
                mark["resource_name"],
            )

    async def handle_service_removal(
        self,
        project_name: str,
        deployment_name: str,
        deployment_data: dict[str, Any],
        project_data: dict[str, Any],
        marked_for_deletion_service: MarkedForDeletionService | None = None,
    ) -> dict[str, Any]:
        """Handle cleanup when persistent-storage service is removed from a deployment.

        Instead of deleting PVC manifests (which would cause ArgoCD to prune
        the PVCs immediately), this method **renames** each PVC manifest file
        with a ``.marked-for-deletion.yaml`` suffix.  The renamed file stays
        in the kustomize directory so ArgoCD keeps the PVC alive.

        A record is inserted into the ``marked_for_deletion`` table with the
        exact filename so the reconciliation job can later remove the file,
        regenerate ``kustomization.yaml``, and let ArgoCD prune the PVC after
        the grace period.

        Args:
            project_name: Name of the project.
            deployment_name: Name of the deployment losing the service.
            deployment_data: The deployment dict from the *previous* YAML.
            project_data: The *previous* project YAML.
            marked_for_deletion_service: Optional service for deferred deletion.

        Returns:
            Structured result dict with operations, errors, success.
        """
        from opi.core.cluster_config import get_prefixed_namespace

        cluster = deployment_data.get("cluster", "")
        namespace = deployment_data.get("namespace", "")
        prefixed_namespace = get_prefixed_namespace(cluster, namespace) if cluster and namespace else namespace

        result: dict[str, Any] = {
            "service": "pvc",
            "deployment": deployment_name,
            "trigger": "service_removal",
            "operations": [],
            "success": True,
            "errors": [],
        }

        # Find components in this deployment that had persistent-storage
        file_handler = self.project_manager._project_file_handler
        component_refs = file_handler.extract_deployment_components(project_data, deployment_name)

        # Get the deployment git connector via the proper API
        repositories = project_data.get("repositories", [])
        repo_config = repositories[0] if repositories else {}
        repo_name = repo_config.get("name", "") if isinstance(repo_config, dict) else ""

        try:
            git_connector = await self.project_manager.get_git_connector_for_deployment(repo_name, repo_config)
        except Exception as e:
            error_msg = (
                f"Cannot access git connector for repository '{repo_name}' "
                f"- cannot rename PVC manifests for deferred deletion: {e}"
            )
            logger.error(error_msg)
            result["errors"].append(error_msg)
            result["success"] = False
            return result

        repo_path = await self.project_manager.get_repository_path(repo_name)
        if repo_path:
            deployment_path = f"{repo_path}/{cluster}/{project_name}/{deployment_name}"
        else:
            deployment_path = f"{cluster}/{project_name}/{deployment_name}"

        working_dir = await git_connector.get_working_dir()
        full_output_dir = os.path.join(working_dir, deployment_path)

        for component_ref in component_refs:
            ref_name = component_ref.get("reference") if isinstance(component_ref, dict) else component_ref
            if not ref_name:
                continue

            # Get persistent storage entries for this component
            storage_configs = file_handler.extract_component_storage(project_data, ref_name)
            persistent_storage = file_handler.get_persistent_storage(storage_configs)

            if not persistent_storage:
                continue

            for storage in persistent_storage:
                storage_name = storage["name"]

                # Get the generation to compute the correct manifest filename
                generation = (
                    file_handler.get_storage_generation(project_data, deployment_name, ref_name, storage_name) or 0
                )

                # Compute the manifest filename (same logic as create_pvc_manifests_for_component)
                manifest_filename = self.get_pvc_manifest_filename(ref_name, storage_name, generation)
                manifest_path = os.path.join(full_output_dir, manifest_filename)

                if not os.path.exists(manifest_path):
                    logger.warning(
                        "PVC manifest file not found at %s - may have been already removed",
                        manifest_path,
                    )
                    continue

                # Rename the file: webapp-data-pvc.yaml -> webapp-data-pvc.marked-for-deletion.yaml
                marked_filename = manifest_filename.replace(".yaml", MARKED_FOR_DELETION_SUFFIX)
                marked_path = os.path.join(full_output_dir, marked_filename)

                os.rename(manifest_path, marked_path)
                logger.info(
                    "Renamed PVC manifest for deferred deletion: %s -> %s",
                    manifest_filename,
                    marked_filename,
                )

                # Record in marked_for_deletion table
                if marked_for_deletion_service is not None:
                    await marked_for_deletion_service.mark_resource(
                        resource_type="pvc",
                        resource_name=marked_filename,
                        project_name=project_name,
                        deployment_name=deployment_name,
                        cluster=cluster,
                        metadata={
                            "deployment_path": deployment_path,
                            "namespace": prefixed_namespace,
                            "component": ref_name,
                            "storage_name": storage_name,
                            "original_filename": manifest_filename,
                        },
                    )
                    result["operations"].append(
                        {
                            "type": "mark_for_deletion",
                            "resource_type": "pvc",
                            "resource_name": marked_filename,
                            "original_filename": manifest_filename,
                            "status": "marked",
                        }
                    )
                    logger.info(
                        "Marked PVC for deferred deletion: %s (project=%s, deployment=%s, component=%s)",
                        marked_filename,
                        project_name,
                        deployment_name,
                        ref_name,
                    )
                else:
                    # No marked_for_deletion_service available - delete the manifest immediately
                    os.remove(marked_path)
                    result["operations"].append(
                        {
                            "type": "immediate_deletion",
                            "resource_type": "pvc",
                            "resource_name": manifest_filename,
                            "status": "deleted",
                        }
                    )
                    logger.warning(
                        "No marked_for_deletion_service available - deleted PVC manifest immediately: %s",
                        manifest_filename,
                    )

        return result
