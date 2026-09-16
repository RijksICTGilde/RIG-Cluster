"""De provisioning-backends: het enige dat per cluster verschilt."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from opi.core.cluster_config import get_image_registries_config
from opi.services.catalog.base import ProjectManifestContext, ProjectManifestSpec
from opi.services.catalog.image_registries.naming import (
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

logger = logging.getLogger(__name__)

#: Elk bestand van deze dienst op het projectniveau begint hiermee, voor de prune.
FILENAME_PREFIX = f"{ServiceType.IMAGE_REGISTRIES.value}-"

#: Terugval als de clusterconfig geen ``organization_api_version`` noemt: de gemeten
#: ODCN-waarde, zie features/image-registries.md.
DEFAULT_ORGANIZATION_API_VERSION = "quay.k8s.rijksapps.nl/v1alpha1"


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
        username = registry.get("username")
        password = _plain_password(registry, ctx)
        if not upstream or not username or not password:
            logger.info(
                f"Registry '{registry.get('name')}' van project '{ctx.project_name}' heeft geen inloggegevens; "
                f"geen pull-secret geschreven"
            )
            return []

        name = direct_secret_name(ctx.project_name, str(registry.get("name", "")))
        # De volledige upstream inclusief pad, want kubelet kiest de meest specifieke match.
        secret = RegistrySecret(registry_url=str(upstream), username=str(username), password=password)
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

        specs: list[ProjectManifestSpec] = []
        credentials_secret: str | None = None
        username = registry.get("username")
        password = _plain_password(registry, ctx)
        if username and password:
            credentials_secret = f"{organization}-upstream-credentials"
            specs.append(
                ProjectManifestSpec(
                    filename=f"{FILENAME_PREFIX}{credentials_secret}",
                    template_path="generic-secret.yaml.to-sops.jinja",
                    values={
                        "name": credentials_secret,
                        "namespace": ctx.namespace,
                        "secret_type": "registry",
                        "secret_pairs": {"username": str(username), "password": password},
                    },
                    encrypt=True,
                )
            )
        else:
            logger.info(
                f"Registry '{registry.get('name')}' van project '{ctx.project_name}' heeft geen inloggegevens; "
                f"de proxy-organisatie wordt zonder credentials aangemaakt"
            )

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
