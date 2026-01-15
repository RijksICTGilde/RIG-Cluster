"""PVC Backup Manager - Orchestrates backups to external S3 using Kopia."""

import asyncio
import base64
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

from opi.connectors.kubectl import KubectlConnector
from opi.connectors.minio_mc import create_minio_connector
from opi.core.cluster_config import get_volume_snapshot_class
from opi.core.config import settings
from opi.utils.naming import (
    generate_backup_clone_pvc_name,
    generate_backup_pod_name,
    generate_backup_prefix,
    generate_backup_snapshot_name,
    generate_restore_pod_name,
    generate_restored_pvc_name,
)

logger = logging.getLogger(__name__)


def _now() -> datetime:
    """Get current UTC datetime."""
    return datetime.now(UTC)


@dataclass
class BackupResult:
    """Result of a backup operation."""

    namespace: str
    pvc_name: str
    success: bool
    snapshot_name: str | None = None
    error: str | None = None
    duration_seconds: float = 0


@dataclass
class BackupStatus:
    """Current backup status."""

    lock_held: bool = False
    current_namespace: str | None = None
    current_pvc: str | None = None
    locked_by: str | None = None
    locked_at: str | None = None


@dataclass
class SnapshotInfo:
    """Information about a Kopia snapshot."""

    snapshot_id: str
    pvc_name: str
    timestamp: str
    size_bytes: int | None = None
    # Extended metadata
    cluster: str | None = None
    namespace: str | None = None
    project_name: str | None = None
    deployment_name: str | None = None
    component_name: str | None = None
    storage_name: str | None = None
    generation: int | None = None
    # Raw tags for debugging
    tags: dict[str, str] | None = None


@dataclass
class RestoreResult:
    """Result of a restore operation."""

    namespace: str
    pvc_name: str
    success: bool
    target_pvc_name: str | None = None
    snapshot_id: str | None = None
    error: str | None = None
    duration_seconds: float = 0


