"""Maakt zichtbaar wanneer de asyncio event loop stilstaat, en waardoor.

WAAROM DIT BESTAAT

FastAPI, de TaskWorker en alle schedulers delen een loop. Een synchrone blokkade raakt dus
webverzoeken, alle lopende taken en de schedulers tegelijk, maar er was nergens een signaal
van. De health-probes zien het bewust niet: die draaien op een eigen draad
(``opi/core/probe_server.py``). Op 15 oktober 2026 stond de loop in productie 14 s stil,
liep een ArgoCD-poll van een andere taak op zijn timeout af en faalde die taak, en het
enige spoor was de afwezigheid van logregels.

HOE HET MEET

Een hartslagtaak op de loop zet elke ``_HARTSLAG`` seconden een tijdstempel. Wat er boven
``_HARTSLAG`` overheen zit is de tijd dat de loop niet aan de taak toekwam, en de taak
weet dat precies zodra hij weer aan de beurt komt. Die seconden gaan op een teller die
alleen oploopt; ``huidige_lag`` vult aan wat een nog lopende stilstand nu al kost.

De waarschuwing komt van een daemon-draad die dezelfde tijdstempel leest. Die MOET een
draad zijn: tijdens de stilstand is er geen taak die iets kan melden, en juist dan is de
stack van de hoofdthread het antwoord op de vraag welke regel de loop vasthoudt.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback

from opi.core.config import settings

logger = logging.getLogger(__name__)

#: Hoe vaak de hartslagtaak de klok zet, en hoe vaak de draad kijkt. Honderd keer per
#: seconde een float schrijven kost niets en maakt een stilstand van 200 ms al zichtbaar.
_HARTSLAG = 0.1

_hartslag_taak: asyncio.Task[None] | None = None
_draad: threading.Thread | None = None
_stoppen = threading.Event()

#: Laatste tijdstempel van de hartslagtaak, en de hoofdthread waarvan de stack ertoe doet.
#: Beide worden door een draad gelezen; een float en een int lezen is atomair genoeg, een
#: lock zou hier alleen de meting zelf kunnen laten wachten.
_laatste_hartslag: float = 0.0
_hoofdthread_id: int = 0

#: Cumulative achterstand en stilstandenteller. Zie metrics.py over waarom /metrics
#: een counter nodig heeft zonder reset-bij-lezen.
_totale_lag: float = 0.0
_stilstanden: int = 0
_slot = threading.Lock()


def _tel_lag_op(lag: float) -> None:
    global _totale_lag
    if lag <= 0:
        return
    with _slot:
        _totale_lag += lag


def totale_lag() -> float:
    """Alle achterstand die de loop sinds het starten heeft opgelopen, in seconden."""
    with _slot:
        return _totale_lag


def aantal_stilstanden() -> int:
    """Hoe vaak de achterstand boven ``EVENT_LOOP_LAG_WARNING_SECONDS`` uitkwam."""
    with _slot:
        return _stilstanden


def huidige_lag() -> float:
    """De achterstand op dit moment: hoe lang de hartslag al niet aan de beurt is geweest.

    Een stilstand die tijdens de scrape nog loopt zit nog niet in ``totale_lag``, want de
    hartslag telt hem pas op zodra hij weer draait. Hier is hij wel al te zien.
    """
    if not _laatste_hartslag:
        return 0.0
    return max(0.0, time.monotonic() - _laatste_hartslag - _HARTSLAG)


async def _hartslag() -> None:
    global _laatste_hartslag
    while True:
        nu = time.monotonic()
        # De taak zelf meet het gat exact: de bewaker ziet alleen wat er op zijn
        # bemonsteringsmoment al verstreken was, en mist dus tot een hartslag van de staart.
        _tel_lag_op(nu - _laatste_hartslag - _HARTSLAG)
        _laatste_hartslag = nu
        await asyncio.sleep(_HARTSLAG)


def _bewaak() -> None:
    global _stilstanden
    gemeld = False
    while not _stoppen.wait(_HARTSLAG):
        lag = huidige_lag()
        if lag < settings.EVENT_LOOP_LAG_WARNING_SECONDS:
            gemeld = False
            continue
        if gemeld:
            # Eenmaal per stilstand, niet elke 100 ms opnieuw: de stack verandert niet
            # zolang dezelfde regel de loop vasthoudt.
            continue
        gemeld = True
        with _slot:
            _stilstanden += 1
        logger.warning(
            "De event loop staat %.1fs stil. Stack van de hoofdthread:\n%s", lag, _stack_van_de_hoofdthread()
        )


def _stack_van_de_hoofdthread() -> str:
    frame = sys._current_frames().get(_hoofdthread_id)
    if frame is None:
        return "  (hoofdthread niet gevonden)"
    return "".join(traceback.format_stack(frame))


def start_loop_watchdog() -> None:
    """Start de hartslag en de bewaker. Veilig om vaker aan te roepen."""
    global _hartslag_taak, _draad, _hoofdthread_id, _laatste_hartslag

    if _hartslag_taak is None or _hartslag_taak.done():
        _laatste_hartslag = time.monotonic()
        _hartslag_taak = asyncio.create_task(_hartslag())

    if _draad is None or not _draad.is_alive():
        _hoofdthread_id = threading.main_thread().ident or 0
        _stoppen.clear()
        _draad = threading.Thread(target=_bewaak, name="loop-watchdog", daemon=True)
        _draad.start()
        logger.info(
            "Event-loop watchdog gestart (hartslag %.1fs, waarschuwing vanaf %.1fs)",
            _HARTSLAG,
            settings.EVENT_LOOP_LAG_WARNING_SECONDS,
        )


def stop_loop_watchdog() -> None:
    """Stop de hartslag en de bewaker."""
    global _hartslag_taak, _draad, _laatste_hartslag

    _stoppen.set()
    if _hartslag_taak is not None and not _hartslag_taak.done():
        _hartslag_taak.cancel()
    _hartslag_taak = None
    # Zonder dit blijft de laatste tijdstempel staan en groeit huidige_lag door: een scrape
    # tijdens het afsluiten zou een stilstand melden die er niet is.
    _laatste_hartslag = 0.0
    if _draad is not None:
        _draad.join(timeout=1.0)
    _draad = None
