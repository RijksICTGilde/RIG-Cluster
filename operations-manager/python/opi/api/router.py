import logging
import threading
import time
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse
from opi.api.endpoint_util import validate_api_token
from opi.connectors.git import GitConnector
from opi.connectors.subdomain import (
    BaseDomainValidationError,
    SubdomainNotAvailableError,
    SubdomainValidationError,
    create_subdomain_connector,
    validate_base_domain,
    validate_subdomain,
)
from opi.core.config import settings
from opi.manager.project_manager import ProjectManager, create_project_manager
from opi.services.project_service import get_project_service
from opi.utils.naming import sanitize_kubernetes_name
from opi.utils.project_utils import generate_self_service_project_yaml, normalize_container_image, validate_project_name
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


def sanitize_for_log(value: str | None, max_length: int = 64) -> str:
    """Sanitize a value for safe inclusion in log messages.

    Prevents log injection attacks by:
    - Truncating to max_length
    - Removing newlines and carriage returns
    - Removing other control characters

    Args:
        value: The value to sanitize
        max_length: Maximum length of the sanitized value (default: 64)

    Returns:
        Sanitized string safe for logging
    """
    if not value:
        return ""
    # Truncate, remove newlines/carriage returns, and strip control characters
    sanitized = value[:max_length].replace("\n", "").replace("\r", "")
    # Remove other ASCII control characters (0x00-0x1F except tab, 0x7F)
    sanitized = "".join(c for c in sanitized if ord(c) >= 32 or c == "\t")
    return sanitized


# Robust rate limiter for subdomain check endpoint with anti-spoofing measures
class IPRateLimiter:
    """Token bucket rate limiter with multi-factor client identification.

    Uses a combination of factors to identify clients, making X-Forwarded-For
    spoofing attacks much harder:
    - IP address (from X-Forwarded-For or direct connection)
    - Client fingerprint (hash of User-Agent + Accept-Language headers)
    - Session ID (when available)

    This defense-in-depth approach means attackers would need to rotate all
    factors simultaneously to bypass rate limiting.

    Additionally implements:
    - Per-IP secondary rate limiting (stricter limit applied even when fingerprint rotates)
    - Gradual emergency purge (removes only truly stale entries, not arbitrary half)
    - New client rate limiting (prevents rapid creation of new identifiers)

    Implements bounded memory usage with aggressive cleanup.
    Thread-safe for use in async environments with concurrent requests.
    """

    # Maximum number of tracked clients to prevent memory exhaustion
    MAX_TRACKED_CLIENTS = 10000
    # Cleanup threshold - trigger cleanup when approaching max
    CLEANUP_THRESHOLD = 8000
    # Minimum age for emergency purge (only remove entries older than this)
    EMERGENCY_PURGE_MIN_AGE_SECONDS = 60
    # Maximum new clients per minute (to prevent identifier flooding)
    MAX_NEW_CLIENTS_PER_MINUTE = 100

    def __init__(
        self,
        requests_per_minute: int = 30,
        burst: int = 10,
        cleanup_interval: float = 60.0,
        per_ip_requests_per_minute: int | None = None,
    ):
        self.rate = requests_per_minute / 60.0  # requests per second
        self.burst = burst
        self._tokens: dict[str, float] = defaultdict(lambda: float(burst))
        self._last_update: dict[str, float] = defaultdict(time.monotonic)
        self._cleanup_interval = cleanup_interval
        self._last_cleanup = time.monotonic()
        # Lock to ensure thread-safety for concurrent async requests
        self._lock = threading.Lock()

        # Secondary per-IP rate limiting (stricter, cannot be bypassed by rotating fingerprint)
        # Default: 3x the normal rate to allow legitimate users with multiple sessions
        self._per_ip_rate = (per_ip_requests_per_minute or (requests_per_minute * 3)) / 60.0
        self._per_ip_burst = burst * 3
        self._ip_tokens: dict[str, float] = defaultdict(lambda: float(self._per_ip_burst))
        self._ip_last_update: dict[str, float] = defaultdict(time.monotonic)

        # Track new client creation rate to detect flooding attacks
        self._new_client_timestamps: list[float] = []

    @staticmethod
    def _get_client_fingerprint(request: Request) -> str:
        """Generate a fingerprint from browser headers to identify clients.

        This makes it harder to bypass rate limiting by just spoofing X-Forwarded-For,
        as the attacker would also need to rotate User-Agent and Accept-Language.

        Uses a hash to prevent the fingerprint from leaking header details in logs.
        """
        import hashlib

        user_agent = request.headers.get("user-agent", "")
        accept_language = request.headers.get("accept-language", "")
        accept_encoding = request.headers.get("accept-encoding", "")

        # Create a fingerprint hash from multiple headers
        fingerprint_data = f"{user_agent}|{accept_language}|{accept_encoding}"
        return hashlib.sha256(fingerprint_data.encode()).hexdigest()[:16]

    @staticmethod
    def get_client_ip(request: Request) -> str:
        """Extract client IP from request, respecting X-Forwarded-For header.

        X-Forwarded-For format: client, proxy1, proxy2, ...
        We take the first (leftmost) IP which is the original client.

        Note: X-Forwarded-For can be spoofed. Use get_client_identifier() for
        robust rate limiting that combines multiple factors.
        """
        # Check X-Forwarded-For header first (set by reverse proxies)
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            # Take the first IP (original client)
            client_ip = forwarded_for.split(",")[0].strip()
            if client_ip:
                return client_ip

        # Fall back to direct connection IP
        return request.client.host if request.client else "unknown"

    @classmethod
    def get_client_identifier(cls, request: Request) -> str:
        """Get a robust client identifier combining multiple factors.

        This combines:
        1. IP address (can be spoofed via X-Forwarded-For)
        2. Client fingerprint (harder to spoof - requires rotating multiple headers)
        3. Session ID (when authenticated - impossible to spoof without valid session)

        An attacker would need to control all three factors to bypass rate limiting.
        """
        ip = cls.get_client_ip(request)
        fingerprint = cls._get_client_fingerprint(request)

        # Try to get session ID if available (most reliable identifier)
        session_id = ""
        if hasattr(request, "session") and request.session:
            # Use session ID if present - this is the most reliable identifier
            session_id = request.session.get("_session_id", "")[:32] if request.session else ""

        # Combine factors into a single identifier
        # Format: ip|fingerprint|session (session may be empty for unauthenticated)
        return f"{ip}|{fingerprint}|{session_id}"

    def _check_new_client_rate_unlocked(self) -> bool:
        """Check if we're creating new clients too fast (flooding attack detection).

        Must be called while holding self._lock.

        Returns:
            True if a new client can be created, False if rate exceeded.
        """
        now = time.monotonic()
        # Remove timestamps older than 1 minute
        self._new_client_timestamps = [ts for ts in self._new_client_timestamps if now - ts < 60]

        if len(self._new_client_timestamps) >= self.MAX_NEW_CLIENTS_PER_MINUTE:
            logger.warning(
                f"New client rate limit exceeded: {len(self._new_client_timestamps)} new clients in last minute"
            )
            return False

        return True

    def _check_per_ip_limit_unlocked(self, ip: str) -> bool:
        """Check secondary per-IP rate limit.

        This provides a backstop against attackers who rotate fingerprints but
        cannot rotate their actual IP address.

        Must be called while holding self._lock.
        """
        now = time.monotonic()
        elapsed = now - self._ip_last_update[ip]
        self._ip_last_update[ip] = now

        # Add tokens based on time elapsed
        self._ip_tokens[ip] = min(self._per_ip_burst, self._ip_tokens[ip] + elapsed * self._per_ip_rate)

        if self._ip_tokens[ip] >= 1:
            self._ip_tokens[ip] -= 1
            return True
        return False

    def is_allowed(self, client_id: str) -> bool:
        """Check if request from client is allowed. Thread-safe.

        Applies two levels of rate limiting:
        1. Per-client-identifier limit (primary, based on IP+fingerprint+session)
        2. Per-IP limit (secondary, cannot be bypassed by rotating fingerprint)

        Args:
            client_id: Client identifier from get_client_identifier() or get_client_ip()
        """
        with self._lock:
            now = time.monotonic()

            # Extract IP from client_id for secondary rate limiting
            ip = client_id.split("|")[0] if "|" in client_id else client_id

            # Check if this is a new client (for flooding detection)
            is_new_client = client_id not in self._last_update
            if is_new_client:
                if not self._check_new_client_rate_unlocked():
                    return False  # Too many new clients being created
                self._new_client_timestamps.append(now)

            elapsed = now - self._last_update[client_id]
            self._last_update[client_id] = now

            # Add tokens based on time elapsed
            self._tokens[client_id] = min(self.burst, self._tokens[client_id] + elapsed * self.rate)

            # Time-based periodic cleanup
            if now - self._last_cleanup > self._cleanup_interval:
                self._cleanup_old_entries_unlocked()
                self._last_cleanup = now
            # Emergency cleanup if approaching memory limit
            elif len(self._tokens) > self.CLEANUP_THRESHOLD:
                self._cleanup_old_entries_unlocked(max_age_seconds=300)
                # If still over limit after cleanup, do gradual purge
                if len(self._tokens) > self.MAX_TRACKED_CLIENTS:
                    self._gradual_purge_unlocked()

            # Primary check: per-client-identifier limit
            if self._tokens[client_id] < 1:
                return False

            # Secondary check: per-IP limit (cannot be bypassed by rotating fingerprint)
            if not self._check_per_ip_limit_unlocked(ip):
                logger.debug(f"Per-IP rate limit exceeded for {ip}")
                return False

            self._tokens[client_id] -= 1
            return True

    def cleanup_old_entries(self, max_age_seconds: int = 600) -> None:
        """Remove entries older than max_age_seconds to prevent memory leak. Thread-safe.

        Default reduced to 10 minutes (was 1 hour) for more aggressive cleanup.
        """
        with self._lock:
            self._cleanup_old_entries_unlocked(max_age_seconds)

    def _cleanup_old_entries_unlocked(self, max_age_seconds: int = 600) -> None:
        """Internal cleanup method. Must be called while holding self._lock."""
        now = time.monotonic()
        to_remove = [ip for ip, last in self._last_update.items() if now - last > max_age_seconds]
        for ip in to_remove:
            del self._tokens[ip]
            del self._last_update[ip]

        # Also cleanup per-IP tracking
        ip_to_remove = [ip for ip, last in self._ip_last_update.items() if now - last > max_age_seconds]
        for ip in ip_to_remove:
            del self._ip_tokens[ip]
            del self._ip_last_update[ip]

        if to_remove or ip_to_remove:
            logger.debug(
                f"Rate limiter cleanup: removed {len(to_remove)} client entries, {len(ip_to_remove)} IP entries"
            )

    def _gradual_purge_unlocked(self) -> None:
        """Gradual purge when memory limit exceeded. Only removes truly stale entries.

        Unlike emergency_purge which removed arbitrary half, this only removes entries
        that are older than EMERGENCY_PURGE_MIN_AGE_SECONDS. This prevents attackers
        from evicting legitimate users by flooding with new identifiers.

        Must be called while holding self._lock.
        """
        if not self._last_update:
            return

        now = time.monotonic()
        min_age = self.EMERGENCY_PURGE_MIN_AGE_SECONDS

        # Only remove entries older than minimum age
        to_remove = [client_id for client_id, last_update in self._last_update.items() if now - last_update > min_age]

        for client_id in to_remove:
            del self._tokens[client_id]
            del self._last_update[client_id]

        if to_remove:
            logger.warning(f"Rate limiter gradual purge: removed {len(to_remove)} stale entries (>{min_age}s old)")

        # If still over limit after gradual purge, we have a real problem
        # Log it but don't remove recent entries (they're likely legitimate)
        if len(self._tokens) > self.MAX_TRACKED_CLIENTS:
            logger.error(
                f"Rate limiter still over limit ({len(self._tokens)} entries) after gradual purge. "
                f"Possible DoS attack or configuration issue."
            )


