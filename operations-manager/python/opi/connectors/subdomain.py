"""
Subdomain registry connector for managing nice URL subdomains.

This module provides functionality to manage globally unique subdomains for nice URLs.
Subdomains are registered per (subdomain, base_domain) pair and associated with projects.
"""

import logging
import re
from datetime import UTC, datetime
from typing import Any

from opi.core.cluster_config import (
    CLUSTER_CONFIG,
    get_ingress_postfix,
    is_domain_subdomain_restricted,
)
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, get_domain_setting
from opi.utils.naming import DOMAIN_FORMAT_TEMPLATES

logger = logging.getLogger(__name__)
# Dedicated audit logger for subdomain operations
audit_logger = logging.getLogger("opi.audit.subdomain")


# ---------------------------------------------------------------------------
# Domain approval state location (RC-5)
#
# The domain/subdomain approval block used to live at the project ROOT
# (``domains:``). It now belongs to the publish-on-web service, under its
# root-level service *definition* config: ``services/[publish-on-web]/config/domains``.
# The block is project-global (one allow-list per project), and publish-on-web is a
# root-level service definition (components/deployments merely reference it), so the
# service-definition config is its natural home.
#
# The canonical relocation is the versioned schema migration
# ``schema_migration.normalize_domains_location`` (v2.4 -> v2.5), which runs on load and
# delegates the placement to :func:`ensure_domains_config` here (the single authority on
# where the block lives). Readers accept BOTH locations (:func:`get_domains_config`) so a
# not-yet-migrated file keeps working; writers use :func:`ensure_domains_config` so a
# write always lands at -- and consolidates onto -- the service path. No schema change is
# needed (service ``config`` is free-form) and old files keep validating + reading.
# ---------------------------------------------------------------------------


def _find_publish_on_web_definition(project_data: dict[str, Any]) -> Any:
    """Return the root ``services:`` entry that defines publish-on-web, or None."""
    from opi.services.services import service_entry_name
    from opi.services.services_enums import ServiceType

    for entry in project_data.get("services") or []:
        if service_entry_name(entry) == ServiceType.PUBLISH_ON_WEB.value:
            return entry
    return None


def get_domains_config(project_data: dict[str, Any]) -> dict[str, Any] | None:
    """Read the domain-approval block, preferring the service path, root as fallback.

    Read-both so files not yet migrated keep working; never mutates ``project_data``.
    """
    entry = _find_publish_on_web_definition(project_data)
    if isinstance(entry, dict):
        config = entry.get("config")
        if isinstance(config, dict) and isinstance(config.get("domains"), dict):
            return config["domains"]
    root = project_data.get("domains")
    return root if isinstance(root, dict) else None


def ensure_domains_config(project_data: dict[str, Any]) -> dict[str, Any]:
    """Return the writable service ``config/domains`` block, creating it as needed.

    The single authority on WHERE the approval block lives, used both by the v2.5 schema
    migration (``normalize_domains_location``) and by the runtime write paths. Absorbs a
    legacy root ``domains:`` block into the publish-on-web service config and removes the
    root copy so state never splits, promotes a bare ``- publish-on-web`` service string
    to a ``{name, config}`` record, and adds the service definition if it is absent. On
    already-migrated data there is no root block to absorb -- it just returns the
    existing service block.
    """
    from opi.services.services_enums import ServiceType

    services = project_data.get("services")
    if not isinstance(services, list):
        services = []
        project_data["services"] = services

    entry = _find_publish_on_web_definition(project_data)
    if not isinstance(entry, dict):
        record = {"name": ServiceType.PUBLISH_ON_WEB.value, "config": {}}
        if entry is None:
            services.append(record)
        else:  # bare string definition -> promote in place
            services[services.index(entry)] = record
        entry = record

    config = entry.get("config")
    if not isinstance(config, dict):
        config = {}
        entry["config"] = config

    domains = config.get("domains")
    if not isinstance(domains, dict):
        legacy_root = project_data.get("domains")
        domains = legacy_root if isinstance(legacy_root, dict) else {}
        config["domains"] = domains
    project_data.pop("domains", None)
    return domains


#: Why a bare (apex) domain is refused on a platform domain. One constant so the form
#: enforcer and the publication path refuse for the same reason, in the same words.
BARE_DOMAIN_PLATFORM_MESSAGE = "Kaal domein is alleen beschikbaar voor eigen domeinen, niet voor platformdomeinen"


