"""De twee bronnen van de regellijst, en het oplossen van een image ertegen.

De dienst kent twee bronnen van waarheid en één bewerking:

* **Clusterconfiguratie**, niet door een gebruiker te bewerken: welke provisioning-backend
  hier geldt, de platformfeiten en de tabel van upstream naar gedeelde proxy inclusief het
  secret dat erbij hoort (``get_image_registries_config``).
* **Gebruikersconfiguratie**: de private registries die een project zelf opgeeft.

``resolve_image()`` is een FUNCTIE, geen haak. Hij heeft geen staat en geen activatie, en
de generator roept hem aan zoals hij vandaag ook een naamfunctie aanroept. Er is dus geen
"dienst die draait zonder gekozen te zijn"; er is een functie die altijd hetzelfde antwoord
geeft op dezelfde vraag.
"""

from __future__ import annotations

from typing import Any

from opi.core.cluster_config import get_image_registries_config
from opi.services.catalog.image_registries.naming import direct_secret_name, pull_secret_name, registry_destination
from opi.services.catalog.image_registries.rules import RegistryRule, ResolvedImage, resolve_image
from opi.services.services import service_entry_config, service_entry_name
from opi.services.services_enums import ServiceType

#: De backend die een registry als dockerconfigjson-secret in de namespace neerzet en de
#: image ongewijzigd laat.
BACKEND_DIRECT_SECRET = "direct-secret"
#: De backend die een ``Organization`` met proxyCache in RCR aanmaakt en de image
#: herschrijft naar de organisatie van dit project.
BACKEND_QUAY_PROXY = "quay-proxy-organization"


def project_registries(project_data: dict[str, Any]) -> list[dict[str, Any]]:
    """De registries uit de projectconfig van deze dienst, in bestandsvolgorde.

    Leest de dienstvermelding via ``service_entry_name``, nooit via de sleutels van de
    dict: een record dat config draagt heeft ``name``/``config`` als sleutels, dus
    sleutel-lezende code laat precies de entries vallen waar het om gaat.
    """
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


def registry_rule(registry: dict[str, Any], project_name: str, cluster: str) -> RegistryRule | None:
    """De regel die bij één private registry hoort, of None als er niets te regelen valt.

    Wat de regel doet hangt af van de backend van het cluster, en dat is het enige dat per
    platform verschilt:

    * ``direct-secret``: bestemming IS de upstream, dus de image blijft ongewijzigd en
      krijgt alleen het secret. Dat is letterlijk het pad dat een registry met
      gebruikersnaam en token vandaag al volgt.
    * ``quay-proxy-organization``: bestemming is de proxy-organisatie van dit project in
      RCR, dus de image wordt herschreven en het namespace-segment valt weg.

    Een entry met ``secretName`` verwijst naar een secret dat het platform zelf neerzet en
    wordt op geen enkel cluster herschreven: de bestemming is de upstream.
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

    Projectregels vooraan, want de eigen registry van een project moet winnen van de
    gedeelde proxy voor dezelfde upstream. ``preferred`` is de registry die een component
    zelf aanwijst en gaat vóór de andere projectregels -- dat is het geval waarin twee
    registries dezelfde upstream hebben met verschillende tokens, en het is de reden dat
    de koppeling een keuze van de afnemer is en geen afleiding uit de image-URL.
    """
    project_name = project_data.get("name", "")
    registries = project_registries(project_data)
    ordered = sorted(registries, key=lambda r: r.get("name") != preferred) if preferred else registries

    rules = [rule for registry in ordered if (rule := registry_rule(registry, project_name, cluster)) is not None]
    rules.extend(cluster_rules(cluster))
    return rules


def resolve_project_image(
    image: str, project_data: dict[str, Any], cluster: str, preferred: str | None = None
) -> ResolvedImage:
    """Los één image op tegen de regels van dit project op dit cluster."""
    return resolve_image(image, build_rules(project_data, cluster, preferred))
