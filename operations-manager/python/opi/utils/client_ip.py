"""The client IP of a request, decided in one place.

``X-Forwarded-For`` is unauthenticated text: anyone on the path may append a value, so the
leftmost value is the caller's own. The only indisputable fact is the TCP peer address. That
is why a fixed number of hops is counted from the RIGHT, as Envoy (``xff_num_trusted_hops``)
and wies (``TRUSTED_PROXY_HOPS``) do: only the value a trusted proxy added itself is worth
anything. With ``TRUSTED_PROXY_HOPS=0`` no header is involved at all.

The chain is read with ``getlist``, because the OpenShift router (HAProxy,
``forwardedHeaderPolicy: Append``) puts its value in a SEPARATE second header line behind the
client's. ``request.headers.get`` then returns the FIRST line, the attacker's.
"""

import ipaddress
import logging

from starlette.requests import Request  # noqa: TC002 -- FastAPI needs Request at runtime

from opi.core.config import settings

logger = logging.getLogger(__name__)

#: Where ``PeerAddressMiddleware`` puts the untouched peer address. Needed because
#: ``ProxyHeadersMiddleware`` overwrites ``scope["client"]`` with a value from the header.
PEER_SCOPE_KEY = "opi_peer_ip"

UNKNOWN = "unknown"


def _strip_port(value: str) -> str:
    """Remove a trailing port: ``1.2.3.4:56789`` and ``[::1]:443``."""
    if value.startswith("["):
        host, _, _ = value.partition("]")
        return host[1:]
    if value.count(":") == 1:
        # A bare IPv6 holds more colons, so this is IPv4 with a port.
        return value.split(":")[0]
    return value


def _as_ip(value: str) -> str | None:
    """The normalised value, or None when it is not an IP address."""
    bare = _strip_port(value.strip())
    try:
        ipaddress.ip_address(bare)
    except ValueError:
        return None
    return bare


def forwarded_chain(request: Request) -> list[str]:
    """Every ``X-Forwarded-For`` value in order, across header lines and commas alike."""
    return [
        part.strip() for line in request.headers.getlist("x-forwarded-for") for part in line.split(",") if part.strip()
    ]


def peer_ip(request: Request) -> str:
    """The TCP peer address: the one fact in a request that cannot be chosen."""
    peer = request.scope.get(PEER_SCOPE_KEY)
    if isinstance(peer, str) and peer:
        return peer
    return request.client.host if request.client else UNKNOWN


def client_ip(request: Request) -> str:
    """The address this client is held to: buckets, log lines and traces.

    With ``TRUSTED_PROXY_HOPS`` hops that is the Nth value from the right, the one the
    trusted proxy added itself. When there is no usable value in that position, or the
    request arrives with fewer hops than configured, it falls back to the peer address: a
    request that bypasses the ingress may never carry an address of its own choosing.
    """
    hops = settings.TRUSTED_PROXY_HOPS
    if hops > 0:
        chain = forwarded_chain(request)
        if len(chain) >= hops:
            address = _as_ip(chain[-hops])
            if address:
                return address
    return peer_ip(request)
