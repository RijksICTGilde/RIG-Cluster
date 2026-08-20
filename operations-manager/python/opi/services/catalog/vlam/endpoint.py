"""Where VLAM sits on a cluster, derived once from the cluster configuration.

The two things this service hands out both come from here: the ADDRESS a consumer's pod
is given, and the NetworkPolicy PEER that pod is allowed to reach. Deriving them from one
entry is the point -- an address that names one pod while the rule opens another fails as
a network timeout and is in truth a configuration mistake, which is the most expensive
kind to debug from the consumer's side.

No cluster name appears in this module. A cluster without a ``vlam`` entry has no VLAM,
and every caller reads that as ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass

from opi.core.cluster_config import get_prefixed_namespace, get_vlam_config
from opi.utils.naming import generate_unique_name


@dataclass(frozen=True)
class VlamEndpoint:
    """The in-cluster VLAM proxy on one cluster."""

    #: Plain HTTP; the proxy sets up the verified TLS session towards VLAM itself.
    api_url: str
    #: Namespace of the proxy, already cluster-prefixed.
    namespace: str
    #: Pod labels that pin the peer to exactly that proxy. ``project`` closes the gap
    #: that another project could take a namespace of the same name -- the same second
    #: gate cross-domain-access uses.
    pod_labels: dict[str, str]
    port: int


def vlam_endpoint(cluster: str) -> VlamEndpoint | None:
    """The VLAM endpoint on ``cluster``, or None when that cluster has no VLAM."""
    config = get_vlam_config(cluster)
    if config is None:
        return None
    unique_name = generate_unique_name(config["deployment"], config["component"])
    namespace = get_prefixed_namespace(cluster, config["namespace"])
    port = int(config["port"])
    return VlamEndpoint(
        api_url=f"http://{unique_name}.{namespace}.svc.cluster.local:{port}",
        namespace=namespace,
        pod_labels={"app": unique_name, "project": config["project"]},
        port=port,
    )
