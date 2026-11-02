"""Het logboek van de proefsandbox: opvangen tijdens de run, en per proef terugvinden
waarom OPI zwichtte.

De sandbox draait in ``proef.main()`` in hetzelfde proces als de gate, dus zijn log gaat
zonder opvang naar de stdout van de gate en is daarna niet meer per proef te scheiden. Deze
module zet er een eigen handler op, laat de proeven hun eigen venster in het logboek
markeren, en leest de regels terug voor het rapport. Elke regel gaat door
``opi.utils.logging_redact``, want een OPI-logregel kan een sleutel of een wachtwoord dragen.
"""

from __future__ import annotations

import datetime
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from opi.utils.logging_redact import SecretRedactionFilter, redact_invite_key_in_path, redact_text

from security_tests.doel import OPILOG

if TYPE_CHECKING:
    from pathlib import Path

#: De vorm van een logregel. Vervolgregels (een traceback) hebben geen kop, zodat een record
#: altijd een kopregel plus zijn vervolg is.
FORMAAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_KOP = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")

#: De markering waarmee een proef zijn eigen venster in het logboek afbakent.
MARKER = "zad.proef marker"

#: De regel die uvicorn schrijft als een verzoek met een exception eindigt.
ASGI_FOUT = "Exception in ASGI application"

#: Beide herkenningen staan vast aan de hele kop: tijd, niveau en logger erbij. Een vrije
#: zoektocht zou ze ook vinden in het PAD van een verzoek, dat van buiten komt.
_TIJDSTEMPEL = r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}"
_MARKER = re.compile(rf"^{_TIJDSTEMPEL} INFO {re.escape(MARKER)} (start|einde) (.+)$")
_ASGI = re.compile(rf"^{_TIJDSTEMPEL} ERROR uvicorn\.error {re.escape(ASGI_FOUT)}$")

#: Oorzaken die horen bij een wegwerp-sandbox zonder database en zonder echte diensten, met
#: de reden erbij. Alles daarbuiten is een bevinding: dan zwichtte OPI om een andere reden.
VERWACHTE_OORZAKEN: dict[str, str] = {
    "socket.gaierror": "de wegwerp-sandbox heeft geen database: de hostnaam ervan bestaat niet",
    "gaierror": "de wegwerp-sandbox heeft geen database: de hostnaam ervan bestaat niet",
    "ConnectionRefusedError": "de wegwerp-sandbox heeft geen database: er luistert niets op die poort",
    "InvalidCatalogNameError": "de wegwerp-sandbox heeft geen database: het schema bestaat niet",
}

#: Exceptievormen die zelf niets over de oorzaak zeggen. Een blok met alleen wrappers en een
#: verwachte oorzaak blijft verwacht; een wrapper zonder verwachte oorzaak is geen verklaring.
WRAPPERS = frozenset({"ExceptionGroup", "BaseExceptionGroup", "OSError", "ConnectionError"})

_EXCEPTIE = re.compile(r"^[\s|+\-]*([A-Za-z_][\w.]*(?:Error|Exception|ExceptionGroup|gaierror))\b")
_VERZOEK = re.compile(r"verzoek (\w+) (\S+)")

#: Hoeveel logregels een bevinding meeneemt, en hoeveel records ervoor als context mee gaan.
MAX_REGELS = 40
CONTEXT_RECORDS = 2

#: De niveaus die zeggen dat OPI iets niet aankon.
_ZWICHT = frozenset({"WARNING", "ERROR", "CRITICAL"})


@dataclass(frozen=True)
class Record:
    """Een logregel met zijn vervolgregels, zoals een traceback onder een ERROR."""

    kop: str
    vervolg: tuple[str, ...] = ()

    @property
    def regels(self) -> tuple[str, ...]:
        return (self.kop, *self.vervolg)

    @property
    def tijd(self) -> str:
        velden = self.kop.split(" ")
        return " ".join(velden[:2])

    @property
    def niveau(self) -> str:
        velden = self.kop.split(" ")
        return velden[2] if len(velden) > 2 else ""

    @property
    def tekst(self) -> str:
        return "\n".join(self.regels)


