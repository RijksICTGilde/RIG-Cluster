"""Base backup functionality - shared across all backup types."""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import yaml

from opi.connectors.kubectl import KubectlConnector
from opi.connectors.minio_mc import create_minio_connector
from opi.core.cluster_config import get_volume_snapshot_class
from opi.core.config import settings
from opi.extensions.pipeline import load_extensions

if TYPE_CHECKING:
    from types import TracebackType

logger = logging.getLogger(__name__)


class ResourceType(StrEnum):
    """Types of resources that can be backed up and restored."""

    PVC = "pvc"
    DATABASE = "database"
    BUCKET = "bucket"

    @classmethod
    def from_string(cls, value: str | None) -> ResourceType:
        """Convert string to ResourceType, defaulting to PVC."""
        if not value:
            return cls.PVC
        try:
            return cls(value.lower())
        except ValueError:
            return cls.PVC


def utc_now() -> datetime:
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
    backup_run_id: str | None = None  # Groups PVCs from same backup run
    # Resource type for filtering (pvc, database, bucket)
    resource_type: str | None = None
    # "scheduled" (subject to retention) or "manual" (protected). Legacy
    # snapshots without the tag are treated as scheduled.
    trigger: str = "scheduled"
    # Kopia source identity (user@host). Snapshots written with the intended
    # stable identity have user "opi-backup"; anything else is debris from
    # runs where the identity override was not applied.
    source_user: str | None = None
    source_host: str | None = None
    # Raw tags for debugging
    tags: dict[str, str] | None = None

    @property
    def reference_name(self) -> str | None:
        """Get reference name based on resource type.

        For PVCs: returns storage_name
        For databases/buckets: extracts from tags
        """
        rt = ResourceType.from_string(self.resource_type)
        if rt == ResourceType.PVC:
            return self.storage_name
        elif rt == ResourceType.DATABASE:
            return self.tags.get("tag:database") or self.tags.get("database") if self.tags else None
        elif rt == ResourceType.BUCKET:
            return self.tags.get("tag:bucket") or self.tags.get("bucket") if self.tags else None
        return self.storage_name


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

    def __init__(self, kubectl: KubectlConnector) -> None:
        self.kubectl = kubectl
        self._held = False
        self.lock_namespace = settings.ARGOCD_MANAGER

    async def acquire(
        self,
        timeout_seconds: int = 3600,
        wait_seconds: int | None = None,
        poll_seconds: float = 5.0,
    ) -> bool:
        """
        Acquire the backup lock, waiting for it to become free if held.

        Uses ``kubectl create`` (not ``apply``) on the ConfigMap so the
        Kubernetes API server provides atomic mutex semantics: exactly one
        concurrent caller can create the resource; everyone else gets
        ``AlreadyExists`` and loops back into the wait. Without this,
        ``apply``'s upsert behavior lets two clients both believe they own
        the lock when they GET-then-APPLY at the same time.

        Args:
            timeout_seconds: Consider an existing lock stale after this many seconds
                (orphan / hung backup) and take it over.
            wait_seconds: How long to wait for the lock to become free before
                giving up. Defaults to ``settings.BACKUP_LOCK_WAIT_SECONDS``.
                Use 0 to fail immediately if the lock is held.
            poll_seconds: How often to re-check while waiting.

        Returns:
            True if lock acquired, False if held by another running process
            past the wait window.
        """
        if wait_seconds is None:
            wait_seconds = settings.BACKUP_LOCK_WAIT_SECONDS
        deadline = utc_now().timestamp() + max(0, wait_seconds)
        logged_wait = False

        while True:
            try:
                # Inspect the current lock state.
                args = ["get", "configmap", self.LOCK_NAME, "-n", self.lock_namespace, "-o", "json"]
                stdout, _stderr, code = await self.kubectl.run_command(args)

                lock_exists = code == 0
                should_take_over = False

                if lock_exists:
                    lock_data = json.loads(stdout)
                    data = lock_data.get("data", {})
                    locked_at_str = data.get("locked_at", "")
                    locked_by = data.get("locked_by", "unknown")

                    if locked_at_str:
                        locked_at = datetime.fromisoformat(locked_at_str)
                        age_seconds = (utc_now() - locked_at).total_seconds()
                        pod_exists = await self._check_pod_exists(locked_by)

                        if not pod_exists:
                            logger.warning(
                                f"Taking over orphaned backup lock (pod {locked_by} no longer exists, "
                                f"lock held for {age_seconds:.0f}s)"
                            )
                            should_take_over = True
                        elif age_seconds < timeout_seconds:
                            # Lock is held by a live pod. Wait for release.
                            if utc_now().timestamp() >= deadline:
                                logger.warning(
                                    f"Backup lock held by {locked_by} since {locked_at_str} "
                                    f"({age_seconds:.0f}s ago); gave up after {wait_seconds}s wait"
                                )
                                return False
                            if not logged_wait:
                                logger.info(
                                    f"Backup lock held by {locked_by} ({age_seconds:.0f}s ago); "
                                    f"waiting up to {wait_seconds}s"
                                )
                                logged_wait = True
                            await asyncio.sleep(poll_seconds)
                            continue
                        else:
                            logger.warning(
                                f"Taking over stale backup lock (held for {age_seconds:.0f}s, "
                                f"timeout is {timeout_seconds}s)"
                            )
                            should_take_over = True
                    else:
                        # ConfigMap exists but has no locked_at — treat as orphan.
                        should_take_over = True

                if should_take_over:
                    # Delete the old lock so the atomic `kubectl create` below can succeed.
                    # If someone beat us to it, the delete fails harmlessly; the create
                    # below will see the new lock and we loop back.
                    delete_args = [
                        "delete",
                        "configmap",
                        self.LOCK_NAME,
                        "-n",
                        self.lock_namespace,
                        "--ignore-not-found=true",
                    ]
                    await self.kubectl.run_command(delete_args)

                # Atomically claim the lock.
                lock_content = {
                    "locked_at": utc_now().isoformat(),
                    "locked_by": os.environ.get("HOSTNAME", "unknown"),
                }
                configmap_yaml = f"""apiVersion: v1
kind: ConfigMap
metadata:
  name: {self.LOCK_NAME}
  namespace: {self.lock_namespace}
data:
  locked_at: "{lock_content["locked_at"]}"
  locked_by: "{lock_content["locked_by"]}"
"""
                create_args = ["create", "-f", "-"]
                _, stderr, code = await self.kubectl.run_command(create_args, stdin_input=configmap_yaml)

                if code == 0:
                    self._held = True
                    logger.info(f"Backup lock acquired by {lock_content['locked_by']}")
                    return True

                # Someone else won the race. Loop back: re-inspect and wait.
                if "alreadyexists" in stderr.lower().replace(" ", "") or "already exists" in stderr.lower():
                    logger.debug("Backup lock claimed by another process during create; retrying")
                    if utc_now().timestamp() >= deadline:
                        logger.warning(f"Lost lock-creation race and gave up after {wait_seconds}s wait")
                        return False
                    await asyncio.sleep(poll_seconds)
                    continue

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
            args = ["delete", "configmap", self.LOCK_NAME, "-n", self.lock_namespace, "--ignore-not-found=true"]
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

    async def _check_pod_exists(self, pod_name: str) -> bool:
        """Check if a pod exists in the rig-system namespace."""
        if not pod_name or pod_name == "unknown":
            return False

        try:
            args = ["get", "pod", pod_name, "-n", self.lock_namespace, "-o", "name"]
            _, _, code = await self.kubectl.run_command(args)
            return code == 0
        except Exception:
            logger.debug(f"Error checking if pod {pod_name} exists, assuming it doesn't")
            return False

    async def get_status(self) -> BackupStatus:
        """Get current lock status."""
        try:
            args = ["get", "configmap", self.LOCK_NAME, "-n", self.lock_namespace, "-o", "json"]
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
  namespace: {self.lock_namespace}