class BackupLock:
    """
    Distributed lock using Kubernetes ConfigMap.

    Ensures only one backup runs at a time across all instances.
    """

    LOCK_NAME = "backup-lock"
    LOCK_NAMESPACE = "rig-system"

    def __init__(self, kubectl: KubectlConnector) -> None:
        self.kubectl = kubectl
        self._held = False

    async def acquire(self, timeout_seconds: int = 3600) -> bool:
        """
        Acquire the backup lock.

        Args:
            timeout_seconds: Consider lock stale after this many seconds

        Returns:
            True if lock acquired, False if already held by another process
        """
        try:
            # Try to get existing lock
            args = ["get", "configmap", self.LOCK_NAME, "-n", self.LOCK_NAMESPACE, "-o", "json"]
            stdout, stderr, code = await self.kubectl.run_command(args)

            if code == 0:
                # Lock exists, check if stale
                lock_data = json.loads(stdout)
                data = lock_data.get("data", {})
                locked_at_str = data.get("locked_at", "")

                if locked_at_str:
                    locked_at = datetime.fromisoformat(locked_at_str)
                    age_seconds = (_now() - locked_at).total_seconds()

                    if age_seconds < timeout_seconds:
                        # Lock is fresh, cannot acquire
                        logger.warning(
                            f"Backup lock held by {data.get('locked_by', 'unknown')} "
                            f"since {locked_at_str} ({age_seconds:.0f}s ago)"
                        )
                        return False
                    else:
                        # Lock is stale, take over
                        logger.warning(
                            f"Taking over stale backup lock (held for {age_seconds:.0f}s, "
                            f"timeout is {timeout_seconds}s)"
                        )

            # Create or update lock
            lock_content = {
                "locked_at": _now().isoformat(),
                "locked_by": os.environ.get("HOSTNAME", "unknown"),
            }

            # Use kubectl apply with configmap
            configmap_yaml = f"""apiVersion: v1
kind: ConfigMap
metadata:
  name: {self.LOCK_NAME}
  namespace: {self.LOCK_NAMESPACE}
data:
  locked_at: "{lock_content["locked_at"]}"
  locked_by: "{lock_content["locked_by"]}"
"""
            args = ["apply", "-f", "-"]
            _, stderr, code = await self.kubectl.run_command(args, stdin_input=configmap_yaml)

            if code == 0:
                self._held = True
                logger.info(f"Backup lock acquired by {lock_content['locked_by']}")
                return True
            else:
                logger.error(f"Failed to create backup lock: {stderr}")
                return False

        except Exception:
            logger.exception("Error acquiring backup lock")
            return False

    async def release(self) -> bool:
        """
        Release the backup lock.

        Returns:
            True if released successfully, False otherwise
        """
        if not self._held:
            return True

        try:
            args = ["delete", "configmap", self.LOCK_NAME, "-n", self.LOCK_NAMESPACE, "--ignore-not-found=true"]
            _, stderr, code = await self.kubectl.run_command(args)

            if code == 0:
                self._held = False
                logger.info("Backup lock released")
                return True
            else:
                logger.error(f"Failed to release backup lock: {stderr}")
                return False

        except Exception:
            logger.exception("Error releasing backup lock")
            return False

    async def get_status(self) -> BackupStatus:
        """Get current lock status."""
        try:
            args = ["get", "configmap", self.LOCK_NAME, "-n", self.LOCK_NAMESPACE, "-o", "json"]
            stdout, _, code = await self.kubectl.run_command(args)

            if code == 0:
                lock_data = json.loads(stdout)
                data = lock_data.get("data", {})
                return BackupStatus(
                    lock_held=True,
                    locked_by=data.get("locked_by"),
                    locked_at=data.get("locked_at"),
                    current_namespace=data.get("current_namespace"),
                    current_pvc=data.get("current_pvc"),
                )
            else:
                return BackupStatus(lock_held=False)

        except Exception:
            logger.exception("Error getting backup lock status")
            return BackupStatus(lock_held=False)

    async def update_progress(self, namespace: str, pvc_name: str) -> None:
        """Update lock with current progress."""
        if not self._held:
            return

        try:
            configmap_yaml = f"""apiVersion: v1
kind: ConfigMap
metadata:
  name: {self.LOCK_NAME}
  namespace: {self.LOCK_NAMESPACE}
data:
  locked_at: "{_now().isoformat()}"
  locked_by: "{os.environ.get("HOSTNAME", "unknown")}"
  current_namespace: "{namespace}"
  current_pvc: "{pvc_name}"
"""
            args = ["apply", "-f", "-"]
            await self.kubectl.run_command(args, stdin_input=configmap_yaml)
        except Exception as e:
            logger.warning(f"Failed to update backup progress: {e}")

    async def __aenter__(self) -> "BackupLock":
        if not await self.acquire():
            raise RuntimeError("Could not acquire backup lock - another backup is running")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.release()


@dataclass
class BackupConfig:
    """Configuration for backup operations."""

    s3_endpoint: str
    s3_bucket: str
    s3_access_key: str
    s3_secret_key: str
    s3_use_tls: bool = False
    snapshot_class: str = "ocs-storagecluster-rbdplugin-snapclass"
    timeout_seconds: int = 3600
    retention_keep_latest: int = 7
    retention_keep_daily: int = 7
    retention_keep_weekly: int = 4

    @classmethod
    def from_settings(cls) -> "BackupConfig":
        """Create BackupConfig from application settings."""
        # Get snapshot class from cluster config, fall back to settings if not configured
        snapshot_class = get_volume_snapshot_class(settings.CLUSTER_MANAGER) or settings.BACKUP_SNAPSHOT_CLASS
        return cls(
            s3_endpoint=settings.BACKUP_S3_ENDPOINT,
            s3_bucket=settings.BACKUP_S3_BUCKET,
            s3_access_key=settings.BACKUP_S3_ACCESS_KEY,
            s3_secret_key=settings.BACKUP_S3_SECRET_KEY,
            s3_use_tls=settings.BACKUP_S3_USE_TLS,
            snapshot_class=snapshot_class,
            timeout_seconds=settings.BACKUP_TIMEOUT_SECONDS,
            retention_keep_latest=settings.BACKUP_RETENTION_KEEP_LATEST,
            retention_keep_daily=settings.BACKUP_RETENTION_KEEP_DAILY,
            retention_keep_weekly=settings.BACKUP_RETENTION_KEEP_WEEKLY,
        )