def validate_bare_domain_allowed(base_domain: str, supported_domains: set[str]) -> None:
    """Raise when exposing a component on the bare (apex) domain is not permitted.

    A bare-domain ingress claims the apex of ``base_domain`` -- and its Let's Encrypt
    certificate -- from one tenant namespace. That is only acceptable for a domain the
    project brought itself; on a platform domain it takes the domain away from every
    other tenant on the cluster.

    Both the form enforcer and the publication path call this, so the rule cannot hold on
    one write path and not on the other. The supported set is passed in rather than looked
    up here: the caller already knows which cluster it is deciding for.
    """
    if base_domain.lower() in supported_domains:
        raise ValueError(BARE_DOMAIN_PLATFORM_MESSAGE)


def get_supported_base_domains(cluster: str | None = None) -> set[str]:
    """Get all supported base domains for nice URLs.

    Args:
        cluster: Optional cluster name to get domains for specific cluster.
                 If None, returns all supported domains across all clusters.

    Returns:
        Set of supported base domain strings
    """

    def _extract_domain(entry: str | dict) -> str:
        return entry["domain"] if isinstance(entry, dict) else entry

    if cluster and cluster in CLUSTER_CONFIG:
        nice_url_config = CLUSTER_CONFIG[cluster].get("nice_url", {})
        return {_extract_domain(d) for d in nice_url_config.get("supported_domains", [])}

    # Collect all supported domains from all clusters
    all_domains: set[str] = set()
    for cluster_config in CLUSTER_CONFIG.values():
        nice_url_config = cluster_config.get("nice_url", {})
        all_domains.update(_extract_domain(d) for d in nice_url_config.get("supported_domains", []))
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
        # Accept any syntactically valid domain (custom domain support)
        if re.match(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)+$", base_domain_lower):
            return True, None
        return False, messages["not_supported"].format(
            base_domain=base_domain_lower, supported=", ".join(sorted(supported_domains))
        )

    return True, None


def get_project_allowed_subdomains(project_data: dict[str, Any], domain: str) -> list[dict[str, Any]]:
    """Get allowed subdomain details for a specific domain from project data.

    Args:
        project_data: Parsed project YAML data
        domain: The base domain to look up (e.g., "rijks.app")

    Returns:
        List of subdomain detail dicts (name, status, history).
        Empty list if no entry found.
    """
    domains_config = get_domains_config(project_data)
    if not domains_config:
        return []
    for entry in domains_config.get("allowed-subdomains", []):
        if isinstance(entry, dict) and entry.get("domain") == domain:
            return entry.get("subdomains", [])
    return []


def get_subdomain_status(project_data: dict[str, Any], domain: str, subdomain: str) -> str | None:
    """Get the approval status for a specific subdomain on a domain.

    Returns:
        The status string ('requested', 'approved', 'denied') or None if
        the subdomain is not in the allow-list.
    """
    for detail in get_project_allowed_subdomains(project_data, domain):
        if isinstance(detail, dict) and detail.get("name", "").lower() == subdomain.lower():
            return detail.get("status")
    return None


def get_project_allowed_domain_config(project_data: dict[str, Any], domain: str) -> dict[str, Any] | None:
    """Get allowed domain configuration from project data.

    Looks up domain in ``domains.allowed-domains``. Works for both
    platform and custom domains — the list is unified.

    Args:
        project_data: Parsed project YAML data
        domain: The domain to look up (e.g., "rijks.app", "mijn-app.nl")

    Returns:
        Domain config dict if found, None otherwise.
    """
    domains_config = get_domains_config(project_data)
    if not domains_config:
        return None
    for entry in domains_config.get("allowed-domains", []):
        if isinstance(entry, dict) and entry.get("domain") == domain:
            return entry
    return None


def is_deployment_domain_approved(
    project_data: dict[str, Any],
    base_domain: str | None,
    subdomain: str | None,
    cluster: str,
) -> bool:
    """Check if a deployment's domain+subdomain combination is approved for use.

    Unified approval check — no distinction between custom and predefined domains.
    The only domain that's always allowed without approval is the cluster default.

    Rules:
    1. Cluster default domain (ingress_postfix) → always True
    2. Any other domain → must be in the project's domains section with status approved
    3. If the domain has restricted subdomains → subdomain must also be approved

    Args:
        project_data: Parsed project YAML data
        base_domain: The base domain (e.g., "rijks.app", "mijn-app.nl")
        subdomain: The subdomain (e.g., "wies"), or None if not used
        cluster: Cluster name for config lookup
    """
    if not base_domain:
        return True  # No domain specified, using cluster default

    # Check if this is the cluster default domain
    ingress_postfix = get_ingress_postfix(cluster)
    cluster_domain = ingress_postfix.lstrip(".")
    if base_domain == cluster_domain:
        return True

    # Check domain approval in allowed-domains (applies to ALL non-default domains)
    domain_config = get_project_allowed_domain_config(project_data, base_domain)
    if not domain_config:
        return False  # Domain not in allowed-domains
    if domain_config.get("status") != "approved":
        return False  # Domain not approved

    # Domain approved — check subdomain if present and restricted
    if subdomain:
        # Check cluster-level restriction
        supported = get_supported_base_domains(cluster)
        if base_domain in supported and is_domain_subdomain_restricted(cluster, base_domain):
            status = get_subdomain_status(project_data, base_domain, subdomain)
            return status == "approved"
        # Check domain-level restriction (for custom domains with restricted-subdomains)
        if domain_config.get("restricted-subdomains", False):
            status = get_subdomain_status(project_data, base_domain, subdomain)
            return status == "approved"

    return True


