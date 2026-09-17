"""De twee bronnen van de regellijst: de clustertabel en de registries van het project."""

from __future__ import annotations

from typing import Any

from opi.core.cluster_config import get_image_registries_config
from opi.services.catalog.image_registries.naming import direct_secret_name, pull_secret_name, registry_destination
from opi.services.catalog.image_registries.rules import RegistryRule, ResolvedImage, original_image, resolve_image
from opi.services.services import service_entry_config, service_entry_name
from opi.services.services_enums import ServiceType

#: Dockerconfigjson-secret in de namespace, image ongewijzigd.
BACKEND_DIRECT_SECRET = "direct-secret"
#: ``Organization`` met proxyCache in RCR, image herschreven naar die organisatie.
BACKEND_QUAY_PROXY = "quay-proxy-organization"


def project_registries(project_data: dict[str, Any]) -> list[dict[str, Any]]:
    """De registries uit de projectconfig van deze dienst, in bestandsvolgorde."""
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
    """De registry-entry met deze naam, of None."""
    for registry in project_registries(project_data):
        if registry.get("name") == name:
            return registry
    return None


def component_registry_name(component: dict[str, Any] | None) -> str | None:
    """De registry die dit component aanwijst, uit zijn eigen dienstvermelding.

    Werkt op allebei de vormen die het schema kent: ``services`` als lijst op een gewoon
    component, als dict op een deployment-component. Geen vermelding geeft None: geen keuze, dus automatisch.
    """
    if not isinstance(component, dict):
        return None
    services = component.get("services")
    config: Any = None
    if isinstance(services, dict):
        # De dienstnaam is hier de sleutel, dus de waarde is de record zelf.
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
    """Zet de registrykeuze op een DEPLOYMENT-component, als dienstvermelding.

    De tegenhanger van ``component_registry_name`` op de dict-vorm. De losse sleutel
    ``registry:`` bestaat sinds v2.9 niet meer en wordt door het schema geweigerd.
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
    """De regel die bij één private registry hoort, of None als er niets te regelen valt.

    Bij ``direct-secret`` en bij een entry met een bestaand ``secretName`` is de
    bestemming de upstream zelf, dus blijft de image ongewijzigd.
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
    """De gedeelde proxy-caches die het platform op dit cluster aanbiedt."""
    rules = get_image_registries_config(cluster).get("rules") or []
    return [
        RegistryRule(match=rule["match"], to=rule["to"], secret=rule["secret"])
        for rule in rules
        if isinstance(rule, dict) and rule.get("match") and rule.get("to") and rule.get("secret")
    ]


def build_rules(project_data: dict[str, Any], cluster: str, preferred: str | None = None) -> list[RegistryRule]:
    """De hele regellijst voor dit project op dit cluster, in voorrangsvolgorde.

    Projectregels vóór de gedeelde proxy, en ``preferred`` (de registry die een component
    zelf aanwijst) vóór de andere projectregels.
    """
    project_name = project_data.get("name", "")
    registries = project_registries(project_data)
    ordered = sorted(registries, key=lambda r: r.get("name") != preferred) if preferred else registries

    rules = [rule for registry in ordered if (rule := registry_rule(registry, project_name, cluster)) is not None]
    rules.extend(cluster_rules(cluster))
    return rules


def display_image(image: str, cluster: str, project_data: dict[str, Any] | None = None) -> str:
    """De image zoals de AFNEMER hem kent, terug uit zijn platformvorm.

    Zonder ``project_data`` alleen de clustertabel, ermee ook de eigen
    proxy-organisaties van dit project.
    """
    rules = build_rules(project_data, cluster) if project_data else cluster_rules(cluster)
    return original_image(image, rules)


def resolve_deployment_component_image(
    project_data: dict[str, Any],
    deployment_component: dict[str, Any],
    component_def: dict[str, Any] | None,
    cluster: str,
) -> ResolvedImage:
    """De image van één deployment-component, opgelost tegen de regels van dit project.

    De keuze van het component geldt, tenzij de deployment hem overschrijft.
    """
    preferred = component_registry_name(deployment_component) or component_registry_name(component_def)
    return resolve_project_image(deployment_component.get("image", ""), project_data, cluster, preferred)


def resolve_project_image(
    image: str, project_data: dict[str, Any], cluster: str, preferred: str | None = None
) -> ResolvedImage:
    """Los één image op tegen de regels van dit project op dit cluster."""
    return resolve_image(image, build_rules(project_data, cluster, preferred))
