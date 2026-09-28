"""Wie welke proxy-organisatie mag noemen.

Een organisatienaam is tenantbreed en ODCN repliceert het pull-secret naar elke
namespace, dus deze regels kijken naar de andere projecten op het cluster.
"""

from __future__ import annotations

from typing import Any

from opi.core.cluster_config import get_image_registries_config
from opi.services.catalog.image_registries.naming import PULL_SECRET_POSTFIX, organization_name
from opi.services.catalog.image_registries.resolution import project_registries
from opi.utils.naming import normalize_registry_repo, split_image_reference


def validate_proxy_organization_ownership(project_data: dict[str, Any]) -> list[str]:
    """Weiger een image die naar de proxy-organisatie van een ANDER project wijst.

    De zusterregel van ``validate_platform_registry_image_ownership``: wie de naam van
    zo'n organisatie kent haalt er met ANDERMANS credentials bovenstrooms uit op. Een
    gedeelde proxy (``ghcr-rig``) draagt geen projectnaam en blijft bruikbaar. De
    projectnamen komen uit de projectenlijst, want een friendlyName mag zelf koppeltekens
    bevatten.
    """
    # Lazy: importcyclus via project_validation en de dienstenlijst.
    from opi.services.project_store import get_project_store

    project_name = project_data.get("name", "")
    if not project_name:
        return []

    other_projects = [p.name for p in get_project_store().get_all() if p.name != project_name]
    if not other_projects:
        return []

    errors: list[str] = []
    for deployment in project_data.get("deployments", []) or []:
        if not isinstance(deployment, dict):
            continue
        cluster_config = get_image_registries_config(str(deployment.get("cluster", "")))
        registry_host = cluster_config.get("registry_host")
        customer_name = cluster_config.get("customer_name")
        if not registry_host or not customer_name:
            continue
        for component in deployment.get("components", []) or []:
            if not isinstance(component, dict):
                continue
            image = component.get("image")
            if not isinstance(image, str) or not image:
                continue
            organization = _proxy_organization_of(image, registry_host)
            if organization is None:
                continue
            owner = next((p for p in other_projects if _belongs_to_project(organization, customer_name, p)), None)
            if owner is not None:
                errors.append(
                    f"deployment '{deployment.get('name')}' component '{component.get('reference')}' verwijst met "
                    f"'{image}' naar de registry-organisatie van project '{owner}'. Die is met de inloggegevens "
                    f"van dat project gevuld; gebruik je eigen registry"
                )
    return errors


def _belongs_to_project(organization: str, customer_name: str, project: str) -> bool:
    """Of een proxy-organisatie van ``project`` is.

    Elke organisatie heeft de vorm ``<friendly>-<customer>-<project>-<hash>``, dus aan
    beide kanten op een segmentgrens matchen: anders eist ``demo`` die van
    ``demonstratie`` op.
    """
    return f"-{customer_name}-{project}-" in organization


def _project_clusters(project_data: dict[str, Any]) -> list[str]:
    """Elk cluster waar dit project iets op draait, zonder dubbelen.

    Een deployment mag een cluster noemen dat niet in ``clusters:`` staat, dus beide
    bronnen tellen mee.
    """
    clusters: list[str] = []
    for cluster in project_data.get("clusters", []) or []:
        if isinstance(cluster, str) and cluster and cluster not in clusters:
            clusters.append(cluster)
    for deployment in project_data.get("deployments", []) or []:
        cluster = deployment.get("cluster") if isinstance(deployment, dict) else None
        if isinstance(cluster, str) and cluster and cluster not in clusters:
            clusters.append(cluster)
    return clusters


def _foreign_owner_of_organization(organization: str, cluster: str, project_name: str) -> str | None:
    """Het ANDERE project waarvan deze proxy-ORGANISATIE is, of None.

    Een gedeelde proxy draagt geen projectnaam en levert dus None op.
    """
    # Lazy: importcyclus via project_validation en de dienstenlijst.
    from opi.services.project_store import get_project_store

    customer_name = get_image_registries_config(cluster).get("customer_name")
    if not customer_name:
        return None
    others = (p.name for p in get_project_store().get_all() if p.name != project_name)
    return next((p for p in others if _belongs_to_project(organization, customer_name, p)), None)


def foreign_proxy_organization_owner(reference: str, cluster: str, project_name: str) -> str | None:
    """Het ANDERE project waarvan ``reference`` de proxy-organisatie noemt, of None.

    ``reference`` is een image of een kale registry-verwijzing; alles buiten de
    proxy-registry van dit cluster levert None op. Publiek, want de ad-hoc jobpod gebruikt
    dezelfde toets zonder projectbestand.
    """
    registry_host = get_image_registries_config(cluster).get("registry_host")
    if not registry_host:
        return None
    organization = _proxy_organization_of(reference, registry_host)
    if organization is None:
        return None
    return _foreign_owner_of_organization(organization, cluster, project_name)