def find_deployments_for_domain_item(project_data: dict[str, Any], item: dict[str, Any]) -> list[str]:
    """Return names of deployments that use the domain/subdomain in an approval item.

    Used to scope a redeploy on domain/subdomain approval to only the affected
    deployment(s) instead of reprocessing the whole project.

    A deployment uses an item when its ``base-domain`` equals the item's domain.
    For ``subdomain`` items the deployment's ``subdomain`` must also match the
    item's ``name``; ``domain`` items match every deployment on that base domain.

    Args:
        project_data: Parsed project YAML data
        item: An approval item: ``{type, domain, name, ...}`` (see _approval_items)

    Returns:
        Deployment names referencing the item (may be empty).
    """
    domain = item.get("domain", "")
    if not domain:
        return []

    sub_name = item.get("name", "") if item.get("type") == "subdomain" else None

    result: list[str] = []
    for dep in project_data.get("deployments", []):
        if not isinstance(dep, dict):
            continue
        if get_domain_setting(dep, DomainSetting.BASE_DOMAIN) != domain:
            continue
        if sub_name is not None and get_domain_setting(dep, DomainSetting.SUBDOMAIN) != sub_name:
            continue
        name = dep.get("name")
        if name:
            result.append(name)
    return result


def is_domain_format_dot_based(domain_format: str) -> bool:
    """Check if a domain format uses dot notation between parts.

    Dot-based formats (e.g., ``component.subdomain``) create multi-level
    hostnames. The format ID itself uses dots vs dashes to indicate this.

    Examples:
        "component.subdomain" → True
        "component-subdomain" → False
        "subdomain" → False (single part)
        "component-deployment-project" → False
    """
    # Format IDs use dots for dot-based, dashes for dash-based
    # Strip trailing domain part if present in ID
    parts_before_domain = domain_format.replace(".domain", "")
    return "." in parts_before_domain


def is_subdomain_allowed_for_project(
    subdomain: str,
    base_domain: str,
    project_data: dict[str, Any],
    cluster: str,
) -> tuple[bool, str | None]:
    """Check if a subdomain is allowed for a project on a restricted domain.

    For domains with restricted_subdomains in cluster config, the subdomain
    must appear in the project's allowed-subdomains list.

    For custom domains with restricted-subdomains, the subdomain must appear
    in the project's allowed-subdomains list for that custom domain.

    Args:
        subdomain: The subdomain to check
        base_domain: The base domain
        project_data: Parsed project YAML data
        cluster: Cluster name

    Returns:
        Tuple of (is_allowed, error_message). If allowed, error_message is None.
    """
    # Check if this is a platform domain with restrictions
    supported = get_supported_base_domains(cluster)
    if base_domain in supported:
        if not is_domain_subdomain_restricted(cluster, base_domain):
            return True, None
    else:
        # Custom domain - check project-level restriction
        custom_config = get_project_allowed_domain_config(project_data, base_domain)
        if custom_config and not custom_config.get("restricted-subdomains", False):
            return True, None
        if not custom_config:
            return True, None  # No config means no restriction at domain level

    # Domain is restricted - check project allow-list
    status = get_subdomain_status(project_data, base_domain, subdomain)
    if status is None:
        return False, (
            f"Het domein '{base_domain}' heeft subdomeinen beperkt. "
            f"Het subdomein '{subdomain}' is niet aangevraagd voor dit project."
        )
    if status == "approved":
        return True, None
    if status == "requested":
        return False, (f"Het subdomein '{subdomain}' op '{base_domain}' is aangevraagd maar nog niet goedgekeurd.")
    # status == "denied"
    return False, (f"Het subdomein '{subdomain}' op '{base_domain}' is afgewezen.")


