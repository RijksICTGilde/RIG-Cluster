"""PVC Backup Manager - Orchestrates PVC backups to external S3 using Kopia."""

import asyncio
import json
import logging
import os
from typing import Any

from opi.core.config import settings
from opi.manager.backup.base import (
    BackupConfig,
    BackupResult,
    BaseBackupManager,
    RestoreResult,
    SnapshotInfo,
    kopia_backup_identity,
    utc_now,
)
from opi.utils.naming import (
    generate_backup_clone_pvc_name,
    generate_backup_pod_name,
    generate_backup_prefix,
    generate_backup_run_id,
    generate_backup_snapshot_name,
    generate_restore_pod_name,
    generate_restored_pvc_name,
)

logger = logging.getLogger(__name__)


class PVCBackupManager(BaseBackupManager):
    """
    Orchestrates PVC backups to external S3 using Kopia.

    Project-aware entry point: `backup_project_deployment(...)`.

    Flow per PVC:
    1. Acquire distributed lock
    2. Create VolumeSnapshot
    3. Create temp PVC from snapshot
    4. Derive backup key from namespace's SOPS age key
    5. Spawn backup Pod (kopia with stable per-resource source identity)
    6. Wait for completion, then cleanup resources
    7. Release lock
    """

    async def _backup_pvc(
        self,
        namespace: str,
        pvc_name: str,
        backup_run_id: str,
        cluster: str | None = None,
        project_name: str | None = None,
        deployment_name: str | None = None,
        component_name: str | None = None,
        storage_name: str | None = None,
        pvc_generation: int | None = None,
        trigger: str = "manual",
    ) -> BackupResult:
        """
        Internal: backup a single PVC (lock must be held).

        Args:
            namespace: Namespace containing the PVC
            pvc_name: Name of the PVC to backup
            backup_run_id: ID to group all PVCs in the same backup run (required)
            cluster: Cluster name for backup prefix (defaults to settings.CLUSTER)
            project_name: Project name for metadata
            deployment_name: Deployment name for metadata
            component_name: Component name for metadata
            storage_name: Storage name for metadata
            pvc_generation: PVC generation number for metadata
            backup_run_id: ID to group all PVCs in the same backup run (YYYYMMDDHHmmss)

        Returns:
            BackupResult with operation details
        """
        start_time = utc_now()
        timestamp = start_time.strftime("%Y%m%d-%H%M%S")

        # Use naming utilities for consistent naming
        snapshot_name = generate_backup_snapshot_name(pvc_name, timestamp)
        clone_pvc_name = generate_backup_clone_pvc_name(pvc_name, timestamp)
        pod_name = generate_backup_pod_name(pvc_name[:20], timestamp)  # Truncate PVC name if too long

        # Determine cluster for backup prefix
        backup_cluster = cluster or settings.CLUSTER_MANAGER

        logger.info(f"Starting backup of {namespace}/{pvc_name}")

        # A PVC backup runs through a VolumeSnapshot, so without a snapshot class there
        # is nothing to do. Say so here: rendering an empty volumeSnapshotClassName
        # produces a snapshot that never becomes ready, and the run would spend the full
        # _wait_for_snapshot timeout before failing with nothing to point at.
        if not self.config.snapshot_class:
            return BackupResult(
                namespace=namespace,
                pvc_name=pvc_name,
                success=False,
                error=(
                    f"No VolumeSnapshotClass configured for cluster '{backup_cluster}'. "
                    "PVC backups need one; set storage.volume_snapshot_class in cluster_config. "
                    "Database and bucket backups are unaffected."
                ),
                duration_seconds=(utc_now() - start_time).total_seconds(),
            )

        try:
            # 1. Get PVC details (size, storage class)
            pvc_info = await self._get_pvc_info(namespace, pvc_name)
            if not pvc_info:
                return BackupResult(
                    namespace=namespace,
                    pvc_name=pvc_name,
                    success=False,
                    error=f"PVC {pvc_name} not found in namespace {namespace}",
                    duration_seconds=(utc_now() - start_time).total_seconds(),
                )

            # 2. Create VolumeSnapshot
            logger.info(f"Creating VolumeSnapshot {snapshot_name}")
            await self._create_snapshot(namespace, pvc_name, snapshot_name)
            await self._wait_for_snapshot(namespace, snapshot_name)

            # 3. Create clone PVC from snapshot
            logger.info(f"Creating clone PVC {clone_pvc_name}")
            await self._create_clone_pvc(
                namespace=namespace,
                snapshot_name=snapshot_name,
                clone_pvc_name=clone_pvc_name,
                source_pvc_name=pvc_name,
                size=pvc_info["size"],
                storage_class=pvc_info["storage_class"],
                timestamp=timestamp,
            )
            await self._wait_for_pvc(namespace, clone_pvc_name)

            # 4. Derive backup encryption key from namespace's SOPS key
            kopia_password = await self._derive_backup_key(namespace)

            # 5. Spawn backup pod
            logger.info(f"Creating backup pod {pod_name}")
            backup_prefix = generate_backup_prefix(backup_cluster, namespace)
            await self._create_backup_pod(
                namespace=namespace,
                pvc_name=pvc_name,
                clone_pvc_name=clone_pvc_name,
                pod_name=pod_name,
                kopia_password=kopia_password,
                timestamp=timestamp,
                backup_prefix=backup_prefix,
                cluster=backup_cluster,
                project_name=project_name,
                deployment_name=deployment_name,
                component_name=component_name,
                storage_name=storage_name,
                pvc_generation=pvc_generation,
                backup_run_id=backup_run_id,
                trigger=trigger,
            )

            # 6. Wait for pod completion
            success = await self._wait_for_pod(namespace, pod_name)

            if not success:
                # Get pod logs for debugging
                logs = await self._get_pod_logs(namespace, pod_name)
                logger.error(f"Backup pod {pod_name} failed. Full logs:\n{logs}")
                return BackupResult(
                    namespace=namespace,
                    pvc_name=pvc_name,
                    success=False,
                    error=f"Backup pod failed. Logs: {logs[-500:] if logs else 'no logs'}",
                    duration_seconds=(utc_now() - start_time).total_seconds(),
                )

            duration = (utc_now() - start_time).total_seconds()
            logger.info(f"Backup of {namespace}/{pvc_name} completed successfully in {duration:.1f}s")

            return BackupResult(
                namespace=namespace,
                pvc_name=pvc_name,
                success=True,
                snapshot_name=snapshot_name,
                duration_seconds=duration,
            )

        except Exception as e:
            duration = (utc_now() - start_time).total_seconds()
            logger.exception("Backup of %s/%s failed after %.1fs", namespace, pvc_name, duration)
            return BackupResult(
                namespace=namespace,
                pvc_name=pvc_name,
                success=False,
                error=str(e),
                duration_seconds=duration,
            )

        finally:
            # 7. Cleanup (best effort)
            await self._cleanup(namespace, pod_name, clone_pvc_name, snapshot_name)

    async def backup_project_deployment(
        self,
        project_name: str,
        project_data: dict[str, Any],
        deployment_name: str,
        namespace: str,
        cluster: str,
        backup_run_id: str | None = None,
        trigger: str = "manual",
    ) -> list[BackupResult]:
        """
        Backup PVCs for a project deployment with full metadata context.

        This method extracts persistent storage definitions from the project file
        to determine which PVCs to backup. Unlike namespace-level backup endpoints
        (which rely on the backup.rig.nl/enabled label), this method uses the
        project file as the source of truth -- consistent with how database and
        MinIO backups discover their targets.

        Args:
            project_name: Name of the project
            project_data: Project configuration data
            deployment_name: Name of the deployment
            namespace: Kubernetes namespace (with cluster prefix)
            cluster: Cluster name
            backup_run_id: Optional backup run ID (generated if not provided)

        Returns:
            List of BackupResult for each PVC
        """
        from opi.handlers.project_file_handler import create_project_file_handler
        from opi.utils.naming import generate_pvc_name, generate_unique_name

        # Ensure backup bucket exists before starting
        await self._ensure_backup_bucket_exists(project_name=project_name, cluster=cluster)

        async with self.lock:
            results: list[BackupResult] = []
            project_file_handler = create_project_file_handler()

            # Use provided backup_run_id or generate one for this entire backup run
            if not backup_run_id:
                backup_run_id = generate_backup_run_id()
            logger.info(f"Starting backup run {backup_run_id} for project {project_name}/{deployment_name}")

            # Extract component references from deployment
            deployment_components = project_file_handler.extract_deployment_components(project_data, deployment_name)

            # Get base components for storage definitions
            base_components = {c.get("name"): c for c in project_data.get("components", [])}

            # Build a map of PVC name -> context from the project file.
            # This is the source of truth for which PVCs belong to this deployment.
            pvc_context_map: dict[str, dict[str, str | int | None]] = {}
            for dep_component in deployment_components:
                # Deployment components use "reference" to point to base component
                component_name = dep_component.get("reference", "")
                if not component_name:
                    continue

                # Get storage definitions from the BASE component
                from opi.handlers.project_file_handler import extract_storage_from_component_services

                base_component = base_components.get(component_name, {})
                storages = extract_storage_from_component_services(base_component)

                for idx, storage in enumerate(storages):
                    # Skip non-persistent storage (ephemeral doesn't have PVCs)
                    if storage.get("type") != "persistent":
                        continue

                    # Storage name comes from the "name" field, or generate from mount-path
                    storage_name = storage.get("name")
                    if not storage_name:
                        from opi.utils.naming import generate_storage_name

                        mount_path = storage.get("mount-path", "") or storage.get("mount_path", "")
                        storage_name = generate_storage_name(mount_path, idx)

                    unique_name = generate_unique_name(deployment_name, component_name)

                    # Get generation from deployment component's services.persistent-storage
                    generation = project_file_handler.get_storage_generation(
                        project_data, deployment_name, component_name, storage_name
                    )

                    # Generate the expected PVC name
                    expected_pvc_name = generate_pvc_name(unique_name, storage_name, generation)

                    logger.info(
                        f"PVC mapping: {expected_pvc_name} -> component={component_name}, "
                        f"storage={storage_name}, generation={generation}"
                    )

                    pvc_context_map[expected_pvc_name] = {
                        "component_name": component_name,
                        "storage_name": storage_name,
                        "generation": generation,
                    }

            if not pvc_context_map:
                logger.warning(
                    f"No persistent storage found in project file for deployment "
                    f"{project_name}/{deployment_name}. No PVCs to backup."
                )
                return results

            logger.info(
                f"Found {len(pvc_context_map)} PVC(s) to backup from project file "
                f"for {project_name}/{deployment_name}: {list(pvc_context_map.keys())}"
            )

            # Backup each PVC identified from the project file
            for pvc_name, context in pvc_context_map.items():
                await self.lock.update_progress(namespace, pvc_name)

                ctx_component = context.get("component_name")
                ctx_storage = context.get("storage_name")
                ctx_gen = context.get("generation")

                result = await self._backup_pvc(
                    namespace=namespace,
                    pvc_name=pvc_name,
                    cluster=cluster,
                    project_name=project_name,
                    deployment_name=deployment_name,
                    component_name=str(ctx_component) if ctx_component else None,
                    storage_name=str(ctx_storage) if ctx_storage else None,
                    pvc_generation=int(ctx_gen) if ctx_gen is not None else None,
                    backup_run_id=backup_run_id,
                    trigger=trigger,
                )
                results.append(result)

            return results

    async def _create_snapshot(self, namespace: str, pvc_name: str, snapshot_name: str) -> None:
        """Create a VolumeSnapshot from a PVC."""
        template_path = os.path.join(self.MANIFESTS_DIR, "backup-snapshot.yaml.jinja")

        with open(template_path) as f:
            template_content = f.read()

        manifest = self._template_manifest(
            template_content,
            {
                "snapshot_name": snapshot_name,
                "namespace": namespace,
                "pvc_name": pvc_name,
                "snapshot_class": self.config.snapshot_class,
                "timestamp": utc_now().strftime("%Y%m%d-%H%M%S"),
            },
        )

        args = ["apply", "-f", "-"]
        _, stderr, code = await self.kubectl.run_command(args, stdin_input=manifest)

        if code != 0:
            raise RuntimeError(f"Failed to create snapshot: {stderr}")

    async def _wait_for_snapshot(self, namespace: str, snapshot_name: str, timeout: int = 300) -> None:
        """Wait for VolumeSnapshot to be ready."""
        logger.info(f"Waiting for snapshot {snapshot_name} to be ready...")

        start_time = asyncio.get_event_loop().time()

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                raise RuntimeError(f"Timeout waiting for snapshot {snapshot_name} after {timeout}s")

            args = [
                "get",
                "volumesnapshot",
                snapshot_name,
                "-n",
                namespace,
                "-o",
                "jsonpath={.status.readyToUse}",
            ]
            stdout, _, code = await self.kubectl.run_command(args)

            if code == 0 and stdout.strip().lower() == "true":
                logger.info(f"Snapshot {snapshot_name} is ready")
                return

            await asyncio.sleep(5)

    async def _create_clone_pvc(
        self,
        namespace: str,
        snapshot_name: str,
        clone_pvc_name: str,
        source_pvc_name: str,
        size: str,
        storage_class: str,
        timestamp: str,
    ) -> None:
        """Create a PVC clone from a VolumeSnapshot."""
        template_path = os.path.join(self.MANIFESTS_DIR, "backup-clone-pvc.yaml.jinja")

        with open(template_path) as f:
            template_content = f.read()

        manifest = self._template_manifest(
            template_content,
            {
                "clone_pvc_name": clone_pvc_name,
                "namespace": namespace,
                "source_pvc_name": source_pvc_name,
                "snapshot_name": snapshot_name,
                "size": size,
                "storage_class_name": storage_class,
                "timestamp": timestamp,
            },
        )

        args = ["apply", "-f", "-"]
        _, stderr, code = await self.kubectl.run_command(args, stdin_input=manifest)

        if code != 0:
            raise RuntimeError(f"Failed to create clone PVC: {stderr}")

    async def _create_backup_pod(
        self,
        namespace: str,
        pvc_name: str,
        clone_pvc_name: str,
        pod_name: str,
        kopia_password: str,
        timestamp: str,
        backup_prefix: str,
        backup_run_id: str,
        cluster: str | None = None,
        project_name: str | None = None,
        deployment_name: str | None = None,
        component_name: str | None = None,
        storage_name: str | None = None,
        pvc_generation: int | None = None,
        trigger: str = "manual",
    ) -> None:
        """Create the backup pod.

        Args:
            namespace: Target namespace
            pvc_name: Original PVC name being backed up
            clone_pvc_name: Cloned PVC name to read from
            pod_name: Name for the backup pod
            kopia_password: Encryption password for Kopia
            timestamp: Backup timestamp string
            backup_prefix: S3 prefix for the backup (cluster/namespace)
            backup_run_id: Backup run ID (required)
            cluster: Cluster name for metadata (optional)
            project_name: Project name for metadata (optional)
            deployment_name: Deployment name for metadata (optional)
            component_name: Component name for metadata (optional)
            storage_name: Storage name for metadata (optional)
            pvc_generation: PVC generation number for metadata (optional)
        """
        template_path = os.path.join(self.MANIFESTS_DIR, "backup-pod.yaml.jinja")

        with open(template_path) as f:
            template_content = f.read()

        # Get the bucket name (may be per-project based on config)
        effective_cluster = cluster or settings.CLUSTER_MANAGER
        bucket_name = self.config.get_bucket_name(project_name, effective_cluster)

        # Stable per-resource Kopia source identity. Storage name is the
        # logical (generation-stable) identifier; falls back to pvc name for
        # callers that don't have project context (e.g. namespace-mode backups).
        kopia_hostname, kopia_source = kopia_backup_identity(
            project_name=project_name,
            deployment_name=deployment_name,
            resource_kind="pvc",
            resource_name=storage_name or pvc_name,
            trigger=trigger,
        )

        manifest = self._template_manifest(
            template_content,
            {
                "pod_name": pod_name,
                "namespace": namespace,
                "pvc_name": pvc_name,
                "clone_pvc_name": clone_pvc_name,
                "timestamp": timestamp,
                "s3_endpoint": self.config.s3_endpoint,
                "s3_bucket": bucket_name,
                "s3_access_key": self.config.s3_access_key,
                "s3_secret_key": self.config.s3_secret_key,
                "s3_disable_tls": not self.config.s3_use_tls,
                "backup_prefix": backup_prefix,
                "kopia_password": kopia_password,
                "kopia_hostname": kopia_hostname,
                "kopia_source": kopia_source,
                "timeout_seconds": self.config.timeout_seconds,
                "retention_keep_latest": self.config.retention_keep_latest,
                "retention_keep_daily": self.config.retention_keep_daily,
                "retention_keep_weekly": self.config.retention_keep_weekly,
                "retention_keep_monthly": self.config.retention_keep_monthly,
                # Project context metadata
                "cluster": effective_cluster,
                "project_name": project_name,
                "deployment_name": deployment_name,
                "component_name": component_name,
                "storage_name": storage_name,
                "pvc_generation": pvc_generation if pvc_generation is not None else 0,
                "backup_run_id": backup_run_id,
                "trigger": trigger,
            },
        )

        args = ["apply", "-f", "-"]
        _, stderr, code = await self.kubectl.run_command(args, stdin_input=manifest)

        if code != 0:
            raise RuntimeError(f"Failed to create backup pod: {stderr}")

    # =========================================================================
    # Restore Methods
    # =========================================================================

    async def list_snapshots(
        self,
        cluster: str,
        namespace: str,
        pvc_name: str | None = None,
        project_name: str | None = None,
    ) -> list[SnapshotInfo]:
        """
        List available Kopia snapshots for a namespace/PVC.

        Uses the Kopia CLI directly to query the repository, which is much faster
        than spawning a Kubernetes pod.

        Args:
            cluster: Cluster name
            namespace: Namespace name
            pvc_name: Optional PVC name to filter by
            project_name: Optional project name for per-project bucket mode

        Returns:
            List of available snapshots
        """
        from opi.connectors.kopia import KopiaConnector, KopiaRepositoryConfig

        backup_prefix = generate_backup_prefix(cluster, namespace)
        kopia_password = await self._derive_backup_key(namespace)
        bucket_name = self.config.get_bucket_name(project_name, cluster)

        # Create Kopia connector and query repository directly
        kopia = KopiaConnector()

        if not KopiaConnector.is_kopia_available:
            logger.warning("Kopia CLI not available, cannot list snapshots")
            return []

        repo_config = KopiaRepositoryConfig(
            s3_endpoint=self.config.s3_endpoint,
            s3_bucket=bucket_name,
            s3_access_key=self.config.s3_access_key,
            s3_secret_key=self.config.s3_secret_key,
            s3_prefix=backup_prefix,
            password=kopia_password,
            use_tls=self.config.s3_use_tls,
        )

        try:
            kopia_snapshots = await kopia.list_snapshots(repo_config)

            # Convert to SnapshotInfo format and optionally filter by PVC name
            snapshots: list[SnapshotInfo] = []
            for ks in kopia_snapshots:
                # Both read endpoints (backup runs and the snapshot listing) end up here,
                # so this is the one place that decides which name a caller gets to see.
                # It has to be the name the restore route accepts -- see restore_reference.
                snapshot_pvc_name = ks.restore_reference

                # Apply filter if specified
                if pvc_name and snapshot_pvc_name != pvc_name:
                    continue

                snapshots.append(
                    SnapshotInfo(
                        snapshot_id=ks.snapshot_id,
                        pvc_name=snapshot_pvc_name,
                        timestamp=ks.timestamp,
                        size_bytes=ks.size_bytes,
                        # Extended metadata from Kopia tags
                        cluster=ks.cluster,
                        namespace=ks.namespace,
                        project_name=ks.project_name,
                        deployment_name=ks.deployment_name,
                        component_name=ks.component_name,
                        storage_name=ks.storage_name,
                        generation=ks.generation,
                        backup_run_id=ks.backup_run_id,
                        resource_type=ks.resource_type,
                        trigger=ks.trigger,
                        source_user=ks.source_user,
                        source_host=ks.source_host,
                        # Raw tags for debugging
                        tags=ks.tags,
                    )
                )

            logger.debug(f"Found {len(snapshots)} snapshots for {namespace}")
            return snapshots

        except Exception as e:
            logger.warning(f"Failed to list snapshots via Kopia CLI: {e}")
            return []

    async def delete_snapshots(
        self,
        cluster: str,
        namespace: str,
        snapshot_ids: list[str],
        project_name: str | None = None,
    ) -> dict[str, bool]:
        """
        Delete Kopia snapshots by ID for a namespace.

        Connects to the namespace's repository once and deletes all given
        snapshots over that connection.

        Args:
            cluster: Cluster name
            namespace: Namespace name
            snapshot_ids: IDs of the snapshots to delete
            project_name: Optional project name for per-project bucket mode

        Returns:
            Mapping of snapshot_id -> deletion success
        """
        from opi.connectors.kopia import KopiaConnector, KopiaRepositoryConfig

        if not snapshot_ids:
            return {}

        backup_prefix = generate_backup_prefix(cluster, namespace)
        kopia_password = await self._derive_backup_key(namespace)
        bucket_name = self.config.get_bucket_name(project_name, cluster)

        kopia = KopiaConnector()

        if not KopiaConnector.is_kopia_available:
            logger.warning("Kopia CLI not available, cannot delete snapshots")
            return dict.fromkeys(snapshot_ids, False)

        repo_config = KopiaRepositoryConfig(
            s3_endpoint=self.config.s3_endpoint,
            s3_bucket=bucket_name,
            s3_access_key=self.config.s3_access_key,
            s3_secret_key=self.config.s3_secret_key,
            s3_prefix=backup_prefix,
            password=kopia_password,
            use_tls=self.config.s3_use_tls,
        )

        return await kopia.delete_snapshots(repo_config, snapshot_ids)

    def _parse_snapshot_list(self, logs: str, pvc_filter: str | None = None) -> list[SnapshotInfo]:
        """Parse Kopia snapshot list output from pod logs."""
        snapshots: list[SnapshotInfo] = []
        try:
            # Look for JSON output in logs
            for line in logs.split("\n"):
                if line.strip().startswith("{") and "snapshot_id" in line:
                    data = json.loads(line)
                    pvc_name = data.get("pvc_name", "")

                    # Apply filter if specified
                    if pvc_filter and pvc_name != pvc_filter:
                        continue

                    snapshots.append(
                        SnapshotInfo(
                            snapshot_id=data.get("snapshot_id", ""),
                            pvc_name=pvc_name,
                            timestamp=data.get("timestamp", ""),
                            size_bytes=data.get("size_bytes"),
                        )
                    )
        except Exception as e:
            logger.warning(f"Error parsing snapshot list: {e}")

        return snapshots

    async def restore_pvc(
        self,
        cluster: str,
        namespace: str,
        pvc_name: str,
        snapshot_id: str | None = None,
        target_pvc_name: str | None = None,
        storage_size: str = "10Gi",
        storage_class: str | None = None,
        overwrite: bool = False,
    ) -> RestoreResult:
        """
        Restore a PVC from a Kopia backup.

        Args:
            cluster: Cluster name where backup was made
            namespace: Namespace for the restore
            pvc_name: Original PVC name (used to find backup)
            snapshot_id: Optional specific snapshot ID (defaults to latest)
            target_pvc_name: Name for restored PVC (defaults to {pvc_name}-restored-{timestamp})
            storage_size: Size for new PVC (required if creating new)
            storage_class: Storage class for new PVC (optional)
            overwrite: If True, allows restoring to existing PVC

        Returns:
            RestoreResult with operation details
        """
        async with self.lock:
            await self.lock.update_progress(namespace, f"restore:{pvc_name}")
            return await self._restore_pvc(
                cluster=cluster,
                namespace=namespace,
                pvc_name=pvc_name,
                snapshot_id=snapshot_id,
                target_pvc_name=target_pvc_name,
                storage_size=storage_size,
                storage_class=storage_class,
                overwrite=overwrite,
            )

    async def _restore_pvc(
        self,
        cluster: str,
        namespace: str,
        pvc_name: str,
        snapshot_id: str | None = None,
        target_pvc_name: str | None = None,
        storage_size: str = "10Gi",
        storage_class: str | None = None,
        overwrite: bool = False,
    ) -> RestoreResult:
        """
        Internal: restore a single PVC (lock must be held).
        """
        start_time = utc_now()
        timestamp = start_time.strftime("%Y%m%d-%H%M%S")

        # Generate target PVC name if not provided
        if not target_pvc_name:
            target_pvc_name = generate_restored_pvc_name(pvc_name, timestamp)

        pod_name = generate_restore_pod_name(pvc_name[:20], timestamp)
        backup_prefix = generate_backup_prefix(cluster, namespace)

        logger.info(f"Starting restore of {pvc_name} to {target_pvc_name} in {namespace}")

        try:
            # 1. Check if target PVC exists
            existing_pvc = await self._get_pvc_info(namespace, target_pvc_name)
            if existing_pvc and not overwrite:
                return RestoreResult(
                    namespace=namespace,
                    pvc_name=pvc_name,
                    success=False,
                    target_pvc_name=target_pvc_name,
                    error=f"Target PVC {target_pvc_name} exists. Set overwrite=true to replace contents.",
                    duration_seconds=(utc_now() - start_time).total_seconds(),
                )

            # 2. Create target PVC if it doesn't exist
            if not existing_pvc:
                logger.info(f"Creating target PVC {target_pvc_name}")
                await self._create_restore_pvc(
                    namespace=namespace,
                    pvc_name=target_pvc_name,
                    size=storage_size,
                    storage_class=storage_class,
                )
                await self._wait_for_pvc(namespace, target_pvc_name)

            # 3. Derive restore key (same as backup key)
            kopia_password = await self._derive_backup_key(namespace)

            # 4. Spawn restore pod
            logger.info(f"Creating restore pod {pod_name}")
            await self._create_restore_pod(
                namespace=namespace,
                pvc_name=pvc_name,
                target_pvc_name=target_pvc_name,
                pod_name=pod_name,
                kopia_password=kopia_password,
                backup_prefix=backup_prefix,
                snapshot_id=snapshot_id,
            )

            # 5. Wait for pod completion
            success = await self._wait_for_pod(namespace, pod_name)

            if not success:
                logs = await self._get_pod_logs(namespace, pod_name)
                logger.error(f"Restore pod {pod_name} failed. Full logs:\n{logs}")
                return RestoreResult(
                    namespace=namespace,
                    pvc_name=pvc_name,
                    success=False,
                    target_pvc_name=target_pvc_name,
                    error=f"Restore pod failed. Logs: {logs[-500:] if logs else 'no logs'}",
                    duration_seconds=(utc_now() - start_time).total_seconds(),
                )

            duration = (utc_now() - start_time).total_seconds()
            logger.info(f"Restore of {pvc_name} to {target_pvc_name} completed in {duration:.1f}s")

            return RestoreResult(
                namespace=namespace,
                pvc_name=pvc_name,
                success=True,
                target_pvc_name=target_pvc_name,
                snapshot_id=snapshot_id,
                duration_seconds=duration,
            )

        except Exception as e:
            duration = (utc_now() - start_time).total_seconds()
            logger.exception("Restore of %s failed after %.1fs", pvc_name, duration)
            return RestoreResult(
                namespace=namespace,
                pvc_name=pvc_name,
                success=False,
                target_pvc_name=target_pvc_name,
                error=str(e),
                duration_seconds=duration,
            )

        finally:
            # Cleanup restore pod (best effort)
            await self._cleanup_pod(namespace, pod_name)

    async def _create_restore_pvc(
        self,
        namespace: str,
        pvc_name: str,
        size: str,
        storage_class: str | None = None,
    ) -> None:
        """Create a PVC for restore."""
        template_path = os.path.join(self.MANIFESTS_DIR, "restore-pvc.yaml.jinja")

        # Check if template exists, if not use inline YAML
        if os.path.exists(template_path):
            with open(template_path) as f:
                template_content = f.read()
        else:
            template_content = """apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {{ pvc_name }}
  namespace: {{ namespace }}
spec:
  accessModes:
    - ReadWriteOnce
{% if storage_class_name %}
  storageClassName: {{ storage_class_name }}
{% endif %}
  resources:
    requests:
      storage: {{ size }}
"""

        manifest = self._template_manifest(
            template_content,
            {
                "pvc_name": pvc_name,
                "namespace": namespace,
                "size": size,
                "storage_class_name": storage_class or "",
            },
        )

        args = ["apply", "-f", "-"]
        _, stderr, code = await self.kubectl.run_command(args, stdin_input=manifest)

        if code != 0:
            raise RuntimeError(f"Failed to create restore PVC: {stderr}")

    async def restore_to_project_pvc(
        self,
        cluster: str,
        namespace: str,
        source_pvc_name: str,
        target_pvc_name: str,
        storage_size: str,
        storage_class: str | None = None,
        access_modes: list[str] | None = None,
        snapshot_id: str | None = None,
        backup_enabled: bool = True,
        project_name: str | None = None,
    ) -> RestoreResult:
        """
        Restore a backup to a specific PVC name (for project-based restore).

        This method is used when restoring to a PVC that will be managed by ArgoCD.
        It creates a PVC with the exact name specified (matching the next generation),
        restores data to it, and returns success. The caller is responsible for
        updating the project file and triggering the refresh.

        Args:
            cluster: Cluster name where backup was made
            namespace: Namespace for the restore
            source_pvc_name: Original PVC name (used to find the backup in Kopia)
            target_pvc_name: Exact name for the new PVC (e.g., webapp-data-pvc-v3)
            storage_size: Size for the new PVC
            storage_class: Storage class for the new PVC
            access_modes: Access modes for the PVC (default: ["ReadWriteOnce"])
            snapshot_id: Optional specific snapshot ID (defaults to latest)
            backup_enabled: Whether to enable backup label on restored PVC
            project_name: Project name for per-project bucket mode

        Returns:
            RestoreResult with operation details
        """
        async with self.lock:
            await self.lock.update_progress(namespace, f"project-restore:{source_pvc_name}")
            return await self._restore_to_project_pvc(
                cluster=cluster,
                namespace=namespace,
                source_pvc_name=source_pvc_name,
                target_pvc_name=target_pvc_name,
                storage_size=storage_size,
                storage_class=storage_class,
                access_modes=access_modes or ["ReadWriteOnce"],
                snapshot_id=snapshot_id,
                backup_enabled=backup_enabled,
                project_name=project_name,
            )

    async def _restore_to_project_pvc(
        self,
        cluster: str,
        namespace: str,
        source_pvc_name: str,
        target_pvc_name: str,
        storage_size: str,
        storage_class: str | None,
        access_modes: list[str],
        snapshot_id: str | None,
        backup_enabled: bool,
        project_name: str | None = None,
    ) -> RestoreResult:
        """
        Internal: restore to a project-managed PVC (lock must be held).
        """
        start_time = utc_now()
        timestamp = start_time.strftime("%Y%m%d-%H%M%S")
        pod_name = generate_restore_pod_name(source_pvc_name[:20], timestamp)
        backup_prefix = generate_backup_prefix(cluster, namespace)

        logger.info(f"Starting project restore: {source_pvc_name} -> {target_pvc_name} in {namespace}")

        try:
            # 1. Check if target PVC already exists (should not for project restore)
            existing_pvc = await self._get_pvc_info(namespace, target_pvc_name)
            if existing_pvc:
                return RestoreResult(
                    namespace=namespace,
                    pvc_name=source_pvc_name,
                    success=False,
                    target_pvc_name=target_pvc_name,
                    error=f"Target PVC {target_pvc_name} already exists. Cannot restore.",
                    duration_seconds=(utc_now() - start_time).total_seconds(),
                )

            # 2. Create target PVC with project-compatible template
            logger.info(f"Creating target PVC {target_pvc_name} for project restore")
            await self._create_project_restore_pvc(
                namespace=namespace,
                pvc_name=target_pvc_name,
                source_pvc_name=source_pvc_name,
                size=storage_size,
                storage_class=storage_class,
                access_modes=access_modes,
                timestamp=timestamp,
                snapshot_id=snapshot_id,
                backup_enabled=backup_enabled,
            )
            await self._wait_for_pvc(namespace, target_pvc_name)

            # 3. Derive restore key (same as backup key)
            kopia_password = await self._derive_backup_key(namespace)

            # 4. Spawn restore pod
            logger.info(f"Creating restore pod {pod_name}")
            await self._create_restore_pod(
                namespace=namespace,
                pvc_name=source_pvc_name,
                target_pvc_name=target_pvc_name,
                pod_name=pod_name,
                kopia_password=kopia_password,
                backup_prefix=backup_prefix,
                snapshot_id=snapshot_id,
                project_name=project_name,
                cluster=cluster,
            )

            # 5. Wait for pod completion
            success = await self._wait_for_pod(namespace, pod_name)

            if not success:
                logs = await self._get_pod_logs(namespace, pod_name)
                logger.error(f"Restore pod {pod_name} failed. Full logs:\n{logs}")
                # Cleanup the PVC we created since restore failed
                try:
                    args = ["delete", "pvc", target_pvc_name, "-n", namespace, "--ignore-not-found=true"]
                    await self.kubectl.run_command(args)
                except Exception as cleanup_err:
                    logger.warning(f"Failed to cleanup PVC after restore failure: {cleanup_err}")

                return RestoreResult(
                    namespace=namespace,
                    pvc_name=source_pvc_name,
                    success=False,
                    target_pvc_name=target_pvc_name,
                    error=f"Restore pod failed. Logs: {logs[-500:] if logs else 'no logs'}",
                    duration_seconds=(utc_now() - start_time).total_seconds(),
                )

            duration = (utc_now() - start_time).total_seconds()
            logger.info(f"Project restore of {source_pvc_name} to {target_pvc_name} completed in {duration:.1f}s")

            return RestoreResult(
                namespace=namespace,
                pvc_name=source_pvc_name,
                success=True,
                target_pvc_name=target_pvc_name,
                snapshot_id=snapshot_id,
                duration_seconds=duration,
            )

        except Exception as e:
            duration = (utc_now() - start_time).total_seconds()
            logger.exception("Project restore of %s failed after %.1fs", source_pvc_name, duration)
            return RestoreResult(
                namespace=namespace,
                pvc_name=source_pvc_name,
                success=False,
                target_pvc_name=target_pvc_name,
                error=str(e),
                duration_seconds=duration,
            )

        finally:
            # Cleanup restore pod (best effort)
            await self._cleanup_pod(namespace, pod_name)

    async def _create_project_restore_pvc(
        self,
        namespace: str,
        pvc_name: str,
        source_pvc_name: str,
        size: str,
        storage_class: str | None,
        access_modes: list[str],
        timestamp: str,
        snapshot_id: str | None,
        backup_enabled: bool,
    ) -> None:
        """Create a PVC for project-based restore with proper labels and annotations."""
        template_path = os.path.join(self.MANIFESTS_DIR, "restore-target-pvc.yaml.jinja")

        with open(template_path) as f:
            template_content = f.read()

        manifest = self._template_manifest(
            template_content,
            {
                "pvc_name": pvc_name,
                "namespace": namespace,
                "source_pvc_name": source_pvc_name,
                "size": size,
                "storage_class_name": storage_class or "",
                "access_modes": access_modes,
                "timestamp": timestamp,
                "snapshot_id": snapshot_id or "",
                "backup_enabled": backup_enabled,
            },
        )

        args = ["apply", "-f", "-"]
        _, stderr, code = await self.kubectl.run_command(args, stdin_input=manifest)

        if code != 0:
            raise RuntimeError(f"Failed to create project restore PVC: {stderr}")

    async def _create_restore_pod(
        self,
        namespace: str,
        pvc_name: str,
        target_pvc_name: str,
        pod_name: str,
        kopia_password: str,
        backup_prefix: str,
        snapshot_id: str | None = None,
        project_name: str | None = None,
        cluster: str | None = None,
    ) -> None:
        """Create the restore pod."""
        template_path = os.path.join(self.MANIFESTS_DIR, "restore-pod.yaml.jinja")

        if not os.path.exists(template_path):
            raise RuntimeError("restore-pod.yaml.jinja template not found")

        with open(template_path) as f:
            template_content = f.read()

        # Get the bucket name (may be per-project based on config)
        bucket_name = self.config.get_bucket_name(project_name, cluster)

        manifest = self._template_manifest(
            template_content,
            {
                "pod_name": pod_name,
                "namespace": namespace,
                "pvc_name": pvc_name,
                "target_pvc_name": target_pvc_name,
                "s3_endpoint": self.config.s3_endpoint,
                "s3_bucket": bucket_name,
                "s3_access_key": self.config.s3_access_key,
                "s3_secret_key": self.config.s3_secret_key,
                "s3_disable_tls": not self.config.s3_use_tls,
                "backup_prefix": backup_prefix,
                "kopia_password": kopia_password,
                "snapshot_id": snapshot_id or "",
                "timeout_seconds": self.config.timeout_seconds,
                "cluster": cluster or "",
            },
        )

        args = ["apply", "-f", "-"]
        _, stderr, code = await self.kubectl.run_command(args, stdin_input=manifest)

        if code != 0:
            raise RuntimeError(f"Failed to create restore pod: {stderr}")

    # =========================================================================
    # Cleanup Methods
    # =========================================================================

    async def _cleanup(self, namespace: str, pod_name: str, clone_pvc_name: str, snapshot_name: str) -> None:
        """Cleanup backup resources (best effort)."""
        logger.info(f"Cleaning up backup resources in {namespace}")

        # Delete pod
        await self._cleanup_pod(namespace, pod_name)

        # Delete clone PVC
        try:
            args = ["delete", "pvc", clone_pvc_name, "-n", namespace, "--ignore-not-found=true"]
            await self.kubectl.run_command(args)
            logger.debug(f"Deleted clone PVC {clone_pvc_name}")
        except Exception as e:
            logger.warning(f"Failed to delete clone PVC {clone_pvc_name}: {e}")

        # Delete snapshot
        try:
            args = ["delete", "volumesnapshot", snapshot_name, "-n", namespace, "--ignore-not-found=true"]
            await self.kubectl.run_command(args)
            logger.debug(f"Deleted snapshot {snapshot_name}")
        except Exception as e:
            logger.warning(f"Failed to delete snapshot {snapshot_name}: {e}")

        logger.info(f"Cleanup completed for {namespace}")


# Backward compatibility alias
BackupManager = PVCBackupManager


def create_backup_manager(config: BackupConfig | None = None) -> PVCBackupManager:
    """
    Create a PVCBackupManager instance.

    Args:
        config: Optional BackupConfig. If not provided, uses settings.

    Returns:
        PVCBackupManager instance
    """
    return PVCBackupManager(config)