# Global rate limiter instance for subdomain checks (30 requests/minute per IP)
subdomain_check_rate_limiter = IPRateLimiter(requests_per_minute=30, burst=10)


class ProjectProcessRequest(BaseModel):
    project_file_path: str


class ProjectRepository(BaseModel):
    url: str
    username: str
    password: str
    branch: str = "main"
    path: str = "."


class ProjectComponent(BaseModel):
    name: str
    inbound: str
    outbound: str


class ProjectDeployment(BaseModel):
    name: str
    cluster: str
    image: str


class ProjectCreateRequest(BaseModel):
    projectName: str
    cluster: str
    repository: ProjectRepository
    component: ProjectComponent
    deployment: ProjectDeployment


class BasicProjectCreateRequest(BaseModel):
    projectName: str
    description: str | None = None
    cluster: str
    imageUrl: str
    appPort: int | None = None
    userEnvVars: str | None = None
    exposeWeb: bool = False
    ssoRijk: bool = False
    persistentStorage: bool = False
    ephemeralStorage: bool = False
    sharedRigDatabase: bool = False


class ComponentReference(BaseModel):
    reference: str = Field(..., max_length=63, description="Component reference name", example="frontend")
    image: str = Field(..., max_length=512, description="Image URL for this component", example="nginx:1.21")


class UpsertDeploymentRequest(BaseModel):
    deploymentName: str = Field(..., max_length=63, description="Name of the deployment", example="production")
    components: list[ComponentReference] = Field(..., description="List of components for this deployment")
    cloneFrom: str | None = Field(
        None, description="Deployment name to clone data from (only on create, or if forceClone is true)"
    )
    forceClone: bool = Field(False, description="Force clone even if target resources exist (runtime parameter)")

    model_config = {
        "json_schema_extra": {
            "example": {
                "deploymentName": "production",
                "components": [
                    {"reference": "frontend", "image": "ghcr.io/minbzk/amt:pr-597"},
                    {"reference": "backend", "image": "ghcr.io/minbzk/amt-api:v1.2.0"},
                ],
                "cloneFrom": "staging",
                "forceClone": False,
            }
        }
    }


class StorageAction(BaseModel):
    action: str = Field(..., description="Action to perform on the storage (recreate, keep)", example="recreate")


# Response Models


class DeploymentUrls(BaseModel):
    """URLs for a single deployment."""

    cluster: str = Field(..., description="Cluster where the deployment runs", example="local")
    urls: dict[str, str] = Field(
        ...,
        description="Component URLs (component name -> public URL)",
        example={"frontend": "https://frontend-main-myproject.rig.dev.local"},
    )


class DeploymentInfo(BaseModel):
    """Information about a deployment."""

    name: str = Field(..., description="Deployment name", example="main")
    project: str = Field(..., description="Project name", example="myproject")
    components: list[ComponentReference] = Field(..., description="Components in this deployment")
    forceClone: bool = Field(..., description="Whether force clone was used")
    created: bool = Field(..., description="True if deployment was newly created, False if updated")


class ProcessingStatus(BaseModel):
    """Processing status information."""

    status: str = Field(..., description="Processing status", example="completed")
    message: str | None = Field(None, description="Status message")
    result: Any | None = Field(None, description="Processing result details")


class UpsertDeploymentResponse(BaseModel):
    """Response for upsert deployment endpoint."""

    status: str = Field(..., description="Operation status", example="success")
    message: str = Field(..., description="Human-readable message", example="Deployment 'main' created successfully")
    deployment: DeploymentInfo = Field(..., description="Deployment information")
    urls: dict[str, DeploymentUrls] = Field(
        default_factory=dict,
        description="Public URLs per deployment",
        example={
            "main": {
                "cluster": "local",
                "urls": {"frontend": "https://frontend-main-myproject.rig.dev.local"},
            }
        },
    )
    processing: ProcessingStatus = Field(..., description="Processing status")

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "success",
                "message": "Deployment 'main' created successfully",
                "deployment": {
                    "name": "main",
                    "project": "myproject",
                    "components": [{"reference": "frontend", "image": "nginx:latest"}],
                    "forceClone": False,
                    "created": True,
                },
                "urls": {
                    "main": {
                        "cluster": "local",
                        "urls": {"frontend": "https://frontend-main-myproject.rig.dev.local"},
                    }
                },
                "processing": {"status": "completed"},
            }
        }
    }


class ProjectInfo(BaseModel):
    """Project information."""

    name: str = Field(..., description="Project name", example="myproject")
    file_path: str = Field(..., description="Path to project YAML file", example="projects/myproject.yaml")


class RefreshProjectResponse(BaseModel):
    """Response for refresh project endpoint."""

    status: str = Field(..., description="Operation status", example="success")
    message: str = Field(
        ..., description="Human-readable message", example="Project 'myproject' refreshed and processed successfully"
    )
    project: ProjectInfo = Field(..., description="Project information")
    urls: dict[str, DeploymentUrls] = Field(
        default_factory=dict,
        description="Public URLs per deployment",
        example={
            "main": {
                "cluster": "local",
                "urls": {
                    "frontend": "https://frontend-main-myproject.rig.dev.local",
                    "api": "https://api-main-myproject.rig.dev.local",
                },
            },
            "staging": {
                "cluster": "staging",
                "urls": {"frontend": "https://frontend-staging-myproject.rig.dev.local"},
            },
        },
    )
    processing: ProcessingStatus = Field(..., description="Processing status")

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "success",
                "message": "Project 'myproject' refreshed and processed successfully",
                "project": {"name": "myproject", "file_path": "projects/myproject.yaml"},
                "urls": {
                    "main": {
                        "cluster": "local",
                        "urls": {
                            "frontend": "https://frontend-main-myproject.rig.dev.local",
                            "api": "https://api-main-myproject.rig.dev.local",
                        },
                    }
                },
                "processing": {
                    "status": "completed",
                    "message": "All project resources processed successfully",
                },
            }
        }
    }


class ServiceReference(BaseModel):
    reference: dict[str, StorageAction] = Field(
        ..., description="Storage references with actions. Key is storage name (e.g., 'data', 'temp')"
    )


class UpdateImageRequest(BaseModel):
    componentName: str = Field(..., description="Name of the component to update", example="frontend")
    newImageUrl: str = Field(..., description="New image URL", example="nginx:1.21")
    registry: str | None = Field(
        None,
        max_length=63,
        description="Registry name to use for pulling this image (must be defined in project registries). "
        "When set, links the deployment component to the registry for imagePullSecret creation.",
    )
    services: dict[str, ServiceReference] | None = Field(
        None,
        description="Service-specific actions for storage recreation. Key is service type (e.g., 'persistent-storage')",
        example={"persistent-storage": {"reference": {"data": {"action": "recreate"}}}},
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"componentName": "frontend", "newImageUrl": "nginx:1.21"},
                {
                    "componentName": "frontend",
                    "newImageUrl": "registry.example.com/myorg/frontend:v1.0",
                    "registry": "my-registry",
                },
                {
                    "componentName": "frontend",
                    "newImageUrl": "nginx:1.22",
                    "services": {"persistent-storage": {"reference": {"data": {"action": "recreate"}}}},
                },
            ]
        }
    }


class ProjectDeleteRequest(BaseModel):
    confirmDeletion: bool = Field(False, description="Safety flag - must be true to confirm deletion", example=True)
    force: bool = Field(
        False,
        description="Force deletion mode - continues on errors and cleans up stuck resources. "
        "Use when a previous deletion failed partially or resources are in an inconsistent state.",
        example=False,
    )

    model_config = {"json_schema_extra": {"example": {"confirmDeletion": True, "force": False}}}


class ChiselTunnelConfig(BaseModel):
    """
    Chisel tunnel configuration for accessing remote services.

    If provided, a Chisel tunnel will be established to access the remote service,
    allowing cloning from sources that are not directly accessible.
    """

    serverUrl: str = Field(
        ..., description="Chisel server URL in source cluster", example="https://chisel.source-cluster.example.com"
    )
    username: str = Field(..., description="Chisel authentication username", example="admin")
    password: str = Field(..., description="Chisel authentication password", example="secret")
    remoteHost: str = Field(
        ..., description="Remote service hostname in source cluster", example="postgres.namespace.svc.cluster.local"
    )
    remotePort: int = Field(..., description="Remote service port in source cluster", example=5432)


class CloneDatabaseFromExternalRequest(BaseModel):
    sourceHost: str | None = Field(
        None, description="External source database host (not needed if using tunnel)", example="localhost"
    )
    sourcePort: int | None = Field(
        None, description="External source database port (not needed if using tunnel)", example=15432
    )
    sourceUsername: str = Field(..., description="Username for external source connection", example="postgres")
    sourcePassword: str = Field(..., description="Password for external source connection", example="password")
    sourceDatabase: str = Field(..., description="Source database name", example="amt_staging")
    sourceSchema: str = Field(..., description="Source schema name", example="amt_staging")
    forceClone: bool = Field(False, description="If true, drop existing target database before cloning", example=True)
    tunnel: ChiselTunnelConfig | None = Field(
        None, description="Optional Chisel tunnel configuration for accessing remote source"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "sourceHost": "localhost",
                    "sourcePort": 15432,
                    "sourceUsername": "postgres",
                    "sourcePassword": "password",
                    "sourceDatabase": "amt_staging",
                    "sourceSchema": "amt_staging",
                    "forceClone": True,
                },
                {
                    "sourceUsername": "postgres",
                    "sourcePassword": "password",
                    "sourceDatabase": "amt_staging",
                    "sourceSchema": "amt_staging",
                    "forceClone": True,
                    "tunnel": {
                        "serverUrl": "https://chisel.source-cluster.example.com",
                        "username": "admin",
                        "password": "secret",
                        "remoteHost": "postgres.namespace.svc.cluster.local",
                        "remotePort": 5432,
                    },
                },
            ]
        }
    }


