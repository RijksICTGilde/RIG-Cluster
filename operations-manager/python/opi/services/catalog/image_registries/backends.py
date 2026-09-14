"""De provisioning-backends: het enige dat per cluster verschilt."""

from __future__ import annotations

from typing import Any, Protocol

from opi.core.cluster_config import get_image_registries_config
from opi.services.catalog.base import ProjectManifestContext, ProjectManifestSpec
from opi.services.catalog.image_registries.naming import (
    PULL_USERNAME_PLACEHOLDER,
    direct_secret_name,
    friendly_name,
    organization_name,
    organization_suffix,
    pull_secret_name,
)
from opi.services.catalog.image_registries.resolution import BACKEND_QUAY_PROXY
from opi.services.services_enums import ServiceType
from opi.utils.age import (
    carries_encrypted_value,
    decrypt_password_smart_sync,
    get_decoded_project_private_key_sync,
)
from opi.utils.secrets import RegistrySecret

#: Elk bestand van deze dienst op het projectniveau begint hiermee, voor de prune.
FILENAME_PREFIX = f"{ServiceType.IMAGE_REGISTRIES.value}-"

#: Terugval als de clusterconfig geen ``organization_api_version`` noemt: de gemeten
#: ODCN-waarde, zie features/image-registries.md.
DEFAULT_ORGANIZATION_API_VERSION = "quay.k8s.rijksapps.nl/v1alpha1"


class MissingRegistryCredentialsError(ValueError):
    """Een registry zonder secretName en zonder token."""

    def __init__(self, registry: str, project: str) -> None:
        super().__init__(
            f"Registry '{registry}' van project '{project}' heeft geen secretName en geen token; "
            f"er valt geen pull-secret te schrijven"
        )


class RegistryBackend(Protocol):
    """Wat er moet worden aangemaakt voor een registry die een afnemer opgeeft.

    Geen ``remove``: verwijderen is declaratief. Levert de backend geen spec meer, dan
    ruimt de prune het bestand op en verwijdert ArgoCD de resource.
    """

    def manifests(self, ctx: ProjectManifestContext, registry: dict[str, Any]) -> list[ProjectManifestSpec]:
        """De projectbrede manifesten voor één registry, replay-safe."""
        ...


class DirectSecretBackend:
    """Een dockerconfigjson-secret in de namespace; de image blijft ongewijzigd."""

    def manifests(self, ctx: ProjectManifestContext, registry: dict[str, Any]) -> list[ProjectManifestSpec]:
        if registry.get("secretName"):
            # Het secret staat er al, gezet door het platform.
            return []
        upstream = registry.get("upstream")
        password = _plain_password(registry, ctx)
        if not upstream or not password:
            # Onbereikbaar: ``RegistryEntry`` eist een secretName of een token. Komt hij hier
            # toch, dan blazen we op in plaats van stil geen pull-secret te schrijven --
            # anders merkt de afnemer het pas als de pod niet kan pullen.
            raise MissingRegistryCredentialsError(str(registry.get("name")), ctx.project_name)
        username = _pull_username(registry)

        name = direct_secret_name(ctx.project_name, str(registry.get("name", "")))
        # De volledige upstream inclusief pad, want kubelet kiest de meest specifieke match.
        secret = RegistrySecret(registry_url=str(upstream), username=username, password=password)
        return [
            ProjectManifestSpec(
                filename=f"{FILENAME_PREFIX}{name}",
                template_path="generic-secret.yaml.to-sops.jinja",
                values={
                    "name": name,
                    "namespace": ctx.namespace,
                    "secret_type": "registry",
                    "secret_k8s_type": "kubernetes.io/dockerconfigjson",
                    "secret_pairs": secret.to_k8s_secret_data(),
                },
                encrypt=True,
            )
        ]


class QuayProxyOrganizationBackend:
    """Een ``Organization`` met proxyCache in RCR, plus het credentials-secret ernaast."""

    def manifests(self, ctx: ProjectManifestContext, registry: dict[str, Any]) -> list[ProjectManifestSpec]:
        if registry.get("secretName"):
            return []
        upstream = registry.get("upstream")
        if not upstream:
            return []

        cluster_config = get_image_registries_config(ctx.cluster)
        customer_name = cluster_config.get("customer_name", "")
        rotation_days = cluster_config.get("rotation_days", 90)
        organization = organization_name(str(upstream), customer_name, ctx.project_name)

        password = _plain_password(registry, ctx)
        if not password:
            # Onbereikbaar, om dezelfde reden als in ``DirectSecretBackend``.
            raise MissingRegistryCredentialsError(str(registry.get("name")), ctx.project_name)
        username = _pull_username(registry)

        credentials_secret = f"{organization}-upstream-credentials"
        specs: list[ProjectManifestSpec] = [
            ProjectManifestSpec(
                filename=f"{FILENAME_PREFIX}{credentials_secret}",
                template_path="generic-secret.yaml.to-sops.jinja",
                values={
                    "name": credentials_secret,
                    "namespace": ctx.namespace,
                    "secret_type": "registry",
                    "secret_pairs": {"username": username, "password": password},
                },
                encrypt=True,
            )
        ]
        specs.append(
            ProjectManifestSpec(
                filename=f"{FILENAME_PREFIX}{organization}",
                template_path="quay-proxy-organization.yaml.jinja",
                values={
                    "api_version": cluster_config.get("organization_api_version", DEFAULT_ORGANIZATION_API_VERSION),
                    "name": organization,
                    "namespace": ctx.namespace,
                    "friendly_name": friendly_name(str(upstream)),
                    "suffix": organization_suffix(str(upstream), ctx.project_name),
                    "upstream": str(upstream),
                    "credentials_secret": credentials_secret,
                    "pull_secret_name": pull_secret_name(str(upstream), customer_name, ctx.project_name),
                    "rotation_days": rotation_days,
                },
            )
        )
        return specs


def backend_for_cluster(cluster: str) -> RegistryBackend:
    """De backend die op dit cluster geldt."""
    if get_image_registries_config(cluster).get("backend") == BACKEND_QUAY_PROXY:
        return QuayProxyOrganizationBackend()
    return DirectSecretBackend()


def _pull_username(registry: dict[str, Any]) -> str:
    """De gebruikersnaam voor de dockerconfigjson: wat de afnemer opgaf, anders de plaatshouder.

    Hier en niet bij het opslaan, want dit is de enige plek waar hij nodig is. Zie
    ``PULL_USERNAME_PLACEHOLDER`` voor waarom er iets moet staan.
    """
    username = registry.get("username")
    if isinstance(username, str) and username:
        return username
    return PULL_USERNAME_PLACEHOLDER


def _plain_password(registry: dict[str, Any], ctx: ProjectManifestContext) -> str | None:
    """Het token in leesbare vorm, of None als er geen token is opgegeven.

    Met de fail-closed sleutelzoeker: dit is de SCHRIJFkant, dus een ontbrekende sleutel
    blaast op in plaats van stil een onbruikbaar credential naar git te schrijven.
    """
    stored = registry.get("password")
    if not isinstance(stored, str) or not stored:
        return None
    private_key = get_decoded_project_private_key_sync(ctx.project_data) if carries_encrypted_value(stored) else None
    return decrypt_password_smart_sync(stored, private_key)