@dataclass(frozen=True)
class AsgiFout:
    """Een ``Exception in ASGI application`` met het oordeel erover."""

    proef: str
    tijd: str
    route: str
    oorzaken: tuple[str, ...]
    verwacht: bool
    reden: str

    @property
    def oordeel(self) -> str:
        return "verwachte uitkomst van de wegwerp-sandbox" if self.verwacht else "BEVINDING"


def vang_op(pad: Path = OPILOG) -> logging.Handler:
    """Begin een vers logboek en hang een handler aan de loggers die OPI en uvicorn gebruiken.

    ``uvicorn.error`` staat op ``propagate = False``, dus een handler op de root logger ziet
    de ASGI-tracebacks niet; die logger krijgt hem daarom apart.
    """
    pad.parent.mkdir(parents=True, exist_ok=True)
    pad.write_text("")
    bestandshandler = logging.FileHandler(pad, encoding="utf-8")
    # INFO en niet DEBUG: de stortvloedproef doet duizenden verzoeken, en op DEBUG groeit het
    # logboek daarmee tot honderden megabytes zonder dat het meer verklaart.
    bestandshandler.setLevel(logging.INFO)
    bestandshandler.setFormatter(logging.Formatter(FORMAAT))
    bestandshandler.addFilter(SecretRedactionFilter())
    for naam in ("", "uvicorn.error"):
        logging.getLogger(naam).addHandler(bestandshandler)
    return bestandshandler


def laat_los(bestandshandler: logging.Handler) -> None:
    """Haal de handler weer van de loggers en sluit het bestand."""
    for naam in ("", "uvicorn.error"):
        logging.getLogger(naam).removeHandler(bestandshandler)
    bestandshandler.close()


def een_regel(tekst: str) -> str:
    """Tekst van buiten als een enkele logregel. Een regeleinde erin, met de kop uit
    ``FORMAAT`` erachter, schrijft een markering of een ASGI-fout die niemand veroorzaakte."""
    return tekst.replace("\r", "\\r").replace("\n", "\\n")