class CloneBucketFromExternalRequest(BaseModel):
    sourceHost: str | None = Field(
        None, description="External source MinIO host (not needed if using tunnel)", example="localhost"
    )
    sourcePort: int | None = Field(
        None, description="External source MinIO port (not needed if using tunnel)", example=19000
    )
    sourceAccessKey: str = Field(..., description="Access key for external source connection", example="minioadmin")
    sourceSecretKey: str = Field(..., description="Secret key for external source connection", example="minioadmin")
    sourceBucket: str = Field(..., description="Source bucket name", example="amt-staging")
    sourceSecure: bool = Field(False, description="Whether source uses HTTPS", example=False)
    forceClone: bool = Field(False, description="If true, overwrite existing target bucket", example=True)
    tunnel: ChiselTunnelConfig | None = Field(
        None, description="Optional Chisel tunnel configuration for accessing remote source"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "sourceHost": "localhost",
                    "sourcePort": 19000,
                    "sourceAccessKey": "minioadmin",
                    "sourceSecretKey": "minioadmin",
                    "sourceBucket": "amt-staging",
                    "sourceSecure": False,
                    "forceClone": True,
                },
                {
                    "sourceAccessKey": "minioadmin",
                    "sourceSecretKey": "minioadmin",
                    "sourceBucket": "amt-staging",
                    "sourceSecure": False,
                    "forceClone": True,
                    "tunnel": {
                        "serverUrl": "https://chisel.source-cluster.example.com",
                        "username": "admin",
                        "password": "secret",
                        "remoteHost": "minio.namespace.svc.cluster.local",
                        "remotePort": 9000,
                    },
                },
            ]
        }
    }


class DeploymentDomainSettingsRequest(BaseModel):
    """Request model for updating deployment domain settings."""

    domain_mode: str = Field(
        ...,
        description="URL mode: 'component-specific', 'deployment-name', 'custom', or 'nice-url'",
        example="nice-url",
        max_length=32,
    )
    subdomain: str | None = Field(
        None,
        description="Subdomain for nice-url or custom mode",
        example="myapp",
        max_length=63,  # DNS subdomain limit
    )
    base_domain: str | None = Field(
        None,
        description="Base domain for nice-url mode (e.g., 'rijks.app')",
        example="rijks.app",
        max_length=255,  # DNS domain limit
    )
    root_component: str | None = Field(
        None,
        description="Component reference to mark as root (receives / path)",
        example="frontend",
        max_length=63,  # Kubernetes name limit
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "domain_mode": "nice-url",
                    "subdomain": "myapp",
                    "base_domain": "rijks.app",
                    "root_component": "frontend",
                },
                {
                    "domain_mode": "component-specific",
                },
            ]
        }
    }


class DeploymentDomainSettingsResponse(BaseModel):
    """Response model for deployment domain settings."""

    deployment_name: str = Field(..., description="Name of the deployment")
    cluster: str = Field(..., description="Cluster where deployment runs")
    domain_mode: str | None = Field(None, description="Current URL mode")
    subdomain: str | None = Field(None, description="Current subdomain (if nice-url or custom)")
    base_domain: str | None = Field(None, description="Current base domain (if nice-url)")
    root_component: str | None = Field(None, description="Component marked as root")
    components: list[dict] = Field(default_factory=list, description="List of components in deployment")
    supported_base_domains: list[dict] = Field(
        default_factory=list, description="Supported base domains for this cluster"
    )


class SelfServiceComponent(BaseModel):
    type: str = Field(..., max_length=32)  # "deployment", "cronjob", "daemonset"
    port: int | None = Field(None, ge=1, le=65535)
    image: str = Field(..., max_length=512)
    path: str = Field("/", max_length=256)  # Publication path for ingress routing (e.g., "/", "/api", "/aanleverapi")
    cpu_limit: str | None = Field(None, max_length=16)  # e.g., "100m", "1000m"
    memory_limit: str | None = Field(None, max_length=16)  # e.g., "128Mi", "1Gi"
    env_vars: str | None = Field(None, max_length=65536)  # Environment variables in KEY=value format
    aliases: str | None = Field(None, max_length=4096)  # Aliases for system-provided variables (not encoded)
    services: list[str] | None = None  # ["keycloak", "postgres", "minio"]
    root: bool = False  # Whether this component receives the root path in nice-url mode


class AddRegistryBySecretRequest(BaseModel):
    """Request to add a registry that references a pre-existing Kubernetes secret."""

    name: str = Field(..., max_length=63, description="Unique registry identifier")
    url: str = Field(..., max_length=512, description="Registry URL without protocol (may include path)")
    secret_name: str = Field(
        ..., max_length=253, alias="secretName", description="Name of existing K8s dockerconfigjson secret"
    )


class AddRegistryByCredentialsRequest(BaseModel):
    """Request to add a registry with username/password credentials."""

    name: str = Field(..., max_length=63, description="Unique registry identifier")
    url: str = Field(..., max_length=512, description="Registry URL without protocol (may include path)")
    username: str = Field(..., max_length=256, description="Registry username or token name")
    password: str = Field(..., max_length=4096, description="Registry password or token (will be AGE-encrypted)")


class AddComponentRequest(BaseModel):
    """Request to add a new component definition to an existing project."""

    name: str = Field(..., max_length=63, description="Component name (must be K8s-compliant)")
    type: str = Field("single", max_length=32, description="Component type (e.g. 'single', 'frontend', 'backend')")
    image: str = Field(..., max_length=512, description="Container image URL")
    port: int | None = Field(None, ge=1, le=65535, description="Inbound port (omit for background workers)")
    path: str = Field("/", max_length=256, description="Ingress path (only relevant with publish-on-web service)")
    services: list[str] | None = Field(
        None, description="Component uses-services list (e.g. ['postgresql-database']). NOT inherited from project."
    )
    cpu_limit: str | None = Field(None, max_length=16, description="CPU limit, e.g. '500m'")
    memory_limit: str | None = Field(None, max_length=16, description="Memory limit, e.g. '512Mi'")
    env_vars: str | None = Field(
        None, max_length=65536, description="User env vars in KEY=value format (will be encrypted)"
    )
    aliases: str | None = Field(
        None,
        max_length=4096,
        description="YAML string of alias definitions (e.g. 'DATABASE_URL: $HOST:$PORT/$DB_NAME')",
    )
    root: bool = Field(False, description="Mark as root component for nice-url mode (receives bare subdomain traffic)")
    deployment_names: list[str] = Field(
        ..., min_length=1, description="Deployments to add this component to (must already exist)"
    )


class AddComponentToDeploymentRequest(BaseModel):
    """Request to add an existing component to a deployment that doesn't yet include it."""

    component_name: str = Field(..., max_length=63, description="Name of an existing component in the project")
    image: str = Field(..., max_length=512, description="Container image URL for this deployment")


class SelfServiceProjectRequest(BaseModel):
    # Project Details (from form fields)
    project_name: str = Field(..., max_length=63)  # Generated technical name (short, compliant)
    display_name: str = Field(..., max_length=128)  # User-friendly name from form (maps to name="display-name")
    project_description: str | None = Field(None, max_length=1024)  # Maps to name="project-description"
    cluster: str = Field(..., max_length=63)  # Maps to name="cluster"
    deployment_name: str = Field("main", max_length=63)  # Name for the deployment (defaults to "main")

    # Web Address Configuration
    domain_mode: str = Field(
        "component-specific", max_length=32
    )  # "component-specific", "deployment-name", "custom", or "nice-url"
    subdomain: str | None = Field(
        None, max_length=63
    )  # For nice-url mode: globally unique subdomain. For custom mode: custom subdomain

    # External Domain Configuration (for public domains with Let's Encrypt)
    base_domain: str | None = Field(None, max_length=255)  # Apex domain (e.g., "rijks.app")
    issuer: str | None = Field(
        None, max_length=64
    )  # Certificate issuer: "letsencrypt", "letsencrypt-staging", or custom issuer name
    contact_email: str | None = Field(
        None, max_length=254
    )  # Contact email for Let's Encrypt (overrides cluster default)

    # Users (from array fields)
    user_email: list[str] | None = None  # Maps to name="user-email[]"
    user_role: list[str] | None = None  # Maps to name="user-role[]"

    # Services (checkboxes)
    services: list[str] | None = None  # Maps to name="services[]"

    # Components array
    components: list[SelfServiceComponent] | None = None


api_router: APIRouter = APIRouter(
    prefix="/api",
    tags=["projects"],
    responses={404: {"description": "Not found"}},
    default_response_class=JSONResponse,
)


@api_router.post(
    "/projects/{project_name}/:upsert-deployment",
    responses={
        200: {"model": UpsertDeploymentResponse, "description": "Deployment updated successfully"},
        201: {"model": UpsertDeploymentResponse, "description": "Deployment created successfully"},
    },
)
@validate_api_token
async def upsert_deployment(
    request: Request, project_name: str, deployment_data: UpsertDeploymentRequest = Body(...)
) -> JSONResponse:
    """
    Create or update a deployment in an existing project.

    If the deployment doesn't exist, it will be created. If it exists, the component
    images will be updated. The cloneFrom parameter is only used when creating a new
    deployment, or when updating with forceClone set to true.

    Headers:
        X-API-Key: The API key for the project (required)

    Example:
    ```bash
    curl -X POST "http://localhost:9595/api/projects/my-project/:upsert-deployment" \
      -H "Content-Type: application/json" \
      -H "X-API-Key: your-api-key" \
      -d '{
        "deploymentName": "production",
        "components": [
          {"reference": "frontend", "image": "ghcr.io/minbzk/amt:pr-597"}
        ],
        "cloneFrom": "staging",
        "forceClone": false
      }'
    ```
    """
    project_manager = None
    try:
        logger.info(f"Upserting deployment '{deployment_data.deploymentName}' to project: {project_name}")

        # Validate project name format
        if not validate_project_name(project_name):
            raise HTTPException(
                status_code=400,
                detail="Invalid project name format. Must start with lowercase letter, then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters",
            )

        # Validate deployment name using naming utilities
        sanitized_name = sanitize_kubernetes_name(deployment_data.deploymentName)
        if sanitized_name != deployment_data.deploymentName.lower():
            raise HTTPException(
                status_code=400,
                detail=f"Invalid deployment name. Use lowercase letters, numbers, and hyphens only. Suggested: {sanitized_name}",
            )

        # Create project manager instance
        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")

        # Upsert the deployment in the project YAML
        result = await project_manager.upsert_deployment(
            deployment_name=deployment_data.deploymentName,
            components=deployment_data.components,
            clone_from=deployment_data.cloneFrom,
            force_clone=deployment_data.forceClone,
        )

        if result["success"]:
            # Process the deployment
            processing_result = await project_manager.process_project_from_git(
                f"projects/{project_name}.yaml",
                deployment_name=deployment_data.deploymentName,
                force_clone=deployment_data.forceClone,
            )

            # Determine status code based on whether it was created or updated
            status_code = 201 if result.get("created") else 200
            action = "created" if result.get("created") else "updated"

            # Get URLs from deployment results collected during processing
            urls: dict[str, dict[str, Any]] = {}
            deployment_results = project_manager.get_deployment_results(deployment_data.deploymentName)
            for dep_name, dep_result in deployment_results.items():
                urls[dep_name] = {
                    "cluster": dep_result.cluster,
                    "urls": dep_result.urls,
                }

            content: dict[str, Any] = {
                "status": "success",
                "message": f"Deployment '{deployment_data.deploymentName}' {action} successfully",
                "deployment": {
                    "name": deployment_data.deploymentName,
                    "project": project_name,
                    "components": [
                        {"reference": c.reference, "image": normalize_container_image(c.image)[0]}
                        for c in deployment_data.components
                    ],
                    "forceClone": deployment_data.forceClone,
                    "created": result.get("created", False),
                },
                "urls": urls,
                "processing": {"status": "completed" if processing_result else "failed"},
            }
            if result.get("warnings"):
                content["warnings"] = result["warnings"]
            return JSONResponse(content=content, status_code=status_code)
        else:
            # Determine appropriate HTTP status code based on error type
            error_status_codes = {
                "invalid_component_references": 400,  # Bad Request
                "ambiguous_repository": 400,  # Bad Request
                "no_repositories": 422,  # Unprocessable Entity
                "internal_error": 500,  # Internal Server Error
            }
            status_code = error_status_codes.get(result.get("error_type"), 400)

            content = {
                "status": "failed",
                "message": f"Failed to upsert deployment '{deployment_data.deploymentName}'",
                "error": result["error"],
                "error_type": result["error_type"],
            }
            return JSONResponse(content=content, status_code=status_code)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error upserting deployment: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error upserting deployment: {e!s}")
    finally:
        if project_manager:
            await project_manager.close()


