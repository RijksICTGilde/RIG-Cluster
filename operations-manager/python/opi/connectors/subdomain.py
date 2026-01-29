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
# Dedicated audit logger for subdomain operations
audit_logger = logging.getLogger("opi.audit.subdomain")


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
            base_domain=base_domain_lower, supported=", ".join(sorted(supported_domains))
        )

    return True, None


# DNS subdomain validation constants
SUBDOMAIN_MAX_LENGTH = 63
SUBDOMAIN_MIN_LENGTH = 1
SUBDOMAIN_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?$")

# Reserved subdomains that cannot be registered
# This list includes:
# - Standard infrastructure subdomains (www, api, mail, etc.)
# - Security-sensitive subdomains that could be used for phishing/abuse
# - Email infrastructure subdomains (RFC 2142 compliance)
# - Auto-discovery endpoints that could leak credentials
# - Common administrative and system subdomains
RESERVED_SUBDOMAINS = frozenset(
    [
        # Standard web/infrastructure
        "www",
        "api",
        "admin",
        "mail",
        "ftp",
        "ns1",
        "ns2",
        "ns3",
        "ns4",
        "smtp",
        "pop",
        "pop3",
        "imap",
        "webmail",
        "email",
        # RFC 2142 required mailbox names (security-critical)
        "postmaster",
        "hostmaster",
        "webmaster",
        "abuse",
        "security",
        "noc",
        "info",
        "marketing",
        "sales",
        "usenet",
        "news",
        "uucp",
        "ftp-admin",
        # Auto-discovery endpoints (credential exposure risk)
        "autoconfig",
        "autodiscover",
        "wpad",
        "isatap",
        # Development/staging
        "test",
        "testing",
        "dev",
        "development",
        "staging",
        "stage",
        "prod",
        "production",
        "localhost",
        "local",
        "beta",
        "alpha",
        "demo",
        "sandbox",
        "qa",
        "uat",
        # Support and documentation
        "support",
        "help",
        "docs",
        "documentation",
        "status",
        "health",
        "healthcheck",
        # CDN and static content
        "cdn",
        "static",
        "assets",
        "media",
        "images",
        "img",
        "files",
        "download",
        "downloads",
        "upload",
        "uploads",
        "cache",
        # Version control and CI/CD
        "git",
        "gitlab",
        "github",
        "bitbucket",
        "jenkins",
        "ci",
        "cd",
        "build",
        "deploy",
        "releases",
        # Container/orchestration
        "registry",
        "docker",
        "kubernetes",
        "k8s",
        "argocd",
        "argo",
        "helm",
        # Authentication
        "keycloak",
        "auth",
        "oauth",
        "oauth2",
        "sso",
        "saml",
        "login",
        "logout",
        "register",
        "signup",
        "signin",
        "password",
        "reset",
        "verify",
        "confirm",
        "activate",
        # User-related
        "account",
        "accounts",
        "profile",
        "profiles",
        "user",
        "users",
        "member",
        "members",
        "settings",
        "preferences",
        # Admin interfaces
        "dashboard",
        "portal",
        "panel",
        "console",
        "control",
        "controlpanel",
        "cpanel",
        "plesk",
        "manager",
        "management",
        "admin1",
        "admin2",
        "administrator",
        # System/infrastructure
        "system",
        "sys",
        "root",
        "master",
        "main",
        "primary",
        "secondary",
        "backup",
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
        # Network infrastructure
        "vpn",
        "proxy",
        "gateway",
        "lb",
        "loadbalancer",
        "firewall",
        "router",
        "switch",
        "dns",
        "ssl",
        "tls",
        "cert",
        "certs",
        "certificate",
        "certificates",
        # Monitoring and logging
        "monitoring",
        "monitor",
        "metrics",
        "prometheus",
        "grafana",
        "kibana",
        "elasticsearch",
        "elastic",
        "logs",
        "logging",
        "syslog",
        "splunk",
        "datadog",
        "newrelic",
        "sentry",
        # Database
        "db",
        "database",
        "mysql",
        "postgres",
        "postgresql",
        "redis",
        "mongo",
        "mongodb",
        "minio",
        "s3",
        # Cloud/platform
        "cloud",
        "aws",
        "azure",
        "gcp",
        "google",
        "microsoft",
        "oracle",
        # API versioning (prevent squatting)
        "v1",
        "v2",
        "v3",
        "api-v1",
        "api-v2",
        # Payment/billing (phishing risk)
        "pay",
        "payment",
        "payments",
        "billing",
        "invoice",
        "invoices",
        "checkout",
        "shop",
        "store",
        "cart",
        # Legal/compliance
        "legal",
        "privacy",
        "terms",
        "tos",
        "gdpr",
        "compliance",
        "audit",
    ]
)


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
    # NOTE: "reserved" uses the same message as "taken" to prevent information disclosure
    # (attackers should not be able to enumerate which subdomains are reserved vs in-use)
    messages_nl = {
        "empty": "Subdomein mag niet leeg zijn",
        "too_short": f"Subdomein moet minimaal {SUBDOMAIN_MIN_LENGTH} teken(s) bevatten",
        "too_long": f"Subdomein mag maximaal {SUBDOMAIN_MAX_LENGTH} tekens bevatten",
        "reserved": "Subdomein '{subdomain}' is niet beschikbaar",  # Generic to prevent enumeration
        "start_hyphen": "Subdomein mag niet beginnen met een koppelteken",
        "end_hyphen": "Subdomein mag niet eindigen met een koppelteken",
        "invalid_chars": "Subdomein mag alleen kleine letters (a-z), cijfers (0-9) en koppeltekens (-) bevatten",
    }

    # English error messages
    # NOTE: "reserved" uses the same message as "taken" to prevent information disclosure
    messages_en = {
        "empty": "Subdomain cannot be empty",
        "too_short": f"Subdomain must be at least {SUBDOMAIN_MIN_LENGTH} character(s)",
        "too_long": f"Subdomain cannot exceed {SUBDOMAIN_MAX_LENGTH} characters",
        "reserved": "Subdomain '{subdomain}' is not available",  # Generic to prevent enumeration
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

        # TOCTOU Protection Strategy:
        # We use a two-layer defense against race conditions:
        #
        # Layer 1 (UX): check_availability provides early, user-friendly error messages
        # with context about why registration failed. This catches 99.9% of conflicts.
        #
        # Layer 2 (Security): INSERT...ON CONFLICT DO NOTHING provides atomic protection
        # against the rare case where two requests pass Layer 1 simultaneously.
        # If ON CONFLICT triggers, we detect it via NULL result and handle accordingly.
        #
        # This approach gives us both good UX (informative errors) and security (atomic ops).
        if not await self.check_availability(subdomain_lower, base_domain_lower):
            # Use generic error message to prevent information disclosure
            # (don't reveal whether subdomain is taken by another project or reserved)
            raise SubdomainNotAvailableError(f"Subdomein '{subdomain_lower}.{base_domain_lower}' is niet beschikbaar")

        pool = self._get_pool()
        conn = await pool.acquire()
        try:
            # Atomic INSERT with ON CONFLICT - the true protection against TOCTOU races
            # If a concurrent request registered the same subdomain between our check and
            # this INSERT, ON CONFLICT DO NOTHING will make result=None, which we handle below
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
                # Use generic error message to prevent information disclosure
                raise SubdomainNotAvailableError(
                    f"Subdomein '{subdomain_lower}.{base_domain_lower}' is niet beschikbaar"
                )

            logger.info(
                f"Registered subdomain '{subdomain_lower}.{base_domain_lower}' "
                f"for project '{project_name}', deployment '{deployment_name}'"
            )
            # Audit log for subdomain registration
            audit_logger.info(
                f"SUBDOMAIN_REGISTERED: {subdomain_lower}.{base_domain_lower} "
                f"project={project_name} deployment={deployment_name} cluster={cluster}"
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
                # Audit log for subdomain deletion
                audit_logger.info(f"SUBDOMAIN_DELETED: {subdomain.lower()}.{base_domain.lower()}")
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
                # Audit log for subdomain deletion by project
                audit_logger.info(f"SUBDOMAINS_DELETED_BY_PROJECT: project={project_name} count={count}")
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
                # Audit log for subdomain deletion by deployment
                audit_logger.info(
                    f"SUBDOMAINS_DELETED_BY_DEPLOYMENT: project={project_name} "
                    f"deployment={deployment_name} count={count}"
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
        It uses a database transaction to ensure atomicity - if the new subdomain registration
        fails, the old subdomain is preserved (not lost).

        The method will:
        1. Check if the deployment already has a subdomain registration
        2. If the subdomain hasn't changed, return the existing registration
        3. If the subdomain has changed, atomically delete the old and insert the new
           within a transaction to prevent data loss on failure

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
        # Validate subdomain format first (before any DB operations)
        is_valid, error_message = validate_subdomain(subdomain)
        if not is_valid:
            raise SubdomainValidationError(error_message)

        subdomain_lower = subdomain.lower()
        base_domain_lower = base_domain.lower()

        # Validate base domain against supported domains for this cluster
        is_valid, error_message = validate_base_domain(base_domain_lower, cluster)
        if not is_valid:
            raise BaseDomainValidationError(error_message)

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

            # Subdomain changed - use atomic transaction to prevent data loss
            # If new subdomain registration fails, old subdomain is preserved
            logger.info(
                f"Subdomain changed from '{existing['subdomain']}.{existing['base_domain']}' to "
                f"'{subdomain_lower}.{base_domain_lower}' for deployment '{project_name}/{deployment_name}'"
            )

            return await self._atomic_subdomain_change(
                old_subdomain=existing["subdomain"],
                old_base_domain=existing["base_domain"],
                new_subdomain=subdomain_lower,
                new_base_domain=base_domain_lower,
                project_name=project_name,
                deployment_name=deployment_name,
                cluster=cluster,
                created_by=created_by,
            )

        # No existing registration - register the new subdomain
        return await self.register(
            subdomain=subdomain,
            base_domain=base_domain,
            project_name=project_name,
            deployment_name=deployment_name,
            cluster=cluster,
            created_by=created_by,
        )

    async def _atomic_subdomain_change(
        self,
        old_subdomain: str,
        old_base_domain: str,
        new_subdomain: str,
        new_base_domain: str,
        project_name: str,
        deployment_name: str,
        cluster: str,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        """Atomically change a deployment's subdomain within a transaction.

        This ensures that if the new subdomain registration fails (e.g., already taken),
        the old subdomain is preserved and not lost.

        Args:
            old_subdomain: The existing subdomain to delete
            old_base_domain: The existing base domain
            new_subdomain: The new subdomain to register
            new_base_domain: The new base domain
            project_name: The project name
            deployment_name: The deployment name
            cluster: The cluster
            created_by: Optional creator identifier

        Returns:
            Dictionary with the new registration details

        Raises:
            SubdomainNotAvailableError: If the new subdomain is already taken
        """
        pool = self._get_pool()
        conn = await pool.acquire()
        try:
            # Start transaction
            async with conn.transaction():
                # Step 1: Check if the new subdomain is available (within transaction)
                existing_new = await conn.fetchval(
                    f"""
                    SELECT 1 FROM {self.TABLE_NAME}
                    WHERE subdomain = $1 AND base_domain = $2
                    """,
                    new_subdomain,
                    new_base_domain,
                )

                if existing_new is not None:
                    # New subdomain is taken - transaction will rollback, old subdomain preserved
                    raise SubdomainNotAvailableError(
                        f"Subdomein '{new_subdomain}.{new_base_domain}' is niet beschikbaar"
                    )

                # Step 2: Delete the old subdomain registration
                await conn.execute(
                    f"""
                    DELETE FROM {self.TABLE_NAME}
                    WHERE subdomain = $1 AND base_domain = $2
                    """,
                    old_subdomain,
                    old_base_domain,
                )

                # Step 3: Insert the new subdomain registration
                result = await conn.fetchrow(
                    f"""
                    INSERT INTO {self.TABLE_NAME}
                    (subdomain, base_domain, project_name, deployment_name, cluster, created_by)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    RETURNING id, subdomain, base_domain, project_name, deployment_name, cluster, created_at, created_by
                    """,
                    new_subdomain,
                    new_base_domain,
                    project_name,
                    deployment_name,
                    cluster,
                    created_by,
                )

                # Transaction commits here if no exception

            # Log success after transaction commits
            logger.info(
                f"Atomically changed subdomain from '{old_subdomain}.{old_base_domain}' to "
                f"'{new_subdomain}.{new_base_domain}' for deployment '{project_name}/{deployment_name}'"
            )
            audit_logger.info(
                f"SUBDOMAIN_CHANGED: {old_subdomain}.{old_base_domain} -> {new_subdomain}.{new_base_domain} "
                f"project={project_name} deployment={deployment_name} cluster={cluster}"
            )

            return dict(result)

        except SubdomainNotAvailableError:
            # Re-raise availability errors (transaction already rolled back)
            raise
        except Exception as e:
            logger.exception(
                f"Failed to atomically change subdomain from '{old_subdomain}.{old_base_domain}' to "
                f"'{new_subdomain}.{new_base_domain}'"
            )
            raise SubdomainError(f"Subdomain change failed: {e}") from e
        finally:
            await pool.release(conn)

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

    async def count_all(self) -> int:
        """Count total number of subdomain registrations.

        Returns:
            Total count of registrations
        """
        pool = self._get_pool()
        conn = await pool.acquire()
        try:
            result = await conn.fetchval(f"SELECT COUNT(*) FROM {self.TABLE_NAME}")
            return result or 0
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
