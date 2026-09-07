"""De provisioning-backends: het enige dat per cluster echt verschilt.

Wat een afnemer invult is op elk platform hetzelfde. Wat er onder gebeurt niet:

* ``direct-secret`` voor kind, sandbox en elk cluster waar de nodes zelf bij de registry
  kunnen: een dockerconfigjson-secret in de namespace, image ongewijzigd. Dat is letterlijk
  het pad dat een registry met gebruikersnaam en token vandaag al volgt.
* ``quay-proxy-organization`` voor ODCN en elk cluster met dezelfde operator: een
  credentials-secret plus een ``Organization`` met proxyCache. De RCR-URL en de secretnaam
  volgen uit de naamfuncties, dus er valt niets terug te geven en niets op te wachten.

Een derde platform is een derde backend plus een tabel in de clusterconfig, en geen
wijziging aan de dienst.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from opi.core.cluster_config import get_image_registries_config
from opi.services.catalog.base import ProjectManifestContext, ProjectManifestSpec
from opi.services.catalog.image_registries.naming import (
    direct_secret_name,
    friendly_name,
    organization_name,
    pull_secret_name,
)
from opi.services.catalog.image_registries.resolution import BACKEND_QUAY_PROXY
from opi.services.services_enums import ServiceType
from opi.utils.secrets import RegistrySecret

logger = logging.getLogger(__name__)

#: Elk bestand dat deze dienst op het projectniveau neerzet begint hiermee, zodat de
#: symmetrische prune ze weer weghaalt zodra de dienst uitgaat.
FILENAME_PREFIX = f"{ServiceType.IMAGE_REGISTRIES.value}-"

#: De groep en versie van de Organization-CRD als de clusterconfig hem niet noemt. Een
#: cluster dat deze backend kiest hoort hem zelf te zetten (``organization_api_version``);
#: dit is de waarde waarmee de proef op productie is gedaan.
DEFAULT_ORGANIZATION_API_VERSION = "quay.redhat.com/v1"


class RegistryBackend(Protocol):
    """Wat er moet worden aangemaakt voor een registry die een afnemer opgeeft.

    Alleen ``ensure``, geen ``remove``. Het VERWIJDEREN is hier declaratief: haalt een
    project een registry weg, dan levert deze backend er geen spec meer voor, ruimt de
    symmetrische prune het bestand uit ``_project/`` op, en verwijdert ArgoCD de resource
    -- waarna de operator de organisatie in RCR opruimt. Een imperatieve ``remove``
    ernaast zou een tweede weg naar dezelfde uitkomst zijn, en die twee kunnen uiteenlopen.
    Verwijderen mag direct (D8): bij een proxy cache verliest de afnemer alleen de kopie,
    bovenstrooms staat alles er nog.
    """

    def manifests(self, ctx: ProjectManifestContext, registry: dict[str, Any]) -> list[ProjectManifestSpec]:
        """De projectbrede manifesten voor één registry. Replay-safe: dezelfde invoer
        levert dezelfde bestanden, dus opnieuw draaien is een normale uitkomst."""
        ...


class DirectSecretBackend:
    """Een dockerconfigjson-secret in de namespace; de image blijft ongewijzigd."""

    def manifests(self, ctx: ProjectManifestContext, registry: dict[str, Any]) -> list[ProjectManifestSpec]:
        if registry.get("secretName"):
            # Het secret staat er al, gezet door het platform. Niets te schrijven; de regel
            # in resolution.py hangt het aan de pods die het nodig hebben.
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
        # De VOLLEDIGE upstream als sleutel in auths, inclusief pad: dat is wat het
        # veld altijd al droeg en wat kubelet als meest specifieke match kiest.
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
                    "suffix": ctx.project_name,
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
    """Het token in leesbare vorm, of None als het er niet is of niet te lezen valt."""
    from opi.forms.editables.converters import resolve_project_private_key
    from opi.utils.age import decrypt_age_content_sync

    stored = registry.get("password")
    if not isinstance(stored, str) or not stored:
        return None
    if "BEGIN AGE ENCRYPTED FILE" not in stored:
        return stored
    private_key = resolve_project_private_key(ctx.project_data)
    if not private_key:
        logger.warning(
            f"Token van registry '{registry.get('name')}' in project '{ctx.project_name}' is niet te ontsleutelen"
        )
        return None
    return decrypt_age_content_sync(stored, private_key)
