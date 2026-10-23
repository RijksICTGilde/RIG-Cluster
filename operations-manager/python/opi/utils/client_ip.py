"""Het client-IP van een verzoek, op één plek bepaald.

``X-Forwarded-For`` is niet-geauthenticeerde tekst: iedereen op het pad mag er een waarde
bij zetten, dus de linkse waarde is die van de aanroeper zelf. Het enige onbetwistbare
gegeven is het TCP-peer-adres. Daarom tellen we een vast aantal hops van RECHTS, net als
Envoy (``xff_num_trusted_hops``) en wies (``TRUSTED_PROXY_HOPS``): alleen de waarde die
een vertrouwde proxy zelf heeft toegevoegd is iets waard. Met ``TRUSTED_PROXY_HOPS=0``
komt er geen enkele header aan te pas.

De keten lezen we met ``getlist``, want de OpenShift-router (HAProxy, ``forwardedHeaderPolicy:
Append``) zet zijn waarde als APARTE tweede headerregel achter die van de client.
``request.headers.get`` geeft dan de EERSTE regel, dus die van de aanvaller.
"""

import ipaddress
import logging

from opi.core.config import settings
from starlette.requests import Request  # noqa: TC002 -- FastAPI heeft Request tijdens runtime nodig

logger = logging.getLogger(__name__)

#: Waar ``PeerAddressMiddleware`` het onaangetaste peer-adres neerzet. Nodig omdat
#: ``ProxyHeadersMiddleware`` ``scope["client"]`` overschrijft met een waarde uit de header.
PEER_SCOPE_KEY = "opi_peer_ip"

ONBEKEND = "unknown"


def _strip_port(waarde: str) -> str:
    """Haalt een achterliggende poort weg: ``1.2.3.4:56789`` en ``[::1]:443``."""
    if waarde.startswith("["):
        host, _, _ = waarde.partition("]")
        return host[1:]
    if waarde.count(":") == 1:
        # Een kale IPv6 heeft meer dubbele punten, dus dit is IPv4 met poort.
        return waarde.split(":")[0]
    return waarde


def _als_ip(waarde: str) -> str | None:
    """De genormaliseerde waarde, of None als het geen IP-adres is."""
    kaal = _strip_port(waarde.strip())
    try:
        ipaddress.ip_address(kaal)
    except ValueError:
        return None
    return kaal


def forwarded_chain(request: Request) -> list[str]:
    """Alle ``X-Forwarded-For``-waarden op rij, over meerdere headerregels en komma's heen."""
    return [
        deel.strip()
        for regel in request.headers.getlist("x-forwarded-for")
        for deel in regel.split(",")
        if deel.strip()
    ]


def peer_ip(request: Request) -> str:
    """Het TCP-peer-adres: het enige gegeven in een verzoek dat niet te kiezen valt."""
    peer = request.scope.get(PEER_SCOPE_KEY)
    if isinstance(peer, str) and peer:
        return peer
    return request.client.host if request.client else ONBEKEND


def client_ip(request: Request) -> str:
    """Het adres waarop we deze client aanspreken: emmers, logregels en sporen.

    Met ``TRUSTED_PROXY_HOPS`` hops is dat de N-de waarde van rechts, die de vertrouwde
    proxy zelf heeft toegevoegd. Staat er geen bruikbare waarde op die plek, of komt het
    verzoek met minder hops binnen dan ingesteld, dan valt het terug op het peer-adres:
    een verzoek dat de ingress omzeilt mag nooit een adres naar keuze meekrijgen.
    """
    hops = settings.TRUSTED_PROXY_HOPS
    if hops > 0:
        keten = forwarded_chain(request)
        if len(keten) >= hops:
            adres = _als_ip(keten[-hops])
            if adres:
                return adres
    return peer_ip(request)
