"""De watchdog maakt een stilstand van de event loop zichtbaar en wijst de regel aan.

WAAROM DIT EEN EIGEN TEST HEEFT

Een stilstand was in productie alleen te zien aan de AFWEZIGHEID van logregels: geen
metriek, geen waarschuwing, en de health-probes merken het bewust niet omdat die op een
eigen draad draaien. Deze test blokkeert de loop echt met ``time.sleep`` en kijkt wat de
watchdog ervan maakt.

Elke toets meet een VERSCHIL, nooit een absolute waarde: de metrieken lopen alleen op en
worden door het lezen niet leeggemaakt, want /metrics is voor iedereen leesbaar.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import pytest
from opi.core.config import settings
from opi.core.loop_watchdog import (
    _HARTSLAG,
    aantal_stilstanden,
    huidige_lag,
    start_loop_watchdog,
    stop_loop_watchdog,
    totale_lag,
)


def _verwachte_lag(blokkade: float) -> float:
    """Een gat van een hartslag is normaal; dat trekt de watchdog eraf."""
    return blokkade - _HARTSLAG


#: Ruim boven de drempel van 1 s, zodat de bewaker hem zeker ziet.
_LANGE_STILSTAND = 1.5
#: Ruim eronder.
_KORTE_STILSTAND = 0.3


def _lag_uit_de_collector() -> float:
    """Wat een lezer van /metrics op dit moment als totale lag te zien krijgt."""
    from opi.core.metrics import OPICollector

    families = {familie.name: familie for familie in OPICollector().collect()}
    return families["opi_event_loop_lag_seconds"].samples[0].value


@pytest.fixture
async def watchdog():
    """Start de watchdog en zet hem daarna weer uit, ook bij een fout."""
    start_loop_watchdog()
    # De hartslag moet eerst een keer gelopen hebben voordat er iets te meten valt.
    await asyncio.sleep(0.2)
    yield
    stop_loop_watchdog()


async def test_een_lange_stilstand_geeft_een_waarschuwing_met_de_stack(watchdog, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="opi.core.loop_watchdog"):
        time.sleep(_LANGE_STILSTAND)
        await asyncio.sleep(0.3)

    waarschuwingen = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(waarschuwingen) == 1, f"verwacht precies een waarschuwing per stilstand, kreeg er {len(waarschuwingen)}"
    tekst = waarschuwingen[0].getMessage()
    assert "staat" in tekst
    assert "test_een_lange_stilstand_geeft_een_waarschuwing_met_de_stack" in tekst, (
        f"de stack van de hoofdthread wijst de blokkerende regel niet aan:\n{tekst}"
    )


async def test_de_counter_loopt_op_met_de_stilstand(watchdog) -> None:
    voor = totale_lag()

    time.sleep(_LANGE_STILSTAND)
    await asyncio.sleep(0.3)

    gemeten = totale_lag() - voor
    assert gemeten >= _verwachte_lag(_LANGE_STILSTAND), f"een blokkade van {_LANGE_STILSTAND}s gaf {gemeten:.3f}s"


async def test_twee_lezers_zien_dezelfde_stilstand(watchdog) -> None:
    """De reden dat dit geen maximum-sinds-de-vorige-scrape meer is.

    /metrics staat in ``SKIP_AUTH_EXACT`` en poort 8000 is open, dus een willekeurige
    lezer (een scanner, een curl) mag de piek niet wegnemen voordat Prometheus scrapet.
    """
    voor = _lag_uit_de_collector()

    time.sleep(_LANGE_STILSTAND)
    await asyncio.sleep(0.3)

    eerste = _lag_uit_de_collector()
    tweede = _lag_uit_de_collector()

    drempel = _verwachte_lag(_LANGE_STILSTAND)
    assert eerste - voor >= drempel, f"de eerste lezer zag {eerste - voor:.3f}s"
    assert tweede - voor >= drempel, f"de tweede lezer zag {tweede - voor:.3f}s, de eerste lezing nam hem weg"
    assert tweede >= eerste, "de counter liep terug"


async def test_de_teller_van_stilstanden_loopt_een_op(watchdog) -> None:
    voor = aantal_stilstanden()

    time.sleep(_LANGE_STILSTAND)
    await asyncio.sleep(0.3)

    assert aantal_stilstanden() - voor == 1


async def test_een_korte_stilstand_geeft_geen_waarschuwing(watchdog, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="opi.core.loop_watchdog"):
        time.sleep(_KORTE_STILSTAND)
        await asyncio.sleep(0.3)

    assert _KORTE_STILSTAND < settings.EVENT_LOOP_LAG_WARNING_SECONDS
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


async def test_een_korte_stilstand_staat_wel_in_de_counter(watchdog) -> None:
    """Onder de drempel geen log, maar de counter telt hem wel: daar zie je de trend."""
    voor = totale_lag()

    time.sleep(_KORTE_STILSTAND)
    await asyncio.sleep(0.3)

    assert totale_lag() - voor >= _verwachte_lag(_KORTE_STILSTAND)


async def test_de_huidige_lag_toont_een_lopende_stilstand(watchdog) -> None:
    """De counter krijgt een stilstand pas bij de volgende hartslag; deze gauge nu al.

    Daarom leest een draad hem midden in de blokkade: de loop zelf kan dat op dat moment
    niet, en dat is precies de situatie waarin een scrape nog moet kunnen zien hoe erg het is.
    """
    assert huidige_lag() < settings.EVENT_LOOP_LAG_WARNING_SECONDS
    meting: list[float] = []

    def _lees_halverwege() -> None:
        time.sleep(_LANGE_STILSTAND / 2)
        meting.append(huidige_lag())

    lezer = threading.Thread(target=_lees_halverwege)
    lezer.start()
    time.sleep(_LANGE_STILSTAND)
    lezer.join(timeout=1.0)
    await asyncio.sleep(0.3)

    # De hartslag kan tot een slag voor de blokkade geweest zijn, dus twee slagen marge.
    ondergrens = _LANGE_STILSTAND / 2 - 2 * _HARTSLAG
    assert meting, "de draad kwam niet aan zijn meting toe"
    assert meting[0] >= ondergrens, f"tijdens een lopende blokkade gaf de gauge {meting[0]:.3f}s"


def test_de_collector_geeft_de_lag_als_prometheus_metrieken_uit(monkeypatch: pytest.MonkeyPatch) -> None:
    """``OPICollector`` is wat ``/metrics`` werkelijk scraped; dit legt de koppeling vast.

    De functies zelf zijn hierboven al getoetst. Zonder deze test zou een hernoemde of
    weggehaalde metriek hier niet opvallen: ze zouden stil verdwijnen terwijl de watchdog
    prima blijft tellen.
    """
    from opi.core.metrics import OPICollector

    monkeypatch.setattr("opi.core.loop_watchdog.totale_lag", lambda: 12.5)
    monkeypatch.setattr("opi.core.loop_watchdog.huidige_lag", lambda: 1.23)
    monkeypatch.setattr("opi.core.loop_watchdog.aantal_stilstanden", lambda: 3)

    families = {familie.name: familie for familie in OPICollector().collect()}
    waarden = {sample.name: sample.value for familie in families.values() for sample in familie.samples}

    assert families["opi_event_loop_lag_seconds"].type == "counter"
    assert waarden["opi_event_loop_lag_seconds_total"] == 12.5
    assert waarden["opi_event_loop_lag_current_seconds"] == 1.23
    assert waarden["opi_event_loop_stalls_total"] == 3


async def test_na_het_stoppen_meldt_de_gauge_geen_stilstand() -> None:
    """Zonder het wissen van de tijdstempel groeit ``huidige_lag`` door na het stoppen.

    De hartslag loopt dan niet meer, dus een scrape tijdens het afsluiten zou een
    stilstand melden die er niet is.
    """
    start_loop_watchdog()
    await asyncio.sleep(0.2)
    stop_loop_watchdog()

    time.sleep(0.5)

    assert huidige_lag() == 0.0
