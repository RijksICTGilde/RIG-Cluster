"""
Kopia connector for backup repository operations.

This module provides functionality to interact with Kopia backup repositories
for listing snapshots and querying backup metadata directly, without needing
to spawn Kubernetes pods.
"""

import asyncio
import json
import logging
import os
import subprocess
import tempfile
import threading
from dataclasses import dataclass

from opi.manager.backup.base import ResourceType

logger = logging.getLogger(__name__)


class KopiaConnectionError(Exception):
    """Exception raised when Kopia connection is not available."""


class KopiaExecutionError(Exception):
    """Exception raised when Kopia command execution fails."""


@dataclass
class KopiaSnapshot:
    """Information about a Kopia snapshot."""

    snapshot_id: str
    source_path: str
    timestamp: str
    size_bytes: int | None = None
    tags: dict[str, str] | None = None

    def _get_tag(self, key: str) -> str | None:
        """Get a tag value, handling Kopia's 'tag:' prefix."""
        if not self.tags:
            return None
        # Kopia stores tags with 'tag:' prefix in JSON output
        value = self.tags.get(f"tag:{key}") or self.tags.get(key)
        # Treat string "None" as actual None
        if value == "None" or value == "unknown":
            return None
        return value

    @property
    def pvc_name(self) -> str:
        """Extract PVC name from tags or source path."""
        value = self._get_tag("pvc")
        if value:
            return value
        # Fallback: extract from source path (e.g., /data -> data)
        return self.source_path.strip("/").split("/")[-1] if self.source_path else "unknown"

    @property
    def cluster(self) -> str | None:
        """Extract cluster from tags."""
        return self._get_tag("cluster")

    @property
    def namespace(self) -> str | None:
        """Extract namespace from tags."""
        return self._get_tag("namespace")

    @property
    def project_name(self) -> str | None:
        """Extract project name from tags."""
        return self._get_tag("project")

    @property
    def deployment_name(self) -> str | None:
        """Extract deployment name from tags."""
        return self._get_tag("deployment")

    @property
    def component_name(self) -> str | None:
        """Extract component name from tags."""
        return self._get_tag("component")

    @property
    def storage_name(self) -> str | None:
        """Extract storage name from tags."""
        return self._get_tag("storage")

    @property
    def generation(self) -> int | None:
        """Extract generation from tags."""
        value = self._get_tag("generation")
        if value:
            try:
                return int(value)
            except ValueError, TypeError:
                return None
        return None

    @property
    def backup_run_id(self) -> str | None:
        """Extract backup run ID from tags (groups PVCs from same backup run)."""
        return self._get_tag("backup_run")

    @property
    def resource_type(self) -> ResourceType:
        """Extract resource type from tags."""
        return ResourceType.from_string(self._get_tag("resource_type"))

    @property
    def trigger(self) -> str:
        """Whether the backup was created by the scheduler or a manual action.

        Returns "scheduled" or "manual". Legacy snapshots without the tag are
        treated as scheduled (the historical default before this field existed).
        """
        value = self._get_tag("trigger")
        return value if value in {"scheduled", "manual"} else "scheduled"

    @property
    def reference_name(self) -> str | None:
        """Extract reference name based on resource type.

        Different resource types store reference names in different tags:
        - pvc: uses 'storage' tag
        - database: uses 'database' tag
        - bucket: uses 'bucket' tag
        """
        tag_map = {
            ResourceType.PVC: "storage",
            ResourceType.DATABASE: "database",
            ResourceType.BUCKET: "bucket",
        }
        return self._get_tag(tag_map.get(self.resource_type, "storage"))


@dataclass
class KopiaRepositoryConfig:
    """Configuration for connecting to a Kopia S3 repository."""

    s3_endpoint: str
    s3_bucket: str
    s3_access_key: str
    s3_secret_key: str
    s3_prefix: str
    password: str
    use_tls: bool = False


