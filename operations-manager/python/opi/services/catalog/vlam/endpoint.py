"""Where VLAM sits on a cluster, derived once from the cluster configuration.

The two things this service hands out both come from here: the ADDRESS a consumer's pod
is given, and the NetworkPolicy PEER that pod is allowed to reach. Deriving them from one
entry is the point -- an address that names one pod while the rule opens another fails as
a network timeout and is in truth a configuration mistake, which is the most expensive
kind to debug from the consumer's side.

Since RC-167 the same entry also yields the DOORLUS half: poort 8443 of that same proxy
passes the TLS session through instead of terminating it, so the consumer speaks to VLAM
itself and verifies the certificate itself. That takes two things: the address and the
name the URL must carry (TLS compares the hostname from the URL against the certificate).
Either one alone is useless. They therefore travel as ONE object, ``VlamPassthrough``:
either a cluster offers the doorlus or it does not, and no caller can accidentally hand
out half of it.

No cluster name appears in this module. A cluster without a ``vlam`` entry has no VLAM,
and every caller reads that as ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opi.core.cluster_config import get_prefixed_namespace, get_vlam_config
from opi.utils.naming import generate_unique_name


@dataclass(frozen=True)
class VlamPassthrough:
    """The doorlus (RC-167): the two things a consumer needs to speak to VLAM itself.

    Built only when the cluster configuration carries all three keys. Missing one means
    the path cannot work, and an address without the name it must be reached under is
    exactly the half-configured state this design exists to prevent, so the whole thing
    is absent instead.

    No CA material here (RC-414): the certificate of ``api_host`` comes from DigiCert and
    is in every standard trust store, so the consumer verifies it the way it verifies any
    other public endpoint.
    """

    #: What the consumer connects to, e.g. ``https://vlam-api.overheid-i.nl:8443``. It
    #: carries the VLAM hostname (TLS validates it) and resolves to our proxy via the
    #: ``hostAliases`` entry below.
    api_url: str
    #: The hostname in that URL, and the left-hand side of the ``/etc/hosts`` line.
    host: str
    #: The ClusterIP of the proxy Service, from the configuration. ``hostAliases`` takes
    #: an address and not a service name; see ``get_vlam_config`` for why that is a
    #: configured value and an accepted risk.
    cluster_ip: str
    #: The port the doorlus listens on.
    port: int


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
    #: The doorlus half, or None when this cluster does not offer it.
    passthrough: VlamPassthrough | None = None

    @property
    def ports(self) -> list[int]:
        """Every port of the proxy a consumer must be able to reach.

        The egress rule is built from this rather than from ``port`` alone: the doorlus
        sits on a second port of the SAME pod, and an address the consumer is given but
        the network rule does not open is the timeout this module exists to prevent.
        """
        if self.passthrough is None:
            return [self.port]
        return [self.port, self.passthrough.port]


def _passthrough(config: dict[str, Any]) -> VlamPassthrough | None:
    """The doorlus half of a cluster's ``vlam`` entry, or None when it is not offered.

    Absent is normal rather than an error: a cluster whose proxy has no passthrough port
    carries none of these keys, and then the terminated path is the only one.
    """
    host = config.get("api_host")
    cluster_ip = config.get("cluster_ip")
    port = config.get("passthrough_port")
    if not (host and cluster_ip and port):
        return None

    return VlamPassthrough(
        api_url=f"https://{host}:{int(port)}",
        host=str(host),
        cluster_ip=str(cluster_ip),
        port=int(port),
    )


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
        passthrough=_passthrough(config),
    )