data:
  locked_at: "{utc_now().isoformat()}"
  locked_by: "{os.environ.get("HOSTNAME", "unknown")}"
  current_namespace: "{namespace}"
  current_pvc: "{pvc_name}"
"""
            args = ["apply", "-f", "-"]
            await self.kubectl.run_command(args, stdin_input=configmap_yaml)
        except Exception as e:
            logger.warning(f"Failed to update backup progress: {e}")

    async def __aenter__(self) -> BackupLock:
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


def normalize_bucket_name(name: str) -> str:
    """
    Normalize a string to be a valid S3 bucket name.

    S3 bucket naming rules:
    - 3-63 characters
    - Lowercase letters, numbers, and hyphens only
    - Must start and end with letter or number
    - No consecutive hyphens
    """
    # Convert to lowercase
    name = name.lower()
    # Replace underscores and other invalid chars with hyphens
    name = re.sub(r"[^a-z0-9-]", "-", name)
    # Remove consecutive hyphens
    name = re.sub(r"-+", "-", name)
    # Remove leading/trailing hyphens
    name = name.strip("-")
    # Truncate to 63 chars (S3 limit)
    if len(name) > 63:
        name = name[:63].rstrip("-")
    return name


def get_backup_bucket_name(
    project_name: str | None = None,
    cluster: str | None = None,
) -> str:
    """
    Get the backup bucket name based on configuration mode.

    If BACKUP_S3_BUCKET_MODE is "single", returns BACKUP_S3_BUCKET.
    If BACKUP_S3_BUCKET_MODE is "per-project", generates a bucket name
    using the pattern: backup-{org}-{project}-{cluster}

    Args:
        project_name: Project name (required for per-project mode)
        cluster: Cluster name (required for per-project mode, defaults to CLUSTER_MANAGER)

    Returns:
        The bucket name to use for backups
    """
    bucket_mode = settings.BACKUP_S3_BUCKET_MODE

    if bucket_mode == "single":
        return settings.BACKUP_S3_BUCKET

    if bucket_mode == "per-project":
        if not project_name:
            logger.warning("per-project bucket mode requires project_name, falling back to single bucket")
            return settings.BACKUP_S3_BUCKET

        # Use provided cluster or default to current cluster
        effective_cluster = cluster or settings.CLUSTER_MANAGER
        org_prefix = settings.BACKUP_S3_ORG_PREFIX

        # Generate normalized bucket name
        raw_name = f"backup-{org_prefix}-{project_name}-{effective_cluster}"
        bucket_name = normalize_bucket_name(raw_name)

        logger.debug(f"Generated per-project bucket name: {bucket_name}")
        return bucket_name

    # Unknown mode, fall back to single bucket
    logger.warning(f"Unknown BACKUP_S3_BUCKET_MODE '{bucket_mode}', using single bucket")
    return settings.BACKUP_S3_BUCKET


@dataclass
class BackupConfig:
    """Configuration for backup operations."""

    s3_endpoint: str
    s3_bucket: str  # Default bucket (used when project context not available)
    s3_access_key: str
    s3_secret_key: str
    s3_use_tls: bool = False
    snapshot_class: str = "ocs-storagecluster-rbdplugin-snapclass"
    timeout_seconds: int = 3600
    retention_keep_latest: int = 30
    retention_keep_daily: int = 30
    retention_keep_weekly: int = 4
    retention_keep_monthly: int = 12

    def get_bucket_name(
        self,
        project_name: str | None = None,
        cluster: str | None = None,
    ) -> str:
        """
        Get the bucket name for a specific project/cluster.

        Uses the centralized get_backup_bucket_name function.
        """
        return get_backup_bucket_name(project_name, cluster)

    @classmethod
    def from_settings(cls) -> BackupConfig:
        """Create BackupConfig from application settings."""
        # Get snapshot class from cluster config, fall back to settings if not configured
        snapshot_class = get_volume_snapshot_class(settings.CLUSTER_MANAGER) or settings.BACKUP_SNAPSHOT_CLASS
        return cls(
            s3_endpoint=settings.BACKUP_S3_ENDPOINT,
            s3_bucket=settings.BACKUP_S3_BUCKET,  # Default bucket
            s3_access_key=settings.BACKUP_S3_ACCESS_KEY,
            s3_secret_key=settings.BACKUP_S3_SECRET_KEY,
            s3_use_tls=settings.BACKUP_S3_USE_TLS,
            snapshot_class=snapshot_class,
            timeout_seconds=settings.BACKUP_TIMEOUT_SECONDS,
            retention_keep_latest=settings.BACKUP_RETENTION_KEEP_LATEST,
            retention_keep_daily=settings.BACKUP_RETENTION_KEEP_DAILY,
            retention_keep_weekly=settings.BACKUP_RETENTION_KEEP_WEEKLY,
            retention_keep_monthly=settings.BACKUP_RETENTION_KEEP_MONTHLY,
        )


_KOPIA_USERNAME = "opi-backup"


def kopia_backup_identity(
    project_name: str | None,
    deployment_name: str | None,
    resource_kind: str,
    resource_name: str | None,
    trigger: str,
) -> tuple[str, str]:
    """Return (kopia_hostname, kopia_source) for a backup snapshot.

    Each (project, deployment, resource) gets its own Kopia source identity so
    retention runs per resource, and manual backups get a separate identity
    (`-manual` suffix) so they're never targeted by scheduled retention.

    - `kopia_hostname` is passed to `kopia snapshot create --override-hostname`.
    - `kopia_source` is `opi-backup@<hostname>` — used for `kopia policy set`
      and `kopia snapshot expire` to scope to this resource only.

    Args:
        project_name: Project name (None falls back to "unknown").
        deployment_name: Deployment name (None falls back to "unknown").
        resource_kind: "pvc" | "db" | "bucket".
        resource_name: Storage name for PVC, reference name for DB/bucket.
        trigger: "scheduled" or "manual".
    """
    parts = [
        project_name or "unknown",
        deployment_name or "unknown",
        resource_kind,
        resource_name or "unknown",
    ]
    hostname = "-".join(parts)
    if trigger == "manual":
        hostname += "-manual"
    return hostname, f"{_KOPIA_USERNAME}@{hostname}"


class BaseBackupManager:
    """
    Base class for backup managers with shared functionality.

    Provides common operations used by all backup types (PVC, database, bucket):
    - Distributed locking
    - S3 bucket management
    - Key derivation from SOPS
    - Pod lifecycle management
    """

    # Path from /app/opi/manager/backup/ to /app/manifests/
    MANIFESTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "manifests")

    def __init__(self, config: BackupConfig | None = None) -> None:
        self.kubectl = KubectlConnector()
        self.config = config or BackupConfig.from_settings()
        self.lock = BackupLock(self.kubectl)

    def _template_manifest(self, manifest_content: str, variables: dict[str, Any]) -> str:
        """Render a manifest template, then apply the cluster's registry rewrite.

        Backup pods are bare ``Pod``s applied directly (outside the project manifest
        pipeline), so without this they keep raw upstream image refs (e.g. ghcr.io)
        and fail to pull on ODCN. Routing them through the same RegistryRewriteExtension
        used for project workloads rewrites images to the RCR mirror and attaches the
        pull secret. No-op on clusters without registry-rewrite extensions (local/sandbox).
        """
        rendered = self.kubectl.template_manifest(manifest_content, variables)
        pipeline = load_extensions(settings.CLUSTER_MANAGER)
        if not pipeline.has_extensions:
            return rendered
        manifest = yaml.safe_load(rendered)
        if not isinstance(manifest, dict):
            return rendered
        return yaml.dump(pipeline.process_manifest(manifest), default_flow_style=False, sort_keys=False)

    async def get_status(self) -> BackupStatus:
        """Get current backup status."""
        return await self.lock.get_status()

    async def _ensure_backup_bucket_exists(
        self,
        project_name: str | None = None,
        cluster: str | None = None,
    ) -> str:
        """
        Ensure the backup S3 bucket exists, creating it if necessary.

        Uses the minio connector to check and create the bucket on the
        backup destination minio instance.

        Args:
            project_name: Project name for per-project bucket mode
            cluster: Cluster name for per-project bucket mode

        Returns:
            The bucket name that was ensured to exist
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

        # Get the bucket name (may be per-project or single bucket based on config)
        bucket_name = self.config.get_bucket_name(project_name, cluster)

        # Check if bucket exists and create if needed
        bucket_result = await minio_connector.create_bucket(alias_name, bucket_name)

        if bucket_result["status"] == "created":
            logger.info(f"Created backup bucket: {bucket_name}")
        elif bucket_result["status"] == "exists":
            logger.debug(f"Backup bucket already exists: {bucket_name}")
        else:
            raise RuntimeError(
                f"Failed to ensure backup bucket exists: {bucket_result.get('message', 'Unknown error')}"
            )

        return bucket_name

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

    # Container statuses that are unrecoverable - fail immediately.
    FATAL_CONTAINER_STATUSES = frozenset(
        {
            "crashloopbackoff",
            "createcontainerconfigerror",
            "invalidimagename",
            "errimageneverpull",
        }
    )
    # Image-pull failures kubelet keeps retrying (e.g. a transient registry/network
    # hiccup). NOT fatal on first sight - we let kubelet's backoff retry recover, and
    # only give up if it stays stuck past IMAGE_PULL_GRACE_SECONDS.
    RETRYABLE_PULL_STATUSES = frozenset({"imagepullbackoff", "errimagepull"})
    IMAGE_PULL_GRACE_SECONDS = 180

    async def _wait_for_pod(self, namespace: str, pod_name: str, timeout: int | None = None) -> bool:
        """
        Wait for pod to complete.

        Returns:
            True if pod completed successfully, False if failed
        """
        timeout = timeout or self.config.timeout_seconds
        logger.info(f"Waiting for pod {pod_name} to complete (timeout: {timeout}s)...")

        start_time = asyncio.get_event_loop().time()
        pull_backoff_since: float | None = None  # when a retryable image-pull failure first appeared

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                logger.error(f"Timeout waiting for pod {pod_name} after {timeout}s")
                return False

            # Get pod status as JSON for detailed inspection
            args = ["get", "pod", pod_name, "-n", namespace, "-o", "json"]
            stdout, stderr, code = await self.kubectl.run_command(args)

            if code != 0:
                logger.warning(f"Failed to get pod status: {stderr}")
                await asyncio.sleep(10)
                continue

            try:
                import json

                pod_data = json.loads(stdout)
                phase = pod_data.get("status", {}).get("phase", "").lower()

                # Inspect container waiting-states. Truly-fatal states fail immediately;
                # image-pull failures are left to kubelet's retry and only fail after a grace,
                # so a transient registry/network hiccup doesn't kill an otherwise-fine backup.
                container_statuses = pod_data.get("status", {}).get("containerStatuses", [])
                fatal: tuple[str, str] | None = None
                pull_retry_reason: str | None = None
                for cs in container_statuses:
                    waiting = cs.get("state", {}).get("waiting", {})
                    reason = waiting.get("reason", "").lower()
                    if reason in self.FATAL_CONTAINER_STATUSES:
                        fatal = (reason, waiting.get("message", "No details"))
                        break
                    if reason in self.RETRYABLE_PULL_STATUSES:
                        pull_retry_reason = reason

                if fatal:
                    logger.error(f"Pod {pod_name} has fatal container error: {fatal[0]} - {fatal[1]}")
                    return False

                if pull_retry_reason:
                    if pull_backoff_since is None:
                        pull_backoff_since = elapsed
                        logger.warning(
                            f"Pod {pod_name} image pull failed ({pull_retry_reason}); letting kubelet "
                            f"retry (grace {self.IMAGE_PULL_GRACE_SECONDS}s)"
                        )
                    elif elapsed - pull_backoff_since > self.IMAGE_PULL_GRACE_SECONDS:
                        logger.error(
                            f"Pod {pod_name} stuck in {pull_retry_reason} for "
                            f">{self.IMAGE_PULL_GRACE_SECONDS}s - giving up"
                        )
                        return False
                else:
                    pull_backoff_since = None  # recovered or no longer in a pull-backoff state

                if phase == "succeeded":
                    logger.info(f"Pod {pod_name} completed successfully")
                    return True
                elif phase == "failed":
                    logger.error(f"Pod {pod_name} failed")
                    return False
                elif phase in ("pending", "running"):
                    logger.debug(f"Pod {pod_name} is {phase}")
                else:
                    logger.warning(f"Unexpected pod phase: {phase}")

            except json.JSONDecodeError:
                logger.warning("Failed to parse pod status JSON")

            await asyncio.sleep(10)

    async def _get_pod_logs(self, namespace: str, pod_name: str) -> str:
        """Get logs from a pod."""
        args = ["logs", pod_name, "-n", namespace, "--tail=100"]
        stdout, stderr, code = await self.kubectl.run_command(args)

        if code != 0:
            return f"Failed to get logs: {stderr}"

        return stdout

    async def _cleanup_pod(self, namespace: str, pod_name: str) -> None:
        """Delete a pod (best effort)."""
        try:
            args = ["delete", "pod", pod_name, "-n", namespace, "--ignore-not-found=true"]
            await self.kubectl.run_command(args)
            logger.debug(f"Deleted pod {pod_name}")
        except Exception as e:
            logger.warning(f"Failed to delete pod {pod_name}: {e}")
