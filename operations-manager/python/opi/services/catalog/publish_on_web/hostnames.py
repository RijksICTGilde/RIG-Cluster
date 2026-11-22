"""The web address of a component: which template renders it, and whether it may.

Publish-on-web decides the shape of a hostname (the ``domain-format`` templates) and
whether the domain it names is approved for this project. The generic name builders it
leans on live in :mod:`opi.utils.naming`.
"""

import logging
from typing import Any

from opi.services.catalog.publish_on_web.domain_config import (
    DOMAIN_FORMAT_TEMPLATES,
    ROOT_COMPONENT_FORMAT_IDS,
    SAFE_FALLBACK_FORMAT,
    DomainSetting,
    get_domain_setting,
    subdomain_used_by_format,
)
from opi.services.catalog.publish_on_web.subdomain import is_deployment_domain_approved
from opi.utils.naming import (
    _sanitize_for_lowercase,
    generate_bare_domain_hostname,
    generate_external_hostname,
    generate_ingress_map,
    generate_root_hostname,
    generate_unique_name,
)

logger = logging.getLogger(__name__)


def resolve_domain_tail(
    base_domain: str | None,
    ingress_postfix: str,
) -> str:
    """Resolve the domain tail for template interpolation.

    Returns base_domain when set, otherwise ingress_postfix with leading dot stripped.
    """
    if base_domain:
        return base_domain
    return ingress_postfix.lstrip(".")


def generate_hostname_from_format(
    domain_format: str,
    component_name: str,
    deployment_name: str,
    project_name: str,
    subdomain: str | None,
    domain: str,
) -> str:
    """Resolve a hostname from a domain-format template.

    Args:
        domain_format: Template ID from DOMAIN_FORMAT_TEMPLATES
        component_name: Component name
        deployment_name: Deployment name
        project_name: Project name
        subdomain: Optional subdomain
        domain: Domain tail (resolved via resolve_domain_tail)

    Raises:
        ValueError: If domain_format is not a known template ID.
    """
    template = DOMAIN_FORMAT_TEMPLATES.get(domain_format)
    if template is None:
        raise ValueError(f"Unknown domain-format: {domain_format}")

    return template.format(
        component=_sanitize_for_lowercase(component_name),
        deployment=_sanitize_for_lowercase(deployment_name),
        project=_sanitize_for_lowercase(project_name),
        subdomain=_sanitize_for_lowercase(subdomain or ""),
        domain=domain,
    )


def find_root_component(deployment: dict) -> str | None:
    """
    Find the root component for a deployment.

    The root component receives traffic for the root hostname (subdomain.base_domain)
    in nice-url mode. It is specified via the ``root-component`` field on the deployment.

    Args:
        deployment: Deployment dictionary with optional 'root-component' field

    Returns:
        Component name, or None if no root component is configured

    Examples:
        >>> find_root_component({"name": "prod", "root-component": "frontend"})
        'frontend'

        >>> find_root_component({"name": "prod"})
        None
    """
    return get_domain_setting(deployment, DomainSetting.ROOT_COMPONENT)


def apply_domain_approval_fallback(
    domain_format: str | None,
    base_domain: str | None,
    subdomain: str | None,
    ingress_postfix: str,
    project_data: dict[str, Any],
    cluster: str,
) -> tuple[str | None, str | None]:
    """Check domain approval and return the effective format + domain.

    If the requested domain+subdomain combination is approved, returns
    them unchanged. If not approved, falls back to the safe format
    (component-deployment-project) on the cluster domain.

    ``domain_format`` may be None: a deployment that names no format composes its
    hostname through the legacy dispatch in :func:`get_component_ingress_map`, and that
    shape needs the very same verdict. An approved domain is handed back untouched
    (None stays None, so the caller keeps its own dispatch); an unapproved one gets the
    safe format regardless of what was asked for, because "no format named" is not a
    reason to publish on a domain nobody approved.

    Args:
        domain_format: Requested domain format ID, or None when none is named
        base_domain: Requested base domain
        subdomain: Requested subdomain
        ingress_postfix: Cluster ingress postfix (for fallback domain)
        project_data: Project YAML data (for approval check)
        cluster: Cluster name (for approval check)
    """
    used_subdomain = subdomain_used_by_format(subdomain, domain_format)
    if is_deployment_domain_approved(project_data, base_domain, used_subdomain, cluster):
        return domain_format, base_domain

    # Not approved: fall back to safe format on cluster domain. The log names the address
    # that lapses, because that is what someone wondering where their address went will
    # search for.
    cluster_domain = ingress_postfix.lstrip(".")
    logger.warning(
        "Address '%s' is not in use: domain '%s' is not approved for this project, publishing on '%s' (%s) instead",
        f"{subdomain}.{base_domain}" if subdomain else base_domain,
        base_domain,
        cluster_domain,
        SAFE_FALLBACK_FORMAT,
    )
    return SAFE_FALLBACK_FORMAT, cluster_domain


