"""De health-check antwoordt ALTIJD, ook als de applicatie het te druk heeft.

DE EIS

Niet "meestal" en niet "snel genoeg", maar altijd. Een health-check die wegvalt onder
belasting haalt de pod uit de service-endpoints, en dan krijgt elke lopende aanroep een 503
van de ingress: ook aanroepen die niets met de drukte te maken hebben. Voor een client is
dat niet te onderscheiden van een echte storing.

WAAROM DIT TE TESTEN IS EN NIET ALLEEN TE BEWEREN

De handlers zaten in FastAPI en dus op de asyncio-eventloop. Ze deden zelf niets zwaars,
maar dat helpt niet als de loop vol staat: dan komen ze niet aan de beurt. In de
reallife-doorloop van 14 augustus 2026 duurden opslagacties tot 9,2 seconden en liep de
probe in zijn timeout van 5.

De test hieronder blokkeert de eventloop expres langer dan die timeout en vraagt ondertussen
de probe op. Slaagt die, dan is de garantie hard: het antwoord komt van een eigen draad, en
die wordt door het besturingssysteem bediend ongeacht wat asyncio doet.
"""

from __future__ import annotations

import asyncio
import threading
import time
import urllib.error
import urllib.request

import pytest
from opi.core.probe_server import start_probe_server, stop_probe_server

#: Een eigen poort per test, zodat een achtergebleven server uit een andere test niet
#: stilzwijgend het antwoord geeft.
POORT_ALTIJD = 8231
POORT_PADEN = 8232

#: Langer dan de timeout van de probe in de deployment (5 seconden), zodat dit werkelijk
#: het geval nabootst waarin het misging.
BLOKKADE_SECONDEN = 7.0


@pytest.fixture
def probeserver():
    def _start(poort: int):
        start_probe_server(poort)
        return poort

    yield _start
    stop_probe_server()


def _haal(poort: int, pad: str, timeout: float = 5.0) -> tuple[int, float]:
    """Vraag de probe op; geef de statuscode en hoe lang het duurde."""
    start = time.monotonic()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{poort}{pad}", timeout=timeout) as antwoord:
            return antwoord.status, time.monotonic() - start
    except urllib.error.HTTPError as fout:
        return fout.code, time.monotonic() - start


def test_de_probe_antwoordt_terwijl_de_eventloop_vaststaat(probeserver) -> None:
    """De kern van deze module. Blokkeert de loop langer dan de probetimeout."""
    poort = probeserver(POORT_ALTIJD)
    gemeten: list[tuple[int, float]] = []

    def vraag_op() -> None:
        # Even wachten zodat de blokkade zeker begonnen is.
        time.sleep(0.5)
        gemeten.append(_haal(poort, "/healthz"))

    async def hoofd() -> None:
        draad = threading.Thread(target=vraag_op)
        draad.start()
        # SYNCHROON slapen in de coroutine: precies wat blokkerend werk in de loop doet.
        time.sleep(BLOKKADE_SECONDEN)
        draad.join()

    asyncio.run(hoofd())

    assert gemeten, "de probe is niet opgevraagd"
    code, duur = gemeten[0]
    assert code == 200, f"de probe gaf {code} terwijl de eventloop vaststond"
    assert duur < 1.0, f"de probe deed er {duur:.2f}s over; hij hoort niet op de loop te wachten"


def test_de_paden_die_kubernetes_gebruikt(probeserver) -> None:
    """healthz en readyz bestaan, en de rest niet.

    readyz geeft hier 503: in een kale test draait geen enkele dienst, en dan is "niet
    gereed" het juiste antwoord. Dat hij ANTWOORDT is wat deze test vastlegt.
    """
    poort = probeserver(POORT_PADEN)

    assert _haal(poort, "/healthz")[0] == 200
    assert _haal(poort, "/health")[0] == 200
    assert _haal(poort, "/readyz")[0] in (200, 503)
    assert _haal(poort, "/iets-anders")[0] == 404


def test_twee_keer_starten_is_geen_fout(probeserver) -> None:
    """De lifespan kan bij een herstart opnieuw langskomen; dat mag niet omvallen."""
    poort = probeserver(POORT_PADEN + 10)
    start_probe_server(poort)

    assert _haal(poort, "/healthz")[0] == 200
