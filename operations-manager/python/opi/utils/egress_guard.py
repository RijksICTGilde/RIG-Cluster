"""Whether the cluster may connect to a host.

Several connectors need this check; it sits in one place so that a destination (a registry,
an external clone source) cannot lie in private or cluster-internal address space.
"""

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse

from opi.core.cluster_config import get_external_clone_allowed_private_networks
from opi.core.config import settings

logger = logging.getLogger(__name__)


async def host_is_allowed(host: str, allowed_networks: list[str] | None = None) -> bool:
    """Does ``host`` resolve to addresses we are allowed to reach?

    Every address has to be global, or fall inside one of ``allowed_networks``. A host that
    does not resolve is not allowed: the difference would otherwise give away which internal
    names exist.
    """
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror, UnicodeError:
        return False
    networks = [ipaddress.ip_network(network, strict=False) for network in allowed_networks or []]
    resolved = [ipaddress.ip_address(address[4][0]) for address in addresses]
    return all(address.is_global or any(address in network for network in networks) for address in resolved)


async def host_is_public(host: str) -> bool:
    """Does ``host`` resolve to global addresses only?"""
    return await host_is_allowed(host)


def has_direct_clone_source(host: str | None, port: int | None) -> bool:
    """Is a direct clone source complete? Without a host or a port there is nothing to dial."""
    return bool(host) and bool(port)


#: One message for every refused clone source, otherwise the refusal is an oracle itself.
REFUSED_CLONE_SOURCE_REASON = "de kloonbron moet op een publiek adres staan"

#: Same, for a destination the caller named for a restore.
REFUSED_EXTERNAL_TARGET_REASON = "het doel moet op een publiek adres staan"

#: One message for every failed clone source connection: unreachable, unknown source and
#: wrong password must not be distinguishable from outside.
UNREACHABLE_SOURCE_REASON = "de bron is niet bereikbaar of weigerde de verbinding"


async def clone_source_refusal(direct_host: str | None, tunnel_server_url: str | None) -> str | None:
    """The message for an external clone source we may not reach, otherwise None.

    What is checked is the host OUR pod addresses: the chisel server when there is a tunnel,
    otherwise the source itself. What lies behind the tunnel resolves on the server side.
    """
    if tunnel_server_url is None and direct_host is None:
        return None

    host = urlparse(tunnel_server_url).hostname if tunnel_server_url else direct_host
    # No host means refused (safer than letting it through); str() because the schema allows integers.
    if await _host_may_be_reached(str(host) if host else None):
        return None
    logger.warning("Externe kloonbron geweigerd: %r resolvet niet of niet naar een toegestaan adres", host)
    return REFUSED_CLONE_SOURCE_REASON


async def external_target_refusal(endpoint: str | None) -> str | None:
    """The message for an external destination we may not write to, otherwise None.

    Same rule as for a clone source: the pod has to dial a public address, or one inside a
    network this cluster allows. ``endpoint`` may be a URL, a host with a port, or a bare
    host, which is how the restore endpoints take a target.
    """
    if endpoint is None:
        return None

    host = endpoint_host(endpoint)
    if await _host_may_be_reached(host):
        return None
    logger.warning("Extern terugzetdoel geweigerd: %r resolvet niet of niet naar een toegestaan adres", host)
    return REFUSED_EXTERNAL_TARGET_REASON


def endpoint_host(endpoint: str) -> str | None:
    """The hostname out of an endpoint: a URL, a host with a port, or a bare host."""
    return urlparse(endpoint if "//" in endpoint else f"//{endpoint}").hostname


async def _host_may_be_reached(host: str | None) -> bool:
    """Whether this cluster may dial ``host``. No host at all is refused."""
    if not host:
        return False
    return await host_is_allowed(host, get_external_clone_allowed_private_networks(settings.CLUSTER_MANAGER))