def get_component_ingress_map(
    component_name: str,
    deployment_name: str,
    project_name: str,
    ingress_postfix: str,
    subdomain: str | None = None,
    base_domain: str | None = None,
    domain_format: str | None = None,
    *,
    project_data: dict[str, Any],
    cluster: str,
) -> dict[str, str]:
    """
    Get the ingress map for a single component.

    Centralizes the hostname/ingress generation logic used by both keycloak_manager
    and project_manager to avoid duplication.

    When ``domain_format`` is set (e.g. ``"deployment-subdomain"``), the hostname
    is resolved from the matching template in ``DOMAIN_FORMAT_TEMPLATES``.
    When ``domain_format`` is ``None``, the legacy dispatch logic is used unchanged.

    Args:
        component_name: Name of the component
        deployment_name: Name of the deployment
        project_name: Name of the project
        ingress_postfix: Cluster ingress postfix
        subdomain: Optional subdomain override
        base_domain: Optional custom base domain (e.g., "rijks.app")
        domain_format: Optional domain-format template ID from project YAML

    Examples:
        # A custom domain without a format: subdomain.base_domain.
        >>> get_component_ingress_map(
        ...     "frontend", "prod", "myapp", ".kind",
        ...     subdomain="myapp", base_domain="custom.nl"
        ... )
        {'prod-frontend': 'myapp.custom.nl'}

        # The cluster domain: component-deployment-project.cluster.
        >>> get_component_ingress_map(
        ...     "frontend", "prod", "myapp", ".kind"
        ... )
        {'prod-frontend': 'frontend-prod-myapp.kind'}

        # Explicit domain-format with dots
        >>> get_component_ingress_map(
        ...     "frontend", "poc", "myapp", ".kind",
        ...     subdomain="moza", base_domain="rijksapp.dev",
        ...     domain_format="deployment.subdomain"
        ... )
        {'poc-frontend': 'poc.moza.rijksapp.dev'}
    """
    base_name = generate_unique_name(deployment_name, component_name)

    # The approval gate runs BEFORE a shape is chosen, so it covers every shape. It used
    # to sit inside the branch below, which meant a deployment that names no
    # domain-format -- a write that only set base-domain and subdomain -- composed its
    # hostname in the legacy dispatch with nobody having checked whether the domain was
    # approved. That published an unapproved domain AND showed it as the component's
    # address, because this function is also what the portal and the API read (RC-104).
    effective_format, effective_domain = apply_domain_approval_fallback(
        domain_format, base_domain, subdomain, ingress_postfix, project_data, cluster
    )

    # A named format, or the safe one an unapproved domain fell back to, resolves through
    # the template.
    if effective_format and effective_format in DOMAIN_FORMAT_TEMPLATES:
        domain = resolve_domain_tail(effective_domain, ingress_postfix)
        hostname = generate_hostname_from_format(
            domain_format=effective_format,
            component_name=component_name,
            deployment_name=deployment_name,
            project_name=project_name,
            subdomain=subdomain,
            domain=domain,
        )
        return {base_name: hostname}

    # Legacy dispatch: no domain_format, and the domain it names IS approved.

    # Custom/external domain (base_domain with subdomain)
    if base_domain and subdomain:
        hostname = generate_external_hostname(subdomain, base_domain)
        return {base_name: hostname}

    # Default cluster domain
    return generate_ingress_map(component_name, deployment_name, project_name, ingress_postfix, subdomain)


def get_deployment_hostnames(
    component_names: list[str],
    deployment_name: str,
    project_name: str,
    ingress_postfix: str,
    subdomain: str | None = None,
    base_domain: str | None = None,
    domain_format: str | None = None,
    expose_on_bare_domain: str | bool = False,
    root_component: str | None = None,
    *,
    project_data: dict[str, Any],
    cluster: str,
) -> list[str]:
    """
    Get all hostnames for components in a deployment.

    Centralizes the hostname generation logic used by keycloak_manager.

    Args:
        component_names: List of component names that need hostnames
        deployment_name: Name of the deployment
        project_name: Name of the project
        ingress_postfix: Cluster ingress postfix
        subdomain: Optional subdomain override
        base_domain: Optional custom base domain (e.g., "rijks.app")
        domain_format: Optional domain-format template ID from project YAML
        expose_on_bare_domain: Component name that serves the bare domain,
            or False/empty when disabled
        root_component: Component that serves the root address
            (``subdomain.base-domain``), or None when no component does
    """
    hostnames: list[str] = []

    for component_name in component_names:
        ingress_map = get_component_ingress_map(
            component_name,
            deployment_name,
            project_name,
            ingress_postfix,
            subdomain,
            base_domain,
            domain_format=domain_format,
            project_data=project_data,
            cluster=cluster,
        )
        hostname = next(iter(ingress_map.values()))
        if hostname not in hostnames:
            hostnames.append(hostname)

    # The root hostname is the one address here that is NOT composed by
    # get_component_ingress_map, so the approval verdict that function applies does not
    # reach it. Without this the components of an unapproved domain move to the cluster
    # address while ``subdomain.base-domain`` is still handed out as a hostname.
    domain_approved = is_deployment_domain_approved(project_data, base_domain, subdomain, cluster)

    # The dotted formats with a component prefix carry a root address next to the
    # per-component ones: ``subdomain.base-domain``, served by the root component. Keyed
    # on the format plus root-component rather than the retired ``domain-mode``: nice-url
    # files migrated to ``component.subdomain`` (schema v2.8), and the same pair gates the
    # root INGRESS in project_manager, so the Keycloak redirect list and the ingress set
    # cannot disagree.
    if domain_approved and domain_format in ROOT_COMPONENT_FORMAT_IDS and root_component and subdomain and base_domain:
        root_hostname = generate_root_hostname(subdomain, base_domain)
        if root_hostname not in hostnames:
            hostnames.append(root_hostname)

    # The apex hangs on the same approval as the root address above. Ungated, it stayed in
    # the list while every other address of that domain fell back to the cluster one.
    if domain_approved and expose_on_bare_domain and base_domain:
        bare_hostname = generate_bare_domain_hostname(base_domain)
        if bare_hostname not in hostnames:
            hostnames.append(bare_hostname)

    return hostnames
