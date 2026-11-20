"""Does the entrypoint this cluster renders for actually exist on it?

Asked before a bulk reprocess writes the publication of every project: without this, one
call could point every project at an entrypoint that is not there.
"""

import logging
from typing import Any

from opi.connectors.kubectl import KubectlConnector, create_kubectl_connector
from opi.core.cluster_config import RoutingApi, get_declared_gateway_config, get_routing_api

logger = logging.getLogger(__name__)

#: kubectl type name of the Gateway, fully qualified so it cannot be mistaken for another CRD.
GATEWAY_TYPE = "gateways.gateway.networking.k8s.io"


class RoutingEntrypointMissingError(Exception):
    """The configured entrypoint is definitively not usable on this cluster."""


class RoutingEntrypointUnknownError(Exception):
    """The check itself failed, so nothing is known about the entrypoint."""


def _is_programmed(gateway: dict[str, Any]) -> bool:
    conditions = gateway.get("status", {}).get("conditions", []) or []
    return any(c.get("type") == "Programmed" and c.get("status") == "True" for c in conditions)


async def _verify_gateway(kubectl: KubectlConnector, cluster: str) -> str:
    """Reads the gateway block without the stand guard: the watcher measures this entrypoint
    while OPI still renders Ingress, and ``get_gateway_config`` refuses exactly then."""
    gateway_config = get_declared_gateway_config(cluster)
    name, namespace = gateway_config["name"], gateway_config["namespace"]

    exists = await kubectl.resource_exists(GATEWAY_TYPE, name, namespace)
    if exists is None:
        raise RoutingEntrypointUnknownError(
            f"Kon niet vaststellen of Gateway '{namespace}/{name}' bestaat. Probeer het opnieuw."
        )
    if not exists:
        raise RoutingEntrypointMissingError(
            f"Gateway '{namespace}/{name}' bestaat niet op cluster '{cluster}'. Rol eerst de "
            f"infrastructuur van de Gateway API uit."
        )

    gateway = await kubectl.get_namespaced_resource(GATEWAY_TYPE, name, namespace)
    if gateway is None:
        raise RoutingEntrypointUnknownError(f"Kon de status van Gateway '{namespace}/{name}' niet lezen.")
    if not _is_programmed(gateway):
        raise RoutingEntrypointMissingError(
            f"Gateway '{namespace}/{name}' heeft geen conditie Programmed=True en accepteert dus nog geen routes."
        )
    return f"Gateway {namespace}/{name} is Programmed"


async def _verify_ingress_class(kubectl: KubectlConnector, cluster: str) -> str:
    """At least one IngressClass, which is as much as an Ingress on this platform needs.

    Not "one that is default": the generated Ingresses carry no ``ingressClassName``, and the
    controller of the sandbox runs with ``--watch-ingress-without-class=true``, so it picks up
    a class-less Ingress. That coupling is measured in
    tests/test_sandbox_routing_contract.py.
    """
    classes = await kubectl.get_cluster_resources("ingressclass")
    if classes is None:
        raise RoutingEntrypointUnknownError("Kon de IngressClasses van het cluster niet lezen. Probeer het opnieuw.")
    names = [item.get("metadata", {}).get("name", "") for item in classes]
    if not any(names):
        raise RoutingEntrypointMissingError(
            f"Cluster '{cluster}' heeft geen enkele IngressClass en dus geen ingress-controller."
        )
    return f"IngressClass {names[0]} bestaat"


async def verify_routing_entrypoint(cluster: str, routing: RoutingApi | None = None) -> str:
    """One line describing the entrypoint that was found, for the answer and the log.

    ``routing`` names the stand to measure; ``None`` is the stand the cluster renders in.
    The watcher passes a stand OPI does not render in yet.

    Raises:
        RoutingEntrypointMissingError: the entrypoint is definitively not usable.
        RoutingEntrypointUnknownError: the check could not be performed.
    """
    if routing is None:
        routing = get_routing_api(cluster)
    kubectl = create_kubectl_connector()
    if routing is RoutingApi.GATEWAY:
        return await _verify_gateway(kubectl, cluster)
    return await _verify_ingress_class(kubectl, cluster)
