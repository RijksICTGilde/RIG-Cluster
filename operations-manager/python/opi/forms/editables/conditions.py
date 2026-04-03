"""Editable conditions for controlling field behavior.

Conditions implement the ``EditableCondition`` protocol and are used in two places:

- ``defer_when``: determines when one field should yield its value to another.
- ``show_when``: determines field visibility based on computed logic
  (receives full ``yaml_data`` instead of a single dependency value).
"""

from __future__ import annotations

from typing import Any

from opi.connectors.subdomain import (
    get_project_allowed_domain_config,
    get_subdomain_status,
    get_supported_base_domains,
)
from opi.core import config as opi_config
from opi.core.cluster_config import get_ingress_postfix, is_domain_subdomain_restricted
from opi.forms.editables.resolvers import get_effective_value


class SentinelValueCondition:
    """True when the field value equals a sentinel that indicates deferral.

    Used by "select with other" patterns: when the select's value is the
    sentinel (e.g. ``__custom__``), the parent defers to the transient
    text field for its final value.
    """

    def __init__(self, sentinel: str = "__custom__") -> None:
        self.sentinel = sentinel

    def check(self, value: Any) -> bool:
        if not value:
            return False
        return str(value) == self.sentinel


class SubdomainNeedsRequestCondition:
    """True when the deployment's subdomain needs a request on a restricted domain.

    Used as ``show_when`` on the subdomain-request checkbox. When used this way,
    ``check()`` receives the full ``yaml_data`` dict.

    Returns True when:
    - The deployment uses a subdomain-based domain format
    - The base domain has restricted subdomains (cluster config)
    - The subdomain is NOT already approved in ``domains.allowed-subdomains``
    """

    def __init__(self, deployment_index: int = 0) -> None:
        self.deployment_index = deployment_index
        self._resolvers: dict | None = None

    def set_resolvers(self, resolvers: dict) -> None:
        """Inject resolver map for transient value resolution."""
        self._resolvers = resolvers

    def check(self, value: Any) -> bool:
        if not isinstance(value, dict):
            return False

        deployments = value.get("deployments", [])
        if len(deployments) <= self.deployment_index:
            return False
        dep = deployments[self.deployment_index]
        if not isinstance(dep, dict):
            return False

        subdomain = dep.get("subdomain")
        base_domain = get_effective_value(value, f"deployments[{self.deployment_index}]/base-domain", self._resolvers)
        if not subdomain or not base_domain or base_domain == "__custom__":
            return False

        cluster = opi_config.settings.CLUSTER_MANAGER

        supported = get_supported_base_domains(cluster)
        if base_domain in supported:
            if not is_domain_subdomain_restricted(cluster, base_domain):
                return False
        else:
            custom_config = get_project_allowed_domain_config(value, base_domain)
            if not custom_config or not custom_config.get("restricted-subdomains", False):
                return False

        # Domain is restricted — check if subdomain is already approved
        status = get_subdomain_status(value, base_domain, subdomain)
        return status != "approved"


class DomainNeedsRequestCondition:
    """True when the deployment's base domain is not the cluster default and not approved.

    Used as ``show_when`` on the domain-request checkbox. When the condition
    returns True, the user needs to request approval for this domain.

    Returns True when:
    - A base domain is selected (not None, not __custom__)
    - The domain is NOT the cluster default
    - The domain is NOT in ``domains.allowed-domains`` with ``status: approved``
    """

    def __init__(self, deployment_index: int = 0) -> None:
        self.deployment_index = deployment_index
        self._resolvers: dict | None = None

    def set_resolvers(self, resolvers: dict) -> None:
        """Inject resolver map for transient value resolution."""
        self._resolvers = resolvers

    def check(self, value: Any) -> bool:
        if not isinstance(value, dict):
            return False

        deployments = value.get("deployments", [])
        if len(deployments) <= self.deployment_index:
            return False
        dep = deployments[self.deployment_index]
        if not isinstance(dep, dict):
            return False

        base_domain = get_effective_value(value, f"deployments[{self.deployment_index}]/base-domain", self._resolvers)
        if not base_domain or base_domain == "__custom__":
            return False

        cluster = opi_config.settings.CLUSTER_MANAGER

        # Cluster default domain is always allowed
        ingress_postfix = get_ingress_postfix(cluster)
        cluster_domain = ingress_postfix.lstrip(".")
        if base_domain == cluster_domain:
            return False

        # Check if domain is already approved
        domain_config = get_project_allowed_domain_config(value, base_domain)
        return not (domain_config and domain_config.get("status") == "approved")
