"""
Skopeo connector for pushing container images to remote registries.

This module provides functionality to push Docker image tarballs to container
registries using the skopeo CLI, following the same singleton pattern as minio_mc.py.
"""

import asyncio
import contextlib
import ipaddress
import logging
import os
import re
import socket
import subprocess
import tempfile
import threading
from typing import TYPE_CHECKING

from opi.core.config import settings
from opi.services.catalog.image_registries.naming import upstream_host
from opi.utils.age import decrypt_password_smart_auto_sync
from opi.utils.naming import REGISTRY_TAG_OWNER_RE, build_registry_tag
from opi.utils.secrets import RegistrySecret

if TYPE_CHECKING:
    from collections.abc import Iterator

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

#: Eén melding voor elke geweigerde bestemming, anders is de weigering zelf een orakel.
REFUSED_DESTINATION_REASON = "het platform mag deze registry niet benaderen"


@contextlib.contextmanager
def _authfile(registry: str, username: str, password: str) -> Iterator[str]:
    """Een authfile (0600, in een eigen tijdelijke map) zodat het wachtwoord niet in de argv staat."""
    with tempfile.TemporaryDirectory(prefix="skopeo-auth-") as directory:
        path = os.path.join(directory, "auth.json")
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
            handle.write(
                RegistrySecret(registry_url=registry, username=username, password=password).to_dockerconfigjson()
            )
        yield path


def _is_sandbox_registry(authority: str) -> bool:
    """De platformregistry in de sandbox: die resolvet in de pod naar de ingress, een privaat adres."""
    return (
        settings.CLUSTER_MANAGER == "sandboxed-local"
        and bool(settings.REGISTRY_URL)
        and authority == upstream_host(settings.REGISTRY_URL)
    )


async def _destination_refused(repository: str) -> bool:
    """Valt de host van ``repository`` in private of bijzondere adresruimte, of resolvet hij niet?

    ``is_global`` en niet alleen ``is_private``: die laatste laat ``0.0.0.0`` (op Linux de
    eigen pod) en ``100.64.0.0/10`` door.
    """
    authority = upstream_host(repository)
    if _is_sandbox_registry(authority):
        return False
    host = authority.partition(":")[0]
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror, UnicodeError:
        return True
    return not addresses or any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses)