@api_router.post(
    "/projects/{project_name}/components",
    responses={
        201: {"description": "Component added successfully"},
    },
)
@validate_api_token
async def add_component(
    request: Request, project_name: str, component_data: AddComponentRequest = Body(...)
) -> JSONResponse:
    """
    Add a new component definition to an existing project.

    The component is added to the project's components array and referenced in
    the specified deployments. Each component declares its own uses-services list,
    which determines what secrets/env vars it receives.

    Headers:
        X-API-Key: The API key for the project (required)

    Example:
    ```bash
    curl -X POST "http://localhost:9595/api/projects/my-project/components" \\
      -H "Content-Type: application/json" \\
      -H "X-API-Key: your-api-key" \\
      -d '{
        "name": "worker",
        "type": "deployment",
        "image": "ghcr.io/myorg/worker:latest",
        "services": ["postgresql-database"],
        "deployment_names": ["main"]
      }'
    ```
    """
    project_manager = None
    try:
        logger.info(
            f"Adding component '{sanitize_for_log(component_data.name)}' to project: {sanitize_for_log(project_name)}"
        )

        # Validate project name format
        if not validate_project_name(project_name):
            raise HTTPException(
                status_code=400,
                detail="Invalid project name format. Must start with lowercase letter, then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters",
            )

        # Validate component name
        sanitized_name = sanitize_kubernetes_name(component_data.name)
        if sanitized_name != component_data.name.lower():
            raise HTTPException(
                status_code=400,
                detail=f"Invalid component name. Use lowercase letters, numbers, and hyphens only. Suggested: {sanitized_name}",
            )

        # Create project manager instance
        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")

        # Add the component
        result = await project_manager.add_component(
            name=component_data.name,
            component_type=component_data.type,
            image=component_data.image,
            deployment_names=component_data.deployment_names,
            port=component_data.port,
            path=component_data.path,
            services=component_data.services,
            cpu_limit=component_data.cpu_limit,
            memory_limit=component_data.memory_limit,
            env_vars=component_data.env_vars,
            aliases=component_data.aliases,
            root=component_data.root,
        )

        if result["success"]:
            # Process the project to create K8s resources for affected deployments
            processing_success = True
            for dep_name in result.get("deployments_updated", []):
                dep_result = await project_manager.process_project_from_git(
                    f"projects/{project_name}.yaml",
                    deployment_name=dep_name,
                )
                if not dep_result:
                    processing_success = False

            # Collect URLs from deployment results
            urls: dict[str, dict[str, Any]] = {}
            for dep_name in result.get("deployments_updated", []):
                deployment_results = project_manager.get_deployment_results(dep_name)
                for name, dep_result in deployment_results.items():
                    urls[name] = {
                        "cluster": dep_result.cluster,
                        "urls": dep_result.urls,
                    }

            content: dict[str, Any] = {
                "status": "success",
                "message": f"Component '{component_data.name}' added successfully",
                "component": result["component"],
                "deployments_updated": result.get("deployments_updated", []),
                "urls": urls,
                "processing": {"status": "completed" if processing_success else "failed"},
            }
            if result.get("warnings"):
                content["warnings"] = result["warnings"]
            return JSONResponse(content=content, status_code=201)
        else:
            error_status_codes = {
                "duplicate_component": 409,
                "invalid_deployments": 400,
                "internal_error": 500,
            }
            status_code = error_status_codes.get(result.get("error_type"), 400)

            content = {
                "status": "failed",
                "message": f"Failed to add component '{component_data.name}'",
                "error": result["error"],
                "error_type": result["error_type"],
            }
            return JSONResponse(content=content, status_code=status_code)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding component: {e!s}")
        raise HTTPException(status_code=500, detail="An internal error occurred")
    finally:
        if project_manager:
            await project_manager.close()


@api_router.post(
    "/projects/{project_name}/deployments/{deployment_name}/components",
    responses={
        201: {"description": "Component added to deployment successfully"},
    },
)
@validate_api_token
async def add_component_to_deployment(
    request: Request,
    project_name: str,
    deployment_name: str,
    component_data: AddComponentToDeploymentRequest = Body(...),
) -> JSONResponse:
    """
    Add an existing component to a deployment that doesn't yet include it.

    The component must already be defined in the project's components array.
    This endpoint adds a reference to it in the specified deployment.

    Headers:
        X-API-Key: The API key for the project (required)

    Example:
    ```bash
    curl -X POST "http://localhost:9595/api/projects/my-project/deployments/staging/components" \\
      -H "Content-Type: application/json" \\
      -H "X-API-Key: your-api-key" \\
      -d '{
        "component_name": "backend",
        "image": "ghcr.io/myorg/backend:latest"
      }'
    ```
    """
    project_manager = None
    try:
        logger.info(
            f"Adding component '{sanitize_for_log(component_data.component_name)}' "
            f"to deployment '{sanitize_for_log(deployment_name)}' "
            f"in project: {sanitize_for_log(project_name)}"
        )

        # Validate project name format
        if not validate_project_name(project_name):
            raise HTTPException(
                status_code=400,
                detail="Invalid project name format. Must start with lowercase letter, then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters",
            )

        # Validate component name
        sanitized_name = sanitize_kubernetes_name(component_data.component_name)
        if sanitized_name != component_data.component_name.lower():
            raise HTTPException(
                status_code=400,
                detail=f"Invalid component name. Use lowercase letters, numbers, and hyphens only. Suggested: {sanitized_name}",
            )

        # Create project manager instance
        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")

        # Add the component to the deployment
        result = await project_manager.add_component_to_deployment(
            deployment_name=deployment_name,
            component_name=component_data.component_name,
            image=component_data.image,
        )

        if result["success"]:
            # Process the project to create K8s resources for the affected deployment
            processing_success = await project_manager.process_project_from_git(
                f"projects/{project_name}.yaml",
                deployment_name=deployment_name,
            )

            # Collect URLs from deployment results
            urls: dict[str, dict[str, Any]] = {}
            deployment_results = project_manager.get_deployment_results(deployment_name)
            for name, dep_result in deployment_results.items():
                urls[name] = {
                    "cluster": dep_result.cluster,
                    "urls": dep_result.urls,
                }

            content: dict[str, Any] = {
                "status": "success",
                "message": f"Component '{component_data.component_name}' added to deployment '{deployment_name}'",
                "deployment": deployment_name,
                "component_reference": result["component_reference"],
                "urls": urls,
                "processing": {"status": "completed" if processing_success else "failed"},
            }
            if result.get("warnings"):
                content["warnings"] = result["warnings"]
            return JSONResponse(content=content, status_code=201)
        else:
            error_status_codes = {
                "deployment_not_found": 404,
                "component_not_found": 400,
                "duplicate_component_in_deployment": 409,
                "validation_error": 400,
                "internal_error": 500,
            }
            status_code = error_status_codes.get(result.get("error_type"), 400)

            content = {
                "status": "failed",
                "message": f"Failed to add component '{component_data.component_name}' to deployment '{deployment_name}'",
                "error": result["error"],
                "error_type": result["error_type"],
            }
            return JSONResponse(content=content, status_code=status_code)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding component to deployment: {e!s}")
        raise HTTPException(status_code=500, detail="An internal error occurred")
    finally:
        if project_manager:
            await project_manager.close()


@api_router.put("/projects/{project_name}/deployments/{deployment_name}/image")
@validate_api_token
async def update_deployment_image(
    request: Request, project_name: str, deployment_name: str, image_data: UpdateImageRequest = Body(...)
) -> JSONResponse:
    """
    Update the container image for a specific component in a deployment.

    Headers:
        X-API-Key: The API key for the project (required)

    Examples:

    Basic image update:
    ```bash
    curl -X PUT "http://localhost:9595/api/projects/my-project/deployments/staging/image" \\
      -H "Content-Type: application/json" \\
      -H "X-API-Key: your-api-key" \\
      -d '{
        "componentName": "frontend",
        "newImageUrl": "nginx:1.21"
      }'
    ```

    Image update with private registry:
    ```bash
    curl -X PUT "http://localhost:9595/api/projects/my-project/deployments/staging/image" \\
      -H "Content-Type: application/json" \\
      -H "X-API-Key: your-api-key" \\
      -d '{
        "componentName": "frontend",
        "newImageUrl": "registry.example.com/myorg/frontend:v1.0",
        "registry": "my-registry"
      }'
    ```

    Image update with storage recreation:
    ```bash
    curl -X PUT "http://localhost:9595/api/projects/my-project/deployments/staging/image" \\
      -H "Content-Type: application/json" \\
      -H "X-API-Key: your-api-key" \\
      -d '{
        "componentName": "frontend",
        "newImageUrl": "nginx:1.22",
        "services": {
          "persistent-storage": {
            "reference": {
              "data": {
                "action": "recreate"
              }
            }
          }
        }
      }'
    ```
    """
    project_manager = None
    try:
        logger.info(f"Updating image for component '{image_data.componentName}' in {project_name}/{deployment_name}")

        # Create project manager instance
        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")

        # Extract service actions if provided (e.g., persistent-storage recreate actions)
        # Convert Pydantic models to dicts for project_manager compatibility
        service_actions = None
        if image_data.services:
            service_actions = {
                service_type: service_ref.model_dump() for service_type, service_ref in image_data.services.items()
            }
            logger.info(f"Service actions requested: {service_actions}")

        # Handle the update-image action with optional service actions
        # TODO: this method exist because we do not 'diff' yet between project files
        #  in the future, a diff would show what has changed and we could determine what actions to take
        result = await project_manager.update_image_and_regenerate(
            deployment_name=deployment_name,
            component_name=image_data.componentName,
            new_image_url=image_data.newImageUrl,
            service_actions=service_actions,
            registry=image_data.registry,
        )

        content = {
            "status": result["status"],
            "message": result["message"],
            "project": project_name,
            "deployment": deployment_name,
            "component": image_data.componentName,
            "updates": result["updates"],
            "actions_performed": result["actions_performed"],
        }
        return JSONResponse(content=content, status_code=200)

    except Exception as e:
        logger.error(f"Error updating image: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error updating image: {e!s}")
    finally:
        if project_manager:
            await project_manager.close()


