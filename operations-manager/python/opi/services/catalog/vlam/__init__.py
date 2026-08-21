"""vlam service: reach the VLAM-API from inside the cluster, without a VPN.

VLAM is the language-model API SSC-ICT offers over the RON. Since RC-142 the vlam project
runs a second, INTERNAL proxy next to the VPN gateway: it accepts plain HTTP and sets up
the verified TLS session towards ``vlam-api.rijksweb.nl`` itself. This service is the thin
ZAD side of that: it hands a consumer the address of that proxy and opens the network path
to it. The VPN (headscale + the passthrough proxy on 8080) is for laptops and is untouched
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
* ``manifest_secret_class`` / ``build_secret_files`` -- the address is not a secret, so it
  is a plain env var. Encrypting a public cluster address would only hide from its owner
  what their pod was told.
* ``config_approvals`` -- there is nothing per project to judge: the VLAM side is open to
  the cluster and VLAM authorizes its own callers. An approval here would gate reachability
  while the thing that actually protects VLAM is its API key.
"""

from __future__ import annotations

import logging
from typing import Any

from opi.services.catalog.base import (
    DeploymentManifestContext,
    DeploymentManifestSpec,
    ManifestContext,
    ManifestContribution,
    Service,
)
from opi.services.catalog.vlam.endpoint import vlam_endpoint
from opi.services.catalog.vlam.variables import VlamVariables
from opi.services.services import ServiceDefinition, service_entry_name
from opi.services.services_enums import CleanupStrategy, ServiceBinding, ServiceType
from opi.utils.naming import generate_network_policy_name

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
        # Per deployment: er valt per component niets te kiezen. Elk component van elke
        # deployment van dit project krijgt hetzelfde adres en dezelfde uitgaande regel.
        binding=ServiceBinding.DEPLOYMENT,
        variables=[var.value for var in VlamVariables],
        cleanup_strategy=CleanupStrategy.NONE,
    )
    #: The project's selection switches this on; no component ever ticks it (see the
    #: binding above), so a component-scoped activation would never fire.
    manifest_activated_by_project = True
    #: After the existing contributors, so no already-rendered manifest changes order.
    manifest_order = 80

    def available_on_cluster(self, cluster: str) -> bool:
        """Only where the cluster configuration knows a VLAM endpoint.

        Read from the configuration rather than from a cluster name, so moving VLAM (or
        adding a second cluster with a RON link) is a configuration change and not a code
        change. Both the wizard card and the save-time refusal go through here.
        """
        return vlam_endpoint(cluster) is not None

    def _selected(self, project_data: dict[str, Any]) -> bool:
        """Whether this project has the service in its project-level services list."""
        return self.service_type.value in [
            service_entry_name(entry) for entry in project_data.get("services", []) or []
        ]

    def contribute_manifest_context(self, ctx: ManifestContext) -> ManifestContribution:
        """``VLAM_API_URL`` for every component of a project that took the service.

        A plain env var, not an envFrom secret: the value is an in-cluster address that
        the reader of the manifest should be able to see. When the cluster has no VLAM
        endpoint nothing is contributed -- the save-time validation refuses that
        combination, and generation must not fail on a project that slipped through.
        """
        endpoint = vlam_endpoint(ctx.cluster)
        if endpoint is None:
            logger.warning(
                "Component '%s' gebruikt vlam maar cluster '%s' kent geen VLAM-endpoint; geen VLAM_API_URL gezet",
                ctx.unique_name,
                ctx.cluster,
            )
            return ManifestContribution()
        api_var = VlamVariables.API_URL.value
        return ManifestContribution(
            # De kanonieke naam EN de gedeclareerde aliassen: de declaratie in
            # variables.py is wat de dienstenpagina en de e2e-probe-spec beloven, dus
            # wat daar staat moet ook echt in de container staan. De coverage-check
            # van de e2e-testpod meet dat letterlijk.
            env_vars=dict.fromkeys((api_var.name, *api_var.aliases), endpoint.api_url)
        )

    def contribute_deployment_manifests(self, ctx: DeploymentManifestContext) -> list[DeploymentManifestSpec]:
        """One egress NetworkPolicy per deployment, towards the VLAM proxy pod.

        Deployment-wide rather than per component, because the service is deployment-bound:
        the pods of a deployment carry ``deployment`` and ``project`` labels, so one policy
        selects exactly them. Egress only -- this opens the way OUT; whether a pod actually
        gets through is decided by the inbound rule at the VLAM side.
        """
        if not self._selected(ctx.project_data):
            return []
        endpoint = vlam_endpoint(ctx.cluster)
        if endpoint is None:
            return []

        deployment_name = ctx.deployment["name"]
        return [
            DeploymentManifestSpec(
                filename=f"{deployment_name}-{self.service_type.value}-network-policy",
                template_path="service-network-policy.yaml.jinja",
                values={
                    "name": generate_network_policy_name(self.service_type.value, deployment_name),
                    "namespace": ctx.namespace,
                    "pod_selector": {"deployment": deployment_name, "project": ctx.project_name},
                    "ingress": [],
                    "egress": [
                        {
                            "peer": {"namespace": endpoint.namespace, "pod_labels": endpoint.pod_labels},
                            "ports": [endpoint.port],
                        }
                    ],
                },
            )
        ]