class SkopeoConnector:
    """Connector for pushing container images using skopeo CLI."""

    _instance = None
    _lock = threading.Lock()
    is_skopeo_available = False

    def __new__(cls) -> SkopeoConnector:
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

    @staticmethod
    def _validate_project_name(project_name: str) -> None:
        """Validate the owning project name.

        The project name becomes the owner prefix of the registry tag, and that
        prefix is only unambiguous because a project name cannot contain the
        separator. Rejecting anything else here keeps that guarantee local.
        """
        if not project_name:
            raise SkopeoValidationError("Project name cannot be empty")
        if not REGISTRY_TAG_OWNER_RE.match(project_name):
            raise SkopeoValidationError(
                f"Invalid project name '{project_name}': must start with a lowercase letter and contain only "
                f"lowercase letters, digits and hyphens"
            )

    def validate_push_target(self, project_name: str, image_name: str, tag: str) -> str:
        """Validate a push target and return the registry tag it resolves to.

        Raises:
            SkopeoValidationError: If any part is invalid, or if the resulting tag
                does not fit the 128 characters a registry tag may have.
        """
        self._validate_project_name(project_name)
        self._validate_image_name(image_name)
        self._validate_tag(tag)

        combined_tag = build_registry_tag(project_name, image_name, tag)
        if not _TAG_RE.match(combined_tag):
            raise SkopeoValidationError(
                f"Image name and tag are too long: they resolve to registry tag '{combined_tag}' "
                f"({len(combined_tag)} characters), and a registry tag may be at most 128"
            )
        return combined_tag

    def _build_destination(self, project_name: str, image_name: str, tag: str) -> str:
        """Build the full destination registry URL.

        Owner, image name and tag are all encoded into the tag (e.g. mink_app-latest)
        because Quay does not support nested repos under a single robot-account-scoped
        repository. The owning project comes first, so one project can never write to
        the tag of another.
        """
        combined_tag = build_registry_tag(project_name, image_name, tag)
        return f"docker://{settings.REGISTRY_URL}/{settings.REGISTRY_ORG}:{combined_tag}"

    def _build_command(self, tarball_path: str, destination: str, authfile: str | None) -> list[str]:
        """Build the skopeo copy command."""
        cmd = ["skopeo", "copy"]

        if authfile:
            cmd.extend(["--dest-authfile", authfile])

        if not settings.REGISTRY_VERIFY_TLS:
            cmd.append("--dest-tls-verify=false")

        cmd.extend([f"docker-archive:{tarball_path}", destination])
        return cmd

    async def check_repository_access(
        self, repository: str, username: str, password: str, timeout_seconds: int = 20
    ) -> tuple[bool, str]:
        """Can these credentials READ this repository? Returns (ok, reason).

        ``list-tags`` is the ``tags/list`` call, which is exactly what needs the read
        scope. With no skopeo CLI this returns ok: refusing a save over a check we could
        not run would block a user for a platform gap.
        """
        if not self.is_skopeo_available:
            logger.info("Skopeo CLI not available; registry credentials not verified")
            return True, ""

        if await _destination_refused(repository):
            logger.warning(f"Registry access check refused: {repository} is not a public destination")
            return False, REFUSED_DESTINATION_REASON

        with _authfile(upstream_host(repository), username, password) as authfile:
            cmd = ["skopeo", "list-tags", "--authfile", authfile, f"docker://{repository}"]
            logger.info(f"Verifying registry access: {' '.join(cmd)}")
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                _stdout, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
            except TimeoutError:
                logger.warning(f"Registry access check timed out for {repository}")
                return True, ""
            except FileNotFoundError:
                return True, ""

        if process.returncode == 0:
            return True, ""
        # The registry's own words say what went wrong, but skopeo echoes the URL it
        # tried, so strip anything that looks like a userinfo part.
        reason = stderr_bytes.decode(errors="replace").strip().splitlines()
        return False, self._mask_userinfo(reason[0]) if reason else "de registry gaf geen reden"

    @staticmethod
    def _mask_userinfo(text: str) -> str:
        """Remove a ``user:token@`` part from a message before it is shown or logged."""
        return re.sub(r"//[^/\s]*:[^/\s]*@", "//***@", text)

    async def push_image(self, tarball_path: str, project_name: str, image_name: str, tag: str) -> str:
        """
        Push a Docker image tarball to the configured registry.

        Owner, image name and tag are encoded into the registry tag (e.g.
        rig/zad:mink_app-latest) because Quay does not support nested repos under a
        single robot-account-scoped repository. The owning project is the first part,
        which makes the destination of one project unreachable for another.

        Args:
            tarball_path: Path to the Docker image tarball (from docker save)
            project_name: Project that owns the image (the API key's project)
            image_name: Name of the image
            tag: Image tag

        Returns:
            The full image reference that was pushed (e.g. rcr.rijksapps.nl/rig/zad:mink_app-latest)

        Raises:
            SkopeoConnectionError: If skopeo is not available
            SkopeoValidationError: If input validation fails
            SkopeoExecutionError: If the push command fails
        """
        if not self.is_skopeo_available:
            raise SkopeoConnectionError("Skopeo CLI is not available")

        if not settings.REGISTRY_URL:
            raise SkopeoValidationError("REGISTRY_URL is not configured")

        combined_tag = self.validate_push_target(project_name, image_name, tag)

        destination = self._build_destination(project_name, image_name, tag)
        credentials = (
            _authfile(settings.REGISTRY_URL, settings.REGISTRY_USERNAME, self._registry_password)
            if self._registry_password and settings.REGISTRY_USERNAME
            else contextlib.nullcontext(None)
        )

        with credentials as authfile:
            cmd = self._build_command(tarball_path, destination, authfile)
            logger.info(f"Pushing image: {' '.join(cmd)}")

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

        image_ref = f"{settings.REGISTRY_URL}/{settings.REGISTRY_ORG}:{combined_tag}"
        logger.info(f"Successfully pushed image to {image_ref}")
        if stdout:
            logger.debug(f"Skopeo output: {stdout.strip()}")

        return image_ref
