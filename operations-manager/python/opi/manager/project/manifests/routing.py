"""Which resource publishes a component: an Ingress, or Gateway API routes and listeners.

The shared Gateway belongs to the infrastructure. OPI only hangs HTTPRoutes and TLSRoutes on
it, and claims a listener per hostname that brings its own certificate.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from opi.core.cluster_config import get_gateway_config
from opi.generation.manifests import MANIFEST_TEMPLATES_DIR
from opi.utils.naming import generate_listener_name

if TYPE_CHECKING:
    from opi.generation.manifests import ManifestGenerator

logger = logging.getLogger(__name__)

#: Basename of the per-deployment ListenerSet manifest. Deployment-level, so it carries no
#: component prefix and ``select_obsolete_component_manifests`` never selects it;
#: :func:`remove_listener_set` is what takes it away again.
LISTENER_SET_MANIFEST = "listenerset"

#: Every manifest type a publication can carry, so a round in either routing API can remove
#: the file the other one would have written.
ROUTE_MANIFEST_TYPES: tuple[str, ...] = ("ingress", "httproute", "tlsroute")

#: Both listeners of the shared Gateway sit on the standard ports, and so does every listener
#: a tenant claims next to them.
HTTPS_PORT = 443


@dataclass(frozen=True)
class RouteParent:
    """The listener an HTTPRoute or TLSRoute attaches to."""

    kind: str
    name: str
    namespace: str | None
    section_name: str


@dataclass(frozen=True)
class PublicationRequest:
    """One hostname of one component, and what publishing it needs to know.

    ``tls_secret_name`` is the Secret the Ingress of this hostname uses today, so a switch
    reuses the certificate cert-manager already holds instead of ordering a new one.
    """

    cluster: str
    hostname: str
    tls_secret_name: str
    issuer_name: str | None = None
    cluster_issuer: str | None = None
    provided_tls_secret: str | None = None
    passthrough: bool = False


@dataclass(frozen=True)
class Publication:
    """The template that publishes one hostname, and the listener it hangs on.

    ``parent`` is None in ingress routing, where the Ingress names no listener.
    """

    template: str
    manifest_type: str
    parent: RouteParent | None


#: The publication in ingress routing: one Ingress, no listener.
INGRESS_PUBLICATION = Publication("ingress.yaml.jinja", "ingress", None)


def covered_by_listener(listener_hostname: str, hostname: str) -> bool:
    """Whether a listener on ``listener_hostname`` already serves this hostname.

    A wildcard covers exactly one label, so ``a.b.sandbox.example`` is NOT covered by
    ``*.sandbox.example`` and needs a listener of its own.
    """
    if listener_hostname.startswith("*."):
        suffix = listener_hostname[1:]
        return hostname.endswith(suffix) and "." not in hostname[: -len(suffix)]
    return hostname == listener_hostname


def shared_https_parent(cluster: str) -> RouteParent:
    """The HTTPS listener of the cluster Gateway, for a hostname its certificate covers."""
    gateway = get_gateway_config(cluster)
    return RouteParent("Gateway", gateway["name"], gateway["namespace"], gateway["https_listener"]["name"])


def hostname_on_shared_listener(cluster: str, hostname: str) -> bool:
    """True when this hostname can ride the certificate of the Gateway itself."""
    listener_hostname = get_gateway_config(cluster)["https_listener"].get("hostname")
    return bool(listener_hostname) and covered_by_listener(listener_hostname, hostname)


@dataclass
class ListenerSetBuilder:
    """Collects the listeners one deployment claims, and writes them as one manifest.

    Two sets, because ``cert-manager.io/issuer`` applies to a whole ListenerSet: a
    customer-provided certificate inside the annotated set would be reissued over.
    """

    cluster: str
    deployment_name: str
    namespace: str
    _managed: dict[str, dict] = field(default_factory=dict, init=False)
    _own: dict[str, dict] = field(default_factory=dict, init=False)
    _issuer_name: str | None = field(default=None, init=False)
    _cluster_issuer: str | None = field(default=None, init=False)

    @property
    def managed_set_name(self) -> str:
        return f"{self.deployment_name}-listeners"

    @property
    def own_set_name(self) -> str:
        return f"{self.deployment_name}-own-listeners"

    def _claim(self, listeners: dict[str, dict], hostname: str, secret_name: str | None) -> str:
        """Record a listener for this hostname and return its name.

        Keyed on the hostname: several components can publish on one hostname and must all
        attach to the same listener. Of their certificate Secrets the first in sort order
        wins, so the outcome does not depend on the order the components are rendered in.
        """
        listener = listeners.setdefault(
            hostname,
            {"name": generate_listener_name(hostname), "hostname": hostname, "port": HTTPS_PORT},
        )
        if secret_name is not None:
            listener["passthrough"] = False
            current = listener.get("tls_secret_name")
            listener["tls_secret_name"] = secret_name if current is None else min(current, secret_name)
        else:
            listener["passthrough"] = True
        return listener["name"]

    def claim_managed(self, request: PublicationRequest) -> RouteParent:
        """A listener whose certificate cert-manager issues, for a hostname off the cluster zone."""
        self._issuer_name = self._issuer_name or request.issuer_name
        self._cluster_issuer = self._cluster_issuer or request.cluster_issuer
        name = self._claim(self._managed, request.hostname, request.tls_secret_name)
        return RouteParent("ListenerSet", self.managed_set_name, None, name)

    def claim_provided(self, request: PublicationRequest) -> RouteParent:
        """A listener on the certificate the customer brought along (tls: provided)."""
        name = self._claim(self._own, request.hostname, request.provided_tls_secret)
        return RouteParent("ListenerSet", self.own_set_name, None, name)

    def claim_passthrough(self, request: PublicationRequest) -> RouteParent:
        """A listener that forwards TLS untouched; the pod terminates it."""
        name = self._claim(self._own, request.hostname, None)
        return RouteParent("ListenerSet", self.own_set_name, None, name)

    def emit(self, manifest_generator: ManifestGenerator, output_dir: str, created_files: list[str]) -> None:
        """Write the ListenerSet manifest, or remove a stale one when nothing was claimed."""
        sets = []
        if self._managed:
            sets.append(
                {
                    "name": self.managed_set_name,
                    "issuer_name": self._issuer_name,
                    "cluster_issuer": self._cluster_issuer,
                    "listeners": sorted(self._managed.values(), key=lambda listener: listener["name"]),
                }
            )
        if self._own:
            sets.append(
                {
                    "name": self.own_set_name,
                    "issuer_name": None,
                    "cluster_issuer": None,
                    "listeners": sorted(self._own.values(), key=lambda listener: listener["name"]),
                }
            )
        if not sets:
            remove_listener_set(output_dir)
            return

        path = manifest_generator.create_manifest_file(
            template_path=os.path.join(MANIFEST_TEMPLATES_DIR, "listenerset.yaml.jinja"),
            values={
                "namespace": self.namespace,
                "gateway": get_gateway_config(self.cluster),
                "listener_sets": sets,
            },
            output_dir=output_dir,
            output_filename=LISTENER_SET_MANIFEST,
            use_sops=False,
        )
        created_files.append(f"{LISTENER_SET_MANIFEST}.yaml")
        logger.info(f"Created ListenerSet manifest with {len(sets)} set(s) for {self.deployment_name}: {path}")


def remove_listener_set(output_dir: str) -> None:
    """Drop the ListenerSet of a deployment that no longer claims a listener.

    Deployment-level and so outside the component-prefix pruning; without this an
    ingress-routing round would leave the file of a gateway-routing round behind.
    """
    stale = os.path.join(output_dir, f"{LISTENER_SET_MANIFEST}.yaml")
    if os.path.exists(stale):
        os.remove(stale)
        logger.info(f"Removed ListenerSet manifest from {output_dir}: no listeners claimed this round")


def publication_for(listeners: ListenerSetBuilder | None, request: PublicationRequest) -> Publication:
    """How this hostname is published, and which listener it claims for it.

    ``listeners`` None means ingress routing. In gateway routing a hostname the Gateway's own
    certificate covers rides the shared listener; every other hostname claims a listener of
    its own in the ListenerSet of this deployment.
    """
    if listeners is None:
        return INGRESS_PUBLICATION
    if request.passthrough:
        return Publication("tlsroute.yaml.jinja", "tlsroute", listeners.claim_passthrough(request))
    if request.provided_tls_secret:
        return Publication("httproute.yaml.jinja", "httproute", listeners.claim_provided(request))
    if hostname_on_shared_listener(request.cluster, request.hostname):
        return Publication("httproute.yaml.jinja", "httproute", shared_https_parent(request.cluster))
    return Publication("httproute.yaml.jinja", "httproute", listeners.claim_managed(request))