# @api_router.post("/projects")
# async def create_project(
#     request: Request, project_data: SelfServiceProjectRequest = Body(...)
# ) -> JSONResponse:
#     """
#     Create a new project from the self-service portal form.
#
#     Example:
#     ```bash
#     curl -X POST "http://localhost:9595/api/projects" \
#       -H "Content-Type: application/json" \
#       -d '{
#         "project_name": "my-project",
#         "display_name": "My Awesome Project",
#         "project_description": "Test project",
#         "cluster": "local",
#         "user_email": ["user@example.com"],
#         "user_role": ["Developer"],
#         "services": ["service-web", "service-sso"],
#         "components": [{
#           "type": "deployment",
#           "port": 8080,
#           "image": "nginx:latest"
#         }]
#       }'
#     ```
#     """
#     return await create_self_service_project(request, project_data)


@api_router.get(
    "/projects/{project_name}/:refresh",
    responses={
        200: {"model": RefreshProjectResponse, "description": "Project refreshed successfully"},
    },
)
@validate_api_token
async def refresh_project(request: Request, project_name: str, force_clone: bool = False) -> JSONResponse:
    """
    Refresh/retry a project deployment by reprocessing the project from its YAML file.

    Query Parameters:
        force_clone: Force clone even if target resources exist (default: False)

    Example:
    curl -X GET "http://localhost:9595/api/projects/example-name/:refresh?force_clone=true" \
      -H "Content-Type: application/json" \
      -H "X-API-Key: d68d6aebd694d636e5eb4784a952b9c3"
    """
    project_manager = None
    try:
        logger.info(f"Project refresh request for: {project_name} (force_clone={force_clone})")

        # Validate project name format
        if not validate_project_name(project_name):
            raise HTTPException(
                status_code=400,
                detail="Invalid project name format. Must start with lowercase letter, then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters",
            )

        # Get project information from project service
        project_service = get_project_service()
        project = project_service.get_project(project_name)

        if not project:
            raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found in project registry")

        # Create project manager instance
        project_manager = create_project_manager()

        # Use the actual filename from project service
        project_file_path = f"projects/{project.filename}"

        # Process the project file from Git (this will handle all the steps)
        processing_result = await project_manager.process_project_from_git(project_file_path, force_clone=force_clone)

        if processing_result:
            logger.info(f"Project refresh completed successfully: {project_name}")

            # Get URLs from deployment results collected during processing
            urls: dict[str, dict[str, Any]] = {}
            deployment_results = project_manager.get_deployment_results()
            for dep_name, dep_result in deployment_results.items():
                urls[dep_name] = {
                    "cluster": dep_result.cluster,
                    "urls": dep_result.urls,
                }

            content = {
                "status": "success",
                "message": f"Project '{project_name}' refreshed and processed successfully",
                "project": {"name": project_name, "file_path": project_file_path},
                "urls": urls,
                "processing": {
                    "status": "completed",
                    "message": "All project resources processed successfully",
                    "result": processing_result,
                },
            }
            return JSONResponse(content=content, status_code=200)
        else:
            logger.warning(f"Project refresh failed: {project_name}")

            content = {
                "status": "failed",
                "message": f"Project '{project_name}' refresh failed",
                "project": {"name": project_name, "file_path": project_file_path},
                "processing": {
                    "status": "failed",
                    "message": "Failed to process project resources",
                    "result": processing_result,
                },
            }
            return JSONResponse(content=content, status_code=500)
    except Exception as e:
        logger.error(f"Error processing project refresh request: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error refreshing project: {e!s}")
    finally:
        if project_manager:
            await project_manager.close()


@api_router.get(
    "/projects/{project_name}/deployments/{deployment_name}/:refresh",
    responses={
        200: {"model": RefreshProjectResponse, "description": "Deployment refreshed successfully"},
    },
)
@validate_api_token
async def refresh_deployment(
    request: Request, project_name: str, deployment_name: str, force_clone: bool = False
) -> JSONResponse:
    """
    Refresh a single deployment by reprocessing it from the project YAML file.

    This re-runs the provisioning steps for the specified deployment only,
    without affecting other deployments in the project.

    Query Parameters:
        force_clone: Force clone even if target resources exist (default: False)

    Example:
    curl -X GET "http://localhost:9595/api/projects/example-name/deployments/staging/:refresh" \\
      -H "X-API-Key: d68d6aebd694d636e5eb4784a952b9c3"
    """
    project_manager = None
    try:
        logger.info(f"Deployment refresh request for: {project_name}/{deployment_name} (force_clone={force_clone})")

        if not validate_project_name(project_name):
            raise HTTPException(
                status_code=400,
                detail="Invalid project name format. Must start with lowercase letter, then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters",
            )

        project_service = get_project_service()
        project = project_service.get_project(project_name)

        if not project:
            raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found in project registry")

        project_manager = create_project_manager()
        project_file_path = f"projects/{project.filename}"

        processing_result = await project_manager.process_project_from_git(
            project_file_path, deployment_name=deployment_name, force_clone=force_clone
        )

        if processing_result:
            logger.info(f"Deployment refresh completed successfully: {project_name}/{deployment_name}")

            urls: dict[str, dict[str, Any]] = {}
            deployment_results = project_manager.get_deployment_results()
            for dep_name, dep_result in deployment_results.items():
                urls[dep_name] = {
                    "cluster": dep_result.cluster,
                    "urls": dep_result.urls,
                }

            content = {
                "status": "success",
                "message": f"Deployment '{deployment_name}' in project '{project_name}' refreshed successfully",
                "project": {"name": project_name, "file_path": project_file_path},
                "urls": urls,
                "processing": {
                    "status": "completed",
                    "message": f"Deployment '{deployment_name}' processed successfully",
                    "result": processing_result,
                },
            }
            return JSONResponse(content=content, status_code=200)
        else:
            logger.warning(f"Deployment refresh failed: {project_name}/{deployment_name}")

            content = {
                "status": "failed",
                "message": f"Deployment '{deployment_name}' in project '{project_name}' refresh failed",
                "project": {"name": project_name, "file_path": project_file_path},
                "processing": {
                    "status": "failed",
                    "message": f"Failed to process deployment '{deployment_name}'",
                    "result": processing_result,
                },
            }
            return JSONResponse(content=content, status_code=500)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing deployment refresh request: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error refreshing deployment: {e!s}")
    finally:
        if project_manager:
            await project_manager.close()


@api_router.delete("/projects/{project_name}")
@validate_api_token
async def delete_project(
    request: Request, project_name: str, delete_data: ProjectDeleteRequest = Body(...)
) -> JSONResponse:
    """
    Delete a project and all its associated resources.

    This endpoint performs a complete cleanup of:
    1. Project YAML file from Git projects repository
    2. ArgoCD GitOps folders for all deployments/clusters
    3. Kubernetes namespaces for all deployments

    WARNING: This operation is irreversible and will permanently delete all project resources.

    Headers:
        X-API-Key: The API key for the project (required)

    Example curl command:
    ```
    curl -X DELETE "http://localhost:9595/api/projects/example-project" \
      -H "Content-Type: application/json" \
      -H "X-API-Key: your-api-key-here" \
      -d '{
        "confirmDeletion": true,
        "force": false
      }'
    ```

    Force mode (force: true):
    - Continues deletion even when some operations fail
    - Removes ArgoCD application finalizers if apps are stuck
    - Skips database cleanup if secrets are inaccessible
    - Use when a previous deletion failed partially

    Args:
        request: The FastAPI request object
        project_name: Name of the project to delete (from URL path)
        delete_data: Deletion confirmation data

    Returns:
        JSON response with detailed deletion results
    """
    project_manager = None
    try:
        force_mode = delete_data.force
        logger.info(f"Project deletion request for: {project_name} (force={force_mode})")

        # Safety check: require explicit confirmation
        if not delete_data.confirmDeletion:
            raise HTTPException(status_code=400, detail="Project deletion requires confirmDeletion to be set to true")

        # Get the project API key from headers
        project_api_key = request.headers.get("X-API-Key")
        if not project_api_key:
            raise HTTPException(status_code=401, detail="Missing X-API-Key header")

        # Create project manager instance to handle the deletion and validation
        project_manager = create_project_manager()

        # Perform the deletion
        deletion_results = await project_manager.delete_project(project_name, force=force_mode)

        # Determine response status code based on results
        if deletion_results["success"]:
            status_code = 200
            message = f"Project '{project_name}' deleted successfully"
        else:
            status_code = 207  # Multi-Status - partial success
            message = f"Project '{project_name}' deletion completed with some errors"

        content = {
            "status": "completed" if deletion_results["success"] else "partial",
            "message": message,
            "project": project_name,
            "deletion_results": deletion_results,
            "warning": "This deletion is permanent and cannot be undone",
        }

        logger.info(f"Project deletion completed for: {project_name} (success: {deletion_results['success']})")
        return JSONResponse(content=content, status_code=status_code)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing project deletion request: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error processing project deletion: {e!s}")
    finally:
        if project_manager:
            await project_manager.close()


