"""Where VLAM sits on a cluster, derived once from the cluster configuration.

The two things this service hands out both come from here: the ADDRESS a consumer's pod
is given, and the NetworkPolicy PEER that pod is allowed to reach. Deriving them from one
entry is the point -- an address that names one pod while the rule opens another fails as
a network timeout and is in truth a configuration mistake, which is the most expensive
kind to debug from the consumer's side.

Since RC-167 the same entry also yields the DOORLUS half: poort 8443 of that same proxy
passes the TLS session through instead of terminating it, so the consumer speaks to VLAM
itself and verifies the certificate itself. That takes three things -- the address, the
name the URL must carry (TLS compares the hostname from the URL against the certificate)
and the issuer to verify against -- and any one of them alone is useless. They therefore
travel as ONE object, ``VlamPassthrough``: either a cluster offers the doorlus or it does
not, and no caller can accidentally hand out two thirds of it.

No cluster name appears in this module. A cluster without a ``vlam`` entry has no VLAM,
and every caller reads that as ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opi.core.cluster_config import get_prefixed_namespace, get_vlam_config
from opi.utils.naming import generate_unique_name

#: Where the CA bundles the cluster configuration names live: in this service's own
#: package (RC-36). The configuration carries the FILE NAME, not a path, so the catalog
#: keeps its own files and ``opi/core`` never points into a service package.
CA_BUNDLE_DIR = Path(__file__).parent

#: Directory the bundle is mounted in. Deliberately NOT ``/etc/ssl/certs/``: some runtimes
#: read that directory by themselves, and then whether the doorlus works would depend on
#: the consumer's base image rather than on what the application asked for.
CONTAINER_CA_DIR = "/etc/ssl/vlam"

#: Where the bundle can be downloaded. Named here rather than in ``routes.py`` so the page
#: block can link to it without importing the route module (which reaches into the app).
CA_BUNDLE_URL = "/services/vlam/ca-bundle"


@dataclass(frozen=True)
class VlamPassthrough:
    """The doorlus (RC-167): the three things a consumer needs to speak to VLAM itself.

    Built only when the cluster configuration carries all four keys AND the CA bundle it
    names is actually present. Missing any of them means the path cannot work, and an
    address without a trustworthy issuer is exactly the half-configured state this plan
    exists to prevent -- so the whole thing is absent instead.
    """

    #: What the consumer connects to, e.g. ``https://vlam-api.rijksweb.nl:8443``. It
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
    #: The bundle on disk, inside this service's package.
    ca_bundle_path: Path
    #: Where it lands in the pod, e.g. ``/etc/ssl/vlam/rijksdienst-ca.pem``.
    container_path: str

    @property
    def ca_bundle_filename(self) -> str:
        """The file's own name -- also the Secret key and the ``subPath`` of the mount."""
        return self.ca_bundle_path.name


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

    Two ways to answer None, and both are normal rather than an error:

    * the entry carries no doorlus keys -- a cluster whose proxy has no passthrough port;
    * the CA bundle it names is not in this package. The bundle is a platform datum that
      is supplied once (and rotated in one place); until it is there, handing out an
      address plus a hostname alias would produce an "unknown issuer" failure at the
      consumer with nothing on this side saying why.
    """
    host = config.get("api_host")
    cluster_ip = config.get("cluster_ip")
    port = config.get("passthrough_port")
    bundle = config.get("ca_bundle")
    if not (host and cluster_ip and port and bundle):
        return None

    ca_bundle_path = CA_BUNDLE_DIR / str(bundle)
    if not ca_bundle_path.is_file():
        return None

    return VlamPassthrough(
        api_url=f"https://{host}:{int(port)}",
        host=str(host),
        cluster_ip=str(cluster_ip),
        port=int(port),
        ca_bundle_path=ca_bundle_path,
        container_path=f"{CONTAINER_CA_DIR}/{ca_bundle_path.name}",
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
