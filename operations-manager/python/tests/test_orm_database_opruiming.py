"""Achtergebleven testdatabases: de wees moet weg, de levende run moet blijven.

Waarom er uberhaupt opgeruimd moet worden: een run die hard eindigt ruimt zelf niets op.
Dat gold voor de wegwerpcontainer die er eerder per run was -- gemeten op 20 augustus
2026 stonden er elf, de oudste 45 uur, want de container draait onder de Docker-daemon
en niet onder pytest -- en het geldt nu voor de database die elke run in de gedeelde
server maakt.

Zomaar alles opruimen mag niet. Hier draaien suites naast elkaar (agents in eigen
worktrees), en een levende run zijn database onder de voeten weghalen gaf ooit veertig
fouten. Vandaar de pid: die zat eerst als etiket op de container en zit nu in de naam van
de database (``zad_test_<pid>``), en alleen wat een DODE maker heeft is een wees.

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


def test_zonder_pid_is_het_een_wees() -> None:
    """Iets van voor deze regeling draagt geen pid; die run is hoe dan ook voorbij."""
    assert _maker_leeft("") is False


def test_rommel_in_de_pid_is_een_wees() -> None:
    assert _maker_leeft("geen-getal") is False


def test_de_fixture_zet_de_eigen_pid_in_de_databasenaam() -> None:
    """De opruiming werkt alleen als de maker zijn pid ook echt achterlaat.

    Niet met een draaiende Postgres gemeten (dat kost Docker en seconden), maar op de
    bron: de fixture moet de naam uit de prefix en de eigen pid opbouwen, want dat is het
    enige waaraan een volgende run een wees herkent.
    """
    import inspect

    import conftest

    bron = inspect.getsource(conftest._orm_db_url.__wrapped__)
    assert "ZAD_TEST_DB_PREFIX" in bron, "de fixture gebruikt de prefix niet meer"
    assert "os.getpid()" in bron, "de databasenaam wordt niet met de eigen pid gevuld"