class BackupManager:
    """
    Orchestrates PVC backups to external S3 using Kopia.

    Flow:
    1. Acquire distributed lock
    2. For each PVC with backup label:
       a. Create VolumeSnapshot
       b. Create temp PVC from snapshot
       c. Derive backup key from namespace's SOPS age key
       d. Spawn backup Pod
       e. Wait for completion
       f. Cleanup resources
    3. Release lock
    """

    BACKUP_LABEL = "backup.rig.nl/enabled"
    MANIFESTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "manifests")

    def __init__(self, config: BackupConfig | None = None) -> None:
        self.kubectl = KubectlConnector()
        self.config = config or BackupConfig.from_settings()
        self.lock = BackupLock(self.kubectl)

    async def get_status(self) -> BackupStatus:
        """Get current backup status."""
        return await self.lock.get_status()

    async def _ensure_backup_bucket_exists(self) -> None:
        """
        Ensure the backup S3 bucket exists, creating it if necessary.

        Uses the minio connector to check and create the bucket on the
        backup destination minio instance.
        """
        minio_connector = create_minio_connector()

        # Configure alias for backup destination minio
        # Endpoint format: "host:port" (e.g., "minio.rig-backup-destination.svc:9000")
        endpoint = self.config.s3_endpoint
        host_with_port = endpoint if ":" in endpoint else f"{endpoint}:9000"

        alias_name = "backup-destination"
        alias_configured = await minio_connector.configure_alias(
            alias=alias_name,
            host=host_with_port,
            access_key=self.config.s3_access_key,
            secret_key=self.config.s3_secret_key,
            secure=False,  # Internal cluster communication
        )

        if not alias_configured:
            raise RuntimeError(f"Failed to configure MinIO alias for backup destination: {host_with_port}")

        # Check if bucket exists and create if needed
        bucket_result = await minio_connector.create_bucket(alias_name, self.config.s3_bucket)

        if bucket_result["status"] == "created":
            logger.info(f"Created backup bucket: {self.config.s3_bucket}")
        elif bucket_result["status"] == "exists":
            logger.debug(f"Backup bucket already exists: {self.config.s3_bucket}")
        else:
            raise RuntimeError(
                f"Failed to ensure backup bucket exists: {bucket_result.get('message', 'Unknown error')}"
            )

    async def backup_namespace(self, namespace: str) -> list[BackupResult]:
        """
        Backup all labeled PVCs in a namespace.

        Args:
            namespace: Namespace to backup

        Returns:
            List of BackupResult for each PVC
        """
        # Ensure backup bucket exists before starting
        await self._ensure_backup_bucket_exists()

        async with self.lock:
            pvcs = await self._get_backup_pvcs(namespace)
            results: list[BackupResult] = []

            if not pvcs:
                logger.info(f"No PVCs with backup label found in namespace {namespace}")
                return results

            logger.info(f"Found {len(pvcs)} PVC(s) to backup in namespace {namespace}")

            for pvc_name in pvcs:
                await self.lock.update_progress(namespace, pvc_name)
                result = await self._backup_pvc(namespace, pvc_name)
                results.append(result)

            return results

    async def backup_pvc(
        self,
        namespace: str,
        pvc_name: str,
        cluster: str | None = None,
        project_name: str | None = None,
        deployment_name: str | None = None,
        component_name: str | None = None,
        storage_name: str | None = None,
        pvc_generation: int | None = None,
    ) -> BackupResult:
        """
        Backup a single PVC.

        Args:
            namespace: Namespace containing the PVC
            pvc_name: Name of the PVC to backup
            cluster: Cluster name for metadata
            project_name: Project name for metadata
            deployment_name: Deployment name for metadata
            component_name: Component name for metadata
            storage_name: Storage name for metadata
            pvc_generation: PVC generation number for metadata

        Returns:
            BackupResult with operation details
        """
        # Ensure backup bucket exists before starting
        await self._ensure_backup_bucket_exists()

        async with self.lock:
            await self.lock.update_progress(namespace, pvc_name)
            return await self._backup_pvc(
                namespace,
                pvc_name,
                cluster=cluster,
                project_name=project_name,
                deployment_name=deployment_name,
                component_name=component_name,
                storage_name=storage_name,
                pvc_generation=pvc_generation,
            )

    async def _backup_pvc(
        self,
        namespace: str,
        pvc_name: str,
        cluster: str | None = None,
        project_name: str | None = None,
        deployment_name: str | None = None,
        component_name: str | None = None,
        storage_name: str | None = None,
        pvc_generation: int | None = None,
    ) -> BackupResult:
        """
        Internal: backup a single PVC (lock must be held).

        Args:
            namespace: Namespace containing the PVC
            pvc_name: Name of the PVC to backup
            cluster: Cluster name for backup prefix (defaults to settings.CLUSTER)
            project_name: Project name for metadata
            deployment_name: Deployment name for metadata
            component_name: Component name for metadata
            storage_name: Storage name for metadata
            pvc_generation: PVC generation number for metadata

        Returns:
            BackupResult with operation details
        """
        start_time = _now()
        timestamp = start_time.strftime("%Y%m%d-%H%M%S")

        # Use naming utilities for consistent naming
        snapshot_name = generate_backup_snapshot_name(pvc_name, timestamp)
        clone_pvc_name = generate_backup_clone_pvc_name(pvc_name, timestamp)
        pod_name = generate_backup_pod_name(pvc_name[:20], timestamp)  # Truncate PVC name if too long

        # Determine cluster for backup prefix
        backup_cluster = cluster or settings.CLUSTER_MANAGER

        logger.info(f"Starting backup of {namespace}/{pvc_name}")

        try:
            # 1. Get PVC details (size, storage class)
            pvc_info = await self._get_pvc_info(namespace, pvc_name)
            if not pvc_info:
                return BackupResult(
                    namespace=namespace,
                    pvc_name=pvc_name,
                    success=False,
                    error=f"PVC {pvc_name} not found in namespace {namespace}",
                    duration_seconds=(_now() - start_time).total_seconds(),
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
                    duration_seconds=(_now() - start_time).total_seconds(),
                )

            duration = (_now() - start_time).total_seconds()
            logger.info(f"Backup of {namespace}/{pvc_name} completed successfully in {duration:.1f}s")

            return BackupResult(
                namespace=namespace,
                pvc_name=pvc_name,
                success=True,
                snapshot_name=snapshot_name,
                duration_seconds=duration,
            )

        except Exception as e:
            duration = (_now() - start_time).total_seconds()
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

    async def _get_backup_pvcs(self, namespace: str) -> list[str]:
        """Get all PVCs with backup label in namespace."""
        args = [
            "get",
            "pvc",
            "-n",
            namespace,
            "-l",
            f"{self.BACKUP_LABEL}=true",
            "-o",
            "jsonpath={.items[*].metadata.name}",
        ]
        stdout, stderr, code = await self.kubectl.run_command(args)

        if code != 0:
            logger.error(f"Failed to list PVCs in {namespace}: {stderr}")
            return []

        pvc_names = stdout.strip().split() if stdout.strip() else []
        return pvc_names

    async def _get_all_pvcs(self, namespace: str) -> list[str]:
        """Get all PVCs in namespace (regardless of labels)."""
        args = [
            "get",
            "pvc",
            "-n",
            namespace,
            "-o",
            "jsonpath={.items[*].metadata.name}",
        ]
        stdout, stderr, code = await self.kubectl.run_command(args)

        if code != 0:
            logger.error(f"Failed to list PVCs in {namespace}: {stderr}")
            return []

        pvc_names = stdout.strip().split() if stdout.strip() else []
        return pvc_names

    async def backup_namespace_all(self, namespace: str) -> list[BackupResult]:
        """
        Backup ALL PVCs in a namespace (no labels required).

        This is useful for Helm charts or externally managed deployments
        where you can't add labels to PVCs.

        Args:
            namespace: Namespace to backup

        Returns:
            List of BackupResult for each PVC
        """
        async with self.lock:
            pvcs = await self._get_all_pvcs(namespace)
            results: list[BackupResult] = []

            if not pvcs:
                logger.info(f"No PVCs found in namespace {namespace}")
                return results

            logger.info(f"Found {len(pvcs)} PVC(s) to backup in namespace {namespace} (all mode)")

            for pvc_name in pvcs:
                await self.lock.update_progress(namespace, pvc_name)
                result = await self._backup_pvc(namespace, pvc_name)
                results.append(result)

            return results

    async def backup_project_deployment(
        self,
        project_name: str,
        project_data: dict,
        deployment_name: str,
        namespace: str,
        cluster: str,
        all_pvcs: bool = False,
    ) -> list[BackupResult]:
        """
        Backup PVCs for a project deployment with full metadata context.

        This method extracts storage definitions from the project file and
        backs up each PVC with proper metadata (project, deployment, component,
        storage name, generation).

        Args:
            project_name: Name of the project
            project_data: Project configuration data
            deployment_name: Name of the deployment
            namespace: Kubernetes namespace (with cluster prefix)
            cluster: Cluster name
            all_pvcs: If True, backup all PVCs; if False, only labeled ones

        Returns:
            List of BackupResult for each PVC
        """
        from opi.handlers.project_file_handler import create_project_file_handler
        from opi.utils.naming import generate_pvc_name, generate_unique_name

        # Ensure backup bucket exists before starting
        await self._ensure_backup_bucket_exists()

        async with self.lock:
            results: list[BackupResult] = []
            project_file_handler = create_project_file_handler()

            # Get PVCs to backup
            if all_pvcs:
                pvc_names = await self._get_all_pvcs(namespace)
            else:
                pvc_names = await self._get_backup_pvcs(namespace)

            if not pvc_names:
                logger.info(f"No PVCs found in namespace {namespace}")
                return results

            logger.info(f"Found {len(pvc_names)} PVC(s) to backup in {namespace} for project {project_name}")

            # Extract component references from deployment
            deployment_components = project_file_handler.extract_deployment_components(project_data, deployment_name)

            # Get base components for storage definitions
            base_components = {c.get("name"): c for c in project_data.get("components", [])}

            # Build a map of PVC name -> context for quick lookup
            pvc_context_map: dict[str, dict[str, str | int | None]] = {}
            for dep_component in deployment_components:
                # Deployment components use "reference" to point to base component
                component_name = dep_component.get("reference", "")
                if not component_name:
                    continue

                # Get storage definitions from the BASE component
                base_component = base_components.get(component_name, {})
                storages = base_component.get("storage", [])

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

                    logger.debug(
                        f"PVC mapping: {expected_pvc_name} -> component={component_name}, "
                        f"storage={storage_name}, generation={generation}"
                    )

                    pvc_context_map[expected_pvc_name] = {
                        "component_name": component_name,
                        "storage_name": storage_name,
                        "generation": generation,
                    }

            # Backup each PVC with context
            for pvc_name in pvc_names:
                await self.lock.update_progress(namespace, pvc_name)

                # Look up context for this PVC
                context = pvc_context_map.get(pvc_name, {})
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
                )
                results.append(result)

            return results

    async def _get_pvc_info(self, namespace: str, pvc_name: str) -> dict[str, str] | None:
        """Get PVC size and storage class."""
        args = ["get", "pvc", pvc_name, "-n", namespace, "-o", "json"]
        stdout, stderr, code = await self.kubectl.run_command(args)

        if code != 0:
            logger.error(f"Failed to get PVC {pvc_name}: {stderr}")
            return None

        pvc_data = json.loads(stdout)
        spec = pvc_data.get("spec", {})
        status = pvc_data.get("status", {})

        # Get actual size from status if available, otherwise from spec
        size = status.get("capacity", {}).get("storage") or spec.get("resources", {}).get("requests", {}).get(
            "storage", "1Gi"
        )

        return {
            "size": size,
            "storage_class": spec.get("storageClassName", ""),
        }

    async def _derive_backup_key(self, namespace: str) -> str:
        """
        Derive Kopia password from namespace's SOPS age key.

        Uses HKDF-like derivation to create a backup-specific password
        from the project's SOPS key.
        """
        # Get the SOPS age secret from the namespace
        age_key = await self.kubectl.get_sops_secret_from_namespace(namespace)

        if not age_key:
            # Fallback to a namespace-based key if no SOPS key found
            logger.warning(f"No SOPS key found in namespace {namespace}, using namespace-based key")
            age_key = f"fallback-key-{namespace}"

        # Derive a backup-specific password
        material = f"kopia-backup-{namespace}-{age_key}".encode()
        derived = hashlib.sha256(material).digest()
        return base64.b64encode(derived).decode()[:32]

    async def _create_snapshot(self, namespace: str, pvc_name: str, snapshot_name: str) -> None:
        """Create a VolumeSnapshot from a PVC."""
        template_path = os.path.join(self.MANIFESTS_DIR, "backup-snapshot.yaml.jinja")

        with open(template_path) as f:
            template_content = f.read()

        manifest = self.kubectl.template_manifest(
            template_content,
            {
                "snapshot_name": snapshot_name,
                "namespace": namespace,
                "pvc_name": pvc_name,
                "snapshot_class": self.config.snapshot_class,
                "timestamp": _now().strftime("%Y%m%d-%H%M%S"),
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

        manifest = self.kubectl.template_manifest(
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

    async def _wait_for_pvc(self, namespace: str, pvc_name: str, timeout: int = 300) -> None:
        """Wait for PVC to be bound."""
        logger.info(f"Waiting for PVC {pvc_name} to be bound...")

        start_time = asyncio.get_event_loop().time()

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                raise RuntimeError(f"Timeout waiting for PVC {pvc_name} after {timeout}s")

            args = ["get", "pvc", pvc_name, "-n", namespace, "-o", "jsonpath={.status.phase}"]
            stdout, _, code = await self.kubectl.run_command(args)

            if code == 0 and stdout.strip().lower() == "bound":
                logger.info(f"PVC {pvc_name} is bound")
                return

            await asyncio.sleep(5)

    async def _create_backup_pod(
        self,
        namespace: str,
        pvc_name: str,
        clone_pvc_name: str,
        pod_name: str,
        kopia_password: str,
        timestamp: str,
        backup_prefix: str,
        cluster: str | None = None,
        project_name: str | None = None,
        deployment_name: str | None = None,
        component_name: str | None = None,
        storage_name: str | None = None,
        pvc_generation: int | None = None,
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

        manifest = self.kubectl.template_manifest(
            template_content,
            {
                "pod_name": pod_name,
                "namespace": namespace,
                "pvc_name": pvc_name,
                "clone_pvc_name": clone_pvc_name,
                "timestamp": timestamp,
                "s3_endpoint": self.config.s3_endpoint,
                "s3_bucket": self.config.s3_bucket,
                "s3_access_key": self.config.s3_access_key,
                "s3_secret_key": self.config.s3_secret_key,
                "s3_disable_tls": not self.config.s3_use_tls,
                "backup_prefix": backup_prefix,
                "kopia_password": kopia_password,
                "timeout_seconds": self.config.timeout_seconds,
                "retention_keep_latest": self.config.retention_keep_latest,
                "retention_keep_daily": self.config.retention_keep_daily,
                "retention_keep_weekly": self.config.retention_keep_weekly,
                # Project context metadata
                "cluster": cluster or settings.CLUSTER_MANAGER,
                "project_name": project_name,
                "deployment_name": deployment_name,
                "component_name": component_name,
                "storage_name": storage_name,
                "pvc_generation": pvc_generation if pvc_generation is not None else 0,
            },
        )

        args = ["apply", "-f", "-"]
        _, stderr, code = await self.kubectl.run_command(args, stdin_input=manifest)

        if code != 0:
            raise RuntimeError(f"Failed to create backup pod: {stderr}")

    async def _wait_for_pod(self, namespace: str, pod_name: str, timeout: int | None = None) -> bool:
        """
        Wait for pod to complete.

        Returns:
            True if pod completed successfully, False if failed
        """
        timeout = timeout or self.config.timeout_seconds
        logger.info(f"Waiting for backup pod {pod_name} to complete (timeout: {timeout}s)...")

        start_time = asyncio.get_event_loop().time()

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                logger.error(f"Timeout waiting for pod {pod_name} after {timeout}s")
                return False

            args = ["get", "pod", pod_name, "-n", namespace, "-o", "jsonpath={.status.phase}"]
            stdout, stderr, code = await self.kubectl.run_command(args)

            if code != 0:
                logger.warning(f"Failed to get pod status: {stderr}")
                await asyncio.sleep(10)
                continue

            phase = stdout.strip().lower()

            if phase == "succeeded":
                logger.info(f"Backup pod {pod_name} completed successfully")
                return True
            elif phase == "failed":
                logger.error(f"Backup pod {pod_name} failed")
                return False
            elif phase in ("pending", "running"):
                logger.debug(f"Backup pod {pod_name} is {phase}")
            else:
                logger.warning(f"Unexpected pod phase: {phase}")

            await asyncio.sleep(10)

    async def _get_pod_logs(self, namespace: str, pod_name: str) -> str:
        """Get logs from a pod."""
        args = ["logs", pod_name, "-n", namespace, "--tail=100"]
        stdout, stderr, code = await self.kubectl.run_command(args)

        if code != 0:
            return f"Failed to get logs: {stderr}"

        return stdout

    # =========================================================================
    # Restore Methods
    # =========================================================================

    async def list_snapshots(self, cluster: str, namespace: str, pvc_name: str | None = None) -> list[SnapshotInfo]:
        """
        List available Kopia snapshots for a namespace/PVC.

        Uses the Kopia CLI directly to query the repository, which is much faster
        than spawning a Kubernetes pod.

        Args:
            cluster: Cluster name
            namespace: Namespace name
            pvc_name: Optional PVC name to filter by

        Returns:
            List of available snapshots
        """
        from opi.connectors.kopia import KopiaConnector, KopiaRepositoryConfig

        backup_prefix = generate_backup_prefix(cluster, namespace)
        kopia_password = await self._derive_backup_key(namespace)

        # Create Kopia connector and query repository directly
        kopia = KopiaConnector()

        if not KopiaConnector.is_kopia_available:
            logger.warning("Kopia CLI not available, cannot list snapshots")
            return []

        repo_config = KopiaRepositoryConfig(
            s3_endpoint=self.config.s3_endpoint,
            s3_bucket=self.config.s3_bucket,
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
                snapshot_pvc_name = ks.pvc_name

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
                        # Raw tags for debugging
                        tags=ks.tags,
                    )
                )

            logger.debug(f"Found {len(snapshots)} snapshots for {namespace}")
            return snapshots

        except Exception as e:
            logger.warning(f"Failed to list snapshots via Kopia CLI: {e}")
            return []

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
        start_time = _now()
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
                    duration_seconds=(_now() - start_time).total_seconds(),
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
                    duration_seconds=(_now() - start_time).total_seconds(),
                )

            duration = (_now() - start_time).total_seconds()
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
            duration = (_now() - start_time).total_seconds()
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
            try:
                args = ["delete", "pod", pod_name, "-n", namespace, "--ignore-not-found=true"]
                await self.kubectl.run_command(args)
            except Exception as e:
                logger.warning(f"Failed to delete restore pod {pod_name}: {e}")

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

        manifest = self.kubectl.template_manifest(
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
    ) -> RestoreResult:
        """
        Internal: restore to a project-managed PVC (lock must be held).
        """
        start_time = _now()
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
                    duration_seconds=(_now() - start_time).total_seconds(),
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
                    duration_seconds=(_now() - start_time).total_seconds(),
                )

            duration = (_now() - start_time).total_seconds()
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
            duration = (_now() - start_time).total_seconds()
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
            try:
                args = ["delete", "pod", pod_name, "-n", namespace, "--ignore-not-found=true"]
                await self.kubectl.run_command(args)
            except Exception as e:
                logger.warning(f"Failed to delete restore pod {pod_name}: {e}")

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

        manifest = self.kubectl.template_manifest(
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
    ) -> None:
        """Create the restore pod."""
        template_path = os.path.join(self.MANIFESTS_DIR, "restore-pod.yaml.jinja")

        if not os.path.exists(template_path):
            raise RuntimeError("restore-pod.yaml.jinja template not found")

        with open(template_path) as f:
            template_content = f.read()

        manifest = self.kubectl.template_manifest(
            template_content,
            {
                "pod_name": pod_name,
                "namespace": namespace,
                "pvc_name": pvc_name,
                "target_pvc_name": target_pvc_name,
                "s3_endpoint": self.config.s3_endpoint,
                "s3_bucket": self.config.s3_bucket,
                "s3_access_key": self.config.s3_access_key,
                "s3_secret_key": self.config.s3_secret_key,
                "s3_disable_tls": not self.config.s3_use_tls,
                "backup_prefix": backup_prefix,
                "kopia_password": kopia_password,
                "snapshot_id": snapshot_id or "",
                "timeout_seconds": self.config.timeout_seconds,
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
        try:
            args = ["delete", "pod", pod_name, "-n", namespace, "--ignore-not-found=true"]
            await self.kubectl.run_command(args)
            logger.debug(f"Deleted pod {pod_name}")
        except Exception as e:
            logger.warning(f"Failed to delete pod {pod_name}: {e}")

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


def create_backup_manager(config: BackupConfig | None = None) -> BackupManager:
    """
    Create a BackupManager instance.

    Args:
        config: Optional BackupConfig. If not provided, uses settings.

    Returns:
        BackupManager instance
    """
    return BackupManager(config)