def validate_registry_entry_ownership(project_data: dict[str, Any]) -> list[str]:
    """Weiger een registry-entry die naar de proxy-organisatie van een ANDER project wijst.

    De andere helft van ``validate_proxy_organization_ownership``, die de images toetst.
    Een entry alleen bouwt al een regel die op elke image onder die upstream slaat, ook op
    de ad-hoc jobpod. Twee velden, want de ``upstream`` bepaalt welke images de regel raakt
    en het ``secretName`` welk credential eraan hangt.
    """
    project_name = project_data.get("name", "")
    registries = project_registries(project_data) if project_name else []
    if not registries:
        return []

    # Meerdere clusters met dezelfde klantnaam leveren dezelfde melding op.
    errors: list[str] = []
    for cluster in _project_clusters(project_data):
        for registry in registries:
            name = registry.get("name")
            for field, value in (("upstream", registry.get("upstream")), ("secretName", registry.get("secretName"))):
                if not isinstance(value, str) or not value:
                    continue
                if field == "secretName":
                    # Een secretName noemt de organisatie als naam, niet als pad.
                    owner = _foreign_owner_of_organization(
                        value.removesuffix(f"-{PULL_SECRET_POSTFIX}"), cluster, project_name
                    )
                else:
                    owner = foreign_proxy_organization_owner(value, cluster, project_name)
                if owner is not None:
                    errors.append(
                        f"registry '{name}' wijst met {field} '{value}' naar de registry-organisatie van project "
                        f"'{owner}'. Die is met de inloggegevens van dat project gevuld; gebruik je eigen registry"
                    )
    return list(dict.fromkeys(errors))


def validate_proxy_organization_claims(project_data: dict[str, Any]) -> list[str]:
    """Weiger een organisatienaam die al door een ANDER project geclaimd is.

    De naam is tenantbreed: twee CR's met dezelfde ``metadata.name`` sturen EEN organisatie
    in RCR aan. ``organization_suffix`` maakt dat bij normaal gebruik onmogelijk, maar de
    naam wordt op 63 tekens afgekapt, dus dit meet de UITKOMST. Binnen het project zelf
    geldt hetzelfde: twee entries op één naam is één bestand op het projectniveau.
    """
    # Lazy: importcyclus via project_validation en de dienstenlijst.
    from opi.services.project_store import get_project_store

    project_name = project_data.get("name", "")
    registries = project_registries(project_data) if project_name else []
    if not registries:
        return []

    errors: list[str] = []
    for cluster in _project_clusters(project_data):
        customer_name = get_image_registries_config(cluster).get("customer_name")
        if not customer_name:
            continue

        claimed: dict[str, str] = {}
        for other in get_project_store().get_all():
            if other.name == project_name or not other.data:
                continue
            for registry in project_registries(other.data):
                upstream = registry.get("upstream")
                if isinstance(upstream, str) and upstream:
                    claimed[organization_name(upstream, customer_name, other.name)] = other.name

        mine: dict[str, str] = {}
        for registry in registries:
            upstream = registry.get("upstream")
            name = str(registry.get("name", ""))
            if not isinstance(upstream, str) or not upstream:
                continue
            organization = organization_name(upstream, customer_name, project_name)
            owner = claimed.get(organization)
            if owner is not None:
                errors.append(
                    f"registry '{name}' komt op registry-organisatie '{organization}' uit, en die is al van "
                    f"project '{owner}'. Kies een andere naam voor je project of een andere upstream"
                )
            elif organization in mine and mine[organization] != name:
                errors.append(
                    f"registry '{name}' komt op dezelfde registry-organisatie '{organization}' uit als registry "
                    f"'{mine[organization]}'. Twee registries kunnen niet één organisatie delen"
                )
            else:
                mine[organization] = name
    return list(dict.fromkeys(errors))


def _proxy_organization_of(image: str, registry_host: str) -> str | None:
    """``rcr.rijksapps.nl/codeoverheid-rig-demo/app:1`` -> ``codeoverheid-rig-demo``."""
    repo, _tag, _digest = split_image_reference(image)
    normalized = normalize_registry_repo(repo)
    host, separator, path = normalized.partition("/")
    if not separator or host != registry_host.lower():
        return None
    organization = path.split("/", 1)[0]
    return organization or None