def markeer(nodeid: str, fase: str, pad: Path = OPILOG) -> None:
    """Zet het begin of het einde van een proef in het logboek.

    De fuzzers draaien in een eigen pytest-proces, dus de markering gaat er met een losse
    append in; zonder die vensters is een logregel niet aan een bevinding te knopen.
    """
    if not pad.parent.exists():
        return
    # Lokale tijd, want dat is wat ``asctime`` in de logregels van OPI zet.
    nu = datetime.datetime.now(tz=datetime.UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
    with pad.open("a", encoding="utf-8") as bestand:
        bestand.write(f"{nu} INFO {MARKER} {fase} {een_regel(nodeid)}\n")


def lees(pad: Path = OPILOG) -> list[Record]:
    """Het logboek als records, geredigeerd."""
    if not pad.exists():
        return []
    records: list[Record] = []
    vervolg: list[str] = []
    for regel in pad.read_text(encoding="utf-8", errors="replace").splitlines():
        schoon = redact_invite_key_in_path(redact_text(regel))
        if _KOP.match(schoon):
            records.append(Record(schoon))
            vervolg = []
            continue
        if records:
            vervolg.append(schoon)
            records[-1] = Record(records[-1].kop, tuple(vervolg))
    return records


def vensters(records: list[Record]) -> dict[str, list[Record]]:
    """Per proef de records die tussen zijn start- en eindmarkering staan."""
    per_proef: dict[str, list[Record]] = {}
    actief: str | None = None
    for record in records:
        gevonden = _MARKER.match(record.kop)
        if gevonden:
            fase, nodeid = gevonden.groups()
            actief = nodeid if fase == "start" else None
            per_proef.setdefault(nodeid, [])
            continue
        if actief is not None:
            per_proef[actief].append(record)
    return per_proef


def _module_en_klassen(classname: str) -> tuple[list[str], list[str]]:
    """De classname van junit gesplitst in paddelen en klassen.

    Pytest collecteert alleen klassen met het prefix Test, dus daar begint het klassedeel.
    """
    delen = [deel for deel in classname.split(".") if deel]
    grens = next((plek for plek, deel in enumerate(delen) if deel.startswith("Test")), len(delen))
    return delen[:grens], delen[grens:]


def bestand_van(classname: str) -> str:
    """Het toetsbestand uit een junit-classname, zonder de klassen erachter."""
    module, _ = _module_en_klassen(classname)
    return module[-1] if module else ""


def toetsnaam(classname: str, naam: str) -> str:
    """De toets zoals hij in de groene lijst staat: bestand, eventuele klasse, en de toets."""
    _, klassen = _module_en_klassen(classname)
    return "::".join([bestand_van(classname), *klassen, naam])


def junit_sleutel(classname: str, naam: str) -> str:
    """De nodeid waarmee een junit-testcase in de vensters terug te vinden is."""
    module, klassen = _module_en_klassen(classname)
    return "::".join(["/".join(module) + ".py", *klassen, naam])


def waarom(records: list[Record]) -> tuple[str, ...]:
    """De logregels die zeggen waarom OPI zwichtte: elk WARNING of erger, met context ervoor."""
    gekozen: list[int] = []
    for nummer, record in enumerate(records):
        if record.niveau not in _ZWICHT:
            continue
        gekozen += [plek for plek in range(max(0, nummer - CONTEXT_RECORDS), nummer + 1) if plek not in gekozen]
    regels: list[str] = []
    for plek in sorted(gekozen):
        regels += records[plek].regels
    if len(regels) > MAX_REGELS:
        weg = len(regels) - MAX_REGELS
        regels = [*regels[: MAX_REGELS // 2], f"... {weg} regels ingekort ...", *regels[-(MAX_REGELS // 2) :]]
    return tuple(regels)


def route_uit(records: list[Record]) -> str:
    """De laatste route die in deze records omviel, zoals ``ProefLogApp`` hem schrijft."""
    for record in reversed(records):
        gevonden = _VERZOEK.search(record.kop)
        if gevonden:
            return f"{gevonden.group(1)} {gevonden.group(2)}"
    return "onbekend"


def oorzaken_uit(record: Record) -> tuple[str, ...]:
    """De exceptievormen in de traceback onder deze logregel, in volgorde van voorkomen."""
    namen: list[str] = []
    for regel in record.vervolg:
        gevonden = _EXCEPTIE.match(regel)
        if gevonden and gevonden.group(1) not in namen:
            namen.append(gevonden.group(1))
    return tuple(namen)


def beoordeel(oorzaken: tuple[str, ...]) -> tuple[bool, str]:
    """Of deze oorzaken bij een wegwerp-sandbox horen, en waarom."""
    verwacht = [naam for naam in oorzaken if naam in VERWACHTE_OORZAKEN]
    onbekend = [naam for naam in oorzaken if naam not in VERWACHTE_OORZAKEN and naam not in WRAPPERS]
    if verwacht and not onbekend:
        return True, VERWACHTE_OORZAKEN[verwacht[0]]
    if not oorzaken:
        return False, "geen traceback in het logboek, dus de oorzaak is niet vast te stellen"
    return False, f"{', '.join(onbekend or oorzaken)} hoort niet bij een ontbrekende dienst in de wegwerp-sandbox"


def asgi_fouten(records: list[Record]) -> list[AsgiFout]:
    """Elke ``Exception in ASGI application`` met een uitspraak erover."""
    fouten: list[AsgiFout] = []
    actief = "buiten een proefvenster"
    gezien: list[Record] = []
    for record in records:
        gevonden = _MARKER.match(record.kop)
        if gevonden:
            fase, nodeid = gevonden.groups()
            actief = nodeid if fase == "start" else "buiten een proefvenster"
            gezien = []
            continue
        gezien.append(record)
        if not _ASGI.match(record.kop):
            continue
        oorzaken = oorzaken_uit(record)
        verwacht, reden = beoordeel(oorzaken)
        fouten.append(
            AsgiFout(
                proef=actief,
                tijd=record.tijd,
                route=route_uit(gezien),
                oorzaken=oorzaken,
                verwacht=verwacht,
                reden=reden,
            )
        )
    return fouten


def laatste_regels(pad: Path = OPILOG, aantal: int = 20) -> tuple[str, ...]:
    """De laatste regels van het logboek, voor een crash waarbij geen proef meer meldt."""
    records = lees(pad)
    regels: list[str] = []
    for record in records:
        regels += record.regels
    return tuple(regels[-aantal:])
