"""The two sources of the rule list: the cluster table and the project's registries."""

from __future__ import annotations

from typing import Any

from opi.core.cluster_config import get_image_registries_config
from opi.services.catalog.image_registries.naming import direct_secret_name, pull_secret_name, registry_destination
from opi.services.catalog.image_registries.rules import RegistryRule, ResolvedImage, original_image, resolve_image
from opi.services.services import service_entry_config, service_entry_name
from opi.services.services_enums import ServiceType

#: Dockerconfigjson secret in the namespace, image unchanged.
BACKEND_DIRECT_SECRET = "direct-secret"  # noqa: S105  # the name of the backend choice, not a secret
#: ``Organization`` with proxyCache in RCR, image rewritten to that organization.
BACKEND_QUAY_PROXY = "quay-proxy-organization"


def project_registries(project_data: dict[str, Any]) -> list[dict[str, Any]]:
    """The registries from this service's project config, in file order."""
    for entry in project_data.get("services", []) or []:
        if service_entry_name(entry) != ServiceType.IMAGE_REGISTRIES.value:
            continue
        config = service_entry_config(entry)
        if not isinstance(config, dict):
            return []
        registries = config.get("registries") or []
        return [r for r in registries if isinstance(r, dict) and r.get("name")]
    return []


def find_registry(project_data: dict[str, Any], name: str) -> dict[str, Any] | None:
    """The registry entry with this name, or None."""
    for registry in project_registries(project_data):
        if registry.get("name") == name:
            return registry
    return None


def component_registry_name(component: dict[str, Any] | None) -> str | None:
    """The registry this component points at, from its own service entry.

    Works on both shapes the schema knows: ``services`` as a list on a plain component, as a
    dict on a deployment component. No entry means automatic.
    """
    if not isinstance(component, dict):
        return None
    services = component.get("services")
    config: Any = None
    if isinstance(services, dict):
        # The service name is the key here, so the value is the record itself.
        record = services.get(ServiceType.IMAGE_REGISTRIES.value)
        config = record.get("config") if isinstance(record, dict) else None
    elif isinstance(services, list):
        for entry in services:
            if service_entry_name(entry) == ServiceType.IMAGE_REGISTRIES.value:
                config = service_entry_config(entry)
                break
    if isinstance(config, dict):
        name = config.get("registry")
        return name if isinstance(name, str) and name else None
    return None


def set_deployment_component_registry(component: dict[str, Any], registry_name: str) -> None:
    """Put the registry choice on a DEPLOYMENT component, as a service entry.

    The counterpart of ``component_registry_name`` on the dict shape. The separate
    ``registry:`` key is gone since v2.9 and the schema refuses it.
    """
    services = component.get("services")
    if not isinstance(services, dict):
        services = {}
        component["services"] = services
    record = services.get(ServiceType.IMAGE_REGISTRIES.value)
    if not isinstance(record, dict):
        record = {}
        services[ServiceType.IMAGE_REGISTRIES.value] = record
    config = record.get("config")
    if not isinstance(config, dict):
        config = {}
        record["config"] = config
    config["registry"] = registry_name


def registry_rule(registry: dict[str, Any], project_name: str, cluster: str) -> RegistryRule | None:
    """The rule belonging to one private registry, or None when there is nothing to rule on.

    With ``direct-secret`` and with an entry that has an existing ``secretName`` the
    destination is the upstream itself, so the image stays unchanged.
    """
    upstream = registry.get("upstream")
    if not isinstance(upstream, str) or not upstream:
        return None

    cluster_config = get_image_registries_config(cluster)
    existing_secret = registry.get("secretName")
    if isinstance(existing_secret, str) and existing_secret:
        return RegistryRule(match=upstream, to=upstream, secret=existing_secret)

    if cluster_config.get("backend") == BACKEND_QUAY_PROXY:
        registry_host = cluster_config.get("registry_host", "")
        customer_name = cluster_config.get("customer_name", "")
        return RegistryRule(
            match=upstream,
            to=registry_destination(upstream, registry_host, customer_name, project_name),
            secret=pull_secret_name(upstream, customer_name, project_name),
        )

    name = registry.get("name", "")
    return RegistryRule(match=upstream, to=upstream, secret=direct_secret_name(project_name, name))


def cluster_rules(cluster: str) -> list[RegistryRule]:
    """The shared proxy caches the platform offers on this cluster."""
    rules = get_image_registries_config(cluster).get("rules") or []
    return [
        RegistryRule(match=rule["match"], to=rule["to"], secret=rule["secret"])
        for rule in rules
        if isinstance(rule, dict) and rule.get("match") and rule.get("to") and rule.get("secret")
    ]


def build_rules(project_data: dict[str, Any], cluster: str, preferred: str | None = None) -> list[RegistryRule]:
    """The whole rule list for this project on this cluster, in precedence order.

    Project rules before the shared proxy, and ``preferred`` (the registry a component points
    at itself) before the other project rules.
    """
    project_name = project_data.get("name", "")
    registries = project_registries(project_data)
    ordered = sorted(registries, key=lambda r: r.get("name") != preferred) if preferred else registries

    rules = [rule for registry in ordered if (rule := registry_rule(registry, project_name, cluster)) is not None]
    rules.extend(cluster_rules(cluster))
    return rules


def display_image(image: str, cluster: str, project_data: dict[str, Any] | None = None) -> str:
    """The image as the CONSUMER knows it, back out of its platform shape.

    Without ``project_data`` only the cluster table, with it also this project's own proxy
    organizations.
    """
    rules = build_rules(project_data, cluster) if project_data else cluster_rules(cluster)
    return original_image(image, rules)


def resolve_deployment_component_image(
    project_data: dict[str, Any],
    deployment_component: dict[str, Any],
    component_def: dict[str, Any] | None,
    cluster: str,
) -> ResolvedImage:
    """The image of one deployment component, resolved against this project's rules.

    The component's choice applies unless the deployment overrides it.
    """
    preferred = component_registry_name(deployment_component) or component_registry_name(component_def)
    return resolve_project_image(deployment_component.get("image", ""), project_data, cluster, preferred)


def resolve_project_image(
    image: str, project_data: dict[str, Any], cluster: str, preferred: str | None = None
) -> ResolvedImage:
    """Resolve one image against this project's rules on this cluster."""
    return resolve_image(image, build_rules(project_data, cluster, preferred))
