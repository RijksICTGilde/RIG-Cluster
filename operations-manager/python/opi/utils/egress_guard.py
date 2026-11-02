"""Toetst of het cluster naar een host mag verbinden.

Meerdere connectoren hebben deze toets nodig; het staat centraal zodat een bestemming
(registry, externe kloonbron) niet in privaat of cluster-intern adresruimte kan liggen.
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
    """Resolvet ``host`` naar adressen die wij mogen benaderen?

    Elk adres moet globaal zijn, of in een van ``allowed_networks`` vallen. Een host die
    niet resolvet is niet toegestaan: anders verklapt het verschil welke interne namen
    bestaan.
    """
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror, UnicodeError:
        return False
    networks = [ipaddress.ip_network(network, strict=False) for network in allowed_networks or []]
    resolved = [ipaddress.ip_address(address[4][0]) for address in addresses]
    return all(address.is_global or any(address in network for network in networks) for address in resolved)


async def host_is_public(host: str) -> bool:
    """Resolvet ``host`` uitsluitend naar globale adressen?"""
    return await host_is_allowed(host)


#: Eén melding voor elke geweigerde kloonbron, anders is de weigering zelf een orakel.
REFUSED_CLONE_SOURCE_REASON = "de kloonbron moet op een publiek adres staan"

#: Eén melding voor elke mislukte kloonbron-verbinding: onbereikbaar, onbekende bron,
#: en fout wachtwoord mogen naar buiten niet te onderscheiden zijn.
UNREACHABLE_SOURCE_REASON = "de bron is niet bereikbaar of weigerde de verbinding"


async def clone_source_refusal(direct_host: str | None, tunnel_server_url: str | None) -> str | None:
    """De melding voor een externe kloonbron die wij niet mogen benaderen, anders None.

    Getoetst wordt de host die ONZE pod aanspreekt: de chisel-server als er een tunnel is,
    anders de bron zelf. Wat achter de tunnel ligt resolvet aan de kant van de server.
    """
    if tunnel_server_url is None and direct_host is None:
        return None

    host = urlparse(tunnel_server_url).hostname if tunnel_server_url else direct_host
    allowed = get_external_clone_allowed_private_networks(settings.CLUSTER_MANAGER)
    # Geen host betekent geweigerd (veiliger dan doorlaten); str() omdat het schema integers toestaat.
    if host and await host_is_allowed(str(host), allowed):
        return None
    logger.warning("Externe kloonbron geweigerd: %r resolvet niet of niet naar een toegestaan adres", host)
    return REFUSED_CLONE_SOURCE_REASON
