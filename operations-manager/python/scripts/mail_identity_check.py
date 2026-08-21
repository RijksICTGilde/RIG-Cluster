#!/usr/bin/env python3
"""RC-114/RC-145: toetst de identiteitsregels van de mailrelay tegen een draaiende sandbox.

Waarom dit bestaat: de regels waarop het hele send-email-ontwerp rust zijn regels in een
sieve-script, en een sieve-script dat stilletjes niets doet ziet er precies zo uit als een
sieve-script dat werkt. De rest van de relayconfiguratie is destijds met de hand nagespeeld
tegen een draaiende Stalwart; het OVERSCHRIJVEN van de From: niet. Dit script maakt daar
een assertie van.

Sinds RC-145 is de From: HELEMAAL van het platform - adres en weergavenaam allebei. Het
toetst dus vijf dingen, en alle vijf zijn ze eerder stuk geweest of ongemeten:

1. Het afzenderADRES is dat van dit account, wat de applicatie ook aanbiedt.
2. De weergavenaam is die uit de projectconfiguratie, ook wanneer de applicatie zelf een
   naam meestuurt. Dat laatste is het geval dat tot RC-145 juist ANDERS liep: de naam van
   de applicatie bleef staan, dus `e2e-allservices` stond boven de post van elk project.
3. De envelope is HETZELFDE adres als de From:. Dit is de belangrijkste:
   `rijksoverheid.nl` publiceert p=reject en wij ondertekenen niet met DKIM, dus
   SPF-uitlijning tussen envelope en From: is het ENIGE dat een bericht door DMARC krijgt.
4. De Reply-To: van de applicatie komt ONGEWIJZIGD aan. Dat is de scheiding waar het
   ontwerp op staat: de From: is identiteit en ligt vast, de Reply-To: zegt alleen waar
   een antwoord heen moet en is dus wel van de applicatie.
5. De Received-keten en de verklikkerheaders zijn eraf.

Draaien tegen de sandbox, met twee port-forwards open:

    kubectl -n rig-ron port-forward svc/rig-mail-relay 1587:587 &
    kubectl -n rig-ron port-forward svc/rig-mail-sink 8025:8025 &
    cd operations-manager/python
    uv run python scripts/mail_identity_check.py --user project-ai1-uit --password <geheim> \\
        --verwacht-adres noreply-rijksapp+ai1-uit@rijksoverheid.nl \\
        --verwacht-naam "Robbert Uittenbroek"

De inloggegevens zijn die van een send-email-account; die van ZAD zelf staan in de Secret
uit MAIL_PLATFORM_SECRET_NAME in de namespace van OPI. Zonder --verwacht-adres toetst het
script de TERUGVAL: een account waarvoor de relay geen afzender houdt, hoort onder het kale
platformadres en zonder naam te vertrekken.
"""

from __future__ import annotations

import argparse
import smtplib
import sys
import time
from email.message import EmailMessage

import requests

#: Het kale afzenderadres van het platform. Sinds RC-145 is dit niet meer het adres dat een
#: project gebruikt (dat draagt de projectnaam in het plusdeel) maar de TERUGVAL: hier komt
#: post terecht van een account waarvoor de relay geen afzender houdt.
KAAL_ADRES = "noreply-rijksapp@rijksoverheid.nl"

#: Het antwoordadres dat de proefberichten meesturen. Het hoort ongewijzigd aan te komen.
ANTWOORDADRES = "antwoord@applicatie.example"

#: Waaraan de Received-regel van de ONTVANGER te herkennen is: elke MTA noemt zichzelf
#: achter "by", en de ontvanger is hier de sink. Zonder deze eis zou een keten van precies
#: een regel ook slagen als die regel van de relay zelf was - zie received_fouten().
ONTVANGER_IN_RECEIVED = "by rig-mail-sink"

#: Headers die er onder geen beding uit mogen komen: ze dragen het account en de client.
VERBODEN_HEADERS = (
    "X-Originating-IP",
    "X-Authenticated-Sender",
    "X-Mailer",
    "X-Originating-Client",
)


def received_fouten(ontvangen: list[str]) -> list[str]:
    """Toetst de Received-keten van een bericht dat bij de sink is aangekomen.

    Waarom dit niet gewoon "er mag geen Received in staan" is: de ONTVANGENDE server zet
    er zelf een. Dat doet elke MTA, het gebeurt NA het moment dat de relay het bericht uit
    handen geeft, en geen enkele relay kan dat wegnemen. Een toets die op de kale
    aanwezigheid van Received afgaat, keurt daarom ook een relay af die precies doet wat
    hij moet doen - gemeten op 19 augustus 2026, toen de drie andere regels al klopten.

    Wat de regel WEL betekent: de relay geeft zijn EIGEN keten niet door. De hop waarmee
    de applicatie bij de relay binnenkwam draagt het pod-IP en de namespace van de
    inzender, en die hoort eraf. Blijft die staan, dan staan er twee.

    Tellen alleen is niet genoeg: gaf de relay precies een eigen hop door en schreef de
    ontvanger er geen, dan is het er ook een. Daarom moet die ene regel aantoonbaar van de
    ONTVANGER zijn - hij noemt zichzelf achter "by". Gemeten op de sandbox op 19 augustus
    2026: "... by rig-mail-sink-6c6d994744-qzqp9 (Mailpit) with SMTP for <...>".
    """
    if len(ontvangen) != 1:
        return [f"Received-keten telt {len(ontvangen)} regels, verwacht alleen die van de ontvanger: {ontvangen}"]
    if ONTVANGER_IN_RECEIVED not in ontvangen[0]:
        return [
            f"de enige Received-regel is niet die van de ontvanger "
            f"(geen {ONTVANGER_IN_RECEIVED!r} erin): {ontvangen[0]!r}"
        ]
    return []


