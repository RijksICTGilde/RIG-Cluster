"""De health-checks, op een eigen draad zodat ze altijd antwoorden.

WAAROM DIT NAAST DE APPLICATIE STAAT

``/healthz`` en ``/readyz`` zaten in FastAPI, dus op de asyncio-eventloop. Die handlers
doen zelf niets zwaars (ze lezen een vlag uit het geheugen), maar dat helpt niet als de
loop vol staat: dan komt de handler simpelweg niet aan de beurt. Gemeten in de
reallife-doorloop van 14 augustus 2026: onder gelijktijdige belasting duurden opslagacties
tot 9,2 seconden, de probe liep in zijn timeout van 5 seconden, en de kubelet haalde de pod
uit de service-endpoints. Elke lopende API-aanroep kreeg daarna een 503 van nginx, ook
aanroepen die niets met de drukte te maken hadden.

De eis is niet "sneller" maar "altijd": de health-check van ZAD hoort te antwoorden wat de
belasting ook is. Dat kan alleen als hij niet op dezelfde loop wacht. Vandaar een eigen
threading-HTTP-server op een eigen poort. Een besturingssysteemdraad wordt door de
scheduler bediend ongeacht wat asyncio doet, dus deze server antwoordt ook terwijl de
applicatie het te druk heeft om iets anders te doen.

WAT HIJ WEL EN NIET DOET

Hij leest dezelfde ``get_readiness_state()`` als de oude handlers, dus het antwoord is
inhoudelijk gelijk. Hij doet geen enkele I/O, geen database, geen netwerk: alleen een dict
uit het geheugen. Daarmee is er niets in deze server dat zelf kan blijven hangen.

De endpoints in FastAPI BLIJVEN bestaan. Wie ze met de hand opvraagt of via de ingress
binnenkomt, krijgt hetzelfde antwoord; alleen de kubelet-probes wijzen naar deze poort. Zo
verandert er niets voor een bestaande monitor die op poort 8000 kijkt.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger(__name__)

#: De poort waarop de probes luisteren. Bewust een andere dan de applicatiepoort: de kubelet
#: moet erbij kunnen ook als de applicatiepoort niets meer uitdeelt.
PROBE_PORT = 8001

_server: ThreadingHTTPServer | None = None
_thread: threading.Thread | None = None


class _ProbeHandler(BaseHTTPRequestHandler):
    """Beantwoordt /healthz en /readyz, en verder niets."""

    # Geen toegangslog: bij een probe elke tien seconden is dat alleen ruis, en het schrijven
    # ervan is de enige I/O die deze draad anders zou doen.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def _antwoord(self, code: int, body: dict[str, Any]) -> None:
        rauw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(rauw)))
        self.end_headers()
        self.wfile.write(rauw)

    def do_GET(self) -> None:
        pad = self.path.split("?", 1)[0]

        if pad in ("/health", "/healthz"):
            # Liveness: draait het proces nog. Antwoordt altijd ok, net als de handler in
            # FastAPI deed; dat een aparte draad hier antwoordt is precies de bedoeling.
            self._antwoord(200, {"status": "ok"})
            return

        if pad == "/readyz":
            # Import hier en niet bovenaan: deze module wordt vroeg geladen en readiness
            # hangt aan de applicatiestatus.
            from opi.core.readiness import get_readiness_state

            readiness = get_readiness_state()
            if readiness.is_ready:
                self._antwoord(200, {"status": "ok"})
            else:
                self._antwoord(503, {"status": "unavailable", "services": readiness.summary()})
            return

        self._antwoord(404, {"status": "not found"})


def start_probe_server(port: int = PROBE_PORT) -> None:
    """Start de probeserver op een eigen draad. Idempotent."""
    global _server, _thread

    if _server is not None:
        return

    try:
        _server = ThreadingHTTPServer(("0.0.0.0", port), _ProbeHandler)
    except OSError as fout:
        # Niet fataal: de endpoints in FastAPI bestaan nog, dus een pod zonder deze server
        # is bruikbaar. Wel luid loggen, want de garantie "altijd antwoord" is dan weg.
        logger.error("Probeserver kon niet starten op poort %s: %s", port, fout)
        return

    _thread = threading.Thread(target=_server.serve_forever, name="probe-server", daemon=True)
    _thread.start()
    logger.info("Probeserver luistert op poort %s (/healthz, /readyz)", port)


def stop_probe_server() -> None:
    """Stop de probeserver, als hij draait."""
    global _server, _thread

    if _server is None:
        return
    _server.shutdown()
    _server.server_close()
    _server = None
    _thread = None
    logger.info("Probeserver gestopt")