@api_router.delete("/projects/{project_name}/{deployment_name}")
@validate_api_token
async def delete_project_deployment(request: Request, project_name: str, deployment_name: str) -> JSONResponse:
    """
    Delete a specific deployment within a project.

    This endpoint deletes a deployment and its associated resources using project-specific API keys.
    The API key is validated against the in-memory mapping of project IDs to API keys.

    Headers:
        X-API-Key: The API key for the project (required)

    Example curl command:
    ```
    curl -X DELETE "http://localhost:9595/api/my-project/staging" \
      -H "Content-Type: application/json" \
      -H "X-API-Key: your-project-api-key-here"
    ```

    Args:
        request: The FastAPI request object
        project_name: Name of the project (from URL path)
        deployment_name: Name of the deployment to delete (from URL path)
        project_id: Project ID extracted from API key validation (injected by decorator)

    Returns:
        JSON response with detailed deletion results
    """
    project_manager = None
    try:
        logger.info(f"Deployment deletion request for: {project_name}/{deployment_name} (project_id: {project_name})")

        # Create project manager instance to handle the deletion
        project_manager = create_project_manager()

        # Perform the deployment deletion
        deletion_results = await project_manager.delete_deployment(project_name, deployment_name)

        # Determine response status code based on results
        if deletion_results.get("success", False):
            status_code = 200
            message = f"Deployment '{deployment_name}' in project '{project_name}' deleted successfully"
        else:
            status_code = 207  # Multi-Status - partial success
            message = f"Deployment '{deployment_name}' deletion completed with some errors"

        content = {
            "status": "completed" if deletion_results.get("success", False) else "partial",
            "message": message,
            "project": project_name,
            "deployment": deployment_name,
            "deletion_results": deletion_results,
            "warning": "This deletion is permanent and cannot be undone",
        }

        logger.info(
            f"Deployment deletion completed for: {project_name}/{deployment_name} (success: {deletion_results.get('success', False)})"
        )
        return JSONResponse(content=content, status_code=status_code)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing deployment deletion request: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error processing deployment deletion: {e!s}")
    finally:
        # TODO: maybe the project manager should close itself when done..
        if project_manager:
            await project_manager.close()


@api_router.post("/projects/{project_name}/deployments/{deployment_name}/:clone-database-from-external")
@validate_api_token
async def clone_database_from_external(
    request: Request, project_name: str, deployment_name: str, clone_data: CloneDatabaseFromExternalRequest = Body(...)
) -> JSONResponse:
    """
    Clone a database from an external source (e.g., another cluster via port-forward) into a deployment.

    This endpoint enables migrating database data from external sources such as:
    - Port-forwarded databases from other clusters (e.g., digilab to local/ODCN)
    - External PostgreSQL instances
    - Production to staging/development environments

    The operation validates connectivity to both source and target before cloning.

    Headers:
        X-API-Key: The API key for the project (required)

    Example curl command (with port-forward running):
    ```bash
    # First, establish port-forward from source cluster:
    # kubectl port-forward -n namespace svc/postgresql 15432:5432

    curl -X POST "http://localhost:9595/api/projects/amt/deployments/production/:clone-database-from-external" \
      -H "Content-Type: application/json" \
      -H "X-API-Key: your-api-key" \
      -d '{
        "sourceHost": "localhost",
        "sourcePort": 15432,
        "sourceUsername": "postgres",
        "sourcePassword": "password",
        "sourceDatabase": "amt_staging",
        "sourceSchema": "amt_staging",
        "forceClone": true
      }'
    ```

    Args:
        request: The FastAPI request object
        project_name: Name of the target project
        deployment_name: Name of the target deployment
        clone_data: External database clone configuration

    Returns:
        JSON response with detailed clone operation results
    """
    project_manager = None
    try:
        # TODO: we need a method to create a project manager from a given project_name
        #  like: init(projectname)
        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")

        # Determine if we're using Chisel tunnel or direct connection
        if clone_data.tunnel:
            # Using Chisel tunnel
            logger.info(
                f"External database clone request via Chisel tunnel: {project_name}/{deployment_name} "
                f"<- {clone_data.tunnel.remoteHost}:{clone_data.tunnel.remotePort}/{clone_data.sourceDatabase} "
                f"(via {clone_data.tunnel.serverUrl})"
            )

            # Execute clone via ProjectManager
            clone_result = await project_manager.clone_database_from_external_with_tunnel(
                project_name=project_name,
                deployment_name=deployment_name,
                source_database=clone_data.sourceDatabase,
                source_schema=clone_data.sourceSchema,
                source_username=clone_data.sourceUsername,
                source_password=clone_data.sourcePassword,
                tunnel_server_url=clone_data.tunnel.serverUrl,
                tunnel_username=clone_data.tunnel.username,
                tunnel_password=clone_data.tunnel.password,
                tunnel_remote_host=clone_data.tunnel.remoteHost,
                tunnel_remote_port=clone_data.tunnel.remotePort,
                force_clone=clone_data.forceClone,
            )
        else:
            # Direct connection (no tunnel)
            if not clone_data.sourceHost or not clone_data.sourcePort:
                raise HTTPException(
                    status_code=400, detail="sourceHost and sourcePort are required when not using tunnel configuration"
                )

            logger.info(
                f"External database clone request (direct): {project_name}/{deployment_name} "
                f"<- {clone_data.sourceHost}:{clone_data.sourcePort}/{clone_data.sourceDatabase}"
            )

            # Execute clone via ProjectManager
            clone_result = await project_manager.clone_database_from_external_direct(
                project_name=project_name,
                deployment_name=deployment_name,
                source_host=clone_data.sourceHost,
                source_port=clone_data.sourcePort,
                source_database=clone_data.sourceDatabase,
                source_schema=clone_data.sourceSchema,
                source_username=clone_data.sourceUsername,
                source_password=clone_data.sourcePassword,
                force_clone=clone_data.forceClone,
            )

        # Determine response status
        if clone_result["success"]:
            status_code = 200
            message = (
                f"Database cloned successfully from {clone_data.sourceHost}:{clone_data.sourcePort} "
                f"to {project_name}/{deployment_name}"
            )
        else:
            status_code = 500
            message = f"Database clone failed: {', '.join(clone_result.get('errors', []))}"

        content = {
            "status": "success" if clone_result["success"] else "failed",
            "message": message,
            "project": project_name,
            "deployment": deployment_name,
            "source": clone_result["source"],
            "target": clone_result.get("target", {}),
            "operations": clone_result["operations"],
            "errors": clone_result.get("errors", []),
        }

        logger.info(
            f"External database clone completed: {project_name}/{deployment_name} (success: {clone_result['success']})"
        )
        return JSONResponse(content=content, status_code=status_code)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing external database clone request: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error cloning database from external source: {e!s}")
    finally:
        if project_manager:
            await project_manager.close()


@api_router.post("/projects/{project_name}/deployments/{deployment_name}/:clone-bucket-from-external")
@validate_api_token
async def clone_bucket_from_external(
    request: Request, project_name: str, deployment_name: str, clone_data: CloneBucketFromExternalRequest = Body(...)
) -> JSONResponse:
    """
    Clone a MinIO bucket from an external source (e.g., another cluster via port-forward) into a deployment.

    This endpoint enables migrating object storage data from external sources such as:
    - Port-forwarded MinIO instances from other clusters (e.g., digilab to local/ODCN)
    - External MinIO instances exposed via LoadBalancer or NodePort
    - Production to staging/development environments

    The operation validates connectivity to both source and target before cloning.

    Headers:
        X-API-Key: The API key for the project (required)

    Example curl command (with port-forward running):
    ```bash
    # First, establish port-forward from source cluster:
    # kubectl port-forward -n namespace svc/minio 19000:9000

    curl -X POST "http://localhost:9595/api/projects/amt/deployments/production/:clone-bucket-from-external" \
      -H "Content-Type: application/json" \
      -H "X-API-Key: your-api-key" \
      -d '{
        "sourceHost": "localhost",
        "sourcePort": 19000,
        "sourceAccessKey": "minioadmin",
        "sourceSecretKey": "minioadmin",
        "sourceBucket": "amt-staging",
        "sourceSecure": false,
        "forceClone": true
      }'
    ```

    Args:
        request: The FastAPI request object
        project_name: Name of the target project
        deployment_name: Name of the target deployment
        clone_data: External bucket clone configuration

    Returns:
        JSON response with detailed clone operation results
    """
    project_manager = None
    try:
        # Create project manager for the target project
        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")

        # Determine if we're using Chisel tunnel or direct connection
        if clone_data.tunnel:
            # Using Chisel tunnel
            logger.info(
                f"External bucket clone request via Chisel tunnel: {project_name}/{deployment_name} "
                f"<- {clone_data.tunnel.remoteHost}:{clone_data.tunnel.remotePort}/{clone_data.sourceBucket} "
                f"(via {clone_data.tunnel.serverUrl})"
            )

            # Execute clone via ProjectManager
            clone_result = await project_manager.clone_minio_bucket_from_external_with_tunnel(
                project_name=project_name,
                deployment_name=deployment_name,
                source_bucket=clone_data.sourceBucket,
                source_access_key=clone_data.sourceAccessKey,
                source_secret_key=clone_data.sourceSecretKey,
                tunnel_server_url=clone_data.tunnel.serverUrl,
                tunnel_username=clone_data.tunnel.username,
                tunnel_password=clone_data.tunnel.password,
                tunnel_remote_host=clone_data.tunnel.remoteHost,
                tunnel_remote_port=clone_data.tunnel.remotePort,
                source_secure=clone_data.sourceSecure,
                force_clone=clone_data.forceClone,
            )
        else:
            # Direct connection (no tunnel)
            if not clone_data.sourceHost or not clone_data.sourcePort:
                raise HTTPException(
                    status_code=400, detail="sourceHost and sourcePort are required when not using tunnel configuration"
                )

            logger.info(
                f"External bucket clone request (direct): {project_name}/{deployment_name} "
                f"<- {clone_data.sourceHost}:{clone_data.sourcePort}/{clone_data.sourceBucket}"
            )

            # Execute clone via ProjectManager
            clone_result = await project_manager.clone_minio_bucket_from_external_direct(
                project_name=project_name,
                deployment_name=deployment_name,
                source_host=clone_data.sourceHost,
                source_port=clone_data.sourcePort,
                source_bucket=clone_data.sourceBucket,
                source_access_key=clone_data.sourceAccessKey,
                source_secret_key=clone_data.sourceSecretKey,
                source_secure=clone_data.sourceSecure,
                force_clone=clone_data.forceClone,
            )

        # Determine response status
        if clone_result["success"]:
            status_code = 200
            message = (
                f"Bucket cloned successfully from {clone_data.sourceHost}:{clone_data.sourcePort} "
                f"to {project_name}/{deployment_name}"
            )
        else:
            status_code = 500
            message = f"Bucket clone failed: {', '.join(clone_result.get('errors', []))}"

        content = {
            "status": "success" if clone_result["success"] else "failed",
            "message": message,
            "project": project_name,
            "deployment": deployment_name,
            "source": clone_result["source"],
            "target": clone_result.get("target", {}),
            "operations": clone_result["operations"],
            "errors": clone_result.get("errors", []),
        }

        logger.info(
            f"External bucket clone completed: {project_name}/{deployment_name} (success: {clone_result['success']})"
        )
        return JSONResponse(content=content, status_code=status_code)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing external bucket clone request: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error cloning bucket from external source: {e!s}")
    finally:
        if project_manager:
            await project_manager.close()


