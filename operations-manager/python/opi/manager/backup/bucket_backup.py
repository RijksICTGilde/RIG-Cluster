"""Bucket Backup Manager - Orchestrates MinIO bucket backups to external S3 using Kopia."""

import logging
import os
from dataclasses import dataclass
from typing import Any

from opi.core.config import settings
from opi.manager.backup.base import (
    BackupConfig,
    BackupResult,
    BaseBackupManager,
    RestoreResult,
    SnapshotInfo,
    utc_now,
)
from opi.utils.naming import generate_backup_prefix

logger = logging.getLogger(__name__)


@dataclass
class BucketBackupResult(BackupResult):
    """Result of a bucket backup operation."""

    bucket_name: str | None = None
    reference_name: str | None = None
    source_type: str | None = None
    use_kopia: bool = True  # True = Kopia backup, False = mc mirror


@dataclass
class BucketRestoreResult(RestoreResult):
    """Result of a bucket restore operation."""

    target_bucket_name: str | None = None
    reference_name: str | None = None


class BucketBackupManager(BaseBackupManager):
    """
    Orchestrates MinIO bucket backups to external S3 using Kopia or mc mirror.

    Two approaches are supported:
    1. Kopia-based (primary): Download bucket via mc, backup through Kopia with encryption
    2. mc mirror (alternative): Direct bucket-to-bucket sync without Kopia encryption

    Flow (Kopia):
    1. Acquire distributed lock
    2. Connect to source MinIO and download bucket to temp dir
    3. Connect to Kopia repository (create if not exists)
    4. Create snapshot with full metadata tags
    5. Apply retention policy
    6. Cleanup and release lock

    Flow (mc mirror):
    1. Acquire lock
    2. Mirror source bucket to backup bucket/path
    3. Create metadata file
    4. Release lock
    """

    async def backup_bucket(
        self,
        namespace: str,
        source_minio_endpoint: str,
        source_bucket_name: str,
        source_access_key: str,
        source_secret_key: str,
        reference_name: str,
        backup_run_id: str,
        source_type: str = "namespace",
        use_kopia: bool = True,
        cluster: str | None = None,
        project_name: str | None = None,
        deployment_name: str | None = None,
        component_name: str | None = None,
        generation: int | None = None,
    ) -> BucketBackupResult:
        """
        Backup a MinIO bucket to S3 via Kopia with SOPS-derived encryption.

        Args:
            namespace: Kubernetes namespace
            source_minio_endpoint: Source MinIO endpoint (e.g., "http://minio.namespace.svc:9000")
            source_bucket_name: Bucket name to backup
            source_access_key: MinIO access key
            source_secret_key: MinIO secret key
            reference_name: Reference name for the bucket (used in naming/tags)
            backup_run_id: Backup run ID to group backups (required)
            source_type: "shared" or "namespace" - indicates bucket source
            use_kopia: True for Kopia backup (encrypted), False for mc mirror (unencrypted)
            cluster: Cluster name for metadata
            project_name: Project name for metadata
            deployment_name: Deployment name for metadata
            component_name: Component name for metadata
            generation: Generation number for metadata

        Returns:
            BucketBackupResult with operation details
        """
        # Ensure backup bucket exists before starting
        await self._ensure_backup_bucket_exists(project_name=project_name, cluster=cluster)

        async with self.lock:
            await self.lock.update_progress(namespace, f"bucket:{reference_name}")

            if use_kopia:
                return await self._backup_bucket_kopia(
                    namespace=namespace,
                    source_minio_endpoint=source_minio_endpoint,
                    source_bucket_name=source_bucket_name,
                    source_access_key=source_access_key,
                    source_secret_key=source_secret_key,
                    reference_name=reference_name,
                    source_type=source_type,
                    cluster=cluster,
                    project_name=project_name,
                    deployment_name=deployment_name,
                    component_name=component_name,
                    generation=generation,
                    backup_run_id=backup_run_id,
                )
            else:
                return await self._backup_bucket_mirror(
                    namespace=namespace,
                    source_minio_endpoint=source_minio_endpoint,
                    source_bucket_name=source_bucket_name,
                    source_access_key=source_access_key,
                    source_secret_key=source_secret_key,
                    reference_name=reference_name,
                    source_type=source_type,
                    cluster=cluster,
                    project_name=project_name,
                    deployment_name=deployment_name,
                    component_name=component_name,
                    generation=generation,
                    backup_run_id=backup_run_id,
                )

    async def _backup_bucket_kopia(
        self,
        namespace: str,
        source_minio_endpoint: str,
        source_bucket_name: str,
        source_access_key: str,
        source_secret_key: str,
        reference_name: str,
        source_type: str,
        backup_run_id: str,
        cluster: str | None = None,
        project_name: str | None = None,
        deployment_name: str | None = None,
        component_name: str | None = None,
        generation: int | None = None,
    ) -> BucketBackupResult:
        """
        Internal: backup a bucket using Kopia (lock must be held).
        """
        start_time = utc_now()
        timestamp = start_time.strftime("%Y%m%d-%H%M%S")

        pod_name = f"bucket-backup-{reference_name[:20]}-{timestamp}"
        backup_cluster = cluster or settings.CLUSTER_MANAGER

        logger.info(f"Starting bucket backup of {reference_name} ({source_bucket_name}@{source_minio_endpoint})")

        try:
            # Derive backup encryption key
            kopia_password = await self._derive_backup_key(namespace)

            # Create backup pod
            logger.info(f"Creating bucket backup pod {pod_name}")
            backup_prefix = generate_backup_prefix(backup_cluster, namespace)
            await self._create_bucket_backup_pod(
                namespace=namespace,
                pod_name=pod_name,
                source_minio_endpoint=source_minio_endpoint,
                source_bucket_name=source_bucket_name,
                source_access_key=source_access_key,
                source_secret_key=source_secret_key,
                reference_name=reference_name,
                source_type=source_type,
                kopia_password=kopia_password,
                timestamp=timestamp,
                backup_prefix=backup_prefix,
                cluster=backup_cluster,
                project_name=project_name,
                deployment_name=deployment_name,
                component_name=component_name,
                generation=generation,
                backup_run_id=backup_run_id,
            )

            # Wait for pod completion
            success = await self._wait_for_pod(namespace, pod_name)

            if not success:
                logs = await self._get_pod_logs(namespace, pod_name)
                logger.error(f"Bucket backup pod {pod_name} failed. Full logs:\n{logs}")
                return BucketBackupResult(
                    namespace=namespace,
                    pvc_name=reference_name,
                    success=False,
                    bucket_name=source_bucket_name,
                    reference_name=reference_name,
                    source_type=source_type,
                    use_kopia=True,
                    error=f"Backup pod failed. Logs: {logs[-500:] if logs else 'no logs'}",
                    duration_seconds=(utc_now() - start_time).total_seconds(),
                )

            duration = (utc_now() - start_time).total_seconds()
            logger.info(f"Bucket backup of {reference_name} completed successfully in {duration:.1f}s")

            return BucketBackupResult(
                namespace=namespace,
                pvc_name=reference_name,
                success=True,
                bucket_name=source_bucket_name,
                reference_name=reference_name,
                source_type=source_type,
                use_kopia=True,
                duration_seconds=duration,
            )

        except Exception as e:
            duration = (utc_now() - start_time).total_seconds()
            logger.exception("Bucket backup of %s failed after %.1fs", reference_name, duration)
            return BucketBackupResult(
                namespace=namespace,
                pvc_name=reference_name,
                success=False,
                bucket_name=source_bucket_name,
                reference_name=reference_name,
                source_type=source_type,
                use_kopia=True,
                error=str(e),
                duration_seconds=duration,
            )

        finally:
            await self._cleanup_pod(namespace, pod_name)

    async def _backup_bucket_mirror(
        self,
        namespace: str,
        source_minio_endpoint: str,
        source_bucket_name: str,
        source_access_key: str,
        source_secret_key: str,
        reference_name: str,
        source_type: str,
        backup_run_id: str,
        cluster: str | None = None,
        project_name: str | None = None,
        deployment_name: str | None = None,
        component_name: str | None = None,
        generation: int | None = None,
    ) -> BucketBackupResult:
        """
        Internal: backup a bucket using mc mirror (lock must be held).
        """
        start_time = utc_now()
        timestamp = start_time.strftime("%Y%m%d-%H%M%S")

        pod_name = f"bucket-mirror-{reference_name[:20]}-{timestamp}"
        backup_cluster = cluster or settings.CLUSTER_MANAGER

        logger.info(f"Starting bucket mirror of {reference_name} ({source_bucket_name}@{source_minio_endpoint})")

        try:
            # Build backup path
            project = project_name or "unknown"
            deployment = deployment_name or "unknown"
            gen = generation or 0
            backup_path = f"backups/{project}/{deployment}/{reference_name}/gen-{gen}/{timestamp}"

            # Create mirror pod
            logger.info(f"Creating bucket mirror pod {pod_name}")
            await self._create_bucket_mirror_pod(
                namespace=namespace,
                pod_name=pod_name,
                source_minio_endpoint=source_minio_endpoint,
                source_bucket_name=source_bucket_name,
                source_access_key=source_access_key,
                source_secret_key=source_secret_key,
                reference_name=reference_name,
                source_type=source_type,
                backup_path=backup_path,
                timestamp=timestamp,
                cluster=backup_cluster,
                project_name=project_name,
                deployment_name=deployment_name,
                component_name=component_name,
                generation=generation,
                backup_run_id=backup_run_id,
            )

            # Wait for pod completion
            success = await self._wait_for_pod(namespace, pod_name)

            if not success:
                logs = await self._get_pod_logs(namespace, pod_name)
                logger.error(f"Bucket mirror pod {pod_name} failed. Full logs:\n{logs}")
                return BucketBackupResult(
                    namespace=namespace,
                    pvc_name=reference_name,
                    success=False,
                    bucket_name=source_bucket_name,
                    reference_name=reference_name,
                    source_type=source_type,
                    use_kopia=False,
                    error=f"Mirror pod failed. Logs: {logs[-500:] if logs else 'no logs'}",
                    duration_seconds=(utc_now() - start_time).total_seconds(),
                )

            duration = (utc_now() - start_time).total_seconds()
            logger.info(f"Bucket mirror of {reference_name} completed successfully in {duration:.1f}s")

            return BucketBackupResult(
                namespace=namespace,
                pvc_name=reference_name,
                success=True,
                bucket_name=source_bucket_name,
                reference_name=reference_name,
                source_type=source_type,
                use_kopia=False,
                duration_seconds=duration,
            )

        except Exception as e:
            duration = (utc_now() - start_time).total_seconds()
            logger.exception("Bucket mirror of %s failed after %.1fs", reference_name, duration)
            return BucketBackupResult(
                namespace=namespace,
                pvc_name=reference_name,
                success=False,
                bucket_name=source_bucket_name,
                reference_name=reference_name,
                source_type=source_type,
                use_kopia=False,
                error=str(e),
                duration_seconds=duration,
            )

        finally:
            await self._cleanup_pod(namespace, pod_name)

    async def _create_bucket_backup_pod(
        self,
        namespace: str,
        pod_name: str,
        source_minio_endpoint: str,
        source_bucket_name: str,
        source_access_key: str,
        source_secret_key: str,
        reference_name: str,
        source_type: str,
        kopia_password: str,
        timestamp: str,
        backup_prefix: str,
        backup_run_id: str,
        cluster: str | None = None,
        project_name: str | None = None,
        deployment_name: str | None = None,
        component_name: str | None = None,
        generation: int | None = None,
    ) -> None:
        """Create the bucket backup pod (Kopia approach)."""
        template_path = os.path.join(self.MANIFESTS_DIR, "backup-bucket-pod.yaml.jinja")

        with open(template_path) as f:
            template_content = f.read()

        # Get the bucket name (may be per-project based on config)
        effective_cluster = cluster or settings.CLUSTER_MANAGER
        bucket_name = self.config.get_bucket_name(project_name, effective_cluster)

        manifest = self.kubectl.template_manifest(
            template_content,
            {
                "pod_name": pod_name,
                "namespace": namespace,
                "source_minio_endpoint": source_minio_endpoint,
                "source_bucket_name": source_bucket_name,
                "source_access_key": source_access_key,
                "source_secret_key": source_secret_key,
                "reference_name": reference_name,
                "source_type": source_type,
                "timestamp": timestamp,
                "s3_endpoint": self.config.s3_endpoint,
                "s3_bucket": bucket_name,
                "s3_access_key": self.config.s3_access_key,
                "s3_secret_key": self.config.s3_secret_key,
                "s3_disable_tls": not self.config.s3_use_tls,
                "backup_prefix": backup_prefix,
                "kopia_password": kopia_password,
                "timeout_seconds": self.config.timeout_seconds,
                "retention_keep_latest": self.config.retention_keep_latest,
                "retention_keep_daily": self.config.retention_keep_daily,
                "retention_keep_weekly": self.config.retention_keep_weekly,
                # Metadata
                "cluster": effective_cluster,
                "project_name": project_name,
                "deployment_name": deployment_name,
                "component_name": component_name,
                "generation": generation if generation is not None else 0,
                "backup_run_id": backup_run_id,
            },
        )

        args = ["apply", "-f", "-"]
        _, stderr, code = await self.kubectl.run_command(args, stdin_input=manifest)

        if code != 0:
            raise RuntimeError(f"Failed to create bucket backup pod: {stderr}")

    async def _create_bucket_mirror_pod(
        self,
        namespace: str,
        pod_name: str,
        source_minio_endpoint: str,
        source_bucket_name: str,
        source_access_key: str,
        source_secret_key: str,
        reference_name: str,
        source_type: str,
        backup_path: str,
        timestamp: str,
        backup_run_id: str,
        cluster: str | None = None,
        project_name: str | None = None,
        deployment_name: str | None = None,
        component_name: str | None = None,
        generation: int | None = None,
    ) -> None:
        """Create the bucket mirror pod (mc mirror approach)."""
        template_path = os.path.join(self.MANIFESTS_DIR, "backup-bucket-mirror-pod.yaml.jinja")

        with open(template_path) as f:
            template_content = f.read()

        # Build backup MinIO endpoint
        backup_endpoint = self.config.s3_endpoint
        if not backup_endpoint.startswith("http"):
            backup_endpoint = f"http://{backup_endpoint}"

        # Get the bucket name (may be per-project based on config)
        effective_cluster = cluster or settings.CLUSTER_MANAGER
        bucket_name = self.config.get_bucket_name(project_name, effective_cluster)

        manifest = self.kubectl.template_manifest(
            template_content,
            {
                "pod_name": pod_name,
                "namespace": namespace,
                "source_minio_endpoint": source_minio_endpoint,
                "source_bucket_name": source_bucket_name,
                "source_access_key": source_access_key,
                "source_secret_key": source_secret_key,
                "reference_name": reference_name,
                "source_type": source_type,
                "backup_minio_endpoint": backup_endpoint,
                "backup_bucket": bucket_name,
                "backup_access_key": self.config.s3_access_key,
                "backup_secret_key": self.config.s3_secret_key,
                "backup_path": backup_path,
                "timestamp": timestamp,
                "timeout_seconds": self.config.timeout_seconds,
                # Metadata
                "cluster": effective_cluster,
                "project_name": project_name,
                "deployment_name": deployment_name,
                "component_name": component_name,
                "generation": generation if generation is not None else 0,
                "backup_run_id": backup_run_id,
            },
        )

        args = ["apply", "-f", "-"]
        _, stderr, code = await self.kubectl.run_command(args, stdin_input=manifest)

        if code != 0:
            raise RuntimeError(f"Failed to create bucket mirror pod: {stderr}")

    # =========================================================================
    # Restore Methods
    # =========================================================================

    async def restore_bucket(
        self,
        cluster: str,
        namespace: str,
        reference_name: str,
        target_minio_endpoint: str,
        target_bucket_name: str,
        target_access_key: str,
        target_secret_key: str,
        snapshot_id: str | None = None,
        clear_target: bool = False,
    ) -> BucketRestoreResult:
        """
        Restore a MinIO bucket from a Kopia backup.

        Args:
            cluster: Cluster name where backup was made
            namespace: Namespace for key derivation
            reference_name: Reference name of the bucket backup
            target_minio_endpoint: Target MinIO endpoint
            target_bucket_name: Target bucket name
            target_access_key: Target MinIO access key
            target_secret_key: Target MinIO secret key
            snapshot_id: Optional specific snapshot ID (defaults to latest)
            clear_target: If True, clear target bucket before restore

        Returns:
            BucketRestoreResult with operation details
        """
        async with self.lock:
            await self.lock.update_progress(namespace, f"restore-bucket:{reference_name}")
            return await self._restore_bucket(
                cluster=cluster,
                namespace=namespace,
                reference_name=reference_name,
                target_minio_endpoint=target_minio_endpoint,
                target_bucket_name=target_bucket_name,
                target_access_key=target_access_key,
                target_secret_key=target_secret_key,
                snapshot_id=snapshot_id,
                clear_target=clear_target,
            )

    async def _restore_bucket(
        self,
        cluster: str,
        namespace: str,
        reference_name: str,
        target_minio_endpoint: str,
        target_bucket_name: str,
        target_access_key: str,
        target_secret_key: str,
        snapshot_id: str | None = None,
        clear_target: bool = False,
    ) -> BucketRestoreResult:
        """
        Internal: restore a bucket from Kopia (lock must be held).
        """
        start_time = utc_now()
        timestamp = start_time.strftime("%Y%m%d-%H%M%S")

        pod_name = f"bucket-restore-{reference_name[:20]}-{timestamp}"
        backup_prefix = generate_backup_prefix(cluster, namespace)

        logger.info(f"Starting bucket restore: {reference_name} -> {target_bucket_name}@{target_minio_endpoint}")

        try:
            # Derive restore key (same as backup key)
            kopia_password = await self._derive_backup_key(namespace)

            # Create restore pod
            logger.info(f"Creating bucket restore pod {pod_name}")
            await self._create_bucket_restore_pod(
                namespace=namespace,
                pod_name=pod_name,
                reference_name=reference_name,
                target_minio_endpoint=target_minio_endpoint,
                target_bucket_name=target_bucket_name,
                target_access_key=target_access_key,
                target_secret_key=target_secret_key,
                kopia_password=kopia_password,
                backup_prefix=backup_prefix,
                snapshot_id=snapshot_id,
                clear_target=clear_target,
            )

            # Wait for pod completion
            success = await self._wait_for_pod(namespace, pod_name)

            if not success:
                logs = await self._get_pod_logs(namespace, pod_name)
                logger.error(f"Bucket restore pod {pod_name} failed. Full logs:\n{logs}")
                return BucketRestoreResult(
                    namespace=namespace,
                    pvc_name=reference_name,
                    success=False,
                    target_bucket_name=target_bucket_name,
                    reference_name=reference_name,
                    snapshot_id=snapshot_id,
                    error=f"Restore pod failed. Logs: {logs[-500:] if logs else 'no logs'}",
                    duration_seconds=(utc_now() - start_time).total_seconds(),
                )

            duration = (utc_now() - start_time).total_seconds()
            logger.info(f"Bucket restore of {reference_name} to {target_bucket_name} completed in {duration:.1f}s")

            return BucketRestoreResult(
                namespace=namespace,
                pvc_name=reference_name,
                success=True,
                target_pvc_name=target_bucket_name,
                target_bucket_name=target_bucket_name,
                reference_name=reference_name,
                snapshot_id=snapshot_id,
                duration_seconds=duration,
            )

        except Exception as e:
            duration = (utc_now() - start_time).total_seconds()
            logger.exception("Bucket restore of %s failed after %.1fs", reference_name, duration)
            return BucketRestoreResult(
                namespace=namespace,
                pvc_name=reference_name,
                success=False,
                target_bucket_name=target_bucket_name,
                reference_name=reference_name,
                error=str(e),
                duration_seconds=duration,
            )

        finally:
            await self._cleanup_pod(namespace, pod_name)

    async def _create_bucket_restore_pod(
        self,
        namespace: str,
        pod_name: str,
        reference_name: str,
        target_minio_endpoint: str,
        target_bucket_name: str,
        target_access_key: str,
        target_secret_key: str,
        kopia_password: str,
        backup_prefix: str,
        snapshot_id: str | None = None,
        clear_target: bool = False,
        project_name: str | None = None,
        cluster: str | None = None,
    ) -> None:
        """Create the bucket restore pod."""
        template_path = os.path.join(self.MANIFESTS_DIR, "restore-bucket-pod.yaml.jinja")

        with open(template_path) as f:
            template_content = f.read()

        # Get the bucket name (may be per-project based on config)
        bucket_name = self.config.get_bucket_name(project_name, cluster)

        manifest = self.kubectl.template_manifest(
            template_content,
            {
                "pod_name": pod_name,
                "namespace": namespace,
                "reference_name": reference_name,
                "target_minio_endpoint": target_minio_endpoint,
                "target_bucket_name": target_bucket_name,
                "target_access_key": target_access_key,
                "target_secret_key": target_secret_key,
                "s3_endpoint": self.config.s3_endpoint,
                "s3_bucket": bucket_name,
                "s3_access_key": self.config.s3_access_key,
                "s3_secret_key": self.config.s3_secret_key,
                "s3_disable_tls": not self.config.s3_use_tls,
                "backup_prefix": backup_prefix,
                "kopia_password": kopia_password,
                "snapshot_id": snapshot_id or "",
                "timeout_seconds": self.config.timeout_seconds,
                "clear_target": clear_target,
            },
        )

        args = ["apply", "-f", "-"]
        _, stderr, code = await self.kubectl.run_command(args, stdin_input=manifest)

        if code != 0:
            raise RuntimeError(f"Failed to create bucket restore pod: {stderr}")

    # =========================================================================
    # List Snapshots
    # =========================================================================

    async def list_bucket_snapshots(
        self,
        cluster: str,
        namespace: str,
        reference_name: str | None = None,
        project_name: str | None = None,
    ) -> list[SnapshotInfo]:
        """
        List available bucket snapshots for a namespace.

        Args:
            cluster: Cluster name
            namespace: Namespace name
            reference_name: Optional bucket reference name to filter by
            project_name: Optional project name for per-project bucket mode

        Returns:
            List of available snapshots
        """
        from opi.connectors.kopia import KopiaConnector, KopiaRepositoryConfig

        backup_prefix = generate_backup_prefix(cluster, namespace)
        kopia_password = await self._derive_backup_key(namespace)
        bucket_name = self.config.get_bucket_name(project_name, cluster)

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

            # Filter by resource_type=bucket and optionally by reference_name
            snapshots: list[SnapshotInfo] = []
            for ks in kopia_snapshots:
                # Only include bucket snapshots
                if ks.resource_type != "bucket":
                    continue

                # Get bucket reference from tags
                bucket_ref = ks.tags.get("tag:bucket") if ks.tags else None

                # Apply filter if specified
                if reference_name and bucket_ref != reference_name:
                    continue

                snapshots.append(
                    SnapshotInfo(
                        snapshot_id=ks.snapshot_id,
                        pvc_name=bucket_ref or ks.pvc_name,
                        timestamp=ks.timestamp,
                        size_bytes=ks.size_bytes,
                        cluster=ks.cluster,
                        namespace=ks.namespace,
                        project_name=ks.project_name,
                        deployment_name=ks.deployment_name,
                        component_name=ks.component_name,
                        storage_name=bucket_ref,
                        generation=ks.generation,
                        backup_run_id=ks.backup_run_id,
                        resource_type="bucket",
                        tags=ks.tags,
                    )
                )

            logger.debug(f"Found {len(snapshots)} bucket snapshots for {namespace}")
            return snapshots

        except Exception as e:
            logger.warning(f"Failed to list bucket snapshots via Kopia CLI: {e}")
            return []

    async def backup_project_bucket(
        self,
        project_name: str,
        project_data: dict[str, Any],
        deployment_name: str,
        namespace: str,
        cluster: str,
        component_name: str,
        reference_name: str,
        use_kopia: bool = True,
    ) -> BucketBackupResult:
        """
        Backup a bucket for a project deployment with full metadata context.

        This method is a placeholder - actual implementation would need to
        extract MinIO connection information from the project file or cluster.

        Args:
            project_name: Name of the project
            project_data: Project configuration data
            deployment_name: Name of the deployment
            namespace: Kubernetes namespace (with cluster prefix)
            cluster: Cluster name
            component_name: Component name containing the bucket
            reference_name: Bucket reference name
            use_kopia: True for Kopia backup, False for mc mirror

        Returns:
            BucketBackupResult with operation details
        """
        # TODO: Extract MinIO connection info from project_data
        # For now, this would need to be passed in or discovered from the cluster
        # The actual implementation would need to:
        # 1. Look up the MinIO service in the namespace
        # 2. Get connection details from secrets/configmaps
        # 3. Call backup_bucket with the extracted info

        logger.warning(
            f"backup_project_bucket is a placeholder - MinIO connection info extraction not yet implemented. "
            f"Project: {project_name}, Deployment: {deployment_name}, Bucket: {reference_name}"
        )

        return BucketBackupResult(
            namespace=namespace,
            pvc_name=reference_name,
            success=False,
            bucket_name=reference_name,
            reference_name=reference_name,
            use_kopia=use_kopia,
            error="MinIO connection info extraction not yet implemented - use backup_bucket directly",
            duration_seconds=0,
        )


def create_bucket_backup_manager(config: BackupConfig | None = None) -> BucketBackupManager:
    """
    Create a BucketBackupManager instance.

    Args:
        config: Optional BackupConfig. If not provided, uses settings.

    Returns:
        BucketBackupManager instance
    """
    return BucketBackupManager(config)
