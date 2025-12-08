"""PVC manager for handling PersistentVolumeClaim resources and lifecycle."""

import glob
import logging
import os
import re

from opi.utils.naming import generate_manifest_name, generate_pvc_manifest_type

logger = logging.getLogger(__name__)


class PVCManager:
    """Manager for PVC-related operations and resources."""

    def __init__(self, project_manager: "ProjectManager") -> None:
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
        manifest_generator: "ManifestGenerator",
    ) -> list[str]:
        """
        Create PVC manifests for a component, handling generation and cleanup.

        This method:
        1. Reads generation for each storage from project data
        2. Deletes old generation manifest files if generation > 0
        3. Creates new PVC manifest with current generation

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

            # Get generation for this storage from project data
            generation = self.project_manager._project_file_handler.get_storage_generation(
                project_data, deployment_name, component_name, storage_name
            )

            logger.info(f"Creating PVC manifest for {component_name}/{storage_name} with generation {generation}")

            # DELETE OLD MANIFESTS if generation > 0
            if generation > 0:
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
            }

            # Handle clone-from logic for PVC
            clone_from = deployment.get("clone-from")
            if clone_from:
                # Generate source PVC name using the same naming convention
                source_unique_name = generate_unique_name(clone_from, component_name)
                source_pvc_name = generate_pvc_name(source_unique_name, storage_name, generation)
                pvc_variables["source_pvc_name"] = source_pvc_name
                logger.info(f"PVC {pvc_variables['name']} will be cloned from {source_pvc_name}")

            # Create PVC manifest using centralized naming utility with generation
            manifest_type = generate_pvc_manifest_type(storage_name)
            pvc_manifest_name = generate_manifest_name(component_name, manifest_type, generation)

            pvc_manifest_path = manifest_generator.create_manifest_file(
                template_path=pvc_template_path,
                values=pvc_variables,
                output_dir=full_output_dir,
                output_filename=pvc_manifest_name,
                use_sops=False,
            )

            created_files.append(f"{pvc_manifest_name}.yaml")
            logger.info(f"Successfully created PVC manifest: {pvc_manifest_path}")

        return created_files