def is_domain_allowed_for_project(
    domain: str,
    project_data: dict[str, Any],
) -> tuple[bool, str | None]:
    """Check if a domain is approved for use in a project.

    All non-default domains must be listed in ``domains.allowed-domains``
    with ``status: approved`` to be used in deployments.

    Args:
        domain: The domain to check (e.g., "rijks.app", "mijn-app.nl")
        project_data: Parsed project YAML data

    Returns:
        Tuple of (is_allowed, error_message). If allowed, error_message is None.
    """
    domain_config = get_project_allowed_domain_config(project_data, domain)
    if domain_config is None:
        return False, (
            f"Het domein '{domain}' is niet goedgekeurd voor dit project. Vraag het domein aan via de wizard."
        )
    status = domain_config.get("status", "")
    if status != "approved":
        return False, (
            f"Het domein '{domain}' heeft status '{status}' en kan nog niet worden gebruikt. "
            f"Alleen domeinen met status 'approved' mogen worden ingezet."
        )
    return True, None


def ensure_domain_requests(project_data: dict[str, Any], cluster: str) -> None:
    """Ensure unapproved domains and subdomains have request entries.

    Scans all deployments in the project data. For each deployment using
    a domain or subdomain that isn't approved, adds a ``status: requested``
    entry to the project's ``domains`` section.

    Called by both the wizard (via hooks) and the API when processing
    project YAML. Mutates ``project_data`` in place.

    Args:
        project_data: Parsed project YAML data (mutated in place)
        cluster: Cluster name for config lookup
    """
    ingress_postfix = get_ingress_postfix(cluster)
    cluster_domain = ingress_postfix.lstrip(".")

    for dep in project_data.get("deployments", []):
        if not isinstance(dep, dict):
            continue

        # An absent base-domain means the cluster's own domain: the wizard hook
        # deliberately does not persist it when it equals the cluster default
        # (``_resolve_missing_base_domains`` in opi/forms/editables/hooks.py).
        # Treating that emptiness as "nothing to do" skipped the whole deployment,
        # including the subdomain branch below that is written for exactly this
        # case, so every subdomain request on the cluster domain vanished silently.
        base_domain = get_domain_setting(dep, DomainSetting.BASE_DOMAIN) or cluster_domain
        subdomain = get_domain_setting(dep, DomainSetting.SUBDOMAIN)
        domain_format = get_domain_setting(dep, DomainSetting.DOMAIN_FORMAT, "")
        template = DOMAIN_FORMAT_TEMPLATES.get(domain_format, "")

        if base_domain == "__custom__":
            continue

        is_cluster_default = base_domain == cluster_domain

        # --- Domain-level request (skip for cluster default) ---
        if not is_cluster_default:
            domain_config = get_project_allowed_domain_config(project_data, base_domain)
            if domain_config is None:
                domains_section = ensure_domains_config(project_data)
                allowed_domains = domains_section.setdefault("allowed-domains", [])
                now = datetime.now(UTC).isoformat()
                allowed_domains.append(
                    {
                        "domain": base_domain,
                        "status": "requested",
                        "history": [{"date": now, "status": "requested"}],
                    }
                )
                logger.info("Domain request created: %s", base_domain)

        # --- Subdomain-level request ---
        if not subdomain or "{subdomain}" not in template:
            continue

        # Check if domain restricts subdomains
        supported = get_supported_base_domains(cluster)
        is_restricted = False
        if base_domain in supported:
            is_restricted = is_domain_subdomain_restricted(cluster, base_domain)
        else:
            dc = get_project_allowed_domain_config(project_data, base_domain)
            is_restricted = bool(dc and dc.get("restricted-subdomains", False))

        if not is_restricted:
            continue

        if get_subdomain_status(project_data, base_domain, subdomain) is not None:
            continue

        domains_section = ensure_domains_config(project_data)
        allowed_subdomains = domains_section.setdefault("allowed-subdomains", [])

        domain_entry = None
        for entry in allowed_subdomains:
            if isinstance(entry, dict) and entry.get("domain") == base_domain:
                domain_entry = entry
                break
        if domain_entry is None:
            domain_entry = {"domain": base_domain, "subdomains": []}
            allowed_subdomains.append(domain_entry)

        now = datetime.now(UTC).isoformat()
        domain_entry["subdomains"].append(
            {
                "name": subdomain.lower(),
                "status": "requested",
                "history": [{"date": now, "status": "requested"}],
            }
        )
        logger.info("Subdomain request created: %s.%s", subdomain, base_domain)


# Sentinel value for bare domain registrations in subdomain_registry.
# Uses the DNS convention "@" to represent the apex/bare domain.
BARE_DOMAIN_SUBDOMAIN = "@"

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