def _stuur(host: str, poort: int, user: str, password: str, from_header: str, onderwerp: str, ontvanger: str) -> None:
    """Biedt een bericht aan met een expres afwijkende From:."""
    bericht = EmailMessage()
    bericht["Subject"] = onderwerp
    # Een EIGEN ontvanger per bericht, want Stalwart telt standaard 25 berichten per uur per
    # (afzenderdomein, ontvanger) - `queue.limiter.inbound.sender`. Met een vaste ontvanger
    # loopt deze toets bij de derde keer draaien vast op "452 4.4.5 Rate limit exceeded", en
    # dat leest als een storing terwijl het de teller van de toets zelf is.
    bericht["To"] = ontvanger
    # Precies het punt van de toets: dit adres MOET verdwijnen, en sinds RC-145 ook de
    # weergavenaam die eraan vastzit.
    bericht["From"] = from_header
    # En dit MOET juist blijven staan.
    bericht["Reply-To"] = ANTWOORDADRES
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
    parser.add_argument(
        "--verwacht-adres",
        default=KAAL_ADRES,
        help="Het adres dat in From: en Return-Path hoort te staan. Standaard het kale "
        "platformadres, wat de terugval toetst voor een account zonder afzender.",
    )
    parser.add_argument(
        "--verwacht-naam",
        default="",
        help="De weergavenaam die de relay ernaast hoort te zetten. Leeg is een geldige "
        "uitkomst: dan verstuurt het project met een kaal adres en zonder naam.",
    )
    args = parser.parse_args()

    stempel = str(int(time.time()))
    # Alle drie de gevallen bieden een andere From: aan, en alle drie horen ze dezelfde
    # afzender op te leveren. Het eerste geval is het geval dat tot RC-145 anders liep.
    gevallen = [
        ("applicatie zet een eigen naam", "Applicatienaam <spoof@evil.example>"),
        ("zonder weergavenaam", "<spoof@evil.example>"),
        ("kaal adres", "spoof@evil.example"),
    ]

    fouten: list[str] = []
    for volgnummer, (naam, from_header) in enumerate(gevallen):
        onderwerp = f"identiteitstoets {naam} {stempel}"
        ontvanger = f"ontvanger-{stempel}-{volgnummer}@example.org"
        _stuur(args.relay_host, args.relay_port, args.user, args.password, from_header, onderwerp, ontvanger)
        bericht = _haal_bericht(args.api, onderwerp)

        # 1 en 2: adres en weergavenaam komen allebei van het platform. Wat de applicatie
        # aanbood is weg, ook de naam.
        afzender = (bericht.get("From") or {}).get("Address", "")
        if afzender != args.verwacht_adres:
            fouten.append(f"[{naam}] From-adres is {afzender!r}, verwacht {args.verwacht_adres!r}")
        getoonde_naam = (bericht.get("From") or {}).get("Name") or ""
        if getoonde_naam != args.verwacht_naam:
            fouten.append(f"[{naam}] weergavenaam is {getoonde_naam!r}, verwacht {args.verwacht_naam!r}")

        # 3: de envelope is hetzelfde adres als de From:, dus SPF lijnt per definitie uit.
        envelope = (bericht.get("ReturnPath") or "").strip("<>")
        if envelope != args.verwacht_adres:
            fouten.append(f"[{naam}] envelope is {envelope!r}, verwacht {args.verwacht_adres!r}")

        headers = _headers(args.api, bericht["ID"])
        aanwezig = {k.lower(): v for k, v in headers.items()}

        # 4: de Reply-To blijft van de applicatie.
        antwoord = [waarde.strip("<> ") for waarde in aanwezig.get("reply-to", [])]
        if antwoord != [ANTWOORDADRES]:
            fouten.append(f"[{naam}] Reply-To is {antwoord!r}, verwacht [{ANTWOORDADRES!r}] ongewijzigd")

        # 5: niets van binnen het cluster gaat mee naar buiten.
        fouten.extend(
            f"[{naam}] header {verboden} staat er nog in"
            for verboden in VERBODEN_HEADERS
            if verboden.lower() in aanwezig
        )
        fouten.extend(f"[{naam}] {fout}" for fout in received_fouten(aanwezig.get("received", [])))

        print(f"  {naam}: From={afzender!r} naam={getoonde_naam!r} envelope={envelope!r} reply-to={antwoord!r}")

    if fouten:
        print("\nNIET GOED:")
        for fout in fouten:
            print(f"  - {fout}")
        return 1
    print("\nAlle identiteitsregels doen wat ze beloven.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
