"""``PrometheusConnector.custom_query`` houdt de event loop niet vast.

WAAROM DIT EEN EIGEN TEST HEEFT

De methode is ``async`` maar de client eronder (``prometheus_api_client``) is synchroon en
praat via ``requests``. Rechtstreeks aangeroepen betekent dat: de coroutine geeft de loop
nooit terug voor de duur van het HTTP-verzoek, en zolang die loopt handelt de applicatie
GEEN ENKEL ander verzoek af. In de code stond dat dat mocht "omdat metriekqueries
laagfrequent zijn"; de frequentie is niet het punt, de DUUR is het.

Wat het kostte, gemeten op /admin/diensten: die pagina haalt drie blokken lui op en belooft
dat een kapot blok alleen dat blok kost. Het Keycloak-blok praat rechtstreeks met deze
connector, en met een Prometheus die niet oplost bleven de twee andere blokken op "wordt
opgehaald..." staan tot de DNS- en retryketen op was. Zeven browsertests stonden daarop
rood.

WAT ER GEMETEN WORDT, EN WAAROM ZO

Niet "roept hij to_thread aan" - dat toetst de implementatie en gaat groen mee met elke
herschrijving die het gedrag stukmaakt. Er draait een tweede taak MEE die per tick een
teller ophoogt. Blokkeert de query de loop, dan komt die taak niet aan de beurt en blijft
de teller op nul staan. Dat is precies de storing, in de vorm waarin een gebruiker hem
merkt.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest
from opi.connectors.prometheus import PrometheusConnector

#: Lang genoeg om ruim boven de tickduur uit te komen, kort genoeg om de suite niet op te
#: houden. Een echte DNS-fout duurt seconden; dit is dezelfde storing, verkleind.
_QUERYDUUR = 0.3
_TICK = 0.01


@pytest.fixture(autouse=True)
def reset_singleton():
    """De connector is een singleton; anders lekt hij naar de volgende test."""
    PrometheusConnector._instance = None
    yield
    PrometheusConnector._instance = None


@pytest.fixture
def connector() -> PrometheusConnector:
    """Een verbonden connector met een client die blokkeert zoals de echte dat doet."""
    with patch.object(PrometheusConnector, "__init__", lambda self: None):
        verbinding = PrometheusConnector.__new__(PrometheusConnector)
        verbinding._initialized = True
        verbinding._prometheus_url = "http://prometheus:9090"
        verbinding.prom = MagicMock()
        PrometheusConnector.is_connected = True
        PrometheusConnector._instance = verbinding
        return verbinding


@pytest.mark.asyncio
async def test_de_loop_blijft_draaien_tijdens_een_trage_query(connector: PrometheusConnector) -> None:
    """Een trage query laat andere taken gewoon aan de beurt komen."""

    def traag(query: str) -> list[dict]:
        time.sleep(_QUERYDUUR)
        return [{"metric": {}, "value": [0, "1"]}]

    connector.prom.custom_query = traag

    ticks = 0
    bezig = True

    async def meeloper() -> None:
        nonlocal ticks
        while bezig:
            await asyncio.sleep(_TICK)
            ticks += 1

    taak = asyncio.create_task(meeloper())
    resultaat = await connector.custom_query("up")
    bezig = False
    await taak

    assert resultaat == [{"metric": {}, "value": [0, "1"]}]
    assert ticks > 1, (
        f"de event loop stond stil tijdens de query: {ticks} ticks in {_QUERYDUUR}s. "
        "Wordt de synchrone client weer rechtstreeks aangeroepen in plaats van via "
        "asyncio.to_thread?"
    )


@pytest.mark.asyncio
async def test_een_fout_uit_de_thread_komt_gewoon_terug(connector: PrometheusConnector) -> None:
    """De verplaatsing naar een thread mag de foutafhandeling niet wegnemen.

    Het blok op /admin/diensten leunt erop: het vangt de fout, zet ``gemeten=False`` en zegt
    op het scherm dat er niet gemeten kon worden. Verdwijnt de fout in de thread, dan leest
    een mislukte meting weer als "niets te melden".
    """
    from opi.connectors.prometheus import PrometheusQueryError

    def stuk(query: str) -> list[dict]:
        raise ConnectionError("naam niet op te lossen")

    connector.prom.custom_query = stuk

    with pytest.raises(PrometheusQueryError):
        await connector.custom_query("up")
