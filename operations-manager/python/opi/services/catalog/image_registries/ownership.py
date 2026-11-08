"""Who may name which proxy organization.

An organization name is tenant wide and ODCN replicates the pull secret into every namespace,
so these rules look at the other projects on the cluster.
"""

from __future__ import annotations

from typing import Any

from opi.core.cluster_config import get_image_registries_config
from opi.services.catalog.image_registries.naming import PULL_SECRET_POSTFIX, organization_name
from opi.services.catalog.image_registries.resolution import project_registries
from opi.utils.naming import normalize_registry_repo, split_image_reference


def validate_proxy_organization_ownership(project_data: dict[str, Any]) -> list[str]:
    """Refuse an image pointing at ANOTHER project's proxy organization.

    The sister rule of ``validate_platform_registry_image_ownership``: whoever knows the name
    of such an organization pulls from upstream with SOMEONE ELSE's credentials. A shared proxy
    (``ghcr-rig``) carries no project name and stays usable. The project names come from the
    project list, because a friendlyName may itself contain hyphens.
    """
    # Lazy: import cycle through project_validation and the services list.
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
    """Whether a proxy organization belongs to ``project``.

    Every organization has the shape ``<friendly>-<customer>-<project>-<hash>``, so match on a
    segment boundary at both ends: otherwise ``demo`` claims the one of ``demonstratie``.
    """
    return f"-{customer_name}-{project}-" in organization


def _project_clusters(project_data: dict[str, Any]) -> list[str]:
    """Every cluster this project runs something on, without duplicates.

    A deployment may name a cluster that is not in ``clusters:``, so both sources count.
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
    """The OTHER project this proxy ORGANIZATION belongs to, or None.

    A shared proxy carries no project name and therefore yields None.
    """
    # Lazy: import cycle through project_validation and the services list.
    from opi.services.project_store import get_project_store

    customer_name = get_image_registries_config(cluster).get("customer_name")
    if not customer_name:
        return None
    others = (p.name for p in get_project_store().get_all() if p.name != project_name)
    return next((p for p in others if _belongs_to_project(organization, customer_name, p)), None)


def foreign_proxy_organization_owner(reference: str, cluster: str, project_name: str) -> str | None:
    """The OTHER project whose proxy organization ``reference`` names, or None.

    ``reference`` is an image or a bare registry reference; anything outside this cluster's
    proxy registry yields None. Public, because the ad-hoc job pod uses the same check without
    a project file.
    """
    registry_host = get_image_registries_config(cluster).get("registry_host")
    if not registry_host:
        return None
    organization = _proxy_organization_of(reference, registry_host)
    if organization is None:
        return None
    return _foreign_owner_of_organization(organization, cluster, project_name)


def validate_registry_entry_ownership(project_data: dict[str, Any]) -> list[str]:
    """Refuse a registry entry pointing at ANOTHER project's proxy organization.

    The other half of ``validate_proxy_organization_ownership``, which checks the images. An
    entry on its own already builds a rule that applies to every image under that upstream,
    including on the ad-hoc job pod. Two fields, because the ``upstream`` decides which images
    the rule touches and the ``secretName`` which credential hangs off it.
    """
    project_name = project_data.get("name", "")
    registries = project_registries(project_data) if project_name else []
    if not registries:
        return []

    # Several clusters with the same customer name yield the same message.
    errors: list[str] = []
    for cluster in _project_clusters(project_data):
        for registry in registries:
            name = registry.get("name")
            for field, value in (("upstream", registry.get("upstream")), ("secretName", registry.get("secretName"))):
                if not isinstance(value, str) or not value:
                    continue
                if field == "secretName":
                    # A secretName names the organization as a name, not as a path.
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
    """Refuse an organization name another project already claimed.

    The name is tenant wide: two CRs with the same ``metadata.name`` drive ONE organization in
    RCR. ``organization_suffix`` makes that impossible under normal use, but the name is cut to
    63 characters, so this measures the OUTCOME. Within the project itself the same holds: two
    entries on one name is one file at the project layer.
    """
    # Lazy: import cycle through project_validation and the services list.
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