@api_router.post("/projects/{project_name}/deployments/{deployment_name}/:validate-clone")
@validate_api_token
async def validate_clone_configuration(request: Request, project_name: str, deployment_name: str) -> JSONResponse:
    """
    Validate clone configuration without executing the clone.

    Performs pre-flight checks:
    - Clone configuration validity
    - Remote source existence (if remote-source type)
    - Source/target connectivity (if applicable)
    - Credentials verification
    - Resource existence checks

    Headers:
        X-API-Key: The API key for the project (required)

    Example:
    ```bash
    curl -X POST "http://localhost:9595/api/projects/amt/deployments/production/:validate-clone" \
      -H "Content-Type: application/json" \
      -H "X-API-Key: your-api-key"
    ```

    Args:
        request: The FastAPI request object
        project_name: Name of the project
        deployment_name: Name of the deployment to validate

    Returns:
        JSON response with detailed validation results
    """
    project_manager = None
    try:
        logger.info(f"Clone validation request for: {project_name}/{deployment_name}")

        # Create project manager instance
        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")

        # Read project data
        project_full_file_path = await project_manager.get_project_full_file_path()
        project_data = await project_manager._project_file_handler.read_project_file(project_full_file_path)

        # Execute validation (no actual cloning)
        validation_result = await project_manager._clone_manager.validate_clone_readiness(
            project_data=project_data, deployment_name=deployment_name
        )

        # Determine status code based on validation result
        if validation_result.get("validation", {}).get("passed"):
            status_code = 200
            message = f"Clone configuration for {deployment_name} is valid and ready"
        else:
            status_code = 422  # Unprocessable Entity
            message = f"Clone validation failed for {deployment_name}"

        content = {
            "status": "valid" if validation_result.get("validation", {}).get("passed") else "invalid",
            "message": message,
            "project": project_name,
            "deployment": deployment_name,
            "validation": validation_result.get("validation", {}),
        }

        logger.info(
            f"Clone validation completed for {project_name}/{deployment_name}: "
            f"passed={validation_result.get('validation', {}).get('passed')}"
        )
        return JSONResponse(content=content, status_code=status_code)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing clone validation request: {e!s}")
        raise HTTPException(status_code=500, detail=f"Error validating clone configuration: {e!s}")
    finally:
        if project_manager:
            await project_manager.close()


async def create_self_service_project(
    request: Request, project_data: SelfServiceProjectRequest = Body(...)
) -> JSONResponse:
    """
    Create a new project from the self-service portal form.

    This endpoint processes the comprehensive self-service form that includes:
    - Project details
    - Team members with roles
    - Multiple components with resource limits
    - Service integrations

    Example curl command:
    ```
    curl -X POST "http://localhost:9595/api/projects/self-service" \
      -H "Content-Type: application/json" \
      -d '{
        "project_name": "my-project",
        "project_description": "Test project",
        "cluster": "local",
        "user_email": ["user@example.com"],
        "user_role": ["Developer"],
        "services": ["service-web", "service-sso"],
        "components": [{
          "type": "deployment",
          "port": 8080,
          "image": "nginx:latest"
        }]
      }'
    ```

    Args:
        request: The FastAPI request object
        project_data: The self-service project creation request data

    Returns:
        JSON response with project creation and processing status
    """
    start_time = time.time()
    project_manager = None
    subdomain_registered = False  # Track if we actually registered a subdomain for rollback
    subdomain_connector = None  # Connector for subdomain operations
    try:
        logger.info(f"Creating self-service project: {project_data.project_name}")

        # Validate project name
        if not validate_project_name(project_data.project_name):
            raise HTTPException(
                status_code=400,
                detail="Project name must start with lowercase letter, then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters",
            )

        # Validate nice-url mode requirements
        if project_data.domain_mode == "nice-url":
            # Check if cluster supports nice-url mode at all
            from opi.core.cluster_config import get_nice_url_supported_domains

            supported_domains = get_nice_url_supported_domains(project_data.cluster)
            if not supported_domains:
                raise HTTPException(
                    status_code=400,
                    detail=f"Cluster '{sanitize_for_log(project_data.cluster)}' does not support nice-url mode. "
                    f"Please select a different domain mode or cluster.",
                )

            # Require both subdomain and base_domain for nice-url mode
            if not project_data.subdomain:
                raise HTTPException(
                    status_code=400,
                    detail="Subdomain is required for nice-url mode",
                )
            if not project_data.base_domain:
                raise HTTPException(
                    status_code=400,
                    detail="Base domain is required for nice-url mode",
                )

            # Validate base domain is supported for this cluster
            is_valid, error_message = validate_base_domain(project_data.base_domain, project_data.cluster)
            if not is_valid:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid base domain for cluster '{sanitize_for_log(project_data.cluster)}': {error_message}",
                )

            # Validate subdomain format early to fail fast
            if project_data.subdomain:
                is_valid, error_message = validate_subdomain(project_data.subdomain)
                if not is_valid:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid subdomain: {error_message}",
                    )

            # F1: Validate root component has a port when nice-url mode is enabled
            # This is critical for proper ingress routing - the root component receives
            # traffic at subdomain.base_domain and must have a port to route to
            if project_data.components:
                root_components = [c for c in project_data.components if c.root]
                if root_components:
                    root_component = root_components[0]
                    if root_component.port is None:
                        # Find the component name/index for error message
                        root_idx = project_data.components.index(root_component)
                        raise HTTPException(
                            status_code=400,
                            detail=f"Root component (component {root_idx + 1}) must have a port defined for nice-url mode. "
                            f"The root component receives traffic at {project_data.subdomain}.{project_data.base_domain}",
                        )

            # Auto-enable Let's Encrypt for nice-url mode (HTTPS by default)
            if not project_data.issuer:
                project_data.issuer = "letsencrypt"
                logger.info(
                    f"Auto-enabled Let's Encrypt issuer for nice-url mode with base domain '{project_data.base_domain}'"
                )

            # Register subdomain BEFORE project processing to ensure atomic rollback
            # This prevents the TOCTOU issue where subdomain_registered flag doesn't match actual state
            if project_data.subdomain:
                subdomain_connector = create_subdomain_connector()
                await subdomain_connector.register(
                    subdomain=project_data.subdomain,
                    base_domain=project_data.base_domain,
                    project_name=project_data.project_name,
                    deployment_name=project_data.deployment_name,
                    cluster=project_data.cluster,
                    created_by=None,
                )
                subdomain_registered = True  # Only set after successful registration
                logger.info(
                    f"Pre-registered subdomain '{project_data.subdomain}.{project_data.base_domain}' "
                    f"for project '{project_data.project_name}'"
                )

        # Generate YAML content from self-service form data
        yaml_content = await generate_self_service_project_yaml(project_data)

        # Create Git connector for projects repository
        git_connector_for_project_files = GitConnector(
            repo_url=settings.GIT_PROJECTS_SERVER_URL,
            username=settings.GIT_PROJECTS_SERVER_USERNAME,
            password=settings.GIT_PROJECTS_SERVER_PASSWORD,
            branch=settings.GIT_PROJECTS_SERVER_BRANCH,
            repo_path=settings.GIT_PROJECTS_SERVER_REPO_PATH,
        )

        # Create project file in Git repository
        project_file_path = f"projects/{project_data.project_name}.yaml"
        await git_connector_for_project_files.check_overwrite_project_file(project_file_path)
        await git_connector_for_project_files.create_or_update_file(project_file_path, yaml_content, False)

        logger.info(f"Self-service project file created successfully: {project_file_path}")

        # Process the project file
        project_manager = ProjectManager(git_connector_for_project_files=git_connector_for_project_files)
        processing_result = await project_manager.process_project_from_git(project_file_path)

        if processing_result:
            elapsed_time = time.time() - start_time
            logger.info(
                f"Self-service project creation completed successfully: {project_data.project_name} (took {elapsed_time:.2f} seconds)"
            )

            content = {
                "status": "success",
                "message": f"Self-service project '{project_data.project_name}' created and processed successfully",
                "project": {
                    "name": project_data.project_name,
                    "file_path": project_file_path,
                    "components": len(project_data.components) if project_data.components else 1,
                    "team_members": len(project_data.user_email) if project_data.user_email else 0,
                },
                "processing": {
                    "status": "completed",
                    "message": "Project resources created successfully",
                    "elapsed_time": f"{elapsed_time:.2f} seconds",
                },
            }
            return JSONResponse(content=content, status_code=200)
        else:
            # Processing failed - rollback subdomain registration if we registered one
            if subdomain_registered and project_data.subdomain and project_data.base_domain:
                await _rollback_subdomain_registration(
                    project_data.project_name,
                    project_data.deployment_name,
                    project_data.subdomain,
                    project_data.base_domain,
                )

            elapsed_time = time.time() - start_time
            logger.warning(
                f"Self-service project creation partially completed: {project_data.project_name} (took {elapsed_time:.2f} seconds)"
            )

            content = {
                "status": "partial_success",
                "message": f"Self-service project '{project_data.project_name}' created but processing failed",
                "project": {"name": project_data.project_name, "file_path": project_file_path},
                "processing": {
                    "status": "failed",
                    "message": "Failed to process project resources",
                    "elapsed_time": f"{elapsed_time:.2f} seconds",
                },
            }
            return JSONResponse(content=content, status_code=207)  # 207 Multi-Status

    except HTTPException:
        raise
    except (SubdomainNotAvailableError, SubdomainValidationError, BaseDomainValidationError) as e:
        # Subdomain-related errors should return 400 Bad Request with clear message
        # Note: subdomain_registered will be False since registration failed
        elapsed_time = time.time() - start_time
        logger.warning(f"Subdomain error creating self-service project: {e!s} (took {elapsed_time:.2f} seconds)")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # Rollback subdomain registration on exception
        if subdomain_registered and project_data.subdomain and project_data.base_domain:
            await _rollback_subdomain_registration(
                project_data.project_name,
                project_data.deployment_name,
                project_data.subdomain,
                project_data.base_domain,
            )

        elapsed_time = time.time() - start_time
        logger.error(f"Error creating self-service project: {e!s} (took {elapsed_time:.2f} seconds)")
        raise HTTPException(status_code=500, detail=f"Error creating self-service project: {e!s}")
    finally:
        if project_manager:
            await project_manager.close()


async def _rollback_subdomain_registration(
    project_name: str, deployment_name: str, subdomain: str, base_domain: str
) -> None:
    """Rollback a subdomain registration after project creation failure.

    This is a best-effort cleanup - errors are logged but not raised.
    """
    try:
        connector = create_subdomain_connector()
        deleted = await connector.delete_by_deployment(project_name, deployment_name)
        if deleted:
            logger.info(
                f"Rolled back subdomain registration '{subdomain}.{base_domain}' for failed project '{project_name}'"
            )
        else:
            logger.debug(f"No subdomain registration to rollback for '{project_name}/{deployment_name}'")
    except Exception as rollback_error:
        # Log but don't raise - rollback is best-effort
        logger.error(f"Failed to rollback subdomain registration for '{project_name}': {rollback_error}")


# Subdomain API endpoints for nice URL feature


class SubdomainCheckResponse(BaseModel):
    """Response for subdomain availability check."""

    subdomain: str = Field(..., description="The subdomain that was checked", example="myapp")
    base_domain: str = Field(..., description="The base domain", example="rijks.app")
    available: bool = Field(..., description="Whether the subdomain is available", example=True)
    validation_error: str | None = Field(
        None, description="Validation error message if subdomain format is invalid", example=None
    )