class KopiaConnector:
    """Connector for interacting with Kopia backup repositories."""

    _instance = None
    _lock = threading.Lock()
    is_kopia_available = False

    def __new__(cls) -> KopiaConnector:
        """Implement singleton pattern."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        """Initialize the Kopia connector."""
        if self._initialized:
            return

        logger.debug("Initializing KopiaConnector")

        # Setup environment variables
        # Set HOME to /tmp to avoid Kopia writing to non-existent /home/appuser
        self.env = os.environ.copy()
        self.env["HOME"] = os.environ.get("TMPDIR", "/tmp")

        # Test Kopia CLI availability synchronously during initialization
        try:
            result = subprocess.run(
                ["kopia", "--version"],
                capture_output=True,
                text=True,
                env=self.env,
                timeout=10,
            )
            if result.returncode == 0:
                version = result.stdout.strip().split()[0] if result.stdout else "unknown"
                logger.info(f"Kopia CLI is available (version: {version})")
                KopiaConnector.is_kopia_available = True
            else:
                logger.warning(f"Kopia CLI not available: {result.stderr}")
                KopiaConnector.is_kopia_available = False
        except FileNotFoundError:
            logger.warning("Kopia CLI not found in PATH")
            KopiaConnector.is_kopia_available = False
        except Exception as e:
            logger.error(f"Error testing Kopia CLI availability: {e}")
            KopiaConnector.is_kopia_available = False

        self._initialized = True
        logger.debug("KopiaConnector initialized successfully")

    async def _run_kopia_command(
        self,
        args: list[str],
        config_file: str | None = None,
        timeout: int = 60,
    ) -> tuple[str, str, int]:
        """
        Run a Kopia command asynchronously.

        Args:
            args: Command arguments (without 'kopia' prefix)
            config_file: Optional path to config file
            timeout: Command timeout in seconds

        Returns:
            Tuple of (stdout, stderr, return_code)

        Raises:
            KopiaConnectionError: If Kopia CLI is not available
            KopiaExecutionError: If command execution fails
        """
        if not KopiaConnector.is_kopia_available:
            raise KopiaConnectionError("Kopia CLI is not available")

        cmd = ["kopia"]

        # Add config file if provided
        if config_file:
            cmd.extend(["--config-file", config_file])

        cmd.extend(args)

        # Log command (redact sensitive info - also redact values after sensitive flags)
        safe_cmd = []
        redact_next = False
        for arg in cmd:
            if redact_next:
                safe_cmd.append("***")
                redact_next = False
            elif "password" in arg.lower() or "secret" in arg.lower():
                safe_cmd.append(arg)
                redact_next = True
            else:
                safe_cmd.append(arg)
        logger.debug(f"Running Kopia command: {' '.join(safe_cmd)}")

        try:
            from opi.core.metrics import track_subprocess_memory

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
            )

            async with track_subprocess_memory("kopia"):
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout,
                )

            stdout_str = stdout.decode("utf-8") if stdout else ""
            stderr_str = stderr.decode("utf-8") if stderr else ""
            return_code = process.returncode or 0

            if return_code != 0:
                logger.debug(f"Kopia command failed with code {return_code}: {stderr_str}")

            return stdout_str, stderr_str, return_code

        except TimeoutError as e:
            logger.error(f"Kopia command timed out after {timeout}s")
            raise KopiaExecutionError(f"Command timed out after {timeout}s") from e
        except Exception as e:
            logger.error(f"Error running Kopia command: {e}")
            raise KopiaExecutionError(f"Command execution failed: {e}") from e

    async def _disconnect_from_repository(self, config_file: str) -> None:
        """Disconnect from repository, ignoring any errors."""
        try:
            await self._run_kopia_command(
                ["repository", "disconnect"],
                config_file=config_file,
                timeout=10,
            )
        except Exception as e:
            logger.debug(f"Failed to disconnect from repository (non-fatal): {e}")

    async def list_snapshots(
        self,
        config: KopiaRepositoryConfig,
        resource_type: str | None = None,
    ) -> list[KopiaSnapshot]:
        """
        List all snapshots in a Kopia repository.

        Args:
            config: Repository connection configuration
            resource_type: Optional filter by resource type ('pvc', 'database', 'bucket').
                          If None, returns all snapshots.

        Returns:
            List of KopiaSnapshot objects, optionally filtered by resource_type
        """
        # Create a temporary config file for this connection
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = os.path.join(temp_dir, "repository.config")
            cache_dir = os.path.join(temp_dir, "cache")
            os.makedirs(cache_dir, exist_ok=True)

            # Build TLS flag
            tls_flag = [] if config.use_tls else ["--disable-tls"]

            # Connect to repository
            connect_args = [
                "repository",
                "connect",
                "s3",
                "--bucket",
                config.s3_bucket,
                "--prefix",
                f"{config.s3_prefix}/",
                "--endpoint",
                config.s3_endpoint,
                "--access-key",
                config.s3_access_key,
                "--secret-access-key",
                config.s3_secret_key,
                "--password",
                config.password,
                "--cache-directory",
                cache_dir,
                "--no-check-for-updates",
                *tls_flag,
            ]

            stdout, stderr, code = await self._run_kopia_command(
                connect_args,
                config_file=config_file,
                timeout=30,
            )

            if code != 0:
                if "repository not found" in stderr.lower() or "no repository" in stderr.lower():
                    logger.debug(f"No repository found at {config.s3_prefix}")
                    return []
                logger.warning(f"Failed to connect to Kopia repository: {stderr}")
                return []

            # List snapshots as JSON
            list_args = ["snapshot", "list", "--json", "--all"]

            stdout, stderr, code = await self._run_kopia_command(
                list_args,
                config_file=config_file,
                timeout=30,
            )

            if code != 0:
                logger.warning(f"Failed to list snapshots: {stderr}")
                return []

            # Parse JSON output
            snapshots = self._parse_snapshot_list(stdout)

            # Disconnect from repository
            await self._disconnect_from_repository(config_file)

            # Apply resource_type filter if provided
            if resource_type:
                snapshots = [s for s in snapshots if s.resource_type == resource_type]
                logger.debug(f"Filtered to {len(snapshots)} snapshots with resource_type={resource_type}")

            return snapshots

    async def delete_snapshot(
        self,
        config: KopiaRepositoryConfig,
        snapshot_id: str,
    ) -> bool:
        """
        Delete a snapshot from a Kopia repository.

        Args:
            config: Repository connection configuration
            snapshot_id: The ID of the snapshot to delete

        Returns:
            True if deletion was successful, False otherwise
        """
        # Create a temporary config file for this connection
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = os.path.join(temp_dir, "repository.config")
            cache_dir = os.path.join(temp_dir, "cache")
            os.makedirs(cache_dir, exist_ok=True)

            # Build TLS flag
            tls_flag = [] if config.use_tls else ["--disable-tls"]

            # Connect to repository
            connect_args = [
                "repository",
                "connect",
                "s3",
                "--bucket",
                config.s3_bucket,
                "--prefix",
                f"{config.s3_prefix}/",
                "--endpoint",
                config.s3_endpoint,
                "--access-key",
                config.s3_access_key,
                "--secret-access-key",
                config.s3_secret_key,
                "--password",
                config.password,
                "--cache-directory",
                cache_dir,
                "--no-check-for-updates",
                *tls_flag,
            ]

            stdout, stderr, code = await self._run_kopia_command(
                connect_args,
                config_file=config_file,
                timeout=30,
            )

            if code != 0:
                logger.warning(f"Failed to connect to Kopia repository: {stderr}")
                return False

            # Delete the snapshot
            delete_args = ["snapshot", "delete", snapshot_id, "--delete"]

            stdout, stderr, code = await self._run_kopia_command(
                delete_args,
                config_file=config_file,
                timeout=30,
            )

            if code != 0:
                logger.warning(f"Failed to delete snapshot {snapshot_id}: {stderr}")
                # Try to disconnect anyway (ignore errors)
                await self._disconnect_from_repository(config_file)
                return False

            logger.info(f"Successfully deleted snapshot {snapshot_id}")

            # Disconnect from repository
            await self._disconnect_from_repository(config_file)

            return True

    async def get_snapshot(
        self,
        config: KopiaRepositoryConfig,
        snapshot_id: str,
    ) -> KopiaSnapshot | None:
        """
        Get a single snapshot by ID.

        Args:
            config: Repository connection configuration
            snapshot_id: The ID of the snapshot to retrieve

        Returns:
            KopiaSnapshot if found, None otherwise
        """
        # Create a temporary config file for this connection
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = os.path.join(temp_dir, "repository.config")
            cache_dir = os.path.join(temp_dir, "cache")
            os.makedirs(cache_dir, exist_ok=True)

            # Build TLS flag
            tls_flag = [] if config.use_tls else ["--disable-tls"]

            # Connect to repository
            connect_args = [
                "repository",
                "connect",
                "s3",
                "--bucket",
                config.s3_bucket,
                "--prefix",
                f"{config.s3_prefix}/",
                "--endpoint",
                config.s3_endpoint,
                "--access-key",
                config.s3_access_key,
                "--secret-access-key",
                config.s3_secret_key,
                "--password",
                config.password,
                "--cache-directory",
                cache_dir,
                "--no-check-for-updates",
                *tls_flag,
            ]

            stdout, stderr, code = await self._run_kopia_command(
                connect_args,
                config_file=config_file,
                timeout=30,
            )

            if code != 0:
                logger.warning(f"Failed to connect to Kopia repository: {stderr}")
                return None

            # Get snapshot info as JSON
            show_args = ["snapshot", "list", "--json", "--all"]

            stdout, stderr, code = await self._run_kopia_command(
                show_args,
                config_file=config_file,
                timeout=30,
            )

            # Disconnect from repository
            await self._disconnect_from_repository(config_file)

            if code != 0:
                logger.warning(f"Failed to list snapshots: {stderr}")
                return None

            # Parse and find the specific snapshot
            snapshots = self._parse_snapshot_list(stdout)
            for snapshot in snapshots:
                if snapshot.snapshot_id == snapshot_id:
                    return snapshot

            return None

    def _parse_snapshot_list(self, json_output: str) -> list[KopiaSnapshot]:
        """
        Parse Kopia snapshot list JSON output.

        Args:
            json_output: JSON output from 'kopia snapshot list --json'

        Returns:
            List of KopiaSnapshot objects
        """
        snapshots: list[KopiaSnapshot] = []

        if not json_output.strip():
            return snapshots

        try:
            data = json.loads(json_output)

            # Kopia returns an array of snapshot objects
            if isinstance(data, list):
                for item in data:
                    snapshot = self._parse_snapshot_item(item)
                    if snapshot:
                        snapshots.append(snapshot)
            elif isinstance(data, dict):
                # Single snapshot
                snapshot = self._parse_snapshot_item(data)
                if snapshot:
                    snapshots.append(snapshot)

        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse Kopia snapshot list JSON: {e}")
            # Try line-by-line parsing (some Kopia versions output JSONL)
            for line in json_output.strip().split("\n"):
                if line.strip().startswith("{"):
                    try:
                        item = json.loads(line)
                        snapshot = self._parse_snapshot_item(item)
                        if snapshot:
                            snapshots.append(snapshot)
                    except json.JSONDecodeError:
                        continue

        return snapshots

    def _parse_snapshot_item(self, item: dict) -> KopiaSnapshot | None:
        """
        Parse a single snapshot item from Kopia JSON output.

        Args:
            item: Dictionary from Kopia JSON output

        Returns:
            KopiaSnapshot object or None if parsing fails
        """
        try:
            snapshot_id = item.get("id", "")
            if not snapshot_id:
                return None

            # Get source path
            source = item.get("source", {})
            source_path = source.get("path", "") if isinstance(source, dict) else str(source)

            # Get timestamp
            timestamp = item.get("startTime", item.get("time", ""))

            # Get size from stats or rootEntry (Kopia stores size in different places)
            size_bytes = None
            stats = item.get("stats", {})
            if isinstance(stats, dict):
                size_bytes = stats.get("totalSize") or stats.get("size")

            # Fallback to rootEntry.summ.size if stats doesn't have it
            if size_bytes is None:
                root_entry = item.get("rootEntry", {})
                if isinstance(root_entry, dict):
                    summ = root_entry.get("summ", {})
                    if isinstance(summ, dict):
                        size_bytes = summ.get("size")

            # Get tags
            tags = item.get("tags", {})

            return KopiaSnapshot(
                snapshot_id=snapshot_id,
                source_path=source_path,
                timestamp=timestamp,
                size_bytes=size_bytes,
                tags=tags if isinstance(tags, dict) else None,
            )

        except Exception as e:
            logger.warning(f"Failed to parse snapshot item: {e}")
            return None


def create_kopia_connector() -> KopiaConnector:
    """Factory function to create a Kopia connector."""
    return KopiaConnector()
