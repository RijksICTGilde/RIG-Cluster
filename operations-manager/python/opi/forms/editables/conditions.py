"""Editable conditions for controlling field behavior.

Conditions implement the ``EditableCondition`` protocol and are used in two places:

- ``defer_when``: determines when one field should yield its value to another.
- ``show_when``: determines field visibility based on computed logic
  (receives full ``yaml_data`` instead of a single dependency value).
"""

from __future__ import annotations

from typing import Any

from opi.core import config as opi_config
from opi.core.cluster_config import get_ingress_postfix, is_domain_subdomain_restricted
from opi.forms.editables.resolvers import get_effective_value
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, domain_setting_path, get_domain_setting
from opi.services.catalog.publish_on_web.subdomain import (
    get_project_allowed_domain_config,
    get_subdomain_status,
    get_supported_base_domains,
)

#: The value of the "own domain" choice, and the key of the transient field it defers to.
#: They belong together: the one says "look there", the other carries the value.
CUSTOM_DOMAIN_SENTINEL = "__custom__"
CUSTOM_BASE_DOMAIN_KEY = "base-domain:custom"


def _effective_base_domain(
    yaml_data: dict[str, Any],
    deployment: dict[str, Any],
    deployment_index: int,
    resolvers: dict | None,
) -> Any:
    """The base domain a condition should judge by: stored value, else the resolver.

    Asks the service first (``get_domain_setting``), so both storage locations answer --
    the resolver map is keyed on the editable's yaml_path, which is the service path only,
    and a deployment that still carries its base domain at the root would otherwise read as
    "not set" and get the cluster default. Falls through to ``get_effective_value``
    when nothing is stored, which is what makes "cluster default" resolvable at all.

    ``__custom__`` does not count as a stored value: it is the switch of the select, and the
    domain itself then sits in the transient field ``base-domain:custom``. While rendering that
    deferral is resolved, while PROCESSING it is not yet (``_resolve_deferrals`` runs after the
    field loop), so returning the sentinel here makes both request conditions read as hidden,
    the processor skips the request checkbox, and a ticked box is thrown away.
    """
    stored = get_domain_setting(deployment, DomainSetting.BASE_DOMAIN)
    if stored is not None and stored != CUSTOM_DOMAIN_SENTINEL:
        return stored
    if stored == CUSTOM_DOMAIN_SENTINEL:
        custom = deployment.get(CUSTOM_BASE_DOMAIN_KEY)
        if custom:
            return custom
        return stored
    return get_effective_value(yaml_data, domain_setting_path(DomainSetting.BASE_DOMAIN, deployment_index), resolvers)


class SentinelValueCondition:
    """True when the field value equals the sentinel that indicates deferral.

    For "select with other": on the sentinel the parent takes its final value from the
    transient text field instead.
    """

    def __init__(self, sentinel: str = CUSTOM_DOMAIN_SENTINEL) -> None:
        self.sentinel = sentinel

    def check(self, value: Any) -> bool:
        if not value:
            return False
        return str(value) == self.sentinel


class SubdomainNeedsRequestCondition:
    """True when the deployment's subdomain needs a request on a restricted domain.

    Used as ``show_when`` on the subdomain-request checkbox, so ``check()`` receives the full
    ``yaml_data`` dict rather than one dependency value.
    """

    def __init__(self, deployment_index: int = 0) -> None:
        self.deployment_index = deployment_index
        self._resolvers: dict | None = None

    def set_resolvers(self, resolvers: dict) -> None:
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

        subdomain = get_domain_setting(dep, DomainSetting.SUBDOMAIN)
        base_domain = _effective_base_domain(value, dep, self.deployment_index, self._resolvers)
        if not subdomain or not base_domain or base_domain == CUSTOM_DOMAIN_SENTINEL:
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

        status = get_subdomain_status(value, base_domain, subdomain)
        return status != "approved"


class DomainNeedsRequestCondition:
    """True when the deployment's base domain is not the cluster default and not approved.

    Used as ``show_when`` on the domain-request checkbox, so ``check()`` receives the full
    ``yaml_data`` dict rather than one dependency value.
    """

    def __init__(self, deployment_index: int = 0) -> None:
        self.deployment_index = deployment_index
        self._resolvers: dict | None = None

    def set_resolvers(self, resolvers: dict) -> None:
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

        base_domain = _effective_base_domain(value, dep, self.deployment_index, self._resolvers)
        if not base_domain or base_domain == CUSTOM_DOMAIN_SENTINEL:
            return False

        cluster = opi_config.settings.CLUSTER_MANAGER

        ingress_postfix = get_ingress_postfix(cluster)
        cluster_domain = ingress_postfix.lstrip(".")
        if base_domain == cluster_domain:
            return False

        domain_config = get_project_allowed_domain_config(value, base_domain)
        return not (domain_config and domain_config.get("status") == "approved")
