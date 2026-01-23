"""
Subdomain registry connector for managing nice URL subdomains.

This module provides functionality to manage globally unique subdomains for nice URLs.
Subdomains are registered per (subdomain, base_domain) pair and associated with projects.
"""

import logging
import re
from typing import Any

from opi.core.database_pools import get_database_pool
from opi.core.cluster_config import CLUSTER_CONFIG

logger = logging.getLogger(__name__)


def get_supported_base_domains(cluster: str | None = None) -> set[str]:
    """Get all supported base domains for nice URLs.

    Args:
        cluster: Optional cluster name to get domains for specific cluster.
                 If None, returns all supported domains across all clusters.

    Returns:
        Set of supported base domain strings
    """
    if cluster and cluster in CLUSTER_CONFIG:
        nice_url_config = CLUSTER_CONFIG[cluster].get("nice_url", {})
        return set(nice_url_config.get("supported_domains", []))

    # Collect all supported domains from all clusters
    all_domains = set()
    for cluster_config in CLUSTER_CONFIG.values():
        nice_url_config = cluster_config.get("nice_url", {})
        all_domains.update(nice_url_config.get("supported_domains", []))
    return all_domains


def validate_base_domain(base_domain: str, cluster: str | None = None, language: str = "nl") -> tuple[bool, str | None]:
    """Validate a base domain against configured supported domains.

    Args:
        base_domain: The base domain to validate
        cluster: Optional cluster name for cluster-specific validation
        language: Language for error messages ("nl" for Dutch, "en" for English)

    Returns:
        Tuple of (is_valid, error_message). If valid, error_message is None.
    """
    messages_nl = {
        "empty": "Base domain mag niet leeg zijn",
        "not_supported": "'{base_domain}' is geen ondersteund base domain. Ondersteunde domeinen: {supported}",
    }

    messages_en = {
        "empty": "Base domain cannot be empty",
        "not_supported": "'{base_domain}' is not a supported base domain. Supported domains: {supported}",
    }

    messages = messages_nl if language == "nl" else messages_en

    if not base_domain:
        return False, messages["empty"]

    base_domain_lower = base_domain.lower()
    supported_domains = get_supported_base_domains(cluster)

    if base_domain_lower not in supported_domains:
        return False, messages["not_supported"].format(
            base_domain=base_domain_lower,
            supported=", ".join(sorted(supported_domains))
        )

    return True, None

# DNS subdomain validation constants
SUBDOMAIN_MAX_LENGTH = 63
SUBDOMAIN_MIN_LENGTH = 1
SUBDOMAIN_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?$")

# Reserved subdomains that cannot be registered
RESERVED_SUBDOMAINS = frozenset([
    "www",
    "api",
    "admin",
    "mail",
    "ftp",
    "ns1",
    "ns2",
    "ns3",
    "smtp",
    "pop",
    "imap",
    "webmail",
    "test",
    "dev",
    "staging",
    "prod",
    "production",
    "localhost",
    "local",
    "beta",
    "alpha",
    "demo",
    "support",
    "help",
    "docs",
    "status",
    "cdn",
    "static",
    "assets",
    "media",
    "images",
    "files",
    "download",
    "downloads",
    "upload",
    "uploads",
    "git",
    "gitlab",
    "github",
    "jenkins",
    "ci",
    "cd",
    "build",
    "deploy",
    "registry",
    "docker",
    "kubernetes",
    "k8s",
    "argocd",
    "argo",
    "keycloak",
    "auth",
    "oauth",
    "sso",
    "login",
    "logout",
    "register",
    "signup",
    "signin",
    "account",
    "profile",
    "settings",
    "dashboard",
    "portal",
    "panel",
    "console",
    "control",
    "manager",
    "management",
    "system",
    "sys",
    "root",
    "master",
    "main",
    "default",
    "null",
    "undefined",
    "none",
    "anonymous",
    "guest",
    "public",
    "private",
    "internal",
    "external",
    "vpn",
    "proxy",
    "gateway",
    "lb",
    "loadbalancer",
    "monitoring",
    "metrics",
    "prometheus",
    "grafana",
    "kibana",
    "elasticsearch",
    "logs",
    "logging",
])


class SubdomainError(Exception):
    """Exception raised when subdomain operations fail."""


