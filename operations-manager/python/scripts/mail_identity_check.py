#!/usr/bin/env python3
"""RC-114: toetst de identiteitsregels van de mailrelay tegen een draaiende sandbox.

Waarom dit bestaat: de regels waarop het hele send-email-ontwerp rust zijn regels in een
sieve-script, en een sieve-script dat stilletjes niets doet ziet er precies zo uit als een
sieve-script dat werkt. De rest van de relayconfiguratie is destijds met de hand nagespeeld
tegen een draaiende Stalwart; het OVERSCHRIJVEN van de From: (identiteitsregel 2 in zijn
huidige vorm) niet. Dit script maakt daar een assertie van.

Het toetst vier dingen, en alle vier zijn ze eerder een keer stuk geweest of ongemeten:

1. De From: wordt overschreven met het vaste adres, MET behoud van de weergavenaam.
2. Ook zonder weergavenaam, en ook bij een kaal adres zonder punthaken.
3. De envelope draagt het account in het plusdeel en blijft in hetzelfde domein. Dit is de
   belangrijkste: `rijksoverheid.nl` publiceert p=reject en wij ondertekenen niet met DKIM,
   dus SPF-uitlijning tussen envelope en From: is het ENIGE dat een bericht door DMARC
   krijgt. Breekt deze regel, dan weigert elke ontvanger buiten de Rijksoverheid alles.
4. De Received-keten en de verklikkerheaders zijn eraf.

Draaien tegen de sandbox, met twee port-forwards open:

    kubectl -n rig-ron port-forward svc/rig-mail-relay 1587:587 &
    kubectl -n rig-ron port-forward svc/rig-mail-sink 8025:8025 &
    cd operations-manager/python
    uv run python scripts/mail_identity_check.py --user <account> --password <geheim>

De inloggegevens zijn die van een send-email-account; die van ZAD zelf staan in de Secret
uit MAIL_PLATFORM_SECRET_NAME in de namespace van OPI.
"""

from __future__ import annotations

import argparse
import smtplib
import sys
import time
from email.message import EmailMessage

import requests

VAST_ADRES = "noreply-rijksapp@rijksoverheid.nl"

#: Headers die er onder geen beding uit mogen komen. De Received-keten draagt pod-IP's en
#: namespace-namen, de rest draagt het account en de client.
VERBODEN_HEADERS = (
    "Received",
    "X-Originating-IP",
    "X-Authenticated-Sender",
    "X-Mailer",
    "X-Originating-Client",
)


def _stuur(host: str, poort: int, user: str, password: str, from_header: str, onderwerp: str) -> None:
    """Biedt een bericht aan met een expres afwijkende From:."""
    bericht = EmailMessage()
    bericht["Subject"] = onderwerp
    bericht["To"] = "ontvanger@example.org"
    # Precies het punt van de toets: dit adres MOET verdwijnen.
    bericht["From"] = from_header
    bericht["X-Mailer"] = "identiteitstoets"
    bericht["X-Originating-IP"] = "10.42.0.99"
    bericht.set_content("Toets van de identiteitsregels.")

    with smtplib.SMTP(host, poort, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        # De envelope die de applicatie AANBIEDT is expres fout; de relay hoort hem weg te
        # gooien en zijn eigen adres ervoor in de plaats te zetten.
        smtp.send_message(bericht, from_addr="wat-de-app-ook-zegt@elders.example")


def _haal_bericht(api: str, onderwerp: str, seconden: int = 20) -> dict:
    """Wacht tot het bericht bij de sink staat en geef het detail terug."""
    einde = time.monotonic() + seconden
    while time.monotonic() < einde:
        lijst = requests.get(f"{api}/api/v1/messages", timeout=10).json()
        for samenvatting in lijst.get("messages", []):
            if samenvatting.get("Subject") == onderwerp:
                return requests.get(f"{api}/api/v1/message/{samenvatting['ID']}", timeout=10).json()
        time.sleep(1)
    raise SystemExit(f"FOUT: geen bericht met onderwerp {onderwerp!r} bij de sink binnen {seconden}s")


def _headers(api: str, bericht_id: str) -> dict:
    return requests.get(f"{api}/api/v1/message/{bericht_id}/headers", timeout=10).json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--relay-host", default="127.0.0.1")
    parser.add_argument("--relay-port", type=int, default=1587)
    parser.add_argument("--api", default="http://127.0.0.1:8025", help="Mailpit-API van de sink")
    parser.add_argument("--user", required=True, help="SMTP-account op de relay")
    parser.add_argument("--password", required=True)
    args = parser.parse_args()

    stempel = str(int(time.time()))
    gevallen = [
        ("met weergavenaam", "Iemand Anders <spoof@evil.example>", "Iemand Anders"),
        ("zonder weergavenaam", "<spoof@evil.example>", None),
        ("kaal adres", "spoof@evil.example", None),
    ]

    fouten: list[str] = []
    for naam, from_header, verwachte_naam in gevallen:
        onderwerp = f"identiteitstoets {naam} {stempel}"
        _stuur(args.relay_host, args.relay_port, args.user, args.password, from_header, onderwerp)
        bericht = _haal_bericht(args.api, onderwerp)

        # 1 en 2: het adres is vervangen, de weergavenaam is behouden waar hij er was.
        afzender = (bericht.get("From") or {}).get("Address", "")
        if afzender != VAST_ADRES:
            fouten.append(f"[{naam}] From-adres is {afzender!r}, verwacht {VAST_ADRES!r}")
        getoonde_naam = (bericht.get("From") or {}).get("Name") or None
        if verwachte_naam is not None and getoonde_naam != verwachte_naam:
            fouten.append(f"[{naam}] weergavenaam is {getoonde_naam!r}, verwacht {verwachte_naam!r}")

        # 3: de envelope draagt het account en blijft in hetzelfde domein.
        envelope = (bericht.get("ReturnPath") or "").strip("<>")
        verwacht = f"noreply-rijksapp+{args.user}@rijksoverheid.nl"
        if envelope != verwacht:
            fouten.append(f"[{naam}] envelope is {envelope!r}, verwacht {verwacht!r}")
        if envelope.rpartition("@")[2] != VAST_ADRES.rpartition("@")[2]:
            fouten.append(f"[{naam}] envelope-domein wijkt af van het From-domein: SPF lijnt niet uit")

        # 4: niets van binnen het cluster gaat mee naar buiten.
        aanwezig = {k.lower() for k in _headers(args.api, bericht["ID"])}
        fouten.extend(
            f"[{naam}] header {verboden} staat er nog in"
            for verboden in VERBODEN_HEADERS
            if verboden.lower() in aanwezig
        )

        print(f"  {naam}: From={afzender!r} naam={getoonde_naam!r} envelope={envelope!r}")

    if fouten:
        print("\nNIET GOED:")
        for fout in fouten:
            print(f"  - {fout}")
        return 1
    print("\nAlle identiteitsregels doen wat ze beloven.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
