"""vlam service: reach the VLAM-API from inside the cluster, without a VPN.

VLAM is the language-model API SSC-ICT offers over the RON. Since RC-142 the vlam project
runs a second, INTERNAL proxy next to the VPN gateway: it accepts plain HTTP and sets up
the verified TLS session towards ``vlam-api.rijksweb.nl`` itself. Since RC-167 that same proxy also
offers a DOORLUS on a second port: it passes the TLS session through instead of
terminating it, so the consumer speaks to VLAM itself. This service is the thin ZAD side
of both: it hands a consumer the addresses of that proxy, opens the network path to them,
and for the doorlus adds the two things that path needs to work at all -- a hosts entry
that points the VLAM name at our proxy, and the CA bundle to verify the certificate
against. The VPN (headscale + the passthrough proxy on 8080) is for laptops and is untouched
by any of this; see ``vlam.md`` and ``features/vlam-service.md``.

Three things are deliberately the way they are:

* **The address comes from the cluster configuration, never from this module.** VLAM hangs
  off the RON link, which exists on exactly one cluster, and the proxy is a component of a
  tenant project (``vlam-wt8``) that may one day move. What a cluster has is a ``vlam``
  entry (``opi/core/cluster_config.py``); what it does not have is the service.
* **This service writes the EGRESS half only.** The ingress half lives once in
  ``vlam-wt8``: a ``cross-domain-access`` inbound rule with the WILDCARD peer
  (``from: {project: "*"}``, RC-142) that opens port 8081 of the proxy to every source. So
  taking the service is enough for a consumer, and the owner of a shared facility is not
  made the gatekeeper of a self-service platform. What the caller may then DO is authorized
  by VLAM itself, on its API key -- the network rule decides reachability, not identity.
* **No config fields.** There is nothing to choose: one endpoint, one variable, one rule.
  A config block would only be a place for a value to go stale.

Deliberately absent, so the next reader does not go looking:

* ``provision`` / ``cleanup_manager_key`` -- nothing is created outside the manifests. The
  generic service-manifest prune removes the policy file when the service is switched off.
* ``manifest_secret_class`` -- neither address is a secret, so both are plain env vars.
  Encrypting a public cluster address would only hide from its owner what their pod was
  told. ``build_secret_files`` IS used, but for a file rather than a credential: the CA
  bundle of the doorlus travels the same Secret-to-volume road an attachment file takes,
  because that is the one existing way to get a file into a pod.
* ``config_approvals`` -- there is nothing per project to judge: the VLAM side is open to
  the cluster and VLAM authorizes its own callers. An approval here would gate reachability
  while the thing that actually protects VLAM is its API key.
"""

from __future__ import annotations

import logging
from typing import Any

from opi.core.config import settings
from opi.services.catalog.base import (
    DeploymentManifestContext,
    DeploymentManifestSpec,
    DetailPageSection,
    ManifestContext,
    ManifestContribution,
    ProjectPageContext,
    SecretFileSpec,
    Service,
)
from opi.services.catalog.events import on
from opi.services.catalog.vlam.endpoint import CA_BUNDLE_URL, vlam_endpoint
from opi.services.catalog.vlam.variables import VlamVariables
from opi.services.services import ServiceDefinition
from opi.services.services_enums import CleanupStrategy, ServiceType, UIEvent
from opi.utils.naming import generate_network_policy_name, generate_unique_name

logger = logging.getLogger(__name__)