class SubdomainNotAvailableError(SubdomainError):
    """Exception raised when a subdomain is already taken."""


class SubdomainValidationError(SubdomainError):
    """Exception raised when subdomain validation fails."""


class BaseDomainValidationError(SubdomainError):
    """Exception raised when base domain validation fails."""


def validate_subdomain(subdomain: str, language: str = "nl") -> tuple[bool, str | None]:
    """Validate a subdomain for DNS compatibility.

    Args:
        subdomain: The subdomain to validate
        language: Language for error messages ("nl" for Dutch, "en" for English)

    Returns:
        Tuple of (is_valid, error_message). If valid, error_message is None.

    Rules:
        - Must be 1-63 characters
        - Must contain only lowercase letters, numbers, and hyphens
        - Cannot start or end with a hyphen
        - Cannot be a reserved subdomain
    """
    # Dutch error messages (default)
    messages_nl = {
        "empty": "Subdomein mag niet leeg zijn",
        "too_short": f"Subdomein moet minimaal {SUBDOMAIN_MIN_LENGTH} teken(s) bevatten",
        "too_long": f"Subdomein mag maximaal {SUBDOMAIN_MAX_LENGTH} tekens bevatten",
        "reserved": "'{subdomain}' is een gereserveerd subdomein en kan niet worden gebruikt",
        "start_hyphen": "Subdomein mag niet beginnen met een koppelteken",
        "end_hyphen": "Subdomein mag niet eindigen met een koppelteken",
        "invalid_chars": "Subdomein mag alleen kleine letters (a-z), cijfers (0-9) en koppeltekens (-) bevatten",
    }

    # English error messages
    messages_en = {
        "empty": "Subdomain cannot be empty",
        "too_short": f"Subdomain must be at least {SUBDOMAIN_MIN_LENGTH} character(s)",
        "too_long": f"Subdomain cannot exceed {SUBDOMAIN_MAX_LENGTH} characters",
        "reserved": "'{subdomain}' is a reserved subdomain and cannot be used",
        "start_hyphen": "Subdomain cannot start with a hyphen",
        "end_hyphen": "Subdomain cannot end with a hyphen",
        "invalid_chars": "Subdomain can only contain lowercase letters (a-z), numbers (0-9), and hyphens (-)",
    }

    messages = messages_nl if language == "nl" else messages_en

    if not subdomain:
        return False, messages["empty"]

    subdomain_lower = subdomain.lower()

    if len(subdomain_lower) < SUBDOMAIN_MIN_LENGTH:
        return False, messages["too_short"]

    if len(subdomain_lower) > SUBDOMAIN_MAX_LENGTH:
        return False, messages["too_long"]

    if subdomain_lower in RESERVED_SUBDOMAINS:
        return False, messages["reserved"].format(subdomain=subdomain_lower)

    if not SUBDOMAIN_PATTERN.match(subdomain_lower):
        if subdomain_lower.startswith("-"):
            return False, messages["start_hyphen"]
        if subdomain_lower.endswith("-"):
            return False, messages["end_hyphen"]
        return False, messages["invalid_chars"]

    return True, None


