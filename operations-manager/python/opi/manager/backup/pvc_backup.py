"""PVC Backup Manager - Orchestrates PVC backups to external S3 using Kopia."""

import asyncio
import json
import logging
import os
from typing import Any

from opi.core.backup_constants import BACKUP_REPOSITORY_UNUSABLE_EXIT_CODE
from opi.core.config import settings
from opi.manager.backup.base import (
    BackupConfig,
    BackupResult,
    BaseBackupManager,
    RestoreResult,
    SnapshotInfo,
    kopia_backup_identity,
    pod_credentials_secret_name,
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
    """Orchestrates PVC backups to external S3 using Kopia.

    Project-aware entry point: `backup_project_deployment(...)`. Per PVC it takes the lock,
    snapshots the volume, clones it into a temp PVC, and lets a Kopia pod read that clone.
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
        """The lock must be held.

        Args:
            backup_run_id: Groups every PVC of one run (YYYYMMDDHHmmss); required.
        """
        start_time = utc_now()
        timestamp = start_time.strftime("%Y%m%d-%H%M%S")

        snapshot_name = generate_backup_snapshot_name(pvc_name, timestamp)
        clone_pvc_name = generate_backup_clone_pvc_name(pvc_name, timestamp)
        pod_name = generate_backup_pod_name(pvc_name[:20], timestamp)  # Truncate PVC name if too long

        backup_cluster = cluster or settings.CLUSTER_MANAGER

        logger.info(f"Starting backup of {namespace}/{pvc_name}")

        try:
            pvc_info = await self._get_pvc_info(namespace, pvc_name)
            if not pvc_info:
                return BackupResult(
                    namespace=namespace,
                    pvc_name=pvc_name,
                    success=False,
                    error=f"PVC {pvc_name} not found in namespace {namespace}",
                    duration_seconds=(utc_now() - start_time).total_seconds(),
                )

            logger.info(f"Creating VolumeSnapshot {snapshot_name}")
            await self._create_snapshot(namespace, pvc_name, snapshot_name)
            await self._wait_for_snapshot(namespace, snapshot_name)

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

            kopia_password = await self._derive_backup_key(namespace)

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

            success = await self._wait_for_pod(namespace, pod_name)

            if not success:
                logs = await self._get_pod_logs(namespace, pod_name)
                logger.error(f"Backup pod {pod_name} failed. Full logs:\n{logs}")
                error = await self._backup_pod_failure_error(namespace, pod_name, logs)
                return BackupResult(
                    namespace=namespace,
                    pvc_name=pvc_name,
                    success=False,
                    error=error,
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

        Which PVCs to back up comes from the persistent storage in the project file, not
        from the backup.rig.nl/enabled label the namespace-level endpoints rely on. That is
        the same source of truth the database and MinIO backups use.

        Args:
            backup_run_id: Generated when not given.
        """
        from opi.services.project_file_handler import create_project_file_handler
        from opi.utils.naming import generate_pvc_name, generate_unique_name

        await self._ensure_backup_bucket_exists(project_name=project_name, cluster=cluster)

        async with self.lock:
            results: list[BackupResult] = []
            project_file_handler = create_project_file_handler()

            if not backup_run_id:
                backup_run_id = generate_backup_run_id()
            logger.info(f"Starting backup run {backup_run_id} for project {project_name}/{deployment_name}")

            deployment_components = project_file_handler.extract_deployment_components(project_data, deployment_name)

            base_components = {c.get("name"): c for c in project_data.get("components", [])}

            # Build a map of PVC name -> context from the project file.
            # This is the source of truth for which PVCs belong to this deployment.
            pvc_context_map: dict[str, dict[str, str | int | None]] = {}
            for dep_component in deployment_components:
                component_name = dep_component.get("reference", "")
                if not component_name:
                    continue

                from opi.services.project_file_handler import extract_storage_from_component_services

                base_component = base_components.get(component_name, {})
                storages = extract_storage_from_component_services(base_component)

                for idx, storage in enumerate(storages):
                    # Ephemeral storage has no PVC.
                    if storage.get("type") != "persistent":
                        continue

                    storage_name = storage.get("name")
                    if not storage_name:
                        from opi.utils.naming import generate_storage_name

                        mount_path = storage.get("mount-path", "") or storage.get("mount_path", "")
                        storage_name = generate_storage_name(mount_path, idx)

                    unique_name = generate_unique_name(deployment_name, component_name)

                    generation = project_file_handler.get_storage_generation(
                        project_data, deployment_name, component_name, storage_name
                    )

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
        """Create the backup pod, which reads from ``clone_pvc_name`` and not from the PVC itself.

        Args:
            backup_prefix: S3 prefix for the backup (cluster/namespace).
            backup_run_id: Groups every PVC of one run; required.
        """
        template_path = os.path.join(self.MANIFESTS_DIR, "backup-pod.yaml.jinja")

        with open(template_path) as f:
            template_content = f.read()

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
                "service_account_name": self._pod_service_account_name(project_name),
                "pvc_name": pvc_name,
                "clone_pvc_name": clone_pvc_name,
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
                "storage_name": storage_name,
                "pvc_generation": pvc_generation if pvc_generation is not None else 0,
                "backup_run_id": backup_run_id,
                "trigger": trigger,
            },
        )

        await self._apply_pod_with_credentials(
            namespace=namespace,
            pod_name=pod_name,
            manifest=manifest,
            credentials=self._kopia_credentials(kopia_password, bucket_name),
            what="backup",
        )

    async def list_snapshots(
        self,
        cluster: str,
        namespace: str,
        pvc_name: str | None = None,
        project_name: str | None = None,
    ) -> list[SnapshotInfo]:
        """The Kopia snapshots of a namespace, optionally only those of one PVC.

        Returns [] when the repository cannot be reached, which is what the backup and
        restore screens have always shown. A caller that has to tell "could not look"
        from "nothing there" uses list_snapshots_or_raise.
        """
        try:
            return await self.list_snapshots_or_raise(cluster, namespace, pvc_name, project_name)
        # Broad on purpose: this is the existing contract of the read path, which swallowed
        # every failure into an empty list.
        except Exception as e:
            logger.warning(f"Failed to list snapshots via Kopia CLI: {e}")
            return []

    async def list_snapshots_or_raise(
        self,
        cluster: str,
        namespace: str,
        pvc_name: str | None = None,
        project_name: str | None = None,
    ) -> list[SnapshotInfo]:
        """The same listing, but failing loudly instead of returning [].

        Returns [] only when the repository is reachable and holds no matching snapshot.

        Raises:
            KopiaConnectionError: Kopia is unavailable or the repository unreachable
            KopiaExecutionError: the listing itself failed
        """
        # Local import: opi.connectors.kopia imports from this package, so a module-level
        # import is circular.
        from opi.connectors.kopia import KopiaConnectionError, KopiaConnector, KopiaRepositoryConfig

        backup_prefix = generate_backup_prefix(cluster, namespace)
        kopia_password = await self._derive_backup_key(namespace)
        bucket_name = self.config.get_bucket_name(project_name, cluster)

        kopia = KopiaConnector()

        if not KopiaConnector.is_kopia_available:
            raise KopiaConnectionError("Kopia CLI not available, cannot list snapshots")

        repo_config = KopiaRepositoryConfig(
            s3_endpoint=self.config.s3_endpoint,
            s3_bucket=bucket_name,
            s3_access_key=self.config.s3_access_key,
            s3_secret_key=self.config.s3_secret_key,
            s3_prefix=backup_prefix,
            password=kopia_password,
            use_tls=self.config.s3_use_tls,
        )

        kopia_snapshots = await kopia.list_snapshots(repo_config)

        snapshots: list[SnapshotInfo] = []
        for ks in kopia_snapshots:
            # Both read endpoints (backup runs and the snapshot listing) end up here,
            # so this is the one place that decides which name a caller gets to see.
            # It has to be the name the restore route accepts, see restore_reference.
            snapshot_pvc_name = ks.restore_reference

            if pvc_name and snapshot_pvc_name != pvc_name:
                continue

            snapshots.append(
                SnapshotInfo(
                    snapshot_id=ks.snapshot_id,
                    pvc_name=snapshot_pvc_name,
                    timestamp=ks.timestamp,
                    size_bytes=ks.size_bytes,
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
                    tags=ks.tags,
                )
            )

        logger.debug(f"Found {len(snapshots)} snapshots for {namespace}")
        return snapshots

    async def delete_snapshots(
        self,
        cluster: str,
        namespace: str,
        snapshot_ids: list[str],
        project_name: str | None = None,
    ) -> dict[str, bool]:
        """Delete snapshots over one connection to the namespace's repository.

        Returns per snapshot id whether the delete succeeded.
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
        snapshots: list[SnapshotInfo] = []
        try:
            for line in logs.split("\n"):
                if line.strip().startswith("{") and "snapshot_id" in line:
                    data = json.loads(line)
                    pvc_name = data.get("pvc_name", "")

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
        project_name: str | None = None,
    ) -> RestoreResult:
        """Restore a PVC from a Kopia backup.

        Args:
            snapshot_id: A specific snapshot; the latest one without it.
            target_pvc_name: Defaults to {pvc_name}-restored-{timestamp}.
            storage_size: Required when the target PVC has to be created.
            overwrite: Allow restoring into an existing PVC.
        """
        # Ensure the bucket and its scoped S3 account exist before the restore pod needs them
        await self._ensure_backup_bucket_exists(project_name=project_name, cluster=cluster)

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
                project_name=project_name,
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
        project_name: str | None = None,
    ) -> RestoreResult:
        """The lock must be held."""
        start_time = utc_now()
        timestamp = start_time.strftime("%Y%m%d-%H%M%S")

        if not target_pvc_name:
            target_pvc_name = generate_restored_pvc_name(pvc_name, timestamp)

        pod_name = generate_restore_pod_name(pvc_name[:20], timestamp)
        backup_prefix = generate_backup_prefix(cluster, namespace)

        logger.info(f"Starting restore of {pvc_name} to {target_pvc_name} in {namespace}")

        try:
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

            if not existing_pvc:
                logger.info(f"Creating target PVC {target_pvc_name}")
                await self._create_restore_pvc(
                    namespace=namespace,
                    pvc_name=target_pvc_name,
                    size=storage_size,
                    storage_class=storage_class,
                )
                await self._wait_for_pvc(namespace, target_pvc_name)

            kopia_password = await self._derive_backup_key(namespace)

            logger.info(f"Creating restore pod {pod_name}")
            await self._create_restore_pod(
                namespace=namespace,
                pvc_name=pvc_name,
                target_pvc_name=target_pvc_name,
                pod_name=pod_name,
                kopia_password=kopia_password,
                backup_prefix=backup_prefix,
                snapshot_id=snapshot_id,
                project_name=project_name,
                cluster=cluster,
            )

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
            await self._cleanup_pod(namespace, pod_name)

    async def _create_restore_pvc(
        self,
        namespace: str,
        pvc_name: str,
        size: str,
        storage_class: str | None = None,
    ) -> None:
        template_path = os.path.join(self.MANIFESTS_DIR, "restore-pvc.yaml.jinja")

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
        """Restore into a PVC that ArgoCD will manage, created under the exact name given.

        Updating the project file and triggering the refresh is up to the caller.

        Args:
            target_pvc_name: Exact name for the new PVC (e.g. webapp-data-pvc-v3).
            access_modes: Defaults to ["ReadWriteOnce"].
            snapshot_id: A specific snapshot; the latest one without it.
        """
        # Ensure the bucket and its scoped S3 account exist before the restore pod needs them
        await self._ensure_backup_bucket_exists(project_name=project_name, cluster=cluster)

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
        """The lock must be held."""
        start_time = utc_now()
        timestamp = start_time.strftime("%Y%m%d-%H%M%S")
        pod_name = generate_restore_pod_name(source_pvc_name[:20], timestamp)
        backup_prefix = generate_backup_prefix(cluster, namespace)

        logger.info(f"Starting project restore: {source_pvc_name} -> {target_pvc_name} in {namespace}")

        try:
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

            kopia_password = await self._derive_backup_key(namespace)

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

            success = await self._wait_for_pod(namespace, pod_name)

            if not success:
                logs = await self._get_pod_logs(namespace, pod_name)
                logger.error(f"Restore pod {pod_name} failed. Full logs:\n{logs}")
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
        template_path = os.path.join(self.MANIFESTS_DIR, "restore-pod.yaml.jinja")

        if not os.path.exists(template_path):
            raise RuntimeError("restore-pod.yaml.jinja template not found")

        with open(template_path) as f:
            template_content = f.read()

        bucket_name = self.config.get_bucket_name(project_name, cluster)

        manifest = self._template_manifest(
            template_content,
            {
                "pod_name": pod_name,
                "namespace": namespace,
                "service_account_name": self._pod_service_account_name(project_name),
                "pvc_name": pvc_name,
                "target_pvc_name": target_pvc_name,
                "s3_endpoint": self.config.s3_endpoint,
                "s3_bucket": bucket_name,
                "s3_disable_tls": not self.config.s3_use_tls,
                "backup_prefix": backup_prefix,
                "snapshot_id": snapshot_id or "",
                "credentials_secret": pod_credentials_secret_name(pod_name),
                "timeout_seconds": self.config.timeout_seconds,
                "cluster": cluster or "",
            },
        )

        await self._apply_pod_with_credentials(
            namespace=namespace,
            pod_name=pod_name,
            manifest=manifest,
            credentials=self._kopia_credentials(kopia_password, bucket_name),
            what="restore",
        )

    async def _cleanup(self, namespace: str, pod_name: str, clone_pvc_name: str, snapshot_name: str) -> None:
        """Best effort: a leftover pod, clone PVC or snapshot does not fail the backup."""
        logger.info(f"Cleaning up backup resources in {namespace}")

        await self._cleanup_pod(namespace, pod_name)

        try:
            args = ["delete", "pvc", clone_pvc_name, "-n", namespace, "--ignore-not-found=true"]
            await self.kubectl.run_command(args)
            logger.debug(f"Deleted clone PVC {clone_pvc_name}")
        except Exception as e:
            logger.warning(f"Failed to delete clone PVC {clone_pvc_name}: {e}")

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
    """Without a ``config`` the manager is built from the settings."""
    return PVCBackupManager(config)
