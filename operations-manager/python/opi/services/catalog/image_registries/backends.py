"""The provisioning backends: the only thing that differs per cluster."""

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
from opi.services.catalog.image_registries.quota import effective_quota_gib
from opi.services.catalog.image_registries.resolution import BACKEND_QUAY_PROXY
from opi.services.secrets import RegistrySecret
from opi.services.services_enums import ServiceType
from opi.utils.age import (
    carries_encrypted_value,
    decrypt_password_smart_sync,
    get_decoded_project_private_key_sync,
)

#: Every project-level file of this service starts with this, for the prune's sake.
FILENAME_PREFIX = f"{ServiceType.IMAGE_REGISTRIES.value}-"

#: Fallback when the cluster config names no ``organization_api_version``: the measured ODCN
#: value, see features/image-registries.md.
DEFAULT_ORGANIZATION_API_VERSION = "quay.k8s.rijksapps.nl/v1alpha1"
#: Fallback when the cluster config names no ``quota_limit_gib``. Whole gibibytes, because
#: ``limitGiB`` is an integer in the CR.
DEFAULT_QUOTA_LIMIT_GIB = 3


def cluster_default_quota_gib(cluster: str) -> int:
    """The quota a registry gets without one of its own, on this cluster."""
    return int(get_image_registries_config(cluster).get("quota_limit_gib", DEFAULT_QUOTA_LIMIT_GIB))


class MissingRegistryCredentialsError(ValueError):
    """A registry with neither a secretName nor a token."""

    def __init__(self, registry: str, project: str) -> None:
        super().__init__(
            f"Registry '{registry}' van project '{project}' heeft geen secretName en geen token; "
            f"er valt geen pull-secret te schrijven"
        )


class RegistryBackend(Protocol):
    """What has to be created for a registry a consumer supplies.

    No ``remove``: deleting is declarative. Once the backend yields no spec, the prune clears
    the file and ArgoCD removes the resource.
    """

    def manifests(self, ctx: ProjectManifestContext, registry: dict[str, Any]) -> list[ProjectManifestSpec]:
        """The project-wide manifests for one registry, replay safe."""
        ...


class DirectSecretBackend:
    """A dockerconfigjson secret in the namespace; the image stays unchanged."""

    def manifests(self, ctx: ProjectManifestContext, registry: dict[str, Any]) -> list[ProjectManifestSpec]:
        if registry.get("secretName"):
            # The secret is already there, put in place by the platform.
            return []
        upstream = registry.get("upstream")
        password = _plain_password(registry, ctx)
        if not upstream or not password:
            # Unreachable: ``RegistryEntry`` demands a secretName or a token. Loud anyway,
            # otherwise the consumer only notices when the pod cannot pull.
            raise MissingRegistryCredentialsError(str(registry.get("name")), ctx.project_name)
        username = _pull_username(registry)

        name = direct_secret_name(ctx.project_name, str(registry.get("name", "")))
        # The full upstream including path, because kubelet picks the most specific match.
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
    """An ``Organization`` with proxyCache in RCR, plus the credentials secret beside it."""

    def manifests(self, ctx: ProjectManifestContext, registry: dict[str, Any]) -> list[ProjectManifestSpec]:
        if registry.get("secretName"):
            return []
        upstream = registry.get("upstream")
        if not upstream:
            return []

        cluster_config = get_image_registries_config(ctx.cluster)
        customer_name = cluster_config.get("customer_name", "")
        rotation_days = cluster_config.get("rotation_days", 90)
        quota_limit_gib = effective_quota_gib(
            ctx.project_data, str(registry.get("name", "")), cluster_default_quota_gib(ctx.cluster)
        )
        organization = organization_name(str(upstream), customer_name, ctx.project_name)

        password = _plain_password(registry, ctx)
        if not password:
            # Unreachable, for the same reason as in ``DirectSecretBackend``.
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
                    "customer_name": customer_name,
                    "tenants": cluster_config.get("tenants", []),
                    "friendly_name": friendly_name(str(upstream)),
                    "suffix": organization_suffix(str(upstream), ctx.project_name),
                    "upstream": str(upstream),
                    "credentials_secret": credentials_secret,
                    "pull_secret_name": pull_secret_name(str(upstream), customer_name, ctx.project_name),
                    "rotation_days": rotation_days,
                    "quota_limit_gib": quota_limit_gib,
                },
            )
        )
        return specs


def backend_for_cluster(cluster: str) -> RegistryBackend:
    """The backend that applies on this cluster."""
    if get_image_registries_config(cluster).get("backend") == BACKEND_QUAY_PROXY:
        return QuayProxyOrganizationBackend()
    return DirectSecretBackend()


def _pull_username(registry: dict[str, Any]) -> str:
    """The username for the dockerconfigjson: what the consumer gave, else the placeholder.

    Here and not at save time, because this is the only place it is needed. See
    ``PULL_USERNAME_PLACEHOLDER`` for why something has to be there.
    """
    username = registry.get("username")
    if isinstance(username, str) and username:
        return username
    return PULL_USERNAME_PLACEHOLDER


def _plain_password(registry: dict[str, Any], ctx: ProjectManifestContext) -> str | None:
    """The token in readable form, or None when no token was supplied.

    With the fail-closed key resolver: this is the WRITE side, so a missing key blows up
    instead of silently writing an unusable credential to git.
    """
    stored = registry.get("password")
    if not isinstance(stored, str) or not stored:
        return None
    private_key = get_decoded_project_private_key_sync(ctx.project_data) if carries_encrypted_value(stored) else None
    return decrypt_password_smart_sync(stored, private_key)