class SubdomainConnector:
    """Connector for managing subdomain registry using the application database pool.

    This connector manages the subdomain_registry table which tracks globally unique
    subdomains for the nice URL feature. Each subdomain + base_domain combination
    must be unique across all projects.
    """

    TABLE_NAME = "subdomain_registry"

    @staticmethod
    def _get_pool():
        """Get the main database pool."""
        return get_database_pool("main")

    async def check_availability(self, subdomain: str, base_domain: str) -> bool:
        """Check if a subdomain is available for registration.

        Args:
            subdomain: The subdomain to check (e.g., "myapp")
            base_domain: The base domain (e.g., "rijks.app")

        Returns:
            True if the subdomain is available, False if already taken
        """
        pool = self._get_pool()
        conn = await pool.acquire()
        try:
            result = await conn.fetchval(
                f"""
                SELECT 1 FROM {self.TABLE_NAME}
                WHERE subdomain = $1 AND base_domain = $2
                """,
                subdomain.lower(),
                base_domain.lower(),
            )
            return result is None
        finally:
            await pool.release(conn)

    async def register(
        self,
        subdomain: str,
        base_domain: str,
        project_name: str,
        deployment_name: str,
        cluster: str,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        """Register a new subdomain.

        Args:
            subdomain: The subdomain to register (e.g., "myapp")
            base_domain: The base domain (e.g., "rijks.app")
            project_name: The project name that owns this subdomain
            deployment_name: The deployment name using this subdomain
            cluster: The cluster where this subdomain is deployed
            created_by: Optional email/identifier of who created the registration

        Returns:
            Dictionary with the created registration details

        Raises:
            SubdomainValidationError: If the subdomain format is invalid
            BaseDomainValidationError: If the base domain is not supported
            SubdomainNotAvailableError: If the subdomain is already taken
            SubdomainError: If registration fails
        """
        # Validate subdomain format
        is_valid, error_message = validate_subdomain(subdomain)
        if not is_valid:
            raise SubdomainValidationError(error_message)

        subdomain_lower = subdomain.lower()
        base_domain_lower = base_domain.lower()

        # Validate base domain against supported domains for this cluster
        is_valid, error_message = validate_base_domain(base_domain_lower, cluster)
        if not is_valid:
            raise BaseDomainValidationError(error_message)

        # Check availability first
        if not await self.check_availability(subdomain_lower, base_domain_lower):
            existing = await self.get_by_subdomain(subdomain_lower, base_domain_lower)
            raise SubdomainNotAvailableError(
                f"Subdomein '{subdomain_lower}.{base_domain_lower}' is al geregistreerd "
                f"door project '{existing.get('project_name') if existing else 'onbekend'}'"
            )

        pool = self._get_pool()
        conn = await pool.acquire()
        try:
            # Use INSERT ... ON CONFLICT to handle race conditions atomically
            # This prevents the TOCTOU race between check_availability and register
            result = await conn.fetchrow(
                f"""
                INSERT INTO {self.TABLE_NAME}
                (subdomain, base_domain, project_name, deployment_name, cluster, created_by)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (subdomain, base_domain) DO NOTHING
                RETURNING id, subdomain, base_domain, project_name, deployment_name, cluster, created_at, created_by
                """,
                subdomain_lower,
                base_domain_lower,
                project_name,
                deployment_name,
                cluster,
                created_by,
            )

            if result is None:
                # Conflict occurred - subdomain was taken between check and register
                existing = await self.get_by_subdomain(subdomain_lower, base_domain_lower)
                if existing and existing.get("project_name") == project_name:
                    # Same project - return existing registration
                    logger.info(
                        f"Subdomain '{subdomain_lower}.{base_domain_lower}' already registered "
                        f"to same project '{project_name}'"
                    )
                    return existing
                raise SubdomainNotAvailableError(
                    f"Subdomain '{subdomain_lower}.{base_domain_lower}' werd zojuist geregistreerd "
                    f"door een ander project"
                )

            logger.info(
                f"Registered subdomain '{subdomain_lower}.{base_domain_lower}' "
                f"for project '{project_name}', deployment '{deployment_name}'"
            )

            return dict(result)
        except SubdomainNotAvailableError:
            raise
        except Exception as e:
            logger.exception(f"Failed to register subdomain '{subdomain_lower}.{base_domain_lower}'")
            raise SubdomainError(f"Subdomain registration failed: {e}") from e
        finally:
            await pool.release(conn)

    async def get_by_subdomain(self, subdomain: str, base_domain: str) -> dict[str, Any] | None:
        """Get a subdomain registration by subdomain and base domain.

        Args:
            subdomain: The subdomain to look up
            base_domain: The base domain

        Returns:
            Dictionary with registration details, or None if not found
        """
        pool = self._get_pool()
        conn = await pool.acquire()
        try:
            result = await conn.fetchrow(
                f"""
                SELECT id, subdomain, base_domain, project_name, deployment_name, cluster, created_at, created_by
                FROM {self.TABLE_NAME}
                WHERE subdomain = $1 AND base_domain = $2
                """,
                subdomain.lower(),
                base_domain.lower(),
            )
            return dict(result) if result else None
        finally:
            await pool.release(conn)

    async def get_by_project(self, project_name: str) -> list[dict[str, Any]]:
        """Get all subdomain registrations for a project.

        Args:
            project_name: The project name to look up

        Returns:
            List of registration dictionaries
        """
        pool = self._get_pool()
        conn = await pool.acquire()
        try:
            results = await conn.fetch(
                f"""
                SELECT id, subdomain, base_domain, project_name, deployment_name, cluster, created_at, created_by
                FROM {self.TABLE_NAME}
                WHERE project_name = $1
                ORDER BY subdomain, base_domain
                """,
                project_name,
            )
            return [dict(row) for row in results]
        finally:
            await pool.release(conn)

    async def get_by_deployment(self, project_name: str, deployment_name: str) -> dict[str, Any] | None:
        """Get subdomain registration for a specific deployment.

        Args:
            project_name: The project name
            deployment_name: The deployment name

        Returns:
            Registration dictionary, or None if not found
        """
        pool = self._get_pool()
        conn = await pool.acquire()
        try:
            result = await conn.fetchrow(
                f"""
                SELECT id, subdomain, base_domain, project_name, deployment_name, cluster, created_at, created_by
                FROM {self.TABLE_NAME}
                WHERE project_name = $1 AND deployment_name = $2
                """,
                project_name,
                deployment_name,
            )
            return dict(result) if result else None
        finally:
            await pool.release(conn)

    async def delete(self, subdomain: str, base_domain: str) -> bool:
        """Delete a subdomain registration.

        Args:
            subdomain: The subdomain to delete
            base_domain: The base domain

        Returns:
            True if deleted, False if not found
        """
        pool = self._get_pool()
        conn = await pool.acquire()
        try:
            result = await conn.execute(
                f"""
                DELETE FROM {self.TABLE_NAME}
                WHERE subdomain = $1 AND base_domain = $2
                """,
                subdomain.lower(),
                base_domain.lower(),
            )
            deleted = result == "DELETE 1"
            if deleted:
                logger.info(f"Deleted subdomain registration '{subdomain.lower()}.{base_domain.lower()}'")
            return deleted
        finally:
            await pool.release(conn)

    async def delete_by_project(self, project_name: str) -> int:
        """Delete all subdomain registrations for a project.

        Args:
            project_name: The project name

        Returns:
            Number of registrations deleted
        """
        pool = self._get_pool()
        conn = await pool.acquire()
        try:
            result = await conn.execute(
                f"""
                DELETE FROM {self.TABLE_NAME}
                WHERE project_name = $1
                """,
                project_name,
            )
            # Parse "DELETE N" to get count
            count = int(result.split()[-1]) if result else 0
            if count > 0:
                logger.info(f"Deleted {count} subdomain registration(s) for project '{project_name}'")
            return count
        finally:
            await pool.release(conn)

    async def delete_by_deployment(self, project_name: str, deployment_name: str) -> int:
        """Delete all subdomain registrations for a specific deployment.

        Args:
            project_name: The project name
            deployment_name: The deployment name

        Returns:
            Number of registrations deleted
        """
        pool = self._get_pool()
        conn = await pool.acquire()
        try:
            result = await conn.execute(
                f"""
                DELETE FROM {self.TABLE_NAME}
                WHERE project_name = $1 AND deployment_name = $2
                """,
                project_name,
                deployment_name,
            )
            # Parse "DELETE N" to get count
            count = int(result.split()[-1]) if result else 0
            if count > 0:
                logger.info(
                    f"Deleted {count} subdomain registration(s) for deployment '{project_name}/{deployment_name}'"
                )
            return count
        finally:
            await pool.release(conn)

    async def register_or_update_for_deployment(
        self,
        subdomain: str,
        base_domain: str,
        project_name: str,
        deployment_name: str,
        cluster: str,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        """Register a subdomain or update if the deployment's subdomain has changed.

        This method handles the case where a deployment's subdomain configuration changes.
        It will:
        1. Check if the deployment already has a subdomain registration
        2. If the subdomain hasn't changed, return the existing registration
        3. If the subdomain has changed, delete the old registration and create a new one

        Args:
            subdomain: The new/current subdomain
            base_domain: The base domain
            project_name: The project name
            deployment_name: The deployment name
            cluster: The cluster
            created_by: Optional creator identifier

        Returns:
            Dictionary with registration details

        Raises:
            SubdomainValidationError: If the subdomain format is invalid
            BaseDomainValidationError: If the base domain is not supported
            SubdomainNotAvailableError: If the new subdomain is already taken by another project
        """
        subdomain_lower = subdomain.lower()
        base_domain_lower = base_domain.lower()

        # Check if this deployment already has a registration
        existing = await self.get_by_deployment(project_name, deployment_name)

        if existing:
            # Check if subdomain has changed
            if existing["subdomain"] == subdomain_lower and existing["base_domain"] == base_domain_lower:
                # No change - return existing
                logger.debug(
                    f"Subdomain '{subdomain_lower}.{base_domain_lower}' unchanged for "
                    f"deployment '{project_name}/{deployment_name}'"
                )
                return existing

            # Subdomain changed - delete old registration first
            logger.info(
                f"Subdomain changed from '{existing['subdomain']}.{existing['base_domain']}' to "
                f"'{subdomain_lower}.{base_domain_lower}' for deployment '{project_name}/{deployment_name}'"
            )
            await self.delete(existing["subdomain"], existing["base_domain"])

        # Register the new subdomain
        return await self.register(
            subdomain=subdomain,
            base_domain=base_domain,
            project_name=project_name,
            deployment_name=deployment_name,
            cluster=cluster,
            created_by=created_by,
        )

    async def update(
        self,
        subdomain: str,
        base_domain: str,
        deployment_name: str | None = None,
        cluster: str | None = None,
    ) -> dict[str, Any] | None:
        """Update a subdomain registration.

        Args:
            subdomain: The subdomain to update
            base_domain: The base domain
            deployment_name: New deployment name (optional)
            cluster: New cluster (optional)

        Returns:
            Updated registration dictionary, or None if not found
        """
        updates = []
        params = [subdomain.lower(), base_domain.lower()]
        param_idx = 3

        if deployment_name is not None:
            updates.append(f"deployment_name = ${param_idx}")
            params.append(deployment_name)
            param_idx += 1

        if cluster is not None:
            updates.append(f"cluster = ${param_idx}")
            params.append(cluster)
            param_idx += 1

        if not updates:
            return await self.get_by_subdomain(subdomain, base_domain)

        pool = self._get_pool()
        conn = await pool.acquire()
        try:
            result = await conn.fetchrow(
                f"""
                UPDATE {self.TABLE_NAME}
                SET {", ".join(updates)}
                WHERE subdomain = $1 AND base_domain = $2
                RETURNING id, subdomain, base_domain, project_name, deployment_name, cluster, created_at, created_by
                """,
                *params,
            )
            return dict(result) if result else None
        finally:
            await pool.release(conn)

    async def list_all(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """List all subdomain registrations.

        Args:
            limit: Maximum number of results to return
            offset: Number of results to skip

        Returns:
            List of registration dictionaries
        """
        pool = self._get_pool()
        conn = await pool.acquire()
        try:
            results = await conn.fetch(
                f"""
                SELECT id, subdomain, base_domain, project_name, deployment_name, cluster, created_at, created_by
                FROM {self.TABLE_NAME}
                ORDER BY subdomain, base_domain
                LIMIT $1 OFFSET $2
                """,
                limit,
                offset,
            )
            return [dict(row) for row in results]
        finally:
            await pool.release(conn)


# Factory function
def create_subdomain_connector() -> SubdomainConnector:
    """Create a SubdomainConnector instance.

    Returns:
        SubdomainConnector instance
    """
    return SubdomainConnector()


# SQL for table creation (used by startup)
SUBDOMAIN_REGISTRY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS subdomain_registry (
    id SERIAL PRIMARY KEY,
    subdomain VARCHAR(63) NOT NULL,
    base_domain VARCHAR(255) NOT NULL,
    project_name VARCHAR(63) NOT NULL,
    deployment_name VARCHAR(63) NOT NULL,
    cluster VARCHAR(63) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    created_by VARCHAR(255),
    UNIQUE (subdomain, base_domain)
);

CREATE INDEX IF NOT EXISTS idx_subdomain_project ON subdomain_registry(project_name);
CREATE INDEX IF NOT EXISTS idx_subdomain_deployment ON subdomain_registry(project_name, deployment_name);
"""
