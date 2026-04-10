"""
Skopeo connector for pushing container images to remote registries.

This module provides functionality to push Docker image tarballs to container
registries using the skopeo CLI, following the same singleton pattern as minio_mc.py.
"""

import asyncio
import logging
import re
import subprocess
import threading

from opi.core.config import settings
from opi.utils.age import decrypt_password_smart_auto_sync

logger = logging.getLogger(__name__)


class SkopeoConnectionError(Exception):
    """Exception raised when skopeo CLI is not available."""


class SkopeoExecutionError(Exception):
    """Exception raised when skopeo command execution fails."""


class SkopeoValidationError(Exception):
    """Exception raised when input validation fails."""


# Regex for validating image names and tags
_IMAGE_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$")
_TAG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")


class SkopeoConnector:
    """Connector for pushing container images using skopeo CLI."""

    _instance = None
    _lock = threading.Lock()
    is_skopeo_available = False

    def __new__(cls) -> "SkopeoConnector":
        """Implement singleton pattern."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        """Initialize the skopeo connector."""
        if self._initialized:
            return

        logger.debug("Initializing SkopeoConnector")

        # Check if skopeo CLI is available
        self._check_skopeo_availability()

        # Decrypt registry password once at init
        self._registry_password: str | None = None
        if settings.REGISTRY_PASSWORD:
            try:
                self._registry_password = decrypt_password_smart_auto_sync(settings.REGISTRY_PASSWORD)
                logger.info("Registry password decrypted successfully")
            except Exception as e:
                logger.error(f"Failed to decrypt registry password: {e}")

        self._initialized = True
        logger.info(f"SkopeoConnector initialized (available: {self.is_skopeo_available})")

    def _check_skopeo_availability(self) -> None:
        """Check if skopeo CLI is available on the system."""
        try:
            result = subprocess.run(
                ["skopeo", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                self.is_skopeo_available = True
                logger.info(f"Skopeo CLI available: {result.stdout.strip()}")
            else:
                self.is_skopeo_available = False
                logger.warning(f"Skopeo CLI check failed: {result.stderr.strip()}")
        except FileNotFoundError:
            self.is_skopeo_available = False
            logger.warning("Skopeo CLI not found on system")
        except Exception as e:
            self.is_skopeo_available = False
            logger.error(f"Error checking skopeo availability: {e}")

    @staticmethod
    def _validate_image_name(image_name: str) -> None:
        """Validate container image name."""
        if not image_name:
            raise SkopeoValidationError("Image name cannot be empty")
        if not _IMAGE_NAME_RE.match(image_name):
            raise SkopeoValidationError(
                f"Invalid image name '{image_name}': must contain only lowercase letters, digits, dots, hyphens, underscores"
            )

    @staticmethod
    def _validate_tag(tag: str) -> None:
        """Validate container image tag."""
        if not tag:
            raise SkopeoValidationError("Tag cannot be empty")
        if not _TAG_RE.match(tag):
            raise SkopeoValidationError(
                f"Invalid tag '{tag}': must start with alphanumeric and contain only letters, digits, dots, hyphens, underscores"
            )

    def _build_destination(self, image_name: str, tag: str) -> str:
        """Build the full destination registry URL.

        Image name is encoded into the tag (e.g. mink-latest) because Quay does not
        support nested repos under a single robot-account-scoped repository.
        """
        combined_tag = f"{image_name}-{tag}"
        return f"docker://{settings.REGISTRY_URL}/{settings.REGISTRY_ORG}:{combined_tag}"

    def _build_command(self, tarball_path: str, destination: str) -> list[str]:
        """Build the skopeo copy command."""
        cmd = ["skopeo", "copy"]

        if self._registry_password and settings.REGISTRY_USERNAME:
            cmd.extend(["--dest-creds", f"{settings.REGISTRY_USERNAME}:{self._registry_password}"])

        if not settings.REGISTRY_VERIFY_TLS:
            cmd.append("--dest-tls-verify=false")

        cmd.extend([f"docker-archive:{tarball_path}", destination])
        return cmd

    @staticmethod
    def _mask_credentials(cmd: list[str]) -> list[str]:
        """Return a copy of the command with credentials masked for logging."""
        masked = list(cmd)
        for i, arg in enumerate(masked):
            if arg == "--dest-creds" and i + 1 < len(masked):
                parts = masked[i + 1].split(":", 1)
                masked[i + 1] = f"{parts[0]}:***" if len(parts) == 2 else "***"
        return masked

    async def push_image(self, tarball_path: str, image_name: str, tag: str) -> str:
        """
        Push a Docker image tarball to the configured registry.

        Image name is encoded into the tag (e.g. rig/zad:mink-latest) because Quay
        does not support nested repos under a single robot-account-scoped repository.

        Args:
            tarball_path: Path to the Docker image tarball (from docker save)
            image_name: Name of the image
            tag: Image tag

        Returns:
            The full image reference that was pushed (e.g. rcr.rijksapps.nl/rig/zad:mink-latest)

        Raises:
            SkopeoConnectionError: If skopeo is not available
            SkopeoValidationError: If input validation fails
            SkopeoExecutionError: If the push command fails
        """
        if not self.is_skopeo_available:
            raise SkopeoConnectionError("Skopeo CLI is not available")

        if not settings.REGISTRY_URL:
            raise SkopeoValidationError("REGISTRY_URL is not configured")

        self._validate_image_name(image_name)
        self._validate_tag(tag)

        destination = self._build_destination(image_name, tag)
        cmd = self._build_command(tarball_path, destination)

        masked_cmd = self._mask_credentials(cmd)
        logger.info(f"Pushing image: {' '.join(masked_cmd)}")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout_bytes, stderr_bytes = await process.communicate()
        stdout = stdout_bytes.decode() if stdout_bytes else ""
        stderr = stderr_bytes.decode() if stderr_bytes else ""

        if process.returncode != 0:
            logger.error(f"Skopeo push failed (exit {process.returncode}): {stderr}")
            raise SkopeoExecutionError(f"Failed to push image: {stderr.strip()}")

        combined_tag = f"{image_name}-{tag}"
        image_ref = f"{settings.REGISTRY_URL}/{settings.REGISTRY_ORG}:{combined_tag}"
        logger.info(f"Successfully pushed image to {image_ref}")
        if stdout:
            logger.debug(f"Skopeo output: {stdout.strip()}")

        return image_ref
