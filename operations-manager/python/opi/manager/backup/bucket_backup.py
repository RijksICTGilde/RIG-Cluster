"""Bucket Backup Manager - Orchestrates MinIO bucket backups to external S3 using Kopia."""

import logging
import os
from dataclasses import dataclass

from opi.core.backup_constants import (
    BACKUP_REPOSITORY_UNUSABLE_EXIT_CODE,
    RESTORE_TARGET_UNUSABLE_EXIT_CODE,
)
from opi.core.config import settings
from opi.manager.backup.base import (
    BackupConfig,
    BackupResult,
    BaseBackupManager,
    RestoreResult,
    SnapshotInfo,
    kopia_backup_identity,
    mc_endpoint_is_usable,
    mc_host_url,
    pod_credentials_secret_name,
    utc_now,
)
from opi.utils.naming import generate_backup_prefix

logger = logging.getLogger(__name__)


@dataclass
class BucketBackupResult(BackupResult):
    bucket_name: str | None = None
    reference_name: str | None = None
    source_type: str | None = None
    use_kopia: bool = True  # True = Kopia backup, False = mc mirror


@dataclass
class BucketRestoreResult(RestoreResult):
    target_bucket_name: str | None = None
    reference_name: str | None = None


class BucketBackupManager(BaseBackupManager):
    """Orchestrates MinIO bucket backups to external S3 using Kopia or mc mirror.

    The Kopia route downloads the bucket with mc and snapshots it encrypted; the mc mirror
    route syncs bucket to bucket without encryption. Both take the distributed lock first.
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
        trigger: str = "manual",
    ) -> BucketBackupResult:
        """Backup a MinIO bucket to S3 via Kopia with SOPS-derived encryption.

        Args:
            reference_name: Name the bucket is known by in naming and tags.
            backup_run_id: Groups the backups of one run; required.
            source_type: "shared" or "namespace".
            use_kopia: False falls back to an unencrypted mc mirror.
        """
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
                    trigger=trigger,
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
        trigger: str = "manual",
    ) -> BucketBackupResult:
        """The lock must be held."""
        start_time = utc_now()
        timestamp = start_time.strftime("%Y%m%d-%H%M%S")

        pod_name = f"bucket-backup-{reference_name[:20]}-{timestamp}"
        backup_cluster = cluster or settings.CLUSTER_MANAGER

        logger.info(f"Starting bucket backup of {reference_name} ({source_bucket_name}@{source_minio_endpoint})")

        try:
            kopia_password = await self._derive_backup_key(namespace)

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
                trigger=trigger,
            )

            success = await self._wait_for_pod(namespace, pod_name)

            if not success:
                logs = await self._get_pod_logs(namespace, pod_name)
                logger.error(f"Bucket backup pod {pod_name} failed. Full logs:\n{logs}")
                error = await self._backup_pod_failure_error(namespace, pod_name, logs)
                return BucketBackupResult(
                    namespace=namespace,
                    pvc_name=reference_name,
                    success=False,
                    bucket_name=source_bucket_name,
                    reference_name=reference_name,
                    source_type=source_type,
                    use_kopia=True,
                    error=error,
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
        """The lock must be held."""
        start_time = utc_now()
        timestamp = start_time.strftime("%Y%m%d-%H%M%S")

        pod_name = f"bucket-mirror-{reference_name[:20]}-{timestamp}"
        backup_cluster = cluster or settings.CLUSTER_MANAGER

        logger.info(f"Starting bucket mirror of {reference_name} ({source_bucket_name}@{source_minio_endpoint})")

        try:
            project = project_name or "unknown"
            deployment = deployment_name or "unknown"
            gen = generation or 0
            backup_path = f"backups/{project}/{deployment}/{reference_name}/gen-{gen}/{timestamp}"

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
        trigger: str = "manual",
    ) -> None:
        template_path = os.path.join(self.MANIFESTS_DIR, "backup-bucket-pod.yaml.jinja")

        with open(template_path) as f:
            template_content = f.read()

        effective_cluster = cluster or settings.CLUSTER_MANAGER
        bucket_name = self.config.get_bucket_name(project_name, effective_cluster)

        # Stable per-resource Kopia source identity for this bucket.
        kopia_hostname, kopia_source = kopia_backup_identity(
            project_name=project_name,
            deployment_name=deployment_name,
            resource_kind="bucket",
            resource_name=reference_name,
            trigger=trigger,
        )

        manifest = self._template_manifest(
            template_content,
            {
                "pod_name": pod_name,
                "namespace": namespace,
                "service_account_name": self._pod_service_account_name(project_name),
                "source_minio_endpoint": source_minio_endpoint,
                "source_bucket_name": source_bucket_name,
                "reference_name": reference_name,
                "source_type": source_type,
                "timestamp": timestamp,
                "s3_endpoint": self.config.s3_endpoint,
                "s3_bucket": bucket_name,
                "s3_disable_tls": not self.config.s3_use_tls,
                "backup_prefix": backup_prefix,
                "kopia_hostname": kopia_hostname,
                "kopia_source": kopia_source,
                "credentials_secret": pod_credentials_secret_name(pod_name),
                "repository_unusable_exit_code": BACKUP_REPOSITORY_UNUSABLE_EXIT_CODE,
                "timeout_seconds": self.config.timeout_seconds,
                "retention_keep_latest": self.config.retention_keep_latest,
                "retention_keep_daily": self.config.retention_keep_daily,
                "retention_keep_weekly": self.config.retention_keep_weekly,
                "retention_keep_monthly": self.config.retention_keep_monthly,
                "cluster": effective_cluster,
                "project_name": project_name,
                "deployment_name": deployment_name,
                "component_name": component_name,
                "generation": generation if generation is not None else 0,
                "backup_run_id": backup_run_id,
                "trigger": trigger,
            },
        )

        await self._apply_pod_with_credentials(
            namespace=namespace,
            pod_name=pod_name,
            manifest=manifest,
            credentials=self._kopia_credentials(kopia_password, bucket_name)
            | {"MC_HOST_source": mc_host_url(source_minio_endpoint, source_access_key, source_secret_key)},
            what="bucket backup",
        )

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
        template_path = os.path.join(self.MANIFESTS_DIR, "backup-bucket-mirror-pod.yaml.jinja")

        with open(template_path) as f:
            template_content = f.read()

        backup_endpoint = self.config.s3_endpoint
        if not backup_endpoint.startswith("http"):
            backup_endpoint = f"http://{backup_endpoint}"

        effective_cluster = cluster or settings.CLUSTER_MANAGER
        bucket_name = self.config.get_bucket_name(project_name, effective_cluster)

        manifest = self._template_manifest(
            template_content,
            {
                "pod_name": pod_name,
                "namespace": namespace,
                "service_account_name": self._pod_service_account_name(project_name),
                "source_minio_endpoint": source_minio_endpoint,
                "source_bucket_name": source_bucket_name,
                "reference_name": reference_name,
                "source_type": source_type,
                "backup_minio_endpoint": backup_endpoint,
                "backup_bucket": bucket_name,
                "backup_path": backup_path,
                "timestamp": timestamp,
                "credentials_secret": pod_credentials_secret_name(pod_name),
                "timeout_seconds": self.config.timeout_seconds,
                "cluster": effective_cluster,
                "project_name": project_name,
                "deployment_name": deployment_name,
                "component_name": component_name,
                "generation": generation if generation is not None else 0,
                "backup_run_id": backup_run_id,
            },
        )

        await self._apply_pod_with_credentials(
            namespace=namespace,
            pod_name=pod_name,
            manifest=manifest,
            credentials={
                "MC_HOST_source": mc_host_url(source_minio_endpoint, source_access_key, source_secret_key),
                "MC_HOST_backup": mc_host_url(backup_endpoint, *self._pod_s3_credentials(bucket_name)),
            },
            what="bucket mirror",
        )

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
        project_name: str | None = None,
    ) -> BucketRestoreResult:
        """Restore a MinIO bucket from a Kopia backup.

        Args:
            snapshot_id: A specific snapshot; the latest one without it.
            clear_target: Empty the target bucket before the restore.
        """
        # Ensure the bucket and its scoped S3 account exist before the restore pod needs them
        await self._ensure_backup_bucket_exists(project_name=project_name, cluster=cluster)

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
                project_name=project_name,
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
        project_name: str | None = None,
    ) -> BucketRestoreResult:
        """The lock must be held."""
        start_time = utc_now()
        timestamp = start_time.strftime("%Y%m%d-%H%M%S")

        pod_name = f"bucket-restore-{reference_name[:20]}-{timestamp}"
        backup_prefix = generate_backup_prefix(cluster, namespace)

        logger.info(f"Starting bucket restore: {reference_name} -> {target_bucket_name}@{target_minio_endpoint}")

        # The target endpoint comes from the caller, so an unusable form is an InvalidTarget.
        # The gate in the pod can no longer judge it: mc gets its alias from MC_HOST_target,
        # and that URL is built here (RC-271).
        if not mc_endpoint_is_usable(target_minio_endpoint):
            return BucketRestoreResult(
                namespace=namespace,
                pvc_name=reference_name,
                success=False,
                target_bucket_name=target_bucket_name,
                reference_name=reference_name,
                error=f"Target MinIO endpoint needs a scheme and a host, got {target_minio_endpoint!r}",
                duration_seconds=(utc_now() - start_time).total_seconds(),
                target_unusable=True,
            )

        try:
            kopia_password = await self._derive_backup_key(namespace)

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
                project_name=project_name,
                cluster=cluster,
            )

            success = await self._wait_for_pod(namespace, pod_name)

            if not success:
                target_unusable = await self._restore_target_was_unusable(namespace, pod_name)
                logs = await self._get_pod_logs(namespace, pod_name)
                logger.error(
                    f"Bucket restore pod {pod_name} failed (target_unusable={target_unusable}). Full logs:\n{logs}"
                )
                return BucketRestoreResult(
                    namespace=namespace,
                    pvc_name=reference_name,
                    success=False,
                    target_bucket_name=target_bucket_name,
                    reference_name=reference_name,
                    snapshot_id=snapshot_id,
                    error=f"Restore pod failed. Logs: {logs[-500:] if logs else 'no logs'}",
                    duration_seconds=(utc_now() - start_time).total_seconds(),
                    target_unusable=target_unusable,
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
        template_path = os.path.join(self.MANIFESTS_DIR, "restore-bucket-pod.yaml.jinja")

        with open(template_path) as f:
            template_content = f.read()

        bucket_name = self.config.get_bucket_name(project_name, cluster)

        manifest = self._template_manifest(
            template_content,
            {
                "pod_name": pod_name,
                "namespace": namespace,
                "service_account_name": self._pod_service_account_name(project_name),
                "reference_name": reference_name,
                "target_minio_endpoint": target_minio_endpoint,
                "target_bucket_name": target_bucket_name,
                "s3_endpoint": self.config.s3_endpoint,
                "s3_bucket": bucket_name,
                "s3_disable_tls": not self.config.s3_use_tls,
                "backup_prefix": backup_prefix,
                "snapshot_id": snapshot_id or "",
                "credentials_secret": pod_credentials_secret_name(pod_name),
                "timeout_seconds": self.config.timeout_seconds,
                "target_unusable_exit_code": RESTORE_TARGET_UNUSABLE_EXIT_CODE,
                "clear_target": clear_target,
            },
        )

        await self._apply_pod_with_credentials(
            namespace=namespace,
            pod_name=pod_name,
            manifest=manifest,
            credentials=self._kopia_credentials(kopia_password, bucket_name)
            | {"MC_HOST_target": mc_host_url(target_minio_endpoint, target_access_key, target_secret_key)},
            what="bucket restore",
        )

    async def list_bucket_snapshots(
        self,
        cluster: str,
        namespace: str,
        reference_name: str | None = None,
        project_name: str | None = None,
    ) -> list[SnapshotInfo]:
        """The bucket snapshots of a namespace, optionally only those of one reference."""
        from opi.connectors.kopia import (
            KopiaConnectionError,
            KopiaConnector,
            KopiaExecutionError,
            KopiaRepositoryConfig,
        )

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

            snapshots: list[SnapshotInfo] = []
            for ks in kopia_snapshots:
                if ks.resource_type != "bucket":
                    continue

                bucket_ref = ks.tags.get("tag:bucket") if ks.tags else None

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
                        trigger=ks.trigger,
                        tags=ks.tags,
                    )
                )

            logger.debug(f"Found {len(snapshots)} bucket snapshots for {namespace}")
            return snapshots

        except (OSError, ValueError, KeyError, TypeError, KopiaConnectionError, KopiaExecutionError) as e:
            logger.warning(f"Failed to list bucket snapshots via Kopia CLI: {e}")
            return []


def create_bucket_backup_manager(config: BackupConfig | None = None) -> BucketBackupManager:
    """Without a ``config`` the manager is built from the settings."""
    return BucketBackupManager(config)