class SubdomainRegistration(BaseModel):
    """Subdomain registration details."""

    id: int = Field(..., description="Registration ID")
    subdomain: str = Field(..., description="The subdomain", example="myapp")
    base_domain: str = Field(..., description="The base domain", example="rijks.app")
    project_name: str = Field(..., description="Project that owns this subdomain", example="my-project")
    deployment_name: str = Field(..., description="Deployment using this subdomain", example="prod")
    cluster: str = Field(..., description="Cluster where deployed", example="odcn-production")
    created_at: str | None = Field(None, description="Registration timestamp")
    created_by: str | None = Field(None, description="Who created the registration")


@api_router.get(
    "/subdomains/check/{subdomain}",
    response_model=SubdomainCheckResponse,
    responses={
        200: {"description": "Subdomain availability check result"},
    },
)
@validate_api_token
async def check_subdomain_availability(request: Request, subdomain: str, base_domain: str) -> SubdomainCheckResponse:
    """
    Check if a subdomain is available for registration.

    This endpoint requires API token authentication to prevent unauthenticated
    subdomain enumeration attacks.

    Rate limited to 30 requests per minute per client (using multi-factor identification
    to prevent X-Forwarded-For spoofing bypasses).

    Headers:
        X-API-Key: The API key for authentication (required)

    Args:
        request: The FastAPI request object
        subdomain: The subdomain to check (e.g., "myapp")
        base_domain: The base domain (e.g., "rijks.app") - must be a supported domain

    Returns:
        SubdomainCheckResponse with availability status

    Example:
    ```bash
    curl "http://localhost:9595/api/subdomains/check/myapp?base_domain=rijks.app" \
      -H "X-API-Key: your-api-key"
    ```
    """
    # Rate limiting check - use robust client identification to prevent X-Forwarded-For spoofing
    # This combines IP + browser fingerprint + session ID (when available)
    client_id = IPRateLimiter.get_client_identifier(request)
    if not subdomain_check_rate_limiter.is_allowed(client_id):
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please wait before checking again.",
        )

    # Audit log for subdomain availability checks (sanitize input to prevent log injection)
    safe_subdomain = sanitize_for_log(subdomain)
    safe_base_domain = sanitize_for_log(base_domain)
    # Note: Only log IP portion of client_id to reduce log verbosity
    client_ip = sanitize_for_log(IPRateLimiter.get_client_ip(request))
    logger.info(f"AUDIT: Subdomain check - subdomain={safe_subdomain}, base_domain={safe_base_domain}, ip={client_ip}")

    try:
        # Validate subdomain format first
        is_valid, validation_error = validate_subdomain(subdomain)
        if not is_valid:
            return SubdomainCheckResponse(
                subdomain=subdomain.lower(),
                base_domain=base_domain.lower(),
                available=False,
                validation_error=validation_error,
            )

        # Validate base_domain is a supported domain (prevents probing arbitrary domains)
        is_valid_domain, domain_error = validate_base_domain(base_domain)
        if not is_valid_domain:
            return SubdomainCheckResponse(
                subdomain=subdomain.lower(),
                base_domain=base_domain.lower(),
                available=False,
                validation_error=domain_error,
            )

        connector = create_subdomain_connector()
        is_available = await connector.check_availability(subdomain, base_domain)

        return SubdomainCheckResponse(
            subdomain=subdomain.lower(),
            base_domain=base_domain.lower(),
            available=is_available,
            validation_error=None,
        )
    except Exception as e:
        logger.error(f"Error checking subdomain availability: {e}")
        raise HTTPException(status_code=500, detail=f"Error checking subdomain availability: {e}")


class SubdomainListResponse(BaseModel):
    """Paginated response for subdomain registrations."""

    items: list[SubdomainRegistration] = Field(..., description="List of subdomain registrations")
    total: int = Field(..., description="Total count of matching registrations")
    limit: int = Field(..., description="Maximum items returned per page")
    offset: int = Field(..., description="Number of items skipped")


@api_router.get(
    "/subdomains",
    response_model=SubdomainListResponse,
    responses={
        200: {"description": "Paginated list of subdomain registrations"},
    },
)
@validate_api_token
async def list_subdomains(
    request: Request,
    project_name: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> SubdomainListResponse:
    """
    List subdomain registrations with pagination support.

    Args:
        project_name: Optional filter by project name
        limit: Maximum number of results to return (default: 100, max: 1000)
        offset: Number of results to skip for pagination (default: 0)

    Returns:
        Paginated list of subdomain registrations with total count

    Example:
    ```bash
    # List first page of all subdomains
    curl "http://localhost:9595/api/subdomains?limit=50&offset=0"

    # List second page
    curl "http://localhost:9595/api/subdomains?limit=50&offset=50"

    # List subdomains for a specific project
    curl "http://localhost:9595/api/subdomains?project_name=my-project&limit=20"
    ```
    """
    # Validate and cap limit to prevent excessive queries
    if limit < 1:
        limit = 1
    elif limit > 1000:
        limit = 1000

    if offset < 0:
        offset = 0

    try:
        connector = create_subdomain_connector()

        if project_name:
            # For project-specific queries, get all and slice (pagination less critical here)
            all_registrations = await connector.get_by_project(project_name)
            total = len(all_registrations)
            registrations = all_registrations[offset : offset + limit]
        else:
            # For full list, use database-level pagination
            registrations = await connector.list_all(limit=limit, offset=offset)
            # Get total count for pagination info
            total = await connector.count_all()

        # Convert datetime objects to strings for JSON serialization
        items = []
        for reg in registrations:
            reg_dict = dict(reg)
            if reg_dict.get("created_at"):
                reg_dict["created_at"] = reg_dict["created_at"].isoformat()
            items.append(reg_dict)

        return SubdomainListResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
        )
    except Exception as e:
        logger.error(f"Error listing subdomains: {e}")
        raise HTTPException(status_code=500, detail=f"Error listing subdomains: {e}")


@api_router.post(
    "/projects/{project_name}/registries/by-secret",
    responses={
        201: {"description": "Registry added successfully"},
        200: {"description": "Registry updated successfully"},
    },
)
@validate_api_token
async def add_registry_by_secret(
    request: Request, project_name: str, registry_data: AddRegistryBySecretRequest = Body(...)
) -> JSONResponse:
    """
    Add or update a container registry that references a pre-existing Kubernetes secret.

    If a registry with the same name already exists, it is updated (upsert).

    Headers:
        X-API-Key: The API key for the project (required)

    Example:
    ```bash
    curl -X POST "http://localhost:9595/api/projects/my-project/registries/by-secret" \\
      -H "Content-Type: application/json" \\
      -H "X-API-Key: your-api-key" \\
      -d '{
        "name": "platform-registry",
        "url": "rcr.rijksapps.nl/rig",
        "secretName": "rcr-pull-secret"
      }'
    ```
    """
    project_manager = None
    try:
        logger.info(
            f"Adding registry '{sanitize_for_log(registry_data.name)}' (by-secret) "
            f"to project: {sanitize_for_log(project_name)}"
        )

        if not validate_project_name(project_name):
            raise HTTPException(
                status_code=400,
                detail="Invalid project name format. Must start with lowercase letter, then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters",
            )

        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")

        result = await project_manager.upsert_registry_by_secret(
            name=registry_data.name,
            url=registry_data.url,
            secret_name=registry_data.secret_name,
        )

        if result["success"]:
            # Process the project to reconcile imagePullSecrets
            processing_result = await project_manager.process_project_from_git(f"projects/{project_name}.yaml")

            status_code = 201 if result["created"] else 200
            return JSONResponse(
                content={
                    "status": "success",
                    "message": f"Registry '{registry_data.name}' {'added' if result['created'] else 'updated'} successfully",
                    "registry": result["registry"],
                    "created": result["created"],
                    "processing": {"status": "completed" if processing_result else "failed"},
                },
                status_code=status_code,
            )
        else:
            return JSONResponse(
                content={
                    "status": "failed",
                    "message": f"Failed to add registry '{registry_data.name}'",
                    "error": result["error"],
                    "error_type": result.get("error_type"),
                },
                status_code=400,
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding registry by secret: {e!s}")
        raise HTTPException(status_code=500, detail="An internal error occurred")
    finally:
        if project_manager:
            await project_manager.close()


@api_router.post(
    "/projects/{project_name}/registries/by-credentials",
    responses={
        201: {"description": "Registry added successfully"},
        200: {"description": "Registry updated successfully"},
    },
)
@validate_api_token
async def add_registry_by_credentials(
    request: Request, project_name: str, registry_data: AddRegistryByCredentialsRequest = Body(...)
) -> JSONResponse:
    """
    Add or update a container registry with username/password credentials.

    The password is AGE-encrypted with the project's public key before storing.
    If a registry with the same name already exists, it is updated (upsert).

    Headers:
        X-API-Key: The API key for the project (required)

    Example:
    ```bash
    curl -X POST "http://localhost:9595/api/projects/my-project/registries/by-credentials" \\
      -H "Content-Type: application/json" \\
      -H "X-API-Key: your-api-key" \\
      -d '{
        "name": "github-packages",
        "url": "ghcr.io",
        "username": "my-github-user",
        "password": "ghp_xxxxxxxxxxxx"
      }'
    ```
    """
    project_manager = None
    try:
        logger.info(
            f"Adding registry '{sanitize_for_log(registry_data.name)}' (by-credentials) "
            f"to project: {sanitize_for_log(project_name)}"
        )

        if not validate_project_name(project_name):
            raise HTTPException(
                status_code=400,
                detail="Invalid project name format. Must start with lowercase letter, then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters",
            )

        project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")

        result = await project_manager.upsert_registry_by_credentials(
            name=registry_data.name,
            url=registry_data.url,
            username=registry_data.username,
            password=registry_data.password,
        )

        if result["success"]:
            # Process the project to reconcile imagePullSecrets
            processing_result = await project_manager.process_project_from_git(f"projects/{project_name}.yaml")

            status_code = 201 if result["created"] else 200
            return JSONResponse(
                content={
                    "status": "success",
                    "message": f"Registry '{registry_data.name}' {'added' if result['created'] else 'updated'} successfully",
                    "registry": result["registry"],
                    "created": result["created"],
                    "processing": {"status": "completed" if processing_result else "failed"},
                },
                status_code=status_code,
            )
        else:
            error_status_codes = {
                "missing_public_key": 400,
            }
            status_code = error_status_codes.get(result.get("error_type"), 400)
            return JSONResponse(
                content={
                    "status": "failed",
                    "message": f"Failed to add registry '{registry_data.name}'",
                    "error": result["error"],
                    "error_type": result.get("error_type"),
                },
                status_code=status_code,
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding registry by credentials: {e!s}")
        raise HTTPException(status_code=500, detail="An internal error occurred")
    finally:
        if project_manager:
            await project_manager.close()
