"""Het dashboard toont de metingen die het ophaalt, en zwijgt niet als dat misgaat.

Gemeten op productie, 17 augustus 2026: het dashboard meldde "geen netwerkverkeer" terwijl
Mimir er wel degelijk data voor had (rig-prd-regel-k4c 2,1 MB/s, rig-prd-wies 16 kB/s). De
oorzaak was geen metriek, geen query en geen connector, maar een ontbrekende import:

    from datetime import UTC          # de klasse datetime zelf stond er niet bij
    ...
    now = datetime.now(UTC)           # NameError: name 'datetime' is not defined

En dat bleef onzichtbaar doordat de except eromheen op DEBUG logde. Een lege grafiek en een
stille NameError zien er voor een gebruiker identiek uit: "er is geen verkeer".

Deze tests dekken allebei die kanten af: de gegevens komen door, en een mislukking is te
horen.
"""

from types import SimpleNamespace
from typing import Any

import pytest
from opi.web.router import collect_dashboard_metrics


class _Connector:
    """Een connector die meet wat er gevraagd wordt en vaste reeksen teruggeeft."""

    is_connected = True

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def custom_query(self, query: str) -> list[dict[str, Any]]:
        self.queries.append(query)
        return [{"value": [0, "42"]}]

    async def query_range(self, query: str, start_time: Any, end_time: Any, step: str) -> list[dict[str, Any]]:
        self.queries.append(query)
        return [{"values": [[1755400000, "2048"], [1755400300, "4096"]]}]


@pytest.fixture
def connector(monkeypatch: pytest.MonkeyPatch) -> _Connector:
    verbinding = _Connector()

    async def _geef(*_args: Any, **_kwargs: Any) -> _Connector:
        return verbinding

    monkeypatch.setattr("opi.connectors.prometheus.get_metrics_connector", _geef)
    return verbinding


@pytest.mark.asyncio
async def test_het_netwerkverkeer_komt_in_de_grafiek(connector: _Connector) -> None:
    """De reeks van de connector belandt in network_in_data/network_out_data.

    Dit is de test die omvalt op de ontbrekende import: zonder `datetime` gooit het blok
    een NameError, vangt de except hem op, en blijven deze lijsten leeg -- precies wat er
    op productie te zien was.
    """
    metrics, _, _ = await collect_dashboard_metrics(["rig-prd-wies"], [])

    assert metrics["network_in_data"], "network_in_data is leeg terwijl de connector data gaf"
    assert metrics["network_out_data"], "network_out_data is leeg terwijl de connector data gaf"
    # 2048 bytes -> 2,0 KB; de weergave rekent om naar kilobytes.
    assert metrics["network_in_data"][0]["v"] == 2.0
    assert "t" in metrics["network_in_data"][0], "een punt zonder tijdstip is niet te tekenen"


@pytest.mark.asyncio
async def test_de_namespaces_gaan_mee_in_de_query(connector: _Connector) -> None:
    """Zonder namespacefilter zou het dashboard het verkeer van het hele cluster tonen."""
    await collect_dashboard_metrics(["rig-prd-wies", "rig-prd-algor-1ha"], [])

    netwerk = [q for q in connector.queries if "container_network_receive_bytes_total" in q]
    assert netwerk, "er is helemaal geen netwerkquery gedaan"
    assert "rig-prd-wies" in netwerk[0]
    assert "rig-prd-algor-1ha" in netwerk[0]


@pytest.mark.asyncio
async def test_zonder_namespaces_wordt_er_niets_gevraagd(connector: _Connector) -> None:
    """Een lege regex zou als namespace=~"" matchen op niets, of erger, op alles."""
    await collect_dashboard_metrics([], [])

    assert connector.queries == []


@pytest.mark.asyncio
async def test_een_mislukte_meting_is_te_horen(monkeypatch: pytest.MonkeyPatch, caplog: Any) -> None:
    """Op WARNING en niet op DEBUG.

    Hier stond DEBUG, en daardoor zag niemand de NameError: het dashboard toonde gewoon
    nul verkeer. Een meting die niet lukt hoort in de log te staan van een omgeving die
    niet op debugniveau draait.
    """
    import logging

    class _Stuk:
        is_connected = True

        async def custom_query(self, query: str) -> list[dict[str, Any]]:
            raise RuntimeError("prometheus onbereikbaar")

        async def query_range(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
            raise RuntimeError("prometheus onbereikbaar")

    async def _geef(*_args: Any, **_kwargs: Any) -> Any:
        return _Stuk()

    monkeypatch.setattr("opi.connectors.prometheus.get_metrics_connector", _geef)

    with caplog.at_level(logging.WARNING, logger="opi.web.router"):
        await collect_dashboard_metrics(["rig-prd-wies"], [])

    gemeld = [r for r in caplog.records if "Dashboard" in r.message and "failed" in r.message]
    assert gemeld, "een mislukte dashboardmeting werd niet op WARNING gemeld"


@pytest.mark.asyncio
async def test_de_pagina_blijft_overeind_als_de_meting_faalt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Geen metingen is vervelend; een dashboard dat 500 geeft is erger."""

    class _Stuk:
        is_connected = True

        async def custom_query(self, query: str) -> list[dict[str, Any]]:
            raise RuntimeError("stuk")

        async def query_range(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
            raise RuntimeError("stuk")

    async def _geef(*_args: Any, **_kwargs: Any) -> Any:
        return _Stuk()

    monkeypatch.setattr("opi.connectors.prometheus.get_metrics_connector", _geef)

    metrics, _, _ = await collect_dashboard_metrics(["rig-prd-wies"], [])

    assert metrics["network_in_data"] == []
    assert isinstance(metrics, dict)


def test_de_module_importeert_datetime_op_moduleniveau() -> None:
    """De directe vangrail voor de fout zelf, los van welke functie hem gebruikt."""
    import opi.web.router as router

    assert hasattr(router, "datetime"), "opi.web.router importeert de klasse datetime niet"
    assert router.datetime.__name__ == "datetime"
    _ = SimpleNamespace  # gebruikt in de typehints hierboven
