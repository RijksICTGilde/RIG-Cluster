"""Achtergebleven testdatabases: de wees moet weg, de levende run moet blijven.

Gemeten op 20 augustus 2026: elf draaiende postgres:16-alpine-containers, de oudste 45
uur. De vorige opruimer pakte alleen ``status=exited``, op de aanname dat een gekilde
run zijn container gestopt achterlaat. Dat is niet zo: de container draait onder de
Docker-daemon en niet onder pytest, dus een SIGKILL op de suite laat de Postgres gewoon
doorlopen. Nagespeeld met een echte kill: de container bleef vrolijk staan.

Zomaar alle draaiende containers opruimen mag ook niet. Hier draaien suites naast elkaar
(agents in eigen worktrees), en een levende run zijn database onder de voeten weghalen
gaf ooit veertig fouten. Vandaar het pid-etiket: elke container draagt de pid van zijn
maker, en alleen een container met een DODE maker is een wees.

Deze tests draaien zonder Docker; ze toetsen de beslislogica, niet de dockeraanroep.
"""

from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
from conftest import _maker_leeft


def test_de_eigen_pid_leeft() -> None:
    assert _maker_leeft(str(os.getpid())) is True


def test_een_dode_pid_is_een_wees() -> None:
    # Een net gestorven kindproces: pid bestond zojuist gegarandeerd, en is nu zeker weg.
    kind = subprocess.Popen([sys.executable, "-c", "pass"])
    kind.wait()
    assert _maker_leeft(str(kind.pid)) is False


def test_zonder_etiket_is_het_een_wees() -> None:
    """Containers van voor deze regeling hebben geen pid-etiket; hun run is hoe dan ook voorbij."""
    assert _maker_leeft("") is False


def test_rommel_in_het_etiket_is_een_wees() -> None:
    assert _maker_leeft("geen-getal") is False


def test_de_fixture_zet_het_pid_etiket() -> None:
    """De opruiming werkt alleen als de maker zijn pid ook echt op de container zet.

    Niet met een draaiende container gemeten (dat kost Docker en seconden), maar op de
    bron: de fixturecode moet het etiket met de eigen pid vullen.
    """
    import inspect

    import conftest

    bron = inspect.getsource(conftest._orm_pg_container.__wrapped__)
    assert "ORM_CONTAINER_PID_LABEL" in bron, "de fixture zet het pid-etiket niet meer"
    assert "os.getpid()" in bron, "het etiket wordt niet met de eigen pid gevuld"
