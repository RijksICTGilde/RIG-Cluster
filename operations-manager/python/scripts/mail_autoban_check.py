#!/usr/bin/env python3
"""Toets dat allowed-ip voorkomt dat internal IPs een auto-ban triggeren.

Breekt 40 verzoeken af en eist dat /healthz/live daarna nog 200 geeft; Stalwart bant IP's
na 30 per dag, dus zonder allowed-ip voor interne ranges faalt deze test. Gemeten in RC-261
toen kubelet-probes op 13 oktober 2026 productie-relay's auto-ban triggerde.

Draaien tegen de sandbox:
    kubectl -n rig-ron port-forward svc/rig-mail-relay 18080:8080 &
    cd operations-manager/python
    uv run python scripts/mail_autoban_check.py --port 18080

Noot: allowed-ip voorkomt NIEUWE bans maar heft bestaande niet op (de ban staat in de
database). Herstel is een DELETE-commando plus herstart van de pod.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time

import requests

#: Meer dan de 30 van ``server.auto-ban.scan.rate``, zodat de drempel echt geraakt wordt.
AANTAL_AFGEBROKEN = 40

#: Een verzoek ZONDER de afsluitende lege regel: de server wacht op de rest van de
#: kopregels, en het sluiten van de socket is dan precies "closed before message completed".
HALF_VERZOEK = b"GET /healthz/live HTTP/1.1\r\nHost: rig-mail-relay\r\n"


def _breek_af(host: str, poort: int) -> str | None:
    """Een verbinding openen, half een verzoek sturen en sluiten. Geeft de fout, of None."""
    try:
        with socket.create_connection((host, poort), timeout=5) as s:
            s.sendall(HALF_VERZOEK)
            # SO_LINGER met tijd 0 geeft een RST in plaats van een nette FIN. Dat is wat een
            # probe doet die op zijn timeoutSeconds afbreekt, en het is de vorm die de relay
            # als scan telde.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
    except OSError as fout:
        return str(fout)
    return None


def _leeft(host: str, poort: int) -> tuple[bool, str]:
    """Of ``/healthz/live`` 200 geeft, met de reden als dat niet zo is."""
    try:
        antwoord = requests.get(f"http://{host}:{poort}/healthz/live", timeout=10)
    except requests.RequestException as fout:
        return False, str(fout)
    if antwoord.status_code != 200:
        return False, f"status {antwoord.status_code}"
    return True, "200"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080, help="de management-poort van de relay")
    parser.add_argument("--aantal", type=int, default=AANTAL_AFGEBROKEN)
    args = parser.parse_args()

    # De nulmeting, want anders leest een relay die al plat lag als een nieuwe ban.
    leeft, reden = _leeft(args.host, args.port)
    if not leeft:
        print(f"FOUT: /healthz/live gaf vooraf al geen 200 ({reden}); eerst de relay repareren")
        return 2
    print(f"vooraf: /healthz/live {reden}")

    for nummer in range(args.aantal):
        fout = _breek_af(args.host, args.port)
        if fout is not None:
            # Een geweigerde verbinding is zelf al de uitkomst: de relay nam ons niet meer aan.
            print(f"\nNIET GOED: verbinding {nummer + 1} van {args.aantal} kwam niet tot stand: {fout}")
            return 1
    print(f"{args.aantal} verzoeken afgebroken voordat ze compleet waren")

    # De ban valt in de accept-lus, dus hij geldt bij de VOLGENDE verbinding. Even wachten
    # zodat een trage schrijfactie naar de configopslag meegenomen is.
    time.sleep(2)

    leeft, reden = _leeft(args.host, args.port)
    if not leeft:
        print(f"\nNIET GOED: /healthz/live geeft na {args.aantal} afgebroken verzoeken geen 200 ({reden}).")
        print("De relay heeft het adres verbannen. Staat [server.allowed-ip] in zijn config.toml?")
        print("Een bestaande ban gaat eruit met het DELETE-commando uit features/send-email.md (Storingen).")
        return 1

    print("\nDe relay verbant een afgebroken verbinding van een intern adres niet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
