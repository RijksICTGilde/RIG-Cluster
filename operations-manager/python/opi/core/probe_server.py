"""The health checks, on a thread of their own so they always answer.

A handler on the asyncio event loop does not get its turn while the loop is full, however
little it does itself, and a probe that then runs into its timeout takes the pod out of the
service endpoints. An operating-system thread is served regardless of what asyncio does, so
this server answers while the application is too busy to do anything else.

It reads the same ``get_readiness_state()`` as the FastAPI handlers, so the answer is the
same. It does no I/O at all, no database, no network: only a dict out of memory, so there is
nothing here that can hang by itself.

The endpoints in FastAPI stay, and whoever asks for them by hand or through the ingress gets
the same answer; only the kubelet probes point at this port.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger(__name__)

#: The port the probes listen on. Deliberately not the application port: the kubelet has to
#: reach it even when the application port hands nothing out any more.
PROBE_PORT = 8001

_server: ThreadingHTTPServer | None = None
_thread: threading.Thread | None = None


class _ProbeHandler(BaseHTTPRequestHandler):
    """Answers /healthz and /readyz, and nothing else."""

    # No access log: with a probe every ten seconds that is noise only, and writing it is the
    # only I/O this thread would otherwise do.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002  # base class signature
        pass

    def _respond(self, code: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]

        if path in ("/health", "/healthz"):
            # Liveness: is the process still running. Always answers ok, just as the handler
            # in FastAPI did; that a separate thread answers here is exactly the point.
            self._respond(200, {"status": "ok"})
            return

        if path == "/readyz":
            # Imported here and not at the top: this module is loaded early and readiness
            # hangs off the application state.
            from opi.core.readiness import get_readiness_state

            readiness = get_readiness_state()
            if readiness.is_ready:
                self._respond(200, {"status": "ok"})
            else:
                self._respond(503, {"status": "unavailable", "services": readiness.summary()})
            return

        self._respond(404, {"status": "not found"})


def start_probe_server(port: int = PROBE_PORT) -> None:
    """Start the probe server on a thread of its own. Idempotent."""
    global _server, _thread

    if _server is not None:
        return

    try:
        _server = ThreadingHTTPServer(("0.0.0.0", port), _ProbeHandler)
    except OSError as error:
        # Not fatal: the endpoints in FastAPI still exist, so a pod without this server is
        # usable. Logged loudly, because the "always answers" guarantee is then gone.
        logger.error("Probeserver kon niet starten op poort %s: %s", port, error)
        return

    _thread = threading.Thread(target=_server.serve_forever, name="probe-server", daemon=True)
    _thread.start()
    logger.info("Probeserver luistert op poort %s (/healthz, /readyz)", port)


def stop_probe_server() -> None:
    """Stop the probe server, when it is running."""
    global _server, _thread

    if _server is None:
        return
    _server.shutdown()
    _server.server_close()
    _server = None
    _thread = None
    logger.info("Probeserver gestopt")