class VlamService(Service):
    service_type = ServiceType.VLAM
    definition = ServiceDefinition(
        name="VLAM-API",
        description=(
            "Geeft de optie te verbinden met de VLAM-API van SSC-ICT. Je hebt zelf"
            " keys nodig om de service te mogen gebruiken, dat kan niet via ZAD."
        ),
        help_template="vlam/help.md",
        icon="wolk",
        color="donkerblauw",
        variables=[var.value for var in VlamVariables],
        cleanup_strategy=CleanupStrategy.NONE,
    )
    #: After the existing contributors, so no already-rendered manifest changes order.
    manifest_order = 80

    def available_on_cluster(self, cluster: str) -> bool:
        """Only where the cluster configuration knows a VLAM endpoint.

        Read from the configuration rather than from a cluster name, so moving VLAM (or
        adding a second cluster with a RON link) is a configuration change and not a code
        change. Both the wizard card and the save-time refusal go through here.
        """
        return vlam_endpoint(cluster) is not None

    def web_routers(self) -> list[Any]:
        """The endpoints its page block needs. Imported here, not at module scope: a
        route module reaches into the app, which the catalog itself must not do."""
        from opi.services.catalog.vlam.routes import vlam_router

        return [*super().web_routers(), vlam_router]

    @on(UIEvent.PROJECT_SECTIONS)
    def vlam_block(self, ctx: ProjectPageContext) -> list[DetailPageSection]:
        """The two paths to VLAM side by side, with the CA bundle to download.

        Only on a cluster that offers the doorlus: without it there is no second address,
        no bundle and nothing to download, and a block that shows one address the user
        already has in an env var would be a page telling them nothing.
        """
        endpoint = vlam_endpoint(settings.CLUSTER_MANAGER)
        if endpoint is None or endpoint.passthrough is None:
            return []
        passthrough = endpoint.passthrough
        return [
            DetailPageSection(
                template="vlam/section-detail.html.j2",
                context={
                    "api_url": endpoint.api_url,
                    "direct_url": passthrough.api_url,
                    "ca_path": passthrough.container_path,
                    "ca_filename": passthrough.ca_bundle_filename,
                    "ca_download_url": CA_BUNDLE_URL,
                },
            )
        ]

    @staticmethod
    def ca_secret_name(deployment_name: str) -> str:
        """The Secret holding the CA bundle for one deployment.

        One per deployment rather than one per component: every component of the
        deployment mounts the same platform file, and a Secret per component would be the
        same bytes written N times under N names.
        """
        return f"{deployment_name}-vlam-ca"

    def contribute_manifest_context(self, ctx: ManifestContext) -> ManifestContribution:
        """What a component that ticked the service is given.

        Always ``VLAM_API_URL``, the TERMINATED path: a plain env var and not an envFrom
        secret, because the value is an in-cluster address that the reader of the manifest
        should be able to see. When the cluster has no VLAM endpoint nothing is
        contributed -- the save-time validation refuses that combination, and generation
        must not fail on a project that slipped through.

        On a cluster that also offers the DOORLUS (RC-167), three more things, and they go
        together or not at all:

        * ``VLAM_API_URL_DIRECT`` -- the address of the passthrough port;
        * a ``hostAliases`` entry -- because that address carries the VLAM hostname (TLS
          compares it against the certificate) while the traffic must reach our proxy;
        * the CA bundle as a mounted file plus ``VLAM_CA_BUNDLE_PATH`` -- because on this
          path the consumer verifies the certificate itself and does not know the issuer.

        Each on its own is useless: an address without the name fails on every connection,
        the name without the issuer fails on an unknown CA. ``VlamEndpoint.passthrough``
        is one object for exactly that reason.
        """
        endpoint = vlam_endpoint(ctx.cluster)
        if endpoint is None:
            logger.warning(
                "Component '%s' gebruikt vlam maar cluster '%s' kent geen VLAM-endpoint; geen VLAM_API_URL gezet",
                ctx.unique_name,
                ctx.cluster,
            )
            return ManifestContribution()

        contribution = ManifestContribution(env_vars={VlamVariables.API_URL.value.name: endpoint.api_url})
        passthrough = endpoint.passthrough
        if passthrough is None:
            logger.info(
                "Cluster '%s' biedt geen VLAM-doorlus aan; component '%s' krijgt alleen VLAM_API_URL",
                ctx.cluster,
                ctx.unique_name,
            )
            return contribution

        contribution.env_vars[VlamVariables.API_URL_DIRECT.value.name] = passthrough.api_url
        contribution.env_vars[VlamVariables.CA_BUNDLE_PATH.value.name] = passthrough.container_path
        contribution.template_vars["host_aliases"] = [{"ip": passthrough.cluster_ip, "hostnames": [passthrough.host]}]
        contribution.secret_mounts = [
            {
                "name": "vlam-ca",
                "secret_name": self.ca_secret_name(ctx.deployment_name),
                "mount_path": passthrough.container_path,
                "sub_path": passthrough.ca_bundle_filename,
            }
        ]
        contribution.secret_files = self.build_secret_files(ctx)
        return contribution

    def build_secret_files(self, ctx: ManifestContext) -> list[SecretFileSpec]:
        """The CA bundle as a Secret, along the same road an attachment file takes.

        The bundle is a PLATFORM datum, not a project one: identical for every consumer
        and changing only when VLAM changes issuer. As an attachment the same public file
        would land AGE-encrypted in every project file, and rotating it would mean every
        project re-uploading it; delivered by the service, rotating is one file in one
        place. What it does reuse is the attachment MACHINERY (Secret -> volume ->
        volumeMount), so there is no second way to get a file into a pod.
        """
        endpoint = vlam_endpoint(ctx.cluster)
        if endpoint is None or endpoint.passthrough is None:
            return []
        passthrough = endpoint.passthrough
        return [
            SecretFileSpec(
                secret_name=self.ca_secret_name(ctx.deployment_name),
                secret_pairs={passthrough.ca_bundle_filename: passthrough.ca_bundle_path.read_text()},
            )
        ]

    def contribute_deployment_manifests(self, ctx: DeploymentManifestContext) -> list[DeploymentManifestSpec]:
        """One egress NetworkPolicy per component that ticked the service.

        Per component and not per deployment, because access is per component: a policy
        selecting the whole deployment opened the way to VLAM for pods whose owner never
        asked for it. Egress only -- this opens the way OUT; whether a pod actually gets
        through is decided by the inbound rule at the VLAM side.
        """
        endpoint = vlam_endpoint(ctx.cluster)
        if endpoint is None:
            return []

        deployment_name = ctx.deployment["name"]
        return [
            DeploymentManifestSpec(
                filename=f"{deployment_name}-{self.service_type.value}-{component}-network-policy",
                template_path="service-network-policy.yaml.jinja",
                values={
                    "name": generate_network_policy_name(f"{self.service_type.value}-{component}", deployment_name),
                    "namespace": ctx.namespace,
                    "pod_selector": {"app": generate_unique_name(deployment_name, component)},
                    "ingress": [],
                    "egress": [
                        {
                            "peer": {"namespace": endpoint.namespace, "pod_labels": endpoint.pod_labels},
                            # Every port of the proxy the consumer was given an address
                            # for. The doorlus sits on a second port of the SAME pod, so
                            # opening only the terminated one hands out an address that
                            # times out.
                            "ports": endpoint.ports,
                        }
                    ],
                },
            )
            for component in self.components_using_service(ctx)
        ]
