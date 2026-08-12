"""Database Backup Manager - Orchestrates PostgreSQL backups to external S3 using Kopia."""

import logging
import os
from dataclasses import dataclass
from typing import Any

from opi.core.backup_constants import RESTORE_TARGET_UNUSABLE_EXIT_CODE
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
from opi.utils.naming import generate_backup_prefix

logger = logging.getLogger(__name__)


@dataclass
class DatabaseBackupResult(BackupResult):
    """Result of a database backup operation."""

    database_name: str | None = None
    reference_name: str | None = None
    source_type: str | None = None


@dataclass
class DatabaseRestoreResult(RestoreResult):
    """Result of a database restore operation."""

    target_database_name: str | None = None
    reference_name: str | None = None


class DatabaseBackupManager(BaseBackupManager):
    """
    Orchestrates PostgreSQL database backups to external S3 using Kopia.

    Flow:
    1. Acquire distributed lock
    2. Connect to Kopia repository (create if not exists)
    3. Stream pg_dump output through Kopia snapshot
    4. Apply retention policy
    5. Release lock

    Restore Flow:
    1. Acquire lock
    2. Connect to Kopia repository
    3. Restore dump file from snapshot
    4. Stream to pg_restore
    5. Cleanup and release lock
    """

    async def backup_database(
        self,
        namespace: str,
        database_host: str,
        database_port: int,
        database_name: str,
        database_user: str,
        database_password: str,
        reference_name: str,
        backup_run_id: str,
        source_type: str = "namespace",
        cluster: str | None = None,
        project_name: str | None = None,
        deployment_name: str | None = None,
        component_name: str | None = None,
        generation: int | None = None,
        trigger: str = "manual",
    ) -> DatabaseBackupResult:
        """
        Backup a PostgreSQL database to S3 via Kopia with SOPS-derived encryption.

        Args:
            namespace: Kubernetes namespace
            database_host: PostgreSQL host
            database_port: PostgreSQL port
            database_name: Database name to backup
            database_user: Database user
            database_password: Database password
            reference_name: Reference name for the database (used in naming/tags)
            source_type: "shared" or "namespace" - indicates database source
            cluster: Cluster name for metadata
            project_name: Project name for metadata
            deployment_name: Deployment name for metadata
            component_name: Component name for metadata
            generation: Generation number for metadata
            backup_run_id: Backup run ID to group backups (required)

        Returns:
            DatabaseBackupResult with operation details
        """
        # Ensure backup bucket exists before starting
        await self._ensure_backup_bucket_exists(project_name=project_name, cluster=cluster)

        async with self.lock:
            await self.lock.update_progress(namespace, f"database:{reference_name}")
            return await self._backup_database(
                namespace=namespace,
                database_host=database_host,
                database_port=database_port,
                database_name=database_name,
                database_user=database_user,
                database_password=database_password,
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

    async def _backup_database(
        self,
        namespace: str,
        database_host: str,
        database_port: int,
        database_name: str,
        database_user: str,
        database_password: str,
        reference_name: str,
        source_type: str,
        backup_run_id: str,
        cluster: str | None = None,
        project_name: str | None = None,
        deployment_name: str | None = None,
        component_name: str | None = None,
        generation: int | None = None,
        trigger: str = "manual",
    ) -> DatabaseBackupResult:
        """
        Internal: backup a PostgreSQL database (lock must be held).
        """
        start_time = utc_now()
        timestamp = start_time.strftime("%Y%m%d-%H%M%S")

        # Generate pod name
        pod_name = f"db-backup-{reference_name[:20]}-{timestamp}"

        # Determine cluster for backup prefix
        backup_cluster = cluster or settings.CLUSTER_MANAGER

        logger.info(f"Starting database backup of {reference_name} ({database_name}@{database_host})")

        try:
            # 1. Derive backup encryption key from namespace's SOPS key
            kopia_password = await self._derive_backup_key(namespace)

            # 2. Create backup pod
            logger.info(f"Creating database backup pod {pod_name}")
            backup_prefix = generate_backup_prefix(backup_cluster, namespace)
            await self._create_database_backup_pod(
                namespace=namespace,
                pod_name=pod_name,
                database_host=database_host,
                database_port=database_port,
                database_name=database_name,
                database_user=database_user,
                database_password=database_password,
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

            # 3. Wait for pod completion
            success = await self._wait_for_pod(namespace, pod_name)

            if not success:
                logs = await self._get_pod_logs(namespace, pod_name)
                logger.error(f"Database backup pod {pod_name} failed. Full logs:\n{logs}")
                return DatabaseBackupResult(
                    namespace=namespace,
                    pvc_name=reference_name,  # Use reference_name for compatibility
                    success=False,
                    database_name=database_name,
                    reference_name=reference_name,
                    source_type=source_type,
                    error=f"Backup pod failed. Logs: {logs[-500:] if logs else 'no logs'}",
                    duration_seconds=(utc_now() - start_time).total_seconds(),
                )

            duration = (utc_now() - start_time).total_seconds()
            logger.info(f"Database backup of {reference_name} completed successfully in {duration:.1f}s")

            return DatabaseBackupResult(
                namespace=namespace,
                pvc_name=reference_name,  # Use reference_name for compatibility
                success=True,
                database_name=database_name,
                reference_name=reference_name,
                source_type=source_type,
                duration_seconds=duration,
            )

        except Exception as e:
            duration = (utc_now() - start_time).total_seconds()
            logger.exception("Database backup of %s failed after %.1fs", reference_name, duration)
            return DatabaseBackupResult(
                namespace=namespace,
                pvc_name=reference_name,
                success=False,
                database_name=database_name,
                reference_name=reference_name,
                source_type=source_type,
                error=str(e),
                duration_seconds=duration,
            )

        finally:
            # Cleanup pod (best effort)
            await self._cleanup_pod(namespace, pod_name)

    async def _create_database_backup_pod(
        self,
        namespace: str,
        pod_name: str,
        database_host: str,
        database_port: int,
        database_name: str,
        database_user: str,
        database_password: str,
        reference_name: str,
        source_type: str,
        kopia_password: str,
        timestamp: str,
        backup_prefix: str,
        cluster: str | None = None,
        project_name: str | None = None,
        deployment_name: str | None = None,
        component_name: str | None = None,
        generation: int | None = None,
        backup_run_id: str | None = None,
        trigger: str = "manual",
    ) -> None:
        """Create the database backup pod."""
        template_path = os.path.join(self.MANIFESTS_DIR, "backup-database-pod.yaml.jinja")

        with open(template_path) as f:
            template_content = f.read()

        # Get the bucket name (may be per-project based on config)
        effective_cluster = cluster or settings.CLUSTER_MANAGER
        bucket_name = self.config.get_bucket_name(project_name, effective_cluster)

        # Stable per-resource Kopia source identity for this database.
        kopia_hostname, kopia_source = kopia_backup_identity(
            project_name=project_name,
            deployment_name=deployment_name,
            resource_kind="db",
            resource_name=reference_name,
            trigger=trigger,
        )

        manifest = self._template_manifest(
            template_content,
            {
                "pod_name": pod_name,
                "namespace": namespace,
                "db_host": database_host,
                "db_port": database_port,
                "db_name": database_name,
                "db_user": database_user,
                "db_password": database_password,
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
                "kopia_hostname": kopia_hostname,
                "kopia_source": kopia_source,
                "timeout_seconds": self.config.timeout_seconds,
                "retention_keep_latest": self.config.retention_keep_latest,
                "retention_keep_daily": self.config.retention_keep_daily,
                "retention_keep_weekly": self.config.retention_keep_weekly,
                "retention_keep_monthly": self.config.retention_keep_monthly,
                # Metadata
                "cluster": effective_cluster,
                "project_name": project_name,
                "deployment_name": deployment_name,
                "component_name": component_name,
                "generation": generation if generation is not None else 0,
                "backup_run_id": backup_run_id,
                "trigger": trigger,
            },
        )

        args = ["apply", "-f", "-"]
        _, stderr, code = await self.kubectl.run_command(args, stdin_input=manifest)

        if code != 0:
            raise RuntimeError(f"Failed to create database backup pod: {stderr}")

    # =========================================================================
    # Restore Methods
    # =========================================================================

    async def restore_database(
        self,
        cluster: str,
        namespace: str,
        reference_name: str,
        target_database_host: str,
        target_database_port: int,
        target_database_name: str,
        target_database_user: str,
        target_database_password: str,
        snapshot_id: str | None = None,
        project_name: str | None = None,
    ) -> DatabaseRestoreResult:
        """
        Restore a PostgreSQL database from a Kopia backup.

        Args:
            cluster: Cluster name where backup was made
            namespace: Namespace for key derivation
            reference_name: Reference name of the database backup
            target_database_host: Target PostgreSQL host
            target_database_port: Target PostgreSQL port
            target_database_name: Target database name
            target_database_user: Target database user
            target_database_password: Target database password
            snapshot_id: Optional specific snapshot ID (defaults to latest)
            project_name: Project name for per-project backup bucket

        Returns:
            DatabaseRestoreResult with operation details
        """
        async with self.lock:
            await self.lock.update_progress(namespace, f"restore-database:{reference_name}")
            return await self._restore_database(
                cluster=cluster,
                namespace=namespace,
                reference_name=reference_name,
                target_database_host=target_database_host,
                target_database_port=target_database_port,
                target_database_name=target_database_name,
                target_database_user=target_database_user,
                target_database_password=target_database_password,
                snapshot_id=snapshot_id,
                project_name=project_name,
            )

    async def _restore_database(
        self,
        cluster: str,
        namespace: str,
        reference_name: str,
        target_database_host: str,
        target_database_port: int,
        target_database_name: str,
        target_database_user: str,
        target_database_password: str,
        snapshot_id: str | None = None,
        project_name: str | None = None,
    ) -> DatabaseRestoreResult:
        """
        Internal: restore a PostgreSQL database (lock must be held).
        """
        start_time = utc_now()
        timestamp = start_time.strftime("%Y%m%d-%H%M%S")

        pod_name = f"db-restore-{reference_name[:20]}-{timestamp}"
        backup_prefix = generate_backup_prefix(cluster, namespace)

        logger.info(f"Starting database restore: {reference_name} -> {target_database_name}@{target_database_host}")

        try:
            # 1. Derive restore key (same as backup key)
            kopia_password = await self._derive_backup_key(namespace)

            # 2. Create restore pod
            logger.info(f"Creating database restore pod {pod_name}")
            await self._create_database_restore_pod(
                namespace=namespace,
                pod_name=pod_name,
                reference_name=reference_name,
                target_database_host=target_database_host,
                target_database_port=target_database_port,
                target_database_name=target_database_name,
                target_database_user=target_database_user,
                target_database_password=target_database_password,
                kopia_password=kopia_password,
                backup_prefix=backup_prefix,
                snapshot_id=snapshot_id,
                project_name=project_name,
                cluster=cluster,
            )

            # 3. Wait for pod completion
            success = await self._wait_for_pod(namespace, pod_name)

            if not success:
                target_unusable = await self._restore_target_was_unusable(namespace, pod_name)
                logs = await self._get_pod_logs(namespace, pod_name)
                logger.error(
                    f"Database restore pod {pod_name} failed (target_unusable={target_unusable}). Full logs:\n{logs}"
                )
                return DatabaseRestoreResult(
                    namespace=namespace,
                    pvc_name=reference_name,
                    success=False,
                    target_database_name=target_database_name,
                    reference_name=reference_name,
                    snapshot_id=snapshot_id,
                    error=f"Restore pod failed. Logs: {logs[-500:] if logs else 'no logs'}",
                    duration_seconds=(utc_now() - start_time).total_seconds(),
                    target_unusable=target_unusable,
                )

            duration = (utc_now() - start_time).total_seconds()
            logger.info(f"Database restore of {reference_name} to {target_database_name} completed in {duration:.1f}s")

            return DatabaseRestoreResult(
                namespace=namespace,
                pvc_name=reference_name,
                success=True,
                target_pvc_name=target_database_name,
                target_database_name=target_database_name,
                reference_name=reference_name,
                snapshot_id=snapshot_id,
                duration_seconds=duration,
            )

        except Exception as e:
            duration = (utc_now() - start_time).total_seconds()
            logger.exception("Database restore of %s failed after %.1fs", reference_name, duration)
            return DatabaseRestoreResult(
                namespace=namespace,
                pvc_name=reference_name,
                success=False,
                target_database_name=target_database_name,
                reference_name=reference_name,
                error=str(e),
                duration_seconds=duration,
            )

        finally:
            # Cleanup restore pod (best effort)
            await self._cleanup_pod(namespace, pod_name)

    async def _create_database_restore_pod(
        self,
        namespace: str,
        pod_name: str,
        reference_name: str,
        target_database_host: str,
        target_database_port: int,
        target_database_name: str,
        target_database_user: str,
        target_database_password: str,
        kopia_password: str,
        backup_prefix: str,
        snapshot_id: str | None = None,
        project_name: str | None = None,
        cluster: str | None = None,
    ) -> None:
        """Create the database restore pod."""
        template_path = os.path.join(self.MANIFESTS_DIR, "restore-database-pod.yaml.jinja")

        with open(template_path) as f:
            template_content = f.read()

        # Get the bucket name (may be per-project based on config)
        bucket_name = self.config.get_bucket_name(project_name, cluster)

        manifest = self._template_manifest(
            template_content,
            {
                "pod_name": pod_name,
                "namespace": namespace,
                "reference_name": reference_name,
                "target_db_host": target_database_host,
                "target_db_port": target_database_port,
                "target_db_name": target_database_name,
                "target_db_user": target_database_user,
                "target_db_password": target_database_password,
                "s3_endpoint": self.config.s3_endpoint,
                "s3_bucket": bucket_name,
                "s3_access_key": self.config.s3_access_key,
                "s3_secret_key": self.config.s3_secret_key,
                "s3_disable_tls": not self.config.s3_use_tls,
                "backup_prefix": backup_prefix,
                "kopia_password": kopia_password,
                "snapshot_id": snapshot_id or "",
                "timeout_seconds": self.config.timeout_seconds,
                "target_unusable_exit_code": RESTORE_TARGET_UNUSABLE_EXIT_CODE,
            },
        )

        args = ["apply", "-f", "-"]
        _, stderr, code = await self.kubectl.run_command(args, stdin_input=manifest)

        if code != 0:
            raise RuntimeError(f"Failed to create database restore pod: {stderr}")

    # =========================================================================
    # List Snapshots
    # =========================================================================

    async def list_database_snapshots(
        self,
        cluster: str,
        namespace: str,
        reference_name: str | None = None,
        project_name: str | None = None,
    ) -> list[SnapshotInfo]:
        """
        List available database snapshots for a namespace.

        Args:
            cluster: Cluster name
            namespace: Namespace name
            reference_name: Optional database reference name to filter by
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

            # Filter by resource_type=database and optionally by reference_name
            snapshots: list[SnapshotInfo] = []
            for ks in kopia_snapshots:
                # Only include database snapshots
                if ks.resource_type != "database":
                    continue

                # Get database reference from tags
                db_ref = ks.tags.get("tag:database") if ks.tags else None

                # Apply filter if specified
                if reference_name and db_ref != reference_name:
                    continue

                snapshots.append(
                    SnapshotInfo(
                        snapshot_id=ks.snapshot_id,
                        pvc_name=db_ref or ks.pvc_name,  # Use database reference as identifier
                        timestamp=ks.timestamp,
                        size_bytes=ks.size_bytes,
                        cluster=ks.cluster,
                        namespace=ks.namespace,
                        project_name=ks.project_name,
                        deployment_name=ks.deployment_name,
                        component_name=ks.component_name,
                        storage_name=db_ref,  # Database reference
                        generation=ks.generation,
                        backup_run_id=ks.backup_run_id,
                        resource_type="database",
                        trigger=ks.trigger,
                        tags=ks.tags,
                    )
                )

            logger.debug(f"Found {len(snapshots)} database snapshots for {namespace}")
            return snapshots

        except Exception as e:
            logger.warning(f"Failed to list database snapshots via Kopia CLI: {e}")
            return []

    async def backup_project_database(
        self,
        project_name: str,
        project_data: dict[str, Any],
        deployment_name: str,
        namespace: str,
        cluster: str,
        component_name: str,
        reference_name: str,
    ) -> DatabaseBackupResult:
        """
        Backup a database for a project deployment with full metadata context.

        This method extracts database connection information from the project file
        and performs the backup with proper metadata.

        Args:
            project_name: Name of the project
            project_data: Project configuration data
            deployment_name: Name of the deployment
            namespace: Kubernetes namespace (with cluster prefix)
            cluster: Cluster name
            component_name: Component name containing the database
            reference_name: Database reference name

        Returns:
            DatabaseBackupResult with operation details
        """
        # TODO: Extract database connection info from project_data
        # For now, this would need to be passed in or discovered from the cluster
        # The actual implementation would need to:
        # 1. Look up the database service in the namespace
        # 2. Get connection details from secrets/configmaps
        # 3. Call backup_database with the extracted info

        logger.warning(
            f"backup_project_database is a placeholder - database connection info extraction not yet implemented. "
            f"Project: {project_name}, Deployment: {deployment_name}, Database: {reference_name}"
        )

        return DatabaseBackupResult(
            namespace=namespace,
            pvc_name=reference_name,
            success=False,
            database_name=reference_name,
            reference_name=reference_name,
            error="Database connection info extraction not yet implemented - use backup_database directly",
            duration_seconds=0,
        )


def create_database_backup_manager(config: BackupConfig | None = None) -> DatabaseBackupManager:
    """
    Create a DatabaseBackupManager instance.

    Args:
        config: Optional BackupConfig. If not provided, uses settings.

    Returns:
        DatabaseBackupManager instance
    """
    return DatabaseBackupManager(config)
